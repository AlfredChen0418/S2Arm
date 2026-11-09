// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using System.Text.Json;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Platform.Storage;
using S2Arm.Launcher.Protocol;

namespace S2Arm.Launcher;

public sealed partial class EnvironmentWindow
{
    private string page = "library";
    private string libraryFolder = "";
    private string? connectedFolder, libraryId, selectedGame;
    private LibraryCatalog? catalog;
    private string librarySummary = "library.disconnected", libraryDiagnostics = "";
    private bool libraryBusy, closingLibrary;
    private CancellationTokenSource? libraryCancellation;
    private Task? libraryOperation;
    private TextBox libraryFolderBox = new();
    private readonly Dictionary<string, Bitmap> covers = new(StringComparer.OrdinalIgnoreCase);
    private readonly string privateRoot = ResolvePrivateRoot();

    private void Navigate(string destination)
    {
        CapturePaths();
        if (page == "library") libraryFolder = libraryFolderBox.Text ?? "";
        page = destination; Render();
    }

    private void ClearCovers()
    {
        foreach (var cover in covers.Values) cover.Dispose();
        covers.Clear();
    }

    private async void CloseLibraryAsync(object? sender, WindowClosingEventArgs args)
    {
        if (!libraryBusy) return;
        args.Cancel = true;
        if (closingLibrary) return;
        closingLibrary = true;
        libraryCancellation?.Cancel();
        if (libraryOperation is { } operation) await operation;
        Close();
    }

    private Border Card(Control content, int padding = 20) => new()
    {
        Background = Surface, CornerRadius = new CornerRadius(12), Padding = new Thickness(padding), Child = content
    };

    private Control GameCover(LibraryGame game, int size)
    {
        // Only a backend-provided local cover in this library is considered; never load a remote URL.
        try
        {
            if (connectedFolder is not null && game.Cover.Length > 0)
            {
                var root = Path.GetFullPath(Path.Combine(connectedFolder, "games")) + Path.DirectorySeparatorChar;
                var folder = Path.GetFullPath(Path.Combine(root, game.Id)) + Path.DirectorySeparatorChar;
                var path = Path.GetFullPath(game.Cover);
                // 只认这款游戏目录下的 cover.jpg 或玩家换的 cover.custom.<8 位十六进制>.jpg（后端 _CUSTOM_COVER）
                if (folder.StartsWith(root, StringComparison.OrdinalIgnoreCase) && path.StartsWith(folder, StringComparison.OrdinalIgnoreCase) &&
                    System.Text.RegularExpressions.Regex.IsMatch(Path.GetFileName(path), @"^cover(\.custom\.[0-9a-f]{8})?\.jpg$") &&
                    Path.GetDirectoryName(path) + Path.DirectorySeparatorChar == folder && new FileInfo(path).Length <= 16 * 1024 * 1024)
                {
                    if (!covers.TryGetValue(path, out var image))
                    {
                        using var file = File.OpenRead(path);
                        image = Bitmap.DecodeToWidth(file, 240);
                        covers.Add(path, image);
                    }
                    return new Image { Source = image, Width = size, Height = size, Stretch = Stretch.UniformToFill };
                }
            }
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException or NotSupportedException) { }
        return new Border { Width = size, Height = size, CornerRadius = new CornerRadius(8),
            Background = Brush.Parse("#303035"), Padding = new Thickness(size / 6),
            Child = new Image
            {
                Source = LoadAppIcon(),
                Stretch = Stretch.Uniform
            } };
    }

    private Bitmap? appIconBitmap;
    private Bitmap LoadAppIcon()
    {
        if (appIconBitmap is not null) return appIconBitmap;
        using var stream = Avalonia.Platform.AssetLoader.Open(new Uri("avares://S2Arm/Assets/AppIcon.png"));
        return appIconBitmap = Bitmap.DecodeToWidth(stream, 480);
    }

