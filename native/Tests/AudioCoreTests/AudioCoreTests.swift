import AVFoundation
import Foundation
import XCTest
@testable import AudioCore

final class TranscriptReducerTests: XCTestCase {
    func testInterimFinalAndFinalCorrectionKeepIdentity() throws {
        var reducer = TranscriptReducer()
        let interim = try XCTUnwrap(reducer.apply(.init(text: "查一下工作", isFinal: false, startMS: 100, endMS: 800)).first)
        let final = try XCTUnwrap(reducer.apply(.init(text: "查一下工作流", isFinal: true, startMS: 100, endMS: 1100)).first)
        let corrected = try XCTUnwrap(reducer.apply(.init(text: "查一下智能体工作流", isFinal: true, startMS: 100, endMS: 1200)).first)
        XCTAssertEqual(interim.segmentID, final.segmentID)
        XCTAssertEqual(final.segmentID, corrected.segmentID)
        XCTAssertEqual([interim.revision, final.revision, corrected.revision], [1, 2, 3])
        XCTAssertEqual(corrected.supersedesRevision, 2)
        XCTAssertEqual(final.type, "transcript.final")
    }

    func testEmptyReplacementRetractsFinalExplicitly() throws {
        var reducer = TranscriptReducer()
        let final = try XCTUnwrap(reducer.apply(.init(text: "错误请求", isFinal: true, startMS: 0, endMS: 1000)).first)
        let retracted = try XCTUnwrap(reducer.apply(.init(text: "", isFinal: true, startMS: 0, endMS: 1000)).first)
        XCTAssertEqual(retracted.type, "transcript.retracted")
        XCTAssertEqual(retracted.text, "")
        XCTAssertEqual(retracted.segmentID, final.segmentID)
        XCTAssertEqual(retracted.revision, 2)
    }

    func testReplacedRangeRetractsDisplacedDraft() throws {
        var reducer = TranscriptReducer()
        let first = try XCTUnwrap(reducer.apply(.init(text: "第一段", isFinal: false, startMS: 0, endMS: 1000)).first)
        let changes = try reducer.apply(.init(text: "合并的完成句", isFinal: true, startMS: 500, endMS: 1500))
        XCTAssertEqual(changes.map(\.type), ["transcript.retracted", "transcript.final"])
        XCTAssertEqual(changes.first?.segmentID, first.segmentID)
        XCTAssertNotEqual(changes.first?.segmentID, changes.last?.segmentID)
    }

    func testDuplicateAndStaleInterimDoNotRecreateFinal() throws {
        var reducer = TranscriptReducer()
        _ = try reducer.apply(.init(text: "完成", isFinal: true, startMS: 0, endMS: 1000))
        _ = try reducer.apply(.init(text: "后一段", isFinal: true, startMS: 1000, endMS: 1500))
        XCTAssertTrue(try reducer.apply(.init(text: "完成", isFinal: true, startMS: 0, endMS: 1000)).isEmpty)
        XCTAssertTrue(try reducer.apply(.init(text: "旧草稿", isFinal: false, startMS: 0, endMS: 2000)).isEmpty)
    }

    func testStopRetractsOnlyUnfinishedDrafts() throws {
        var reducer = TranscriptReducer()
        _ = try reducer.apply(.init(text: "完成", isFinal: true, startMS: 0, endMS: 1000))
        _ = try reducer.apply(.init(text: "未完成", isFinal: false, startMS: 1200, endMS: 1800))
        let changes = reducer.retractOpen()
        XCTAssertEqual(changes.count, 1)
        XCTAssertEqual(changes.first?.type, "transcript.retracted")
        XCTAssertTrue(reducer.retractOpen().isEmpty)
    }

    func testOutOfWindowCorrectionFailsInsteadOfInventingRevision() throws {
        var reducer = TranscriptReducer(retainedSegments: 2)
        for index in 0..<3 {
            _ = try reducer.apply(.init(text: "第\(index)段", isFinal: true, startMS: index * 1000, endMS: (index + 1) * 1000))
        }
        XCTAssertThrowsError(try reducer.apply(.init(text: "旧句更正", isFinal: true, startMS: 0, endMS: 1000)))
        XCTAssertThrowsError(try reducer.apply(.init(text: "无效", isFinal: true, startMS: 2000, endMS: 1000)))
    }
}

