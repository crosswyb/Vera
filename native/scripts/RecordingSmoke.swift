import AVFoundation
import Foundation
import Darwin

@main struct RecordingSmoke {
    static func main() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48000, channels: 1, interleaved: false)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4800)!
        buffer.frameLength = 4800
        for i in 0..<4800 { buffer.floatChannelData![0][i] = Float(sin(Double(i) * 0.05)) * 0.2 }
        let recorder = try CaptureRecording(directory: folder, epoch: "smoke", source: .microphone)
        try recorder.append(buffer); try recorder.append(buffer); recorder.close()
        let file = try AVAudioFile(forReading: recorder.url)
        precondition(file.length == 9600 && file.processingFormat.sampleRate == 48000)
        let saved = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 9600)!
        try file.read(into: saved)
        for i in 0..<9600 {
            precondition(abs(saved.floatChannelData![0][i] - buffer.floatChannelData![0][i % 4800]) < 0.000001)
        }
        var conflict = false
        do { _ = try CaptureRecording(directory: folder, epoch: "smoke", source: .microphone) }
        catch { conflict = true }
        precondition(conflict)
        let permissions = try FileManager.default.attributesOfItem(atPath: recorder.url.path)[.posixPermissions] as! Int
        precondition(permissions == 0o600)
        let crashPath = folder.appendingPathComponent("01-原始音频-system-crash.caf")
        if CommandLine.arguments.count > 2, CommandLine.arguments[1] == "--crash-child" {
            let writer = try CaptureRecording(directory: URL(fileURLWithPath: CommandLine.arguments[2]), epoch: "crash", source: .system)
            try writer.append(buffer); try writer.append(buffer)
            Darwin._exit(0) // Simulate process loss without AVAudioFile.close/deinit.
        }
        let child = Process()
        child.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
        child.arguments = ["--crash-child", folder.path]
        try child.run(); child.waitUntilExit()
        precondition(child.terminationStatus == 0)
        let recovered = try AVAudioFile(forReading: crashPath)
        precondition(recovered.length == 9600)
        print("Recording smoke passed: 9600 PCM samples preserved, playable CAF, private permissions, no overwrite.")
        print("Unclosed CAF remains readable after abrupt child process exit: 9600 frames.")
    }
}
