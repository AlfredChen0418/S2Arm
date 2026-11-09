import Foundation
import ShellUI
#if canImport(UIKit)
import PhotosUI
import SwiftUI
import UIKit
#endif

/// 这台 iPhone 上玩家给某款游戏改的显示名和封面（Documents/games/<游戏号>/identity.json + identity-cover.jpg）。
/// 只改显示；优先于电脑端写进包里的名字和封面，重装 App 后还在。
public struct GameIdentity: Equatable {
    public var name: String
    public var cover: URL?
}

public struct GameIdentityStore {
    public var root: URL
    public init(root: URL) { self.root = root }
    public static var live: GameIdentityStore {
        let env = ProcessInfo.processInfo.environment["S2A_SHELL_DOCS"]   // 只给单测
        let docs = env.map { URL(fileURLWithPath: $0, isDirectory: true) } ?? FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        return GameIdentityStore(root: docs)
    }
    private func folder(_ id: String) -> URL { root.appendingPathComponent("games", isDirectory: true).appendingPathComponent(id, isDirectory: true) }
    private func record(_ id: String) -> URL { folder(id).appendingPathComponent("identity.json") }
    private func coverFile(_ id: String) -> URL { folder(id).appendingPathComponent("identity-cover.jpg") }

    public func load(_ id: String) -> GameIdentity? {
        let saved = (try? Data(contentsOf: record(id))).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        let name = (saved?["name"] as? String) ?? ""
        let cover = FileManager.default.fileExists(atPath: coverFile(id).path) ? coverFile(id) : nil
        return name.isEmpty && cover == nil ? nil : GameIdentity(name: name, cover: cover)
    }

    /// name 去掉首尾空白后为空 = 用默认名；cover 为 nil = 不动封面。
    public func save(_ id: String, name: String, cover: Data?) throws {
        try FileManager.default.createDirectory(at: folder(id), withIntermediateDirectories: true)
        let trimmed = String(name.trimmingCharacters(in: .whitespacesAndNewlines).prefix(64))
        try JSONSerialization.data(withJSONObject: ["name": trimmed]).write(to: record(id), options: .atomic)
        if let cover { try cover.write(to: coverFile(id), options: .atomic) }
    }

    public func clear(_ id: String) {
        try? FileManager.default.removeItem(at: record(id))
        try? FileManager.default.removeItem(at: coverFile(id))
    }
}

#if canImport(UIKit)
/// 改名字和封面的编辑页：盖在库界面上，只用触屏。
struct S2ArmGameIdentityView: View {
    let title: String
    let initialName: String
    let hasCustom: Bool
    let save: (String, Data?) -> Void
    let reset: () -> Void
    let close: () -> Void
    @State private var name = ""
    @State private var item: PhotosPickerItem?
    @State private var cover: Data?
    @State private var preview: UIImage?

    var body: some View {
        ZStack {
            Color.black.opacity(0.82).ignoresSafeArea().accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 18) {
                Text("名字和封面").font(.title2.weight(.heavy)).foregroundStyle(.white).accessibilityIdentifier("identity.title")
                TextField(title, text: $name).textFieldStyle(.roundedBorder).accessibilityIdentifier("identity.name")
                HStack(spacing: 14) {
                    if let preview {
                        Image(uiImage: preview).resizable().scaledToFill().frame(width: 128, height: 72).clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    PhotosPicker(selection: $item, matching: .images) { Text("从相册选封面") }
                        .buttonStyle(.bordered).accessibilityIdentifier("identity.cover")
                }
                HStack {
                    if hasCustom { Button("恢复默认", role: .destructive) { reset() }.accessibilityIdentifier("identity.reset") }
                    Spacer()
                    Button("取消") { close() }.accessibilityIdentifier("identity.cancel")
                    Button("保存") { save(name, cover) }.buttonStyle(.borderedProminent).accessibilityIdentifier("identity.save")
                }
            }
            .padding(24).frame(maxWidth: 480)
            .background(Color(white: 0.12), in: RoundedRectangle(cornerRadius: 16))
            .padding(24)
        }
        .onAppear { name = initialName }
        .onChange(of: item) { _, picked in
            guard let picked else { return }
            Task {
                guard let data = try? await picked.loadTransferable(type: Data.self), let image = UIImage(data: data),
                      let jpeg = Self.coverJPEG(image) else { return }
                await MainActor.run { cover = jpeg; preview = UIImage(data: jpeg) }
            }
        }
    }

    /// 长边压到 1280，封面不需要原图那么大。
    private static func coverJPEG(_ image: UIImage) -> Data? {
        let scale = min(1, 1280 / max(image.size.width, image.size.height, 1))
        let size = CGSize(width: image.size.width * scale, height: image.size.height * scale)
        let format = UIGraphicsImageRendererFormat.default(); format.scale = 1
        return UIGraphicsImageRenderer(size: size, format: format).image { _ in image.draw(in: CGRect(origin: .zero, size: size)) }.jpegData(compressionQuality: 0.85)
    }
}
#endif
