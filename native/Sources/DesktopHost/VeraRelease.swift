import Foundation

struct VeraRelease: Decodable {
    static let releasesPage = URL(string: "https://github.com/crosswyb/Vera/releases")!
    let tag_name: String
    let build: Int

    enum Failure: LocalizedError {
        case invalidRelease, http(Int)
        var errorDescription: String? {
            switch self {
            case .invalidRelease: return "发布信息不完整，请到官方发布页核对。"
            case let .http(status): return "GitHub 返回 HTTP \(status)，请稍后重试或打开发布页。"
            }
        }
    }

    private enum CodingKeys: String, CodingKey { case tag_name, draft, prerelease, assets }
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
