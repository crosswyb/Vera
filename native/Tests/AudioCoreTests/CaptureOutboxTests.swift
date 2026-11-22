import AVFoundation
import XCTest
@testable import AudioCore

final class CaptureOutboxTests: XCTestCase {
    func testBlockedDeliveryKeepsBothRecordingsAndSealsExplicitGaps() async throws {
        let parent = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: parent) }
        let outbox = parent.appendingPathComponent("events")
        let manifest = CaptureEventOutbox.Manifest(meetingID: "meeting", epoch: "epoch", sources: [.system, .microphone])
        try manifest.write(to: outbox)
        let writer = CaptureEventOutbox.Writer(directory: outbox, manifest: manifest, eventLimit: 2)
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 1600))
        buffer.frameLength = 1600
        buffer.floatChannelData?[0].initialize(repeating: 0.1, count: 1600)
        for source in manifest.sources {
            let recorder = try CaptureRecording(directory: parent, epoch: manifest.epoch, source: source)
            for sequence in 1...30 {
                try recorder.append(buffer)
                var final = AudioEvent(meetingID: manifest.meetingID, captureEpoch: manifest.epoch,
                                       streamID: source.rawValue, seq: sequence, type: "transcript.final", source: source)
                final.segmentID = "s\(sequence)"; final.revision = 1; final.text = "合成测试"
                final.audioStartMS = (sequence - 1) * 100; final.audioEndMS = sequence * 100
                writer.submit(final)
            }
            recorder.close()
            let audio = try AVAudioFile(forReading: recorder.url)
            XCTAssertEqual(audio.length, 48000)
            let decoded = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: audio.processingFormat, frameCapacity: 48000))
            try audio.read(into: decoded)
            XCTAssertEqual(decoded.frameLength, 48000)
            var saved = AudioEvent(meetingID: manifest.meetingID, captureEpoch: manifest.epoch,
                                   streamID: source.rawValue, seq: 31, type: "audio.recording", source: source)
            saved.state = "saved"; saved.framesReceived = recorder.frames; saved.audioEndMS = 3000
            writer.submit(saved)
        }
        writer.seal()
        await writer.flush()
        let records = try CaptureEventOutbox.pending(in: outbox)
        let events = try records.map { try JSONDecoder().decode(AudioEvent.self, from: Data(contentsOf: $0)) }
        XCTAssertEqual(events.filter { $0.type == "transcript.final" }.count, 2)
        XCTAssertEqual(events.filter { $0.type == "audio.recording" }.count, 2)
        let gaps = events.filter { $0.type == "asr.gap" }
        XCTAssertEqual(Set(gaps.map(\.source)), Set(manifest.sources))
        XCTAssertTrue(gaps.allSatisfy { ($0.audioStartMS ?? 3000) < 3000 && $0.audioEndMS == 3000 })
        let seal = try CaptureEventOutbox.Seal.read(from: outbox)
        XCTAssertTrue(seal.saved); XCTAssertTrue(seal.hadAudio)
        try FileManager.default.removeItem(at: records[0])
        XCTAssertEqual(try CaptureEventOutbox.pending(in: outbox), Array(records.dropFirst()))
    }
}
