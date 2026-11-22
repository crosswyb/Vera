import Foundation

/// A private snapshot for one native task. Never expose this value in events.
public struct SpeechSessionConfig: Decodable, Sendable {
    public let provider: String
    public let endpoint: String
    public let model: String
    public let apiKey: String
    public let fallbackToApple: Bool
    public let revision: Int
    public let leaseSeconds: Double?

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        provider = try values.decode(String.self, forKey: .provider)
        endpoint = try values.decode(String.self, forKey: .endpoint)
        model = try values.decodeIfPresent(String.self, forKey: .model) ?? ""
        apiKey = try values.decode(String.self, forKey: .apiKey)
        fallbackToApple = try values.decode(Bool.self, forKey: .fallbackToApple)
        revision = try values.decode(Int.self, forKey: .revision)
        leaseSeconds = try values.decodeIfPresent(Double.self, forKey: .leaseSeconds)
    }

    enum CodingKeys: String, CodingKey {
        case provider, endpoint, model, revision
        case apiKey = "api_key", fallbackToApple = "fallback_to_apple", leaseSeconds = "lease_seconds"
    }
}

/// Short-lived permission to transmit. Once expired or revoked, a delayed
/// renewal cannot reopen this session. Lease expiry also cancels active I/O.
@MainActor
public final class SpeechLease {
    private let clock = ContinuousClock()
    private var deadline: ContinuousClock.Instant
    private var timer: Task<Void, Never>?
    private var revoked = false
    private var handlers: [UUID: () -> Void] = [:]

    public init(seconds: Double) {
        let duration = Self.duration(seconds)
        deadline = clock.now.advanced(by: duration)
        schedule()
    }

    public func renew(seconds: Double) {
        guard isValid else { return }
        deadline = clock.now.advanced(by: Self.duration(seconds))
        schedule()
    }

    public func revoke() {
        guard !revoked else { return }
        revoked = true
        timer?.cancel()
        let callbacks = handlers.values
        handlers.removeAll()
        for callback in callbacks { callback() }
    }

    public var isValid: Bool {
        if !revoked && clock.now >= deadline { revoke() }
        return !revoked
    }

    func check() throws {
        guard isValid else { throw CloudSpeechError.authorizationEnded }
    }

    func onRevoke(_ handler: @escaping () -> Void) -> UUID {
        let id = UUID()
        if isValid { handlers[id] = handler } else { handler() }
        return id
    }

    func removeHandler(_ id: UUID?) {
        if let id { handlers.removeValue(forKey: id) }
    }

    private static func duration(_ seconds: Double) -> Duration {
        .seconds(seconds.isFinite ? max(0, min(seconds, 30)) : 0)
    }

    private func schedule() {
        timer?.cancel()
        let target = deadline
        timer = Task { [weak self, clock] in
            do { try await clock.sleep(until: target) } catch { return }
            guard let self, self.deadline == target else { return }
            self.revoke()
        }
    }
}

/// Error descriptions are deliberately independent of the raw response and URL.
/// Neither a server-supplied message nor an underlying HTTP request is logged.
enum CloudSpeechError: LocalizedError, Equatable {
    case invalidConfiguration, authorizationEnded, permission, quota, model
    case unavailable, timeout, rateLimited, invalidAudio, invalidResponse, responseTooLarge
    case inputOverflow, outputOverflow, missingSample
    case providerFailure(code: String, message: String)

    var retryable: Bool {
        switch self {
        case .unavailable, .timeout, .rateLimited: return true
        case .providerFailure: return providerCategory == .unavailable || providerCategory == .timeout || providerCategory == .rateLimited
        default: return false
        }
    }

    private enum ProviderCategory { case permission, quota, model, unavailable, timeout, rateLimited, other }

    private var providerCategory: ProviderCategory {
        guard case let .providerFailure(code, message) = self else { return .other }
        let descriptor = (code + " " + message).lowercased()
        if descriptor.contains("quota") || descriptor.contains("balance") || descriptor.contains("arrear") { return .quota }
        if descriptor.contains("auth") || descriptor.contains("permission") || descriptor.contains("access_denied")
            || descriptor.contains("api key") || descriptor.contains("apikey") || descriptor.contains("api_key") { return .permission }
        if descriptor.contains("model") { return .model }
        if descriptor.contains("throttl") || descriptor.contains("ratelimit") || descriptor.contains("rate_limit") { return .rateLimited }
        if descriptor.contains("timeout") || descriptor.contains("timed out") { return .timeout }
        if descriptor.contains("server") || descriptor.contains("internal") || descriptor.contains("unavailable") { return .unavailable }
        return .other
    }

    private var providerDetail: (code: String, message: String)? {
        guard case let .providerFailure(code, message) = self else { return nil }
        return (code, message)
    }

    var code: String {
        switch self {
        case .invalidConfiguration: return "speech_configuration_invalid"
        case .authorizationEnded: return "speech_authorization_ended"
        case .permission: return "speech_authentication_failed"
        case .quota: return "speech_quota_exhausted"
        case .model: return "speech_model_unavailable"
        case .unavailable: return "speech_service_unavailable"
        case .timeout: return "speech_timeout"
        case .rateLimited: return "speech_rate_limited"
        case .invalidAudio: return "speech_audio_invalid"
        case .invalidResponse: return "speech_response_invalid"
        case .responseTooLarge: return "speech_response_too_large"
        case .inputOverflow: return "speech_input_overflow"
        case .outputOverflow: return "speech_output_overflow"
        case .missingSample: return "speech_sample_missing"
        case .providerFailure:
            switch providerCategory {
            case .permission: return "speech_authentication_failed"
            case .quota: return "speech_quota_exhausted"
            case .model: return "speech_model_unavailable"
            case .unavailable: return "speech_service_unavailable"
            case .timeout: return "speech_timeout"
            case .rateLimited: return "speech_rate_limited"
            case .other: return "speech_provider_rejected"
            }
        }
    }

