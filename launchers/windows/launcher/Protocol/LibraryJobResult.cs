// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using System.Text.Json;

namespace S2Arm.Launcher.Protocol;

public sealed class LibraryProtocolException(string message, Exception? inner = null) : Exception(message, inner);

public sealed record LibraryJobResult(string State, string Message, JsonElement Result, JsonElement Diagnostics)
{
    public bool Succeeded => State == "succeeded";

    // Jobs NDJSON is not the oneclick ev/t stream. A terminal event alone is not process success.
    public static LibraryJobResult Parse(string ndjson, int exitCode, string jobId, string operation)
    {
        try
        {
            LibraryJobResult? result = null;
            long previous = 0;
            foreach (var line in ndjson.Split('\n', StringSplitOptions.RemoveEmptyEntries))
            {
                if (string.IsNullOrWhiteSpace(line)) continue;
                if (result is not null) throw new LibraryProtocolException("Event follows the terminal result.");
                using var document = JsonDocument.Parse(line);
                var e = document.RootElement;
                UniqueFields(e);
                if (e.GetProperty("schema").GetInt32() != 1 || Text(e, "jobId") != jobId ||
                    Text(e, "operation") != operation ||
                    (e.TryGetProperty("taskId", out var task) && task.GetString() != jobId))
                    throw new LibraryProtocolException("Event schema, task or operation does not match the request.");
                var seq = e.GetProperty("seq").GetInt64();
                if (seq <= previous) throw new LibraryProtocolException("Event sequence is not increasing.");
                previous = seq;
                var type = Text(e, "type");
                var state = Text(e, "state");
                if (type != "result")
                {
                    if (state != "running") throw new LibraryProtocolException("Terminal state lacks a result event.");
                    continue;
                }
                if (state is not ("succeeded" or "failed" or "cancelled"))
                    throw new LibraryProtocolException("Unknown terminal result state.");
                var payload = e.GetProperty("result");
                if (payload.ValueKind != JsonValueKind.Object) throw new LibraryProtocolException("Result must be an object.");
                result = new(state, Text(e, "message", allowEmpty: true), payload.Clone(),
                    e.TryGetProperty("diagnostics", out var diagnostics) ? diagnostics.Clone() : default);
            }
            if (result is null) throw new LibraryProtocolException("EOF without a terminal result.");
            if (exitCode != (result.State == "succeeded" ? 0 : result.State == "cancelled" ? 130 : 1))
                throw new LibraryProtocolException("Terminal result disagrees with process exit.");
            return result;
        }
        catch (Exception error) when (error is JsonException or InvalidOperationException or KeyNotFoundException or FormatException or OverflowException)
        {
            throw new LibraryProtocolException("Invalid jobs event stream.", error);
        }
    }

    internal static string Text(JsonElement value, string key, bool allowEmpty = false)
    {
        var field = value.GetProperty(key);
        if (field.ValueKind != JsonValueKind.String || (!allowEmpty && string.IsNullOrWhiteSpace(field.GetString())))
            throw new LibraryProtocolException("Expected text: " + key);
        return field.GetString()!;
    }

    internal static void UniqueFields(JsonElement value)
    {
        if (value.ValueKind == JsonValueKind.Object)
        {
            var names = new HashSet<string>(StringComparer.Ordinal);
            foreach (var property in value.EnumerateObject())
            {
                if (!names.Add(property.Name)) throw new LibraryProtocolException("Duplicate JSON field.");
                UniqueFields(property.Value);
            }
        }
        else if (value.ValueKind == JsonValueKind.Array)
            foreach (var item in value.EnumerateArray()) UniqueFields(item);
    }
}

/// <summary>One registered package in the library (base / update / dlc), mirrors GamePackage in the Mac adapter.</summary>
public sealed record GamePackage(string Type, string TitleId, int Version, string Display, long Size, string Format);

public sealed record LibraryGame(string Id, string Name, string Version, string Cover, bool Hidden,
    IReadOnlyList<string> Missing, bool IphoneReady = false, double? IphoneInstalledAt = null,
    bool IphoneFree = false, IReadOnlyList<GamePackage>? Packages = null, bool CustomName = false, bool CustomCover = false)
{
    /// <summary>NSP / XCI need prod.keys; extracted folders and NUP containers do not (mirrors GameSummary.needsKeys).</summary>
    public bool NeedsKeys => (Packages ?? []).Any(p => p.Format is not ("nup" or "dump" or "extract" or "loose"));
    /// <summary>DLC count deduplicated by title id (mirrors GameSummary.dlcCount).</summary>
    public int DlcCount => (Packages ?? []).Where(p => p.Type == "dlc")
        .Select(p => p.TitleId).Distinct(StringComparer.Ordinal).Count();
    /// <summary>Display version of the newest base / update package; null when none.</summary>
    public string? BaseVersion => PackageDisplayVersion("base");
    public string? UpdateVersion => PackageDisplayVersion("update");
    /// <summary>Total raw size of all registered packages (SHA-256 dedup not available here; uses title-id dedup per type).</summary>
    public long TotalBytes => (Packages ?? []).Sum(p => p.Size);
    private string? PackageDisplayVersion(string type) =>
        (Packages ?? []).Where(p => p.Type == type).OrderByDescending(p => p.Version).FirstOrDefault() is { } pkg
            ? (pkg.Display.Length > 0 ? pkg.Display : "v" + pkg.Version) : null;
};

