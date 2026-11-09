// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using S2Arm.Launcher.Protocol;

namespace S2Arm.Launcher;

// 欢迎页：游戏库为空（或刚导入第一款）时显示，同时是环境检查。
// 左列：App 介绍；右列：三步（准备这台电脑 → 添加游戏 → 放入密钥）。
// 回到窗口自动重查；导入成功后右下角「一键出包」。
// Windows 版不提 Xcode / Metal / iOS 平台组件，只列后端返回的 non-ok 检查项。
public sealed partial class EnvironmentWindow
{
    private enum WelcomeStep { Pc = 0, Games = 1, Keys = 2 }

    private bool ShowsWelcome =>
        catalog == null ||
        (catalog.Games.All(g => g.Hidden) && welcomeGameId == null) ||
        welcomeGameId != null;

    // 检查本身没做成不等于环境有问题：不拦玩家，真缺东西时出包会报具体原因
    private bool EnvironmentUnknown => !busy && (report is { Error: not null } ||
        (report is null && summaryKey is "process.start_failed" or "protocol.invalid_report" or "process.interrupted" or "environment.failed"));
    private bool PcReady => report is { Ready: true } || EnvironmentUnknown;
    private bool GamesChosen => pendingImportPaths.Length > 0 || welcomeGameId != null;
    private bool KeysSettled => welcomeGameId != null ||
        (catalog?.KeysReady == true && !keysRejected) ||
        (pendingImportPaths.Length > 0 && catalog?.KeysReady == true);
    private WelcomeStep WelcomeCurrent =>
        !PcReady ? WelcomeStep.Pc : !GamesChosen ? WelcomeStep.Games : WelcomeStep.Keys;
    private bool WelcomeDone(WelcomeStep s) => s switch
    {
        WelcomeStep.Pc => PcReady,
        WelcomeStep.Games => GamesChosen,
        WelcomeStep.Keys => KeysSettled,
        _ => false
    };
    private LibraryGame? WelcomeGame => welcomeGameId is null ? null
        : catalog?.Games.FirstOrDefault(g => g.Id == welcomeGameId && !g.Hidden);

    private void RenderWelcome(StackPanel body)
    {
        var outer = new Grid { ColumnDefinitions = new ColumnDefinitions("*,580") };

        // ── 左列：介绍 ──────────────────────────────────────────────────
        var intro = new StackPanel { Spacing = 0 };
        var journey = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };
        void JourneyNode(string label) => journey.Children.Add(new TextBlock
        {
            Text = label, FontSize = 17, FontWeight = FontWeight.Light, Foreground = Accent, Opacity = 0.75,
            VerticalAlignment = VerticalAlignment.Center
        });
        void JourneyBar() => journey.Children.Add(new Border
        { Width = 40, Height = 1, Background = Accent, Opacity = 0.35, VerticalAlignment = VerticalAlignment.Center });
        JourneyNode("Switch"); JourneyBar(); JourneyNode(T("welcome.win.journey.pc")); JourneyBar(); JourneyNode("iPhone");
        intro.Children.Add(journey);
        intro.Children.Add(Spacer(34));
        intro.Children.Add(BigTitle(T("welcome.win.title")));
        intro.Children.Add(Spacer(20));
        intro.Children.Add(Lead(T("welcome.win.lead")));
        intro.Children.Add(Spacer(14));
        intro.Children.Add(new TextBlock { Text = T("welcome.soon"), FontSize = 14, FontWeight = FontWeight.SemiBold,
            Foreground = Accent, Opacity = 0.75, TextWrapping = TextWrapping.Wrap });
        intro.VerticalAlignment = VerticalAlignment.Center;
        intro.Margin = new Thickness(48);
        Grid.SetColumn(intro, 0); outer.Children.Add(intro);

