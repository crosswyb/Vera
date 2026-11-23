import AVFoundation
import Foundation

struct SpeechPCM {
    static let rate = 16_000
    let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: Double(rate), channels: 1, interleaved: false)!
    private let converter = PCMConverter()

    func samples(_ input: AVAudioPCMBuffer) throws -> [Int16] {
        let mono: AVAudioPCMBuffer
        if input.format.channelCount == 1 {
            mono = input
        } else {
            // Decode file input as Float32. Explicitly include all channels,
            // since some services only read the left side of stereo WAVs.
            guard let floatSamples = input.floatChannelData,
                  let format = AVAudioFormat(standardFormatWithSampleRate: input.format.sampleRate, channels: 1),
                  let mixed = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: input.frameLength),
                  let output = mixed.floatChannelData?[0] else {
                throw AudioCoreError.conversionFailed
            }
            mixed.frameLength = input.frameLength
            let channels = Int(input.format.channelCount)
            for frame in 0..<Int(input.frameLength) {
                var total: Float = 0
                for channel in 0..<channels {
                    total += input.format.isInterleaved ? floatSamples[0][frame * channels + channel] : floatSamples[channel][frame]
                }
                output[frame] = total / Float(channels)
            }
            mono = mixed
        }
        guard let converted = converter.convert(mono, to: format, allowEmpty: true), let samples = converted.int16ChannelData?[0] else {
            throw AudioCoreError.conversionFailed
        }
        return Array(UnsafeBufferPointer(start: samples, count: Int(converted.frameLength)))
    }

    func drain() throws -> [Int16] {
        var samples: [Int16] = []
        try converter.drain { output in
            if let channel = output.int16ChannelData?[0] {
                samples.append(contentsOf: UnsafeBufferPointer(start: channel, count: Int(output.frameLength)))
            }
        }
        return samples
    }

    static func bytes(_ samples: [Int16]) -> Data {
        // AudioCore is supported on little-endian Apple Silicon.
        samples.withUnsafeBytes { Data($0) }
    }

    static func wav(_ samples: [Int16]) -> Data {
        var data = Data("RIFF".utf8)
        func append<T: FixedWidthInteger>(_ value: T) {
            var littleEndian = value.littleEndian
            withUnsafeBytes(of: &littleEndian) { data.append(contentsOf: $0) }
        }
        let bytes = UInt32(samples.count * 2)
        append(bytes + 36)
        data.append(contentsOf: "WAVEfmt ".utf8)
        append(UInt32(16))
        append(UInt16(1))
        append(UInt16(1))
        append(UInt32(rate))
        append(UInt32(rate * 2))
        append(UInt16(2))
        append(UInt16(16))
        data.append(contentsOf: "data".utf8)
        append(bytes)
        data.append(Self.bytes(samples))
        return data
    }
}

/// Consumes a bounded decoded stream. Boundaries use PCM frames, so adjacent
/// requests cover exactly adjacent ranges without depending on vendor timing.
struct CloudAudioChunker {
    let earliest: Int
    let latest: Int
    let pause: Int
    private let expectedFrames: Int?
    private let tailWindow: Int
    private var samples: [Int16] = []
    private var quietFrames = 0
    private(set) var sentFrames = 0

    init(rate: Int = SpeechPCM.rate, minimumSeconds: Int = 20, maximumSeconds: Int = 60,
         expectedFrames: Int? = nil) {
        earliest = minimumSeconds * rate
        latest = maximumSeconds * rate
        pause = rate * 8 / 10
        self.expectedFrames = expectedFrames
        tailWindow = rate * 2
    }

