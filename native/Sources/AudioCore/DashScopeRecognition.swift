import Foundation

/// Fun-ASR WebSocket events carry a stable sentence ID across time corrections.
struct DashScopeEvent: Decodable {
    struct Header: Decodable {
        let taskID: String
        let event: String
        let errorCode: String?
        let errorMessage: String?
        enum CodingKeys: String, CodingKey {
            case event, taskID = "task_id", errorCode = "error_code", errorMessage = "error_message"
        }
    }

    struct Sentence: Decodable {
        let id: Int?
        let begin: Int?
        let end: Int?
        let text: String?
        let final: Bool?
        let heartbeat: Bool?
        enum CodingKeys: String, CodingKey {
            case text, heartbeat
            case id = "sentence_id", begin = "begin_time", end = "end_time", final = "sentence_end"
        }
    }

    struct Payload: Decodable {
        struct Output: Decodable { let sentence: Sentence? }
        let output: Output?
    }

    let header: Header
    let payload: Payload?
}

private extension DashScopeEvent {
    static func providerFailure(from data: Data) -> CloudSpeechError? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        let code = (object["code"] as? String) ?? (object["error_code"] as? String) ?? ""
        let message = (object["message"] as? String) ?? (object["error_message"] as? String) ?? ""
        guard !code.isEmpty || !message.isEmpty else { return nil }
        return .classifiedProviderFailure(code: code, message: message)
    }
}

struct DashScopeSentences {
    private var changesByID: [Int: TranscriptChange] = [:]
    private var finalizedThrough = 0
    private var openIDs: Set<Int> = []
    var hasOpenSentence: Bool { !openIDs.isEmpty }

    mutating func apply(_ sentence: DashScopeEvent.Sentence, sentMS: Int) throws -> TranscriptChange? {
        if sentence.heartbeat == true { return nil }
        guard let id = sentence.id, id > 0, let final = sentence.final,
              let begin = sentence.begin, begin >= 0, begin <= sentMS, let rawText = sentence.text else {
            throw CloudSpeechError.invalidResponse
        }
        guard id > finalizedThrough else { return nil }
        let prior = changesByID[id]
        if prior?.type == "transcript.final" && !final { return nil }
        // Unknown interim end times are never carried forward as final times.
        let end: Int
        if let value = sentence.end, value >= begin, value <= sentMS + 250 {
            end = min(value, sentMS)
        } else if !final {
            end = sentMS
        } else {
            throw CloudSpeechError.invalidResponse
        }
        let text = String(String.UnicodeScalarView(rawText.trimmingCharacters(in: .whitespacesAndNewlines)
            .unicodeScalars.prefix(4500)))
        if final { openIDs.remove(id) } else { openIDs.insert(id) }
        guard openIDs.count <= 256 else { throw CloudSpeechError.outputOverflow }
        if text.isEmpty && prior == nil { return nil }
        let type = text.isEmpty ? "transcript.retracted" : final ? "transcript.final" : "transcript.interim"
        if let prior, prior.type == type, prior.text == text, prior.startMS == begin, prior.endMS == end { return nil }
        let result = TranscriptChange(type: type, segmentID: "ds\(id)", revision: (prior?.revision ?? 0) + 1,
                                      supersedesRevision: prior?.revision, text: text, startMS: begin, endMS: end)
        changesByID[id] = result
        if changesByID.count > 256,
           let oldest = changesByID.keys.min(), changesByID[oldest]?.type != "transcript.interim" {
            finalizedThrough = oldest
            changesByID.removeValue(forKey: oldest)
        }
        guard changesByID.count <= 256 else { throw CloudSpeechError.outputOverflow }
        return result
    }

    mutating func retractOpen() -> [TranscriptChange] {
        openIDs.removeAll()
        let open = changesByID.filter { $0.value.type == "transcript.interim" }.sorted { $0.key < $1.key }
        return open.map { id, value in
            changesByID.removeValue(forKey: id)
            return TranscriptChange(type: "transcript.retracted", segmentID: value.segmentID,
                                    revision: value.revision + 1, supersedesRevision: value.revision,
                                    text: "", startMS: value.startMS, endMS: value.endMS)
        }
    }
}

