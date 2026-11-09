// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using S2Arm.Launcher.Protocol;

namespace S2Arm.Launcher;

public enum ImportKind { Games, Keys }

// 导入弹窗状态：欢迎页和游戏库页共用，在 EnvironmentWindow 字段里持有。
public sealed class ImportDialogState
{
    public ImportKind Kind;
    public bool Targeted;   // 拖拽高亮
    public ImportDialogState(ImportKind kind) { Kind = kind; }
}

public sealed partial class EnvironmentWindow
{
    // 欢迎页新手流程状态
    private string? welcomeGameId;
    private string[] pendingImportPaths = [];
    private bool keysRejected;
    private ImportDialogState? importDialog;
    private bool renderDeferred;   // 拖动途中到来的刷新，等松手或拖走再做

    private void RenderImportDialog(StackPanel body)
    {
        var dlg = importDialog!;
        var kind = dlg.Kind;
        var prefix = kind == ImportKind.Keys ? "welcome.sheet.keys." : "welcome.sheet.games.";
        string S(string id) => T(prefix + id);
        string C(string id) => T("welcome.sheet." + id);

        var panel = new StackPanel { Spacing = 0, MaxWidth = 560, HorizontalAlignment = HorizontalAlignment.Center };
        panel.Children.Add(Label(S("title"), 26));
        panel.Children.Add(new TextBlock { Text = S("what"), FontSize = 14, Foreground = Brush.Parse("#A8A6A1"),
            TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 6, 0, 20) });

        // 进行中：进度；否则拖拽区
        if (importBusy)
        {
            var running = new StackPanel { Spacing = 10, Height = 220,
                VerticalAlignment = VerticalAlignment.Center, HorizontalAlignment = HorizontalAlignment.Center };
            if (importJobName.Length > 0)
                running.Children.Add(Label(importJobName, 16));
            running.Children.Add(new ProgressBar { IsIndeterminate = true, Height = 4, MaxWidth = 380 });
            if (importJobMessage.Length > 0)
                running.Children.Add(new TextBlock { Text = importJobMessage, FontSize = 12.5,
                    Foreground = Brush.Parse("#A8A6A1"), TextWrapping = TextWrapping.Wrap });
            panel.Children.Add(running);
        }
        else
        {
            var zone = new Border
            {
                Height = 220, CornerRadius = new CornerRadius(16),
                Child = BuildDropZoneContent(kind, S, C)
            };
            // 拖动过程中只改这个控件的样子，不能 Render：Render 换掉控件后，松手事件发给旧控件，到不了这里
            zone.AddHandler(DragDrop.DragEnterEvent, (object? _, DragEventArgs e) => { e.DragEffects = DragDropEffects.Copy; SetDropTargeted(dlg, zone, true); });
            zone.AddHandler(DragDrop.DragOverEvent, (object? _, DragEventArgs e) => e.DragEffects = DragDropEffects.Copy);
            zone.AddHandler(DragDrop.DragLeaveEvent, (object? _, DragEventArgs _2) => SetDropTargeted(dlg, zone, false));
            zone.AddHandler(DragDrop.DropEvent, (object? _, DragEventArgs e) =>
            {
                e.DragEffects = DragDropEffects.Copy;
                SetDropTargeted(dlg, zone, false);
                AcceptDrop(e.DataTransfer.TryGetFiles(), kind);
            });
            StyleDropZone(zone, false);
            DragDrop.SetAllowDrop(zone, true);
            panel.Children.Add(zone);
        }