    var errorDescription: String? {
        switch self {
        case .invalidConfiguration: return "语音服务配置无效，请检查服务地址、密钥和用途。"
        case .authorizationEnded: return "语音上传授权已撤回或到期。"
        case .permission: return "语音服务拒绝鉴权，请检查 API Key 和权限。"
        case .quota: return "语音服务额度不足，请检查账号余额及套餐。"
        case .model: return "语音服务不支持当前模型，请检查模型和区域。"
        case .unavailable: return "语音服务暂时不可用，请检查网络或稍后重试。"
        case .timeout: return "等待语音服务超时。"
        case .rateLimited: return "语音服务请求过多，请稍后重试。"
        case .invalidAudio: return "语音服务无法处理这段音频，请检查音频格式、大小或时长。"
        case .invalidResponse: return "语音服务返回的结果不完整或格式无效。"
        case .responseTooLarge: return "语音服务返回的单条结果或文本超过处理上限。"
        case .inputOverflow: return "待转写的音频积压，已停止当前识别。"
        case .outputOverflow: return "转写结果积压，已停止当前识别。"
        case .missingSample: return "缺少测试音频，请检查 App 安装包。"
        case .providerFailure:
            switch providerCategory {
            case .permission: return "语音服务拒绝鉴权，请检查 API Key 和权限。"
            case .quota: return "语音服务额度不足，请检查账号余额及套餐。"
            case .model: return "语音服务不支持当前模型，请检查模型和区域。"
            case .unavailable: return "语音服务暂时不可用，请检查网络或稍后重试。"
            case .timeout: return "等待语音服务超时。"
            case .rateLimited: return "语音服务请求过多，请稍后重试。"
            case .other:
                guard let detail = providerDetail else { return "语音服务拒绝了请求。" }
                let code = detail.code.filter { $0.isNumber || $0.isLetter || $0 == "_" || $0 == "-" }
                return code.isEmpty ? "语音服务拒绝了请求。" : "语音服务拒绝了请求（\(code)）。"
            }
        }
    }

    static func http(_ status: Int) -> Self {
        switch status {
        case 400, 422: return .providerFailure(code: "http_\(status)", message: "服务拒绝请求，未返回结构化错误")
        case 413, 415: return .invalidAudio
        case 401, 403: return .permission
        case 402: return .quota
        case 404: return .model
        case 408, 504: return .timeout
        case 429: return .rateLimited
        case 500...599: return .unavailable
        default: return .invalidResponse
        }
    }

    static func classifiedProviderFailure(code: String, message: String = "") -> Self {
        func clean(_ value: String, limit: Int) -> String {
            String(value.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
                .trimmingCharacters(in: .whitespacesAndNewlines).prefix(limit))
        }
        return .providerFailure(code: clean(code, limit: 96), message: clean(message, limit: 320))
    }

    static func openNetwork(_ error: Error) -> Self {
        if let error = error as? Self { return error }
        return (error as? URLError)?.code == .timedOut ? .timeout : .unavailable
    }
}

// One production route per provider and purpose. The desktop passes a private
// backend snapshot, but the last hop checks the authority again before upload.
extension SpeechSessionConfig {
    func request(kind: String) throws -> URLRequest {
        guard let parts = URLComponents(string: endpoint),
              let url = parts.url, let host = parts.host?.lowercased(),
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              (parts.port == nil || parts.port == 443), !apiKey.isEmpty,
              apiKey.utf8.allSatisfy({ (33...126).contains($0) }) else {
            throw CloudSpeechError.invalidConfiguration
        }
        let valid: Bool
        if provider == "deepseek" {
            valid = kind == "file" && parts.scheme == "https" && host == "api.deepseek.com"
                && parts.percentEncodedPath == "/v1/tools/asr"
        } else if provider == "dashscope" {
            let expectedPath = kind == "live" ? "/api-ws/v1/inference" : "/api/v1/services/aigc/multimodal-generation/generation"
            let officialHost = ["dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"].contains(host)
                || host.range(of: #"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com$"#,
                              options: .regularExpression) != nil
            valid = officialHost && (kind == "live" || kind == "file") && !model.isEmpty
                && parts.scheme == (kind == "live" ? "wss" : "https") && parts.percentEncodedPath == expectedPath
        } else {
            valid = false
        }
        guard valid else { throw CloudSpeechError.invalidConfiguration }
        var requestURL = url
        if provider == "dashscope", kind == "live", var components = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            components.queryItems = [URLQueryItem(name: "model", value: model)]
            requestURL = components.url ?? url
        }
        var request = URLRequest(url: requestURL)
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        return request
    }
}

/// Credentials and audio stay on the approved endpoint. Follow no redirects.
final class SpeechNetworkDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }

    static func ephemeral(requestSeconds: TimeInterval, resourceSeconds: TimeInterval) -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.urlCredentialStorage = nil
        configuration.urlCache = nil
        configuration.timeoutIntervalForRequest = requestSeconds
        configuration.timeoutIntervalForResource = resourceSeconds
        return URLSession(configuration: configuration, delegate: SpeechNetworkDelegate(), delegateQueue: nil)
    }
}
