import Foundation

@main
struct ReleaseCheckSmoke {
    @MainActor static func main() async throws {
        func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
            if !condition() { throw NSError(domain: "ReleaseCheckSmoke", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
        }
        func release(_ build: Int, draft: Bool = false, prerelease: Bool = false, asset: Bool = true,
                     archiveChanges: [String: Any] = [:]) throws -> VeraRelease {
            let prefix = "Vera-0.1.0-build\(build)-apple-silicon"
            var archive: [String: Any] = ["id": 12345, "name": prefix + ".zip", "size": 1024,
                "digest": "sha256:" + String(repeating: "a", count: 64),
                "browser_download_url": "https://github.com/crosswyb/Vera/releases/download/v0.1.0-build\(build)/\(prefix).zip"]
            archive.merge(archiveChanges) { _, value in value }
            let files: [[String: Any]] = [["name": prefix + ".dmg"], archive]
            let value: [String: Any] = ["tag_name": "v0.1.0-build\(build)", "draft": draft, "prerelease": prerelease,
                "body": "合成发布说明", "assets": asset ? files : []]
            return try JSONDecoder().decode(VeraRelease.self, from: JSONSerialization.data(withJSONObject: value))
        }
        let newer = try release(58)
        for options in [(true, false, true), (false, true, true), (false, false, false)] {
            do {
                _ = try release(58, draft: options.0, prerelease: options.1, asset: options.2)
                throw NSError(domain: "ReleaseCheckSmoke", code: 2, userInfo: [NSLocalizedDescriptionKey: "Invalid release accepted"])
            } catch VeraRelease.Failure.invalidRelease { }
        }
        for changes: [String: Any] in [["id": 0], ["id": -1], ["id": NSNull()], ["digest": ""], ["digest": "sha256:abc"], ["size": 0],
            ["size": 1_073_741_825], ["browser_download_url": "https://example.invalid/update.zip"], ["name": "unrelated.zip"]] {
            do {
                _ = try release(58, archiveChanges: changes)
                throw NSError(domain: "ReleaseCheckSmoke", code: 3, userInfo: [NSLocalizedDescriptionKey: "Untrusted archive accepted"])
            } catch VeraRelease.Failure.invalidRelease { }
        }
        try require(newer.notes == "合成发布说明" && newer.page.lastPathComponent == "v0.1.0-build58", "Release details lost")
        try require(newer.archive.downloadURL?.absoluteString == "https://api.github.com/repos/crosswyb/Vera/releases/assets/12345",
                    "Archive download escaped the official repository API")
        var requests = 0
        var fail = false
        var reply = newer
        var downloads = 0
        var downloadFailure = true
        let checker = VeraUpdateChecker(version: "0.1.0", build: 57, fetchLatest: {
            requests += 1
            if fail { throw VeraRelease.Failure.http(403) }
            return reply
        }, prepareUpdate: { _, progress, stageChanged in
            downloads += 1
            stageChanged(.downloading)
            progress(512, 1024)
            if downloadFailure { throw VeraRelease.Failure.http(503) }
            stageChanged(.verifying)
            return URL(fileURLWithPath: "/tmp/synthetic-update.app")
        })
        let start = Date(timeIntervalSince1970: 1000)
        var observedStates: [String] = []
        checker.didChange = { observedStates.append(checker.payload["state"] as! String) }
        await checker.check(automatically: true, at: start)
        try require(requests == 1 && checker.available && downloads == 1 &&
                    observedStates == ["checking", "preparing", "downloading", "error"] &&
                    checker.payload["failureStage"] as? String == "download", "Automatic download did not start or report failure")
        _ = try JSONSerialization.data(withJSONObject: checker.payload)
        await checker.check(automatically: true, at: start.addingTimeInterval(60))
        try require(requests == 1, "Activation bypassed the automatic interval")
        downloadFailure = false
        fail = true
        observedStates.removeAll()
        await checker.retry()
        try require(requests == 1 && downloads == 2 && checker.prepared != nil &&
                    observedStates == ["preparing", "downloading", "verifying", "ready"],
                    "Manual retry did not reuse the known release while the release endpoint was unavailable")
        fail = true
        await checker.check(at: start.addingTimeInterval(61))
        try require(requests == 2 && checker.failure != nil && checker.available && checker.prepared != nil && checker.latest?.notes == newer.notes,
                    "Failed manual check lost a known update or could not bypass the interval")
        fail = false
        reply = try release(57)
        await checker.check(automatically: true, at: start.addingTimeInterval(61 + VeraUpdateChecker.automaticInterval))
        try require(requests == 3 && !checker.available && checker.failure == nil && checker.prepared == nil, "Scheduled check did not recover")
        reply = try release(56)
        await checker.check()
        try require(!checker.available, "An older release was offered as an update")

        let (started, signal) = AsyncStream.makeStream(of: Void.self)
        var completion: CheckedContinuation<VeraRelease, Error>?
        var concurrentRequests = 0
        let shared = VeraUpdateChecker(version: "0.1.0", build: 57, fetchLatest: {
            concurrentRequests += 1
            return try await withCheckedThrowingContinuation { continuation in
                completion = continuation
                signal.yield(())
            }
        }, prepareUpdate: { _, _, _ in URL(fileURLWithPath: "/tmp/synthetic-update.app") })
        let pending = Task { await shared.check(automatically: true) }
        var iterator = started.makeAsyncIterator()
        _ = await iterator.next()
        await shared.check()
        try require(concurrentRequests == 1 && shared.checking, "Concurrent callers duplicated the request")
        completion?.resume(returning: newer)
        await pending.value
        signal.finish()
        try require(shared.available && !shared.checking, "Shared check did not settle")
        let invalid = VeraUpdateChecker(version: "fixture", build: nil, fetchLatest: { newer })
        await invalid.check()
        try require(invalid.failure != nil && !invalid.available, "Missing bundle build was accepted")
        _ = try JSONSerialization.data(withJSONObject: invalid.payload)
        var validations = 0
        var candidate = newer
        let rejected = VeraUpdateChecker(version: "0.1.0", build: 57, fetchLatest: { candidate },
                                         prepareUpdate: { _, _, stageChanged in
            validations += 1
            stageChanged(.verifying)
            if validations == 1 {
                throw VeraUpdatePreparationFailure.validation(VeraUpdateError(message: "更新包包含无效路径"))
            }
            return URL(fileURLWithPath: "/tmp/synthetic-update.app")
        })
        await rejected.check()
        await rejected.retry()
        try require(validations == 1 && rejected.prepared == nil &&
                    rejected.payload["failureStage"] as? String == "validation",
                    "Checking again repeated validation of the same rejected package")
        candidate = try release(59)
        await rejected.check()
        try require(validations == 2 && rejected.prepared != nil, "A new release did not clear the previous rejection")
        print("PASS: release validation, automatic interval, manual retry, failed refresh, older release, and shared requests")
        if CommandLine.arguments.contains("--live") {
            let live = try await VeraRelease.latest()
            print("PASS: live GitHub release \(live.tag_name), build \(live.build)")
        }
        if CommandLine.arguments.contains("--live-download") {
            let live = try await VeraRelease.latest()
            let testDirectory = FileManager.default.temporaryDirectory.appendingPathComponent("VeraLiveUpdate-" + UUID().uuidString)
            defer { try? FileManager.default.removeItem(at: testDirectory) }
            _ = try await VeraUpdatePackage.prepare(live, progress: { _, _ in }, cache: testDirectory,
                currentApp: URL(fileURLWithPath: "/Applications/真实交互.app"))
            print("PASS: actual official archive downloaded, SHA-256 matched, application identity and signing team matched, Gatekeeper accepted")
        }
    }
}
