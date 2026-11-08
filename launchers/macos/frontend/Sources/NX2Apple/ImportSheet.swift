// S2Arm（不是上游文件）：添加游戏 / 放密钥弹窗（11-08 维护者）。拖进来或点按钮选；作业的进度和失败原因都留在弹窗里，成了自动关。
// 游戏能不能不要密钥由作业层判（解包好的游戏不要，NSP / XCI 要，keys_missing），这里不判格式。
// InlineProgress 是欢迎页下载行和这里共用的进度条。
import AppKit
import SwiftUI
import UniformTypeIdentifiers
import NXCore
import NXModel
import NXLauncher
import S2ArmAppleAdapter

struct ImportSheet: View {
    enum Kind { case games, keys }

    @ObservedObject var model: S2ArmLauncherModel
    @EnvironmentObject var loc: Localizer
    let kind: Kind
    @State private var targeted = false
    @State private var dropHint: String?

    private func w(_ id: String) -> String {
        BackendText.launcherText("welcome.sheet." + (kind == .games ? "games." : "keys.") + id, language: loc.lang == .zh ? "zh-Hans" : "en")
    }
    private func common(_ id: String) -> String { BackendText.launcherText("welcome.sheet." + id, language: loc.lang == .zh ? "zh-Hans" : "en") }

    private var job: JobSession? {
        guard let s = model.session, [.keys, .importGames].contains(s.kind) else { return nil }
        return s
    }
    private var failure: String? {
        guard let f = model.inlineFailure, [.keys, .importGames].contains(f.kind) else { return nil }
        return f.message
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(w("title")).font(.manrope(26, .heavy)).tracking(-0.6)
            Text(w("what")).font(.manrope(14)).foregroundStyle(.secondary).lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true).padding(.top, 6)
            Group {
                if let job {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(job.name).font(.manrope(16, .bold)).lineLimit(1)
                        InlineProgress(session: job)
                        if !job.message.isEmpty, job.message != job.stage, job.message != "操作完成" {
                            Text(loc.backend(job.message)).font(.manrope(12.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(24)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
                } else {
                    dropZone
                }
            }
            .frame(height: 220)
            .padding(.top, 20)
            .animation(.easeOut(duration: 0.2), value: job == nil)
            if kind == .keys, model.keysRejected, job == nil {
                Text(BackendText.launcherText("welcome.keys.unusable", language: loc.lang == .zh ? "zh-Hans" : "en")).font(.manrope(13)).foregroundStyle(LauncherPalette.danger)
                    .fixedSize(horizontal: false, vertical: true).padding(.top, 12)
            }
            if let dropHint, job == nil {
                Text(dropHint).font(.manrope(13)).foregroundStyle(LauncherPalette.danger)
                    .fixedSize(horizontal: false, vertical: true).padding(.top, 12)
            }
            if let failure {
                Text(loc.backend(failure)).font(.manrope(13)).foregroundStyle(LauncherPalette.danger)
                    .fixedSize(horizontal: false, vertical: true).padding(.top, 12)
                CopyReportButton(model: model, gameID: nil, operations: ["import", "keys", "setup"], fallback: failure).padding(.top, 8)
            }
            Spacer(minLength: 16)
            HStack {
                Spacer()
                Button(common("cancel")) { cancel() }
                    .buttonStyle(LauncherSecondaryButtonStyle()).keyboardShortcut(.cancelAction)
                    .disabled(job?.phase == .stopping)
            }
        }
        .padding(30)
        .frame(width: 560, height: 500)
        .background(LauncherPalette.background)
        .font(.manrope(13))
        .preferredColorScheme(.dark)
        .tint(LauncherPalette.cream)
    }

    private var dropZone: some View {
        VStack(spacing: 12) {
            Image(systemName: kind == .games ? "square.and.arrow.down" : "key").font(.system(size: 34, weight: .light))
                .foregroundStyle(LauncherPalette.cream)
            Text(w("drop")).font(.manrope(18, .bold))
            Text(common("or")).font(.manrope(12.5)).foregroundStyle(.secondary)
            Button { browse() } label: { Text(w("browse")).font(.manrope(14, .bold)).foregroundStyle(.black).padding(.horizontal, 20).padding(.vertical, 9).background(LauncherPalette.cream, in: Capsule()).contentShape(Capsule()) }
                .buttonStyle(.plain).disabled(model.busy)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(targeted ? LauncherPalette.cream.opacity(0.07) : LauncherPalette.surface.opacity(0.45), in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(targeted ? LauncherPalette.cream : Color.white.opacity(0.18), style: StrokeStyle(lineWidth: targeted ? 1.5 : 1, dash: [7, 5])))
        .animation(.easeOut(duration: 0.15), value: targeted)
        .onDrop(of: [UTType.fileURL], isTargeted: $targeted) { providers in
            LauncherView.fileURLs(providers) { accept($0) }
            return true
        }
    }

    private func accept(_ urls: [URL]) {
        dropHint = urls.isEmpty ? common("dropUnreadable") : model.busy ? common("dropBusy") : nil
        guard dropHint == nil else { return }
        if kind == .games { model.importGames(urls) } else { model.importKeys(urls[0]) }
    }

    private func browse() {
        let panel = NSOpenPanel()
        panel.title = w("browse")
        panel.canChooseFiles = true
        panel.canChooseDirectories = kind == .games
        panel.allowsMultipleSelection = kind == .games
        if panel.runModal() == .OK { accept(panel.urls) }
    }

    private func cancel() {
        if job != nil { model.stop(); return }
        dropHint = nil
        model.clearInlineFailure()
        if kind == .games { model.showAddGames = false } else { model.showKeys = false }
    }
}

/// 进度条 + 「百分比 · 已下 / 总量 · 速度 · 已用 · 还剩」，每秒走一次倒计时。没有读数时显示作业当前阶段。
struct InlineProgress: View {
    let session: JobSession
    @EnvironmentObject var loc: Localizer

    var body: some View {
        let p = session.progress
        VStack(alignment: .leading, spacing: 6) {
            if let fraction = p.fraction {
                ProgressView(value: fraction).progressViewStyle(.linear).tint(LauncherPalette.cream)
            } else {
                ProgressView().progressViewStyle(.linear).tint(LauncherPalette.cream)
            }
            TimelineView(.periodic(from: .now, by: 1)) { timeline in
                let parts = [p.fraction.map { "\(Int($0 * 100))%" }, p.unit == "%" ? "" : p.countText, p.unit == "B" ? p.rateText : "",
                             p.fraction == nil ? loc.backend(session.stage) : p.clock(at: timeline.date, left: loc.t("prog.eta"))]
                Text(parts.compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.manrope(12.5, .medium)).monospacedDigit().foregroundStyle(.secondary).lineLimit(1)
            }
        }
    }
}

/// 「复制错误摘要」：正文由作业层生成（Mac / Windows 同一份）。operations 为 nil：这种失败没有作业记录（环境检查），直接用 fallback。
struct CopyReportButton: View {
    @ObservedObject var model: S2ArmLauncherModel
    @EnvironmentObject var loc: Localizer
    let gameID: String?
    let operations: [String]?
    let fallback: String
    @State private var copied = false

    var body: some View {
        Button(copied ? loc.t("s2arm.export.copied") : loc.t("s2arm.export.copyError")) {
            Task {
                await model.copyErrorReport(platform: nil, gameID: gameID, operations: operations, fallback: fallback)
                copied = true
                try? await Task.sleep(for: .seconds(2))
                copied = false
            }
        }
        .buttonStyle(LauncherSecondaryButtonStyle())
    }
}