public sealed record LibraryCatalog(IReadOnlyList<LibraryGame> Games, bool KeysReady, string KeysMessage)
{
    public static LibraryCatalog Parse(JsonElement result)
    {
        try
        {
            LibraryJobResult.UniqueFields(result);
            var games = new List<LibraryGame>();
            var ids = new HashSet<string>(StringComparer.Ordinal);
            foreach (var game in result.GetProperty("games").EnumerateArray())
            {
                var id = LibraryJobResult.Text(game, "id");
                if (!ids.Add(id)) throw new LibraryProtocolException("Duplicate game id.");
                var missing = game.GetProperty("missing").EnumerateArray().Select(item =>
                    item.GetString() ?? throw new LibraryProtocolException("Invalid requirement text.")).ToArray();
                // packages is optional (older catalog versions may omit it)
                var packages = new List<GamePackage>();
                if (game.TryGetProperty("packages", out var pkgsEl) && pkgsEl.ValueKind == JsonValueKind.Array)
                    foreach (var p in pkgsEl.EnumerateArray())
                    {
                        var ptype = p.TryGetProperty("type", out var t) && t.ValueKind == JsonValueKind.String ? t.GetString() ?? "" : "";
                        if (ptype.Length == 0) continue;
                        var titleId = p.TryGetProperty("titleId", out var ti) && ti.ValueKind == JsonValueKind.String ? ti.GetString() ?? "" : "";
                        var version = p.TryGetProperty("version", out var v) && v.ValueKind == JsonValueKind.Number ? v.GetInt32() : 0;
                        var display = p.TryGetProperty("display", out var d) && d.ValueKind == JsonValueKind.String ? d.GetString() ?? "" : "";
                        var size = p.TryGetProperty("size", out var sz) && sz.ValueKind == JsonValueKind.Number ? sz.GetInt64() : 0;
                        var format = p.TryGetProperty("format", out var f) && f.ValueKind == JsonValueKind.String ? f.GetString() ?? "" : "";
                        packages.Add(new(ptype, titleId, version, display, size, format));
                    }
                // iphoneInstall: { at, free } — matches the Mac adapter's LibraryProvider
                double? iphoneAt = null; var iphoneFree = false;
                if (game.TryGetProperty("iphoneInstall", out var inst) && inst.ValueKind == JsonValueKind.Object)
                {
                    if (inst.TryGetProperty("at", out var at) && at.ValueKind == JsonValueKind.Number) iphoneAt = at.GetDouble();
                    if (inst.TryGetProperty("free", out var fr) && fr.GetBoolean()) iphoneFree = true;
                }
                var iphoneReady = game.TryGetProperty("iphoneReady", out var ir) && ir.GetBoolean();
                games.Add(new(id, LibraryJobResult.Text(game, "name"),
                    LibraryJobResult.Text(game, "display", true), LibraryJobResult.Text(game, "cover", true),
                    game.GetProperty("hidden").GetBoolean(), missing, iphoneReady, iphoneAt, iphoneFree, packages.AsReadOnly(),
                    game.TryGetProperty("customName", out var customName) && customName.ValueKind == JsonValueKind.True,
                    game.TryGetProperty("customCover", out var customCover) && customCover.ValueKind == JsonValueKind.True));
            }
            var keys = result.GetProperty("keys");
            return new(games.AsReadOnly(), keys.GetProperty("ready").GetBoolean(),
                LibraryJobResult.Text(keys, "message", true));
        }
        catch (Exception error) when (error is InvalidOperationException or KeyNotFoundException)
        {
            throw new LibraryProtocolException("Invalid library catalogue.", error);
        }
    }
}

public sealed record LibraryConnection(string Folder, string Id)
{
    public static LibraryConnection Parse(JsonElement result)
    {
        try
        {
            LibraryJobResult.UniqueFields(result);
            var folder = LibraryJobResult.Text(result, "library");
            var id = LibraryJobResult.Text(result, "libraryId");
            if (!Path.IsPathFullyQualified(folder)) throw new LibraryProtocolException("Backend library path must be absolute.");
            // setup owns identity and canonicalization; existing library ids are opaque strings, not necessarily UUIDs.
            return new(Path.GetFullPath(folder), id);
        }
        catch (Exception error) when (error is InvalidOperationException or KeyNotFoundException or ArgumentException)
        { throw new LibraryProtocolException("Invalid library connection result.", error); }
    }
}
