import AVFoundation
import Foundation

@MainActor
private final class FixtureSocket: DashScopeSocket {
    var response: HTTPURLResponse?
    private(set) var taskID: String?
    private(set) var frames = 0
    private(set) var actions: [String] = []
    private(set) var closed = false
    private var inbox: [URLSessionWebSocketTask.Message] = []
    private var waiter: CheckedContinuation<URLSessionWebSocketTask.Message, Error>?

    func send(_ message: URLSessionWebSocketTask.Message) async throws {
        switch message {
        case .string(let json):
            let body = try JSONSerialization.jsonObject(with: Data(json.utf8)) as! [String: Any]
            let header = body["header"] as! [String: String]
            taskID = header["task_id"]
            actions.append(header["action"]!)
        case .data(let data): frames += data.count / 2
        @unknown default: throw URLError(.badServerResponse)
        }
    }

    func receive() async throws -> URLSessionWebSocketTask.Message {
        guard !closed else { throw URLError(.networkConnectionLost) }
        if !inbox.isEmpty { return inbox.removeFirst() }
        return try await withCheckedThrowingContinuation { waiter = $0 }
    }

    func close() {
        closed = true
        waiter?.resume(throwing: URLError(.networkConnectionLost))
        waiter = nil
    }

    func server(_ event: String, header extra: [String: String] = [:], sentence: [String: Any]? = nil) throws {
        var header = extra
        header["task_id"] = taskID!
        header["event"] = event
        var body: [String: Any] = ["header": header]
        if let sentence { body["payload"] = ["output": ["sentence": sentence]] }
        let data = try JSONSerialization.data(withJSONObject: body)
        let message = URLSessionWebSocketTask.Message.data(data)
        if let waiter {
            self.waiter = nil
            waiter.resume(returning: message)
        } else {
            inbox.append(message)
        }
    }

    func sentence(_ text: String, start: Int, end: Int?, final: Bool, id: Int) throws {
        try server("result-generated", sentence: [
            "sentence_id": id, "begin_time": start, "end_time": end as Any? ?? NSNull(),
            "text": text, "sentence_end": final,
        ])
    }
}

private final class FixtureSource: CapturedAudioSource {
    var onAudio: ((AVAudioPCMBuffer) -> Void)?
    var onRateChange: (() -> Void)?
    var onFailure: ((String) -> Void)?
    private(set) var active = false
    func start() throws { active = true }
    func stop() { active = false }
    func play(_ buffer: AVAudioPCMBuffer) { if active { onAudio?(buffer) } }
}

@main
@MainActor
struct CloudASRSmoke {
    private static func require(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        if try !condition() { throw NSError(domain: "CloudASRSmoke", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
    }

    private static func wait(_ message: String, seconds: Double = 2, until condition: () -> Bool) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(seconds))
        while !condition(), .now < deadline { try await Task.sleep(for: .milliseconds(5)) }
        try require(condition(), message)
    }

    private static func config(_ provider: String, _ endpoint: String) throws -> SpeechSessionConfig {
        let json: [String: Any] = ["provider": provider, "endpoint": endpoint, "model": "fixture-model",
                                   "api_key": "fixture-only", "fallback_to_apple": false, "revision": 1]
        return try JSONDecoder().decode(SpeechSessionConfig.self, from: JSONSerialization.data(withJSONObject: json))
    }

    private static func liveConfig() throws -> SpeechSessionConfig {
        try config("dashscope", "wss://dashscope.aliyuncs.com/api-ws/v1/inference")
    }

    static func main() async throws {
        try await realtimeProtocol()
        try await sealFailureRecovery()
        try await missingTail()
        try await retryAndRevoke()
        try await boundedReconnects()
        try await backlogPauseContinuesRecording()
        try await bothSourcesStopConcurrently()
        try await fileRequests()
        try await filePipeline()
        print("Cloud ASR fixtures passed: protocol and revisions, missing tail, durable seal retry, bounded reconnects and cancellation, two recordings, file formats and chunk continuity.")
    }

