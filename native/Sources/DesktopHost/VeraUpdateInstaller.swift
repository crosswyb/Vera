import AppKit
import Darwin
import Foundation

/// The existing executable also runs the installer after its parent has quit.
/// No shell script, user-supplied command, or elevated service is involved.
struct VeraUpdateInstallPlan: Codable {
    let source: URL
    let destination: URL
    let version: String
    let build: Int
    let previousBuild: Int
    let team: String
    let parentPID: Int32

    func preflight() throws {
        let manager = FileManager.default
        guard source.isFileURL, destination.isFileURL, build > previousBuild,
              destination.pathExtension == "app", source.pathExtension == "app",
              !source.resolvingSymlinksInPath().path.hasPrefix(destination.resolvingSymlinksInPath().path + "/"),
              manager.isWritableFile(atPath: destination.path),
              manager.isWritableFile(atPath: destination.deletingLastPathComponent().path) else {
            throw VeraUpdateError(message: "当前 App 所在位置不可写。请将 App 放入您有写入权限的应用程序目录后重试。")
        }
    }

    func validateSource(_ app: URL) throws {
        try VeraUpdatePackage.validate(app, build: build, version: version, team: team)
    }

    func install(validate: (URL) throws -> Void, relaunch: (URL) throws -> Void) throws {
        try preflight()
        try validate(source)
        let manager = FileManager.default
        let directory = destination.deletingLastPathComponent()
        let identifier = UUID().uuidString
        let staging = directory.appendingPathComponent(".Vera-update-\(identifier).app", isDirectory: true)
        let backupName = ".Vera-backup-\(identifier).app"
        let backup = directory.appendingPathComponent(backupName, isDirectory: true)
        defer { try? manager.removeItem(at: staging) }
        // Copy to the destination volume before the atomic replacement. The old
        // application is retained until LaunchServices accepts the new one.
        try VeraUpdatePackage.run("/usr/bin/ditto", [source.path, staging.path])
        try validate(staging)
        do {
            _ = try manager.replaceItemAt(destination, withItemAt: staging, backupItemName: backupName,
                                         options: [.withoutDeletingBackupItem])
            try relaunch(destination)
        } catch {
            if manager.fileExists(atPath: backup.path) {
                if manager.fileExists(atPath: destination.path) { try manager.removeItem(at: destination) }
                try manager.moveItem(at: backup, to: destination)
            }
            // The helper's error handler reopens the restored application once.
            throw error
        }
        try? manager.removeItem(at: backup)
    }

    @MainActor static func runHelper(planURL: URL) -> Int32 {
        let application = NSApplication.shared
        application.setActivationPolicy(.accessory)
        let manager = FileManager.default
        var plan: VeraUpdateInstallPlan?
        do {
            let value = try JSONDecoder().decode(Self.self, from: Data(contentsOf: planURL))
            plan = value
            guard value.parentPID > 1, value.parentPID == getppid(),
                  value.destination.resolvingSymlinksInPath() == Bundle.main.bundleURL.resolvingSymlinksInPath(),
                  value.previousBuild == Int(Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? ""),
                  value.team == (try VeraUpdatePackage.signingTeam(Bundle.main.bundleURL)),
                  value.source.resolvingSymlinksInPath().path.hasPrefix(VeraUpdatePackage.cacheDirectory.resolvingSymlinksInPath().path + "/")
            else { throw VeraUpdateError(message: "安装请求无效，当前 App 未作更改。") }
            try value.preflight()
            try Data("ready".utf8).write(to: planURL.appendingPathExtension("ready"), options: .atomic)
            let deadline = Date().addingTimeInterval(120)
            while kill(value.parentPID, 0) == 0 {
                guard Date() < deadline else { throw VeraUpdateError(message: "应用尚未退出，已取消安装。请稍后重试。") }
                Thread.sleep(forTimeInterval: 0.1)
            }
            try value.install(validate: value.validateSource) { app in
                try VeraUpdatePackage.run("/usr/bin/open", ["-n", app.path])
            }
            try? manager.removeItem(at: planURL.deletingLastPathComponent())
            return 0
        } catch {
            // Leave the downloaded package in place for an explicit retry.
            // Do not expose diagnostic paths in the user-facing error.
            let alert = NSAlert()
            alert.messageText = "更新安装未完成"
            alert.informativeText = error.localizedDescription + "\n原 App 已保留，可重新打开后重试。"
            alert.addButton(withTitle: "好")
            application.activate(ignoringOtherApps: true)
            alert.runModal()
            if let plan, kill(plan.parentPID, 0) != 0 { _ = try? VeraUpdatePackage.run("/usr/bin/open", ["-n", plan.destination.path]) }
            return 1
        }
    }
}
