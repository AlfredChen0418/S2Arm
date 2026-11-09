// S2Arm（不是上游文件）：出包页共用的游戏信息（11-08 维护者）：图标、名称、版本、本体 / 更新 / DLC、原包大小、程序号。
// 所有项都来自游戏库 catalog；没有封面（比如解包好的文件夹）用 S2Arm 自己的图标。
import AppKit
import SwiftUI
import NXCore
import NXModel
import NXLauncher
import S2ArmAppleAdapter

/// 游戏封面；没有封面放 S2Arm 图标。
/// 没封面时的透明底白字字标（只放深色底，启动器固定深色）。
enum BrandArt {
    static let wordmark: NSImage = Bundle.main.url(forResource: "wordmark-transparent", withExtension: "png").flatMap(NSImage.init(contentsOf:)) ?? NSImage()
}

struct GameArtwork: View {
    @ObservedObject var covers: CoverCache
    let cover: String?
    var body: some View {
        ZStack {
            if let image = covers.image(cover) {
                CoverFill(image: image)
            } else {
                LauncherPalette.surface
                GeometryReader { g in
                    Image(nsImage: BrandArt.wordmark).resizable().interpolation(.high).scaledToFit()
                        .frame(width: g.size.width * 0.68, height: g.size.height * 0.68)
                        .frame(width: g.size.width, height: g.size.height)
                }
            }
        }
        .accessibilityHidden(true)
    }
}

struct GameInfoPanel: View {
    @ObservedObject var covers: CoverCache
    @EnvironmentObject var loc: Localizer
    let game: GameSummary

    private func t(_ id: String) -> String { BackendText.launcherText("export.info." + id, language: loc.lang == .zh ? "zh-Hans" : "en") }

    private var dlc: String {
        guard game.dlcCount > 0 else { return t("none") }
        // 版本 0 又没显示版本 = DLC 没带版本信息（常见于卡带），只写个数，不写「v0」
        guard let latest = game.packages.filter({ $0.type == "dlc" }).max(by: { $0.version < $1.version }), latest.version > 0 || !latest.display.isEmpty,
              let version = game.version("dlc") else { return t("dlcCount").replacingOccurrences(of: "{0}", with: "\(game.dlcCount)") }
        return t("dlcValue").replacingOccurrences(of: "{0}", with: "\(game.dlcCount)").replacingOccurrences(of: "{1}", with: version)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            GameArtwork(covers: covers, cover: game.cover)
                .aspectRatio(1, contentMode: .fit)
                .clipShape(RoundedRectangle(cornerRadius: 14))
                .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.white.opacity(0.08)))
                .shadow(color: .black.opacity(0.4), radius: 18, y: 8)
            Text(game.name).font(.manrope(22, .heavy)).tracking(-0.5).lineLimit(3).fixedSize(horizontal: false, vertical: true)
            VStack(spacing: 0) {
                row(t("version"), game.display.isEmpty ? (game.version("base") ?? "-") : game.display)
                row(t("base"), game.version("base") ?? "-")
                row(t("update"), game.version("update") ?? t("none"))
                row(t("dlc"), dlc)
                row(t("size"), game.totalBytes > 0 ? ByteCountFormatter.string(fromByteCount: game.totalBytes, countStyle: .file) : "-")
                row(t("program"), game.id, mono: true)
            }
        }
    }

    private func row(_ label: String, _ value: String, mono: Bool = false) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(label).font(.manrope(12.5)).foregroundStyle(.secondary)
            Spacer(minLength: 8)
            Text(value).font(mono ? .mono(11) : .manrope(13, .semibold)).monospacedDigit().lineLimit(1).truncationMode(.middle)
        }
        .padding(.vertical, 7)
        .overlay(alignment: .bottom) { Rectangle().fill(Color.white.opacity(0.06)).frame(height: 1) }
    }
}

/// 进行中的那段进度条上一道缓慢掠过的光：只在作业真的在跑时出现。
struct ProgressSweep: View {
    @State private var moving = false
    var body: some View {
        GeometryReader { g in
            LinearGradient(colors: [.clear, Color.white.opacity(0.55), .clear], startPoint: .leading, endPoint: .trailing)
                .frame(width: max(40, g.size.width * 0.35))
                .offset(x: moving ? g.size.width : -max(40, g.size.width * 0.35))
        }
        .clipped()
        .allowsHitTesting(false)
        .onAppear { withAnimation(.linear(duration: 1.7).repeatForever(autoreverses: false)) { moving = true } }
    }
}

/// 当前步骤前面呼吸的小圆点。
struct PulseDot: View {
    @State private var on = false
    var body: some View {
        ZStack {
            Circle().fill(LauncherPalette.cream.opacity(on ? 0 : 0.35)).frame(width: on ? 26 : 12, height: on ? 26 : 12)
            Circle().fill(LauncherPalette.cream).frame(width: 10, height: 10)
        }
        .frame(width: 26, height: 26)
        .onAppear { withAnimation(.easeOut(duration: 1.4).repeatForever(autoreverses: false)) { on = true } }
    }
}

/// 出包成功的封章：绿底对勾弹出，外圈一道光环扩散一次。
struct SuccessSeal: View {
    @State private var shown = false
    var body: some View {
        ZStack {
            Circle().fill(LauncherPalette.ready.opacity(shown ? 0.35 : 0)).frame(width: 84, height: 84).blur(radius: 18)   // 光只在对勾周围，不铺到别的区域
            Circle().strokeBorder(LauncherPalette.ready.opacity(shown ? 0 : 0.7), lineWidth: 2).frame(width: 84, height: 84).scaleEffect(shown ? 2.1 : 1)
            Circle().fill(LauncherPalette.ready).frame(width: 84, height: 84).scaleEffect(shown ? 1 : 0.4)
            Image(systemName: "checkmark").font(.system(size: 38, weight: .bold)).foregroundStyle(.black).scaleEffect(shown ? 1 : 0.2).opacity(shown ? 1 : 0)
        }
        .frame(width: 84, height: 84)
        .accessibilityHidden(true)
        .onAppear { withAnimation(.spring(response: 0.55, dampingFraction: 0.58)) { shown = true } }
    }
}
