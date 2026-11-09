import Foundation

/// S2Arm 作为 NX2Apple 启动器里的一个引擎（设计稿 2.13 EngineProvider）。每个方法都是交 S2Arm 作业层（pack/jobs/main.py）的作业：
///
/// | 设计稿 | S2Arm 作业 |
/// |---|---|
/// | inspect | import（读原件、登记进游戏库）+ catalog（读回这几款） |
/// | build | Mac：build（作业层的 prepare，出运行文件）；iPhone：ipa（缺 .NET SDK 先 installDotnet） |
/// | settingsSchema | 空表：没有每款的游戏选项（11-07 维护者：语言跟系统、Mac 主机模式、60 帧 / 满屏缺省开 + 自动降级，作业层定） |
/// | run | launch（作业层校验运行文件、恢复存档、起 s2arm-host 并等它退出） |
/// | control | stop = 请 launch 作业停（SIGTERM） |
///
/// 现有服务（LibraryProvider / SettingsProvider / PlayService / EnvironmentService / InstallService / SpaceService）是它的内部实现，也照旧对外可用：
/// 设计稿的协议没有的事（环境检查、装到 iPhone、释放空间、换游戏库）走这些服务或本类型的扩展方法。
public final class S2ArmEngineProvider: EngineProvider {
    public let id = EngineID("s2arm")
    /// 两个目标都出；Switch 2 款同一条出包 / 起局路径（NVN2，11-06 金刚 Switch 2 纯 Windows 出包进局）；宽屏 / 60 帧是宿主开关（按款有表才可用）；
    /// Mac 局首遇的着色器现编（runtime shaders）。没有的：120 帧、渲染倍率、金手指、云存档（作业层没有对应作业）。
    public let capabilities = EngineCapabilities(targets: [.mac, .iPhone], switch2: true, runtimeShaders: true, widescreen: true, fps60: true)

    public let client: JobClient
    public let library: LibraryProvider
    public let settings: SettingsProvider
    public let play: PlayService
    public let environment: EnvironmentService
    public let installer: InstallService
    public let space: SpaceService

    public init(client: JobClient, xcodeLocator: @escaping () -> String? = XcodeLocator.detect) {
        self.client = client
        library = LibraryProvider(client: client)
        settings = SettingsProvider(client: client)
        play = PlayService(client: client)
        environment = EnvironmentService(client: client, xcodeLocator: xcodeLocator)
        installer = InstallService(client: client)
        space = SpaceService(client: client)
    }

    // MARK: inspect

    /// 设计稿 inspect：放进来的文件里第一款。一次放进多款用 importGames。
    public func inspect(_ inputs: GameInputs) async throws -> GameInfo {
        guard let first = try await importGames(inputs).first else { throw JobClientError(Self.nothingImported) }
        return first
    }

    public static let nothingImported = "没有读到游戏：放进来的文件里没有可导入的 NSP / XCI。"

    /// import 作业登记进游戏库（原件留在原处、只读不复制），再 catalog 读回这几款（已在库里的也算，按导入顺序）。
    public func importGames(_ inputs: GameInputs, events: EventSink = .discard) async throws -> [GameInfo] {
        guard !inputs.paths.isEmpty else { throw JobClientError(Self.nothingImported) }
        try await library.ensureLibrary(events: events)
        let result = try await client.perform("import", extra: ["paths": inputs.paths.map(\.path)], events: events).result
        var ids: [String] = []
        for item in result["imports"] as? [[String: Any]] ?? [] {
            if let id = item["id"] as? String, !id.isEmpty, !ids.contains(id) { ids.append(id) }
        }
        let games = try await library.games()
        return ids.compactMap { id in games.first { $0.id == id }.map(GameInfo.init(summary:)) }
    }

    /// 游戏库全部（catalog）。
    public func catalog() async throws -> [GameSummary] { try await library.games() }

    // MARK: build

    public static let dotnetRecheckFailed = ".NET SDK 装完复查仍不通过：打开环境检查看缺什么，处理后再打 iPhone 包。"