    mutating func next(_ buffer: [Int16], from offset: inout Int) -> [Int16]? {
        let start = offset
        while offset < buffer.count {
            let quiet = abs(Int(buffer[offset])) < 160
            quietFrames = quiet ? quietFrames + 1 : 0
            offset += 1
            let count = samples.count + offset - start
            if count >= latest || (count >= earliest && quietFrames >= pause) {
                // Keep a short tail with the previous audio. DashScope can
                // reject an isolated tail even though the preceding slice worked.
                if let expectedFrames, abs(expectedFrames - sentFrames - count) <= tailWindow { continue }
                samples.append(contentsOf: buffer[start..<offset])
                return finish()
            }
        }
        samples.append(contentsOf: buffer[start..<offset])
        return nil
    }

    mutating func finish() -> [Int16]? {
        guard !samples.isEmpty else { return nil }
        let chunk = samples
        sentFrames += chunk.count
        samples = []
        quietFrames = 0
        return chunk
    }
}

struct CloudFileRequest {
    static func providerError(from data: Data) -> CloudSpeechError? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        let code = (object["code"] as? String)
            ?? (object["error_code"] as? String)
            ?? (object["error"] as? String)
            ?? ""
        let message = (object["message"] as? String)
            ?? (object["error_message"] as? String)
            ?? (object["error_description"] as? String)
            ?? ""
        guard !code.isEmpty || !message.isEmpty else { return nil }
        return .providerFailure(code: code, message: message)
    }

    static func build(config: SpeechSessionConfig, pcm: [Int16]) throws -> URLRequest {
        // DashScope Flash permits five minutes. Keep inline WAV/base64 below
        // the stricter 10 MB input limit, including older qwen3-asr variants.
        let maximumSeconds = config.provider == "dashscope" ? 180 : 120
        guard !pcm.isEmpty, pcm.count <= maximumSeconds * SpeechPCM.rate else { throw CloudSpeechError.invalidAudio }
        let wav = SpeechPCM.wav(pcm)
        var request = try config.request(kind: "file")
        request.httpMethod = "POST"
        if config.provider == "deepseek" {
            guard wav.count <= 8 * 1024 * 1024 else {
                throw CloudSpeechError.invalidAudio
            }
            let boundary = "ZSJH-" + UUID().uuidString
            request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
            // Use a constant filename so the user’s local path is never transmitted.
            var body = Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"segment.wav\"\r\nContent-Type: audio/wav\r\n\r\n".utf8)
            body.append(wav)
            body.append(contentsOf: "\r\n--\(boundary)--\r\n".utf8)
            request.httpBody = body
        } else {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue("enable", forHTTPHeaderField: "X-DashScope-SSE")
            let content = ["type": "input_audio", "input_audio": ["data": "data:audio/wav;base64," + wav.base64EncodedString()]] as [String: Any]
            let body = try JSONSerialization.data(withJSONObject: [
                "model": config.model,
                "input": ["messages": [["role": "user", "content": [content]]]],
                "parameters": ["format": "wav", "sample_rate": "16000"],
            ])
            guard body.count <= 10_000_000 else { throw CloudSpeechError.invalidAudio }
            request.httpBody = body
        }
        return request
    }

    static func text(from data: Data, provider: String) throws -> String {
        var decoder = CloudFileResponse(provider: provider)
        for byte in data { try decoder.append(byte) }
        return try decoder.finish()
    }
}

/// DashScope repeats cumulative text and sentence metadata in each SSE event.
/// Bound one event and the final text, never the sum of the entire stream.
struct CloudFileResponse {
    static let maximumEventBytes = 2 * 1024 * 1024
    static let maximumTextBytes = 2 * 1024 * 1024

    private enum Format { case unknown, json, events }
    private let provider: String
    private var format = Format.unknown
    private var buffer = Data()
    private var eventData = Data()
    private var eventBytes = 0
    private var afterCR = false
    private var done = false
    private var latest: String?

    init(provider: String) { self.provider = provider }