@MainActor
protocol DashScopeSocket: AnyObject {
    var response: HTTPURLResponse? { get }
    func send(_ message: URLSessionWebSocketTask.Message) async throws
    func receive() async throws -> URLSessionWebSocketTask.Message
    func close()
}

@MainActor
private final class NativeDashScopeSocket: DashScopeSocket {
    private let session: URLSession
    private let task: URLSessionWebSocketTask

    init(request: URLRequest) {
        session = SpeechNetworkDelegate.ephemeral(requestSeconds: 15, resourceSeconds: 8 * 3600)
        task = session.webSocketTask(with: request)
        task.maximumMessageSize = 512 * 1024
        task.resume()
    }

    var response: HTTPURLResponse? { task.response as? HTTPURLResponse }
    func send(_ message: URLSessionWebSocketTask.Message) async throws { try await task.send(message) }
    func receive() async throws -> URLSessionWebSocketTask.Message { try await task.receive() }
    func close() {
        task.cancel(with: .normalClosure, reason: nil)
        session.invalidateAndCancel()
    }
}

/// One independent task per audio source. All control and queue mutations run
/// on the main actor. The device callback and recorder never await this object.
@MainActor
final class DashScopeRecognition {
    static let sampleRate = 16_000
    static let packetBytes = 3200 // 100 ms of mono Int16 PCM
    private let taskID = UUID().uuidString.lowercased()
    private let model: String
    private let socket: any DashScopeSocket
    private let lease: SpeechLease?
    private var leaseHandler: UUID?
    private var reader: Task<Void, Never>?
    private var pump: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var pending = Data()
    private var offeredFrames = 0
    private var sentences = DashScopeSentences()
    private var started = false
    private var inputFinished = false
    private var finishSent = false
    private var completion: Result<Void, CloudSpeechError>?
    private var completionWaiter: CheckedContinuation<Result<Void, CloudSpeechError>, Never>?
    private(set) var retryAfterSeconds: Double?

    var onStarted: (() -> Void)?
    var onChange: ((TranscriptChange) -> Void)?
    var onFailure: ((CloudSpeechError) -> Void)?

    init(config: SpeechSessionConfig, lease: SpeechLease?,
         makeSocket: ((URLRequest) -> any DashScopeSocket)? = nil) throws {
        try lease?.check()
        model = config.model
        self.lease = lease
        let request = try config.request(kind: "live")
        socket = makeSocket?(request) ?? NativeDashScopeSocket(request: request)
    }

    func start(until deadline: ContinuousClock.Instant) {
        guard reader == nil, completion == nil else { return }
        leaseHandler = lease?.onRevoke { [weak self] in self?.fail(.authorizationEnded) }
        guard completion == nil else { return }
        setDeadline(deadline)
        reader = Task { [weak self] in
            guard let self else { return }
            do {
                try self.lease?.check()
                try await self.socket.send(.string(self.command("run-task")))
                while self.completion == nil {
                    let message = try await self.socket.receive()
                    guard self.completion == nil else { return }
                    try self.lease?.check()
                    try self.handle(message)
                }
            } catch {
                self.fail(self.networkError(error))
            }
        }
    }

    func check() throws {
        try completion?.get()
    }

    func append(_ pcm: Data) throws {
        try check()
        guard completion == nil, !finishSent, pcm.count.isMultiple(of: 2) else { throw CloudSpeechError.invalidAudio }
        try lease?.check()
        guard pending.count + pcm.count <= Self.sampleRate * 2 * 5 else { throw CloudSpeechError.inputOverflow }
        pending.append(pcm)
        sendPending()
    }

    func finish(until deadline: ContinuousClock.Instant) async throws {
        if let completion { return try completion.get() }
        inputFinished = true
        setDeadline(deadline)
        sendPending()
        let result: Result<Void, CloudSpeechError> = await withTaskCancellationHandler {
            await withCheckedContinuation { completionWaiter = $0 }
        } onCancel: { [weak self] in
            Task { @MainActor in self?.cancel() }
        }
        try result.get()
    }

    func cancel() { fail(.unavailable) }

    private func setDeadline(_ deadline: ContinuousClock.Instant) {
        timeout?.cancel()
        timeout = Task { [weak self] in
            do { try await ContinuousClock().sleep(until: deadline) } catch { return }
            self?.fail(.timeout)
        }
    }

