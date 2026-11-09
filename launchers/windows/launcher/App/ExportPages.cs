// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using System.Diagnostics;
using System.Text.Json;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input.Platform;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using S2Arm.Launcher.Protocol;

namespace S2Arm.Launcher;

// 出包页 → 出包进度 → 装到 iPhone（#1629 后续片，启动器线）。交互照 Mac 新前端 ExportView / ExportSessionView / InstallView：
// 游戏信息 + 平台卡（Windows 只出 iPhone / iPad；macOS 仅 Mac 可出；安卓、鸿蒙即将上线）→ 开始出包。没有「游戏选项」（维护者 11-07 拍板）：
// 语言跟设备、移动端掌机、60 帧缺省开起不来自动降级、满屏缺省通用 + 补丁，全由后端 / App 定，出包页不给开关；
// 进度只用作业层里程碑事件的真读数（MilestoneEstimate 与 Mac 适配层同一规则）；装机前清单 installCheck 全齐才可 install。
// 游戏库页交过来的只有当前连接（connectedFolder / libraryId）、目录（catalog）和选中的 selectedGame；本文件不改游戏库。
public sealed partial class EnvironmentWindow
{
    // Impactor bundled with the launcher zip (launchers/windows/bundle_win.py, pinned by SHA-256; same layout as pack/jobs/impactor.py BUNDLE_DIR).
    private static string BundledImpactor => Path.Combine(AppContext.BaseDirectory, "ThirdParty", "Impactor", "Impactor-windows-x86_64-portable.exe");

    // First Apple ID sign-in happens in Impactor itself (the password never passes through S2Arm); the checklist re-asks every 5 s and ticks when done.
    private void OpenBundledImpactor()
    {
        try
        {
            using var p = Process.Start(new ProcessStartInfo(BundledImpactor) { UseShellExecute = false, WorkingDirectory = Path.GetDirectoryName(BundledImpactor)! });
            if (p is null) installDiagnostics = T("process.start_failed");
        }
        catch (Exception error) when (error is IOException or System.ComponentModel.Win32Exception or InvalidOperationException)
        { installDiagnostics = T("process.start_failed") + " " + error.Message; }
        Render();
    }

    private const string PlatformIos = "ios";
    private bool exportIos = true;
    // pack job
    private LibraryGame? packGame;
    private bool packRunning;
    private CancellationTokenSource? packCancellation;
    private Task? packOperation;
    private MilestoneTrack packTrack = new();
    private DateTime packStarted;
    private double? packSeconds;
    private string packState = "";   // "" | running | succeeded | failed | cancelled | invalid
    private string packSummary = "", packDiagnostics = "";
    private string? packFailureCode;
    private string? packIpa;
    private long? packIpaBytes, packPackBytes;
    private PackSpace? packSpace;
    // in-progress capsule (shown while session is hidden)
    private bool sessionHidden;
    // finished results per game id, so player can review after going back to library
    private readonly Dictionary<string, (string state, string ipa, long? ipaBytes, long? packBytes, double seconds)> finishedPacks = new(StringComparer.Ordinal);
    // install
    private InstallReadiness? readiness;
    private string? readinessFor;
    private bool installChecking, installBusy, installCheckFailed;
    // A checklist that could not be read must not lock the player out: start and let the install job give the real reason.
    private bool InstallAllowed => readiness?.Ready == true || (readiness is null && installCheckFailed);
    private string installState = "", installMessage = "", installDiagnostics = "";
    private DateTime lastCheck = DateTime.MinValue;
    private DispatcherTimer? ticker;
    private ScrollViewer? packScroller;
    private string packScrollerPage = "";

    private bool PackBusy => packRunning || installBusy;
    private LibraryGame? SelectedGame => catalog?.Games.FirstOrDefault(g => g.Id == selectedGame && !g.Hidden);
    private static double Clock => DateTime.UtcNow.Subtract(DateTime.UnixEpoch).TotalSeconds;

    /// <summary>Pages owned by this file; false = not one of them.</summary>
    private bool RenderPackPages(StackPanel body)
    {
        if (page is not ("build" or "progress" or "install")) return false;
        EnsureTicker();
        if (page == "progress" && packGame is null) page = "build";

        // 底栏固定在窗口底部：用 DockPanel 把 content+bottomBar 分开
        var contentBody = new StackPanel { Spacing = 20, Margin = new Thickness(32, 32, 32, 0) };

        if (page == "build") RenderExport(contentBody);
        else if (page == "progress") RenderProgress(contentBody);
        else RenderInstall(contentBody);

        // 底部操作栏
        var bottomBar = RenderBottomBar();

        var dock = new DockPanel { LastChildFill = true };
        DockPanel.SetDock(bottomBar, Dock.Bottom);
        dock.Children.Add(bottomBar);

        var offset = packScroller is not null && packScrollerPage == page ? packScroller.Offset : default;
        packScroller = new ScrollViewer { Content = contentBody, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
        packScrollerPage = page;
        if (offset != default) Dispatcher.UIThread.Post(() => packScroller.Offset = offset, DispatcherPriority.Loaded);
        dock.Children.Add(packScroller);

        Content = dock;
        return true;
    }

    private Control RenderBottomBar()
    {
        var bar = new Border
        {
            Background = Brushes.Transparent, BorderBrush = Brush.Parse("#12FFFFFF"),
            BorderThickness = new Thickness(0, 1, 0, 0),
            Padding = new Thickness(32, 14)
        };
        var inner = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };

        if (page == "progress" && packRunning)
        {
            var stop = new Button { Content = T("progress.stop"), IsEnabled = packCancellation is not null,
                Padding = new Thickness(16, 9), Background = Brush.Parse("#29292D"), Foreground = Ink,
                CornerRadius = new CornerRadius(8) };
            AutomationId(stop, "progress.stop");
            stop.Click += (_, _) => packCancellation?.Cancel();
            inner.Children.Add(stop);

            var backBtn = new Button { Content = T("export.running.back"), Padding = new Thickness(16, 9),
                Background = Brush.Parse("#29292D"), Foreground = Ink, CornerRadius = new CornerRadius(8) };
            backBtn.Click += (_, _) => { sessionHidden = true; Navigate("library"); };
            inner.Children.Add(backBtn);
        }
        else if (page == "progress" && !packRunning)
        {
            var backLib = new Button { Content = T("nav.library"), Padding = new Thickness(16, 9),
                Background = Brush.Parse("#29292D"), Foreground = Ink, CornerRadius = new CornerRadius(8) };
            backLib.Click += (_, _) => Navigate("library");
            inner.Children.Add(backLib);
        }
        else
        {
            var backLib = new Button { Content = T("nav.library"), Padding = new Thickness(16, 9),
                Background = Brush.Parse("#29292D"), Foreground = Ink, CornerRadius = new CornerRadius(8) };
            backLib.Click += (_, _) => Navigate("library");
            inner.Children.Add(backLib);
        }

        bar.Child = inner;
        return bar;
    }