        // 失败或密钥拒绝提示
        if (kind == ImportKind.Keys && keysRejected && !importBusy)
            panel.Children.Add(new TextBlock { Text = T("welcome.keys.unusable"), FontSize = 13,
                Foreground = Brush.Parse("#E55"), TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 12, 0, 0) });
        if (importError.Length > 0)
        {
            panel.Children.Add(new TextBlock { Text = importError, FontSize = 13,
                Foreground = Brush.Parse("#E55"), TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 12, 0, 0) });
            var copyError = CopyReportButton(null, ["import", "keys", "setup"], () => importError);
            copyError.Margin = new Thickness(0, 10, 0, 0); copyError.HorizontalAlignment = HorizontalAlignment.Left;
            panel.Children.Add(copyError);
        }

        var footer = new StackPanel { Orientation = Orientation.Horizontal,
            HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 16, 0, 0), Spacing = 8 };
        if (importBusy)
        {
            var cancel = Action("welcome.tool.cancel", () => importCancellation?.Cancel(), !false);
            footer.Children.Add(cancel);
        }
        else
        {
            var close = Action("welcome.sheet.cancel", CloseImportDialog);
            footer.Children.Add(close);
        }
        panel.Children.Add(footer);
        body.Children.Add(Card(panel, 28));
    }

    private static void StyleDropZone(Border zone, bool targeted)
    {
        zone.Background = targeted ? Brush.Parse("#12FFFFFF") : Brush.Parse("#19191C");
        zone.BorderBrush = targeted ? Accent : Brush.Parse("#30FFFFFF");
        zone.BorderThickness = new Thickness(targeted ? 1.5 : 1);
    }

    private void SetDropTargeted(ImportDialogState dlg, Border zone, bool targeted)
    {
        dlg.Targeted = targeted;
        StyleDropZone(zone, targeted);
        if (!targeted && renderDeferred) { renderDeferred = false; Render(); }
    }

    private void AcceptDrop(IEnumerable<IStorageItem>? items, ImportKind kind)
    {
        // 文件夹也要收（整理好的游戏文件夹、解包好的游戏），密钥只收文件；认不认识这些内容由作业层判
        var paths = (items ?? []).Where(i => i.Path.IsFile && (kind == ImportKind.Games || i is IStorageFile))
            .Select(LocalPath).ToArray();
        if (paths.Length == 0) { importError = T("welcome.sheet.dropUnreadable"); Render(); return; }
        AcceptImport(paths, kind);
    }

    private Control BuildDropZoneContent(ImportKind kind, Func<string, string> S, Func<string, string> C)
    {
        var inner = new StackPanel { Spacing = 12, VerticalAlignment = VerticalAlignment.Center,
            HorizontalAlignment = HorizontalAlignment.Center };
        var icon = new TextBlock
        {
            Text = kind == ImportKind.Keys ? "🗝" : "📦", FontSize = 34,
            HorizontalAlignment = HorizontalAlignment.Center
        };
        inner.Children.Add(icon);
        inner.Children.Add(new TextBlock { Text = S("drop"), FontSize = 18, FontWeight = FontWeight.Bold,
            Foreground = Ink, HorizontalAlignment = HorizontalAlignment.Center });
        inner.Children.Add(new TextBlock { Text = C("or"), FontSize = 12.5, Foreground = Brush.Parse("#A8A6A1"),
            HorizontalAlignment = HorizontalAlignment.Center });
        var browse = new Button
        {
            Content = S("browse"), Background = Accent, Foreground = Canvas,
            Padding = new Thickness(20, 9), CornerRadius = new CornerRadius(20),
            FontSize = 14, FontWeight = FontWeight.SemiBold,
            HorizontalAlignment = HorizontalAlignment.Center
        };
        browse.Click += (_, _) => BeginPicker(() => BrowseImportAsync(kind));
        inner.Children.Add(browse);
        return inner;
    }

    private void CloseImportDialog()
    {
        importDialog = null; importError = ""; keysRejected = false;
        pendingImportPaths = [];
        Render();
    }

    // ---- import job state ----
    private bool importBusy;
    private string importJobName = "", importJobMessage = "", importError = "";
    private CancellationTokenSource? importCancellation;

    private async Task BrowseImportAsync(ImportKind kind)
    {
        if (kind == ImportKind.Keys)
        {
            var files = await StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
            {
                Title = T("welcome.step.keys.panel"), AllowMultiple = false,
                FileTypeFilter = [new FilePickerFileType(T("library.import_keys")) { Patterns = ["prod.keys", "*.keys"] }]
            });
            if (files.Count == 0) return;
            AcceptImport([LocalPath(files[0])], ImportKind.Keys);
        }
        else
        {
            var files = await StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
            {
                Title = T("library.import_files"), AllowMultiple = true,
                FileTypeFilter = [new FilePickerFileType(T("library.import_files")) { Patterns = ["*.nsp", "*.xci", "*.nsz", "*.xcz"] }]
            });
            var folders = await StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions
                { Title = T("library.import_folder") });
            var paths = files.Select(LocalPath).Concat(folders.Select(LocalPath)).ToArray();
            if (paths.Length == 0) return;
            AcceptImport(paths, ImportKind.Games);
        }
    }

    private void AcceptImport(string[] paths, ImportKind kind)
    {
        if (importBusy) return;
        if (connectedFolder is null || libraryId is null) { importError = T("welcome.sheet.dropBusy"); Render(); return; }
        if (kind == ImportKind.Keys)
            _ = RunImportKeysAsync(paths[0]);
        else
            _ = RunImportGamesAsync(paths);
    }

    private async Task RunImportKeysAsync(string keyPath)
    {
        if (importBusy || connectedFolder is null || libraryId is null) return;
        importBusy = true; importError = ""; importJobName = Path.GetFileName(keyPath); importJobMessage = "";
        keysRejected = false; Render();
        using var cts = new CancellationTokenSource(); importCancellation = cts;
        try
        {
            var result = await LibraryJobClient.RunAsync(paths, privateRoot, connectedFolder, libraryId,
                "keys", [keyPath], cts.Token);
            DisplayJob(result);
            if (!result.Report.Succeeded) { importError = result.Report.Message; return; }
            // 放好密钥后重查 catalog，接着导入悬挂的游戏
            var listing = await LibraryJobClient.RunAsync(paths, privateRoot, connectedFolder, libraryId,
                "catalog", null, cts.Token);
            if (listing.Report.Succeeded)
            { catalog = LibraryCatalog.Parse(listing.Report.Result); ClearCovers(); }
            importDialog = null;
            if (pendingImportPaths.Length > 0)
            {
                var pending = pendingImportPaths; pendingImportPaths = [];
                importBusy = false; importCancellation = null;
                _ = RunImportGamesAsync(pending);
                return;
            }
        }
        catch (OperationCanceledException) { importError = ""; }
        catch (LibraryProtocolException error) { importError = error.Message; }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException or
            InvalidOperationException or System.ComponentModel.Win32Exception)
        { importError = error.Message; }
        finally { importCancellation = null; importBusy = false; Render(); }
    }

    private async Task RunImportGamesAsync(string[] filePaths)
    {
        if (importBusy || connectedFolder is null || libraryId is null) return;
        importBusy = true; importError = ""; importJobName = Path.GetFileName(filePaths[0]); importJobMessage = "";
        keysRejected = false; Render();
        using var cts = new CancellationTokenSource(); importCancellation = cts;
        try
        {
            var result = await LibraryJobClient.RunAsync(paths, privateRoot, connectedFolder, libraryId,
                "import", filePaths, cts.Token);
            DisplayJob(result);
            if (!result.Report.Succeeded)
            {
                // 检查错误码
                var code = GetDiagCode(result);
                if (code == "keys_missing")
                {
                    // 记下待导入文件，打开放密钥弹窗
                    pendingImportPaths = filePaths;
                    importDialog = new ImportDialogState(ImportKind.Keys);
                    importError = "";
                }
                else if (code == "content_unreadable" && catalog?.KeysReady == true)
                {
                    // 密钥在但读不了：留选择，提示换一份
                    pendingImportPaths = filePaths; keysRejected = true;
                    importDialog = new ImportDialogState(ImportKind.Keys);
                    importError = "";
                }
                else { importError = result.Report.Message; }
                return;
            }
            // 成功：重查目录，更新欢迎页状态
            var listing = await LibraryJobClient.RunAsync(paths, privateRoot, connectedFolder, libraryId,
                "catalog", null, cts.Token);
            if (listing.Report.Succeeded)
            {
                catalog = LibraryCatalog.Parse(listing.Report.Result); ClearCovers();
                if (welcomeGameId == null)
                {
                    var visible = catalog.Games.Where(g => !g.Hidden).ToArray();
                    if (visible.Length > 0) welcomeGameId = visible[0].Id;
                }
            }
            pendingImportPaths = [];
            importDialog = null;   // 成功自动关
        }
        catch (OperationCanceledException) { importError = ""; }
        catch (LibraryProtocolException error) { importError = error.Message; }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException or
            InvalidOperationException or System.ComponentModel.Win32Exception)
        { importError = error.Message; }
        finally { importCancellation = null; importBusy = false; Render(); }
    }

    private static string? GetDiagCode(LibraryJobOutput job) => DiagnosticCode(job.Report.Diagnostics);

    private static string? DiagnosticCode(System.Text.Json.JsonElement diagnostics)
    {
        if (diagnostics.ValueKind == System.Text.Json.JsonValueKind.Array)
            foreach (var d in diagnostics.EnumerateArray())
                if (d.TryGetProperty("code", out var code) && code.GetString() is { Length: > 0 } c)
                    return c;
        return null;
    }
}