    private static func realtimeProtocol() async throws {
        let socket = FixtureSocket()
        let lease = SpeechLease(seconds: 10)
        let session = try DashScopeRecognition(config: liveConfig(), lease: lease, makeSocket: { _ in socket })
        defer { session.cancel() }
        var changes: [TranscriptChange] = []
        session.onChange = { changes.append($0) }
        session.start(until: .now.advanced(by: .seconds(2)))
        try session.append(Data(repeating: 9, count: 6400))
        try await wait("run-task") { socket.taskID != nil }
        try require(socket.actions == ["run-task"] && socket.frames == 0, "PCM sent before task-started")
        try socket.server("task-started")
        try await wait("send buffered PCM") { socket.frames == 3200 }
        try socket.server("result-generated", sentence: ["heartbeat": true, "sentence_id": 0])
        try socket.sentence("中", start: 0, end: nil, final: false, id: 1)
        try await wait("interim sent") { changes.count == 1 }
        try require(changes[0].endMS == 200, "interim should use offered audio end")
        let finishing = Task { try await session.finish(until: .now.advanced(by: .seconds(2))) }
        try await wait("finish-task") { socket.actions.last == "finish-task" }
        try socket.sentence("中文", start: 20, end: 180, final: true, id: 1)
        try socket.server("task-finished")
        try await finishing.value
        try require(changes.count == 2 && changes[1].segmentID == changes[0].segmentID &&
                    changes[1].revision == 2 && changes[1].supersedesRevision == 1 &&
                    changes[1].startMS == 20 && changes[1].endMS == 180, "sentence_id revisions and final timestamps")
    }

    private static func missingTail() async throws {
        let socket = FixtureSocket()
        let session = try DashScopeRecognition(config: liveConfig(), lease: SpeechLease(seconds: 10), makeSocket: { _ in socket })
        defer { session.cancel() }
        session.start(until: .now.advanced(by: .seconds(2)))
        try await wait("run-task with missing tail") { socket.taskID != nil }
        try socket.server("task-started")
        try session.append(Data(repeating: 8, count: 3200))
        try await wait("send PCM before missing tail") { socket.frames == 1600 }
        // The documented sentence-start example initially has empty text.
        try socket.sentence("", start: 0, end: nil, final: false, id: 1)
        let finishing = Task { try await session.finish(until: .now.advanced(by: .seconds(2))) }
        try await wait("finish-task with an open sentence") { socket.actions.last == "finish-task" }
        try socket.server("task-finished")
        do {
            try await finishing.value
            try require(false, "task-finished without sentence final was accepted")
        } catch CloudSpeechError.invalidResponse { }
        let expired = SpeechLease(seconds: 0.015)
        try await wait("lease expiry") { !expired.isValid }
        expired.renew(seconds: 10)
        try require(!expired.isValid, "expired lease was renewed")
    }

