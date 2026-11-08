// S2Arm（不是上游文件）：覆盖更新（11-08 维护者）。下载、校验、替换、回滚都在共用的 pack/jobs/updater.py，这里只管状态和按钮。
// 玩家的数据不在 App 里；出包等作业进行中不提示、不替换。
import AppKit
import Foundation
import S2ArmAppleAdapter

enum UpdateState: Equatable {
    case idle
    case working(String, Int?)   // 文案号，百分比
    case ready
    case failed(String)          // 错误码
}

extension S2ArmLauncherModel {
    /// 控制口用：{state, detail, percent}。
    var updateStatus: [String: Any] {
        switch updateState {
        case .idle: return ["state": "idle"]
        case .working(let id, let percent): return ["state": "working", "detail": id, "percent": percent ?? NSNull()]
        case .ready: return ["state": "ready", "detail": updateReady?["version"] ?? "", "via": updateReady?["via"] ?? ""]
        case .failed(let code): return ["state": "failed", "detail": code]
        }
    }

    private var currentVersion: String { Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "" }
    private var updatesFolder: URL { provider.client.environment.privateRoot.appendingPathComponent("updates") }

    /// 启动时：告诉替换进程新版起来了；上一次更新失败要告诉玩家。
    func finishPendingUpdate() {
        let fm = FileManager.default
        if fm.fileExists(atPath: updatesFolder.appendingPathComponent("pending.json").path) {
            try? Data("ok".utf8).write(to: updatesFolder.appendingPathComponent("health.ok"))
        }
        let failure = updatesFolder.appendingPathComponent("last_failure.json")
        if let data = try? Data(contentsOf: failure), let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            if let code = json["code"] as? String { updateState = .failed(code) }
            try? fm.removeItem(at: failure)
        }
    }

    /// 启动时查一次；有新版就在后台下好，不打断玩家。查不到（没网、GitHub 不通）什么都不显示。
    func checkForUpdate() {
        guard updateState == .idle else { return }
        Task {
            guard await provider.client.updateCheck(current: currentVersion) != nil, updateState == .idle else { return }
            downloadUpdate()
        }
    }

    func downloadUpdate() {
        switch updateState { case .idle, .failed: break; default: return }
        updateState = .working("update.downloading", 0)
        let install = Bundle.main.bundlePath
        Task {
            let result = await provider.client.updatePrepare(current: currentVersion, install: install) { [weak self] event in
                let state = event["state"] as? String, done = event["done"] as? Double ?? (event["done"] as? Int).map(Double.init), total = event["total"] as? Double ?? (event["total"] as? Int).map(Double.init)
                Task { @MainActor in
                    switch state {
                    case "downloading": self?.updateState = .working("update.downloading", (done != nil && (total ?? 0) > 0) ? Int(done! * 100 / total!) : nil)
                    case "verifying": self?.updateState = .working("update.verifying", nil)
                    case "unpacking": self?.updateState = .working("update.unpacking", nil)
                    default: break
                    }
                }
            }
            guard let result, result["state"] as? String == "ready" else {
                updateState = .failed(result?["code"] as? String ?? "update_failed")
                return
            }
            updateReady = result
            updateState = .ready
        }
    }

    /// 只有玩家点了才重启；出包等作业进行中按钮置灰。
    func restartToUpdate() {
        guard let ready = updateReady, !busy else { return }
        do {
            try provider.client.updateApply(ready: ready, install: Bundle.main.bundlePath, arguments: Array(CommandLine.arguments.dropFirst()))
            NSApp.terminate(nil)
        } catch {
            updateState = .failed("update_failed")
        }
    }
}