final class AudioEventTests: XCTestCase {
    func testWireFormatPreservesEmptyRetractionAndUTC() throws {
        var event = AudioEvent(meetingID: "m-test", captureEpoch: "epoch", streamID: "system-epoch", seq: 2,
                               type: "transcript.retracted", source: .system, now: Date(timeIntervalSince1970: 0))
        event.segmentID = "s1"; event.revision = 2; event.supersedesRevision = 1
        event.text = ""; event.audioStartMS = 0; event.audioEndMS = 1000
        let data = try JSONEncoder().encode(event)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(object["text"] as? String, "")
        XCTAssertEqual(object["protocol_version"] as? Int, 1)
        XCTAssertEqual(object["occurred_at"] as? String, "1970-01-01T00:00:00Z")
        XCTAssertNil(object["meetingID"])
        XCTAssertEqual(try JSONDecoder().decode(AudioEvent.self, from: data).revision, 2)
    }

    func testStatusNeedsNoTranscriptFields() throws {
        let event = AudioEvent(meetingID: "m", captureEpoch: "e", streamID: "control-e", seq: 1, type: "audio.status", source: .system)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(event)) as? [String: Any])
        XCTAssertNil(object["segment_id"])
        XCTAssertNil(object["audio_start_ms"])
        XCTAssertNil(object["text"])
    }
}

final class PCMConverterTests: XCTestCase {
    func testSampleRateConversionCarriesRealRateAndMonoSamples() throws {
        let inputFormat = try XCTUnwrap(AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48000, channels: 1, interleaved: false))
        let outputFormat = try XCTUnwrap(AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: false))
        let input = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: 48000))
        input.frameLength = 48000
        for index in 0..<48000 { input.floatChannelData![0][index] = Float(sin(Double(index) * 2 * .pi * 440 / 48000)) * 0.2 }
        let output = try XCTUnwrap(PCMConverter().convert(input, to: outputFormat))
        XCTAssertEqual(output.format.sampleRate, 16000)
        XCTAssertEqual(output.format.channelCount, 1)
        XCTAssertGreaterThan(output.frameLength, 15800)
        XCTAssertLessThanOrEqual(output.frameLength, 16064)
        XCTAssertTrue((0..<Int(output.frameLength)).contains { abs(Int(output.int16ChannelData![0][$0])) > 100 })
    }
}

final class CaptureRecordingTests: XCTestCase {
    func testNativeRatePCMIsPreservedAcrossBuffersAndCanBeReopened() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let format = try XCTUnwrap(AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48000, channels: 1, interleaved: false))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4800))
        buffer.frameLength = 4800
        for i in 0..<4800 { buffer.floatChannelData![0][i] = Float(sin(Double(i) * 0.05)) * 0.2 }
        let recorder = try CaptureRecording(directory: directory, epoch: "test-epoch", source: .system)
        try recorder.append(buffer); try recorder.append(buffer); recorder.close()
        let saved = try AVAudioFile(forReading: recorder.url)
        XCTAssertEqual(saved.length, 9600)
        XCTAssertEqual(saved.processingFormat.sampleRate, 48000)
        let read = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: saved.processingFormat, frameCapacity: 9600))
        try saved.read(into: read)
        for i in 0..<9600 { XCTAssertEqual(read.floatChannelData![0][i], buffer.floatChannelData![0][i % 4800], accuracy: 0.000001) }
        XCTAssertThrowsError(try CaptureRecording(directory: directory, epoch: "test-epoch", source: .system))
        let mode = try FileManager.default.attributesOfItem(atPath: recorder.url.path)[.posixPermissions] as? Int
        XCTAssertEqual(mode, 0o600)
    }
}

final class LifecycleTests: XCTestCase {
    @MainActor func testInvalidConfigurationDoesNotStartCapture() async {
        let core = AudioCore()
        do { try await core.start(.init(meetingID: "", sources: [.system])); XCTFail("Expected rejection") }
        catch { XCTAssertEqual(core.state, "idle") }
        do { try await core.start(.init(meetingID: "m", sources: [.system, .system])); XCTFail("Expected rejection") }
        catch { XCTAssertEqual(core.state, "idle") }
    }

    @MainActor func testRepeatedStopIsSafeAndDoesNotStartAnyDevice() async {
        let core = AudioCore()
        var events: [AudioEvent] = []
        core.onEvent = { events.append($0) }
        await core.stop(); await core.stop()
        XCTAssertEqual(core.state, "stopped")
        XCTAssertFalse(events.contains { $0.type == "audio.ready" })
        XCTAssertEqual(events.map(\.seq), Array(1...events.count))
    }
}