    public func build(_ job: BuildJob, events: EventSink) async throws -> BuildResult {
        switch job.target {
        case .mac:
            var extra: [String: Any] = ["gameId": job.gameId]
            if job.spaceOverride { extra["spaceOverride"] = true }
            let r = try await client.perform("build", extra: extra, events: events).result
            return BuildResult(gameId: job.gameId, target: .mac, outputPath: r["outputPath"] as? String, ipa: nil, raw: r)
        case .iPhone:
            // 打 iPhone 包要 .NET SDK：上次检查说没装就先一键装好再接着出（同旧启动器 packIPhone：玩家只点一次）
            if dotnetMissing {
                _ = try await environment.repair("installDotnet", events: events)
                if dotnetMissing { throw JobClientError(Self.dotnetRecheckFailed) }
            }
            var extra: [String: Any] = ["gameId": job.gameId]
            if let target = job.deviceTarget { extra["target"] = target }
            if job.spaceOverride { extra["spaceOverride"] = true }
            let r = try await client.perform("ipa", extra: extra, events: events).result
            return BuildResult(gameId: job.gameId, target: .iPhone, outputPath: r["outputPath"] as? String, ipa: r["ipa"] as? String, raw: r)
        }
    }

    private var dotnetMissing: Bool { environment.checks?.first { $0.action == "installDotnet" }?.ok == false }

    // MARK: 本机控制口用到的几个作业（AI 操作，11-07；参数同旧启动器 Store 的同名动作）

    /// 放密钥（keys 作业：校验 prod.keys、放进游戏库；新玩家的游戏库先建好）；完了重读游戏库，keysReady 跟着更新。
    public func importKeys(_ file: URL, events: EventSink = .discard) async throws -> String {
        try await library.ensureLibrary(events: events)
        let r = try await client.perform("keys", extra: ["path": file.path], events: events).result
        _ = try? await library.games()
        return r["message"] as? String ?? ""
    }

    /// 删这款（delete 作业：IPA / 资源包 / 出包中间产物 / 缓存，存档与原始游戏文件保留）。
    public func deleteGame(_ gameId: String, events: EventSink = .discard) async throws {
        _ = try await client.perform("delete", extra: ["gameId": gameId], events: events)
    }

    /// 改游戏显示名 / 封面：nil 不动，空字符串恢复默认。
    public func customizeGame(_ gameId: String, name: String?, coverPath: String?) async throws {
        var extra: [String: Any] = ["gameId": gameId]
        if let name { extra["name"] = name }
        if let coverPath { extra["coverPath"] = coverPath }
        _ = try await client.perform("customize", extra: extra, events: .discard)
    }

    /// Mac 端诊断包写到 path（diagnostics 作业）。
    public func exportDiagnostics(to path: URL, events: EventSink = .discard) async throws {
        _ = try await client.perform("diagnostics", extra: ["path": path.path], events: events)
    }

    /// 拉回连着的 iPhone 上各款的诊断（pullDiagnostics 作业）→ 结果（outputPath、apps）。
    public func pullDiagnostics(events: EventSink = .discard) async throws -> [String: Any] {
        try await client.perform("pullDiagnostics", events: events).result
    }

    /// 打 iPhone 包的目标设备探测（targetDevice 作业）：connected / models / osChoices；quick 只给静态机型表、不读设备。
    public func targetDevices(quick: Bool = false) async throws -> [String: Any] {
        try await client.perform("targetDevice", extra: quick ? ["quick": true] : [:]).result
    }

    // MARK: settings

    public func settingsSchema(for game: GameInfo) -> SettingsSchema { SettingsSchema(rows: []) }

    // MARK: run / control

    /// 起一局：settings 只用 uiLanguage（宿主内提示的语言）；游戏语言跟系统、主机模式、宽屏 / 60 帧都由作业层定，settings.values 不读。
    /// 事件：作业事件按序转出，最后一条是这局的结局（result）。
    public func run(_ game: InstalledGame, settings s: Settings, events: EventSink) throws -> RunningGame {
        let handle = try play.play(gameId: game.gameId, uiLanguage: s.uiLanguage,
                                   onEvent: { if !$0.isFinal { events.emit(EngineEvent(job: $0)) } },
                                   onFinish: { events.emit(EngineEvent(outcome: $0)) })
        return RunningGame(gameId: game.gameId, taskId: handle.id)
    }

    public func control(_ game: RunningGame, _ cmd: ControlCommand) {
        switch cmd {
        case .stop:
            guard play.current?.id == game.taskId else { return }   // 不是这一局（已结束 / 别的局）不动
            play.stop()
        }
    }

    // MARK: iPhone 安装（设计稿协议之外）

    public func installReadiness(gameId: String) async throws -> InstallReadiness { try await installer.readiness(gameId: gameId) }

    public func install(gameId: String, events: EventSink = .discard) async throws -> InstallResult {
        try await installer.install(gameId: gameId, events: events)
    }

    public func removeFromPhone(gameId: String, events: EventSink = .discard) async throws -> RemoveResult {
        try await installer.remove(gameId: gameId, events: events)
    }
}
