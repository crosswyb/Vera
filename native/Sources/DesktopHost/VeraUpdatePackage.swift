import CryptoKit
import Foundation
import Security

struct VeraUpdateError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

enum VeraUpdatePreparationFailure: LocalizedError {
    case archive(Error)
    case validation(Error)

    var errorDescription: String? {
        switch self {
        case let .archive(error), let .validation(error): return error.localizedDescription
        }
    }
}

enum VeraUpdatePreparationStage: String {
    case downloading, verifying
}

/// Only release archives from the existing official feed enter this pipeline.
enum VeraUpdatePackage {
    static var cacheDirectory: URL {
        FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("com.zsjh.workbench/Updates", isDirectory: true)
    }

    @MainActor
    static func prepare(_ release: VeraRelease, progress: @escaping (Int64, Int64) -> Void,
                        stageChanged: @MainActor (VeraUpdatePreparationStage) -> Void = { _ in },
                        cache: URL = cacheDirectory, currentApp: URL = Bundle.main.bundleURL) async throws -> URL {
        let directory = cache.appendingPathComponent(release.tag_name + "-" + release.archive.sha256.prefix(12), isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                              attributes: [.posixPermissions: 0o700])
        let archive = directory.appendingPathComponent("update.zip")
        let size = release.archive.size ?? 0
        if !FileManager.default.fileExists(atPath: archive.path) {
            stageChanged(.downloading)
            let space = try directory.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
                .volumeAvailableCapacityForImportantUsage ?? 0
            guard space > size * 4 + 100_000_000 else { throw VeraUpdateError(message: "磁盘空间不足，请清理后重试。") }
            guard let url = release.archive.downloadURL else { throw VeraRelease.Failure.invalidRelease }
            try await VeraArchiveDownload.fetch(url, to: archive, expectedSize: size, progress: progress)
        }
        stageChanged(.verifying)
        // Hashing, extraction and signature checks must not block audio or the UI.
        return try await Task.detached(priority: .utility) {
            do { try verifyArchive(archive, expectedSize: size, sha256: release.archive.sha256) }
            catch {
                try? FileManager.default.removeItem(at: archive)
                throw VeraUpdatePreparationFailure.archive(error)
            }
            let payload = directory.appendingPathComponent("payload", isDirectory: true)
            do {
                if FileManager.default.fileExists(atPath: payload.path) { try FileManager.default.removeItem(at: payload) }
                try FileManager.default.createDirectory(at: payload, withIntermediateDirectories: true)
                let app = try extractArchive(archive, to: payload)
                try validate(app, build: release.build, version: String(release.tag_name.dropFirst().components(separatedBy: "-build")[0]),
                             team: signingTeam(currentApp))
                return app
            } catch {
                try? FileManager.default.removeItem(at: payload)
                throw VeraUpdatePreparationFailure.validation(error)
            }
        }.value
    }

    static func extractArchive(_ archive: URL, to payload: URL) throws -> URL {
        var environment = ProcessInfo.processInfo.environment
        // Finder launches can omit LANG. libarchive otherwise escapes non-ASCII names in listings.
        environment["LC_ALL"] = "en_US.UTF-8"
        let listing = try run("/usr/bin/bsdtar", ["-tf", archive.path], environment: environment)
        for entry in listing.split(separator: "\n") {
            let parts = entry.split(separator: "/")
            guard !entry.hasPrefix("/"), !parts.contains(".."), !entry.contains("\\"),
                  parts.first == "真实交互.app" || parts.first == "__MACOSX"
                    || entry == "Applications" || entry == "安装与首次使用.md" else {
                throw VeraUpdateError(message: "更新包包含无效路径，已停止安装。")
            }
        }
        // The distribution ZIP also contains a Finder alias and a guide.
        // Extract only the app, preserving Unicode names and rejecting unsafe links.
        try run("/usr/bin/bsdtar", ["-xf", archive.path, "-C", payload.path, "真实交互.app"], environment: environment)
        return payload.appendingPathComponent("真实交互.app", isDirectory: true)
    }

    static func verifyArchive(_ archive: URL, expectedSize: Int64, sha256: String) throws {
        let attributes = try FileManager.default.attributesOfItem(atPath: archive.path)
        guard (attributes[.size] as? NSNumber)?.int64Value == expectedSize else {
            throw VeraUpdateError(message: "下载未完成或文件大小不符，请重新下载。")
        }
        let handle = try FileHandle(forReadingFrom: archive)
        defer { try? handle.close() }
        var hash = SHA256()
        while let chunk = try handle.read(upToCount: 1_048_576), !chunk.isEmpty { hash.update(data: chunk) }
        guard hash.finalize().map({ String(format: "%02x", $0) }).joined() == sha256 else {
            throw VeraUpdateError(message: "更新包校验不通过，请重新下载。")
        }
    }

