import AppKit
import AVFoundation
import Foundation

/// Owns capture, explicitly requested local recordings, and local transcription.
/// It never reads preferences, calls a model, or uploads meeting data.
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

    public init() {}

    public func start(_ requested: AudioConfig) async throws {
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
            if requested.transcribe {
                let model = await LocalSpeechModels.status(locale: requested.locale)
                guard generation == token, !Task.isCancelled else { return }
                guard model == "installed" else { throw AudioCoreError.modelUnavailable(model) }
            }
            if requested.sources.contains(.microphone) {
                let permitted = await AVCaptureDevice.requestAccess(for: .audio)
                guard generation == token, !Task.isCancelled else { return }
                guard permitted else { throw AudioCoreError.microphoneDenied }
            }
            for kind in requested.sources {
                let recognizer = requested.transcribe ? try await LocalRecognition.start(locale: requested.locale) : nil
                guard generation == token, !Task.isCancelled else {
                    await recognizer?.cancel(); return
                }
                let stream = try CaptureStream(config: requested, epoch: epoch, kind: kind, recognizer: recognizer,
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
        state = "stopping"
        reportStatus()
        let task = Task { @MainActor [self] in
            for stream in finishing { await stream.finish(graceful: graceful) }
            stopTask = nil
            state = "stopped"
            reportStatus()
        }
        stopTask = task
        await task.value
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
    private let recognizer: LocalRecognition?
    private let onEvent: (AudioEvent) -> Void
    private let onFailure: (String, String) -> Void
    private let source: any CapturedAudioSource
    private var reducer = TranscriptReducer()
    private var seq = 0
    private var inputTask: Task<Void, Never>?
    private var resultTask: Task<Void, Never>?
    private var finalizationTask: Task<Void, Never>?
    private var input: AsyncStream<AVAudioPCMBuffer>.Continuation?
    private var stopped = false
    private var failed = false
    private var frames: UInt64 = 0
    private var nonSilentFrames: UInt64 = 0
    private var sampleRate: Double = 0
    private var lastRMS: Float = 0
    private var seconds = 0.0
    private var lastLevelSeconds = 0.0
    private let recording: CaptureRecording?

    init(config: AudioConfig, epoch: String, kind: AudioSourceKind, recognizer: LocalRecognition?,
         onEvent: @escaping (AudioEvent) -> Void, onFailure: @escaping (String, String) -> Void) throws {
        self.config = config; self.epoch = epoch; self.kind = kind; self.recognizer = recognizer
        self.onEvent = onEvent; self.onFailure = onFailure
        self.recording = try config.recordingDirectory.map { try CaptureRecording(directory: $0, epoch: epoch, source: kind) }
        if kind == .system {
            let tap = SystemAudioTap()
            tap.includedProcessID = config.includedProcessID
            self.source = tap
        } else { self.source = MicrophoneSource() }
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
            let converter = PCMConverter()
            var appendedSeconds = 0.0
            var silence = 0.0
            var voiced = 0.0
            var lastFinalized = 0.0
            for await buffer in buffers {
                guard !Task.isCancelled else { break }
                do { try self.recording?.append(buffer) }
                catch {
                    self.fail(code: "recording_failed", detail: "Local recording could not be written; capture stopped."); break
                }
                self.observe(buffer)
                guard let recognizer = self.recognizer else { continue }
                guard let converted = converter.convert(buffer, to: recognizer.format) else {
                    self.fail(code: "conversion_failed", detail: AudioCoreError.conversionFailed.localizedDescription); break
                }
                do {
                    try recognizer.append(converted)
                    let duration = Double(converted.frameLength) / converted.format.sampleRate
                    appendedSeconds += duration
                    if self.lastRMS > (self.kind == .system ? 0.002 : 0.01) {
                        voiced += duration; silence = 0
                    } else { silence += duration }
                    // Finalize at pauses or after 12 seconds, without ending capture.
                    if !self.stopped && self.finalizationTask == nil && voiced >= 0.25 && ((silence >= 0.8 && appendedSeconds - lastFinalized >= 1) || appendedSeconds - lastFinalized >= 12) {
                        self.requestFinalization(through: appendedSeconds, recognizer: recognizer)
                        lastFinalized = appendedSeconds; voiced = 0; silence = 0
                    }
                } catch {
                    if !self.stopped { self.fail(code: "recognition_failed", detail: error.localizedDescription) }
                    break
                }
            }
        }
        if let recognizer {
            resultTask = Task { [weak self] in
                guard let self else { return }
                do {
                    for try await piece in recognizer.results {
                        if Task.isCancelled { break }
                        for change in try self.reducer.apply(piece) { self.emit(change) }
                    }
                } catch {
                    if !self.stopped { self.fail(code: "recognition_failed", detail: error.localizedDescription) }
                }
            }
        }
        try source.start()
        var event = makeEvent(type: "audio.ready")
        event.state = "running"
        event.code = recognizer == nil ? "capture_only" : "local_transcription"
        event.detail = "Capture started; nonzero samples and transcription still require observation."
        onEvent(event)
    }

    func halt(discardBuffered: Bool = false) {
        if discardBuffered { inputTask?.cancel() }
        guard !stopped else { return }
        stopped = true
        source.stop()
        input?.finish()
    }

    private func requestFinalization(through seconds: Double, recognizer: LocalRecognition) {
        // finalize may wait for following audio. Awaiting it inside the input loop
        // stalls that same audio and can deadlock a stop at this boundary.
        finalizationTask = Task { [weak self] in
            do { try await recognizer.finalize(through: seconds) }
            catch {
                if let self, !self.stopped { self.fail(code: "finalization_failed", detail: error.localizedDescription) }
            }
            self?.finalizationTask = nil
        }
    }

    func finish(graceful: Bool) async {
        halt(discardBuffered: !graceful)
        // A timer requests cancellation if the final tail takes too long. Input has
        // already stopped; framework cancellation itself is not a hard deadline.
        if let recognizer {
            let timeout = Task {
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
                inputTask?.cancel()
                var event = makeEvent(type: "audio.gap")
                event.code = "finalization_timeout"; event.detail = "Final transcription tail exceeded 3 seconds; remaining input cancelled."
                onEvent(event)
                await recognizer.cancel()
            }
            await inputTask?.value
            if graceful {
                do { try await recognizer.finish() }
                catch {
                    var event = makeEvent(type: "audio.gap")
                    event.code = "finalization_interrupted"; event.detail = error.localizedDescription
                    onEvent(event)
                }
            } else { await recognizer.cancel() }
            timeout.cancel()
        } else { await inputTask?.value }
        await finalizationTask?.value
        await resultTask?.value
        for change in reducer.retractOpen() { emit(change) }
        if let recording {
            recording.close()
            var saved = makeEvent(type: "audio.recording")
            saved.state = recording.frames > 0 ? (failed || !graceful ? "partial" : "saved") : "missing"
            saved.framesReceived = recording.frames; saved.sampleRate = recording.sampleRate
            saved.audioEndMS = recording.sampleRate > 0 ? Int(Double(recording.frames) / recording.sampleRate * 1000) : 0
            onEvent(saved)
        }
        reportLevel()
        var event = makeEvent(type: "audio.stopped")
        event.state = "stopped"; onEvent(event)
    }

    private func fail(code: String, detail: String) {
        guard !failed, !stopped else { return }
        failed = true
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

    private func emit(_ change: TranscriptChange) {
        var event = makeEvent(type: change.type)
        event.segmentID = change.segmentID; event.revision = change.revision
        event.supersedesRevision = change.supersedesRevision
        event.audioStartMS = change.startMS; event.audioEndMS = change.endMS
        event.text = change.text; onEvent(event)
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
