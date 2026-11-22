import AVFoundation
import Foundation

/// One recognizer per source. It receives only audio the capture stream has
/// already processed for recording, and cannot fail or stop that stream.
@MainActor
final class LiveCaptureRecognition {
    private struct Input {
        let buffer: AVAudioPCMBuffer
        let start: Int
        let voiced: Bool
        var end: Int { start + Int(buffer.frameLength) }
    }

    private let config: AudioConfig
    private let speech: SpeechSessionConfig?
    private let lease: SpeechLease?
    private let makeSocket: ((URLRequest) -> any DashScopeSocket)?
    private let onEvent: (String, String, String?, String?, String?, ClosedRange<Int>?) -> Void
    private let onChange: (TranscriptChange, String) -> Void
    private let clock = ContinuousClock()
    private var provider: String
    private var generation = 0
    private var cloud: DashScopeRecognition?
    private var apple: LocalRecognition?
    private var cloudPCM: SpeechPCM?
    private var applePCM = PCMConverter()
    private var reducer = TranscriptReducer()
    private var connecting: Task<Void, Never>?
    private var pump: Task<Void, Never>?
    private var recoveryLeaseHandler: UUID?
    private var results: Task<Void, Never>?
    private var finalize: Task<Void, Never>?
    private var retryAttempts = 0
    private var stopping = false
    private var ready = false
    private var finishDeadline: ContinuousClock.Instant?
    private var pausedCode: String?
    private var captureRate = 0.0
    private var capturedFrames = 0
    private var nextFrame = 0
    private var generationOriginMS = 0
    private var confirmedThroughMS = 0
    private var accountedThroughMS = 0
    private var tail: [Input] = []
    private var open: [String: TranscriptChange] = [:]
    private var appendedSeconds = 0.0
    private var voiceSeconds = 0.0
    private var silenceSeconds = 0.0
    private var finalizedSeconds = 0.0

    init(config: AudioConfig, speech: SpeechSessionConfig?, lease: SpeechLease?,
         makeSocket: ((URLRequest) -> any DashScopeSocket)?,
         onEvent: @escaping (String, String, String?, String?, String?, ClosedRange<Int>?) -> Void,
         onChange: @escaping (TranscriptChange, String) -> Void) {
        self.config = config
        self.speech = speech
        self.lease = lease
        self.makeSocket = makeSocket
        self.onEvent = onEvent
        self.onChange = onChange
        provider = speech == nil ? "apple_local" : "dashscope"
    }

    func start() {
        if let speech {
            startCloud(speech, deadline: clock.now.advanced(by: .seconds(5)))
        } else {
            startApple(fallback: false)
        }
    }

    func receive(_ buffer: AVAudioPCMBuffer, voiced: Bool) {
        captureRate = buffer.format.sampleRate
        let input = Input(buffer: buffer, start: capturedFrames, voiced: voiced)
        capturedFrames = input.end
        guard pausedCode == nil else { return }
        tail.append(input)
        let cutoff = max(0, capturedFrames - Int(10 * captureRate))
        if let first = tail.firstIndex(where: { $0.end > cutoff }), first > 0 { tail.removeFirst(first) }
        schedulePump()
    }

    private func startCloud(_ speech: SpeechSessionConfig, deadline: ContinuousClock.Instant) {
        do {
            guard config.recordingDirectory != nil, let lease else { throw CloudSpeechError.invalidConfiguration }
            let session = try DashScopeRecognition(config: speech, lease: lease, makeSocket: makeSocket)
            let token = generation
            cloud = session
            cloudPCM = SpeechPCM()
            onEvent("asr.status", provider, "connecting", nil, nil, nil)
            session.onStarted = { [weak self] in
                guard let self, self.generation == token else { return }
                // Reserve a packet of headroom in the five-second send queue.
                self.becomeReady(replaySeconds: 4.8, state: "running")
            }
            session.onChange = { [weak self] change in
                guard let self, self.generation == token else { return }
                self.emit(change)
            }
            session.onFailure = { [weak self, weak session] error in
                guard let self, self.generation == token else { return }
                self.cloudFailed(error, retryAfter: session?.retryAfterSeconds)
            }
            session.start(until: deadline)
        } catch {
            cloudFailed(CloudSpeechError.openNetwork(error), retryAfter: nil)
        }
    }