    private void EnsureTicker()
    {
        if (ticker is not null) return;
        ticker = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        ticker.Tick += (_, _) =>
        {
            if (page == "progress" && packRunning) Render();
            // The checklist changes when the player plugs a phone in or installs Impactor: re-ask every 5 s while visible and idle.
            if (page == "install" && !installBusy && !installChecking && DateTime.UtcNow - lastCheck > TimeSpan.FromSeconds(5))
                _ = CheckInstallAsync();
        };
        ticker.Start();
        Closed += (_, _) => ticker.Stop();
    }

    /// <summary>The build tab opens the progress page while a pack is running or its result is on screen.</summary>
    private void OpenBuild()
    {
        if (packRunning || (packState == "succeeded" && packGame?.Id == selectedGame)) { Navigate("progress"); return; }
        Navigate("build");
    }

    private static string Duration(double seconds)
    {
        var s = (int)Math.Round(Math.Max(0, seconds));
        return s >= 3600 ? $"{s / 3600}:{s / 60 % 60:00}:{s % 60:00}" : $"{s / 60}:{s % 60:00}";
    }

    private Button Primary(string text, Action handler, bool enabled = true)
    {
        var button = new Button { Content = text, IsEnabled = enabled, Background = Accent, Foreground = Canvas,
            Padding = new Thickness(26, 12), CornerRadius = new CornerRadius(24), FontSize = 16, FontWeight = FontWeight.SemiBold };
        button.Click += (_, _) => handler();
        return button;
    }

    private TextBlock Muted(string text, double size = 13) => new()
    {
        Text = text, FontSize = size, TextWrapping = TextWrapping.Wrap, Foreground = Brush.Parse("#A8A6A1")
    };

    private Control GameHeader(LibraryGame game)
    {
        var header = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*"), ColumnSpacing = 24 };
        header.Children.Add(GameCover(game, 120));
        var title = new StackPanel { Spacing = 8, VerticalAlignment = VerticalAlignment.Center };
        title.Children.Add(Label(game.Name, 30));
        title.Children.Add(Muted(game.Version.Length == 0 ? T("export.version_unknown") : T("library.version") + " · " + game.Version, 14));
        if (game.Missing.Count > 0)
            title.Children.Add(Muted(T("library.requirements") + " · " + string.Join(" / ", game.Missing)));
        Grid.SetColumn(title, 1); header.Children.Add(title);
        return header;
    }

    // ---- export page ----

    private void RenderExport(StackPanel body)
    {
        var game = SelectedGame;
        body.Children.Add(Action("export.back", () => Navigate("library")));
        if (game is null || connectedFolder is null || libraryId is null)
        {
            body.Children.Add(Label(T("export.no_game"), 18));
            return;
        }
        body.Children.Add(GameHeader(game));

        // platforms
        var heading = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        heading.Children.Add(Label(T("export.platforms"), 16));
        var existing = Action("export.install_existing", () => { readinessFor = null; Navigate("install"); }, !PackBusy);
        Grid.SetColumn(existing, 1); heading.Children.Add(existing);
        body.Children.Add(heading);
        var tiles = new UniformGrid { Columns = 4 };
        foreach (var platform in new[] { PlatformIos, "mac", "android", "ohos" })
        {
            var open = platform == PlatformIos;
            var on = open && exportIos;
            var tile = new StackPanel { Spacing = 10 };
            var top = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
            top.Children.Add(Label(T("export.platform." + platform), 16));
            var mark = Label(open ? (on ? "☑" : "☐") : "🔒", 16);
            Grid.SetColumn(mark, 1); top.Children.Add(mark);
            tile.Children.Add(top);
            tile.Children.Add(Muted(T(platform switch
            {
                PlatformIos => "export.note.ios", "mac" => "export.note.mac_only", _ => "export.soon"
            })));
            var card = new Button
            {
                Content = tile, IsEnabled = open && !PackBusy, Margin = new Thickness(0, 0, 12, 0), Padding = new Thickness(18),
                HorizontalAlignment = HorizontalAlignment.Stretch, HorizontalContentAlignment = HorizontalAlignment.Stretch,
                VerticalAlignment = VerticalAlignment.Stretch, MinHeight = 120, CornerRadius = new CornerRadius(14),
                Background = on ? Brush.Parse("#2A2722") : Surface, Foreground = Ink, Opacity = open ? 1 : 0.5,
                BorderBrush = on ? Accent : Brush.Parse("#2A2A2E"), BorderThickness = new Thickness(on ? 1.5 : 1)
            };
            AutomationId(card, "export.platform." + platform);
            card.Click += (_, _) => { exportIos = !exportIos; Render(); };
            tiles.Children.Add(card);
        }
        body.Children.Add(tiles);

        // action bar
        var bar = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto"), ColumnSpacing = 16 };
        var blocker = ExportBlocker(game);
        var reason = new StackPanel { Spacing = 8, VerticalAlignment = VerticalAlignment.Center };
        reason.Children.Add(Muted(blocker ?? T("export.hint"), 14));
        if (blocker == T("export.block.keys")) reason.Children.Add(Action("nav.library", () => Navigate("library")));
        bar.Children.Add(reason);
        var start = Primary(T("export.start"), () => StartPack(game), blocker is null);
        AutomationId(start, "export.start");
        Grid.SetColumn(start, 1); bar.Children.Add(start);
        body.Children.Add(Card(bar, 18));
    }

    private static void AutomationId(Control control, string id) => Avalonia.Automation.AutomationProperties.SetAutomationId(control, id);

    /// <summary>Why export cannot start now (null = it can). Order as the Mac export page: platform → game files → keys → busy.</summary>
    private string? ExportBlocker(LibraryGame game)
    {
        if (!exportIos) return T("export.block.no_platform");
        if (game.Missing.Count > 0) return T("library.requirements") + " · " + string.Join(" / ", game.Missing);
        if (game.NeedsKeys && catalog?.KeysReady != true) return T("export.block.keys");
        if (PackBusy || libraryBusy || busy) return T("export.block.busy");
        return null;
    }