    mutating func append(_ byte: UInt8) throws {
        switch format {
        case .unknown:
            if buffer.isEmpty && [9, 10, 13, 32].contains(byte) { return }
            buffer.append(byte)
            // A UTF-8 BOM can itself span network reads.
            if buffer.first == 0xef && buffer.count < 3 { return }
            if buffer == Data([0xef, 0xbb, 0xbf]) { buffer.removeAll(); return }
            if buffer.first == 0x7b || buffer.first == 0x5b {
                format = .json
            } else {
                format = .events
                let prefix = buffer
                buffer.removeAll()
                for value in prefix { try appendEventByte(value) }
            }
        case .json:
            guard buffer.count < Self.maximumEventBytes else { throw CloudSpeechError.responseTooLarge }
            buffer.append(byte)
        case .events:
            try appendEventByte(byte)
        }
    }

    mutating func finish() throws -> String {
        switch format {
        case .json: try decode(buffer)
        case .events:
            // An unfinished event cannot acknowledge an audio chunk, even if
            // earlier cumulative text was valid. Keep its resume point intact.
            guard buffer.isEmpty && eventData.isEmpty else { throw CloudSpeechError.invalidResponse }
        case .unknown: throw CloudSpeechError.invalidResponse
        }
        guard let latest else { throw CloudSpeechError.invalidResponse }
        return latest.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private mutating func appendEventByte(_ byte: UInt8) throws {
        if afterCR {
            afterCR = false
            if byte == 10 { return }
        }
        if byte == 10 || byte == 13 {
            try finishLine()
            afterCR = byte == 13
        } else {
            guard eventBytes < Self.maximumEventBytes else { throw CloudSpeechError.responseTooLarge }
            eventBytes += 1
            buffer.append(byte)
        }
    }

    private mutating func finishLine() throws {
        if buffer.isEmpty {
            if !eventData.isEmpty {
                guard !done else { throw CloudSpeechError.invalidResponse }
                if eventData == Data("[DONE]".utf8) { done = true }
                else { try decode(eventData) }
                eventData.removeAll(keepingCapacity: true)
            }
            eventBytes = 0
            return
        }
        guard let line = String(data: buffer, encoding: .utf8) else { throw CloudSpeechError.invalidResponse }
        buffer.removeAll(keepingCapacity: true)
        let fields = line.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
        guard fields.first == "data" else { return }
        var value = fields.count == 2 ? fields[1] : Substring()
        if value.first == " " { value = value.dropFirst() }
        if !eventData.isEmpty { eventData.append(10) }
        eventData.append(contentsOf: value.utf8)
        guard eventData.count <= Self.maximumEventBytes else { throw CloudSpeechError.responseTooLarge }
    }

    private mutating func decode(_ payload: Data) throws {
        struct Response: Decodable {
            struct Output: Decodable { let text: String? }
            let output: Output?
            let text: String?
            let code: String?
            let message: String?
        }
        guard let response = try? JSONDecoder().decode(Response.self, from: payload) else {
            throw CloudSpeechError.invalidResponse
        }
        if let code = response.code, !code.isEmpty {
            throw CloudSpeechError.classifiedProviderFailure(code: code, message: response.message ?? "")
        }
        if let failure = CloudFileRequest.providerError(from: payload) { throw failure }
        if let text = provider == "deepseek" ? response.text : response.output?.text {
            guard text.utf8.count <= Self.maximumTextBytes else { throw CloudSpeechError.responseTooLarge }
            latest = text
        }
    }
}

@MainActor
final class CloudFileRecognition {
    typealias Transport = (URLRequest) async throws -> (Data, URLResponse)

    private let config: SpeechSessionConfig
    private let lease: SpeechLease?
    private let session: URLSession
    private let transport: Transport?
    private var leaseHandler: UUID?
    private var cancelled = false

    init(config: SpeechSessionConfig, lease: SpeechLease?, requestSeconds: TimeInterval = 120,
         transport: Transport? = nil, session: URLSession? = nil) throws {
        _ = try config.request(kind: "file")
        try lease?.check()
        self.config = config
        self.lease = lease
        self.session = session ?? SpeechNetworkDelegate.ephemeral(requestSeconds: requestSeconds, resourceSeconds: requestSeconds)
        self.transport = transport
        leaseHandler = lease?.onRevoke { [weak self] in self?.cancel() }
    }

