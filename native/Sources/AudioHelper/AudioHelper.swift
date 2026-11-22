import AppKit
import AudioCore
import CryptoKit
import Foundation

private struct Command: Decodable {
    let command: String
    var meetingID: String?
    var source: AudioSourceKind?
    var sources: [AudioSourceKind]?
    var locale: String?
    var allowDownload: Bool?
    var seconds: Int?
    var processID: Int32?
    var recordingDirectory: String?
    var captureEpoch: String?
    var outbox: String?
    var speech: SpeechSessionConfig?
    var leaseExpiresUptime: Double?
    var testID: String?
    var sampleVersion: String?
    var kind: String?
    enum CodingKeys: String, CodingKey {
        case command, source, sources, locale, seconds, outbox, speech, kind
        case leaseExpiresUptime = "lease_expires_uptime", testID = "test_id"
        case sampleVersion = "sample_version"
        case meetingID = "meeting_id", allowDownload = "allow_download"
        case processID = "process_id"
        case recordingDirectory = "recording_directory", captureEpoch = "capture_epoch"
    }
}

/// Pipe writes stay off the capture actor. Durable events use the outbox;
/// the pipe is for display updates, control notices and probe results.
private final class JSONLWriter: @unchecked Sendable {
    private let queue = DispatchQueue(label: "com.zsjh.audio.jsonl")
    private let lock = NSLock()
    private var pending = 0
    private var latest: [String: Data] = [:]
    private var failed = false
    var onFailure: (() -> Void)?

    func write(_ event: AudioEvent) {
        guard let data = try? JSONEncoder().encode(event) else { return }
        let key = CaptureEventOutbox.replaceable(event.type)
            ? "\(event.streamID)/\(event.type)/\(event.segmentID ?? "")" : nil
        write(data, replacing: key)
    }

    func write(_ value: [String: Any]) {
        if let data = try? JSONSerialization.data(withJSONObject: value) { write(data, replacing: nil) }
    }

    private func write(_ payload: Data, replacing key: String?) {
        lock.lock()
        defer { lock.unlock() }
        guard !failed else { return }
        if let key {
            if latest[key] != nil { latest[key] = payload; return }
            guard latest.count < 32 else { return }
            latest[key] = payload
        } else {
            guard pending + payload.count <= 2_000_000 else { return }
            pending += payload.count
        }
        queue.async { [self] in
            lock.lock()
            let value = key.flatMap { latest.removeValue(forKey: $0) } ?? payload
            let outputAvailable = !failed
            lock.unlock()
            if outputAvailable {
                var line = value
                line.append(10)
                do { try FileHandle.standardOutput.write(contentsOf: line) }
                catch {
                    lock.lock(); let notify = !failed; failed = true; lock.unlock()
                    if notify { onFailure?() }
                }
            }
            if key == nil { lock.lock(); pending -= payload.count; lock.unlock() }
        }
    }

    func flush() async { await withCheckedContinuation { continuation in queue.async { continuation.resume() } } }
}

private func sampleURL() throws -> URL {
    guard let sample = Bundle.main.url(forResource: "speech-probe-zh-CN", withExtension: "wav"),
          FileManager.default.isReadableFile(atPath: sample.path) else {
        throw AudioCoreError.invalidConfig("缺少测试音频，请重新安装应用。")
    }
    return sample
}

@MainActor
private final class Host {
    let core = AudioCore()
    let writer = JSONLWriter()
    var startTask: Task<Void, Never>?
    var closing = false
    private var outbox: CaptureEventOutbox.Writer?
    private var lease: SpeechLease?
    private var probe: Task<Void, Never>?
    private var currentEpoch: String?
    private var probeID: String?

    init() {
        core.onEvent = { [weak self] event in
            guard let self else { return }
            self.outbox?.submit(event)
            self.writer.write(event)
            if event.type == "audio.status", event.state == "stopped" {
                // The core may report a startup error immediately after a
                // failed stop. Schedule the seal after this delivery turns over.
                Task { @MainActor [journal = self.outbox, lease = self.lease] in
                    journal?.seal()
                    lease?.revoke()
                }
            }
        }
        writer.onFailure = {
            FileHandle.standardError.write(Data("Audio display pipe unavailable; reliable events remain in the capture outbox.\n".utf8))
        }
    }