    private func cloudFailed(_ error: CloudSpeechError, retryAfter: Double?) {
        report(error)
        invalidate()
        guard !stopping, let speech else { pause(code: error.code); return }
        let delay = max(retryAfter ?? 0, min(2, 0.5 * pow(2, Double(retryAttempts))))
        if retryAttempts < 3, error.retryable, delay <= 5, lease?.isValid == true {
            retryAttempts += 1
            onEvent("asr.status", provider, "connecting", error.code, "录音继续，正在重连语音服务。", nil)
            let token = generation
            connecting = Task { [weak self] in
                do { try await Task.sleep(for: .seconds(delay)) } catch { return }
                guard let self, self.generation == token, !self.stopping else { return }
                self.connecting = nil
                self.lease?.removeHandler(self.recoveryLeaseHandler)
                self.recoveryLeaseHandler = nil
                self.startCloud(speech, deadline: self.clock.now.advanced(by: .seconds(5)))
            }
            recoveryLeaseHandler = lease?.onRevoke { [weak self] in
                guard let self, self.generation == token else { return }
                self.cloudFailed(.authorizationEnded, retryAfter: nil)
            }
        } else if speech.fallbackToApple {
            startApple(fallback: true)
        } else {
            pause(code: error.code)
        }
    }

    private func startApple(fallback: Bool) {
        provider = "apple_local"
        onEvent("asr.status", provider, "connecting", nil, nil, nil)
        let token = generation
        let locale = config.locale
        connecting = Task { [weak self] in
            do {
                let status = await LocalSpeechModels.status(locale: locale)
                guard !Task.isCancelled, self?.generation == token else { return }
                guard status == "installed" else { throw AudioCoreError.modelUnavailable(status) }
                let recognition = try await LocalRecognition.start(locale: locale)
                guard !Task.isCancelled, let self, self.generation == token else {
                    await recognition.cancel()
                    return
                }
                self.connecting = nil
                self.apple = recognition
                self.becomeReady(replaySeconds: 10, state: fallback ? "degraded" : "running")
                self.results = Task { [weak self] in
                    do {
                        for try await piece in recognition.results {
                            guard let self, !Task.isCancelled, self.generation == token else { return }
                            for change in try self.reducer.apply(piece) { self.emit(change) }
                        }
                        if let self, self.generation == token, !self.stopping {
                            self.appleFailed(code: "speech_inactive", detail: "Apple 中文识别已停止。")
                        }
                    } catch {
                        guard let self, self.generation == token else { return }
                        self.appleFailed(code: "speech_recognition_failed", detail: "Apple 中文识别返回错误。")
                    }
                }
            } catch {
                guard let self, self.generation == token, !Task.isCancelled else { return }
                let detail: String
                if case AudioCoreError.modelUnavailable = error {
                    detail = "Apple 中文模型尚未就绪，请在语音转写设置中检查。"
                } else {
                    detail = "Apple 中文识别无法启动。"
                }
                self.appleFailed(code: "speech_model_unavailable", detail: detail)
            }
        }
    }

    private func becomeReady(replaySeconds: Double, state: String) {
        let replayFloor = max(confirmedThroughMS, accountedThroughMS)
        let retainedStart = max(0, capturedFrames - Int(replaySeconds * captureRate))
        let floorFrame = Int((Double(replayFloor) * captureRate / 1000).rounded(.up))
        nextFrame = min(capturedFrames, max(floorFrame, retainedStart, tail.first?.start ?? 0))
        generationOriginMS = milliseconds(nextFrame)
        gap(from: replayFloor, to: generationOriginMS, code: "speech_replay_window")
        ready = true
        onEvent("asr.status", provider, state, nil, nil, nil)
        schedulePump()
    }