    func cancel() {
        cancelled = true
        session.invalidateAndCancel()
    }

    func close() {
        lease?.removeHandler(leaseHandler)
        cancel()
    }

    func transcribe(_ samples: [Int16]) async throws -> String {
        try check()
        let outgoing = try CloudFileRequest.build(config: config, pcm: samples)
        for attempt in 0...1 {
            try check()
            let response: HTTPURLResponse
            let text: String
            do { (text, response) = try await receive(outgoing) }
            catch {
                try check()
                // A file import can replay this chunk without publishing any
                // partial text. Keep the settings probe to a single transfer.
                if attempt == 0, config.provider == "dashscope", lease != nil,
                   let code = (error as? URLError)?.code,
                   code == .timedOut || code == .networkConnectionLost {
                    try await Task.sleep(for: .seconds(1))
                    continue
                }
                throw CloudSpeechError.openNetwork(error)
            }
            try check()
            if response.statusCode == 429 && attempt == 0 {
                let delay = DashScopeRecognition.retryAfter(response) ?? 1
                if delay <= 5 {
                    try await Task.sleep(for: .seconds(delay))
                    continue
                }
            }
            guard (200..<300).contains(response.statusCode) else { throw CloudSpeechError.http(response.statusCode) }
            return text
        }
        throw CloudSpeechError.rateLimited
    }

    private func receive(_ outgoing: URLRequest) async throws -> (String, HTTPURLResponse) {
        if let transport {
            let (data, received) = try await transport(outgoing)
            try check()
            guard let response = received as? HTTPURLResponse else { throw CloudSpeechError.invalidResponse }
            if response.statusCode == 429 { return ("", response) }
            guard (200..<300).contains(response.statusCode) else {
                if let error = CloudFileRequest.providerError(from: Data(data.prefix(CloudFileResponse.maximumEventBytes))) { throw error }
                throw CloudSpeechError.http(response.statusCode)
            }
            return (try CloudFileRequest.text(from: data, provider: config.provider), response)
        }
        let (bytes, received) = try await session.bytes(for: outgoing)
        // Each concurrent request owns its task. Parse failure or cancellation
        // closes only this response, while lease revocation closes the session.
        defer { bytes.task.cancel() }
        guard let response = received as? HTTPURLResponse else { throw CloudSpeechError.invalidResponse }
        if response.statusCode == 429 { return ("", response) }
        let text = try await Self.read(bytes, response: response, provider: config.provider)
        return (text, response)
    }

    // Keep byte iteration off the main actor: AsyncBytes.next() must not cause
    // an executor hop for every byte of each concurrent response.
    private nonisolated static func read(_ bytes: URLSession.AsyncBytes, response: HTTPURLResponse,
                                         provider: String) async throws -> String {
        return try await withTaskCancellationHandler {
            var decoder = CloudFileResponse(provider: provider)
            var failureBody = Data()
            let success = (200..<300).contains(response.statusCode)
            for try await byte in bytes {
                try Task.checkCancellation()
                if success { try decoder.append(byte) }
                else {
                    guard failureBody.count < CloudFileResponse.maximumEventBytes else { throw CloudSpeechError.http(response.statusCode) }
                    failureBody.append(byte)
                }
            }
            try Task.checkCancellation()
            guard success else {
                if let error = CloudFileRequest.providerError(from: failureBody) { throw error }
                throw CloudSpeechError.http(response.statusCode)
            }
            return try decoder.finish()
        } onCancel: {
            bytes.task.cancel()
        }
    }

    private func check() throws {
        try lease?.check()
        try Task.checkCancellation()
        guard !cancelled else { throw CancellationError() }
    }
}