    private func startProbe(_ command: Command) {
        guard probe == nil, let id = command.testID, let kind = command.kind,
              let config = command.speech, ["live", "file"].contains(kind) else { return }
        probeID = id
        probe = Task { @MainActor [weak self] in
            guard let self else { return }
            let start = ContinuousClock.now
            var report: [String: Any] = ["type": "speech.probe", "test_id": id, "succeeded": false, "text": ""]
            do {
                let sample = try sampleURL()
                let contents = try Data(contentsOf: sample, options: .mappedIfSafe)
                let version = SHA256.hash(data: contents).map { String(format: "%02x", $0) }.joined()
                guard version == command.sampleVersion else {
                    throw AudioCoreError.invalidConfig("测试音频与当前版本不一致，请重新安装应用。")
                }
                report["sample_version"] = version
                report["text"] = try await SpeechProbe.run(config: config, kind: kind, sampleURL: sample)
                report["succeeded"] = true
            } catch is CancellationError {
                report["error"] = "测试已取消。"
            } catch {
                report["error"] = error.localizedDescription
            }
            let elapsed = start.duration(to: .now).components
            report["duration_ms"] = max(0, Int(elapsed.seconds) * 1000 + Int(elapsed.attoseconds / 1_000_000_000_000_000))
            self.writer.write(report)
            self.probeID = nil
            self.probe = nil
        }
    }

    private func remainingLease(_ command: Command) -> Double {
        max(0, (command.leaseExpiresUptime ?? 0) - ProcessInfo.processInfo.systemUptime)
    }

