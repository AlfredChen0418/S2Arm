import Foundation

/// 装机前清单的一项（installCheck 作业，pack/jobs/install.py readiness）。
public struct InstallItem: Identifiable, Equatable {
    public let id: String
    public let ok: Bool
    /// 前一项没好，这一项先不判（不给说明）。
    public let waiting: Bool
    public let title: String
    public let detail: String
    /// human = 只能玩家本人做；app = 启动器自己能做（如先做 iPhone 版）。
    public let who: String

    public init(id: String, ok: Bool, waiting: Bool = false, title: String, detail: String = "", who: String = "human") {
        self.id = id; self.ok = ok; self.waiting = waiting; self.title = title; self.detail = detail; self.who = who
    }

    public init?(json i: [String: Any]) {
        guard let id = i["id"] as? String, let title = i["title"] as? String else { return nil }
        self.init(id: id, ok: i["ok"] as? Bool ?? false, waiting: i["waiting"] as? Bool ?? false, title: title,
                  detail: i["detail"] as? String ?? "", who: i["who"] as? String ?? "human")
    }
}

/// 装机前清单：ready 由作业层判定（只有 iPhone 版没做好才挡），其余项只提示。
public struct InstallReadiness: Equatable {
    public let items: [InstallItem]
    public let ready: Bool
    /// 连着的设备名（只连一台时）。
    public let device: String
    /// 本机签名团队名。
    public let account: String
    /// 这款最新的 .ipa；还没做好 iPhone 版时为空。
    public let ipaPath: String
    /// 诊断用：Xcode 版本和账号、团队、证书的数量，不含账号和团队名。
    public let signing: String

    public init(items: [InstallItem], ready: Bool, device: String = "", account: String = "", ipaPath: String = "", signing: String = "") {
        self.items = items; self.ready = ready; self.device = device; self.account = account; self.ipaPath = ipaPath; self.signing = signing
    }

    public init(result r: [String: Any]) {
        let items = (r["items"] as? [[String: Any]] ?? []).compactMap(InstallItem.init(json:))
        self.init(items: items, ready: r["ready"] as? Bool ?? false,
                  device: r["device"] as? String ?? "", account: r["account"] as? String ?? "", ipaPath: r["ipaPath"] as? String ?? "", signing: r["signing"] as? String ?? "")
    }
}

/// 装好的结果。
public struct InstallResult: Equatable {
    public let device: String
    /// 推到手机上的资源包文件数（没推为 nil）。
    public let packFiles: Int?
    public let outputPath: String?
}

/// 从手机删一款的结果（installRemove 作业，pack/jobs/install.py remove_from_phone）。
public struct RemoveResult: Equatable {
    /// 删除的游戏号。
    public let removed: String
    /// 手机上 S2Arm 里还剩的游戏号列表。
    public let games: [String]
    /// 本机备份的存档路径（为空表示没有存档或备份失败）。
    public let saveBackup: String
    /// 是否成功删除了手机上这款的文档数据。
    public let dataRemoved: Bool

    public init(removed: String, games: [String], saveBackup: String, dataRemoved: Bool) {
        self.removed = removed; self.games = games; self.saveBackup = saveBackup; self.dataRemoved = dataRemoved
    }

    public init(result r: [String: Any]) {
        self.init(removed: r["removed"] as? String ?? "",
                  games: r["games"] as? [String] ?? [],
                  saveBackup: r["saveBackup"] as? String ?? "",
                  dataRemoved: r["dataRemoved"] as? Bool ?? false)
    }
}

/// 装到 iPhone（10-26 维护者：Xcode 做主路）：用玩家在 Xcode 登录的 Apple ID 重签自签 IPA、数据线装上、拷资源包并逐文件核对（pack/jobs/install.py）。
public final class InstallService {
    private let client: JobClient
    public init(client: JobClient) { self.client = client }

    /// 装机前清单。只读：不申请描述文件、不签名、不碰设备上的 App。
    public func readiness(gameId: String) async throws -> InstallReadiness {
        InstallReadiness(result: try await client.perform("installCheck", extra: ["gameId": gameId]).result)
    }

    /// 装。
    public func install(gameId: String, teamId: String? = nil, events: EventSink = .discard) async throws -> InstallResult {
        var extra: [String: Any] = ["gameId": gameId]
        if let teamId, !teamId.isEmpty { extra["teamId"] = teamId }
        let r = try await client.perform("install", extra: extra, events: events).result
        return InstallResult(device: r["device"] as? String ?? "", packFiles: (r["packFiles"] as? NSNumber)?.intValue,
                             outputPath: r["outputPath"] as? String)
    }

    /// 生成给「没有签名团队」玩家用的 Xcode 引导项目，返回项目路径。
    public func signingProject() async throws -> String {
        try await client.perform("signingProject", extra: [:], events: .discard).result["path"] as? String ?? ""
    }

    /// 从手机容器删一款（installRemove 作业）。
    public func remove(gameId: String, events: EventSink = .discard) async throws -> RemoveResult {
        let r = try await client.perform("installRemove", extra: ["gameId": gameId], events: events).result
        return RemoveResult(result: r)
    }
}
