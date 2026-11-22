import Foundation

struct VeraRelease: Decodable {
    static let releasesPage = URL(string: "https://github.com/crosswyb/Vera/releases")!
    let tag_name: String
    let build: Int
    let notes: String

    var page: URL { Self.releasesPage.appendingPathComponent("tag").appendingPathComponent(tag_name) }

    enum Failure: LocalizedError {
        case invalidRelease, http(Int)
        var errorDescription: String? {
            switch self {
            case .invalidRelease: return "发布信息不完整，请到官方发布页核对。"
            case let .http(status): return "GitHub 返回 HTTP \(status)，请稍后重试或打开发布页。"
            }
        }
    }

    private enum CodingKeys: String, CodingKey { case tag_name, draft, prerelease, assets, body }
    private struct Asset: Decodable { let name: String }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        tag_name = try values.decode(String.self, forKey: .tag_name)
        guard try !values.decode(Bool.self, forKey: .draft), try !values.decode(Bool.self, forKey: .prerelease),
              tag_name.range(of: #"^v[0-9]+\.[0-9]+\.[0-9]+-build[1-9][0-9]*$"#, options: .regularExpression) != nil,
              let number = Int(tag_name.components(separatedBy: "-build").last ?? "") else { throw Failure.invalidRelease }
        let expected = "Vera-\(tag_name.dropFirst())-apple-silicon.dmg"
        guard try values.decode([Asset].self, forKey: .assets).contains(where: { $0.name == expected }) else { throw Failure.invalidRelease }
        build = number
        notes = String((try values.decodeIfPresent(String.self, forKey: .body) ?? "").prefix(20_000))
    }

    static func latest() async throws -> VeraRelease {
        let url = URL(string: "https://api.github.com/repos/crosswyb/Vera/releases/latest")!
        let session = URLSession(configuration: .ephemeral)
        defer { session.finishTasksAndInvalidate() }
        let request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 15)
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw Failure.invalidRelease }
        guard http.statusCode == 200 else { throw Failure.http(http.statusCode) }
        return try JSONDecoder().decode(VeraRelease.self, from: data)
    }
}

/// The menu and workbench share one result and one in-flight request.
@MainActor
final class VeraUpdateChecker {
    static let automaticInterval: TimeInterval = 6 * 60 * 60
    let currentVersion: String
    let currentBuild: Int?
    private(set) var checking = false
    private(set) var latest: VeraRelease?
    private(set) var checkedAt: Date?
    private(set) var failure: String?
    private var lastAttemptAt: Date?
    private let fetchLatest: () async throws -> VeraRelease
    var didChange: (() -> Void)?

    init(version: String, build: Int?, fetchLatest: @escaping () async throws -> VeraRelease = { try await VeraRelease.latest() }) {
        currentVersion = version
        currentBuild = build
        self.fetchLatest = fetchLatest
    }

    var available: Bool {
        guard let latest, let currentBuild else { return false }
        return latest.build > currentBuild
    }

    var payload: [String: Any] {
        let state: String
        if checking { state = "checking" }
        else if failure != nil { state = "error" }
        else if latest == nil { state = "idle" }
        else if available { state = "available" }
        else { state = "current" }
        return ["state": state, "available": available, "currentVersion": currentVersion,
                "currentBuild": currentBuild as Any? ?? NSNull(),
                "latestVersion": latest?.tag_name as Any? ?? NSNull(),
                "notes": latest?.notes ?? "", "error": failure ?? "",
                "checkedAt": checkedAt?.timeIntervalSince1970 as Any? ?? NSNull()]
    }

    func check(automatically: Bool = false, at now: Date = Date()) async {
        guard !checking else { return }
        if automatically, let lastAttemptAt, now.timeIntervalSince(lastAttemptAt) < Self.automaticInterval { return }
        lastAttemptAt = now
        checking = true
        failure = nil
        didChange?()
        defer { checking = false; didChange?() }
        do {
            guard let currentBuild, currentBuild > 0 else { throw VeraRelease.Failure.invalidRelease }
            latest = try await fetchLatest()
            checkedAt = now
        } catch {
            failure = error.localizedDescription
        }
    }
}