    func dispatch(_ line: String) {
        guard !closing else { return }
        guard line.utf8.count <= 16_384, let data = line.data(using: .utf8) else {
            core.reportError(code: "invalid_command", detail: "Command exceeds 16 KiB."); return
        }
        do {
            let command = try JSONDecoder().decode(Command.self, from: data)
            switch command.command {
            case "capture_probe":
                guard let meeting = command.meetingID, let processID = command.processID,
                      processID > 0, let seconds = command.seconds, (1...30).contains(seconds) else {
                    core.reportError(code: "invalid_command", detail: "capture_probe requires meeting_id, synthetic process_id, and seconds:1...30."); return
                }
                startTask = Task { [self] in
                    guard !closing, !Task.isCancelled else { return }
                    do {
                        try await core.start(AudioConfig(meetingID: meeting, transcribe: false, includedProcessID: processID,
                            recordingDirectory: command.recordingDirectory.map { URL(fileURLWithPath: $0, isDirectory: true) },
                            captureEpoch: command.captureEpoch))
                        try await Task.sleep(for: .seconds(seconds))
                        await core.stop()
                    } catch is CancellationError { }
                    catch { core.reportError(code: "capture_probe_failed", detail: error.localizedDescription) }
                }
            case "start":
                guard let meeting = command.meetingID else {
                    core.reportError(code: "invalid_command", detail: "start requires meeting_id."); return
                }
                currentEpoch = command.captureEpoch
                let sources = command.sources ?? [command.source ?? .system]
                if let directory = command.outbox, let epoch = command.captureEpoch {
                    outbox = CaptureEventOutbox.Writer(directory: URL(fileURLWithPath: directory, isDirectory: true),
                        manifest: .init(meetingID: meeting, epoch: epoch, sources: sources)) { [weak self] in
                        Task { @MainActor [weak self] in
                            guard let self, self.currentEpoch == epoch else { return }
                            self.core.pauseRecognitionForDeliveryBacklog()
                        }
                    }
                } else { outbox = nil }
                lease?.revoke()
                lease = command.speech.map { _ in SpeechLease(seconds: remainingLease(command)) }
                startTask = Task { [self] in
                    guard !closing, !Task.isCancelled else { return }
                    do {
                        try await core.start(AudioConfig(meetingID: meeting,
                            sources: sources, locale: command.locale ?? "zh-CN",
                            includedProcessID: command.processID,
                            recordingDirectory: command.recordingDirectory.map { URL(fileURLWithPath: $0, isDirectory: true) },
                            captureEpoch: command.captureEpoch), speech: command.speech, lease: lease)
                    } catch AudioCoreError.microphoneDenied {
                        core.reportError(code: "microphone_denied", detail: "麦克风权限未开启，请在系统设置中允许真实交互使用麦克风，再重新开始收听。")
                    } catch { core.reportError(code: "start_rejected", detail: error.localizedDescription) }
                }
            case "speech_lease":
                if command.captureEpoch != nil, command.captureEpoch == currentEpoch {
                    lease?.renew(seconds: remainingLease(command))
                }
            case "speech_revoke":
                if command.captureEpoch == currentEpoch { lease?.revoke() }
            case "speech_probe": startProbe(command)
            case "cancel_speech_probe":
                if command.testID == probeID { probe?.cancel() }
            case "stop":
                startTask?.cancel()
                Task { await core.stop() }
            case "status": core.reportStatus()
            case "model_status":
                Task {
                    let locale = command.locale ?? "zh-CN"
                    core.reportModelStatus(locale: locale, status: await LocalSpeechModels.status(locale: locale))
                }
            case "install_model":
                guard command.allowDownload == true else {
                    core.reportError(code: "download_not_requested", detail: "install_model requires allow_download:true."); return
                }
                Task {
                    let locale = command.locale ?? "zh-CN"
                    do {
                        try await LocalSpeechModels.install(locale: locale)
                        core.reportModelStatus(locale: locale, status: await LocalSpeechModels.status(locale: locale))
                    } catch { core.reportError(code: "model_install_failed", detail: error.localizedDescription) }
                }
            case "quit":
                closing = true; startTask?.cancel()
                Task { await finish(); exit(0) }
            default: core.reportError(code: "unknown_command", detail: "Use start, stop, status, model_status, install_model, capture_probe or quit.")
            }
        } catch { core.reportError(code: "invalid_command", detail: "Expected one JSON command per line.") }
    }

    func shutdown() async {
        closing = true; startTask?.cancel()
        await finish()
    }

    private func finish() async {
        lease?.revoke()
        probe?.cancel()
        await core.stop()
        outbox?.seal()
        await outbox?.flush()
        await writer.flush()
    }
}

@main
struct AudioHelperMain {
    @MainActor static func main() async {
        signal(SIGPIPE, SIG_IGN)
        if CommandLine.arguments.contains("--help") {
            print("ZSJH AudioHelper: JSONL stdin commands start/stop/status/model_status/install_model/quit. No capture until explicit start. See native/README.md.")
            return
        }
        if CommandLine.arguments.contains("--speech-probe-info") {
            do {
                let info = try await AudioFileInfo.inspect(sampleURL())
                print("{\"ready\":true,\"duration_ms\":\(info.durationMS),\"bytes\":\(info.bytes)}")
            } catch {
                FileHandle.standardError.write(Data((error.localizedDescription + "\n").utf8))
                exit(66)
            }
            return
        }
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)
        let host = Host()
        if CommandLine.arguments.contains("--model-status") {
            let locale = "zh-CN"
            host.core.reportModelStatus(locale: locale, status: await LocalSpeechModels.status(locale: locale))
            await host.writer.flush(); return
        }
        let (commands, input) = AsyncStream.makeStream(of: String.self, bufferingPolicy: .bufferingOldest(32))
        DispatchQueue.global(qos: .userInitiated).async {
            while let line = readLine() {
                if case .dropped = input.yield(line) {
                    FileHandle.standardError.write(Data("Command queue exhausted; closing capture.\n".utf8))
                    break
                }
            }
            input.finish()
        }
        for await line in commands { host.dispatch(line) }
        await host.shutdown()
    }
}
