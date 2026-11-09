// S2Arm（不是上游文件）：装到 iPhone 前的清单（installCheck 作业：iPhone 版做好了没有、签名团队、连上 / 信任 / 开发者模式），
// 每几秒重查一次（玩家在手机上点信任、开开发者模式后自动打勾），iPhone 版做好就能点「开始装」，其余项只提示（install 作业，跑在作业页、可停）。
// 卡片同 NXLauncher（CardGroup / CardRow）。
import AppKit
import SwiftUI
import NXCore
import NXModel
import NXLauncher
import S2ArmAppleAdapter

struct InstallView: View {
    @ObservedObject var model: S2ArmLauncherModel
    @EnvironmentObject var loc: Localizer
    @Environment(\.dismiss) private var dismiss
    let gameID: String
    @State private var showRemoveConfirm = false

    private var game: GameSummary? { model.games.first { $0.id == gameID } }
    private static let pollSeconds: UInt64 = 5

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(loc.t("s2arm.install.title")).font(.manrope(26, .heavy)).tracking(-0.6)
                    if let game { Text(game.name).font(.manrope(14)).foregroundStyle(.secondary) }
                }
                Spacer()
                if model.installChecking { ProgressView().controlSize(.small) }
            }
            .padding(.horizontal, 30).padding(.top, 28).padding(.bottom, 20)
            if let error = model.installError {
                Text(loc.backend(error)).font(.manrope(12)).foregroundStyle(LauncherPalette.danger)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 30).padding(.bottom, 12)
            }
            if model.installDone == gameID { doneContent } else if let job = installJob {
                VStack(alignment: .leading, spacing: 10) {
                    Text(loc.backend(job.stage.isEmpty ? job.message : job.stage)).font(.manrope(16, .bold)).lineLimit(1)
                    InlineProgress(session: job)
                    Text(text("install.keychain")).font(.manrope(13)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                .padding(.horizontal, 30).frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    if let failed = model.inlineFailure, failed.kind == .install {
                        Text(loc.backend(failed.message)).font(.manrope(13)).foregroundStyle(LauncherPalette.danger).fixedSize(horizontal: false, vertical: true)
                        CopyReportButton(model: model, gameID: gameID, operations: ["install", "installRemove"], fallback: failed.message)
                    }
                    if let r = model.installReadiness {
                        CardGroup(loc.t("s2arm.install.checklist"), footnote: footnote(r)) {
                            ForEach(r.items) { item in
                                CardRow(loc.backend(item.title), detail: loc.backend(item.detail), dot: dot(item)) {
                                    if !item.ok, item.id == "package", let game {
                                        Button(loc.t("s2arm.action.ipa")) { dismiss(); model.exportFor = game.id }
                                            .buttonStyle(LauncherSecondaryButtonStyle()).disabled(model.busy)
                                    }
                                    if !item.ok, item.id == "appleId" {
                                        Button(text("install.apple.open")) {
                                            if case .open(let url) = model.provider.environment.repairStep(for: "openXcode") { NSWorkspace.shared.open(url) }
                                        }
                                        .buttonStyle(LauncherSecondaryButtonStyle())
                                    }
                                }
                                if item.id != r.items.last?.id { CardDivider() }
                            }
                        }
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        Text(text("install.container_note")).font(.manrope(12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        Text(text("install.legacy_note")).font(.manrope(12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                    if let game {
                        Button(text("install.remove")) { showRemoveConfirm = true }
                            .buttonStyle(LauncherSecondaryButtonStyle())
                            .disabled(model.busy)
                            .confirmationDialog(text("install.remove_title"), isPresented: $showRemoveConfirm, titleVisibility: .visible) {
                                Button(text("install.remove_confirm"), role: .destructive) { model.removeFromPhone(game) }
                                Button(loc.t("launch.done"), role: .cancel) {}
                            } message: {
                                Text(text("install.remove_body"))
                            }
                    }
                }
                .padding(.horizontal, 30).padding(.bottom, 24)
            }
            }
            Divider().opacity(0.3)
            if model.installDone == gameID {
                HStack { Spacer(); Button(text("install.close")) { model.installDone = nil; dismiss() }.buttonStyle(LauncherSecondaryButtonStyle()).keyboardShortcut(.defaultAction) }
                    .padding(.horizontal, 30).padding(.vertical, 16)
            } else if let job = installJob {
                HStack { Spacer(); Button(loc.t("s2arm.job.cancel")) { model.stop() }.buttonStyle(LauncherSecondaryButtonStyle()).disabled(job.phase == .stopping) }
                    .padding(.horizontal, 30).padding(.vertical, 16)
            } else {
            HStack(spacing: 14) {
                Text(loc.t("s2arm.install.note")).font(.manrope(12)).foregroundStyle(.secondary).lineLimit(3)
                Spacer(minLength: 8)
                Button(loc.t("launch.done")) { dismiss() }.buttonStyle(LauncherSecondaryButtonStyle())
                Button {
                    if let game { model.clearInlineFailure(); model.install(game) }
                } label: {
                    Text(loc.t("s2arm.install.start")).font(.manrope(13.5, .bold)).foregroundStyle(.black)
                        .padding(.horizontal, 22).padding(.vertical, 9)
                        .background(LauncherPalette.cream, in: Capsule())
                }
                .buttonStyle(.plain).keyboardShortcut(.defaultAction)
                .disabled(!model.installAllowed(gameID) || model.busy)
                .opacity(model.installAllowed(gameID) && !model.busy ? 1 : 0.45)
            }
            .padding(.horizontal, 30).padding(.vertical, 16)
            }
        }
        .task {
            // 开着就每几秒问一次（旧启动器装机页同此）；关掉页面 Task 自动取消
            while !Task.isCancelled {
                if installJob == nil, model.installDone != gameID { model.checkInstall(gameID) }
                try? await Task.sleep(nanoseconds: Self.pollSeconds * 1_000_000_000)
            }
        }
        .frame(width: 600, height: 520)
        .background(LauncherPalette.background)
        .font(.manrope(13))
        .preferredColorScheme(.dark)
        .tint(LauncherPalette.cream)
    }

    private var installJob: JobSession? { (model.session?.kind == .install || model.session?.kind == .installRemove) ? model.session : nil }
    private func text(_ id: String) -> String { BackendText.launcherText(id, language: loc.lang == .zh ? "zh-Hans" : "en") }

    private var doneContent: some View {
        VStack(spacing: 16) {
            SuccessSeal()
            Text(text("install.done.title")).font(.manrope(30, .heavy)).tracking(-0.8)
            Text(text("install.done.trust")).font(.manrope(14)).foregroundStyle(.secondary).multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
            if game?.iphoneFree == true {
                Text(text("install.done.free")).font(.manrope(13)).foregroundStyle(.secondary).multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, 44).frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func dot(_ item: InstallItem) -> Color {
        item.ok ? LauncherPalette.ready : (item.waiting ? LauncherPalette.unbuilt : LauncherPalette.building)
    }

    private func footnote(_ r: InstallReadiness) -> String {
        var parts: [String] = []
        if !r.device.isEmpty { parts.append(String(format: loc.t("s2arm.install.device"), r.device)) }
        if !r.account.isEmpty { parts.append(String(format: loc.t("s2arm.install.account"), r.account)) }
        return parts.joined(separator: " · ")
    }
}