    private PackJobClient.Request PackRequest(string operation, string gameId, IReadOnlyDictionary<string, object?>? extra = null) =>
        new(paths, privateRoot, connectedFolder!, libraryId!, operation, gameId, extra);

    // ---- pack job ----

    private void StartPack(LibraryGame game, bool spaceOverride = false)
    {
        if (ExportBlocker(game) is not null) return;
        packGame = game; packRunning = true; packState = "running"; packTrack = new(); packStarted = DateTime.UtcNow;
        packSeconds = null; packSummary = ""; packDiagnostics = ""; packIpa = null;
        packIpaBytes = null; packPackBytes = null; packSpace = null; sessionHidden = false; packFailureCode = null;
        Navigate("progress");
        packOperation = RunPackAsync(game, spaceOverride);
    }

    private async Task RunPackAsync(LibraryGame game, bool spaceOverride)
    {
        using var cancellation = new CancellationTokenSource();
        packCancellation = cancellation;
        try
        {
            var job = await PackJobClient.RunAsync(PackRequest("ipa", game.Id, spaceOverride ? new Dictionary<string, object?> { ["spaceOverride"] = true } : null),
                e => Dispatcher.UIThread.Post(() =>
                {
                    packTrack.Ingest(e, Clock);
                    // space event: { "type": "space", "space": { "volumes": [...] } }
                    if (e.TryGetProperty("type", out var et) && et.GetString() == "space" &&
                        e.TryGetProperty("space", out var sp))
                        packSpace = PackSpace.TryParse(sp);
                }), cancellation.Token);
            packState = job.Report.State;
            packSummary = job.Report.Message;
            packDiagnostics = JobDiagnostics(job);
            packFailureCode = job.Report.Succeeded ? null : DiagnosticCode(job.Report.Diagnostics);
            if (job.Report.Succeeded)
            {
                if (job.Report.Result.TryGetProperty("ipa", out var ipa) && ipa.ValueKind == JsonValueKind.String)
                    packIpa = ipa.GetString();
                if (job.Report.Result.TryGetProperty("ipaBytes", out var ib) && ib.ValueKind == JsonValueKind.Number)
                    packIpaBytes = ib.GetInt64();
                if (job.Report.Result.TryGetProperty("packBytes", out var pb) && pb.ValueKind == JsonValueKind.Number)
                    packPackBytes = pb.GetInt64();
            }
        }
        catch (LibraryProtocolException error) { packState = "invalid"; packDiagnostics = error.Message; }
        catch (OperationCanceledException) { packState = "cancelled"; }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException or InvalidOperationException or
            PlatformNotSupportedException or System.ComponentModel.Win32Exception)
        { packState = "failed"; packDiagnostics = error.Message; }
        finally
        {
            // Posted events may still be queued: finish the track after them.
            Dispatcher.UIThread.Post(() =>
            {
                packTrack.Finish(Clock); packSeconds = (DateTime.UtcNow - packStarted).TotalSeconds;
                packCancellation = null; packRunning = false;
                if (packState == "succeeded")
                {
                    readinessFor = null;
                    if (packGame is not null)
                        finishedPacks[packGame.Id] = (packState, packIpa ?? "", packIpaBytes, packPackBytes, packSeconds ?? 0);
                }
                Render();
            });
        }
    }

    private string JobDiagnostics(LibraryJobOutput job)
    {
        var text = job.Report.Message + "\n" + T("diagnostics.exit_code") + ": " + job.Output.ExitCode;
        if (job.Report.Diagnostics.ValueKind == JsonValueKind.Array)
            foreach (var diagnostic in job.Report.Diagnostics.EnumerateArray())
            {
                if (diagnostic.TryGetProperty("code", out var code) && code.ValueKind == JsonValueKind.String)
                    text += "\n" + T("errors." + code.GetString() + ".title") + "\n" + T("errors." + code.GetString() + ".detail");
                foreach (var key in new[] { "summary", "suggestion" })
                    if (diagnostic.TryGetProperty(key, out var message) && message.ValueKind == JsonValueKind.String)
                        text += "\n" + message.GetString();
                if (diagnostic.TryGetProperty("failureSummary", out var failureSummary) && failureSummary.ValueKind == JsonValueKind.String)
                    text += "\n" + failureSummary.GetString();
            }
        if (!string.IsNullOrWhiteSpace(job.Output.StandardError)) text += "\n" + job.Output.StandardError;
        return text;
    }

    private void RenderProgress(StackPanel body)
    {
        var game = packGame!;
        var elapsed = packSeconds ?? (DateTime.UtcNow - packStarted).TotalSeconds;
        var estimate = packTrack.Estimate(Clock);

        // Top action bar: back button + in-progress capsule
        var topBar = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*,Auto"), ColumnSpacing = 12 };
        topBar.Children.Add(Action("export.back", () =>
        {
            if (packRunning) { sessionHidden = true; Navigate("library"); } else Navigate("build");
        }));
        if (packRunning && !sessionHidden)
        {
            var hint = Muted(T("export.hint"), 12); hint.VerticalAlignment = VerticalAlignment.Center;
            Grid.SetColumn(hint, 1); topBar.Children.Add(hint);
        }
        body.Children.Add(topBar);

        // Two-column layout: left=game info + space, right=progress
        var twoCol = new Grid { ColumnDefinitions = new ColumnDefinitions("260,*"), ColumnSpacing = 36 };

        // ---- LEFT: game info panel + disk space ----
        var leftCol = new StackPanel { Spacing = 16 };
        leftCol.Children.Add(GameInfoPanel(game));
        if (packSpace is { } sp) leftCol.Children.Add(SpaceCard(sp));
        twoCol.Children.Add(leftCol);

        // ---- RIGHT: heading + overall + steps / results ----
        var rightCol = new StackPanel { Spacing = 20 };
        Grid.SetColumn(rightCol, 1);

        // Status dot + label + platform
        var statusRow = new WrapPanel { Orientation = Orientation.Horizontal };
        var (statusText, statusColor) = packState switch
        {
            "running" => (T("progress.running"), "#FFC857"),
            "succeeded" => (T("progress.done"), "#52C87A"),
            "cancelled" => (T("progress.cancelled"), "#8C8A85"),
            _ => (T("progress.failed"), "#E05C5C")
        };
        var dot = new Border { Width = 9, Height = 9, CornerRadius = new CornerRadius(5),
            Background = Brush.Parse(statusColor), Margin = new Thickness(0, 4, 8, 0) };
        statusRow.Children.Add(dot);
        var statusLabel = Label(statusText, 14); statusLabel.FontWeight = FontWeight.Bold;
        statusLabel.Foreground = Brush.Parse(statusColor); statusRow.Children.Add(statusLabel);
        var platLabel = Muted(" · " + T("export.platform.ios"), 14);
        platLabel.Margin = new Thickness(2, 0, 0, 0); statusRow.Children.Add(platLabel);
        rightCol.Children.Add(statusRow);

        if (packRunning || packState == "")
        {
            rightCol.Children.Add(OverallSection(estimate, elapsed));
            rightCol.Children.Add(StepsSection(estimate));
            // 取消和回到游戏库按钮在底栏（RenderBottomBar）
        }
        else
        {
            // 成功页：ResultSection 包含步骤折叠，不单独显示步骤列表
            rightCol.Children.Add(ResultSection(game, elapsed, estimate));
        }
        twoCol.Children.Add(rightCol);
        body.Children.Add(twoCol);

        if (packDiagnostics.Length > 0)
            body.Children.Add(new Expander { Header = T("library.job_diagnostics"), IsExpanded = packState is "failed" or "invalid",
                HorizontalAlignment = HorizontalAlignment.Stretch,
                Content = new TextBox { Text = packDiagnostics, IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MaxHeight = 260 } });
    }

    // ---- game info panel (left column) ----

    private Control GameInfoPanel(LibraryGame game)
    {
        var panel = new StackPanel { Spacing = 12 };
        // Cover artwork
        var cover = GameCover(game, 220);
        if (cover is Border b) b.CornerRadius = new CornerRadius(10);
        panel.Children.Add(cover);
        // Name
        var name = Label(game.Name, 20); name.FontWeight = FontWeight.Bold;
        name.TextWrapping = TextWrapping.Wrap; name.MaxHeight = 72;
        panel.Children.Add(name);
        // Info rows
        void InfoRow(StackPanel p, string label, string value, bool mono = false)
        {
            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto"), Margin = new Thickness(0, 0, 0, 0) };
            var lbl = Muted(label, 12); row.Children.Add(lbl);
            var val = Label(value, 13); val.FontWeight = FontWeight.SemiBold;
            if (mono) val.FontFamily = new FontFamily("Consolas,Courier New,monospace");
            val.HorizontalAlignment = HorizontalAlignment.Right; val.TextWrapping = TextWrapping.NoWrap;
            val.MaxWidth = 140; val.TextTrimming = TextTrimming.CharacterEllipsis;
            Grid.SetColumn(val, 1); row.Children.Add(val);
            // divider
            var border = new Border { BorderBrush = Brush.Parse("#28282C"), BorderThickness = new Thickness(0,0,0,1), Child = row, Padding = new Thickness(0, 6) };
            p.Children.Add(border);
        }
        var rows = new StackPanel { Spacing = 0 };
        InfoRow(rows, T("export.info.version"), game.Version.Length > 0 ? game.Version : "-");
        InfoRow(rows, T("export.info.base"), game.BaseVersion ?? T("export.info.none"));
        InfoRow(rows, T("export.info.update"), game.UpdateVersion ?? T("export.info.none"));
        var dlcText = game.DlcCount > 0
            ? T("export.info.dlcCount").Replace("{0}", game.DlcCount.ToString())
            : T("export.info.none");
        InfoRow(rows, T("export.info.dlc"), dlcText);
        var sizeText = game.TotalBytes > 0 ? FormatBytes(game.TotalBytes) : "-";
        InfoRow(rows, T("export.info.size"), sizeText);
        InfoRow(rows, T("export.info.program"), game.Id, mono: true);
        panel.Children.Add(rows);
        return Card(panel, 14);
    }

    // ---- disk space card ----

    private Control SpaceCard(PackSpace sp)
    {
        var card = new StackPanel { Spacing = 8 };
        card.Children.Add(Label(T("export.space.title"), 13));
        if (sp.NeedBytes is { } need && sp.FreeBytes is { } free && free > 0)
        {
            var fill = Math.Min(1.0, (double)need / free);
            var barOuter = new Border { Height = 6, CornerRadius = new CornerRadius(3),
                Background = Brush.Parse("#2E2E32") };
            var barInner = new Border { Height = 6, CornerRadius = new CornerRadius(3),
                Background = need > free ? Brush.Parse("#E05C5C") : Brush.Parse("#FFF0D6"),
                HorizontalAlignment = HorizontalAlignment.Left };
            // width set via a measure pass substitute: use a Grid with star/auto
            var barGrid = new Grid { ColumnDefinitions = new ColumnDefinitions($"{fill}*,{1-fill}*") };
            barGrid.Children.Add(new Border { Background = need > free ? Brush.Parse("#E05C5C") : Brush.Parse("#FFF0D6"),
                CornerRadius = new CornerRadius(3) });
            barGrid.Children.Add(new Border());   // filler
            Grid.SetColumn(barGrid.Children[1], 1);
            barOuter.Child = barGrid;
            card.Children.Add(barOuter);
        }
        var figRow = new Grid { ColumnDefinitions = new ColumnDefinitions("*,*") };
        var needBlock = new StackPanel { Spacing = 2 };
        needBlock.Children.Add(Muted(T("export.space.need"), 11));
        needBlock.Children.Add(Label(sp.NeedBytes is { } nb ? FormatBytes(nb) : "—", 14));
        figRow.Children.Add(needBlock);
        var freeBlock = new StackPanel { Spacing = 2 };
        freeBlock.Children.Add(Muted(T("export.space.free"), 11));
        freeBlock.Children.Add(Label(sp.FreeBytes is { } fb ? FormatBytes(fb) : "—", 14));
        Grid.SetColumn(freeBlock, 1); figRow.Children.Add(freeBlock);
        card.Children.Add(figRow);
        if (sp.Basis is "measured" or "rule")
            card.Children.Add(Muted(T("export.space.basis." + sp.Basis), 11));
        return Card(card, 14);
    }

    // ---- overall progress (big %, segmented bar, three times) ----

    private Control OverallSection(MilestoneEstimate estimate, double elapsed)
    {
        var plan = packTrack.Plan;
        var fraction = OverallFraction(estimate, plan);
        var section = new StackPanel { Spacing = 12 };

        // Big %
        var pctRow = new WrapPanel { Orientation = Orientation.Horizontal };
        var pct = Label($"{(int)(fraction * 100)}%", 52); pct.FontWeight = FontWeight.Black;
        pct.Margin = new Thickness(0, 0, 10, 0);
        pctRow.Children.Add(pct);
        var overallLbl = Muted(T("export.overall"), 14);
        overallLbl.VerticalAlignment = VerticalAlignment.Bottom; overallLbl.Margin = new Thickness(0, 0, 0, 8);
        pctRow.Children.Add(overallLbl);
        section.Children.Add(pctRow);

        // Segmented bar
        if (plan.Count > 0) section.Children.Add(SegmentedBar(estimate, plan));

        // Three time figures
        var times = new WrapPanel { Orientation = Orientation.Horizontal };
        void TimeFigure(string label, string value, bool accent = false)
        {
            var cell = new StackPanel { Spacing = 3, Margin = new Thickness(0, 0, 36, 0) };
            cell.Children.Add(Muted(label, 12));
            var v = Label(value, 26); v.FontWeight = FontWeight.Bold;
            if (accent) v.Foreground = Accent;
            cell.Children.Add(v);
            times.Children.Add(cell);
        }
        TimeFigure(T("progress.elapsed"), Duration(elapsed));
        if (packRunning)
        {
            if (estimate.Remaining is { } rem)
            {
                TimeFigure(T("progress.remaining"), Duration(rem), accent: true);
                TimeFigure(T("progress.finish_at"), DateTime.Now.AddSeconds(rem).ToString("HH:mm"));
            }
            else if (estimate.StepRemainingByRate is { } stepRem)
                TimeFigure(T("progress.remaining"), Duration(stepRem), accent: true);
        }
        section.Children.Add(times);

        // History/rate note
        if (packRunning && plan.Count > 0)
        {
            if (estimate.Remaining is not null && estimate.HistorySamples > 0)
                section.Children.Add(Muted(string.Format(T("progress.basis"), estimate.HistorySamples) +
                    (estimate.StepRemainingByRate is not null ? " · " + T("progress.by_rate") : ""), 12));
            else if (estimate.Remaining is null && estimate.StepRemainingByRate is null)
                section.Children.Add(Muted(T("progress.no_history"), 12));
        }
        return section;
    }

    private static double OverallFraction(MilestoneEstimate estimate, IReadOnlyList<Milestone> plan)
    {
        if (plan.Count == 0) return 0;
        var weights = Weights(plan); var total = weights.Sum();
        var thisPlatform = total > 0 ? plan.Select((m, k) => estimate.Fill(k) * weights[k]).Sum() / total : 0;
        return Math.Min(1, thisPlatform);
    }

    private static double[] Weights(IReadOnlyList<Milestone> plan)
    {
        var measured = plan.Select(m => m.Measured ?? -1).ToArray();
        return measured.Contains(-1) || measured.Sum() <= 0
            ? plan.Select(_ => 1.0).ToArray()
            : measured.Select(v => Math.Max(v, 1)).ToArray();
    }

    // ---- segmented bar with sweep — 铺满父容器宽度 ----

    private Control SegmentedBar(MilestoneEstimate estimate, IReadOnlyList<Milestone> plan)
    {
        var weights = Weights(plan); var total = Math.Max(weights.Sum(), 1);
        // 用 Grid 按权重分配列宽，确保铺满右栏
        var colDefs = string.Join(",", weights.Select(w => $"{w}*"));
        var bar = new Grid { ColumnDefinitions = new ColumnDefinitions(colDefs), Height = 10, ColumnSpacing = 3 };
        for (var k = 0; k < plan.Count; k++)
        {
            var fill = estimate.Fill(k);
            var active = k == estimate.Index && packRunning;
            var seg = new Grid();
            seg.ColumnDefinitions = new ColumnDefinitions($"{fill}*,{Math.Max(0, 1 - fill)}*");
            seg.Children.Add(new Border { Background = Brush.Parse("#222226"), CornerRadius = new CornerRadius(2), [Grid.ColumnSpanProperty] = 2 });
            if (fill > 0)
                seg.Children.Add(new Border { Background = active ? Accent : Brush.Parse("#B8A07A"), CornerRadius = new CornerRadius(2) });
            Grid.SetColumn(seg, k); bar.Children.Add(seg);
        }
        return bar;
    }

    // ---- steps list ----

    private Control StepsSection(MilestoneEstimate estimate)
    {
        var steps = new StackPanel { Spacing = 12 };
        if (packTrack.Plan.Count == 0)
        {
            steps.Children.Add(Muted(T(packRunning ? "progress.preparing" : "progress.no_steps")));
            return steps;
        }
        for (var k = 0; k < packTrack.Plan.Count; k++)
        {
            var m = packTrack.Plan[k];
            var isRunning = k == estimate.Index && packRunning;
            var isDone = k < (estimate.Index ?? (packRunning ? 0 : packTrack.Plan.Count));
            var skipped = isDone && !packTrack.Started.ContainsKey(m.Id);

            // 图标：运行中=实心圆点（呼吸效果通过色差模拟），完成=绿对勾，排队=空心圆
            var markText = isRunning ? "●" : isDone ? (skipped ? "↺" : "✓") : "○";
            var markColor = isRunning ? "#FFC857" : isDone ? (skipped ? "#8C8A85" : "#52C87A") : "#4A4A50";

            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("28,*,Auto"), ColumnSpacing = 10 };
            var markLbl = new TextBlock { Text = markText, FontSize = isRunning ? 18 : 15,
                Foreground = Brush.Parse(markColor), VerticalAlignment = VerticalAlignment.Top };
            row.Children.Add(markLbl);

            var content = new StackPanel { Spacing = 4 };
            var nameLbl = Label((k + 1) + ". " + m.Label, isRunning ? 16 : 14);
            if (isRunning) nameLbl.FontWeight = FontWeight.Bold;
            if (!isDone && !isRunning) nameLbl.Foreground = Brush.Parse("#8C8A85");
            content.Children.Add(nameLbl);
            if (m.Purpose.Length > 0)
            {
                var purpose = Muted(m.Purpose, 12); content.Children.Add(purpose);
            }
            if (isRunning)
            {
                var fill = estimate.Fill(k);
                content.Children.Add(new ProgressBar { Minimum = 0, Maximum = 1, Value = fill, Height = 6,
                    IsIndeterminate = packTrack.Fraction is null && m.Measured is null,
                    Foreground = Accent, Margin = new Thickness(0, 4, 0, 0) });
                var details = new List<string>();
                if (packTrack.Fraction is { } f) details.Add($"{(int)(f * 100)}%");
                if (estimate.StepRemainingByRate is { } sr)
                    details.Add(T("export.step.left").Replace("{0}", Duration(sr)));
                if (packTrack.Message.Length > 0) details.Add(packTrack.Message);
                if (details.Count > 0) content.Children.Add(Muted(string.Join(" · ", details), 12));
            }
            Grid.SetColumn(content, 1); row.Children.Add(content);

            // 单步用时：从该步骤自己的开始时间算，不用全局时钟基准
            string rightText;
            if (isRunning)
            {
                // StepElapsed 是从该步开始的秒数
                rightText = T("progress.step") + " " + Duration(estimate.StepElapsed);
            }
            else if (isDone)
            {
                rightText = packTrack.Duration(m.Id) is { } d ? Duration(d) : m.Reuse ? T("progress.reuse") : T("progress.skipped");
            }
            else
            {
                rightText = m.Measured is { } ex && ex > 0 ? T("export.step.expected").Replace("{0}", Duration(ex)) : "";
            }
            var side = Muted(rightText, 12); side.VerticalAlignment = VerticalAlignment.Top;
            Grid.SetColumn(side, 2); row.Children.Add(side);
            steps.Children.Add(row);
        }
        return steps;
    }

    // ---- result section (success / failure / cancelled) ----

    // ---- result section (success / failure / cancelled) ----

    private Control ResultSection(LibraryGame game, double elapsed, MilestoneEstimate? estimate = null)
    {
        var result = new StackPanel { Spacing = 20, VerticalAlignment = VerticalAlignment.Center };
        if (packState == "succeeded")
        {
            // 绿对勾（84 大小）
            var seal = new Border { Width = 84, Height = 84, CornerRadius = new CornerRadius(42), HorizontalAlignment = HorizontalAlignment.Left,
                Background = Brush.Parse("#40D678"),
                Child = new TextBlock { Text = "✓", FontSize = 44, FontWeight = FontWeight.Black,
                    Foreground = Brushes.Black, HorizontalAlignment = HorizontalAlignment.Center,
                    VerticalAlignment = VerticalAlignment.Center } };
            result.Children.Add(seal);
            result.Children.Add(Label(T("export.done.title"), 40));
            result.Children.Add(Muted(T("export.done.ios"), 16));

            // 三个大数字
            var stats = new WrapPanel { Orientation = Orientation.Horizontal };
            void BigStat(string label, string value)
            {
                var cell = new StackPanel { Spacing = 4, Margin = new Thickness(0, 0, 44, 0) };
                cell.Children.Add(Muted(label, 13));
                var v = Label(value, 32); v.FontWeight = FontWeight.Black;
                cell.Children.Add(v);
                stats.Children.Add(cell);
            }
            BigStat(T("export.done.time"), Duration(elapsed));
            if (packIpaBytes is { } ib) BigStat(T("export.done.ipa"), FormatBytes(ib));
            if (packPackBytes is { } pb) BigStat(T("export.done.pack"), FormatBytes(pb));
            result.Children.Add(stats);

            // 两个大按钮
            var buttons = new WrapPanel { Orientation = Orientation.Horizontal };
            var install = Primary(T("export.done.install"), () => { readinessFor = null; Navigate("install"); });
            AutomationId(install, "progress.install");
            install.Margin = new Thickness(0, 0, 12, 0); buttons.Children.Add(install);
            if (packIpa is not null)
            {
                var reveal = Action("progress.reveal_ipa", () => Reveal(packIpa));
                reveal.Margin = new Thickness(0, 0, 12, 0); buttons.Children.Add(reveal);
            }
            result.Children.Add(buttons);
            result.Children.Add(Muted(T("export.win.done.installNote"), 12));

            // 详细记录（折叠步骤列表）
            if (estimate is not null)
            {
                var detail = new Expander
                {
                    Header = T("export.detail.title"),
                    HorizontalAlignment = HorizontalAlignment.Stretch,
                    Content = StepsSection(estimate)
                };
                result.Children.Add(detail);
            }
        }
        else if (packState is "failed" or "invalid")
        {
            if (packSummary.Length > 0) result.Children.Add(Label(packSummary, 16));
            var buttons = new WrapPanel { Orientation = Orientation.Horizontal };
            var retry = Action("progress.again", () => Navigate("build"));
            retry.Margin = new Thickness(0, 0, 12, 0); buttons.Children.Add(retry);
            if (packFailureCode == "disk_full")   // 磁盘预算偏保守：和 Mac 一样允许玩家自己决定继续
            {
                var proceed = Action("export.fix.continue", () => StartPack(game, spaceOverride: true), !PackBusy);
                proceed.Margin = new Thickness(0, 0, 12, 0); buttons.Children.Add(proceed);
            }
            if (packState == "failed")
            {
                var copy = CopyReportButton(game.Id, ["ipa"], () => ErrorSummary(game));
                buttons.Children.Add(copy);
            }
            result.Children.Add(buttons);
            var feedback = new WrapPanel { Orientation = Orientation.Horizontal };
            feedback.Children.Add(Muted(T("export.fail.feedback"), 12));
            var gh = new Button { Content = T("export.fail.github"), Background = Transparent,
                Foreground = Accent, Padding = new Thickness(6, 0), FontSize = 12 };
            gh.Click += async (_, _) => await OpenOfficialAsync(new Uri("https://github.com/jhaizhou-ops/S2Arm/issues"));
            feedback.Children.Add(gh);
            var xBtn = new Button { Content = T("export.fail.x"), Background = Transparent,
                Foreground = Accent, Padding = new Thickness(6, 0), FontSize = 12 };
            xBtn.Click += async (_, _) => await OpenOfficialAsync(new Uri("https://x.com/happyjoejiang"));
            feedback.Children.Add(xBtn);
            result.Children.Add(feedback);
        }
        else if (packState == "cancelled")
        {
            result.Children.Add(Label(T("progress.cancelled"), 18));
            result.Children.Add(Action("progress.again", () => Navigate("build")));
        }
        return result;
    }

    /// 「复制错误摘要」：正文由作业层 report.py 生成（Mac / Windows 同一份）；没有作业记录才用 fallback。
    /// operations 为 null：这种失败没有作业记录（环境检查），直接用 fallback。
    private Button CopyReportButton(string? gameId, string[]? operations, Func<string> fallback)
    {
        var copy = new Button { Content = T("export.fail.copy"), Padding = new Thickness(16, 9),
            Background = Brush.Parse("#29292D"), Foreground = Ink, CornerRadius = new CornerRadius(8) };
        copy.Click += async (_, _) =>
        {
            var extra = System.Text.Json.JsonSerializer.Serialize(new Dictionary<string, object?>
            {
                ["app"] = AppVersion,
                ["checks"] = (report?.Checks ?? []).Where(c => c.Status != "ok")
                    .Select(c => new Dictionary<string, string> { ["name"] = c.Name, ["message"] = c.Message }).ToList()
            });
            var text = (operations is null ? null : await JobReportClient.RunAsync(paths, privateRoot, gameId, operations, extra)) ?? fallback();
            if (TopLevel.GetTopLevel(this)?.Clipboard is { } clipboard) await clipboard.SetTextAsync(text);
            copy.Content = T("export.fail.copied");
        };
        return copy;
    }

    // ---- error summary for clipboard (only when the job left no record) ----

    private string ErrorSummary(LibraryGame game)
    {
        var lines = new List<string>
        {
            "S2Arm 出包失败报告", "",
            "游戏：" + game.Name,
            "版本：" + game.Version,
            "程序号：" + game.Id,
            "原包大小：" + FormatBytes(game.TotalBytes),
            "",
            "本体 / 更新 / DLC：" + (game.BaseVersion ?? "-") + " / " + (game.UpdateVersion ?? "-") + " / " + game.DlcCount,
            "",
            "错误：", packSummary,
            "", packDiagnostics
        };
        return string.Join("\n", lines);
    }

    private static string FormatBytes(long bytes)
    {
        if (bytes >= 1024L * 1024 * 1024) return $"{bytes / (1024.0 * 1024 * 1024):0.##} GB";
        if (bytes >= 1024L * 1024) return $"{bytes / (1024.0 * 1024):0.##} MB";
        if (bytes >= 1024L) return $"{bytes / 1024.0:0.##} KB";
        return bytes + " B";
    }

    private static readonly IBrush Transparent = Brushes.Transparent;

    private void Reveal(string path)
    {
        try
        {
            // Explorer selects the file; the path goes in as one argument, never through a shell.
            if (OperatingSystem.IsWindows()) Process.Start(new ProcessStartInfo("explorer.exe") { ArgumentList = { "/select," + path } })?.Dispose();
        }
        catch (Exception error) when (error is System.ComponentModel.Win32Exception or InvalidOperationException)
        { packDiagnostics += "\n" + T("process.start_failed") + "\n" + error.Message; Render(); }
    }

    // ---- install page ----

    private void RenderInstall(StackPanel body)
    {
        var game = SelectedGame ?? packGame;
        body.Children.Add(Action("export.back", () => Navigate("build"), !installBusy));
        if (game is null || connectedFolder is null || libraryId is null) { body.Children.Add(Label(T("export.no_game"), 18)); return; }
        if (readinessFor != game.Id && !installChecking) { readinessFor = game.Id; readiness = null; installCheckFailed = false; _ = CheckInstallAsync(); }
        body.Children.Add(GameHeader(game));
        body.Children.Add(Label(T("install.title"), 22));
        body.Children.Add(Muted(T("install.description"), 14));
        body.Children.Add(Muted(T("install.container_note"), 14));
        body.Children.Add(Muted(T("install.legacy_note"), 14));
        if (File.Exists(BundledImpactor)) body.Children.Add(Muted(T("install.third_party")));   // 随包第三方工具要写明（11-07 维护者 随包下发）

        var list = new StackPanel { Spacing = 12 };
        if (readiness is null) list.Children.Add(Muted(T(installChecking ? "install.checking" : "install.no_report")));
        foreach (var item in readiness?.Items ?? [])
        {
            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("28,*,Auto"), ColumnSpacing = 10 };
            row.Children.Add(Label(item.Ok ? "✓" : item.Waiting ? "…" : "○", 16));
            var text = new StackPanel { Spacing = 4 };
            // Titles / details are backend text (Chinese); the English interface shows them as they come.
            text.Children.Add(Label(item.Title, 15));
            if (item.Detail.Length > 0) text.Children.Add(Muted(item.Detail));
            Grid.SetColumn(text, 1); row.Children.Add(text);
            if (!item.Ok && item.OfficialUrl is { } url)
            {
                var official = new Button { Content = T("action.open_official"), Background = Accent, Foreground = Canvas,
                    Padding = new Thickness(14, 8), CornerRadius = new CornerRadius(8), VerticalAlignment = VerticalAlignment.Center };
                official.Click += async (_, _) => await OpenOfficialAsync(url);
                Grid.SetColumn(official, 2); row.Children.Add(official);
            }
            else if (!item.Ok && item.Action == "openImpactor")
            {
                var open = new Button { Content = T("action.open_impactor"), Background = Accent, Foreground = Canvas,
                    Padding = new Thickness(14, 8), CornerRadius = new CornerRadius(8), VerticalAlignment = VerticalAlignment.Center };
                AutomationId(open, "install.open_impactor");
                open.Click += (_, _) => OpenBundledImpactor();
                Grid.SetColumn(open, 2); row.Children.Add(open);
            }
            list.Children.Add(row);
        }
        if (readiness is { Device.Length: > 0 }) list.Children.Add(Muted(T("install.device") + " · " + readiness.Device));
        var buttons = new WrapPanel { Orientation = Orientation.Horizontal };
        var recheck = Action("action.recheck", () => _ = CheckInstallAsync(), !installChecking && !installBusy);
        recheck.Margin = new Thickness(0, 0, 12, 0); buttons.Children.Add(recheck);
        var start = Primary(T("install.start"), () => _ = InstallAsync(game), InstallAllowed && !installBusy && !packRunning);
        AutomationId(start, "install.start");
        buttons.Children.Add(start);
        var remove = new Button { Content = T("install.remove_game"), IsEnabled = !installBusy && !packRunning,
            Padding = new Thickness(14, 8), CornerRadius = new CornerRadius(8),
            Background = Brush.Parse("#29292D"), Foreground = Ink, Margin = new Thickness(12, 0, 0, 0),
            VerticalAlignment = VerticalAlignment.Center };
        AutomationId(remove, "install.remove_game");
        remove.Click += (_, _) => _ = ConfirmRemoveAsync(game);
        buttons.Children.Add(remove);
        if (readiness is { IpaPath.Length: > 0 } located)
        {
            var reveal = Action("progress.reveal_ipa", () => Reveal(located.IpaPath));
            reveal.Margin = new Thickness(12, 0, 0, 0);
            AutomationId(reveal, "install.reveal_ipa");
            buttons.Children.Add(reveal);
        }
        list.Children.Add(buttons);
        body.Children.Add(Card(list));

        if (installState != "")
        {
            var state = new StackPanel { Spacing = 8 };
            state.Children.Add(Label(T(installState switch
            {
                "running" => "install.running", "succeeded" => "install.done", "cancelled" => "progress.cancelled",
                "invalid" => "library.invalid_result", _ => "install.failed"
            }), 18));
            if (installBusy) state.Children.Add(new ProgressBar { IsIndeterminate = true, Height = 4 });
            if (installMessage.Length > 0) state.Children.Add(Muted(installMessage));
            if (installState is "failed" or "invalid")
                state.Children.Add(CopyReportButton(game.Id, ["install", "installRemove"], () => installMessage + "\n" + installDiagnostics));
            body.Children.Add(Card(state));
        }
        if (installDiagnostics.Length > 0)
            body.Children.Add(new Expander { Header = T("library.job_diagnostics"), IsExpanded = installState is "failed" or "invalid",
                HorizontalAlignment = HorizontalAlignment.Stretch,
                Content = new TextBox { Text = installDiagnostics, IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MaxHeight = 260 } });
    }

    private async Task CheckInstallAsync()
    {
        var game = SelectedGame ?? packGame;
        if (installChecking || installBusy || game is null || connectedFolder is null || libraryId is null) return;
        installChecking = true; lastCheck = DateTime.UtcNow;
        try
        {
            var job = await PackJobClient.RunAsync(PackRequest("installCheck", game.Id), null, CancellationToken.None);
            if (readinessFor == game.Id)
            {
                if (job.Report.Succeeded) { readiness = InstallReadiness.Parse(job.Report.Result); installCheckFailed = false; }
                else { installDiagnostics = JobDiagnostics(job); installCheckFailed = true; }
            }
        }
        catch (Exception error) when (error is LibraryProtocolException or IOException or UnauthorizedAccessException or ArgumentException or
            InvalidOperationException or OperationCanceledException or PlatformNotSupportedException or System.ComponentModel.Win32Exception)
        { installDiagnostics = error.Message; installCheckFailed = true; }
        finally { installChecking = false; lastCheck = DateTime.UtcNow; if (page == "install") Render(); }
    }

    private async Task InstallAsync(LibraryGame game)
    {
        if (installBusy || !InstallAllowed) return;
        installBusy = true; installState = "running"; installMessage = ""; installDiagnostics = ""; Render();
        try
        {
            var job = await PackJobClient.RunAsync(PackRequest("install", game.Id), e => Dispatcher.UIThread.Post(() =>
            {
                if (e.TryGetProperty("message", out var m) && m.ValueKind == JsonValueKind.String && m.GetString() is { Length: > 0 } text)
                { installMessage = text; Render(); }
            }), CancellationToken.None);
            installState = job.Report.State;
            installMessage = job.Report.Succeeded && job.Report.Result.TryGetProperty("device", out var device) && device.ValueKind == JsonValueKind.String
                ? T("install.device") + " · " + device.GetString() : job.Report.Message;
            installDiagnostics = JobDiagnostics(job);
        }
        catch (LibraryProtocolException error) { installState = "invalid"; installDiagnostics = error.Message; }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException or InvalidOperationException or
            OperationCanceledException or PlatformNotSupportedException or System.ComponentModel.Win32Exception)
        { installState = "failed"; installDiagnostics = error.Message; }
        finally { installBusy = false; readinessFor = null; Render(); }
    }

    private async Task ConfirmRemoveAsync(LibraryGame game)
    {
        if (installBusy || packRunning) return;
        var dialog = new Window
        {
            Title = T("install.remove_title"), Width = 420, SizeToContent = SizeToContent.Height,
            CanResize = false, WindowStartupLocation = WindowStartupLocation.CenterOwner,
            Background = Canvas, Foreground = Ink,
            FontFamily = new FontFamily("avares://S2Arm/Assets/Fonts#Manrope")
        };
        var confirmed = false;
        var body = new StackPanel { Spacing = 16, Margin = new Thickness(28) };
        body.Children.Add(Label(T("install.remove_title"), 20));
        body.Children.Add(Muted(T("install.remove_body"), 14));
        var buttons = new WrapPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
        var cancel = new Button { Content = T("action.cancel"), Padding = new Thickness(16, 9),
            Background = Brush.Parse("#29292D"), Foreground = Ink, CornerRadius = new CornerRadius(8),
            Margin = new Thickness(0, 0, 12, 0) };
        cancel.Click += (_, _) => dialog.Close();
        buttons.Children.Add(cancel);
        var confirm = new Button { Content = T("install.remove_confirm"), Padding = new Thickness(16, 9),
            Background = Accent, Foreground = Canvas, CornerRadius = new CornerRadius(8) };
        AutomationId(confirm, "install.remove_confirm");
        confirm.Click += (_, _) => { confirmed = true; dialog.Close(); };
        buttons.Children.Add(confirm);
        body.Children.Add(buttons);
        dialog.Content = body;
        await dialog.ShowDialog(this);
        if (confirmed) await RemoveAsync(game);
    }

    private async Task RemoveAsync(LibraryGame game)
    {
        if (installBusy || packRunning) return;
        installBusy = true; installState = "running"; installMessage = T("install.remove_running"); installDiagnostics = ""; Render();
        try
        {
            var job = await PackJobClient.RunAsync(PackRequest("installRemove", game.Id), e => Dispatcher.UIThread.Post(() =>
            {
                if (e.TryGetProperty("message", out var m) && m.ValueKind == JsonValueKind.String && m.GetString() is { Length: > 0 } text)
                { installMessage = text; Render(); }
            }), CancellationToken.None);
            installState = job.Report.State;
            installMessage = job.Report.Succeeded ? T("install.remove_done") : job.Report.Message;
            installDiagnostics = JobDiagnostics(job);
        }
        catch (LibraryProtocolException error) { installState = "invalid"; installDiagnostics = error.Message; }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException or InvalidOperationException or
            OperationCanceledException or PlatformNotSupportedException or System.ComponentModel.Win32Exception)
        { installState = "failed"; installMessage = T("install.remove_failed"); installDiagnostics = error.Message; }
        finally { installBusy = false; Render(); }
    }

    /// <summary>Closing while a pack runs: cancel through the job control plane first, then close.</summary>
    private async void ClosePackAsync(object? sender, WindowClosingEventArgs args)
    {
        if (!packRunning) return;
        args.Cancel = true;
        packCancellation?.Cancel();
        if (packOperation is { } operation) await operation;
        await Dispatcher.UIThread.InvokeAsync(() => { }, DispatcherPriority.Background);
        Close();
    }
}
