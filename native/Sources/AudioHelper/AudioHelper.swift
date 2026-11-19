import AppKit
import AudioCore
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
    enum CodingKeys: String, CodingKey {
        case command, source, sources, locale, seconds
        case meetingID = "meeting_id", allowDownload = "allow_download"
        case processID = "process_id"
        case recordingDirectory = "recording_directory", captureEpoch = "capture_epoch"
    }
}

/// stdout writes happen off the capture/control actor. An unread pipe has a
/// bounded pending budget and never keeps the microphone running on stdin EOF.
private final class JSONLWriter: @unchecked Sendable {
    private let queue = DispatchQueue(label: "com.zsjh.audio.jsonl")
    private let lock = NSLock()
    private var pending = 0
    private var failed = false
    var onFailure: (() -> Void)?
    func write(_ event: AudioEvent) {
        guard var data = try? JSONEncoder().encode(event) else { return }
        data.append(10)
        lock.lock()
        if failed { lock.unlock(); return }
        guard pending + data.count <= 2_000_000 else {
            failed = true; lock.unlock(); onFailure?(); return
        }
        pending += data.count; lock.unlock()
        queue.async { [self, data] in
            do { try FileHandle.standardOutput.write(contentsOf: data) }
            catch {
                lock.lock(); let notify = !failed; failed = true; lock.unlock()
                if notify { onFailure?() }
            }
            lock.lock(); pending -= data.count; lock.unlock()
        }
    }
    func flush() async { await withCheckedContinuation { continuation in queue.async { continuation.resume() } } }
}

@MainActor
private final class Host {
    let core = AudioCore()
    let writer = JSONLWriter()
    var startTask: Task<Void, Never>?
    var closing = false

    init() {
        core.onEvent = { [writer] in writer.write($0) }
        writer.onFailure = { [weak self] in
            FileHandle.standardError.write(Data("Audio event output unavailable; capture stopped.\n".utf8))
            Task { @MainActor in
                guard let self else { return }
                self.closing = true; self.startTask?.cancel()
                await self.core.stop(graceful: false)
                exit(74)
            }
        }
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
                startTask = Task { [self] in
                    guard !closing, !Task.isCancelled else { return }
                    do {
                        try await core.start(AudioConfig(meetingID: meeting,
                            sources: command.sources ?? [command.source ?? .system], locale: command.locale ?? "zh-CN",
                            includedProcessID: command.processID,
                            recordingDirectory: command.recordingDirectory.map { URL(fileURLWithPath: $0, isDirectory: true) },
                            captureEpoch: command.captureEpoch))
                    } catch AudioCoreError.microphoneDenied {
                        core.reportError(code: "microphone_denied", detail: "麦克风权限未开启，请在系统设置中允许真实交互工作台使用麦克风，再重新开始收听。")
                    } catch { core.reportError(code: "start_rejected", detail: error.localizedDescription) }
                }
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
                Task { await core.stop(); await writer.flush(); exit(0) }
            default: core.reportError(code: "unknown_command", detail: "Use start, stop, status, model_status, install_model, capture_probe or quit.")
            }
        } catch { core.reportError(code: "invalid_command", detail: "Expected one JSON command per line.") }
    }

    func shutdown() async {
        closing = true; startTask?.cancel()
        await core.stop()
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