    static func signingTeam(_ app: URL) throws -> String {
        var code: SecStaticCode?
        var information: CFDictionary?
        guard SecStaticCodeCreateWithPath(app as CFURL, [], &code) == errSecSuccess, let code,
              SecCodeCopySigningInformation(code, SecCSFlags(rawValue: kSecCSSigningInformation), &information) == errSecSuccess,
              let values = information as? [String: Any], let team = values[kSecCodeInfoTeamIdentifier as String] as? String,
              team.range(of: "^[A-Z0-9]{10}$", options: .regularExpression) != nil else {
            throw VeraUpdateError(message: "当前 App 没有正式签名，无法自动安装更新。")
        }
        return team
    }

    static func validate(_ app: URL, build: Int, version: String, team: String) throws {
        guard let bundle = Bundle(url: app), bundle.bundleIdentifier == "com.zsjh.workbench",
              bundle.object(forInfoDictionaryKey: "CFBundleVersion") as? String == String(build),
              bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String == version else {
            throw VeraUpdateError(message: "更新包的应用身份或版本不符。")
        }
        let root = app.resolvingSymlinksInPath().path + "/"
        guard let files = FileManager.default.enumerator(at: app, includingPropertiesForKeys: [.isSymbolicLinkKey]) else {
            throw VeraUpdateError(message: "无法读取更新包。")
        }
        for case let file as URL in files {
            if try file.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink == true,
               !file.resolvingSymlinksInPath().path.hasPrefix(root) {
                throw VeraUpdateError(message: "更新包包含指向应用之外的链接。")
            }
        }
        guard team.range(of: "^[A-Z0-9]{10}$", options: .regularExpression) != nil else { throw VeraRelease.Failure.invalidRelease }
        var code: SecStaticCode?
        var requirement: SecRequirement?
        let identity = "anchor apple generic and identifier \"com.zsjh.workbench\" and certificate leaf[subject.OU] = \"\(team)\""
        let flags = SecCSFlags(rawValue: kSecCSStrictValidate | kSecCSCheckNestedCode | kSecCSCheckAllArchitectures)
        guard SecStaticCodeCreateWithPath(app as CFURL, [], &code) == errSecSuccess, let code,
              SecRequirementCreateWithString(identity as CFString, [], &requirement) == errSecSuccess,
              SecStaticCodeCheckValidity(code, flags, requirement) == errSecSuccess else {
            throw VeraUpdateError(message: "更新包签名不正确，或与当前 App 的开发者不一致。")
        }
        try run("/usr/sbin/spctl", ["--assess", "--type", "execute", app.path])
    }

    @discardableResult static func run(_ executable: String, _ arguments: [String], environment: [String: String]? = nil) throws -> String {
        let process = Process(), output = Pipe()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.environment = environment
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            throw VeraUpdateError(message: "更新包验证或安装未完成（\(URL(fileURLWithPath: executable).lastPathComponent)，\(process.terminationStatus)），请重试。")
        }
        return String(decoding: data, as: UTF8.self)
    }
}

final class VeraArchiveDownload: NSObject, URLSessionDownloadDelegate, @unchecked Sendable {
    let destination: URL
    let expectedSize: Int64
    let progress: (Int64, Int64) -> Void
    var continuation: CheckedContinuation<Void, Error>?
    var failure: Error?
    var saved = false

    init(destination: URL, expectedSize: Int64, progress: @escaping (Int64, Int64) -> Void) {
        self.destination = destination
        self.expectedSize = expectedSize
        self.progress = progress
    }

    static func fetch(_ url: URL, to destination: URL, expectedSize: Int64,
                      progress: @escaping (Int64, Int64) -> Void) async throws {
        let delegate = VeraArchiveDownload(destination: destination, expectedSize: expectedSize, progress: progress)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 30 * 60
        let session = URLSession(configuration: configuration, delegate: delegate, delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        try await withCheckedThrowingContinuation { continuation in
            delegate.continuation = continuation
            var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData)
            request.setValue("application/octet-stream", forHTTPHeaderField: "Accept")
            session.downloadTask(with: request).resume()
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        let url = request.url
        // GitHub release downloads redirect to their asset CDN. Never follow an arbitrary host or downgrade TLS.
        let allowed = url?.scheme == "https" && ["github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"].contains(url?.host ?? "")
        completionHandler(allowed ? request : nil)
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64,
                    totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        if totalBytesWritten > expectedSize {
            failure = VeraUpdateError(message: "下载文件大小超出发布信息。")
            downloadTask.cancel()
        }
        progress(min(totalBytesWritten, expectedSize), expectedSize)
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        do {
            guard let response = downloadTask.response as? HTTPURLResponse else { throw VeraRelease.Failure.invalidRelease }
            guard response.statusCode == 200 else { throw VeraRelease.Failure.http(response.statusCode) }
            try FileManager.default.moveItem(at: location, to: destination)
            saved = true
        } catch { failure = error }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error = failure ?? error { continuation?.resume(throwing: error) }
        else if saved { continuation?.resume() }
        else { continuation?.resume(throwing: VeraUpdateError(message: "未获得完整的更新包。")) }
        continuation = nil
    }
}