    private static func fixtureBuffer() -> AVAudioPCMBuffer {
        let format = AVAudioFormat(standardFormatWithSampleRate: 16_000, channels: 1)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 1600)!
        buffer.frameLength = 1600
        buffer.floatChannelData![0].initialize(repeating: 0.125, count: 1600)
        return buffer
    }

    private static func tempDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("zsjh-asr-\(UUID())")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private static func retryAndRevoke() async throws {
        let directory = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let source = FixtureSource()
        var sockets: [FixtureSocket] = []
        let core = AudioCore(makeSource: { _, _ in source }, makeSocket: { _ in
            let socket = FixtureSocket()
            sockets.append(socket)
            return socket
        })
        var events: [AudioEvent] = []
        core.onEvent = { events.append($0) }
        let lease = SpeechLease(seconds: 10)
        try await core.start(.init(meetingID: "retry", recordingDirectory: directory, captureEpoch: "retry"),
                             speech: liveConfig(), lease: lease)
        try require(source.active && core.state == "running", "recording did not start before recognition")
        try await wait("first socket handshakes") { sockets.first?.taskID != nil }
        let first = sockets[0]
        try first.server("task-started")
        let audio = fixtureBuffer()
        for _ in 0..<3 { source.play(audio) }
        try await wait("first socket received all audio") { first.frames == 4800 }
        try first.sentence("已确认", start: 0, end: 100, final: true, id: 1)
        try first.sentence("暂未确认", start: 100, end: nil, final: false, id: 2)
        try await wait("first socket transcript") { events.contains { $0.type == "transcript.interim" } }
        first.close()
        try await wait("one reconnect started") { sockets.count == 2 && sockets[1].taskID != nil }
        try require(source.active, "network failure stopped recording")
        source.play(audio)
        let second = sockets[1]
        try second.server("task-started")
        try await wait("replayed precisely the unconfirmed audio") { second.frames >= 4800 }
        try require(second.frames == 4800, "retry uploaded already confirmed audio")
        try second.sentence("重连后确认", start: 0, end: 200, final: true, id: 1)
        try await wait("retry emitted the final") { events.filter { $0.type == "transcript.final" }.count == 2 }
        lease.revoke()
        for _ in 0..<2 { source.play(audio) }
        await core.stop()
        let finals = events.filter { $0.type == "transcript.final" }
        let retract = events.first { $0.type == "transcript.retracted" }
        let draft = events.first { $0.type == "transcript.interim" }
        let gap = events.last { $0.type == "asr.gap" }
        try require(finals[0].segmentID != finals[1].segmentID && finals[1].audioStartMS == 100 && finals[1].audioEndMS == 300,
                    "restart IDs and time offset")
        try require(retract?.segmentID == draft?.segmentID && retract?.supersedesRevision == draft?.revision,
                    "abandoned interim was not retracted")
        try require(second.frames == 4800 && second.closed && sockets.count == 2, "revocation sent more audio or reconnected")
        try require(gap?.audioStartMS == 300 && gap?.audioEndMS == 600, "missing timed gap after revocation")
        try require(!events.contains { $0.type == "audio.gap" }, "speech failures must not emit capture errors")
        try require(events.contains { $0.type == "audio.recording" && $0.state == "saved" && $0.framesReceived == 9600 },
                    "revoked capture recording was not saved")
        let caf = try AVAudioFile(forReading: directory.appendingPathComponent("01-原始音频-system-retry.caf"))
        try require(caf.length == 9600, "CAFs must contain pre- and post-revocation frames")
    }

    private static func boundedReconnects() async throws {
        let directory = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        var sockets: [FixtureSocket] = []
        var states: [String] = []
        var confirmed = 0
        let lease = SpeechLease(seconds: 30)
        let recognition = LiveCaptureRecognition(
            config: .init(meetingID: "reconnects", recordingDirectory: directory, captureEpoch: "reconnects"),
            speech: try liveConfig(), lease: lease,
            makeSocket: { _ in let socket = FixtureSocket(); sockets.append(socket); return socket },
            onEvent: { _, _, state, _, _, _ in if let state { states.append(state) } },
            onChange: { change, _ in if change.type == "transcript.final" { confirmed += 1 } })
        recognition.start()
        recognition.receive(fixtureBuffer(), voiced: true)
        for attempt in 0...3 {
            try await wait("transient failure reconnect \(attempt)", seconds: 3) {
                sockets.count == attempt + 1 && sockets[attempt].taskID != nil
            }
            try sockets[attempt].server("task-started")
            try await wait("reconnect accepted PCM") { sockets[attempt].frames > 0 }
            if attempt == 2 {
                // Useful confirmed text resets the consecutive-failure budget.
                try sockets[attempt].sentence("恢复后已确认", start: 0, end: 100, final: true, id: 1)
                try await wait("useful result resets the retry budget") { confirmed == 1 }
                recognition.receive(fixtureBuffer(), voiced: true)
            }
            sockets[attempt].close()
        }
        // Two more retries are available after the confirmed result above.
        for attempt in 4...5 {
            try await wait("retry budget after useful result \(attempt)", seconds: 3) {
                sockets.count == attempt + 1 && sockets[attempt].taskID != nil
            }
            try sockets[attempt].server("task-started")
            try await wait("retry accepted remaining PCM") { sockets[attempt].frames > 0 }
            sockets[attempt].close()
        }
        try await wait("consecutive retries are bounded") { states.last == "paused" }
        await recognition.finish(graceful: false, deadline: .now.advanced(by: .seconds(1)))
        try require(sockets.count == 6, "transient failures exhausted too early or retried without a bound")

        var stoppedSockets: [FixtureSocket] = []
        var stoppedConnecting = 0
        let stopped = LiveCaptureRecognition(
            config: .init(meetingID: "stopped", recordingDirectory: directory, captureEpoch: "stopped"),
            speech: try liveConfig(), lease: SpeechLease(seconds: 10),
            makeSocket: { _ in let socket = FixtureSocket(); stoppedSockets.append(socket); return socket },
            onEvent: { _, _, state, _, _, _ in if state == "connecting" { stoppedConnecting += 1 } }, onChange: { _, _ in })
        stopped.start()
        try await wait("connection before stop") { stoppedSockets.first?.taskID != nil }
        stoppedSockets[0].close()
        try await wait("reconnect is scheduled before stop") { stoppedConnecting == 2 }
        await stopped.finish(graceful: false, deadline: .now.advanced(by: .seconds(1)))
        try await Task.sleep(for: .milliseconds(650))
        try require(stoppedSockets.count == 1, "stop did not cancel a scheduled reconnect")

        var revokedSockets: [FixtureSocket] = []
        var reconnects = 0
        let revokedLease = SpeechLease(seconds: 10)
        let revoked = LiveCaptureRecognition(
            config: .init(meetingID: "revoked", recordingDirectory: directory, captureEpoch: "revoked"),
            speech: try liveConfig(), lease: revokedLease,
            makeSocket: { _ in let socket = FixtureSocket(); revokedSockets.append(socket); return socket },
            onEvent: { _, _, state, _, _, _ in if state == "connecting" { reconnects += 1 } }, onChange: { _, _ in })
        revoked.start()
        try await wait("connection before lease revocation") { revokedSockets.first?.taskID != nil }
        revokedSockets[0].close()
        try await wait("reconnect is scheduled before lease revocation") { reconnects == 2 }
        revokedLease.revoke()
        try await Task.sleep(for: .milliseconds(650))
        try require(revokedSockets.count == 1, "revocation did not cancel a scheduled reconnect")
        await revoked.finish(graceful: false, deadline: .now.advanced(by: .seconds(1)))
    }

    private static func sealFailureRecovery() async throws {
        let directory = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let manifest = CaptureEventOutbox.Manifest(meetingID: "seal", epoch: "seal", sources: [.system])
        try manifest.write(to: directory)
        let journal = CaptureEventOutbox.Writer(directory: directory, manifest: manifest, eventLimit: 0)
        var event = AudioEvent(meetingID: "seal", captureEpoch: "seal", streamID: "system", seq: 1,
                               type: "transcript.final", source: .system)
        event.audioStartMS = 0; event.audioEndMS = 100; event.segmentID = "s1"; event.revision = 1; event.text = "合成"
        journal.submit(event)
        let seal = directory.appendingPathComponent("seal.json")
        try FileManager.default.createDirectory(at: seal, withIntermediateDirectories: false)
        journal.seal()
        var failed = false
        do { try await journal.flush() } catch { failed = true }
        try require(failed, "seal write failure must propagate to the helper")
        try FileManager.default.removeItem(at: seal)
        journal.seal()
        try await journal.flush()
        try require(try CaptureEventOutbox.Seal.read(from: directory).saved, "seal retry did not recover")
        try require(try CaptureEventOutbox.pending(in: directory).count == 1, "seal retry duplicated gap events")
    }

    private static func backlogPauseContinuesRecording() async throws {
        let directory = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let outbox = directory.appendingPathComponent("outbox", isDirectory: true)
        let sources: [AudioSourceKind] = [.system, .microphone]
        let system = FixtureSource()
        let microphone = FixtureSource()
        var sockets: [FixtureSocket] = []
        let core = AudioCore(makeSource: { kind, _ in kind == .system ? system : microphone }, makeSocket: { _ in
            let socket = FixtureSocket()
            sockets.append(socket)
            return socket
        }, requestMicrophoneAccess: { true })
        let manifest = CaptureEventOutbox.Manifest(meetingID: "pressure", epoch: "pressure", sources: sources)
        try manifest.write(to: outbox)
        let journal = CaptureEventOutbox.Writer(directory: outbox, manifest: manifest, eventLimit: 1) { [weak core] in
            Task { @MainActor [weak core] in core?.pauseRecognitionForDeliveryBacklog() }
        }
        var events: [AudioEvent] = []
        core.onEvent = { event in events.append(event); journal.submit(event) }
        try await core.start(.init(meetingID: "pressure", sources: sources, recordingDirectory: directory, captureEpoch: "pressure"),
                             speech: liveConfig(), lease: SpeechLease(seconds: 10))
        try await wait("backlog sockets run-task") { sockets.count == 2 && sockets.allSatisfy { $0.taskID != nil } }
        for socket in sockets { try socket.server("task-started") }
        let buffer = fixtureBuffer()
        system.play(buffer); microphone.play(buffer)
        try await wait("both recognizers received PCM before backlog") { sockets.allSatisfy { $0.frames == 1600 } }
        try sockets[1].sentence("积压前的临时结果", start: 0, end: nil, final: false, id: 1)
        try await wait("interim before backlog") { events.contains { $0.type == "transcript.interim" } }
        try await journal.flush()
        // Leave prior events unacknowledged so the real writer is over its
        // final-text quota when this result arrives.
        try sockets[0].sentence("无法递交的最终结果", start: 0, end: 100, final: true, id: 1)
        try await wait("outbox pressure pauses both streams") {
            sockets.allSatisfy(\.closed) && events.filter { $0.type == "asr.status" && $0.state == "paused" }.count == 2
        }
        try require(system.active && microphone.active && core.state == "running", "delivery pause stopped the device")
        for _ in 0..<4 { system.play(buffer); microphone.play(buffer) }
        await core.stop()
        journal.seal()
        try await journal.flush()
        try require(sockets.allSatisfy { $0.frames == 1600 }, "delivery pause sent more audio")
        try require(events.contains { $0.type == "transcript.retracted" && $0.source == .microphone }, "delivery pause left an interim active")
        let seal = try CaptureEventOutbox.Seal.read(from: outbox)
        try require(seal.saved && seal.hadAudio, "delivery pause left the recording unverified")
        let journaled = try CaptureEventOutbox.pending(in: outbox).map { url in
            try JSONDecoder().decode(AudioEvent.self, from: Data(contentsOf: url))
        }
        try require(!journaled.contains { $0.type == "transcript.final" }, "outbox didn't exceed the final-text quota")
        try require(journaled.contains { $0.type == "asr.gap" && $0.source == .system && $0.audioStartMS == 0 && $0.audioEndMS == 100 },
                    "outbox did not preserve the dropped final's interval")
        for kind in sources {
            let gapStart = kind == .system ? 100 : 0
            try require(journaled.contains {
                $0.type == "asr.gap" && $0.source == kind && $0.code == "delivery_backlog" &&
                    $0.audioStartMS == gapStart && $0.audioEndMS == 500
            }, "delivery backlog did not record the new missing interval for \(kind)")
            try require(journaled.contains {
                $0.type == "audio.recording" && $0.state == "saved" && $0.source == kind && $0.framesReceived == 8000
            }, "delivery pause lost audio for \(kind)")
            let caf = try AVAudioFile(forReading: directory.appendingPathComponent("01-原始音频-\(kind.rawValue)-pressure.caf"))
            try require(caf.length == 8000, "recording frames missing under pressure for \(kind)")
        }
    }

    private static func bothSourcesStopConcurrently() async throws {
        let directory = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let system = FixtureSource()
        let microphone = FixtureSource()
        var sockets: [FixtureSocket] = []
        let core = AudioCore(makeSource: { kind, _ in kind == .system ? system : microphone }, makeSocket: { _ in
            let socket = FixtureSocket()
            sockets.append(socket)
            return socket
        }, requestMicrophoneAccess: { true })
        var events: [AudioEvent] = []
        core.onEvent = { events.append($0) }
        try await core.start(.init(meetingID: "both", sources: [.system, .microphone], recordingDirectory: directory,
                                   captureEpoch: "both"), speech: liveConfig(), lease: SpeechLease(seconds: 15))
        try await wait("both sockets run independently") { sockets.count == 2 && sockets.allSatisfy { $0.taskID != nil } }
        for socket in sockets { try socket.server("task-started") }
        system.play(fixtureBuffer()); microphone.play(fixtureBuffer())
        try await wait("both sockets receive their own PCM") { sockets.allSatisfy { $0.frames == 1600 } }
        let start = ContinuousClock.now
        let stopping = Task { await core.stop() }
        try await wait("both finish commands arrived without waiting for any task-finished") {
            sockets.allSatisfy { $0.actions.last == "finish-task" }
        }
        try sockets[1].sentence("停止前的最后一句", start: 0, end: 100, final: true, id: 1)
        try sockets[1].server("task-finished")
        // The system socket never sends task-finished.
        await stopping.value
        try require(ContinuousClock.now - start < .seconds(5), "recognition exceeded the shared stop budget")
        try require(events.last?.type == "audio.status" && events.last?.state == "stopped", "stop preceded tail events")
        try require(events.contains { $0.type == "transcript.final" && $0.source == .microphone }, "microphone final lost")
        try require(events.contains { $0.type == "asr.gap" && $0.source == .system && $0.audioEndMS == 100 }, "timeout gap lost")
        for kind in [AudioSourceKind.system, .microphone] {
            try require(events.contains { $0.type == "audio.recording" && $0.state == "saved" && $0.source == kind }, "one recording lost")
            let caf = try AVAudioFile(forReading: directory.appendingPathComponent("01-原始音频-\(kind.rawValue)-both.caf"))
            try require(caf.length == 1600, "one input recording failed frame check")
        }
    }

    private static func fileRequests() async throws {
        let deepSeek = try config("deepseek", "https://api.deepseek.com/v1/tools/asr")
        let dashScope = try config("dashscope", "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation")
        let samples: [Int16] = [1, -125, 33]
        let wav = SpeechPCM.wav(samples)
        let request = try CloudFileRequest.build(config: deepSeek, pcm: samples)
        let body = request.httpBody!
        try require(request.httpMethod == "POST" && request.value(forHTTPHeaderField: "Authorization") == "Bearer fixture-only" &&
                    body.range(of: Data("name=\"file\"; filename=\"segment.wav\"".utf8)) != nil && body.range(of: wav) != nil,
                    "DeepSeek did not send the documented multipart WAV body")
        try require(String(decoding: wav.prefix(4), as: UTF8.self) == "RIFF" && wav.count == 50 &&
                    Array(wav[22..<28]) == [1, 0, 0x80, 0x3e, 0, 0], "WAV must be mono 16 kHz PCM")
        do {
            _ = try deepSeek.request(kind: "live")
            try require(false, "DeepSeek must never connect for live audio")
        } catch CloudSpeechError.invalidConfiguration { }
        let jsonRequest = try CloudFileRequest.build(config: dashScope, pcm: samples)
        let json = try JSONSerialization.jsonObject(with: jsonRequest.httpBody!) as! [String: Any]
        let input = json["input"] as! [String: Any]
        let message = (input["messages"] as! [[String: Any]])[0]
        let content = (message["content"] as! [[String: Any]])[0]
        let attachment = content["input_audio"] as! [String: String]
        try require(jsonRequest.value(forHTTPHeaderField: "X-DashScope-SSE") == "enable" &&
                    attachment["data"] == "data:audio/wav;base64," + wav.base64EncodedString(), "Flash SSE JSON body")
        let expected = "第一句。第二句。"
        let flash = Data("data: {\"output\":{\"text\":\"第一句。\"}}\n\ndata: {\"output\":{\"text\":\"\(expected)\"}}\n\ndata: [DONE]\n\n".utf8)
        try require(try CloudFileRequest.text(from: flash, provider: "dashscope") == expected, "Flash returned only its last sentence")
        let deepseek = Data("{\"request_id\":\"test\",\"text\":\"\(expected)\",\"duration_ms\":1}".utf8)
        try require(try CloudFileRequest.text(from: deepseek, provider: "deepseek") == expected, "DeepSeek whole text parsing")
        let lease = SpeechLease(seconds: 5)
        var requests = 0
        let worker = try CloudFileRecognition(config: deepSeek, lease: lease, transport: { request in
            requests += 1
            let status = requests == 1 ? 429 : 200
            return (deepseek, HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Retry-After": "0"])!)
        })
        defer { worker.close() }
        let text = try await worker.transcribe(samples)
        try require(text == expected && requests == 2, "file limit retry changed the response or attempted more than once")
        lease.revoke()
        do {
            _ = try await worker.transcribe(samples)
            try require(false, "revoked file recognition accepted another chunk")
        } catch CloudSpeechError.authorizationEnded { }
        try require(requests == 2, "revoked file sent another chunk")
        let format = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 2)!
        let stereo = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4800)!
        stereo.frameLength = 4800
        stereo.floatChannelData![0].initialize(repeating: 0, count: 4800)
        stereo.floatChannelData![1].initialize(repeating: 0.5, count: 4800)
        let pcm = SpeechPCM()
        let mono = try pcm.samples(stereo) + pcm.drain()
        try require(mono.count == 1600 && mono.contains { $0 > 500 }, "stereo conversion: \(mono.count) samples, peak \(mono.max() ?? 0)")
        let original = Array(repeating: Int16(600), count: 21) + Array(repeating: Int16(0), count: 8) + Array(repeating: Int16(601), count: 65)
        var chunker = CloudAudioChunker(rate: 10, minimumSeconds: 2, maximumSeconds: 6)
        var chunks: [[Int16]] = []
        for start in stride(from: 0, to: original.count, by: 13) {
            let batch = Array(original[start..<min(start + 13, original.count)])
            var input = 0
            while input < batch.count {
                if let chunk = chunker.next(batch, from: &input) { chunks.append(chunk) }
            }
        }
        if let last = chunker.finish() { chunks.append(last) }
        try require(chunks.map(\.count) == [29, 60, 5] && chunks.flatMap { $0 } == original && chunker.sentFrames == original.count,
                    "pause chunking dropped or duplicated PCM")
    }

    private static func filePipeline() async throws {
        let directory = try tempDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let audio = directory.appendingPathComponent("synthetic.wav")
        try SpeechPCM.wav(Array(repeating: Int16(600), count: 420 * SpeechPCM.rate)).write(to: audio)
        let config = try config("dashscope", "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation")
        var active = 0
        var maximumActive = 0
        var requests = 0
        var ends: [Int] = []
        var texts: [String] = []
        let worker = FileTranscription(cloudTransport: { request in
            requests += 1
            let index = requests
            active += 1
            maximumActive = max(maximumActive, active)
            defer { active -= 1 }
            // Force later requests to finish first. Publication must still
            // follow audio order and advance only the contiguous checkpoint.
            try await Task.sleep(for: .milliseconds(index == 1 ? 300 : index == 2 ? 150 : 50))
            return (Data("{\"output\":{\"text\":\"合成片段\(index)\"}}".utf8),
                    HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        })
        worker.onEvent = { event in
            if event.type == "transcript.final" { texts.append(event.text ?? "") }
            if event.type == "import.progress" { ends.append(event.audioEndMS ?? -1) }
        }
        let started = ContinuousClock.now
        try await worker.run(url: audio, meetingID: "synthetic-meeting", epoch: "fixture-file", speech: config,
                             lease: SpeechLease(seconds: 30))
        print("File pipeline: 420 seconds synthetic audio, \(requests) requests, maximum concurrency \(maximumActive), elapsed \(started.duration(to: .now))")
        try require(requests == 3 && maximumActive == 3, "DashScope file imports still use short serial requests")
        try require(texts == ["合成片段1", "合成片段2", "合成片段3"] && ends == [180000, 360000, 420000],
                    "out-of-order ASR completion reordered text or skipped a checkpoint")

        var failureRequests = 0
        var failureEnds: [Int] = []
        var failureTexts: [String] = []
        let failing = FileTranscription(cloudTransport: { request in
            failureRequests += 1
            let index = failureRequests
            try await Task.sleep(for: .milliseconds(index == 2 ? 80 : 10))
            return (Data("{\"output\":{\"text\":\"合成片段\(index)\"}}".utf8),
                    HTTPURLResponse(url: request.url!, statusCode: index == 2 ? 503 : 200, httpVersion: nil, headerFields: nil)!)
        })
        failing.onEvent = { event in
            if event.type == "import.progress" { failureEnds.append(event.audioEndMS ?? -1) }
            if event.type == "transcript.final" { failureTexts.append(event.text ?? "") }
        }
        do {
            try await failing.run(url: audio, meetingID: "synthetic-meeting", epoch: "fixture-file", speech: config,
                                  lease: SpeechLease(seconds: 30))
            try require(false, "failed middle chunk was marked as completed")
        } catch CloudSpeechError.unavailable { }
        try require(failureRequests == 3 && failureEnds == [180000] && failureTexts == ["合成片段1"],
                    "a later successful chunk advanced past the failed middle chunk")

        var resumedEvents: [AudioEvent] = []
        let resumed = FileTranscription(cloudTransport: { request in
            (Data("{\"output\":{\"text\":\"恢复合成片段\"}}".utf8),
             HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        })
        resumed.onEvent = { resumedEvents.append($0) }
        try await resumed.run(url: audio, meetingID: "synthetic-meeting", epoch: "fixture-file", speech: config,
                              lease: SpeechLease(seconds: 30), startMS: 180000, revision: 2, initialSequence: 17)
        try require(resumedEvents.map(\.seq) == [18, 19, 20, 21] &&
                    resumedEvents.filter { $0.type == "transcript.final" }.map { $0.audioStartMS ?? -1 } == [180000, 360000],
                    "resume reused old event IDs or restarted from the beginning of the recording")
        let alreadyRecognized = FileTranscription(cloudTransport: { _ in
            throw NSError(domain: "UnexpectedUploadAtEOF", code: 1)
        })
        var eofEvents = 0
        alreadyRecognized.onEvent = { _ in eofEvents += 1 }
        try await alreadyRecognized.run(url: audio, meetingID: "synthetic-meeting", epoch: "fixture-file", speech: config,
                                        lease: SpeechLease(seconds: 30), startMS: 420000, revision: 3, initialSequence: 21)
        try require(eofEvents == 0, "finalizing an EOF checkpoint uploaded or published recognized audio again")

        var cancelledRequests = 0
        let cancellation = FileTranscription(cloudTransport: { request in
            cancelledRequests += 1
            try await Task.sleep(for: .seconds(30))
            return (Data(), HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        })
        let cancelledRun = Task { @MainActor in
            try await cancellation.run(url: audio, meetingID: "synthetic-meeting", epoch: "fixture-file", speech: config,
                                       lease: SpeechLease(seconds: 30))
        }
        try await wait("parallel requests did not start", until: { cancelledRequests == 3 })
        let cancellationStart = ContinuousClock.now
        cancelledRun.cancel()
        do {
            try await cancelledRun.value
            try require(false, "cancelled file import completed")
        } catch is CancellationError { }
        try require(cancellationStart.duration(to: .now) < .seconds(1), "cancelling a file import waited for an entire network timeout")
        print("File pipeline recovery passed: middle failure preserves checkpoint, resume keeps sequence and audio offset, cancellation stops pending requests.")
    }
}
