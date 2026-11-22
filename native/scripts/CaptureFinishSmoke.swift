// Appended to the real DesktopHost source by test-capture-finish.sh. No window,
// recording device, backend, or real meeting is started by this executable.
extension DesktopController {
    @MainActor static func verifyCaptureFinishWatchdog() async throws {
        func require(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
            if try condition() == false { throw DesktopError(message: message) }
        }
        let lifecycle = DesktopController()
        try require(!lifecycle.applicationShouldTerminateAfterLastWindowClosed(NSApp),
                    "closing the last window terminates background transcription and research")
        lifecycle.window = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: 80, height: 80),
                                    styleMask: [.titled, .closable], backing: .buffered, defer: false)
        lifecycle.window.isReleasedWhenClosed = false
        lifecycle.activeMeeting = "synthetic-background-import"
        lifecycle.captureState = "importing"
        var backgroundTicks = 0
        lifecycle.fileTask = Task { @MainActor in
            while !Task.isCancelled {
                backgroundTicks += 1
                try? await Task.sleep(for: .milliseconds(5))
            }
        }
        lifecycle.window.close()
        try await Task.sleep(for: .milliseconds(40))
        try require(backgroundTicks > 0 && lifecycle.fileTask?.isCancelled == false && !lifecycle.quitting,
                    "closing the window cancelled the active import")
        try require(lifecycle.applicationShouldHandleReopen(NSApp, hasVisibleWindows: false) && lifecycle.window.isVisible,
                    "Dock reopen did not restore the retained window")
        lifecycle.fileTask?.cancel()
        lifecycle.window.close()
        let core = AudioCore()
        var stopAcknowledgements = 0
        core.onEvent = { event in
            if event.type == "audio.status", event.state == "stopped" { stopAcknowledgements += 1 }
        }
        await core.stop()
        await core.stop()
        try require(stopAcknowledgements == 2, "repeated stop must acknowledge closure again so sealing can retry")
        func fixture() throws -> (DesktopController, Process, URL) {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
            let input = Pipe()
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/bin/cat")
            process.standardInput = input
            let commandLog = directory.appendingPathComponent("commands.jsonl")
            FileManager.default.createFile(atPath: commandLog.path, contents: Data())
            process.standardOutput = try FileHandle(forWritingTo: commandLog)
            process.standardError = FileHandle.nullDevice
            try process.run()
            let controller = DesktopController()
            controller.helper = process
            controller.helperInput = input.fileHandleForWriting
            controller.activeMeeting = "meeting"
            controller.activeEpoch = "epoch-1"
            controller.activeOutbox = directory
            controller.captureState = "listening"
            return (controller, process, directory)
        }
        func stopped(_ controller: DesktopController, epoch: String) throws {
            controller.audioLine(try JSONSerialization.data(withJSONObject: [
                "type": "audio.status", "state": "stopped", "meeting_id": "meeting", "capture_epoch": epoch,
            ]))
        }
        func sealFailure(_ controller: DesktopController) throws {
            controller.audioLine(try JSONSerialization.data(withJSONObject: [
                "type": "audio.error", "code": "outbox_seal_failed", "state": "stopped",
                "meeting_id": "meeting", "capture_epoch": "epoch-1",
            ]))
        }
        func stopCommands(in directory: URL) throws -> Int {
            let data = try Data(contentsOf: directory.appendingPathComponent("commands.jsonl"))
            return try data.split(separator: 10).filter { line in
                let command = try JSONSerialization.jsonObject(with: Data(line)) as? [String: Any]
                return command?["command"] as? String == "stop"
            }.count
        }

        let completed = try fixture()
        let unfinished = try fixture()
        let nextEpoch = try fixture()
        let fixtures = [completed, unfinished, nextEpoch]
        defer {
            for (controller, process, directory) in fixtures {
                controller.closing = true
                controller.stopWatchdog?.cancel()
                try? controller.helperInput?.close()
                if process.isRunning { process.terminate() }
                try? FileManager.default.removeItem(at: directory)
            }
        }
        let speech = completed.0
        func speechEvent(_ type: String, _ fields: [String: String]) throws {
            var event = fields
            event["type"] = type; event["source"] = "system"
            event["meeting_id"] = "meeting"; event["capture_epoch"] = "epoch-1"
            speech.audioLine(try JSONSerialization.data(withJSONObject: event))
        }
        try speechEvent("asr.error", ["provider": "apple_local", "code": "speech_model_unavailable", "detail": "合成模型不可用"])
        try require(speech.transcription["system"]?["state"] == "error" &&
                    speech.transcription["system"]?["detail"] == "合成模型不可用", "native state discarded the ASR failure")
        try speechEvent("asr.gap", ["code": "speech_model_unavailable"])
        try require(speech.transcription["system"]?["state"] == "error", "gap erased the recognizer state")
        try speechEvent("asr.status", ["provider": "apple_local", "state": "running"])
        try require(speech.transcription["system"]?["code"] == nil && speech.transcription["system"]?["detail"] == nil,
                    "recovery retained a stale failure")
        completed.0.stopWithStatus("fixture stop")
        try stopped(completed.0, epoch: "epoch-1")
        unfinished.0.stopWithStatus("fixture stop")
        // A stopped event from an older epoch cannot authorize keeping a
        // newer capture alive after its stop deadline.
        try stopped(nextEpoch.0, epoch: "epoch-1")
        nextEpoch.0.activeEpoch = "epoch-2"
        nextEpoch.0.captureState = "listening"
        nextEpoch.0.stopWithStatus("fixture stop")
        try stopped(nextEpoch.0, epoch: "epoch-1")

        let deadline = ContinuousClock.now.advanced(by: .seconds(12))
        while fixtures.contains(where: { $0.0.captureState != "error" }), .now < deadline {
            try await Task.sleep(for: .milliseconds(20))
        }
        try require(fixtures.allSatisfy { $0.0.captureState == "error" }, "stop watchdog did not report a retryable error")
        try require(completed.1.isRunning && completed.0.helperInput != nil && completed.0.canStop,
                    "seal failure killed an already stopped helper, making retry impossible")
        try require(unfinished.0.helperInput == nil, "unconfirmed capture did not receive EOF")
        try require(nextEpoch.0.helperInput == nil, "new epoch retained the previous stopped flag")
        // Delayed seal failures can arrive after the watchdog has exposed an
        // error. They must not send Stop and reset the helper's retry budget.
        try require(try stopCommands(in: completed.2) == 1, "fixture did not send its initial stop")
        for _ in 0..<3 { try sealFailure(completed.0) }
        try require(completed.0.captureState == "error" && completed.0.captureFinish == "failed" && completed.0.canStop,
                    "seal failure automatically restarted stopping instead of remaining retryable")
        try await Task.sleep(for: .milliseconds(50))
        try require(try stopCommands(in: completed.2) == 1, "seal failure sent an unsolicited stop command")
        // The still-alive helper accepts the user's retry command.
        completed.0.stopWithStatus("fixture retry")
        try require(completed.0.captureState == "stopping" && completed.0.captureFinish == nil,
                    "retry did not clear the previous failure and restart finalization")
        try await Task.sleep(for: .milliseconds(50))
        try require(try stopCommands(in: completed.2) == 2, "explicit retry did not send exactly one new stop")
        try stopped(completed.0, epoch: "epoch-1")
        for _ in 0..<3 { try sealFailure(completed.0) }
        try require(completed.0.captureState == "error" && completed.0.canStop,
                    "seal failure during explicit retry did not remain retryable")
        try await Task.sleep(for: .milliseconds(50))
        try require(try stopCommands(in: completed.2) == 2, "retry failure restarted stopping without a user command")
        print("Capture finish watchdog passed: stopped helper remains retryable, unconfirmed capture receives EOF, epoch binding resets; only explicit retries send a new stop.")
    }
}

@main
struct CaptureFinishSmokeMain {
    @MainActor static func main() async throws {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        try await DesktopController.verifyCaptureFinishWatchdog()
    }
}