    private void RenderLibrary(StackPanel body)
    {
        body.Children.Add(Label(T("library.title"), 23));
        body.Children.Add(Label(T("library.description")));

        // In-progress capsule: shown when a pack is running and the session is hidden
        if (packRunning && sessionHidden)
        {
            var elapsed = (DateTime.UtcNow - packStarted).TotalSeconds;
            var estimate = packTrack.Estimate(Clock);
            var fraction = OverallFraction(estimate, packTrack.Plan);
            var capsuleBar = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto"), ColumnSpacing = 16 };
            var left = new StackPanel { Spacing = 4, VerticalAlignment = VerticalAlignment.Center };
            left.Children.Add(Muted(T("progress.running") + " · " + (packGame?.Name ?? ""), 13));
            left.Children.Add(new ProgressBar { Minimum = 0, Maximum = 1, Value = fraction, Height = 4, Foreground = Accent });
            capsuleBar.Children.Add(left);
            var back = Primary(T("export.pill.running"), () => { sessionHidden = false; Navigate("progress"); });
            back.Padding = new Thickness(14, 8); back.CornerRadius = new CornerRadius(16);
            Grid.SetColumn(back, 1); capsuleBar.Children.Add(back);
            body.Children.Add(new Border { Background = Brush.Parse("#1D1D20"), CornerRadius = new CornerRadius(12),
                Padding = new Thickness(16, 12), Child = capsuleBar });
        }

        var games = catalog?.Games.Where(g => !g.Hidden).ToArray() ?? [];
        var hero = games.FirstOrDefault(g => g.Id == selectedGame) ?? games.FirstOrDefault();
        if (hero is not null)
        {
            var banner = new Grid { ColumnDefinitions = new ColumnDefinitions("100,*"), ColumnSpacing = 24 };
            banner.Children.Add(GameCover(hero, 100));
            var title = new StackPanel { Spacing = 8 };
            title.Children.Add(Label(T(selectedGame is null ? "library.title" : "library.selected")));
            title.Children.Add(Label(hero.Name, 28));
            title.Children.Add(Label(T("library.version") + " · " + hero.Version));
            Grid.SetColumn(title, 1); banner.Children.Add(title); body.Children.Add(Card(banner, 20));
        }

        var connection = new StackPanel { Spacing = 10 };
        connection.Children.Add(Label(T("library.folder")));
        libraryFolderBox = new TextBox { Text = libraryFolder, IsEnabled = !libraryBusy };
        connection.Children.Add(libraryFolderBox);
        var buttons = new WrapPanel { Orientation = Orientation.Horizontal };
        void AddButton(string key, Action action, bool enabled = true)
        {
            var button = Action(key, action, enabled && !libraryBusy); button.Margin = new Thickness(0, 0, 10, 8);
            buttons.Children.Add(button);
        }
        AddButton("library.choose_folder", () => BeginPicker(ChooseLibraryAsync));
        AddButton("library.connect", () => BeginLibrary("setup"));
        AddButton("library.refresh", () => BeginLibrary("catalog"), libraryId is not null);
        connection.Children.Add(buttons);
        connection.Children.Add(Label(T("library.source_hint")));
        if (catalog is null) body.Children.Add(Card(connection));
        else body.Children.Add(new Expander { Header = T("library.folder") + " · " + connectedFolder,
            HorizontalAlignment = HorizontalAlignment.Stretch, Content = Card(connection) });

        var status = new StackPanel { Spacing = 8 };
        status.Children.Add(Label(T(librarySummary), 18));
        if (libraryBusy) status.Children.Add(new ProgressBar { IsIndeterminate = true, Height = 4 });
        if (catalog is not null)
        {
            status.Children.Add(Label(T("library.keys_status") + " · " + catalog.KeysMessage));
            var imports = new WrapPanel { Orientation = Orientation.Horizontal };
            foreach (var (key, handler) in new (string, Func<Task>)[] {
                ("library.import_keys", () => ChooseFilesAsync(true)),
                ("library.import_files", () => ChooseFilesAsync(false)),
                ("library.import_folder", ChooseSourceFolderAsync) })
            {
                var button = Action(key, () => BeginPicker(handler), !libraryBusy);
                button.Margin = new Thickness(0, 0, 10, 8); imports.Children.Add(button);
            }
            status.Children.Add(imports);
        }
        body.Children.Add(Card(status));

        foreach (var game in games)
        {
            var line = new Grid { ColumnDefinitions = new ColumnDefinitions("72,*,Auto"), ColumnSpacing = 18 };
            line.Children.Add(GameCover(game, 72));
            var detail = new StackPanel { Spacing = 5 };
            detail.Children.Add(Label(game.Name, 18));
            detail.Children.Add(Label(T("library.version") + " · " + game.Version));
            if (game.Missing.Count > 0)
                detail.Children.Add(Label(T("library.requirements") + " · " + string.Join(" / ", game.Missing)));
            // iPhone / Mac dual status line
            detail.Children.Add(GameStatusLine(game));
            Grid.SetColumn(detail, 1); line.Children.Add(detail);
            // Primary button: if iPhone pack is ready → install; otherwise export
            var primaryKey = game.IphoneReady ? "library.hero.install" : "library.select";
            var primaryAction = game.IphoneReady
                ? (Action)(() => { libraryFolder = libraryFolderBox.Text ?? ""; selectedGame = game.Id; readinessFor = null; Navigate("install"); })
                : () => { libraryFolder = libraryFolderBox.Text ?? ""; selectedGame = game.Id; OpenBuild(); };
            var select = Action(primaryKey, primaryAction, !libraryBusy && !PackBusy);
            select.VerticalAlignment = VerticalAlignment.Center;
            // secondary: show past result if one exists and game is not currently running
            if (finishedPacks.TryGetValue(game.Id, out _) && !(packRunning && packGame?.Id == game.Id))
            {
                var actionCol = new StackPanel { Spacing = 6, VerticalAlignment = VerticalAlignment.Center };
                actionCol.Children.Add(select);
                var results = Action("library.hero.results", () =>
                { libraryFolder = libraryFolderBox.Text ?? ""; selectedGame = game.Id; Navigate("progress"); }, !libraryBusy && !PackBusy);
                actionCol.Children.Add(results);
                Grid.SetColumn(actionCol, 2); line.Children.Add(actionCol);
            }
            else
            {
                Grid.SetColumn(select, 2); line.Children.Add(select);
            }
            body.Children.Add(Card(line, 12));
        }
        if (catalog is not null && games.Length == 0) body.Children.Add(Label(T("library.empty"), 18));
        if (libraryDiagnostics.Length > 0)
            body.Children.Add(new Expander { Header = T("library.job_diagnostics"), IsExpanded = librarySummary is "library.failed" or "library.invalid_result" or "library.interrupted",
                HorizontalAlignment = HorizontalAlignment.Stretch,
                Content = new TextBox { Text = libraryDiagnostics, IsReadOnly = true, AcceptsReturn = true,
                    TextWrapping = TextWrapping.Wrap, MaxHeight = 240 } });
    }

