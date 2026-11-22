import AppKit
import AVFoundation
import Foundation

/// Owns capture and local recording. Recognition uses the task's fixed speech
/// configuration and cannot stop the capture devices.
@MainActor
public final class AudioCore {
    public var onEvent: ((AudioEvent) -> Void)?
    public private(set) var state = "idle"
    private var config: AudioConfig?
    private var generation: UUID?
    private var epoch = UUID().uuidString
    private var controlSeq = 0
    private var streams: [CaptureStream] = []
    private var stopTask: Task<Void, Never>?
    private var sleepObserver: NSObjectProtocol?

    private let makeSource: (AudioSourceKind, AudioConfig) -> any CapturedAudioSource
    private let makeSocket: ((URLRequest) -> any DashScopeSocket)?
    private let requestMicrophoneAccess: () async -> Bool

    public convenience init() {
        self.init(makeSource: { kind, config in
            if kind == .system {
                let tap = SystemAudioTap()
                tap.includedProcessID = config.includedProcessID
                return tap
            }
            return MicrophoneSource()
        })
    }

    init(makeSource: @escaping (AudioSourceKind, AudioConfig) -> any CapturedAudioSource,
         makeSocket: ((URLRequest) -> any DashScopeSocket)? = nil,
         requestMicrophoneAccess: @escaping () async -> Bool = { await AVCaptureDevice.requestAccess(for: .audio) }) {
        self.requestMicrophoneAccess = requestMicrophoneAccess
        self.makeSource = makeSource
        self.makeSocket = makeSocket
    }

    public func start(_ requested: AudioConfig, speech: SpeechSessionConfig? = nil, lease: SpeechLease? = nil) async throws {
        #if !arch(arm64)
        throw AudioCoreError.unsupportedMachine
        #endif
        guard !requested.meetingID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              requested.meetingID.count <= 200, !requested.sources.isEmpty,
              Set(requested.sources).count == requested.sources.count, !requested.sources.contains(.file),
              requested.locale.count <= 80 else {
            throw AudioCoreError.invalidConfig("meeting_id, unique sources and a valid locale are required.")
        }
        if (state == "preparing" || state == "running") && config == requested {
            reportStatus(); return
        }
        guard state == "idle" || state == "stopped" else { throw AudioCoreError.alreadyRunning }
        let token = UUID()
        generation = token; epoch = requested.captureEpoch ?? UUID().uuidString; controlSeq = 0
        config = requested; state = "preparing"
        reportStatus()
        do {
            if requested.sources.contains(.microphone) {
                let permitted = await requestMicrophoneAccess()
                guard generation == token, !Task.isCancelled else { return }
                guard permitted else { throw AudioCoreError.microphoneDenied }
            }
            for kind in requested.sources {
                guard generation == token, !Task.isCancelled else { return }
                let stream = try CaptureStream(config: requested, epoch: epoch, kind: kind,
                                           source: makeSource(kind, requested),
                                           onEvent: { [weak self] event in self?.onEvent?(event) },
                                           onFailure: { [weak self] code, detail in
                    self?.failCapture(code: code, detail: detail)
                })
                streams.append(stream)
                try stream.start()
            }
            guard generation == token else { return }
            state = "running"
            sleepObserver = NSWorkspace.shared.notificationCenter.addObserver(
                forName: NSWorkspace.willSleepNotification, object: nil, queue: .main
            ) { [weak self] _ in
                Task { @MainActor in
                    guard self?.generation == token else { return }
                    self?.failCapture(code: "system_sleep", detail: "System sleep interrupted capture; restart creates a new epoch.")
                }
            }
            reportStatus()
            for stream in streams { stream.startRecognition(speech: speech, lease: lease, makeSocket: makeSocket) }
        } catch {
            guard generation == token else { return }
            await stop(graceful: false)
            throw error
        }
    }

    /// Device release occurs synchronously before the first await. Repeated stop is safe.
    public func stop(graceful: Bool = true) async {
        generation = nil
        if let sleepObserver { NSWorkspace.shared.notificationCenter.removeObserver(sleepObserver) }
        sleepObserver = nil
        if let stopTask { await stopTask.value; return }
        let finishing = streams
        streams = []
        for stream in finishing { stream.halt() }
        let deadline = ContinuousClock.now.advanced(by: .seconds(4))
        state = "stopping"
        reportStatus()
        let task = Task { @MainActor [self] in
            // Start both tasks before awaiting either so neither source consumes
            // the other's finalization time.
            let sourceTasks = finishing.map { stream in
                Task { @MainActor in await stream.finish(graceful: graceful, deadline: deadline) }
            }
            for sourceTask in sourceTasks { await sourceTask.value }
            stopTask = nil
            state = "stopped"
            reportStatus()
        }
        stopTask = task
        await task.value
    }

