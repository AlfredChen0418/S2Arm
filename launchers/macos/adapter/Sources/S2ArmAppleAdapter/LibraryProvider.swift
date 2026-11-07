import Foundation

/// 一款游戏登记的一个原件（catalog 的 packages 一项）：本体 / 更新 / DLC。
public struct GamePackage: Equatable {
    /// base / update / dlc。
    public let type: String
    public let titleId: String
    public let version: Int
    /// 版本号的显示（如 1.2.0）；没有为空。
    public let display: String
    public let size: Int64
    public let sha256: String
    /// 原件形态：空 = NSP / XCI（要密钥）；nup / dump / extract / loose 不加密，不要密钥。
    public let format: String

    public init(type: String, titleId: String = "", version: Int = 0, display: String = "", size: Int64 = 0, sha256: String = "", format: String = "") {
        self.type = type; self.titleId = titleId; self.version = version; self.display = display; self.size = size; self.sha256 = sha256; self.format = format
    }

    init?(catalog p: [String: Any]) {
        guard let type = p["type"] as? String else { return nil }
        self.init(type: type, titleId: p["titleId"] as? String ?? "", version: (p["version"] as? NSNumber)?.intValue ?? 0,
                  display: p["display"] as? String ?? "", size: (p["size"] as? NSNumber)?.int64Value ?? 0, sha256: p["sha256"] as? String ?? "",
                  format: p["format"] as? String ?? "")
    }
}

/// 游戏库里的一款（catalog 作业 result.games 的一项，只取界面要的字段）。
public struct GameSummary: Identifiable, Equatable {
    public let id: String
    public let name: String
    /// 导入版本（catalog 的 display），不宣称为当前运行版本。
    public let display: String
    /// 封面文件路径（游戏库 games/<id>/cover.jpg）；没有为 nil。
    public let cover: String?
    /// 玩家改过名字 / 封面（可以恢复默认）
    public let customName: Bool
    public let customCover: Bool
    /// 运行文件已准备好、可以启动。
    public let ready: Bool
    public let needsPreparation: Bool
    public let compatibility: String
    public let missing: [String]
    /// 登记的原件（本体 / 更新 / DLC），出包页的游戏信息用。
    public let packages: [GamePackage]
    /// iPhone 包已经出好（IPA 和资源包都在）。
    public let iphoneReady: Bool
    /// 上次装到 iPhone 的时刻（秒）和是不是免费账号签的；没装过为 nil。
    public let iphoneInstalledAt: Double?
    public let iphoneFree: Bool

    public init(id: String, name: String, display: String = "", cover: String? = nil, ready: Bool = false,
                needsPreparation: Bool = false, compatibility: String = "experimental", missing: [String] = [], packages: [GamePackage] = [],
                iphoneReady: Bool = false, iphoneInstalledAt: Double? = nil, iphoneFree: Bool = false, customName: Bool = false, customCover: Bool = false) {
        self.customName = customName; self.customCover = customCover
        self.iphoneReady = iphoneReady; self.iphoneInstalledAt = iphoneInstalledAt; self.iphoneFree = iphoneFree
        self.id = id; self.name = name; self.display = display; self.cover = cover; self.ready = ready
        self.needsPreparation = needsPreparation; self.compatibility = compatibility; self.missing = missing; self.packages = packages
    }