    /// <summary>iPhone / Mac dual status line for a game card — mirrors Mac statusLine(game).</summary>
    private Control GameStatusLine(LibraryGame game)
    {
        string text; string color;
        // Running or queued
        if (packRunning && packGame?.Id == game.Id)
        {
            text = T("s2arm.job.running"); color = "#FFC857";
        }
        else if (finishedPacks.ContainsKey(game.Id) && packRunning && packGame?.Id == game.Id)
        {
            text = T("export.status.queued"); color = "#FFC857";
        }
        else
        {
            var parts = new List<string>();
            if (game.IphoneReady)
            {
                if (game.IphoneInstalledAt is not null)
                {
                    // days left (free signing = 7 days from install)
                    if (game.IphoneFree)
                    {
                        var daysLeft = (int)Math.Ceiling((game.IphoneInstalledAt.Value + 7 * 86400 - DateTimeOffset.UtcNow.ToUnixTimeSeconds()) / 86400.0);
                        daysLeft = Math.Max(0, daysLeft);
                        if (daysLeft == 0)
                            parts.Add(T("library.status.expired"));
                        else
                            parts.Add(T("library.status.installed").Replace("{0}", daysLeft.ToString()));
                    }
                    else parts.Add(T("library.status.installedPaid"));
                }
                else parts.Add(T("library.status.iphone"));
            }
            if (parts.Count == 0 && finishedPacks.TryGetValue(game.Id, out var fin) && fin.state == "succeeded")
                parts.Add(T("library.status.iphone"));
            if (parts.Count == 0)
            {
                text = T("s2arm.library.notPrepared"); color = "#4A4A50";
                goto render;
            }
            text = string.Join(" · ", parts);
            color = game.IphoneInstalledAt is not null && game.IphoneFree &&
                (int)Math.Ceiling((game.IphoneInstalledAt.Value + 7 * 86400 - DateTimeOffset.UtcNow.ToUnixTimeSeconds()) / 86400.0) == 0
                ? "#E05C5C" : "#52C87A";
        }
        render:
        var dot = new Border { Width = 7, Height = 7, CornerRadius = new CornerRadius(4),
            Background = Brush.Parse(color), Margin = new Thickness(0, 3, 6, 0), VerticalAlignment = VerticalAlignment.Top };
        var lbl = new TextBlock { Text = text, FontSize = 12, Foreground = Brush.Parse(color), TextWrapping = TextWrapping.Wrap };
        var row = new StackPanel { Orientation = Orientation.Horizontal };
        row.Children.Add(dot); row.Children.Add(lbl);
        return row;
    }