    private func schedulePump() {
        guard ready, nextFrame < capturedFrames, pump == nil else { return }
        let token = generation
        pump = Task { [weak self] in
            while !Task.isCancelled, let self, self.generation == token, self.nextFrame < self.capturedFrames {
                if let deadline = self.finishDeadline, self.clock.now >= deadline { break }
                do { try self.sendNext() }
                catch {
                    // Lease expiration can synchronously invalidate this generation.
                    guard self.generation == token else { return }
                    if let cloud = self.cloud {
                        let failure = (error as? CloudSpeechError) ?? .invalidAudio
                        self.cloudFailed(failure, retryAfter: cloud.retryAfterSeconds)
                    } else {
                        self.appleFailed(code: "speech_input_failed", detail: "无法向 Apple 中文识别传入录音。")
                    }
                    return
                }
                await Task.yield()
            }
            if let self, self.generation == token { self.pump = nil }
        }
    }

    private func sendNext() throws {
        let earliest = max(0, capturedFrames - Int(10 * captureRate))
        guard nextFrame >= earliest, let input = tail.first(where: { $0.end > nextFrame }), input.start <= nextFrame else {
            throw CloudSpeechError.inputOverflow
        }
        let offset = nextFrame - input.start
        let buffer = try suffix(input.buffer, after: offset)
        if let cloud, let cloudPCM {
            let samples = try cloudPCM.samples(buffer)
            if !samples.isEmpty { try cloud.append(SpeechPCM.bytes(samples)) }
        } else if let apple {
            guard let audio = applePCM.convert(buffer, to: apple.format) else { throw AudioCoreError.conversionFailed }
            try apple.append(audio)
            scheduleAppleFinalize(apple, length: Double(audio.frameLength) / audio.format.sampleRate, voiced: input.voiced)
        }
        nextFrame = input.end
    }

