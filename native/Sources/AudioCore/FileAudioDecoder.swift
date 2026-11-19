import AVFoundation
import CoreMedia
import Foundation

/// Pulls one PCM buffer at a time from the media container. AVAudioFile cannot
/// read some valid M4A codecs (notably Opus); AVAssetReader can decode them.
/// The reader has a single sequential consumer. The Sendable wrapper lets that
/// consumer run on SpeechAnalyzer's executor without an unbounded producer task.
final class FileAudioDecoder: @unchecked Sendable {
    private let reader: AVAssetReader
    private let output: AVAssetReaderTrackOutput

    static func open(_ asset: AVURLAsset, format: AVAudioFormat? = nil) async throws -> FileAudioDecoder {
        let tracks = try await asset.loadTracks(withMediaType: .audio)
        try Task.checkCancellation()
        guard let track = tracks.first else {
            throw AudioCoreError.invalidConfig("文件中没有可读取的音轨。")
        }
        return try FileAudioDecoder(asset: asset, track: track, format: format)
    }

    private init(asset: AVURLAsset, track: AVAssetTrack, format: AVAudioFormat?) throws {
        reader = try AVAssetReader(asset: asset)
        let settings: [String: Any] = format?.settings ?? [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVLinearPCMBitDepthKey: 32,
            AVLinearPCMIsFloatKey: true,
            AVLinearPCMIsNonInterleaved: false,
        ]
        output = AVAssetReaderTrackOutput(track: track, outputSettings: settings)
        guard reader.canAdd(output) else {
            throw AudioCoreError.invalidConfig("无法解码这个音频的编码，请尝试重新导出音频。")
        }
        reader.add(output)
        guard reader.startReading() else {
            throw reader.error ?? AudioCoreError.invalidConfig("无法开始读取音频，请检查文件是否完整。")
        }
    }

    deinit { reader.cancelReading() }
    func cancel() { reader.cancelReading() }

    func nextBuffer() throws -> AVAudioPCMBuffer? {
        try Task.checkCancellation()
        while let sample = output.copyNextSampleBuffer() {
            let frames = CMSampleBufferGetNumSamples(sample)
            if frames == 0 { continue }
            guard frames > 0, frames <= Int(Int32.max),
                  let description = CMSampleBufferGetFormatDescription(sample) else {
                throw AudioCoreError.invalidConfig("无法读取音频样本，请检查文件是否完整。")
            }
            let format = AVAudioFormat(cmAudioFormatDescription: description)
            guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)) else {
                throw AudioCoreError.invalidConfig("无法读取音频样本，请检查文件是否完整。")
            }
            buffer.frameLength = AVAudioFrameCount(frames)
            let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(sample, at: 0,
                frameCount: Int32(frames), into: buffer.mutableAudioBufferList)
            guard status == noErr else {
                throw AudioCoreError.invalidConfig("音频解码失败，请尝试重新导出音频。")
            }
            return buffer
        }
        try Task.checkCancellation()
        guard reader.status == .completed else {
            throw reader.error ?? AudioCoreError.invalidConfig("音频读取中断，已有转写保留。")
        }
        return nil
    }
}
