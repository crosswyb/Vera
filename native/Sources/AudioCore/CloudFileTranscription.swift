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
    private var samples: [Int16] = []
    private var quietFrames = 0
    private(set) var sentFrames = 0

    init(rate: Int = SpeechPCM.rate, minimumSeconds: Int = 20, maximumSeconds: Int = 60) {
        earliest = minimumSeconds * rate
        latest = maximumSeconds * rate
        pause = rate * 8 / 10
    }

    mutating func next(_ buffer: [Int16], from offset: inout Int) -> [Int16]? {
        let start = offset
        while offset < buffer.count {
            let quiet = abs(Int(buffer[offset])) < 160
            quietFrames = quiet ? quietFrames + 1 : 0
            offset += 1
            let count = samples.count + offset - start
            if count >= latest || (count >= earliest && quietFrames >= pause) {
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
        guard !pcm.isEmpty, pcm.count <= 120 * SpeechPCM.rate else { throw CloudSpeechError.invalidAudio }
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
            request.httpBody = try JSONSerialization.data(withJSONObject: [
                "model": config.model,
                "input": ["messages": [["role": "user", "content": [content]]]],
                "parameters": ["format": "wav", "sample_rate": "16000"],
            ])
        }
        return request
    }

    static func text(from data: Data, provider: String) throws -> String {
        struct Response: Decodable {
            struct Output: Decodable { let text: String? }
            let output: Output?
            let text: String?
            let code: String?
            let message: String?
        }

        func decode(_ payload: Data) throws -> String? {
            guard let response = try? JSONDecoder().decode(Response.self, from: payload) else { return nil }
            if let code = response.code, !code.isEmpty {
                throw CloudSpeechError.classifiedProviderFailure(code: code, message: response.message ?? "")
            }
            return provider == "deepseek" ? response.text : response.output?.text
        }

        if let fullText = try decode(data) {
            return fullText.trimmingCharacters(in: .whitespacesAndNewlines)
        }

        // Bailian's current recorded-ASR example returns cumulative output.text
        // as Server-Sent Events. URLSession collects the stream into one Data.
        var latest: String?
        for line in String(decoding: data, as: UTF8.self).split(whereSeparator: \.isNewline) {
            let value = line.trimmingCharacters(in: .whitespacesAndNewlines)
            guard value.hasPrefix("data:") else { continue }
            let payload = String(value.dropFirst(5)).trimmingCharacters(in: .whitespaces)
            if payload == "[DONE]" { continue }
            guard let event = payload.data(using: .utf8) else { continue }
            if let text = try decode(event) { latest = text }
            else if let failure = CloudFileRequest.providerError(from: event) { throw failure }
        }
        guard let latest else { throw CloudSpeechError.invalidResponse }
        return latest.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

@MainActor
final class CloudFileRecognition {
    typealias Transport = (URLRequest) async throws -> (Data, URLResponse)

    private let config: SpeechSessionConfig
    private let lease: SpeechLease?
    private let session: URLSession
    private let request: Transport
    private var leaseHandler: UUID?
    private var cancelled = false

    init(config: SpeechSessionConfig, lease: SpeechLease?, requestSeconds: TimeInterval = 120,
         transport: Transport? = nil) throws {
        _ = try config.request(kind: "file")
        try lease?.check()
        self.config = config
        self.lease = lease
        let session = SpeechNetworkDelegate.ephemeral(requestSeconds: requestSeconds, resourceSeconds: requestSeconds)
        self.session = session
        request = transport ?? { try await session.data(for: $0) }
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
            let data: Data
            let received: URLResponse
            do { (data, received) = try await request(outgoing) }
            catch {
                try check()
                throw CloudSpeechError.openNetwork(error)
            }
            try check()
            guard let response = received as? HTTPURLResponse else { throw CloudSpeechError.invalidResponse }
            if response.statusCode == 429 && attempt == 0 {
                let delay = DashScopeRecognition.retryAfter(response) ?? 1
                if delay <= 5 {
                    try await Task.sleep(for: .seconds(delay))
                    continue
                }
            }
            guard (200..<300).contains(response.statusCode) else {
                if let error = CloudFileRequest.providerError(from: data) { throw error }
                throw CloudSpeechError.http(response.statusCode)
            }
            guard data.count <= 2 * 1024 * 1024 else { throw CloudSpeechError.invalidResponse }
            return try CloudFileRequest.text(from: data, provider: config.provider)
        }
        throw CloudSpeechError.rateLimited
    }

    private func check() throws {
        try lease?.check()
        try Task.checkCancellation()
        guard !cancelled else { throw CancellationError() }
    }
}
