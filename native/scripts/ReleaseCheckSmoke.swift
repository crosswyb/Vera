import Foundation

@main
struct ReleaseCheckSmoke {
    @MainActor static func main() async throws {
        func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
            if !condition() { throw NSError(domain: "ReleaseCheckSmoke", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
        }
        func release(_ build: Int, draft: Bool = false, prerelease: Bool = false, asset: Bool = true) throws -> VeraRelease {
            let value: [String: Any] = ["tag_name": "v0.1.0-build\(build)", "draft": draft, "prerelease": prerelease,
                "body": "合成发布说明", "assets": asset ? [["name": "Vera-0.1.0-build\(build)-apple-silicon.dmg"]] : []]
            return try JSONDecoder().decode(VeraRelease.self, from: JSONSerialization.data(withJSONObject: value))
        }
        let newer = try release(58)
        for options in [(true, false, true), (false, true, true), (false, false, false)] {
            do {
                _ = try release(58, draft: options.0, prerelease: options.1, asset: options.2)
                throw NSError(domain: "ReleaseCheckSmoke", code: 2, userInfo: [NSLocalizedDescriptionKey: "Invalid release accepted"])
            } catch VeraRelease.Failure.invalidRelease { }
        }
        try require(newer.notes == "合成发布说明" && newer.page.lastPathComponent == "v0.1.0-build58", "Release details lost")
        var requests = 0
        var fail = false
        var reply = newer
        let checker = VeraUpdateChecker(version: "0.1.0", build: 57) {
            requests += 1
            if fail { throw VeraRelease.Failure.http(403) }
            return reply
        }
        let start = Date(timeIntervalSince1970: 1000)
        var observedStates: [String] = []
        checker.didChange = { observedStates.append(checker.payload["state"] as! String) }
        await checker.check(automatically: true, at: start)
        try require(requests == 1 && checker.available && observedStates == ["checking", "available"], "Startup check failed")
        _ = try JSONSerialization.data(withJSONObject: checker.payload)
        await checker.check(automatically: true, at: start.addingTimeInterval(60))
        try require(requests == 1, "Activation bypassed the automatic interval")
        fail = true
        await checker.check(at: start.addingTimeInterval(61))
        try require(requests == 2 && checker.failure != nil && checker.available && checker.latest?.notes == newer.notes,
                    "Failed manual check lost a known update or could not bypass the interval")
        fail = false
        reply = try release(57)
        await checker.check(automatically: true, at: start.addingTimeInterval(61 + VeraUpdateChecker.automaticInterval))
        try require(requests == 3 && !checker.available && checker.failure == nil, "Scheduled check did not recover")
        reply = try release(56)
        await checker.check()
        try require(!checker.available, "An older release was offered as an update")

        let (started, signal) = AsyncStream.makeStream(of: Void.self)
        var completion: CheckedContinuation<VeraRelease, Error>?
        var concurrentRequests = 0
        let shared = VeraUpdateChecker(version: "0.1.0", build: 57) {
            concurrentRequests += 1
            return try await withCheckedThrowingContinuation { continuation in
                completion = continuation
                signal.yield(())
            }
        }
        let pending = Task { await shared.check(automatically: true) }
        var iterator = started.makeAsyncIterator()
        _ = await iterator.next()
        await shared.check()
        try require(concurrentRequests == 1 && shared.checking, "Concurrent callers duplicated the request")
        completion?.resume(returning: newer)
        await pending.value
        signal.finish()
        try require(shared.available && !shared.checking, "Shared check did not settle")
        let invalid = VeraUpdateChecker(version: "fixture", build: nil) { newer }
        await invalid.check()
        try require(invalid.failure != nil && !invalid.available, "Missing bundle build was accepted")
        _ = try JSONSerialization.data(withJSONObject: invalid.payload)
        print("PASS: release validation, automatic interval, manual retry, failed refresh, older release, and shared requests")
        if CommandLine.arguments.contains("--live") {
            let live = try await VeraRelease.latest()
            print("PASS: live GitHub release \(live.tag_name), build \(live.build)")
        }
    }
}
