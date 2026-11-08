// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using System.Diagnostics;
using System.Text.Json;

namespace S2Arm.Launcher.Protocol;

/// <summary>覆盖更新的进程调用（下载、校验、替换、回滚都在 pack/jobs/updater.py）。只用随包 Python 的标准库，不需要运行环境脚本。</summary>
public static class UpdateClient
{
    /// <summary>安装包打进去的版本号（bundle_win.py 写）；开发版没有这个文件，就不提示更新。</summary>
    public static string CurrentVersion(string installDir)
    {
        try { return File.ReadAllText(Path.Combine(installDir, "version.txt")).Trim(); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { return ""; }
    }

    private static ProcessStartInfo Start(string python, IEnumerable<string> arguments)
    {
        var info = new ProcessStartInfo(python)
        {
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = System.Text.Encoding.UTF8
        };
        foreach (var argument in new[] { "-X", "utf8", "-B", "-I" }.Concat(arguments)) info.ArgumentList.Add(argument);
        return info;
    }

    /// <summary>跑一条 updater 命令，逐行交 JSON，返回最后一行；起不来或超时返回 null。</summary>
    public static async Task<JsonElement?> RunAsync(LauncherPaths paths, IEnumerable<string> command, Action<JsonElement>? onEvent, TimeSpan limit)
    {
        var python = Path.Combine(Path.GetFullPath(paths.Runtime), "python", "python.exe");
        var updater = Path.Combine(Path.GetFullPath(paths.Engine), "pack", "jobs", "updater.py");
        JsonElement? last = null;
        using var cancel = new CancellationTokenSource(limit);
        try
        {
            using var process = Process.Start(Start(python, new[] { updater }.Concat(command)))!;
            process.StandardInput.Close();
            var drain = process.StandardError.ReadToEndAsync(cancel.Token);
            while (await process.StandardOutput.ReadLineAsync(cancel.Token) is { } line)
            {
                try { using var document = JsonDocument.Parse(line); last = document.RootElement.Clone(); }
                catch (JsonException) { continue; }
                onEvent?.Invoke(last.Value);
            }
            await process.WaitForExitAsync(cancel.Token);
            await drain;
            return last;
        }
        catch (Exception error) when (error is IOException or OperationCanceledException or System.ComponentModel.Win32Exception or InvalidOperationException) { return last; }
    }

    /// <summary>起独立的替换进程：它等本进程退出后换掉安装目录并重启，用的是 updates 里的 Python，不占安装目录。</summary>
    public static void Apply(JsonElement ready, string installDir, string privateRoot, IEnumerable<string> relaunchArguments)
    {
        var info = new ProcessStartInfo(ready.GetProperty("python").GetString()!) { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Path.GetTempPath() };
        foreach (var argument in new[] { "-X", "utf8", "-B", "-I", ready.GetProperty("updater").GetString()!, "apply",
                     "--staged", ready.GetProperty("staged").GetString()!, "--install", installDir, "--pid", Environment.ProcessId.ToString(), "--private-root", privateRoot })
            info.ArgumentList.Add(argument);
        foreach (var argument in relaunchArguments) info.ArgumentList.Add("--arg=" + argument);
        Process.Start(info)?.Dispose();
    }
}
