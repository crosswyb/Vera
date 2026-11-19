// Modern, local-only subset adapted from Cicada Recognition.swift at
// 31574edbd43486eb5c4b958f82b503a63bb4697d. Apache-2.0; see third_party/cicada.
import AVFoundation
import CoreMedia
import Speech

public enum LocalSpeechModels {
    static func transcriber(_ locale: String) -> SpeechTranscriber {
        SpeechTranscriber(locale: Locale(identifier: locale), transcriptionOptions: [],
                          // Keep live previews without the smaller context window
                          // and accuracy tradeoff imposed by fastResults.
                          reportingOptions: [.volatileResults], attributeOptions: [])
    }
    public static func status(locale: String) async -> String {
        guard SpeechTranscriber.isAvailable else { return "unsupported" }
        switch await AssetInventory.status(forModules: [transcriber(locale)]) {
        case .installed: return "installed"
        case .unsupported: return "unsupported"
        case .downloading: return "downloading"
        case .supported: return "downloadable"
        @unknown default: return "unknown"
        }
    }
    /// Called only by an explicit install_model command. Capture never calls this.
    public static func install(locale: String) async throws {
        let module = transcriber(locale)
        guard await AssetInventory.status(forModules: [module]) != .unsupported else {
            throw AudioCoreError.modelUnavailable("unsupported")
        }
        if let request = try await AssetInventory.assetInstallationRequest(supporting: [module]) {
            try await request.downloadAndInstall()
        }
    }
}

final class LocalRecognition: @unchecked Sendable {
    let format: AVAudioFormat
    let results: AsyncThrowingStream<SpeechPiece, Error>
    private let analyzer: SpeechAnalyzer
    private let input: AsyncStream<AnalyzerInput>.Continuation
    private var reader: Task<Void, Never>?

    static func start(locale: String) async throws -> LocalRecognition {
        let module = LocalSpeechModels.transcriber(locale)
        guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [module]) else {
            throw AudioCoreError.invalidConfig("No compatible speech analyzer format.")
        }
        let analyzer = SpeechAnalyzer(modules: [module])
        try await analyzer.prepareToAnalyze(in: format)
        let (stream, continuation) = AsyncStream.makeStream(of: AnalyzerInput.self, bufferingPolicy: .bufferingOldest(128))
        let recognizer = LocalRecognition(module: module, analyzer: analyzer, format: format, input: continuation)
        do { try await analyzer.start(inputSequence: stream) }
        catch { await recognizer.cancel(); throw error }
        return recognizer
    }

    private init(module: SpeechTranscriber, analyzer: SpeechAnalyzer, format: AVAudioFormat,
                 input: AsyncStream<AnalyzerInput>.Continuation) {
        self.analyzer = analyzer; self.format = format; self.input = input
        let (results, output) = AsyncThrowingStream.makeStream(of: SpeechPiece.self, bufferingPolicy: .bufferingOldest(256))
        self.results = results
        reader = Task {
            do {
                for try await result in module.results {
                    let start = CMTimeGetSeconds(result.range.start)
                    let end = CMTimeGetSeconds(result.range.end)
                    guard start.isFinite, end.isFinite, start >= 0, end >= start else {
                        output.finish(throwing: TranscriptReducerError.invalidRange); return
                    }
                    let piece = SpeechPiece(text: String(result.text.characters), isFinal: result.isFinal,
                                            startMS: Int((start * 1000).rounded()), endMS: Int((end * 1000).rounded()))
                    switch output.yield(piece) {
                    case .dropped:
                        output.finish(throwing: AudioCoreError.outputOverflow); return
                    case .terminated: return
                    case .enqueued: break
                    @unknown default: break
                    }
                }
                output.finish()
            } catch { output.finish(throwing: error) }
        }
    }

    func append(_ buffer: AVAudioPCMBuffer) throws {
        switch input.yield(AnalyzerInput(buffer: buffer)) {
        case .dropped: throw AudioCoreError.inputOverflow
        case .terminated: throw AudioCoreError.invalidConfig("Speech input already closed.")
        case .enqueued: break
        @unknown default: break
        }
    }
    func finalize(through seconds: Double) async throws {
        try await analyzer.finalize(through: CMTime(seconds: seconds, preferredTimescale: 1000))
    }
    func finish() async throws {
        input.finish()
        try await analyzer.finalizeAndFinishThroughEndOfInput()
        await reader?.value
    }
    func cancel() async {
        input.finish()
        await analyzer.cancelAndFinishNow()
        reader?.cancel()
        await reader?.value
    }
}
