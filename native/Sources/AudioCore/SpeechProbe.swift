import AVFoundation
import Foundation

public enum SpeechProbe {
    /// Tests the production protocol with only the packaged, synthetic sample.
    @MainActor
    public static func run(config: SpeechSessionConfig, kind: String, sampleURL: URL) async throws -> String {
        guard sampleURL.isFileURL, FileManager.default.isReadableFile(atPath: sampleURL.path) else {
            throw CloudSpeechError.missingSample
        }
        _ = try config.request(kind: kind)
        let decoder: FileAudioDecoder
        do { decoder = try await FileAudioDecoder.open(AVURLAsset(url: sampleURL)) }
        catch is CancellationError { throw CancellationError() }
        catch { throw CloudSpeechError.invalidAudio }
        defer { decoder.cancel() }
        let pcm = SpeechPCM()
        var samples: [Int16] = []
        while let buffer = try decoder.nextBuffer() {
            try samples.append(contentsOf: pcm.samples(buffer))
            guard samples.count <= SpeechPCM.rate * 15 else { throw CloudSpeechError.invalidAudio }
        }
        try samples.append(contentsOf: pcm.drain())
        guard !samples.isEmpty, samples.count <= SpeechPCM.rate * 15 else { throw CloudSpeechError.invalidAudio }
        if kind == "file" {
            let worker = try CloudFileRecognition(config: config, lease: nil, requestSeconds: 22)
            defer { worker.close() }
            let text = try await worker.transcribe(samples)
            guard !text.isEmpty else { throw CloudSpeechError.invalidResponse }
            return text
        }
        let session = try DashScopeRecognition(config: config, lease: nil)
        defer { session.cancel() }
        var finalized: [String: TranscriptChange] = [:]
        session.onChange = { change in
            if change.type == "transcript.final" { finalized[change.segmentID] = change }
            if change.type == "transcript.retracted" { finalized.removeValue(forKey: change.segmentID) }
        }
        var ready = false
        session.onStarted = { ready = true }
        let deadline = ContinuousClock.now.advanced(by: .seconds(5))
        session.start(until: deadline)
        while !ready && ContinuousClock.now < deadline {
            try session.check()
            try await Task.sleep(for: .milliseconds(25))
        }
        guard ready else { try session.check(); throw CloudSpeechError.timeout }
        for start in stride(from: 0, to: samples.count, by: DashScopeRecognition.packetBytes / 2) {
            let end = min(samples.count, start + DashScopeRecognition.packetBytes / 2)
            try session.append(SpeechPCM.bytes(Array(samples[start..<end])))
            try await Task.sleep(for: .milliseconds(100))
        }
        try await session.finish(until: .now.advanced(by: .seconds(4)))
        return finalized.values.sorted { ($0.startMS, $0.endMS, $0.segmentID) < ($1.startMS, $1.endMS, $1.segmentID) }
            .map(\.text).joined(separator: " ")
    }
}
