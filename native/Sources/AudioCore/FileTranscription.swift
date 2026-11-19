import AVFoundation
import CoreMedia
import Foundation
import Speech

/// File input uses Apple's file-analysis API. It never starts a capture device,
/// plays the recording, uploads audio, or copies the user's original file.
public struct AudioFileInfo: Sendable {
    public static let extensions = ["wav", "m4a", "mp3", "aiff", "aif", "caf", "flac"]
    public let name: String
    public let durationMS: Int
    public let bytes: Int64

    public static func inspect(_ url: URL) async throws -> AudioFileInfo {
        guard url.isFileURL, extensions.contains(url.pathExtension.lowercased()) else {
            throw AudioCoreError.invalidConfig("请选择 WAV、M4A、MP3、AIFF、CAF 或 FLAC 音频。")
        }
        let values = try url.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])
        guard values.isRegularFile == true, let size = values.fileSize, size > 0, size <= 2_000_000_000 else {
            throw AudioCoreError.invalidConfig("请选择不超过 2 GB 的有效音频文件。")
        }
        let asset = AVURLAsset(url: url, options: [AVURLAssetPreferPreciseDurationAndTimingKey: true])
        let duration: Double
        do { duration = CMTimeGetSeconds(try await asset.load(.duration)) }
        catch is CancellationError { throw CancellationError() }
        catch { throw AudioCoreError.invalidConfig("无法读取这个音频，请检查文件是否完整。") }
        guard duration.isFinite, duration > 0 else {
            throw AudioCoreError.invalidConfig("无法读取有效音频时长，请检查文件是否完整。")
        }
        guard duration <= 6 * 3600 else {
            throw AudioCoreError.invalidConfig("音频超过六小时，请拆分后导入。")
        }
        // Validate actual decoding as well as metadata. Do not mistake missing
        // AVAudioFile frame counts for empty audio, or accept an undecodable file.
        do {
            let decoder = try await FileAudioDecoder.open(asset)
            defer { decoder.cancel() }
            guard try decoder.nextBuffer() != nil else {
                throw AudioCoreError.invalidConfig("文件中没有可读取的音频样本。")
            }
        } catch is CancellationError { throw CancellationError() }
        catch { throw AudioCoreError.invalidConfig("无法解码这个音频，请检查文件是否完整，或重新导出后重试。") }
        try Task.checkCancellation()
        return AudioFileInfo(name: url.lastPathComponent, durationMS: max(1, Int((duration * 1000).rounded())), bytes: Int64(size))
    }
}

@MainActor
public final class FileTranscription {
    /// Awaiting delivery gives durable consumers backpressure, including during
    /// finalization. A completed import means every transcript was acknowledged.
    public var onEvent: ((AudioEvent) async throws -> Void)?
    private var analyzer: SpeechAnalyzer?
    private var cancelled = false
    public init() {}

    public func cancel() async {
        cancelled = true
        await analyzer?.cancelAndFinishNow()
    }

    public func run(url: URL, meetingID: String, epoch: String) async throws {
        guard analyzer == nil else { throw AudioCoreError.alreadyRunning }
        let info = try await AudioFileInfo.inspect(url)
        let status = await LocalSpeechModels.status(locale: "zh-CN")
        guard status == "installed" else {
            throw AudioCoreError.invalidConfig("请先安装文件转写使用的中文识别模型，再重新导入。")
        }
        try Task.checkCancellation()
        guard !cancelled else { throw CancellationError() }
        let module = SpeechTranscriber(locale: Locale(identifier: "zh-CN"), transcriptionOptions: [],
                                       reportingOptions: [], attributeOptions: [])
        guard let compatible = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [module]),
              let format = AVAudioFormat(commonFormat: compatible.commonFormat, sampleRate: compatible.sampleRate,
                                        channels: compatible.channelCount, interleaved: true) else {
            throw AudioCoreError.invalidConfig("没有可用的本机文件识别音频格式。")
        }
        let decoder = try await FileAudioDecoder.open(AVURLAsset(url: url), format: format)
        defer { decoder.cancel() }
        let engine = SpeechAnalyzer(modules: [module])
        try await engine.prepareToAnalyze(in: format)
        try Task.checkCancellation()
        guard !cancelled else { throw CancellationError() }
        analyzer = engine
        defer { analyzer = nil }
        var sequence = 0
        func event(_ type: String) -> AudioEvent {
            sequence += 1
            return AudioEvent(meetingID: meetingID, captureEpoch: epoch, streamID: "file-\(epoch)",
                              seq: sequence, type: type, source: .file)
        }
        let sink = onEvent
        let results = Task { @MainActor in
            var count = 0
            var processed = 0
            for try await result in module.results {
                if self.cancelled || Task.isCancelled { throw CancellationError() }
                guard result.isFinal else { continue }
                let start = CMTimeGetSeconds(result.range.start), end = CMTimeGetSeconds(result.range.end)
                guard start.isFinite, end.isFinite, start >= 0, end >= start else {
                    throw TranscriptReducerError.invalidRange
                }
                var remaining = String(result.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
                // Long framework results must still fit the existing transcript
                // contract; split by characters without inventing speaker IDs.
                while !remaining.isEmpty {
                    let text = String(remaining.prefix(4500)); remaining.removeFirst(text.count)
                    count += 1
                    var value = event("transcript.final")
                    value.segmentID = "f\(count)"; value.revision = 1; value.text = text
                    value.audioStartMS = Int((start * 1000).rounded())
                    value.audioEndMS = min(info.durationMS, Int((end * 1000).rounded()))
                    if value.audioEndMS! < value.audioStartMS! { value.audioEndMS = value.audioStartMS }
                    try await sink?(value)
                }
                processed = max(processed, min(info.durationMS, Int((end * 1000).rounded())))
                var progress = event("import.progress"); progress.audioEndMS = processed
                try await sink?(progress)
            }
        }
        do {
            // Unfolding pulls only when the analyzer asks for another buffer.
            // No full-file PCM copy, background queue, or cloud upload is needed.
            let input = AsyncThrowingStream<AnalyzerInput, Error>(unfolding: {
                guard let buffer = try decoder.nextBuffer() else { return nil }
                return AnalyzerInput(buffer: buffer)
            })
            if let end = try await engine.analyzeSequence(input) {
                try await engine.finalizeAndFinish(through: end)
            } else { try await engine.finalizeAndFinishThroughEndOfInput() }
            try await results.value
            try Task.checkCancellation()
            guard !cancelled else { throw CancellationError() }
        } catch {
            await engine.cancelAndFinishNow()
            results.cancel()
            _ = try? await results.value
            throw error
        }
    }
}
