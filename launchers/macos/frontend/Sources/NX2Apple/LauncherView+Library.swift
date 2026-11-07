// Derived from NX2Apple frontend/Sources/NX2Apple/LauncherView+Library.swift (908e4e84, MIT; see ../../UPSTREAM.md).
// S2Arm changes: the games are S2ArmAppleAdapter GameSummary (catalog job) instead of GameLibrary/LibraryGame; the artwork is the
// library's cover.jpg drawn by CoverFill (upstream's ArtworkFill / artwork editor / slots are not taken); the hero's primary action is
// Export (11-07 维护者: the launcher is built around exporting, Mac and iPhone / iPad alike; it opens ExportView), and a game whose Mac
// version is ready also gets Play (launch job) beside it, then the launch options button and Free up space (FreeSpaceView: spaceSurvey / spaceFree jobs); the cards and the hero's buttons carry
// their own accessibility labels (a card read as an image before); the empty library offers Add games; the rebuild, iPhone
// re-sign, Manage menu, Switch 2 badge and library filter are left out.
import AppKit
import SwiftUI
import NXCore
import NXModel
import NXLauncher
import S2ArmAppleAdapter

extension LauncherView {
    var libraryPage: some View {
        GeometryReader { geometry in
            if model.games.isEmpty && !model.libraryLoaded {
                loadingLibrary
            } else if model.showsWelcome {
                emptyLibrary
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        renewBanner
                        if let game = model.selected {
                            hero(game, width: geometry.size.width, height: max(240, geometry.size.height - collectionHeight))
                        }
                        collection(width: geometry.size.width)
                            .background(GeometryReader { g in Color.clear.preference(key: CollectionHeightKey.self, value: g.size.height) })
                    }
                }
                .onPreferenceChange(CollectionHeightKey.self) { h in if h > 0, abs(h - collectionHeight) > 0.5 { collectionHeight = h } }
            }
        }
    }

    /// 第一次读游戏库还没回来：只显示「正在读取」，不先闪「还没有游戏」（11-07 界面验收）。
    var loadingLibrary: some View {
        VStack(spacing: 14) {
            ProgressView().controlSize(.regular)
            Text(loc.t("s2arm.library.loading")).font(.manrope(15)).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    /// 游戏库还是空的：欢迎页兼环境检查（11-08 维护者），有游戏后回到游戏库。
    var emptyLibrary: some View {
        WelcomeView(model: model)
    }

    func hero(_ game: GameSummary, width: CGFloat, height: CGFloat) -> some View {
        ZStack(alignment: .bottomLeading) {
            if let image = covers.image(game.cover) {
                CoverFill(image: image).frame(width: width, height: height).clipped()
            } else {
                LauncherPalette.surface
                Image(nsImage: BrandArt.wordmark).resizable().interpolation(.high).scaledToFit().frame(width: 300, height: 78)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topTrailing).padding(54)
            }
            LinearGradient(stops: [.init(color: .clear, location: 0.25),
                                   .init(color: .black.opacity(0.25), location: 0.45),
                                   .init(color: LauncherPalette.background, location: 1)],
                           startPoint: .top, endPoint: .bottom)
            LinearGradient(colors: [.black.opacity(0.24), .clear], startPoint: .leading, endPoint: .trailing)
            VStack(alignment: .leading, spacing: 14) {
                status(game, prominent: true)
                Text(game.name).font(.manrope(min(68, width * 0.052), .heavy))
                    .tracking(-1.6).lineLimit(2).shadow(color: .black.opacity(0.25), radius: 12)
                HStack(spacing: 14) {
                    let queueable = !model.busy || model.session?.export != nil
                    if game.iphoneReady {
                        // 包已出好：主按钮是装机 / 续签，重新出包退到次要
                        Button { installFor = game.id } label: {
                            LauncherHeroActionLabel(title: txt(game.iphoneInstalledAt != nil && game.iphoneFree ? "library.hero.renew" : "library.hero.install"), symbol: "iphone")
                        }
                        .buttonStyle(.plain).keyboardShortcut(.return, modifiers: []).accessibilityIdentifier("hero.install")
                        heroCapsule(loc.t("s2arm.export.action"), symbol: "shippingbox", id: "hero.export", enabled: queueable) { model.exportFor = game.id }
                    } else {
                        Button { model.exportFor = game.id } label: {
                            LauncherHeroActionLabel(title: loc.t("s2arm.export.action"), symbol: "shippingbox.fill")
                        }
                        .buttonStyle(.plain).disabled(!queueable)
                        .keyboardShortcut(.return, modifiers: [])
                        .accessibilityLabel(loc.t("s2arm.export.action")).accessibilityIdentifier("hero.export")
                        .padTarget("hero.export", scope: "main", radius: 30) { model.exportFor = game.id }
                    }
                    if game.playable {
                        heroCapsule(loc.t("library.play"), symbol: "play.fill", id: "hero.play", enabled: !model.busy) { model.play(game, uiLanguage: uiLanguage) }
                    }
                    if model.finishedExports[game.id] != nil, !(model.busy && model.session?.gameID == game.id) {
                        heroCapsule(txt("library.hero.results"), symbol: "checkmark.seal", id: "hero.results", enabled: true) { model.viewingResult = model.finishedExports[game.id] }
                    }
                    heroCircle("externaldrive", label: loc.t("s2arm.action.freeSpace"), id: "hero.space") { spaceFor = game.id }
                    heroCircle("ellipsis", label: txt("game.more"), id: "hero.more") { moreFor = game.id }
                        .popover(isPresented: Binding(get: { moreFor == game.id }, set: { if !$0 { moreFor = nil } }), arrowEdge: .bottom) { moreMenu(game) }
                }
                if let error = model.actionError(for: game.id) {
                    Text(loc.backend(error)).font(.manrope(12.5)).foregroundStyle(LauncherPalette.danger).fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.horizontal, 34).padding(.bottom, 34)
        }
        .frame(width: width, height: height).clipped()
    }

    /// A round secondary action of the hero (the launch options; more round actions sit after it).
    func heroCircle(_ symbol: String, label: String, id: String, enabled: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 17))
                .frame(width: 48, height: 48)
                .background(Color.white.opacity(0.10), in: Circle())
                .overlay(Circle().strokeBorder(Color.white.opacity(0.14)))
                .contentShape(Circle())
        }
        .buttonStyle(.plain).disabled(model.busy && !enabled).help(label).accessibilityLabel(label).accessibilityIdentifier(id)
        .padTarget(id, scope: "main", radius: 24, activate: action)
    }

    func collection(width: CGFloat) -> some View {
        let cardWidth = min(300, max(190, (width - 124) / 5))
        let step = cardWidth + 14
        let heroID = model.selected?.id
        return VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 16) {
                Text(loc.t("library.collection")).font(.manrope(28, .bold)).tracking(-0.6)
                Spacer()
                Button { model.refresh() } label: {
                    Image(systemName: "arrow.clockwise").font(.system(size: 13, weight: .semibold))
                }
                .buttonStyle(LauncherSecondaryButtonStyle()).disabled(model.loading)
                .help(loc.t("s2arm.library.refresh")).accessibilityLabel(loc.t("s2arm.library.refresh"))
                Text(model.games.count == 1 ? loc.t("library.gameCountOne") : String(format: loc.t("library.gameCount"), model.games.count))
                    .font(.manrope(12)).foregroundStyle(.secondary)
            }
            ScrollViewReader { proxy in
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(alignment: .top, spacing: 14) {
                        ForEach(model.games) { game in
                            let _ = LauncherWork.count(.card)
                            Button { model.selectedID = game.id } label: {
                                VStack(alignment: .leading, spacing: 8) {
                                    GameArtwork(covers: covers, cover: game.cover)
                                    .frame(width: cardWidth, height: cardWidth / 1.8).clipped()
                                    .clipShape(RoundedRectangle(cornerRadius: 5))
                                    .overlay(RoundedRectangle(cornerRadius: 5).strokeBorder(
                                        game.id == heroID ? LauncherPalette.cream : .clear, lineWidth: 2))
                                    Text(game.name).font(.manrope(16, .semibold)).lineLimit(1)
                                    status(game)
                                    Capsule().fill(game.id == heroID ? LauncherPalette.cream : .clear)
                                        .frame(height: 3).padding(.top, 4)
                                }
                                .frame(width: cardWidth, alignment: .leading).contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel(game.name)
                            .accessibilityValue(statusLine(game).text)
                            .accessibilityAddTraits(game.id == heroID ? .isSelected : [])
                            .accessibilityIdentifier("card." + game.id)
                            .padTarget("card." + game.id, scope: "main", radius: 7) { model.selectedID = game.id }
                        }
                    }
                    .padding(.horizontal, Self.ringRoom).padding(.top, Self.ringRoom).padding(.bottom, 5 + Self.ringRoom)
                    .background(EnclosingScrollView(row: row))
                }
                .padding(.horizontal, -Self.ringRoom).padding(.vertical, -Self.ringRoom)
                .onChange(of: padFocus.focused) { id in
                    guard let id, id.hasPrefix("card.") else { return }
                    withAnimation(.easeInOut(duration: 0.25)) { proxy.scrollTo(id, anchor: .center) }
                }
            }
            .overlay(alignment: .topLeading) {
                if row.canBack { pager("chevron.left", height: cardWidth / 1.8) { row.page(-1, step: step) }.offset(x: -22) }
            }
            .overlay(alignment: .topTrailing) {
                if row.canForward { pager("chevron.right", height: cardWidth / 1.8) { row.page(1, step: step) }.offset(x: 22) }
            }
        }
        .padding(.horizontal, 34).padding(.top, 8).padding(.bottom, 22)
    }

    /// A round button floating over the row's edge, centred on the artwork, shown while the row scrolls that way.
    func pager(_ symbol: String, height: CGFloat, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 16, weight: .semibold)).foregroundStyle(.white)
                .frame(width: 44, height: 44)
                .background(Color.black.opacity(0.72), in: Circle())
                .overlay(Circle().strokeBorder(Color.white.opacity(0.16)))
                .shadow(color: .black.opacity(0.4), radius: 10)
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .frame(height: height)
        .accessibilityLabel(loc.t(symbol == "chevron.left" ? "library.scrollBack" : "library.scrollForward"))
        .transition(.opacity)
    }

    /// Ready (green) when Play can start the runtime; not prepared (grey) otherwise.
    private func heroCapsule(_ title: String, symbol: String, id: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 8) { Image(systemName: symbol); Text(title).lineLimit(1) }
                .font(.manrope(16, .bold)).foregroundStyle(LauncherPalette.cream)
                .padding(.horizontal, 22).frame(height: LauncherHeroAction.height)
                .background(Color.white.opacity(0.10), in: Capsule())
                .overlay(Capsule().strokeBorder(LauncherPalette.cream.opacity(0.35)))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain).disabled(!enabled).accessibilityLabel(title).accessibilityIdentifier(id)
    }

    /// 免费签名快到期 / 已到期的提醒（只提最急的一款）。
    @ViewBuilder var renewBanner: some View {
        if let game = model.games.filter({ ($0.iphoneDaysLeft() ?? 9) <= 1 }).min(by: { ($0.iphoneDaysLeft() ?? 9) < ($1.iphoneDaysLeft() ?? 9) }), let days = game.iphoneDaysLeft() {
            HStack(spacing: 12) {
                Image(systemName: "clock.badge.exclamationmark").foregroundStyle(LauncherPalette.building)
                Text((days == 0 ? txt("library.banner.expired") : txt("library.banner.renew")).replacingOccurrences(of: "{0}", with: game.name).replacingOccurrences(of: "{1}", with: "\(days)")).font(.manrope(13.5, .semibold))
                Spacer(minLength: 12)
                Button(txt("library.hero.renew")) { installFor = game.id }.buttonStyle(LauncherSecondaryButtonStyle())
            }
            .padding(.horizontal, 34).padding(.vertical, 10).background(LauncherPalette.building.opacity(0.10))
        }
    }

    private func moreMenu(_ game: GameSummary) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            moreRow(txt("game.display"), "slider.horizontal.3") { displayFor = game.id }
            moreRow(txt("game.saves"), "tray.full") { saveFor = game.id }
            moreRow(txt("game.input"), "gamecontroller") { inputFor = game.id }
            moreRow(txt("game.diag"), "doc.zipper") { exportDiagnostics() }
            Divider().opacity(0.3).padding(.vertical, 4)
            moreRow(txt("game.delete"), "trash", danger: true) { deleteFor = game }
        }
        .padding(8).frame(width: 220).font(.manrope(13.5, .semibold)).preferredColorScheme(.dark)
    }

    private func moreRow(_ title: String, _ symbol: String, danger: Bool = false, action: @escaping () -> Void) -> some View {
        Button { moreFor = nil; action() } label: {
            Label(title, systemImage: symbol).frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 10).padding(.vertical, 8).contentShape(Rectangle())
                .foregroundStyle(danger ? LauncherPalette.danger : Color.primary)
        }
        .buttonStyle(.plain)
    }

    private func exportDiagnostics() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "S2Arm-diagnostics.zip"
        if panel.runModal() == .OK, let url = panel.url { model.diagnostics(exportTo: url) }
    }

    /// 卡片和大图上的状态：两端都算（原作者只看 Mac 版）。
    private func statusLine(_ game: GameSummary) -> (text: String, color: Color) {
        if model.busy && model.session?.gameID == game.id {
            return (loc.t(model.session?.kind == .play ? "s2arm.library.running" : "s2arm.job.running"), LauncherPalette.building)
        }
        if model.exportQueue.contains(where: { $0.game.id == game.id }) { return (txt("export.status.queued"), LauncherPalette.building) }
        var parts: [String] = []
        var expired = false
        if game.iphoneInstalledAt != nil {
            if let days = game.iphoneDaysLeft() {
                expired = days == 0
                parts.append(expired ? txt("library.status.expired") : txt("library.status.installed").replacingOccurrences(of: "{0}", with: "\(days)"))
            } else { parts.append(txt("library.status.installedPaid")) }
        } else if game.iphoneReady { parts.append(txt("library.status.iphone")) }
        if game.playable { parts.append(loc.t("library.ready")) }
        if parts.isEmpty { return (loc.t("s2arm.library.notPrepared"), LauncherPalette.unbuilt) }
        return (parts.joined(separator: " · "), expired ? LauncherPalette.danger : LauncherPalette.ready)
    }

    func status(_ game: GameSummary, prominent: Bool = false) -> some View {
        let (text, color) = statusLine(game)
        return HStack(spacing: 7) {
            Circle().fill(color).frame(width: prominent ? 9 : 7, height: prominent ? 9 : 7)
            Text(prominent ? text.uppercased() : text)
                .font(prominent ? .manrope(12, .bold) : .manrope(13, .medium))
                .tracking(prominent ? 1.3 : 0)
                .foregroundStyle(color)
            if !game.display.isEmpty {
                Text("· " + game.display).font(.manrope(prominent ? 13 : 12)).foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

/// A cover filling its frame (upstream ArtworkFill without the focus point: S2Arm covers have none).
struct CoverFill: View {
    let image: NSImage
    var body: some View {
        GeometryReader { g in
            Image(nsImage: image).resizable().interpolation(.high).aspectRatio(contentMode: .fill)
                .frame(width: g.size.width, height: g.size.height).clipped()
        }
    }
}

/// Covers decoded once per path (the library redraws on every job event while a game runs).
@MainActor final class CoverCache: ObservableObject {
    private var images: [String: NSImage] = [:]
    private var missing: Set<String> = []
    func image(_ path: String?) -> NSImage? {
        guard let path else { return nil }
        if let image = images[path] { return image }
        if missing.contains(path) { return nil }
        let _ = LauncherWork.count(.artworkDecode)
        guard let image = NSImage(contentsOfFile: path) else { missing.insert(path); return nil }
        images[path] = image
        return image
    }
}
