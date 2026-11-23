import Foundation

struct VeraRelease: Decodable {
    static let releasesPage = URL(string: "https://github.com/crosswyb/Vera/releases")!
    let tag_name: String
    let build: Int
    let notes: String
    let archive: Asset

    var page: URL { Self.releasesPage.appendingPathComponent("tag").appendingPathComponent(tag_name) }

    enum Failure: LocalizedError {
        case invalidRelease, http(Int)
        var errorDescription: String? {
            switch self {
            case .invalidRelease: return "更新包信息不完整或不可信，请稍后重试。"
            case let .http(status): return "更新服务返回 HTTP \(status)，请稍后重试。"
            }
        }
    }

    private enum CodingKeys: String, CodingKey { case tag_name, draft, prerelease, assets, body }
    struct Asset: Decodable {
        let id: Int64?
        let name: String
        let browser_download_url: URL?
        let size: Int64?
        let digest: String?

        var sha256: String { String((digest ?? "").dropFirst(7)) }
        var downloadURL: URL? {
            guard let id, id > 0 else { return nil }
            return URL(string: "https://api.github.com/repos/crosswyb/Vera/releases/assets/\(id)")
        }
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        tag_name = try values.decode(String.self, forKey: .tag_name)
        guard try !values.decode(Bool.self, forKey: .draft), try !values.decode(Bool.self, forKey: .prerelease),
              tag_name.range(of: #"^v[0-9]+\.[0-9]+\.[0-9]+-build[1-9][0-9]*$"#, options: .regularExpression) != nil,
              let number = Int(tag_name.components(separatedBy: "-build").last ?? "") else { throw Failure.invalidRelease }
        let expected = "Vera-\(tag_name.dropFirst())-apple-silicon.dmg"
        let assets = try values.decode([Asset].self, forKey: .assets)
        let archiveName = "Vera-\(tag_name.dropFirst())-apple-silicon.zip"
        guard assets.contains(where: { $0.name == expected }),
              let zip = assets.first(where: { $0.name == archiveName }),
              zip.downloadURL != nil,
              let size = zip.size, size > 0, size <= 1_073_741_824,
              zip.digest?.range(of: #"^sha256:[0-9a-f]{64}$"#, options: .regularExpression) != nil,
              zip.browser_download_url?.absoluteString == "https://github.com/crosswyb/Vera/releases/download/\(tag_name)/\(archiveName)"
        else { throw Failure.invalidRelease }
        archive = zip
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
    private enum FailureStage: String {
        case check, download, archive, validation, install
    }

    static let automaticInterval: TimeInterval = 6 * 60 * 60
    let currentVersion: String
    let currentBuild: Int?
    private(set) var checking = false
    private(set) var latest: VeraRelease?
    private(set) var checkedAt: Date?
    private(set) var failure: String?
    private(set) var preparing = false
    private(set) var prepareStage: VeraUpdatePreparationStage?
    private(set) var prepared: URL?
    private(set) var installing = false
    private(set) var downloadedBytes: Int64 = 0
    private(set) var totalBytes: Int64 = 0
    var installBlockedReason = ""
    private var lastAttemptAt: Date?
    private var failureStage: FailureStage?
    private var rejectedArchiveMessage: String?
    private let fetchLatest: () async throws -> VeraRelease
    private let prepareUpdate: (VeraRelease, @escaping (Int64, Int64) -> Void, @MainActor (VeraUpdatePreparationStage) -> Void) async throws -> URL
    var didChange: (() -> Void)?
    var didPrepare: (() -> Void)?

    init(version: String, build: Int?, fetchLatest: @escaping () async throws -> VeraRelease = { try await VeraRelease.latest() },
         prepareUpdate: @escaping (VeraRelease, @escaping (Int64, Int64) -> Void, @MainActor (VeraUpdatePreparationStage) -> Void) async throws -> URL = { release, progress, stageChanged in
             try await VeraUpdatePackage.prepare(release, progress: progress, stageChanged: stageChanged)
         }) {
        currentVersion = version
        currentBuild = build
        self.fetchLatest = fetchLatest
        self.prepareUpdate = prepareUpdate
    }

    var available: Bool {
        guard let latest, let currentBuild else { return false }
        return latest.build > currentBuild
    }

    var payload: [String: Any] {
        let state: String
        if installing { state = "installing" }
        else if preparing { state = prepareStage?.rawValue ?? "preparing" }
        else if checking { state = "checking" }
        else if prepared != nil { state = "ready" }
        else if failure != nil { state = "error" }
        else if latest == nil { state = "idle" }
        else if available { state = "available" }
        else { state = "current" }
        return ["state": state, "available": available, "currentVersion": currentVersion,
                "currentBuild": currentBuild as Any? ?? NSNull(),
                "latestVersion": latest?.tag_name as Any? ?? NSNull(),
                "notes": latest?.notes ?? "", "error": failure ?? "", "failureStage": failureStage?.rawValue ?? "",
                "downloadedBytes": downloadedBytes, "totalBytes": totalBytes,
                "downloadProgress": totalBytes > 0 ? min(1, Double(downloadedBytes) / Double(totalBytes)) : 0,
                "installBlockedReason": installBlockedReason,
                "checkedAt": checkedAt?.timeIntervalSince1970 as Any? ?? NSNull()]
    }

    func check(automatically: Bool = false, at now: Date = Date()) async {
        guard !checking, !preparing, !installing else { return }
        if automatically, let lastAttemptAt, now.timeIntervalSince(lastAttemptAt) < Self.automaticInterval { return }
        lastAttemptAt = now
        checking = true
        failure = nil
        failureStage = nil
        didChange?()
        do {
            guard let currentBuild, currentBuild > 0 else { throw VeraRelease.Failure.invalidRelease }
            let release = try await fetchLatest()
            if latest?.tag_name != release.tag_name || latest?.archive.digest != release.archive.digest {
                prepared = nil
                rejectedArchiveMessage = nil
            }
            latest = release
            checkedAt = now
        } catch {
            failure = error.localizedDescription
            failureStage = .check
        }
        checking = false
        if available, prepared == nil, failure == nil, let release = latest {
            if let rejectedArchiveMessage {
                failure = rejectedArchiveMessage
                failureStage = .validation
            } else {
                await prepare(release)
                return
            }
        }
        didChange?()
    }

    func retry() async {
        guard !checking, !preparing, !installing else { return }
        if available, prepared == nil, let release = latest, [.download, .archive].contains(failureStage) {
            await prepare(release)
        } else {
            await check()
        }
    }

    private func prepare(_ release: VeraRelease) async {
        preparing = true
        failure = nil
        failureStage = nil
        downloadedBytes = 0
        totalBytes = release.archive.size ?? 0
        didChange?()
        do {
            prepared = try await prepareUpdate(release, { [weak self] received, total in
                Task { @MainActor in
                    guard let self, self.prepareStage == .downloading else { return }
                    self.downloadedBytes = max(self.downloadedBytes, received)
                    self.totalBytes = total
                    self.didChange?()
                }
            }, { stage in
                prepareStage = stage
                if stage == .verifying { downloadedBytes = totalBytes }
                didChange?()
            })
            downloadedBytes = totalBytes
        } catch VeraUpdatePreparationFailure.archive(let error) {
            failureStage = .archive
            failure = error.localizedDescription
        } catch VeraUpdatePreparationFailure.validation(let error) {
            failureStage = .validation
            failure = error.localizedDescription
            rejectedArchiveMessage = error.localizedDescription
        } catch {
            failureStage = .download
            failure = error.localizedDescription
        }
        prepareStage = nil
        preparing = false
        didChange?()
        if prepared != nil { didPrepare?() }
    }

    func installationStarted() {
        installing = true
        failure = nil
        failureStage = nil
        didChange?()
    }

    func installationFailed(_ error: Error) {
        installing = false
        failure = error.localizedDescription
        failureStage = .install
        didChange?()
    }
}