    private func command(_ action: String) throws -> String {
        var payload: [String: Any] = ["input": [:]]
        if action == "run-task" {
            payload["task_group"] = "audio"
            payload["task"] = "asr"
            payload["function"] = "recognition"
            payload["model"] = model
            payload["parameters"] = ["format": "pcm", "sample_rate": Self.sampleRate, "heartbeat": true]
        }
        let header = ["action": action, "task_id": taskID, "streaming": "duplex"]
        let data = try JSONSerialization.data(withJSONObject: ["header": header, "payload": payload])
        return String(decoding: data, as: UTF8.self)
    }

    private func handle(_ message: URLSessionWebSocketTask.Message) throws {
        let data: Data
        switch message {
        case .string(let text): data = Data(text.utf8)
        case .data(let bytes): data = bytes
        @unknown default: throw CloudSpeechError.invalidResponse
        }
        guard data.count <= 512 * 1024 else { throw CloudSpeechError.invalidResponse }
        guard let event = try? JSONDecoder().decode(DashScopeEvent.self, from: data) else {
            if let failure = DashScopeEvent.providerFailure(from: data) { throw failure }
            throw CloudSpeechError.invalidResponse
        }
        guard event.header.taskID.lowercased() == taskID else { return }
        switch event.header.event {
        case "task-started":
            guard !started else { return }
            started = true
            if !inputFinished { timeout?.cancel() }
            onStarted?()
            sendPending()
        case "result-generated":
            guard started, let sentence = event.payload?.output?.sentence else { throw CloudSpeechError.invalidResponse }
            if let change = try sentences.apply(sentence, sentMS: offeredFrames * 1000 / Self.sampleRate) {
                onChange?(change)
            }
        case "task-finished":
            guard finishSent, !sentences.hasOpenSentence else { throw CloudSpeechError.invalidResponse }
            complete(.success(()))
        case "task-failed":
            throw CloudSpeechError.classifiedProviderFailure(code: event.header.errorCode ?? "", message: event.header.errorMessage ?? "")
        default: break
        }
    }

    private func sendPending() {
        guard started, completion == nil, pump == nil else { return }
        pump = Task { [weak self] in
            guard let self else { return }
            defer { self.pump = nil }
            do {
                while self.completion == nil {
                    try self.lease?.check()
                    if self.pending.count >= Self.packetBytes || (self.inputFinished && !self.pending.isEmpty) {
                        let packet = Data(self.pending.prefix(Self.packetBytes))
                        self.pending.removeFirst(packet.count)
                        self.offeredFrames += packet.count / 2
                        try await self.socket.send(.data(packet))
                    } else if self.inputFinished && !self.finishSent {
                        self.finishSent = true
                        try await self.socket.send(.string(self.command("finish-task")))
                        return
                    } else { return }
                }
            } catch {
                self.fail(self.networkError(error))
            }
        }
    }

    private func networkError(_ error: Error) -> CloudSpeechError {
        if let known = error as? CloudSpeechError { return known }
        if let response = socket.response, response.statusCode != 101 {
            retryAfterSeconds = Self.retryAfter(response)
            return CloudSpeechError.http(response.statusCode)
        }
        return CloudSpeechError.openNetwork(error)
    }

    static func retryAfter(_ response: HTTPURLResponse) -> Double? {
        guard let value = response.value(forHTTPHeaderField: "Retry-After") else { return nil }
        if let seconds = Double(value), seconds.isFinite { return max(0, seconds) }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "EEE, dd MMM yyyy HH:mm:ss z"
        return formatter.date(from: value).map { max(0, $0.timeIntervalSinceNow) }
    }

    private func fail(_ error: CloudSpeechError) {
        guard completion == nil else { return }
        for change in sentences.retractOpen() { onChange?(change) }
        complete(.failure(error))
        onFailure?(error)
    }

    private func complete(_ result: Result<Void, CloudSpeechError>) {
        guard completion == nil else { return }
        completion = result
        timeout?.cancel()
        reader?.cancel()
        pump?.cancel()
        socket.close()
        pending.removeAll()
        lease?.removeHandler(leaseHandler)
        completionWaiter?.resume(returning: result)
        completionWaiter = nil
    }
}
