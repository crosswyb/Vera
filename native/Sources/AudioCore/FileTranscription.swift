import AVFoundation
import CoreMedia
import Foundation
import Speech

/// Validate file input without opening a capture device or sending audio.
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
    private var cloud: CloudFileRecognition?
    private var running = false
    private var cancelled = false
    public init() {}

    public func cancel() async {
        cancelled = true
        cloud?.cancel()
        await analyzer?.cancelAndFinishNow()
    }

    public func run(url: URL, meetingID: String, epoch: String,
                    speech: SpeechSessionConfig? = nil, lease: SpeechLease? = nil,
                    startMS: Int = 0, revision: Int = 1) async throws {
        guard !running else { throw AudioCoreError.alreadyRunning }
        running = true
        defer { running = false }
        let info = try await AudioFileInfo.inspect(url)
        if let speech, speech.provider != "apple_local" {
            guard let lease else { throw CloudSpeechError.authorizationEnded }
            try await runCloud(url: url, info: info, meetingID: meetingID, epoch: epoch,
                               config: speech, lease: lease, startMS: startMS, revision: revision)
            return
        }
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
                    value.provider = "apple_local"
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

    private func runCloud(url: URL, info: AudioFileInfo, meetingID: String, epoch: String,
                          config: SpeechSessionConfig, lease: SpeechLease, startMS: Int, revision: Int) async throws {
        try Task.checkCancellation()
        guard !cancelled else { throw CancellationError() }
        let worker = try CloudFileRecognition(config: config, lease: lease)
        cloud = worker
        defer { worker.close(); cloud = nil }
        let decoder = try await FileAudioDecoder.open(AVURLAsset(url: url))
        defer { decoder.cancel() }
        let pcm = SpeechPCM()
        var chunker = CloudAudioChunker()
        var sequence = 0
        let requestedStartMS = max(0, min(startMS, info.durationMS))
        let startFrame = Int((Double(requestedStartMS) * Double(SpeechPCM.rate) / 1000).rounded())
        var remainingPrefixFrames = startFrame
        var publishedMS = requestedStartMS
        func event(_ type: String) -> AudioEvent {
            sequence += 1
            var value = AudioEvent(meetingID: meetingID, captureEpoch: epoch, streamID: "file-\(epoch)",
                                   seq: sequence, type: type, source: .file)
            value.provider = config.provider
            return value
        }
        func publishProgress(_ offset: Int) async throws {
            var progress = event("import.progress")
            progress.audioEndMS = offset
            try await onEvent?(progress)
            publishedMS = offset
        }
        func submit(_ samples: [Int16], through endFrame: Int, last: Bool = false) async throws {
            try Task.checkCancellation()
            try lease.check()
            guard !cancelled else { throw CancellationError() }
            guard Self.milliseconds(endFrame) <= info.durationMS + 250 else { throw CloudSpeechError.invalidAudio }
            // Do not upload chunks of digital silence.
            let text = samples.allSatisfy { $0 == 0 } ? "" : try await worker.transcribe(samples)
            let absoluteEndFrame = startFrame + endFrame
            let start = min(info.durationMS, Self.milliseconds(absoluteEndFrame - samples.count))
            let end = last ? info.durationMS : min(info.durationMS, Self.milliseconds(absoluteEndFrame))
            let chunkStartMS = start
            for index in stride(from: 0, to: text.unicodeScalars.count, by: 4500) {
                try Task.checkCancellation()
                try lease.check()
                guard !cancelled else { throw CancellationError() }
                let part = text.unicodeScalars.dropFirst(index).prefix(4500)
                var result = event("transcript.final")
                result.segmentID = "chunk-\(chunkStartMS)-\(index / 4500)"
                result.revision = max(1, revision)
                result.text = String(String.UnicodeScalarView(part))
                result.audioStartMS = start; result.audioEndMS = end
                try await onEvent?(result)
            }
            try lease.check()
            if end < info.durationMS { try await publishProgress(end) }
        }
        func feed(_ samples: [Int16]) async throws {
            var input = samples
            if remainingPrefixFrames > 0 {
                if input.count <= remainingPrefixFrames {
                    remainingPrefixFrames -= input.count
                    return
                }
                input = Array(input.dropFirst(remainingPrefixFrames))
                remainingPrefixFrames = 0
            }
            var offset = 0
            while offset < input.count {
                if let chunk = chunker.next(input, from: &offset) {
                    try await submit(chunk, through: chunker.sentFrames)
                }
            }
        }
        while let buffer = try decoder.nextBuffer() {
            try Task.checkCancellation()
            try lease.check()
            guard !cancelled else { throw CancellationError() }
            try await feed(pcm.samples(buffer))
            await Task.yield()
        }
        try await feed(pcm.drain())
        guard remainingPrefixFrames == 0 else { throw CloudSpeechError.invalidAudio }
        let lastChunk = chunker.finish()
        let decodedMS = Self.milliseconds(startFrame + chunker.sentFrames)
        // The resampler may round a few frames. Larger metadata/decoded duration
        // differences must not turn missing audio into an acknowledged interval.
        guard decodedMS > 0, abs(info.durationMS - decodedMS) <= 250 else {
            throw CloudSpeechError.invalidAudio
        }
        if let lastChunk { try await submit(lastChunk, through: chunker.sentFrames, last: true) }
        try Task.checkCancellation()
        try lease.check()
        guard !cancelled else { throw CancellationError() }
        if publishedMS < info.durationMS { try await publishProgress(info.durationMS) }
    }

    private static func milliseconds(_ frames: Int) -> Int {
        Int((Double(frames) * 1000 / Double(SpeechPCM.rate)).rounded())
    }

}
