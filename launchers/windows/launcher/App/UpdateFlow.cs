// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using System.Text.Json;
using Avalonia.Controls;
using Avalonia.Media;
using Avalonia.Threading;
using S2Arm.Launcher.Protocol;

namespace S2Arm.Launcher;

// 覆盖更新：下载、校验、替换、回滚都在共用的 pack/jobs/updater.py，这里只管状态和按钮。出包等作业进行中不提示、不替换。
public sealed partial class EnvironmentWindow
{
    private string updateProgress = "";
    private JsonElement? updateReady;
    private string updateFailure = "";   // 错误码
    private static string InstallDir => Path.TrimEndingDirectorySeparator(AppContext.BaseDirectory);
    // The release version lives in version.txt (written by bundle_win.py); the assembly version is always 1.0.0.0.
    private static string AppVersion => UpdateClient.CurrentVersion(InstallDir) is { Length: > 0 } version ? version : "-";
    private string UpdatesFolder => Path.Combine(privateRoot, "updates");
    private bool UpdateBlocked => PackBusy || libraryBusy || importBusy || busy;

    private Dictionary<string, object?> UpdateStatus => updateProgress.Length > 0
        ? new() { ["state"] = "working", ["detail"] = updateProgress }
        : updateReady is { } ready ? new() { ["state"] = "ready", ["detail"] = ready.GetProperty("version").GetString(), ["via"] = ready.GetProperty("via").GetString() }
        : updateFailure.Length > 0 ? new() { ["state"] = "failed", ["detail"] = updateFailure }
        : new() { ["state"] = "idle" };

    private void FinishPendingUpdate()
    {
        try
        {
            if (File.Exists(Path.Combine(UpdatesFolder, "pending.json"))) File.WriteAllText(Path.Combine(UpdatesFolder, "health.ok"), "ok");
            var failure = Path.Combine(UpdatesFolder, "last_failure.json");
            if (File.Exists(failure))
            {
                using var document = JsonDocument.Parse(File.ReadAllText(failure));
                updateFailure = document.RootElement.GetProperty("code").GetString() ?? "update_failed";
                File.Delete(failure);
            }
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException or KeyNotFoundException) { }
    }

    private async Task CheckForUpdateAsync()
    {
        var current = UpdateClient.CurrentVersion(InstallDir);
        if (current.Length == 0 || EnvironmentProbe.BundleIncomplete(paths)) return;
        var result = await UpdateClient.RunAsync(paths, ["check", "--platform", "windows", "--current", current], null, TimeSpan.FromSeconds(20));
        if (result is { } value && value.TryGetProperty("available", out var available) && available.GetBoolean())
            await DownloadUpdateAsync();   // 后台下好，不打断玩家
    }

    private async Task DownloadUpdateAsync()
    {
        if (updateProgress.Length > 0) return;
        updateFailure = ""; updateProgress = T("update.downloading").Replace("{0}", "0"); Render();
        var command = new List<string> { "prepare", "--platform", "windows", "--current", UpdateClient.CurrentVersion(InstallDir), "--private-root", privateRoot, "--install", InstallDir };
        if (connectedFolder is not null) { command.Add("--protect"); command.Add(connectedFolder); }
        var result = await UpdateClient.RunAsync(paths, command, e => Dispatcher.UIThread.Post(() =>
        {
            switch (e.GetProperty("state").GetString())
            {
                case "downloading":
                    var total = e.GetProperty("total").GetInt64();
                    updateProgress = T("update.downloading").Replace("{0}", total > 0 ? (e.GetProperty("done").GetInt64() * 100 / total).ToString() : "0");
                    Render(); break;
                case "verifying": updateProgress = T("update.verifying"); Render(); break;
                case "unpacking": updateProgress = T("update.unpacking"); Render(); break;
            }
        }), TimeSpan.FromHours(2));
        updateProgress = "";
        if (result is { } ready && ready.GetProperty("state").GetString() == "ready")
            updateReady = ready;
        else updateFailure = (result is { } failed && failed.TryGetProperty("code", out var code) ? code.GetString() : null) ?? "update_failed";
        Render();
    }

    private void RestartToUpdate()
    {
        if (updateReady is not { } ready || UpdateBlocked) return;
        try
        {
            UpdateClient.Apply(ready, InstallDir, privateRoot, Environment.GetCommandLineArgs().Skip(1));
            Close();
        }
        catch (Exception error) when (error is IOException or InvalidOperationException or System.ComponentModel.Win32Exception or KeyNotFoundException)
        { updateFailure = "update_failed"; updateReady = null; Render(); }
    }

    private void AddUpdateControls(StackPanel bar)
    {
        if (updateProgress.Length > 0)
            bar.Children.Add(new TextBlock { Text = updateProgress, FontSize = 12, Foreground = Brush.Parse("#A8A6A1"), VerticalAlignment = Avalonia.Layout.VerticalAlignment.Center });
        else if (updateReady is not null)   // 只有玩家点了才重启；作业进行中置灰并说明
            bar.Children.Add(Action(UpdateBlocked ? "update.restart.busy" : "update.restart", RestartToUpdate, !UpdateBlocked));
        else if (updateFailure.Length > 0)
        {
            var retry = Action("update.retry", () => _ = DownloadUpdateAsync());
            ToolTip.SetTip(retry, T("update.error." + updateFailure));
            bar.Children.Add(retry);
        }
    }
}
