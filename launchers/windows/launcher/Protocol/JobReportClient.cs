// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
namespace S2Arm.Launcher.Protocol;

/// <summary>最近一次失败作业的错误摘要（pack/jobs/report.py）。作业层起不来或没有记录时返回 null，由界面退回基本信息。</summary>
public static class JobReportClient
{
    public static async Task<string?> RunAsync(LauncherPaths paths, string privateRoot, string? gameId, IReadOnlyList<string> operations, string extraJson)
    {
        var info = EnvironmentProbe.StartInfo(paths, Path.Combine(privateRoot, "report"));
        info.Environment["S2A_UI_PRIVATE"] = Path.GetFullPath(privateRoot);
        info.Environment["S2A_UI_GAME"] = gameId ?? "";
        info.Environment["S2A_UI_OPERATIONS"] = string.Join(",", operations);
        info.Environment["S2A_REPORT_EXTRA"] = extraJson;
        var game = gameId is null ? "" : " --game \"%S2A_UI_GAME%\"";
        var scope = operations.Count == 0 ? "" : " --operations \"%S2A_UI_OPERATIONS%\"";
        info.Arguments = "/d /v:off /s /c \"\"%S2A_UI_RUNTIME%\\s2arm_env.cmd\" && cd /d \"%S2A_UI_ENGINE%\" && python -X utf8 pack/jobs/main.py report --private-root \"%S2A_UI_PRIVATE%\"" + game + scope + "\"";
        using var limit = new CancellationTokenSource(TimeSpan.FromSeconds(30));
        try
        {
            var output = await EnvironmentProbe.RunProcessAsync(info, limit.Token, 2 * 1024 * 1024);
            return output.ExitCode == 0 && output.StandardOutput.Length > 0 ? output.StandardOutput : null;
        }
        catch (Exception error) when (error is IOException or OperationCanceledException or System.ComponentModel.Win32Exception or InvalidOperationException) { return null; }
    }
}
