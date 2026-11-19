// Minimal runtime checks for Command Line Tools installations without XCTest.
// The full suite remains in Tests/AudioCoreTests and runs with swift test + Xcode.
@testable import AudioCore
import AVFoundation
import Foundation

@main
struct CoreSmoke {
    static func require(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        if try !condition() { throw NSError(domain: "AudioCoreSmoke", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
    }

    @MainActor static func main() async throws {
        var reducer = TranscriptReducer()
        let draft = try reducer.apply(.init(text: "查一下工作", isFinal: false, startMS: 0, endMS: 700))[0]
        let final = try reducer.apply(.init(text: "查一下工作流", isFinal: true, startMS: 0, endMS: 1000))[0]
        try require(draft.segmentID == final.segmentID && final.revision == 2 && final.supersedesRevision == 1, "final identity/revision")
        try require(try reducer.apply(.init(text: "查一下工作流", isFinal: true, startMS: 0, endMS: 1000)).isEmpty, "duplicate suppression")
        let retract = try reducer.apply(.init(text: "", isFinal: true, startMS: 0, endMS: 1000))[0]
        try require(retract.type == "transcript.retracted" && retract.text == "" && retract.revision == 3, "explicit empty retraction")
        _ = try reducer.apply(.init(text: "旧范围", isFinal: false, startMS: 1000, endMS: 2000))
        let replacement = try reducer.apply(.init(text: "新范围", isFinal: true, startMS: 1500, endMS: 2500))
        try require(replacement.map(\.type) == ["transcript.retracted", "transcript.final"], "range replacement")
        var bounded = TranscriptReducer(retainedSegments: 2)
        for index in 0..<3 { _ = try bounded.apply(.init(text: "\(index)", isFinal: true, startMS: index * 1000, endMS: index * 1000 + 900)) }
        var rejected = false
        do { _ = try bounded.apply(.init(text: "旧更正", isFinal: true, startMS: 0, endMS: 900)) }
        catch TranscriptReducerError.outsideCorrectionWindow { rejected = true }
        try require(rejected, "bounded revision window fails explicitly")
        var event = AudioEvent(meetingID: "m", captureEpoch: "e", streamID: "system-e", seq: 1,
                               type: "transcript.retracted", source: .system, now: Date(timeIntervalSince1970: 0))
        event.text = ""
        let object = try JSONSerialization.jsonObject(with: JSONEncoder().encode(event)) as! [String: Any]
        try require(object["text"] as? String == "" && object["occurred_at"] as? String == "1970-01-01T00:00:00Z", "wire format / UTC")
        let core = AudioCore()
        let microphoneConfig = AudioConfig(meetingID: "mic-test", sources: LiveAudioInput.microphone.sources)
        try require(microphoneConfig.sources == [.microphone], "microphone input must never silently become system capture")
        try require(Set(LiveAudioInput.both.sources) == Set([AudioSourceKind.system, .microphone])
                    && LiveAudioInput.both.sources.count == 2 && LiveAudioInput(rawValue: "file") == nil,
                    "live choices preserve both independent streams and reject file input")
        do { try await core.start(.init(meetingID: "")); throw NSError(domain: "expected rejection", code: 1) }
        catch AudioCoreError.invalidConfig { }
        try require(core.state == "idle", "invalid config starts no device")
        var events: [AudioEvent] = []
        core.onEvent = { events.append($0) }
        await core.stop(); await core.stop()
        try require(core.state == "stopped" && !events.contains(where: { $0.type == "audio.ready" }), "stop is idempotent")
        do { try await core.start(.init(meetingID: "m", sources: [.file])); throw NSError(domain: "expected rejection", code: 1) }
        catch AudioCoreError.invalidConfig { }
        try require(core.state == "stopped", "file input never starts a live device")
        let temporary = FileManager.default.temporaryDirectory.appendingPathComponent("zsjh-file-smoke-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: temporary) }
        let audioURL = temporary.appendingPathComponent("中文会议.wav")
        let format = AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!
        try autoreleasepool {
            let file = try AVAudioFile(forWriting: audioURL, settings: format.settings)
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 16000)!
            buffer.frameLength = 16000
            buffer.floatChannelData![0].initialize(repeating: 0, count: 16000)
            try file.write(from: buffer)
        }
        let info = try await AudioFileInfo.inspect(audioURL)
        try require(info.name == "中文会议.wav" && info.durationMS == 1000 && info.bytes > 0, "decoded file metadata")
        let opusURL = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Tests/Fixtures/opus-in-m4a.m4a")
        let opus = try await AudioFileInfo.inspect(opusURL)
        try require(opus.durationMS == 1000 && opus.bytes > 0, "Opus-in-M4A must be selectable with its real duration")
        let speechFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
        for url in [audioURL, opusURL] {
            let decoder = try await FileAudioDecoder.open(AVURLAsset(url: url), format: speechFormat)
            defer { decoder.cancel() }
            var frames = 0, nonSilent = false
            while let buffer = try decoder.nextBuffer() {
                try require(buffer.format.sampleRate == 16000 && buffer.format.channelCount == 1,
                            "file decoder must produce the requested speech format")
                frames += Int(buffer.frameLength)
                if let data = buffer.int16ChannelData {
                    nonSilent = nonSilent || (0..<Int(buffer.frameLength)).contains { abs(Int(data[0][$0])) > 100 }
                }
            }
            // The fixture uses 20 ms Opus packets; AVAssetReader can retain
            // codec padding in the final PCM packet. Metadata is checked above.
            let padding = url == opusURL ? 320 : 0
            try require(abs(frames - 16000) <= padding, "decoder must read through the end of WAV and Opus-in-M4A")
            if url == opusURL { try require(nonSilent, "M4A decoding must produce real PCM samples") }
        }
        for name in ["broken.m4a", "unsupported.txt"] {
            let url = temporary.appendingPathComponent(name)
            try Data("not audio".utf8).write(to: url)
            do { _ = try await AudioFileInfo.inspect(url); throw NSError(domain: "expected rejection", code: 1) }
            catch AudioCoreError.invalidConfig { }
        }
        print("AudioCore smoke: transcript, lifecycle, WAV and Opus-in-M4A checks passed; no capture or external service used.")
    }
}
