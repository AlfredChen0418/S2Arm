// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using System.Collections.ObjectModel;
using System.Text.Json;

namespace S2Arm.Launcher.Protocol;

public sealed class EnvironmentProtocolException(string message, Exception? inner = null)
    : Exception(message, inner);

public sealed record EnvironmentCheck(
    string Id, string Name, string Status, string Message, string? Code, string? Action, Uri? OfficialUrl, bool Blocking = false)
{
    // Recovery text is displayed verbatim; it is never interpreted as a command or URL.
    public bool HasAction => !string.IsNullOrWhiteSpace(Action);
}

public sealed record EnvironmentError(string Code, string Message);

public sealed record EnvironmentReport(
    bool Ready, ReadOnlyCollection<EnvironmentCheck> Checks, EnvironmentError? Error)
{
    public static EnvironmentReport Parse(string json, int exitCode)
    {
        try
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            ValidateUniqueFields(root);
            if (root.ValueKind != JsonValueKind.Object ||
                !root.GetProperty("schema").TryGetInt32(out var schema) || schema != 1)
                throw new EnvironmentProtocolException("Unsupported environment report schema.");

            var ready = root.GetProperty("ready").GetBoolean();
            var checksElement = root.GetProperty("checks");
            if (checksElement.ValueKind != JsonValueKind.Array)
                throw new EnvironmentProtocolException("Environment checks must be an array.");

            var checks = new List<EnvironmentCheck>();
            var ids = new HashSet<string>(StringComparer.Ordinal);
            foreach (var item in checksElement.EnumerateArray())
            {
                var id = RequiredText(item, "id");
                if (!ids.Add(id)) throw new EnvironmentProtocolException("Duplicate environment check id.");
                var status = RequiredText(item, "status");
                if (status is not ("ok" or "missing" or "mismatch" or "unverified" or "error" or "user-selected"))
                    throw new EnvironmentProtocolException("Unsupported environment check status.");
                if (item.GetProperty("ok").GetBoolean() != (status == "ok"))
                    throw new EnvironmentProtocolException("Environment check status disagrees with ok.");
                var code = NullableText(item, "code");
                var action = NullableText(item, "action");
                if (status == "ok" ? code is not null || action is not null :
                    string.IsNullOrWhiteSpace(code) || string.IsNullOrWhiteSpace(action))
                    throw new EnvironmentProtocolException("Environment recovery fields disagree with status.");
                var url = OfficialUrl(item);
                if (status == "ok" && url is not null)
                    throw new EnvironmentProtocolException("Successful environment check has an official action.");
                checks.Add(new EnvironmentCheck(id, RequiredText(item, "name"), status,
                    RequiredText(item, "message"), code, action, url,
                    item.TryGetProperty("blocking", out var blocking) ? blocking.GetBoolean() : status != "ok"));
            }

            EnvironmentError? error = null;
            if (root.TryGetProperty("error", out var errorElement))
                error = new(RequiredText(errorElement, "code"), RequiredText(errorElement, "message"));

            if (error is not null)
            {
                if (exitCode != 2 || ready || checks.Count != 0)
                    throw new EnvironmentProtocolException("Invalid environment request-error envelope.");
            }
            else if (checks.Count == 0 || exitCode != (ready ? 0 : 1))
                throw new EnvironmentProtocolException("Environment report disagrees with process exit or observations.");

            return new(ready, checks.AsReadOnly(), error);
        }
        catch (Exception exception) when (exception is JsonException or InvalidOperationException or
                                         KeyNotFoundException or FormatException)
        {
            // Do not copy untrusted output or local paths into the protocol error text.
            throw new EnvironmentProtocolException("Malformed environment report.", exception);
        }
    }

    private static string RequiredText(JsonElement item, string name)
    {
        var value = item.GetProperty(name).GetString();
        return !string.IsNullOrWhiteSpace(value) ? value :
            throw new EnvironmentProtocolException("Missing environment text field.");
    }

    private static string? NullableText(JsonElement item, string name)
    {
        var element = item.GetProperty(name);
        return element.ValueKind == JsonValueKind.Null ? null : element.GetString();
    }

    private static Uri? OfficialUrl(JsonElement item)
    {
        // The backend owns the official destination. Never infer links from recovery text.
        if (!item.TryGetProperty("url", out var element) || element.ValueKind == JsonValueKind.Null)
            return null;
        if (element.ValueKind != JsonValueKind.String ||
            !Uri.TryCreate(element.GetString(), UriKind.Absolute, out var url) ||
            url.Scheme != Uri.UriSchemeHttps || url.Host.Length == 0 || url.UserInfo.Length != 0)
            throw new EnvironmentProtocolException("Invalid official download URL.");
        return url;
    }

    private static void ValidateUniqueFields(JsonElement element)
    {
        if (element.ValueKind == JsonValueKind.Object)
        {
            var names = new HashSet<string>(StringComparer.Ordinal);
            foreach (var property in element.EnumerateObject())
            {
                if (!names.Add(property.Name))
                    throw new EnvironmentProtocolException("Duplicate JSON field in environment report.");
                ValidateUniqueFields(property.Value);
            }
        }
        else if (element.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in element.EnumerateArray()) ValidateUniqueFields(item);
        }
    }
}