    public func pauseRecognitionForDeliveryBacklog() {
        for stream in streams { stream.pauseRecognitionForDeliveryBacklog() }
    }

    public func reportStatus() {
        var event = controlEvent(type: "audio.status")
        event.state = state
        event.code = config?.transcribe == false ? "capture_only" : nil
        event.detail = state == "running" ? "Local capture active. System audio permission cannot be inferred from silence." : nil
        onEvent?(event)
        for stream in streams { stream.reportLevel() }
    }

    public func reportError(code: String, detail: String) {
        var event = controlEvent(type: "audio.error")
        event.state = state; event.code = code; event.detail = detail
        onEvent?(event)
    }

    public func reportModelStatus(locale: String, status: String) {
        var event = controlEvent(type: "audio.model_status")
        event.state = status; event.detail = locale
        onEvent?(event)
    }

    private func failCapture(code: String, detail: String) {
        guard state == "running" || state == "preparing" else { return }
        var event = controlEvent(type: "audio.gap")
        event.code = code; event.detail = detail
        onEvent?(event)
        // Stop every input immediately; an error in one source must be visible.
        for stream in streams { stream.halt(discardBuffered: true) }
        Task { await stop(graceful: false) }
    }

    private func controlEvent(type: String) -> AudioEvent {
        controlSeq += 1
        return AudioEvent(meetingID: config?.meetingID ?? "", captureEpoch: epoch,
                          streamID: "control-\(epoch)", seq: controlSeq,
                          type: type, source: .system)
    }
}

@MainActor
private final class CaptureStream {
    private let config: AudioConfig
    private let epoch: String
    private let kind: AudioSourceKind
    private let onEvent: (AudioEvent) -> Void
    private let onFailure: (String, String) -> Void
    private let source: any CapturedAudioSource
    private var recognition: LiveCaptureRecognition?
    private var seq = 0
    private var inputTask: Task<Void, Never>?
    private var input: AsyncStream<AVAudioPCMBuffer>.Continuation?
    private var stopped = false
    private var failed = false
    private var recordingWriteFailed = false
    private var frames: UInt64 = 0
    private var nonSilentFrames: UInt64 = 0
    private var sampleRate: Double = 0
    private var lastRMS: Float = 0
    private var seconds = 0.0
    private var lastLevelSeconds = 0.0
    private let recording: CaptureRecording?

    init(config: AudioConfig, epoch: String, kind: AudioSourceKind, source: any CapturedAudioSource,
         onEvent: @escaping (AudioEvent) -> Void, onFailure: @escaping (String, String) -> Void) throws {
        self.config = config; self.epoch = epoch; self.kind = kind; self.source = source
        self.onEvent = onEvent; self.onFailure = onFailure
        self.recording = try config.recordingDirectory.map { try CaptureRecording(directory: $0, epoch: epoch, source: kind) }
    }

    func start() throws {
        let (buffers, continuation) = AsyncStream.makeStream(of: AVAudioPCMBuffer.self, bufferingPolicy: .bufferingOldest(128))
        input = continuation
        let gate = FailureGate()
        let fail: (String, String) -> Void = { [weak self] code, detail in
            guard gate.claim() else { return }
            Task { @MainActor in self?.fail(code: code, detail: detail) }
        }
        source.onAudio = { buffer in
            if case .dropped = continuation.yield(buffer) {
                fail("capture_overflow", AudioCoreError.inputOverflow.localizedDescription)
            }
        }
        source.onFailure = { fail("capture_failed", $0) }
        source.onRateChange = { fail("device_changed", "Audio device changed; restart capture to create a new epoch.") }
        inputTask = Task { [weak self] in
            guard let self else { return }
            for await buffer in buffers {
                guard !Task.isCancelled else { break }
                guard buffer.frameLength > 0 else { continue }
                if self.sampleRate > 0, self.sampleRate != buffer.format.sampleRate {
                    self.fail(code: "device_changed", detail: "Audio device changed; restart capture to create a new epoch.")
                    break
                }
                do { try self.recording?.append(buffer) }
                catch {
                    self.fail(code: "recording_failed", detail: "Local recording could not be written; capture stopped.")
                    break
                }
                self.observe(buffer)
                self.recognition?.receive(buffer, voiced: self.lastRMS > (self.kind == .system ? 0.002 : 0.01))
                await Task.yield()
            }
        }
        try source.start()
        var event = makeEvent(type: "audio.ready")
        event.state = "running"
        event.code = config.transcribe ? "transcription_requested" : "capture_only"
        event.detail = "Capture started; nonzero samples and transcription still require observation."
        onEvent(event)
    }

