import CryptoKit
import Foundation

private struct SmokeFailure: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

private enum InjectedFailure: Error { case validation, relaunch }

private final class DownloadProgress {
    private let lock = NSLock()
    private var value: (Int64, Int64)?

    func update(_ received: Int64, _ total: Int64) {
        lock.lock()
        defer { lock.unlock() }
        value = (received, total)
    }

    func result() -> (Int64, Int64)? {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

@main
struct UpdateInstallSmoke {
    static let manager = FileManager.default

    static func require(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        if try !condition() { throw SmokeFailure(message) }
    }

    static func expectFailure(_ action: () throws -> Void) throws {
        do { try action() }
        catch { return }
        throw SmokeFailure("An operation expected to fail succeeded")
    }

    static func expectDownloadFailure(_ action: () async throws -> Void) async throws -> Error {
        do { try await action() }
        catch { return error }
        throw SmokeFailure("A download expected to fail succeeded")
    }

    static func marker(_ app: URL) throws -> String {
        try String(contentsOf: app.appendingPathComponent("Contents/Resources/fixture-version"), encoding: .utf8)
    }

    static func app(at url: URL, marker: String) throws {
        let contents = url.appendingPathComponent("Contents", isDirectory: true)
        try manager.createDirectory(at: contents.appendingPathComponent("Resources"), withIntermediateDirectories: true)
        let plist: [String: String] = ["CFBundleIdentifier": "com.zsjh.workbench", "CFBundlePackageType": "APPL",
                                     "CFBundleVersion": marker == "old" ? "58" : "59",
                                     "CFBundleShortVersionString": marker == "old" ? "0.1.1" : "0.1.2"]
        try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
            .write(to: contents.appendingPathComponent("Info.plist"))
        try Data(marker.utf8).write(to: contents.appendingPathComponent("Resources/fixture-version"))
    }

    static func plan(in directory: URL, name: String) throws -> VeraUpdateInstallPlan {
        let root = directory.appendingPathComponent(name, isDirectory: true)
        let source = root.appendingPathComponent("下载 副本/真实交互.app", isDirectory: true)
        let destination = root.appendingPathComponent("安装 目录/真实交互.app", isDirectory: true)
        try app(at: source, marker: "new")
        try app(at: destination, marker: "old")
        return VeraUpdateInstallPlan(source: source, destination: destination, version: "0.1.2", build: 59,
                                     previousBuild: 58, team: "SYNTHETIC1", parentPID: 2)
    }

    static func verifyNoInstallationResidue(_ plan: VeraUpdateInstallPlan) throws {
        let entries = try manager.contentsOfDirectory(atPath: plan.destination.deletingLastPathComponent().path)
        try require(entries == ["真实交互.app"], "Staging or backup apps were left in the installation directory")
    }

    static func downloadChecks(in directory: URL) async throws {
        let server = try JSONSerialization.jsonObject(with: Data(contentsOf: directory.appendingPathComponent("download-server.json"))) as! [String: String]
        let base = URL(string: server["url"]!)!
        let payload = try Data(contentsOf: directory.appendingPathComponent("fixture-payload.bin"))
        let expectedSize = Int64(payload.count)
        let digest = SHA256.hash(data: payload).map { String(format: "%02x", $0) }.joined()
        let archive = directory.appendingPathComponent("成功 下载.bin")
        let progress = DownloadProgress()
        try await VeraArchiveDownload.fetch(base.appendingPathComponent("archive"), to: archive,
                                            expectedSize: expectedSize, progress: progress.update)
        try require(try Data(contentsOf: archive) == payload, "Successful HTTP download changed bytes")
        try require(progress.result()?.0 == expectedSize && progress.result()?.1 == expectedSize, "Download progress never reached its expected length")
        try VeraUpdatePackage.verifyArchive(archive, expectedSize: expectedSize, sha256: digest)
        try expectFailure { try VeraUpdatePackage.verifyArchive(archive, expectedSize: expectedSize, sha256: String(repeating: "0", count: 64)) }
        var corrupted = payload
        corrupted[0] ^= 0xff
        let corruptArchive = directory.appendingPathComponent("损坏.bin")
        try corrupted.write(to: corruptArchive)
        try expectFailure { try VeraUpdatePackage.verifyArchive(corruptArchive, expectedSize: expectedSize, sha256: digest) }
        print("PASS: real offline HTTP download, progress and SHA-256 corruption rejection")

        let recovered = directory.appendingPathComponent("recovered.bin")
        try await VeraArchiveDownload.fetch(base.appendingPathComponent("flaky"), to: recovered,
                                            expectedSize: expectedSize, progress: { _, _ in })
        try VeraUpdatePackage.verifyArchive(recovered, expectedSize: expectedSize, sha256: digest)
        print("PASS: interrupted transfer recovered without a second user action")

        for route in ["missing", "truncated", "oversized", "redirect"] {
            let destination = directory.appendingPathComponent(route + ".bin")
            let size = route == "oversized" ? 1024 : expectedSize
            let error = try await expectDownloadFailure {
                try await VeraArchiveDownload.fetch(base.appendingPathComponent(route), to: destination,
                                                    expectedSize: size, progress: { _, _ in })
            }
            if route == "missing" {
                guard case VeraRelease.Failure.http(404) = error else { throw SmokeFailure("404 download had an unexpected failure: \(error)") }
            }
            if route == "redirect" {
                guard case VeraRelease.Failure.http(302) = error else { throw SmokeFailure("Untrusted redirect was followed: \(error)") }
            }
            try require(!manager.fileExists(atPath: destination.path), "Failed \(route) download left an installable archive")
        }
        let short = directory.appendingPathComponent("short.bin")
        try await VeraArchiveDownload.fetch(base.appendingPathComponent("short"), to: short,
                                            expectedSize: expectedSize, progress: { _, _ in })
        try expectFailure { try VeraUpdatePackage.verifyArchive(short, expectedSize: expectedSize, sha256: digest) }
        print("PASS: HTTP 404, truncated body, oversized body, untrusted redirect and short archive rejection")
    }

    static func installChecks(in directory: URL) throws {
        let meeting = directory.appendingPathComponent("应用外 用户资料/会议 空间/会议记录.md")
        try manager.createDirectory(at: meeting.deletingLastPathComponent(), withIntermediateDirectories: true)
        let meetingBytes = Data("合成会议资料保持原样\n".utf8)
        try meetingBytes.write(to: meeting)

        let success = try plan(in: directory, name: "成功 安装")
        var validations = 0
        var relaunches = 0
        try success.install(validate: { app in
            validations += 1
            try require(try marker(app) == "new", "The candidate was not the new app")
        }, relaunch: { app in
            relaunches += 1
            try require(app == success.destination && (try marker(app)) == "new", "Relaunch did not use the installed app")
        })
        try require(validations == 2 && relaunches == 1, "Installation did not validate both source and staged copy")
        try require(try marker(success.destination) == "new", "Successful replacement did not install the new app")
        try require(try marker(success.source) == "new", "Installation unexpectedly removed its downloaded source")
        try verifyNoInstallationResidue(success)

        let invalidSource = try plan(in: directory, name: "原包 校验失败")
        var invalidSourceLaunched = false
        try expectFailure {
            try invalidSource.install(validate: { _ in throw InjectedFailure.validation },
                                      relaunch: { _ in invalidSourceLaunched = true })
        }
        try require(!invalidSourceLaunched, "Invalid source was launched")
        try require(try marker(invalidSource.destination) == "old", "Source validation failure changed the old app")
        try verifyNoInstallationResidue(invalidSource)

        let invalidCopy = try plan(in: directory, name: "复制后 校验失败")
        var invalidCopyLaunched = false
        try expectFailure {
            try invalidCopy.install(validate: { candidate in
                if candidate != invalidCopy.source { throw InjectedFailure.validation }
            }, relaunch: { _ in invalidCopyLaunched = true })
        }
        try require(!invalidCopyLaunched, "Invalid staged copy was launched")
        try require(try marker(invalidCopy.destination) == "old", "Staging validation failure changed the old app")
        try verifyNoInstallationResidue(invalidCopy)

        let failedRelaunch = try plan(in: directory, name: "重新打开失败 回滚")
        var launchedVersions: [String] = []
        try expectFailure {
            try failedRelaunch.install(validate: { candidate in
                try require(try marker(candidate) == "new", "Rollback fixture candidate was invalid")
            }, relaunch: { candidate in
                let version = try marker(candidate)
                launchedVersions.append(version)
                if version == "new" { throw InjectedFailure.relaunch }
            })
        }
        try require(launchedVersions == ["new"], "Rollback attempted an extra launch before the helper could reopen the old app")
        try require(try marker(failedRelaunch.destination) == "old", "Relaunch failure did not restore the old app")
        try verifyNoInstallationResidue(failedRelaunch)
        try require(try Data(contentsOf: meeting) == meetingBytes, "Updating the app changed external meeting data")
        print("PASS: actual copy/replacement, source and staging rejection, relaunch rollback, Chinese/space paths and untouched external meeting data")
        print("NOTE: installer validation and app launching were injected; this smoke does not claim Developer ID, Gatekeeper or LaunchServices acceptance")
    }

    static func extractionChecks(in directory: URL) throws {
        let payload = directory.appendingPathComponent("中文 分发包", isDirectory: true)
        try manager.createDirectory(at: payload, withIntermediateDirectories: true)
        let app = try VeraUpdatePackage.extractArchive(directory.appendingPathComponent("distribution.zip"), to: payload)
        let contents = try String(contentsOf: app.appendingPathComponent("Contents/Resources/中文 文件.txt"), encoding: .utf8)
        try require(contents == "合成应用内容", "Unicode archive paths were not preserved")
        try require(try manager.contentsOfDirectory(atPath: payload.path) == ["真实交互.app"],
                    "Distribution extras were extracted alongside the app")
        try expectFailure {
            _ = try VeraUpdatePackage.extractArchive(directory.appendingPathComponent("unsafe.zip"), to: payload)
        }
        try require(!manager.fileExists(atPath: directory.appendingPathComponent("escape.txt").path), "Archive traversal escaped the payload")
        print("PASS: Unicode distribution ZIP, selective app extraction and path traversal rejection")
    }

    static func main() async throws {
        let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        try await downloadChecks(in: root)
        try extractionChecks(in: root)
        try installChecks(in: root)
    }
}