    private void BeginPicker(Func<Task> picker)
    {
        if (libraryBusy) return;
        libraryFolder = libraryFolderBox.Text ?? "";
        libraryOperation = GuardPickerAsync(picker);
    }

    private async Task GuardPickerAsync(Func<Task> picker)
    {
        try { await picker(); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or InvalidOperationException or ArgumentException)
        { librarySummary = "library.failed"; libraryDiagnostics = error.Message; Render(); }
    }

    private static string LocalPath(IStorageItem item) => item.Path.IsFile ? Path.GetFullPath(item.Path.LocalPath)
        : throw new IOException("The selected item does not have a local filesystem path.");

    private async Task ChooseLibraryAsync()
    {
        var folders = await StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions { Title = T("library.choose_folder") });
        if (folders.Count == 0) return;
        libraryFolder = LocalPath(folders[0]); Render();
    }

    private string LibraryPreference => Path.Combine(privateRoot, "library-location.txt");

    private string SavedLibrary()
    {
        try { var saved = File.ReadAllText(LibraryPreference).Trim(); return saved.Length > 0 ? saved : DefaultLibrary; }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { return DefaultLibrary; }
    }

    private string DefaultLibrary => Path.Combine(privateRoot, "DefaultLibrary");

    private async Task ChangeLibraryAsync()
    {
        var folders = await StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions { Title = T("library.choose_folder") });
        if (folders.Count > 0) ConnectLibrary(LocalPath(folders[0]));
    }

    /// <summary>换到新位置：连上后记下来，下次启动还用它。</summary>
    private void ConnectLibrary(string folder)
    {
        connectedFolder = null; libraryId = null;
        libraryFolder = folder;
        libraryOperation = SwitchLibraryAsync(folder);
    }

    private async Task SwitchLibraryAsync(string folder)
    {
        await EnsureLibraryAsync(folder, () => { });
        if (connectedFolder is null) return;
        try { PrivateJobRequest.EnsurePrivateRoot(privateRoot); File.WriteAllText(LibraryPreference, folder); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { importError = error.Message; Render(); }
    }

    private async Task ChooseSourceFolderAsync()
    {
        var folders = await StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions { Title = T("library.import_folder") });
        if (folders.Count == 0) return;
        await RunLibraryAsync("import", [LocalPath(folders[0])]);
    }

    private async Task ChooseFilesAsync(bool keys)
    {
        var files = await StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions {
            Title = T(keys ? "library.choose_keys" : "library.import_files"), AllowMultiple = !keys,
            FileTypeFilter = [new FilePickerFileType(T(keys ? "library.import_keys" : "library.import_files"))
                { Patterns = keys ? ["prod.keys", "*.keys"] : ["*.nsp", "*.xci", "*.nsz", "*.xcz"] }] });
        if (files.Count == 0) return;
        await RunLibraryAsync(keys ? "keys" : "import", files.Select(LocalPath).ToArray());
    }

    private void BeginLibrary(string operation)
    {
        if (libraryBusy) return;
        libraryFolder = libraryFolderBox.Text ?? "";
        libraryOperation = RunLibraryAsync(operation, null);
    }

    private async Task RunLibraryAsync(string operation, string[]? selectedPaths)
    {
        if (libraryBusy) return;
        if (operation != "setup")
        {
            if (connectedFolder is null || libraryId is null) return;
            string pending;
            try { pending = Path.GetFullPath(libraryFolder); }
            catch (ArgumentException) { pending = ""; }
            if (!string.Equals(pending, connectedFolder, StringComparison.OrdinalIgnoreCase))
            {
                // A changed output field must never send an import to the previous library.
                connectedFolder = null; libraryId = null; catalog = null; selectedGame = null; ClearCovers();
                librarySummary = "library.disconnected"; Render(); return;
            }
        }
        libraryBusy = true; librarySummary = operation == "catalog" ? "library.loading" : "library.working";
        libraryDiagnostics = ""; Render();
        using var cancellation = new CancellationTokenSource();
        libraryCancellation = cancellation;
        try
        {
            if (operation == "setup")
            {
                connectedFolder = null; libraryId = null; catalog = null; selectedGame = null; ClearCovers();
                var folder = Path.GetFullPath(libraryFolder);
                var setup = await LibraryJobClient.RunAsync(paths, privateRoot, folder, null, "setup", null, cancellation.Token);
                DisplayJob(setup);
                if (!setup.Report.Succeeded) { librarySummary = "library.failed"; return; }
                var connection = LibraryConnection.Parse(setup.Report.Result);
                connectedFolder = connection.Folder; libraryId = connection.Id; libraryFolder = connectedFolder;
            }
            else if (operation != "catalog")
            {
                var output = await LibraryJobClient.RunAsync(paths, privateRoot, connectedFolder!, libraryId, operation, selectedPaths, cancellation.Token);
                DisplayJob(output);
                if (!output.Report.Succeeded) { librarySummary = "library.failed"; return; }
            }
            var listing = await LibraryJobClient.RunAsync(paths, privateRoot, connectedFolder!, libraryId, "catalog", null, cancellation.Token);
            DisplayJob(listing);
            if (!listing.Report.Succeeded) { librarySummary = "library.failed"; return; }
            catalog = LibraryCatalog.Parse(listing.Report.Result); ClearCovers();
            if (!catalog.Games.Any(g => g.Id == selectedGame && !g.Hidden)) selectedGame = null;
            librarySummary = "library.completed";
        }
        catch (LibraryProtocolException error)
        { librarySummary = "library.invalid_result"; libraryDiagnostics += "\n" + error.Message; }
        catch (OperationCanceledException)
        { librarySummary = "library.interrupted"; }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException or
            InvalidOperationException or System.ComponentModel.Win32Exception or KeyNotFoundException)
        { librarySummary = "library.failed"; libraryDiagnostics += "\n" + error.Message; }
        finally
        { libraryCancellation = null; libraryBusy = false; Render(); }
    }

    private void DisplayJob(LibraryJobOutput job)
    {
        libraryDiagnostics = job.Report.Message + "\n" + T("diagnostics.exit_code") + ": " + job.Output.ExitCode;
        if (job.Report.Diagnostics.ValueKind == JsonValueKind.Array)
            foreach (var diagnostic in job.Report.Diagnostics.EnumerateArray())
            {
                if (diagnostic.TryGetProperty("code", out var code))
                {
                    var value = code.GetString();
                    libraryDiagnostics += "\n" + T("errors." + value + ".title") + "\n" + T("errors." + value + ".detail");
                }
                foreach (var key in new[] { "summary", "suggestion" })
                    if (diagnostic.TryGetProperty(key, out var message) && message.ValueKind == JsonValueKind.String)
                        libraryDiagnostics += "\n" + message.GetString();
            }
        if (!string.IsNullOrWhiteSpace(job.Output.StandardError)) libraryDiagnostics += "\n" + job.Output.StandardError;
    }
}