    func startRecognition(speech: SpeechSessionConfig?, lease: SpeechLease?,
                          makeSocket: ((URLRequest) -> any DashScopeSocket)?) {
        guard config.transcribe else { return }
        recognition = LiveCaptureRecognition(config: config, speech: speech, lease: lease, makeSocket: makeSocket,
            onEvent: { [weak self] type, provider, state, code, detail, interval in
                guard let self else { return }
                var event = self.makeEvent(type: type)
                event.provider = provider
                event.state = state
                event.code = code
                event.detail = detail
                event.audioStartMS = interval?.lowerBound
                event.audioEndMS = interval?.upperBound
                self.onEvent(event)
            }, onChange: { [weak self] change, provider in self?.emit(change, provider: provider) })
        recognition?.start()
    }

    func pauseRecognitionForDeliveryBacklog() { recognition?.pauseForDeliveryBacklog() }

    func halt(discardBuffered: Bool = false) {
        if discardBuffered { inputTask?.cancel() }
        guard !stopped else { return }
        stopped = true
        recognition?.prepareToStop()
        source.stop()
        input?.finish()
    }

    func finish(graceful: Bool, deadline: ContinuousClock.Instant) async {
        halt(discardBuffered: !graceful)
        await inputTask?.value
        // Close the file even if the recognition service never returns its tail.
        recording?.close()
        await recognition?.finish(graceful: graceful, deadline: deadline)
        if let recording {
            var event = makeEvent(type: "audio.recording")
            if recordingWriteFailed {
                event.state = "failed"
                event.code = "recording_failed"
                event.detail = "录音写入失败，无法确认保存。"
            } else if recording.frames == 0 && !failed {
                event.state = "missing"
            } else if recording.hasReadableFrames() {
                event.state = failed || !graceful ? "partial" : "saved"
            } else {
                event.state = "failed"
                event.code = "recording_unreadable"
                event.detail = "录音关闭后读取失败，无法确认保存。"
            }
            event.framesReceived = recording.frames
            event.sampleRate = recording.sampleRate
            event.audioEndMS = recording.sampleRate > 0 ? Int(Double(recording.frames) / recording.sampleRate * 1000) : 0
            onEvent(event)
        }
        reportLevel()
        var event = makeEvent(type: "audio.stopped")
        event.state = "stopped"; onEvent(event)
    }

    private func fail(code: String, detail: String) {
        guard !failed else { return }
        failed = true
        if stopped {
            // A disk error while draining capture must still mark the recording
            // failed, even though the hardware has already stopped.
            var event = makeEvent(type: "audio.error")
            event.code = code
            event.detail = detail
            onEvent(event)
            return
        }
        halt(discardBuffered: true)
        onFailure(code, "\(kind.rawValue): \(detail)")
    }

    private func observe(_ buffer: AVAudioPCMBuffer) {
        sampleRate = buffer.format.sampleRate
        let count = Int(buffer.frameLength)
        frames += UInt64(count)
        seconds += Double(count) / sampleRate
        if let samples = buffer.floatChannelData, count > 0 {
            var energy: Float = 0
            for index in 0..<count { energy += samples[0][index] * samples[0][index] }
            lastRMS = sqrt(energy / Float(count))
        } else { lastRMS = 0 }
        if lastRMS > 0.0001 { nonSilentFrames += UInt64(count) }
        if seconds - lastLevelSeconds >= 1 { reportLevel(); lastLevelSeconds = seconds }
    }

    func reportLevel() {
        var event = makeEvent(type: "audio.level")
        event.rms = lastRMS; event.framesReceived = frames
        event.nonSilentFrames = nonSilentFrames; event.sampleRate = sampleRate
        event.audioEndMS = Int((seconds * 1000).rounded())
        onEvent(event)
    }

    private func emit(_ change: TranscriptChange, provider: String) {
        var event = makeEvent(type: change.type)
        event.segmentID = change.segmentID; event.revision = change.revision
        event.supersedesRevision = change.supersedesRevision
        event.audioStartMS = change.startMS; event.audioEndMS = change.endMS
        event.text = change.text; event.provider = provider
        onEvent(event)
    }

    private func makeEvent(type: String) -> AudioEvent {
        seq += 1
        return AudioEvent(meetingID: config.meetingID, captureEpoch: epoch,
                          streamID: "\(kind.rawValue)-\(epoch)", seq: seq, type: type, source: kind)
    }
}

/// Limits a failing render callback to one pending main-actor notification.
private final class FailureGate: @unchecked Sendable {
    private let lock = NSLock()
    private var failed = false
    func claim() -> Bool {
        lock.lock(); defer { lock.unlock() }
        if failed { return false }
        failed = true; return true
    }
}
