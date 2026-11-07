// S2Arm（不是上游文件）：一款游戏的画面设置（60 帧、宽屏强制启用、MetalFX 放大）。作业层 community / communitySave；
//   开关只改下次启动的宿主设置，不用重新准备。可用与否按这款的能力清单，不可用时显示原因。
import SwiftUI
import NXCore
import NXModel
import NXLauncher
import S2ArmAppleAdapter

/// 一个开关的状态（作业层 community 结果的 mac 字段）。
struct DisplayOption: Equatable {
    let available: Bool
    let enabled: Bool
    let reason: String

    init(available: Bool, enabled: Bool, reason: String = "") { self.available = available; self.enabled = enabled; self.reason = reason }

    init(_ value: Any?) {
        let row = value as? [String: Any] ?? [:]
        available = row["available"] as? Bool ?? false
        enabled = row["enabled"] as? Bool ?? false
        reason = row["reason"] as? String ?? ""
    }
}

extension S2ArmLauncherModel {
    func displaySettings(_ gameID: String, change: [String: Any]? = nil) async throws -> [String: DisplayOption] {
        let result = try await saveJob(change == nil ? "community" : "communitySave", gameID: gameID, extra: change ?? [:])
        let mac = result["mac"] as? [String: Any] ?? [:]
        return ["fps60": DisplayOption(mac["fps60"]), "widenForce": DisplayOption(mac["widenForce"]), "metalfx": DisplayOption(mac["metalfx"])]
    }
}

struct GameDisplaySheet: View {
    @EnvironmentObject private var loc: Localizer
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var model: S2ArmLauncherModel
    let gameID: String
    @State private var options: [String: DisplayOption] = [:]
    @State private var failure: String?
    @State private var saving = false

    init(model: S2ArmLauncherModel, gameID: String, options: [String: DisplayOption] = [:]) {
        self.model = model; self.gameID = gameID; _options = State(initialValue: options)
    }

    private func t(_ id: String) -> String { BackendText.launcherText(id, language: loc.lang == .zh ? "zh-Hans" : "en") }
    private var gameName: String { model.games.first { $0.id == gameID }?.name ?? gameID }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 4) {
                Text(t("display.title")).font(.manrope(26, .heavy)).tracking(-0.6)
                Text(gameName).font(.manrope(14)).foregroundStyle(.secondary).lineLimit(1)
                Text(t("display.hint")).font(.manrope(12.5)).foregroundStyle(.secondary).padding(.top, 6)
            }
            if let failure {
                Label { Text(loc.backend(failure)).fixedSize(horizontal: false, vertical: true) } icon: { Image(systemName: "exclamationmark.circle.fill") }
                    .font(.manrope(12.5)).foregroundStyle(LauncherPalette.danger)
            }
            if options.isEmpty && failure == nil {
                ProgressView().controlSize(.small)
            } else if !options.isEmpty {
                CardGroup("") {
                    row("fps60", t("display.fps60"), t("display.fps60Note"))
                    CardDivider()
                    row("widenForce", t("display.widen"), t("display.widenNote"))
                    CardDivider()
                    row("metalfx", t("display.metalfx"), t("display.metalfxNote"))
                }
            }
            Spacer(minLength: 0)
            HStack {
                Spacer()
                Button(t("display.done")) { dismiss() }.buttonStyle(LauncherSecondaryButtonStyle()).keyboardShortcut(.cancelAction)
            }
        }
        .padding(30)
        .frame(width: 560, height: 470)
        .background(LauncherPalette.background)
        .font(.manrope(13))
        .preferredColorScheme(.dark)
        .tint(LauncherPalette.cream)
        .task { if options.isEmpty { await load(nil) } }
    }

    private func row(_ key: String, _ title: String, _ note: String) -> some View {
        let option = options[key]
        return CardRow(title, detail: option?.available == false ? loc.backend(option?.reason ?? "") : note) {
            Toggle("", isOn: Binding(get: { option?.enabled ?? false }, set: { on in Task { await load([key: on]) } }))
                .toggleStyle(LauncherSwitchStyle()).labelsHidden().accessibilityLabel(title)
                .disabled(option?.available != true || saving || model.busy)
        }
    }

    private func load(_ change: [String: Any]?) async {
        saving = true; defer { saving = false }
        do {
            options = try await model.displaySettings(gameID, change: change)
            failure = nil
        } catch {
            failure = (error as? JobClientError)?.message ?? error.localizedDescription
        }
    }
}