    private func suffix(_ buffer: AVAudioPCMBuffer, after offset: Int) throws -> AVAudioPCMBuffer {
        guard offset > 0 else { return buffer }
        let count = buffer.frameLength - AVAudioFrameCount(offset)
        guard let result = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: count) else {
            throw AudioCoreError.conversionFailed
        }
        result.frameLength = count
        let stride = Int(buffer.format.streamDescription.pointee.mBytesPerFrame)
        let inputs = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
        let outputs = UnsafeMutableAudioBufferListPointer(result.mutableAudioBufferList)
        for (input, output) in zip(inputs, outputs) {
            guard let source = input.mData, let target = output.mData else { throw AudioCoreError.conversionFailed }
            memcpy(target, source.advanced(by: offset * stride), Int(count) * stride)
        }
        return result
    }

    private func scheduleAppleFinalize(_ apple: LocalRecognition, length: Double, voiced: Bool) {
        appendedSeconds += length
        if voiced {
            voiceSeconds += length
            silenceSeconds = 0
        } else { silenceSeconds += length }
        let elapsed = appendedSeconds - finalizedSeconds
        guard !stopping, finalize == nil, voiceSeconds >= 0.25,
              (silenceSeconds >= 0.8 && elapsed >= 1) || elapsed >= 12 else { return }
        finalizedSeconds = appendedSeconds
        voiceSeconds = 0
        silenceSeconds = 0
        let boundary = appendedSeconds
        let token = generation
        finalize = Task { [weak self] in
            do { try await apple.finalize(through: boundary) }
            catch {
                if let self, self.generation == token, !self.stopping {
                    self.appleFailed(code: "speech_finalization_failed", detail: "Apple 中文识别无法定稿。")
                }
            }
            if let self, self.generation == token { self.finalize = nil }
        }
    }

    private func emit(_ change: TranscriptChange) {
        let value = TranscriptChange(type: change.type, segmentID: "asr\(generation)-\(change.segmentID)",
                                     revision: change.revision, supersedesRevision: change.supersedesRevision,
                                     text: String(String.UnicodeScalarView(change.text.unicodeScalars.prefix(4500))),
                                     startMS: generationOriginMS + change.startMS,
                                     endMS: min(milliseconds(capturedFrames), generationOriginMS + change.endMS))
        guard value.startMS <= value.endMS else { return }
        if value.type == "transcript.interim" {
            open[value.segmentID] = value
        } else {
            open.removeValue(forKey: value.segmentID)
            if value.type == "transcript.final", value.endMS > confirmedThroughMS {
                confirmedThroughMS = value.endMS
                retryAttempts = 0
            }
        }
        onChange(value, provider)
    }

    private func report(_ error: CloudSpeechError) {
        onEvent("asr.error", provider, nil, error.code, error.localizedDescription, nil)
    }

    private func appleFailed(code: String, detail: String) {
        onEvent("asr.error", provider, nil, code, detail, nil)
        invalidate()
        pause(code: code)
    }

    private func invalidate(cancelSession: Bool = true) {
        generation += 1
        ready = false
        connecting?.cancel(); connecting = nil
        lease?.removeHandler(recoveryLeaseHandler)
        recoveryLeaseHandler = nil
        pump?.cancel(); pump = nil
        results?.cancel(); results = nil
        finalize?.cancel(); finalize = nil
        for change in open.values.sorted(by: { $0.startMS < $1.startMS }) {
            onChange(TranscriptChange(type: "transcript.retracted", segmentID: change.segmentID,
                                      revision: change.revision + 1, supersedesRevision: change.revision,
                                      text: "", startMS: change.startMS, endMS: change.endMS), provider)
        }
        open.removeAll()
        if cancelSession {
            cloud?.cancel()
            if let apple { Task { await apple.cancel() } }
        }
        cloud = nil
        cloudPCM = nil
        apple = nil
    }

    private func pause(code: String) {
        guard pausedCode == nil else { return }
        pausedCode = code
        tail.removeAll()
        onEvent("asr.status", provider, "paused", code, "转写暂停，录音继续。", nil)
    }

    func pauseForDeliveryBacklog() {
        guard pausedCode == nil else { return }
        invalidate()
        pause(code: "delivery_backlog")
    }

    func prepareToStop() { stopping = true }

    func finish(graceful: Bool, deadline: ContinuousClock.Instant) async {
        prepareToStop()
        finishDeadline = deadline
        if graceful {
            // Allow a connection already in progress to finish opening within
            // this stop. No new connection is started during shutdown.
            while !ready, pausedCode == nil, clock.now < deadline,
                  cloud != nil || (provider == "apple_local" && connecting != nil) {
                do { try await clock.sleep(until: min(deadline, clock.now.advanced(by: .milliseconds(20)))) }
                catch { break }
            }
        }
        var failureCode = pausedCode
        var completed = false
        if graceful && failureCode == nil && ready {
            await pump?.value
            if nextFrame < capturedFrames || clock.now >= deadline {
                failureCode = pausedCode ?? CloudSpeechError.timeout.code
            } else if let cloud, let cloudPCM {
                do {
                    let remaining = try cloudPCM.drain()
                    if !remaining.isEmpty { try cloud.append(SpeechPCM.bytes(remaining)) }
                    try await cloud.finish(until: deadline)
                    completed = true
                } catch { failureCode = CloudSpeechError.openNetwork(error).code }
            } else if let apple {
                completed = await finishApple(apple, deadline: deadline) && open.isEmpty && pausedCode == nil
                if !completed { failureCode = pausedCode ?? "speech_finalization_incomplete" }
            }
        }
        if !completed {
            gap(from: max(confirmedThroughMS, accountedThroughMS), to: milliseconds(capturedFrames),
                code: failureCode ?? "speech_finalization_interrupted")
        }
        invalidate(cancelSession: !completed)
        tail.removeAll()
    }

    private func finishApple(_ apple: LocalRecognition, deadline: ContinuousClock.Instant) async -> Bool {
        let (completion, signal) = AsyncStream.makeStream(of: Bool.self, bufferingPolicy: .bufferingOldest(1))
        let reader = results
        let worker = Task {
            do {
                try await apple.finish()
                await reader?.value
                signal.yield(true)
            } catch { signal.yield(false) }
        }
        let timer = Task { [clock] in
            do { try await clock.sleep(until: deadline) } catch { return }
            signal.yield(false)
        }
        var iterator = completion.makeAsyncIterator()
        let complete = await iterator.next() ?? false
        timer.cancel()
        worker.cancel()
        signal.finish()
        return complete
    }

    private func milliseconds(_ frames: Int) -> Int {
        captureRate > 0 ? Int((Double(frames) * 1000 / captureRate).rounded()) : 0
    }

    private func gap(from start: Int, to end: Int, code: String) {
        guard end > start else { return }
        accountedThroughMS = max(accountedThroughMS, end)
        onEvent("asr.gap", provider, nil, code, nil, start...end)
    }
}
