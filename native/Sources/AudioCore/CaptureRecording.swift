import AVFoundation
import Foundation

/// Uncompressed native-rate PCM, written outside the realtime device callback.
/// CAF supports long recordings without WAV's 4 GB size ceiling.
final class CaptureRecording {
    let url: URL
    private var file: AVAudioFile?
    private var closed = false
    private(set) var frames: UInt64 = 0
    private(set) var sampleRate: Double = 0

    init(directory: URL, epoch: String, source: AudioSourceKind) throws {
        guard directory.isFileURL, directory.path.hasPrefix("/"),
              !epoch.isEmpty, epoch.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-" || $0 == "_") }) else {
            throw AudioCoreError.invalidConfig("Invalid recording destination.")
        }
        url = directory.appendingPathComponent("01-原始音频-\(source.rawValue)-\(epoch).caf")
        guard !FileManager.default.fileExists(atPath: url.path) else {
            throw AudioCoreError.invalidConfig("Recording already exists; start a new recording segment.")
        }
    }

    func append(_ buffer: AVAudioPCMBuffer) throws {
        guard !closed else { throw AudioCoreError.invalidConfig("Recording is already closed.") }
        guard buffer.frameLength > 0 else { return }
        if file == nil {
            file = try AVAudioFile(forWriting: url, settings: buffer.format.settings,
                                   commonFormat: buffer.format.commonFormat, interleaved: buffer.format.isInterleaved)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
            sampleRate = buffer.format.sampleRate
        }
        try file!.write(from: buffer)
        frames += UInt64(buffer.frameLength)
    }

    func close() {
        closed = true
        file?.close()
        file = nil
    }

    deinit { file?.close() }
}