    /// 从 catalog 的一项读；没有 id / 名字的、标了 hidden 的不要。
    public init?(catalog g: [String: Any]) {
        guard let id = g["id"] as? String, !id.isEmpty, let name = g["name"] as? String, g["hidden"] as? Bool != true else { return nil }
        let cover = (g["cover"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        self.init(id: id, name: name, display: g["display"] as? String ?? "", cover: cover, ready: g["ready"] as? Bool ?? false,
                  needsPreparation: g["needsPreparation"] as? Bool ?? false, compatibility: g["compatibility"] as? String ?? "experimental",
                  missing: g["missing"] as? [String] ?? [],
                  packages: (g["packages"] as? [[String: Any]] ?? []).compactMap(GamePackage.init(catalog:)),
                  iphoneReady: g["iphoneReady"] as? Bool ?? false,
                  iphoneInstalledAt: ((g["iphoneInstall"] as? [String: Any])?["at"] as? NSNumber)?.doubleValue,
                  iphoneFree: (g["iphoneInstall"] as? [String: Any])?["free"] as? Bool ?? false,
                  customName: g["customName"] as? Bool ?? false, customCover: g["customCover"] as? Bool ?? false)
    }

    /// 某类原件里版本最新的那个的显示版本（base / update）；没有这类为 nil。
    public func version(_ type: String) -> String? {
        packages.filter { $0.type == type }.max { $0.version < $1.version }.map { $0.display.isEmpty ? "v\($0.version)" : $0.display }
    }

    /// DLC 个数（按程序号去重）。
    public var dlcCount: Int { Set(packages.filter { $0.type == "dlc" }.map(\.titleId)).count }

    /// 原件总大小（同一个文件里的多项只算一次，同旧启动器 ExportGameCard）。
    public var totalBytes: Int64 {
        Dictionary(grouping: packages, by: \.sha256).values.compactMap(\.first).reduce(0) { $0 + $1.size }
    }

    /// 免费签名还剩几天（到期 = 0）；付费账号或没装过为 nil。
    public func iphoneDaysLeft(now: Date = Date()) -> Int? {
        guard iphoneFree, let at = iphoneInstalledAt else { return nil }
        return max(0, Int(((at + 7 * 86400 - now.timeIntervalSince1970) / 86400).rounded(.up)))
    }

    /// 出包要不要 prod.keys：只有加密原件（NSP / XCI）要，解包好的游戏和 NUP 不要。
    public var needsKeys: Bool { packages.contains { !["nup", "dump", "extract", "loose"].contains($0.format) } }

    /// 能不能直接 Play：运行文件在、且不需要重新准备（同旧启动器起局前提；不能时先 build 的 prepare）。
    public var playable: Bool { ready && !needsPreparation }

    /// 能不能准备 / 出包：不是不支持的款、文件齐（同旧启动器 Game.buildable）。
    public var buildable: Bool { compatibility != "unsupported" && missing.isEmpty }
}

/// 游戏库：交 catalog 作业，读结果。
public final class LibraryProvider {
    private let client: JobClient
    private let lock = NSLock()
    private var keys: Bool?
    /// 上次读游戏库时 prod.keys 齐没齐（catalog 的 keys.ready）；还没读过为 nil。
    public var keysReady: Bool? { lock.withLock { keys } }

    public init(client: JobClient) { self.client = client }

    private func remember(_ result: [String: Any]) {
        let ready = (result["keys"] as? [String: Any])?["ready"] as? Bool
        lock.withLock { keys = ready }
    }

    private var snapshotFile: URL { client.environment.privateRoot.appendingPathComponent("library-snapshot.json") }

    /// 上一次读到的游戏列表：启动时先显示它，catalog 作业在后台跑完再替换。换了游戏库（libraryId 不符）或没读过时为 nil。
    public func lastKnownGames() -> [GameSummary]? {
        guard !client.environment.libraryId.isEmpty, let data = try? Data(contentsOf: snapshotFile),
              let saved = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              saved["libraryId"] as? String == client.environment.libraryId,
              let result = saved["result"] as? [String: Any] else { return nil }
        remember(result)
        return Self.games(in: result)
    }

    private func saveSnapshot(_ result: [String: Any]) {
        let saved: [String: Any] = ["libraryId": client.environment.libraryId,
                                    "result": ["games": result["games"] ?? [], "keys": result["keys"] ?? [:]]]
        guard JSONSerialization.isValidJSONObject(saved), let data = try? JSONSerialization.data(withJSONObject: saved) else { return }
        try? data.write(to: snapshotFile, options: .atomic)
    }

    public static let noLibraryMessage = "还没有游戏库：请先在 S2Arm 启动器里选择游戏库并导入游戏。"

    /// 读一遍游戏库。没配游戏库时不起作业，直接回失败。
    @discardableResult
    public func load(_ completion: @escaping (Result<[GameSummary], JobClientError>) -> Void) -> JobHandle? {
        guard !client.environment.library.isEmpty else {
            completion(.failure(JobClientError(Self.noLibraryMessage)))
            return nil
        }
        do {
            return try client.start(operation: "catalog", onFinish: { outcome in
                guard outcome.state == .completed else {
                    completion(.failure(JobClientError(outcome.message.isEmpty ? JobOutcome.interruptedMessage : outcome.message)))
                    return
                }
                self.remember(outcome.result)
                self.saveSnapshot(outcome.result)
                completion(.success(Self.games(in: outcome.result)))
            })
        } catch let error as JobClientError {
            completion(.failure(error))
        } catch {
            completion(.failure(JobClientError(error.localizedDescription)))
        }
        return nil
    }

    /// 同 load，async（EngineProvider 用）：没配游戏库直接抛错，不起作业。
    public func games() async throws -> [GameSummary] {
        guard !client.environment.library.isEmpty else { throw JobClientError(Self.noLibraryMessage) }
        let result = try await client.perform("catalog").result
        remember(result)
        return Self.games(in: result)
    }

    /// 新玩家还没有游戏库：建在私有目录下的「游戏库」（同旧启动器 defaultLibraryURL），玩家不用选；放密钥、添加游戏前调用。已有游戏库不动。
    public func ensureLibrary(events: EventSink = .discard) async throws {
        guard client.environment.library.isEmpty else { return }
        let folder = client.environment.privateRoot.appendingPathComponent("游戏库", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        try await change(to: folder, events: events)
    }

    /// 换游戏库（同旧启动器 chooseLibrary → setupLibrary）：setup 作业建好 / 认出这个文件夹（作业层拦非 APFS、云盘同步目录、非空的生文件夹），
    /// 成了把 library / libraryId 写回 preferences.json，本进程之后的作业都用新库。没成抛错、什么都不改。
    @discardableResult
    public func change(to folder: URL, moveFrom: String? = nil, events: EventSink = .discard) async throws -> String {
        var extra: [String: Any] = ["library": folder.path, "libraryId": ""]
        if let moveFrom { extra["moveFrom"] = moveFrom }
        let r = try await client.perform("setup", extra: extra, events: events).result
        let library = r["library"] as? String ?? folder.path
        let libraryId = r["libraryId"] as? String ?? ""
        try JobEnvironment.saveLibrary(privateRoot: client.environment.privateRoot, library: library, libraryId: libraryId)
        client.environment.library = library
        client.environment.libraryId = libraryId
        return library
    }

    /// catalog 结果里的游戏（没 id / 名字、标了 hidden 的不要）。
    static func games(in result: [String: Any]) -> [GameSummary] {
        (result["games"] as? [[String: Any]] ?? []).compactMap(GameSummary.init(catalog:))
    }
}