        // ── 右列：三步 ──────────────────────────────────────────────────
        var steps = new StackPanel { Spacing = 0, Margin = new Thickness(0, 44, 48, 44) };
        steps.Children.Add(WelcomeStepRow(WelcomeStep.Pc,
            T(PcReady ? "welcome.win.step.pc.done" : "welcome.win.step.pc.title"), false, PcContent()));
        steps.Children.Add(WelcomeStepRow(WelcomeStep.Games,
            T(GamesChosen ? "welcome.step.games.done" : "welcome.step.games.title"), false, GamesContent()));
        steps.Children.Add(WelcomeStepRow(WelcomeStep.Keys,
            T(KeysSettled ? "welcome.step.keys.done" : welcomeGameId != null ? "welcome.step.keys.notNeeded" : "welcome.step.keys.title"),
            true, KeysContent()));
        Grid.SetColumn(steps, 1); outer.Children.Add(steps);
        body.Children.Add(outer);

        // 右下角「一键出包」
        if (PcReady && welcomeGameId != null)
        {
            var oneclick = new Button
            {
                Content = T("welcome.oneclick"), Background = Accent, Foreground = Canvas,
                Padding = new Thickness(28, 12), CornerRadius = new CornerRadius(24),
                FontSize = 16, FontWeight = FontWeight.SemiBold, HorizontalAlignment = HorizontalAlignment.Right
            };
            oneclick.Click += (_, _) => StartOneClick(WelcomeGame!);
            body.Children.Add(oneclick);
        }
    }

    private string EnvironmentSummary()
    {
        var lines = new List<string> { "S2Arm 环境检查未通过", "", "S2Arm：" + (typeof(EnvironmentWindow).Assembly.GetName().Version?.ToString() ?? "-"),
            "系统：" + System.Runtime.InteropServices.RuntimeInformation.OSDescription, "" };
        lines.AddRange((report?.Checks ?? []).Where(c => c.Status != "ok").Select(c => $"{c.Name}（{c.Status}，{c.Code ?? "-"}）：{c.Message}"));
        lines.Add(""); lines.Add(diagnostics);
        return string.Join("\n", lines).Replace(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "~");
    }

    private Control GamesContent()
    {
        var panel = new StackPanel { Spacing = 8 };
        if (welcomeGameId != null)
        {
            var game = WelcomeGame;
            panel.Children.Add(Muted(game is not null ? game.Name : welcomeGameId));
            return panel;
        }
        if (pendingImportPaths.Length > 0)
        {
            var name = Path.GetFileName(pendingImportPaths[0]);
            var suffix = pendingImportPaths.Length > 1 ? $" +{pendingImportPaths.Length - 1}" : "";
            panel.Children.Add(Muted(name + suffix));
            return panel;
        }
        if (WelcomeCurrent == WelcomeStep.Games)
            panel.Children.Add(Muted(T("welcome.step.games.what")));
        // Xcode 下载很久：不是当前步骤也能先把游戏选了（Mac 同逻辑），但用次级按钮样式
        var btn = WelcomePrimary(T("welcome.step.games.action"), () => OpenWelcomeGames(), WelcomeCurrent == WelcomeStep.Games);
        panel.Children.Add(btn);
        return panel;
    }

    private Control KeysContent()
    {
        var panel = new StackPanel { Spacing = 8 };
        if (KeysSettled) return panel;   // 完成后标题已改，内容留空
        if (WelcomeCurrent == WelcomeStep.Keys)
        {
            panel.Children.Add(Muted(T("welcome.step.keys.what")));
            if (keysRejected)
                panel.Children.Add(new TextBlock { Text = T("welcome.keys.unusable"), FontSize = 13,
                    Foreground = Brush.Parse("#E55"), TextWrapping = TextWrapping.Wrap });
            panel.Children.Add(WelcomePrimary(T("welcome.step.keys.action"), () => OpenWelcomeKeys(), true));
        }
        else
            panel.Children.Add(Muted(T("welcome.step.keys.skip")));
        return panel;
    }

    private Button WelcomePrimary(string text, Action handler, bool primary)
    {
        if (primary)
        {
            var btn = new Button
            {
                Content = text, Background = Accent, Foreground = Canvas,
                Padding = new Thickness(20, 10), CornerRadius = new CornerRadius(20),
                FontSize = 14, FontWeight = FontWeight.SemiBold
            };
            btn.Click += (_, _) => handler();
            return btn;
        }
        else
        {
            var btn = Action("", handler);
            btn.Content = text;
            return btn;
        }
    }

    private void OpenWelcomeGames()
    {
        if (libraryBusy) return;
        // 确保游戏库存在（没库就建一个私有默认库）
        importDialog = new ImportDialogState(ImportKind.Games); importError = "";   // 弹窗先开：建库失败的原因显示在弹窗里，不能点了没反应
        EnsureLibrary(() => { });
    }

    private void OpenWelcomeKeys()
    {
        if (libraryBusy) return;
        importDialog = new ImportDialogState(ImportKind.Keys); importError = "";   // 弹窗先开：建库失败的原因显示在弹窗里，不能点了没反应
        EnsureLibrary(() => { });
    }

    // 默认库：S2Arm 私有目录下的 DefaultLibrary，如果还没连接就先 setup。
    private void EnsureLibrary(Action next)
    {
        if (connectedFolder is not null && libraryId is not null) { next(); Render(); return; }
        var defaultLib = SavedLibrary();
        libraryFolder = defaultLib;
        libraryOperation = EnsureLibraryAsync(defaultLib, next);
    }

    private async Task EnsureLibraryAsync(string folder, Action next)
    {
        if (connectedFolder is not null && libraryId is not null) { next(); Render(); return; }
        libraryBusy = true; librarySummary = "library.working"; Render();
        using var cancellation = new CancellationTokenSource();
        libraryCancellation = cancellation;
        try
        {
            connectedFolder = null; libraryId = null; catalog = null; selectedGame = null; ClearCovers();
            PrivateJobRequest.EnsurePrivateRoot(privateRoot);
            Directory.CreateDirectory(folder);
            var setup = await LibraryJobClient.RunAsync(paths, privateRoot, folder, null, "setup", null, cancellation.Token);
            DisplayJob(setup);
            if (!setup.Report.Succeeded) { librarySummary = "library.failed"; importError = setup.Report.Message; return; }
            var conn = LibraryConnection.Parse(setup.Report.Result);
            connectedFolder = conn.Folder; libraryId = conn.Id; libraryFolder = connectedFolder;
            var listing = await LibraryJobClient.RunAsync(paths, privateRoot, connectedFolder, libraryId, "catalog", null, cancellation.Token);
            DisplayJob(listing);
            if (listing.Report.Succeeded)
            { catalog = LibraryCatalog.Parse(listing.Report.Result); ClearCovers(); }
            librarySummary = "library.completed";
            next();
        }
        catch (LibraryProtocolException error)
        { librarySummary = "library.invalid_result"; libraryDiagnostics += "\n" + error.Message; importError = error.Message; }
        catch (OperationCanceledException) { librarySummary = "library.interrupted"; }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException or
            InvalidOperationException or System.ComponentModel.Win32Exception or KeyNotFoundException)
        { librarySummary = "library.failed"; libraryDiagnostics += "\n" + error.Message; importError = error.Message; }
        finally { libraryCancellation = null; libraryBusy = false; Render(); }
    }

    // 欢迎页「一键出包」：进入出包页（由 ExportPages.cs 渲染），留空壳等另一个任务填。
    public void StartOneClick(LibraryGame game)
    {
        welcomeGameId = null;
        selectedGame = game.Id;
        Navigate("build");
    }

    private static Canvas Spacer(double height) => new() { Height = height };

    private static TextBlock BigTitle(string text) => new()
    {
        Text = text, FontSize = 40, FontWeight = FontWeight.Bold, Foreground = Ink,
        TextWrapping = TextWrapping.Wrap, LineHeight = 48
    };

    private static TextBlock Lead(string text) => new()
    {
        Text = text, FontSize = 16, Foreground = Brush.Parse("#A8A6A1"),
        TextWrapping = TextWrapping.Wrap, MaxWidth = 440, LineHeight = 24, HorizontalAlignment = HorizontalAlignment.Left
    };

    private Control WelcomeStepRow(WelcomeStep step, string title, bool last, Control content)
    {
        var done = WelcomeDone(step);
        var active = step == WelcomeCurrent;
        var leftCol = new Grid { RowDefinitions = new RowDefinitions("Auto,*") };
        var circle = new Border
        {
            Width = 30, Height = 30, CornerRadius = new CornerRadius(15),
            Background = done ? Brush.Parse("#3D8565") : Brushes.Transparent,
            BorderBrush = done ? Brushes.Transparent : (active ? Accent : Brush.Parse("#30FFFFFF")),
            BorderThickness = new Thickness(1.5),
            Child = new TextBlock
            {
                Text = done ? "✓" : ((int)step + 1).ToString(), FontSize = done ? 13 : 14,
                FontWeight = FontWeight.Bold, Foreground = done ? Canvas : (active ? Accent : Brush.Parse("#FFFFFF60")),
                HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center
            }
        };
        leftCol.Children.Add(circle);
        if (!last)
        {
            var line = new Border { Width = 1, Background = Brush.Parse("#FFFFFF1A"),
                HorizontalAlignment = HorizontalAlignment.Center, Margin = new Thickness(0, 4, 0, 0) };
            Grid.SetRow(line, 1); leftCol.Children.Add(line);
        }
        var right = new StackPanel { Spacing = 10, Margin = new Thickness(0, 0, 0, last ? 0 : 30) };
        right.Children.Add(new TextBlock { Text = title, FontSize = active ? 22 : 18, FontWeight = FontWeight.Bold,
            Foreground = active || done ? Ink : Brush.Parse("#FFFFFF80"), TextWrapping = TextWrapping.Wrap });
        right.Children.Add(content);
        var row = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*"), ColumnSpacing = 16 };
        row.Children.Add(leftCol);
        Grid.SetColumn(right, 1); row.Children.Add(right);
        return row;
    }

    private Control PcContent()
    {
        var panel = new StackPanel { Spacing = 10 };
        if (busy)
        {
            panel.Children.Add(Muted(T("welcome.win.step.pc.checking")));
            panel.Children.Add(new ProgressBar { IsIndeterminate = true, Height = 4, MaxWidth = 200 });
            return panel;
        }
        if (PcReady) { panel.Children.Add(Muted(T("welcome.win.step.pc.doneDetail"))); return panel; }
        if (report is null)
        {
            // 尚未检查：Activated 事件会自动触发；从压缩包里直接打开时不会有报告，要告诉玩家先解压
            panel.Children.Add(Muted(T(summaryKey == "environment.unzip_first" ? summaryKey : "welcome.win.step.pc.checking")));
            return panel;
        }
        panel.Children.Add(Muted(T("welcome.win.step.pc.what")));
        // 随包项先列；Metal 编译器要玩家自己装，放最后（OrderBy 稳定，其余顺序不变）
        foreach (var item in report.Checks.Where(c => c.Blocking).OrderBy(c => c.Id == "metal" ? 1 : 0))
        {
            var row = new StackPanel { Spacing = 4, Margin = new Thickness(0, 8, 0, 0) };
            row.Children.Add(new TextBlock { Text = item.Name, FontSize = 14.5, FontWeight = FontWeight.SemiBold,
                Foreground = Ink, TextWrapping = TextWrapping.Wrap });
            row.Children.Add(new TextBlock { Text = item.Id == "metal" && item.Status == "missing" ? T("welcome.win.metal.detail") : item.Message,
                FontSize = 13, Foreground = Brush.Parse("#A8A6A1"), TextWrapping = TextWrapping.Wrap });
            var btns = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
            if (item.OfficialUrl is { } url)
            {
                var official = Action("action.open_official", async () => await OpenOfficialAsync(url), !busy);
                btns.Children.Add(official);
            }
            if (btns.Children.Count > 0) row.Children.Add(btns);
            panel.Children.Add(row);
        }
        var pcButtons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Margin = new Thickness(0, 8, 0, 0) };
        pcButtons.Children.Add(Action("action.recheck", () => _ = CheckAsync(), !busy));
        pcButtons.Children.Add(CopyReportButton(null, null, EnvironmentSummary));
        panel.Children.Add(pcButtons);
        return panel;
    }
}
