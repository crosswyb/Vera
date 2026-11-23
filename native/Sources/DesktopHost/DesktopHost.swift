import AppKit
import AudioCore
import Darwin
import Foundation
import UniformTypeIdentifiers
import WebKit

private struct DesktopError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

private struct HTTPFailure: LocalizedError {
    let status: Int
    var errorDescription: String? { "会议服务拒绝请求（\(status)）。" }
}

private final class LocalRequestDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil) // An API redirect must never carry native credentials elsewhere.
    }
}

/// Pipes have a per-line cap. Private backend handshakes never become logs.
private final class LineReader: @unchecked Sendable {
    let handle: FileHandle
    let limit: Int
    init(_ handle: FileHandle, limit: Int = 65_536) { self.handle = handle; self.limit = limit }
    func start(line: @escaping (Data) -> Void, ended: @escaping () -> Void) {
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            var buffer = Data()
            defer { ended() }
            var chunk = [UInt8](repeating: 0, count: 4096)
            while true {
                // POSIX read returns the available pipe bytes immediately;
                // fixed-size Foundation reads can delay a short ready line.
                let received = chunk.withUnsafeMutableBytes { Darwin.read(handle.fileDescriptor, $0.baseAddress!, $0.count) }
                if received < 0, errno == EINTR { continue }
                guard received > 0 else { return }
                buffer.append(contentsOf: chunk.prefix(received))
                while let end = buffer.firstIndex(of: 10) {
                    guard buffer.distance(from: buffer.startIndex, to: end) <= limit else { return }
                    let value = Data(buffer[..<end]); buffer.removeSubrange(...end)
                    line(value)
                }
                guard buffer.count <= limit else { return }
            }
        }
    }
}

/// One pending main-actor delivery per reader; a busy UI cannot create an
/// unbounded collection of tasks before the bridge's event cap is checked.
private func deliverToMain(_ action: @MainActor @escaping () -> Void) {
    let delivered = DispatchSemaphore(value: 0)
    Task { @MainActor in action(); delivered.signal() }
    delivered.wait()
}

@MainActor
private final class DesktopController: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, WKDownloadDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private let startupView = NSView()
    private let startupTitle = NSTextField(labelWithString: "正在打开工作区")
    private let startupDetail = NSTextField(wrappingLabelWithString: "正在准备本机服务…")
    private let startupSpinner = NSProgressIndicator()
    private let startupExit = NSButton(title: "退出应用", target: nil, action: nil)
    private var startupFailed = false
    private let updates = VeraUpdateChecker(
        version: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "未知版本",
        build: (Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String).flatMap(Int.init))
    private var updateTask: Task<Void, Never>?
    private var updateReadinessTask: Task<Void, Never>?
    private var updateBackendBlockReason = "正在确认会议与任务状态…"
    private var updatePromptVisible = false
    private var updatePromptPending = false
    private let updateItem = NSMenuItem(title: "检查更新…", action: nil, keyEquivalent: "")
    private var status = "正在启动…" { didSet { publishNativeState() } }
    private let startItem = NSMenuItem(title: "开始收听", action: nil, keyEquivalent: "")
    private let stopItem = NSMenuItem(title: "停止收听", action: nil, keyEquivalent: "")
    private let modelItem = NSMenuItem(title: "检查中文模型", action: nil, keyEquivalent: "")
    private let installItem = NSMenuItem(title: "安装中文模型…", action: nil, keyEquivalent: "")
    private var backend: Process?
    private var helper: Process?
    private var helperInput: FileHandle?
    private var readyWaiter: CheckedContinuation<Void, Error>?
    private var port = 0
    private var ownerToken = ""
    private var agentToken = ""
    private var speechToken = ""
    private var speechLiveProvider = "apple_local"
    private var speechFileProvider = "apple_local"
    private var speechLiveReady = false
    private var speechFileReady = false
    private var speechRevision = 0
    private var speechTestID: String?
    private var speechTestError: String?
    private var speechLeaseTask: Task<Void, Never>?
    private var fileLease: SpeechLease?
    private var captureInputsStopped = false
    private var activeEpoch: String? { didSet { captureInputsStopped = false } }
    private var activeOutbox: URL?
    private var outboxTask: Task<Void, Never>?
    private var captureFinish: String?
    private var captureFinishMeetingId: String?
    private var transcription: [String: [String: String]] = [:]
    private var backendFailureKind = ""
    private var activeMeeting: String? { didSet { publishNativeState() } }
    private var lastMeeting: String?
    private var captureState = "idle" { didSet { publishNativeState() } }
    private var modelState = "unknown" { didSet { publishNativeState() } }
    private var fileModelState = "unknown" { didSet { publishNativeState() } }
    private var themePreference = UserDefaults.standard.string(forKey: "themePreference") ?? "system"
    private var layoutPreference = UserDefaults.standard.string(forKey: "layoutPreference") == "b" ? "b" : "a"
    private var beginningCapture = false { didSet { publishNativeState() } }
    private var captureGeneration = 0
    private var pageLoaded = false
    private var captureProblem: String?
    private var selectedInput = LiveAudioInput(rawValue: UserDefaults.standard.string(forKey: "liveAudioInput") ?? "system") ?? .system
    private var sourceLevels: [String: Float] = [:]
    private var sourceFrames: [String: UInt64] = [:]
    private var pendingInterims: [String: (String, Data)] = [:]
    private var postingInterims = false
    private var bridgeFailed = false
    private var closing = false
    private var quitting = false
    private var healthTask: Task<Void, Never>?
    private var stopWatchdog: Task<Void, Never>?
    private struct SelectedAudio {
        let id: String
        let url: URL
        let info: AudioFileInfo
    }
    private var selectedAudio: SelectedAudio?
    private var selectedAudioError = ""
    private var audioSelectionTask: Task<Void, Never>?
    private var activeInput = "live"
    private var fileTask: Task<Void, Never>?
    private var fileTranscriber: FileTranscription?
    private struct MarkdownExport {
        let download: WKDownload
        var temporaryDirectory: URL?
        var destination: URL?
        var replacingExisting = false
    }
    private var exports: [ObjectIdentifier: MarkdownExport] = [:]
    private lazy var session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.connectionProxyDictionary = [:]
        config.httpShouldSetCookies = false
        config.urlCache = nil
        config.timeoutIntervalForRequest = 3
        config.timeoutIntervalForResource = 4
        return URLSession(configuration: config, delegate: LocalRequestDelegate(), delegateQueue: nil)
    }()
    private let stateDirectory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/ZSJH/Workbench", isDirectory: true)
    private let outputRoot = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Documents/ZSJH/Meetings", isDirectory: true)

    private var childEnvironment: [String: String] {
        let systemKeys = Set(["HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"])
        var values = ProcessInfo.processInfo.environment.filter { systemKeys.contains($0.key) }
        values["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        return values
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        Task {
            do {
                try await bootBackend()
                showStartupProgress("正在连接音频组件…")
                try launchHelper()
                showStartupProgress("正在加载应用…")
                try? await refreshSpeechConfiguration()
                replaySavedCaptures()
                loadWorkbench()
                status = "未在收听"
                checkModel()
            } catch {
                status = error.localizedDescription
                showStartupFailure(error.localizedDescription)
            }
        }
    }

    private func buildWindow() {
        applyThemePreference(themePreference)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "真实交互"
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 980, height: 680)
        let root = NSView()
        window.contentView = root
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.userContentController.add(self, name: "workbench")
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: root.topAnchor),
            webView.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])
        buildStartupView()
        for item in [startItem, stopItem] { item.isEnabled = false; item.target = self }
        startItem.action = #selector(startCapture)
        stopItem.action = #selector(stopCapture)
        modelItem.target = self; modelItem.action = #selector(checkModel); modelItem.isEnabled = false
        installItem.target = self; installItem.action = #selector(installModel); installItem.isEnabled = false
        let appMenu = NSMenu()
        let rootItem = NSMenuItem()
        let menu = NSMenu()
        let aboutItem = menu.addItem(withTitle: "关于真实交互", action: #selector(showAbout), keyEquivalent: "")
        aboutItem.target = self
        updateItem.target = self; updateItem.action = #selector(checkForUpdate(_:))
        menu.addItem(updateItem)
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出真实交互", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        rootItem.submenu = menu; appMenu.addItem(rootItem)
        let editItem = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        let edit = NSMenu(title: "编辑")
        for (title, action, key) in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"), ("复制", "copy:", "c"),
                                      ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        editItem.submenu = edit; appMenu.addItem(editItem)
        let audioItem = NSMenuItem(title: "音频", action: nil, keyEquivalent: "")
        let audioMenu = NSMenu(title: "音频"); audioMenu.autoenablesItems = false
        audioMenu.addItem(startItem); audioMenu.addItem(stopItem); audioMenu.addItem(.separator())
        audioMenu.addItem(modelItem); audioMenu.addItem(installItem)
        audioItem.submenu = audioMenu; appMenu.addItem(audioItem); NSApp.mainMenu = appMenu
        window.center(); window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func showAbout() {
        NSApp.orderFrontStandardAboutPanel(options: [.applicationName: "真实交互 · Vera"])
    }

    @objc private func checkForUpdate(_ sender: NSMenuItem) {
        if updates.prepared != nil {
            Task { await promptToInstallUpdate() }
            return
        }
        checkForUpdates(showResult: true)
    }

    private func startUpdateChecks() {
        guard updateTask == nil else { return }
        updates.didChange = { [weak self] in self?.publishNativeState() }
        updates.didPrepare = { [weak self] in Task { await self?.promptToInstallUpdate() } }
        updateReadinessTask = Task { [weak self] in
            while !Task.isCancelled {
                if let self, self.updates.prepared != nil, !self.updates.installing {
                    await self.refreshUpdateReadiness()
                    if self.updatePromptPending { await self.promptToInstallUpdate() }
                }
                do { try await Task.sleep(for: .seconds(5)) } catch { return }
            }
        }
        updateTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self, !self.closing else { return }
                await self.updates.check(automatically: true)
                do { try await Task.sleep(for: .seconds(VeraUpdateChecker.automaticInterval)) }
                catch { return }
            }
        }
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        guard pageLoaded, !closing else { return }
        Task { await updates.check(automatically: true) }
    }

    private func checkForUpdates(showResult: Bool) {
        guard !updates.checking, !updates.downloading, !updates.installing, !closing else { return }
        Task {
            guard !updates.checking else { return }
            await updates.check()
            guard showResult, !closing else { return }
            if updates.available, updates.failure == nil { return }
            let alert = NSAlert()
            if let failure = updates.failure {
                alert.messageText = updates.available ? "更新未完成" : "未能检查更新"
                alert.informativeText = failure
            } else {
                alert.messageText = "当前没有更新"
                alert.informativeText = "已安装 build \(updates.currentBuild ?? 0)，最新正式版为 \(updates.latest?.tag_name ?? "未知")。"
            }
            alert.addButton(withTitle: "好")
            await alert.beginSheetModal(for: window)
        }
    }

    private var updateInstallBlockedReason: String {
        if activeMeeting != nil || beginningCapture || fileTask != nil || activeOutbox != nil {
            return "录音、导入或保存尚未完成，请完成后再安装更新。"
        }
        if !hostReady { return "会议服务尚未就绪，请稍后重试。" }
        return updateBackendBlockReason
    }

    private func refreshUpdateReadiness() async {
        do {
            let data = try await request("/live/v2/home")
            guard let home = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let readiness = home["update_readiness"] as? [String: Any],
                  let allowed = readiness["can_install"] as? Bool, let reason = readiness["reason"] as? String,
                  allowed == reason.isEmpty else { throw DesktopError(message: "无法确认会议与任务状态。") }
            updateBackendBlockReason = reason
        } catch { updateBackendBlockReason = "无法确认会议与任务状态，请稍后重试。" }
        publishNativeState()
    }

    private func promptToInstallUpdate() async {
        guard !updatePromptVisible, !updates.installing, !closing,
              let release = updates.latest, let source = updates.prepared, let window else { return }
        updatePromptPending = true
        guard window.attachedSheet == nil else { return }
        updatePromptVisible = true
        defer { updatePromptVisible = false }
        await refreshUpdateReadiness()
        guard !closing, window.attachedSheet == nil else { return }
        updatePromptPending = false
        let alert = NSAlert()
        alert.messageText = "新版 Vera 已下载"
        let reason = updateInstallBlockedReason
        alert.informativeText = "\(release.tag_name) 已下载并通过校验。现在安装并重新打开应用吗？会议文件和设置将保留。"
            + (reason.isEmpty ? "" : "\n\n" + reason)
        alert.addButton(withTitle: "安装并重新打开")
        alert.addButton(withTitle: "稍后")
        alert.buttons[0].isEnabled = reason.isEmpty
        guard await alert.beginSheetModal(for: window) == .alertFirstButtonReturn else { return }
        await refreshUpdateReadiness()
        guard updateInstallBlockedReason.isEmpty else { return }
        guard updates.latest?.tag_name == release.tag_name,
              updates.latest?.archive.digest == release.archive.digest,
              updates.prepared == source else {
            // A newer download can finish while the confirmation remains open.
            // Ask about that exact candidate before installing it.
            updatePromptPending = true
            return
        }
        await installPreparedUpdate()
    }

    private func installPreparedUpdate() async {
        guard !updates.installing, !closing, updateInstallBlockedReason.isEmpty,
              let source = updates.prepared, let release = updates.latest, let previousBuild = updates.currentBuild,
              let executable = Bundle.main.executableURL else { return }
        let process = Process()
        do {
            let plan = VeraUpdateInstallPlan(source: source, destination: Bundle.main.bundleURL,
                version: String(release.tag_name.dropFirst().components(separatedBy: "-build")[0]), build: release.build,
                previousBuild: previousBuild, team: try VeraUpdatePackage.signingTeam(Bundle.main.bundleURL), parentPID: getpid())
            try plan.preflight()
            let job = VeraUpdatePackage.cacheDirectory.appendingPathComponent("install-" + UUID().uuidString, isDirectory: true)
            try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let planURL = job.appendingPathComponent("plan.json")
            try JSONEncoder().encode(plan).write(to: planURL, options: .atomic)
            process.executableURL = executable
            process.arguments = ["--install-update", planURL.path]
            process.environment = childEnvironment
            process.standardInput = FileHandle.nullDevice
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            updates.installationStarted()
            try process.run()
            let deadline = Date().addingTimeInterval(10)
            while !FileManager.default.fileExists(atPath: planURL.appendingPathExtension("ready").path) {
                guard process.isRunning, Date() < deadline else { throw DesktopError(message: "安装程序未能启动，当前 App 未作更改。") }
                try await Task.sleep(for: .milliseconds(100))
            }
            await refreshUpdateReadiness()
            guard updateInstallBlockedReason.isEmpty else { throw DesktopError(message: updateInstallBlockedReason) }
            NSApp.terminate(nil)
        } catch {
            if process.isRunning { process.terminate() }
            updates.installationFailed(error)
            let alert = NSAlert()
            alert.messageText = "暂时无法安装更新"
            alert.informativeText = error.localizedDescription
            alert.addButton(withTitle: "好")
            alert.runModal()
        }
    }

    private func buildStartupView() {
        startupView.wantsLayer = true
        startupView.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        startupView.translatesAutoresizingMaskIntoConstraints = false
        startupSpinner.style = .spinning
        startupSpinner.controlSize = .regular
        startupSpinner.isDisplayedWhenStopped = false
        startupTitle.font = .systemFont(ofSize: 22, weight: .semibold)
        startupTitle.alignment = .center
        startupDetail.font = .systemFont(ofSize: 15)
        startupDetail.textColor = .secondaryLabelColor
        startupDetail.alignment = .center
        startupDetail.preferredMaxLayoutWidth = 520
        startupDetail.widthAnchor.constraint(equalToConstant: 520).isActive = true
        startupExit.bezelStyle = .rounded
        startupExit.target = NSApp
        startupExit.action = #selector(NSApplication.terminate(_:))
        startupExit.isHidden = true
        let stack = NSStackView(views: [startupSpinner, startupTitle, startupDetail, startupExit])
        stack.orientation = .vertical; stack.alignment = .centerX; stack.spacing = 14
        stack.translatesAutoresizingMaskIntoConstraints = false
        startupView.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: startupView.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: startupView.centerYAnchor),
        ])
        showStartupProgress("正在准备本机工作区…")
    }

    private func attachStartupView() {
        guard startupView.superview == nil, let root = window?.contentView else { return }
        root.addSubview(startupView)
        NSLayoutConstraint.activate([
            startupView.topAnchor.constraint(equalTo: root.topAnchor),
            startupView.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            startupView.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            startupView.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])
    }

    private func showStartupProgress(_ message: String) {
        guard !closing, !startupFailed else { return }
        attachStartupView()
        startupTitle.stringValue = "正在打开工作区"
        startupDetail.stringValue = message
        startupExit.isHidden = true
        startupSpinner.startAnimation(nil)
    }

    private func showStartupFailure(_ message: String) {
        guard !closing else { return }
        startupFailed = true
        attachStartupView()
        startupSpinner.stopAnimation(nil)
        startupTitle.stringValue = "无法打开工作区"
        startupDetail.stringValue = message + "\n请退出后重新打开应用。"
        startupExit.isHidden = false
    }

    private func arguments() -> [String] {
        ["--state-dir", stateDirectory.path, "--env-file", stateDirectory.appendingPathComponent("workbench.env").path,
         "--output-root", outputRoot.path]
    }

    private func bootBackend() async throws {
        guard let resources = Bundle.main.resourceURL else { throw DesktopError(message: "请从已打包的真实交互 App 启动。") }
        let executable = resources.appendingPathComponent("backend/zsjh-workbench-backend")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else { throw DesktopError(message: "应用包缺少本机服务，请重新构建。") }
        if !FileManager.default.fileExists(atPath: stateDirectory.appendingPathComponent("credentials.json").path) {
            showStartupProgress("正在创建本机工作区…")
            try await runInitialization(executable)
        }
        showStartupProgress("正在启动本机服务…")
        let process = Process()
        let output = Pipe()
        let diagnostic = Pipe()
        process.executableURL = executable
        process.environment = childEnvironment
        process.arguments = ["serve"] + arguments() + ["--port", "0", "--desktop", "--audio"]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = output
        process.standardError = diagnostic
        backend = process
        process.terminationHandler = { [weak self] _ in Task { @MainActor in self?.backendExited() } }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            readyWaiter = continuation
            do {
                try process.run()
                LineReader(diagnostic.fileHandleForReading).start(line: { [weak self] data in
                    // Keep only exception classes; tracebacks may contain private data.
                    let line = String(data: data, encoding: .utf8) ?? ""
                    let kinds = ["PermissionError", "sqlite3.OperationalError", "sqlite3.DatabaseError", "FileNotFoundError", "ImportError", "ModuleNotFoundError", "RuntimeError", "ValueError", "TypeError", "KeyError", "OSError"]
                    let kind = kinds.first(where: { line.hasPrefix($0 + ":") })
                        ?? (line.contains("Failed to load Python shared library") ? "Python runtime load failed" : nil)
                    if let kind {
                        deliverToMain {
                            self?.backendFailureKind = kind
                            if self?.backend?.isRunning == false { self?.backendExited() }
                        }
                    }
                }, ended: {})
                LineReader(output.fileHandleForReading).start(line: { [weak self] data in
                    deliverToMain { self?.backendLine(data) }
                }, ended: { [weak self] in Task { @MainActor in self?.backendExited() } })
                Task { [weak self] in
                    try? await Task.sleep(for: .seconds(45))
                    guard let self, self.readyWaiter != nil else { return }
                    self.completeReady(DesktopError(message: "本机服务启动超时，尚未收听。"))
                    if process.isRunning { process.terminate() }
                }
            } catch { completeReady(DesktopError(message: "无法启动本机服务。")) }
        }
    }

    private func runInitialization(_ executable: URL) async throws {
        let process = Process()
        process.executableURL = executable; process.arguments = ["init"] + arguments()
        process.environment = childEnvironment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
        backend = process
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            process.terminationHandler = { result in
                if result.terminationStatus == 0 { continuation.resume() }
                else { continuation.resume(throwing: DesktopError(message: "无法初始化私有工作区，请检查目录权限。")) }
            }
            do { try process.run() }
            catch { continuation.resume(throwing: DesktopError(message: "无法运行工作区初始化。")) }
            Task {
                try? await Task.sleep(for: .seconds(30))
                if process.isRunning { process.terminate() }
            }
        }
    }

    private func backendLine(_ data: Data) {
        guard readyWaiter != nil, let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              value["type"] as? String == "ready", let candidate = value["port"] as? Int, (1...65_535).contains(candidate),
              let owner = value["owner_token"] as? String, !owner.isEmpty,
              let agent = value["agent_token"] as? String, !agent.isEmpty else { return }
        port = candidate; ownerToken = owner; agentToken = agent
        speechToken = value["speech_token"] as? String ?? ""
        completeReady(nil)
    }

    private func completeReady(_ error: Error?) {
        let continuation = readyWaiter; readyWaiter = nil
        if let error { continuation?.resume(throwing: error) } else { continuation?.resume() }
    }

    private func backendExited() {
        guard !closing else { return }
        completeReady(DesktopError(message: "本机服务已退出。"))
        let exitCode = backend?.isRunning == false ? "退出码 \(backend!.terminationStatus)" : ""
        let detail = [backendFailureKind, exitCode].filter { !$0.isEmpty }.joined(separator: " · ")
        if activeMeeting != nil, activeInput == "live" {
            status = "本机服务中断。录音继续，转写等待保存。"
            revokeSpeech()
        } else {
            failBridge(detail.isEmpty ? "本机服务已停止。" : "本机服务已停止（\(detail)）。")
        }
        startItem.isEnabled = false
    }

    private func loadWorkbench() {
        let encoded = String(data: try! JSONSerialization.data(withJSONObject: [ownerToken]), encoding: .utf8)!
        let theme = String(data: try! JSONSerialization.data(withJSONObject: [themePreference]), encoding: .utf8)!
        let layout = String(data: try! JSONSerialization.data(withJSONObject: [layoutPreference]), encoding: .utf8)!
        let script = "if(location.origin==='http://127.0.0.1:\(port)' && location.pathname==='/workbench'){sessionStorage.setItem('wb_access',\(encoded)[0]);window.__ZSJH_THEME_PREFERENCE__=\(theme)[0];window.__ZSJH_LAYOUT_PREFERENCE__=\(layout)[0];}"
        webView.configuration.userContentController.addUserScript(WKUserScript(source: script, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        webView.load(URLRequest(url: URL(string: "http://127.0.0.1:\(port)/workbench")!))
    }

    private func isLocal(_ url: URL) -> Bool {
        url.scheme == "http" && url.host == "127.0.0.1" && url.port == port && url.user == nil && url.password == nil
    }

    private func isWorkbench(_ url: URL?) -> Bool {
        guard let url else { return false }
        return isLocal(url) && url.path == "/workbench"
    }

    private func isWorkbenchFrame(_ frame: WKFrameInfo) -> Bool {
        let origin = frame.securityOrigin
        return frame.isMainFrame && frame.webView === webView && isWorkbench(frame.request.url)
            && origin.protocol == "http" && origin.host == "127.0.0.1" && origin.port == port
    }

    private var hostReady: Bool {
        !closing && !bridgeFailed && port > 0 && backend?.isRunning == true && helper?.isRunning == true
    }

    private var canStart: Bool {
        hostReady && pageLoaded && activeMeeting == nil && !beginningCapture && !updates.installing
            && (speechLiveProvider == "apple_local" ? modelState == "installed" : speechLiveReady)
    }

    private var canStop: Bool {
        activeMeeting != nil && captureState != "stopping" && captureState != "stopped" && !closing
    }

    private var canImport: Bool {
        hostReady && pageLoaded && activeMeeting == nil && !beginningCapture && !updates.installing
            && (speechFileProvider == "apple_local" ? fileModelState == "installed" : speechFileReady)
    }

    private func applyThemePreference(_ value: String) {
        let preference = ["system", "light", "dark"].contains(value) ? value : "system"
        themePreference = preference
        UserDefaults.standard.set(preference, forKey: "themePreference")
        NSApp.appearance = preference == "system" ? nil : NSAppearance(named: preference == "dark" ? .darkAqua : .aqua)
    }

    private func publishNativeState() {
        updates.installBlockedReason = updateInstallBlockedReason
        updateItem.isEnabled = !updates.checking && !updates.downloading && !updates.installing
        if updates.downloading {
            updateItem.title = "正在下载更新…"
        } else if updates.prepared != nil {
            updateItem.title = "安装已下载的更新…"
        } else if updates.checking {
            updateItem.title = "正在检查更新…"
        } else {
            updateItem.title = "检查更新…"
        }
        startItem.isEnabled = canStart; stopItem.isEnabled = canStop
        modelItem.isEnabled = hostReady
        installItem.isEnabled = hostReady && activeMeeting == nil && !beginningCapture
            && ![modelState, fileModelState].contains("downloading")
            && [modelState, fileModelState].contains(where: { ["downloadable", "unknown"].contains($0) })
        guard let webView, pageLoaded, isWorkbench(webView.url), !closing else { return }
        let payload: [String: Any] = [
            "ready": hostReady, "captureState": captureState, "status": status,
            "themePreference": themePreference,
            "layoutPreference": layoutPreference,
            "updates": updates.payload,
            "meetingId": activeMeeting.map { $0 as Any } ?? NSNull(), "modelState": modelState,
            "speechLiveProvider": speechLiveProvider, "speechFileProvider": speechFileProvider,
            "captureFinish": captureFinish as Any? ?? NSNull(),
            "captureFinishMeetingId": captureFinishMeetingId as Any? ?? NSNull(),
            "speechTestError": speechTestError as Any? ?? NSNull(),
            "transcription": transcription,
            "canStart": canStart, "canStop": canStop,
            "canSelectSource": hostReady && activeMeeting == nil && !beginningCapture,
            "canImport": canImport, "fileModelState": fileModelState,
            "inputKind": activeInput, "audioFileError": selectedAudioError,
            "audioSource": selectedInput.rawValue, "sourceLevels": sourceLevels, "sourceFrames": sourceFrames,
            "captureProblem": captureProblem as Any? ?? NSNull(), "lastMeetingId": lastMeeting as Any? ?? NSNull(),
            "audioFile": selectedAudio.map { ["id": $0.id, "name": $0.info.name,
                "durationMS": $0.info.durationMS, "bytes": $0.info.bytes] as [String: Any] } as Any? ?? NSNull(),
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let encoded = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.__ZSJH_NATIVE__=\(encoded);window.dispatchEvent(new CustomEvent('zsjh-native-state',{detail:window.__ZSJH_NATIVE__}));", completionHandler: nil)
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        pageLoaded = false; publishNativeState()
        showStartupProgress("正在加载应用…")
        if activeMeeting != nil { stopWithStatus("页面正在重新连接，收听已暂停…") }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        pageLoaded = isWorkbench(webView.url); publishNativeState()
        guard !startupFailed else { return }
        guard pageLoaded, hostReady else {
            showStartupFailure("应用尚未成功连接本机服务。")
            return
        }
        startupSpinner.stopAnimation(nil)
        startupView.removeFromSuperview()
        startUpdateChecks()
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if navigationResponse.isForMainFrame, isWorkbench(navigationResponse.response.url),
           let response = navigationResponse.response as? HTTPURLResponse,
           !(200...299).contains(response.statusCode) {
            showStartupFailure("本机服务未能打开应用页面（\(response.statusCode)）。")
            decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showNavigationFailure(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showNavigationFailure(error)
    }

    private func showNavigationFailure(_ error: Error) {
        let failure = error as NSError
        guard !(failure.domain == NSURLErrorDomain && failure.code == NSURLErrorCancelled) else { return }
        pageLoaded = false; publishNativeState()
        if activeMeeting != nil { stopWithStatus("应用连接中断，正在停止收听…") }
        if !startupFailed {
            showStartupFailure(failure.domain == NSURLErrorDomain && failure.code == NSURLErrorTimedOut
                               ? "应用页面连接超时。" : "无法加载本机应用页面。")
        }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        pageLoaded = false; publishNativeState()
        if activeMeeting != nil { stopWithStatus("应用页面已退出，正在停止收听…") }
        showStartupFailure("应用页面进程已退出。")
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "workbench", message.webView === webView, isWorkbench(webView.url),
              isWorkbenchFrame(message.frameInfo), let body = message.body as? [String: Any],
              body.count <= 5, Set(body.keys).isSubset(of: ["action", "meetingId", "fileId", "audioSource", "themePreference", "layoutPreference", "testId"]),
              let action = body["action"] as? String else { return }
        guard !updates.installing || action == "requestState" else { return }
        switch action {
        case "requestState": publishNativeState()
        case "checkForUpdates": checkForUpdates(showResult: false)
        case "installUpdate": Task { await promptToInstallUpdate() }
        case "refreshSpeechSettings": Task { try? await refreshSpeechConfiguration() }
        case "speechTest":
            guard let test = body["testId"] as? String, validMeetingID(test) else { return }
            startSpeechProbe(test)
        case "cancelSpeechTest":
            if let test = body["testId"] as? String, test == speechTestID {
                sendHelper(["command": "cancel_speech_probe", "test_id": test])
            }
        case "revokeSpeech":
            if let meeting = body["meetingId"] as? String, meeting == activeMeeting {
                // The backend remains the authority. Promptly recheck the
                // lease rather than trusting the page to grant credentials.
                revokeSpeech()
            }
        case "setThemePreference":
            guard let preference = body["themePreference"] as? String, ["system", "light", "dark"].contains(preference) else { return }
            applyThemePreference(preference)
            publishNativeState()
        case "setLayoutPreference":
            guard let preference = body["layoutPreference"] as? String, ["a", "b"].contains(preference) else { return }
            layoutPreference = preference
            UserDefaults.standard.set(preference, forKey: "layoutPreference")
            publishNativeState()
        case "setAudioSource":
            guard let raw = body["audioSource"] as? String, let input = LiveAudioInput(rawValue: raw) else { return }
            guard activeMeeting == nil, !beginningCapture else { publishNativeState(); return }
            selectInput(input)
        case "openMicrophoneSettings":
            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") {
                NSWorkspace.shared.open(url)
            }
        case "chooseAudioFile": chooseAudioFile()
        case "useMeetingAudio":
            guard let meeting = body["meetingId"] as? String, validMeetingID(meeting),
                  let file = body["fileId"] as? String, activeMeeting == nil, !beginningCapture else { return }
            useMeetingAudio(meeting: meeting, fileID: file)
        case "importAudioFile":
            guard let id = body["meetingId"] as? String, validMeetingID(id),
                  let fileID = body["fileId"] as? String, fileID == selectedAudio?.id else { return }
            beginFileImport(meeting: id, fileID: fileID)
        case "resumeAudioImport":
            guard let id = body["meetingId"] as? String, validMeetingID(id),
                  let fileID = body["fileId"] as? String, fileID == selectedAudio?.id else { return }
            beginFileImport(meeting: id, fileID: fileID, resume: true)
        case "startCapture", "stopCapture":
            guard let id = body["meetingId"] as? String, validMeetingID(id), pageLoaded else { return }
            if action == "startCapture" {
                guard let raw = body["audioSource"] as? String, let input = LiveAudioInput(rawValue: raw) else { return }
                beginCapture(expectedMeeting: id, input: input)
            }
            else { stopRequestedCapture(expectedMeeting: id) }
        case "checkModel": if modelItem.isEnabled { checkModel() }
        case "installModel": if installItem.isEnabled { installModel() }
        case "openConfiguration": openConfiguration()
        case "chooseOutputDirectory": chooseOutputDirectory()
        case "openOutput":
            if let value = body["meetingId"] {
                guard let id = value as? String, validMeetingID(id) else { return }
                openOutput(meetingID: id)
            } else { openOutput(meetingID: nil) }
        case "openMeetingAudio":
            guard let id = body["meetingId"] as? String, validMeetingID(id), let fileID = body["fileId"] as? String else { return }
            Task {
                do {
                    let snapshot = try await meetingSnapshot(id)
                    guard let meeting = snapshot["meeting"] as? [String: Any],
                          let recordings = meeting["recordings"] as? [[String: Any]],
                          let record = recordings.first(where: { $0["id"] as? String == fileID }),
                          ["saved", "partial"].contains(record["state"] as? String ?? ""),
                          let relative = record["path"] as? String else { throw DesktopError(message: "录音尚未保存。") }
                    let url = try meetingFileURL(relative, meeting: meeting)
                    guard FileManager.default.fileExists(atPath: url.path) else {
                        throw DesktopError(message: "找不到本场录音。")
                    }
                    if let player = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.QuickTimePlayerX") {
                        NSWorkspace.shared.open([url], withApplicationAt: player, configuration: NSWorkspace.OpenConfiguration()) { [weak self] _, error in
                            if error != nil { Task { @MainActor in self?.showNativeError("无法播放录音，请打开会议文件夹查看原文件。") } }
                        }
                    } else { NSWorkspace.shared.activateFileViewerSelecting([url]) }
                } catch { showNativeError(error.localizedDescription) }
            }
        default: break
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if navigationAction.shouldPerformDownload, isLocalBlob(url), isWorkbench(webView.url),
           isWorkbenchFrame(navigationAction.sourceFrame), exports.isEmpty {
            decisionHandler(.download); return
        }
        if isWorkbench(url), navigationAction.targetFrame?.isMainFrame == true { decisionHandler(.allow); return }
        if navigationAction.navigationType == .linkActivated { openExternal(url) }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { openExternal(url) }
        return nil
    }

    private func openExternal(_ url: URL) {
        guard ["http", "https"].contains(url.scheme), !isLocal(url), url.user == nil, url.password == nil else { return }
        NSWorkspace.shared.open(url)
    }

    private func openConfiguration() {
        do {
            let manager = FileManager.default
            try manager.createDirectory(at: stateDirectory, withIntermediateDirectories: true,
                                        attributes: [.posixPermissions: 0o700])
            let url = stateDirectory.appendingPathComponent("workbench.env")
            if !manager.fileExists(atPath: url.path) {
                // A private template never imports repo credentials. Calls also
                // require configured services and each meeting's visible controls.
                let template = """
                # 修改后请退出并重新打开应用。只在本机填写密钥。
                # 配置服务后，新会议默认联网查证；创建时可关闭。调用不设次数上限。
                ZSJH_LIVE_MODEL_ENDPOINT=https://api.deepseek.com
                ZSJH_LIVE_MODEL_PROVIDER=deepseek
                ZSJH_LIVE_MODEL_NAME=deepseek-flash
                ZSJH_LIVE_MODEL_THINKING=disabled
                ZSJH_LIVE_MODEL_MAX_TOKENS=8192
                ZSJH_LIVE_MODEL_API_KEY=
                ZSJH_LIVE_MODEL_REQUEST_LIMIT=0
                ZSJH_WB_MODEL_REQUEST_LIMIT=unlimited
                ZSJH_WB_MODEL_APPROVED=true
                ZSJH_WB_MODEL_MEETING_IDS=
                ZSJH_WB_SEARCH_APPROVED=true
                ZSJH_WB_SEARCH_MEETING_IDS=
                ZSJH_WB_SEARCH_ENDPOINT=https://api.deepseek.com
                ZSJH_WB_SEARCH_API_KEY=
                ZSJH_WB_SEARCH_REQUEST_LIMIT=unlimited

                """
                let descriptor = Darwin.open(url.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, S_IRUSR | S_IWUSR)
                guard descriptor >= 0 else { throw DesktopError(message: "无法创建本机配置文件，请检查目录权限。") }
                let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
                do { try handle.write(contentsOf: Data(template.utf8)); try handle.close() }
                catch { try? handle.close(); throw error }
            }
            let attributes = try manager.attributesOfItem(atPath: url.path)
            guard attributes[.type] as? FileAttributeType == .typeRegular else {
                throw DesktopError(message: "配置文件必须是本机普通文件，不能是链接。")
            }
            if let editor = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.TextEdit") {
                NSWorkspace.shared.open([url], withApplicationAt: editor, configuration: NSWorkspace.OpenConfiguration()) { [weak self] _, error in
                    if error != nil { Task { @MainActor in self?.showNativeError("无法打开配置文件，请在 Finder 中查看本机配置目录。") } }
                }
            } else { NSWorkspace.shared.activateFileViewerSelecting([url]) }
        } catch { showNativeError(error.localizedDescription) }
    }

    private func openOutput(meetingID: String?) {
        Task {
            do {
                let destination: URL
                if let id = meetingID {
                    let snapshot = try await meetingSnapshot(id)
                    guard let meeting = snapshot["meeting"] as? [String: Any], let relative = meeting["directory"] as? String else {
                        throw DesktopError(message: "该会议资料目录不可用。")
                    }
                    destination = try meetingFileURL(relative, meeting: meeting)
                } else {
                    destination = try await configuredOutputRoot()
                }
                guard !closing, pageLoaded, isWorkbench(webView.url) else { return }
                var directory: ObjCBool = false
                guard FileManager.default.fileExists(atPath: destination.path, isDirectory: &directory), directory.boolValue,
                      NSWorkspace.shared.open(destination) else { throw DesktopError(message: "会议资料目录不存在或无法打开。") }
            } catch { showNativeError(error.localizedDescription) }
        }
    }

    private func meetingFileURL(_ relative: String, meeting: [String: Any]) throws -> URL {
        guard let path = meeting["output_root"] as? String, path.hasPrefix("/"),
              let directory = meeting["directory"] as? String, !directory.isEmpty,
              !relative.hasPrefix("/"), !directory.hasPrefix("/"),
              !directory.split(separator: "/").contains("..") else {
            throw DesktopError(message: "会议保存位置不可用，请重新打开应用后重试。")
        }
        let root = URL(fileURLWithPath: path, isDirectory: true).standardizedFileURL
        let folder = root.appendingPathComponent(directory).standardizedFileURL
        let file = root.appendingPathComponent(relative).standardizedFileURL
        guard folder != root, contains(folder, under: root), contains(file, under: folder),
              file == folder || file.deletingLastPathComponent() == folder,
              contains(folder.resolvingSymlinksInPath(), under: root.resolvingSymlinksInPath()),
              contains(file.resolvingSymlinksInPath(), under: folder.resolvingSymlinksInPath()) else {
            throw DesktopError(message: "会议文件路径超出本场保存位置。")
        }
        return file
    }

    private func configuredOutputRoot() async throws -> URL {
        let data = try await request("/live/v2/storage", owner: true)
        guard let settings = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let path = settings["output_root"] as? String, path.hasPrefix("/") else {
            throw DesktopError(message: "无法读取会议文件保存位置。")
        }
        return URL(fileURLWithPath: path, isDirectory: true)
    }

    private var choosingOutputDirectory = false
    private func chooseOutputDirectory() {
        guard hostReady, !choosingOutputDirectory, let window else { return }
        choosingOutputDirectory = true
        Task {
            do {
                let current = try await configuredOutputRoot()
                guard !closing else { choosingOutputDirectory = false; return }
                let panel = NSOpenPanel()
                panel.title = "选择会议文件保存位置"
                panel.message = "新会议保存到这里，已有会议继续使用原位置。"
                panel.prompt = "使用此文件夹"
                panel.canChooseFiles = false; panel.canChooseDirectories = true
                panel.canCreateDirectories = true; panel.allowsMultipleSelection = false
                panel.directoryURL = current
                panel.beginSheetModal(for: window) { [weak self = self] response in
                    guard let self else { return }
                    guard response == .OK, let url = panel.url else { self.choosingOutputDirectory = false; return }
                    Task { @MainActor in
                        defer { self.choosingOutputDirectory = false }
                        do {
                            let body = try JSONSerialization.data(withJSONObject: ["output_root": url.path])
                            _ = try await self.request("/live/v2/storage", body: body, method: "PUT", owner: true)
                            _ = try? await self.webView.evaluateJavaScript("window.dispatchEvent(new CustomEvent('zsjh-storage-changed'));")
                        } catch {
                            // A lost response does not prove the server rolled
                            // back the setting. Read it back before reporting.
                            let message = error.localizedDescription
                            if let actual = try? await self.configuredOutputRoot() {
                                _ = try? await self.webView.evaluateJavaScript("window.dispatchEvent(new CustomEvent('zsjh-storage-changed'));")
                                if actual.resolvingSymlinksInPath() != url.resolvingSymlinksInPath() {
                                    self.showNativeError("未保存所选位置：" + message + "\n当前保存位置：" + actual.path)
                                }
                            } else {
                                self.showNativeError("保存结果暂无法确认，请重新打开设置核对。\n" + message)
                            }
                        }
                    }
                }
            } catch { choosingOutputDirectory = false; showNativeError(error.localizedDescription) }
        }
    }

    private func contains(_ candidate: URL, under root: URL) -> Bool {
        candidate.isFileURL && root.isFileURL && candidate.pathComponents.starts(with: root.pathComponents)
    }

    private func showNativeError(_ message: String) {
        guard !closing else { return }
        let alert = NSAlert(); alert.messageText = message; alert.addButton(withTitle: "好")
        alert.beginSheetModal(for: window)
    }

    private func isLocalBlob(_ url: URL) -> Bool {
        guard url.scheme == "blob", let origin = URL(string: String(url.absoluteString.dropFirst(5))) else { return false }
        return isLocal(origin)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        guard exports.isEmpty, isWorkbench(webView.url), isWorkbenchFrame(download.originatingFrame),
              let url = download.originalRequest?.url, isLocalBlob(url) else { download.cancel(nil); return }
        exports[ObjectIdentifier(download)] = MarkdownExport(download: download)
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let key = ObjectIdentifier(download)
        guard exports[key] != nil, isWorkbench(webView.url), isWorkbenchFrame(download.originatingFrame),
              ["text/markdown", "text/plain"].contains(response.mimeType?.lowercased() ?? ""),
              let filename = markdownFilename(suggestedFilename) else {
            exports.removeValue(forKey: key); completionHandler(nil); return
        }
        let panel = NSSavePanel()
        panel.title = "导出 Markdown"
        panel.nameFieldStringValue = filename
        panel.allowedContentTypes = [UTType(filenameExtension: "md") ?? .plainText]
        panel.canCreateDirectories = true
        panel.beginSheetModal(for: window) { [weak self] result in
            guard let self else { completionHandler(nil); return }
            guard !self.closing, result == .OK, let destination = panel.url,
                  destination.isFileURL, destination.pathExtension.lowercased() == "md",
                  var record = self.exports[key] else {
                self.exports.removeValue(forKey: key); completionHandler(nil); return
            }
            do {
                // WebKit requires a nonexistent destination. Stage privately so
                // an approved replacement occurs only after the whole download.
                let staging = FileManager.default.temporaryDirectory
                    .appendingPathComponent("zsjh-markdown-" + UUID().uuidString, isDirectory: true)
                try FileManager.default.createDirectory(at: staging, withIntermediateDirectories: false,
                                                        attributes: [.posixPermissions: 0o700])
                record.temporaryDirectory = staging; record.destination = destination
                record.replacingExisting = FileManager.default.fileExists(atPath: destination.path)
                self.exports[key] = record
                completionHandler(staging.appendingPathComponent("document.md"))
            } catch {
                self.exports.removeValue(forKey: key); completionHandler(nil)
                self.showNativeError("无法创建文档导出文件，请检查保存位置。")
            }
        }
    }

    private func markdownFilename(_ suggestion: String) -> String? {
        let basename = (suggestion as NSString).lastPathComponent
        guard (basename as NSString).pathExtension.lowercased() == "md" else { return nil }
        let rawStem = (basename as NSString).deletingPathExtension
        let stem = rawStem.unicodeScalars.map { CharacterSet.controlCharacters.contains($0) || "/\\:".unicodeScalars.contains($0) ? "_" : String($0) }.joined()
        var shortStem = ""
        for character in stem {
            guard shortStem.utf8.count + String(character).utf8.count <= 220 else { break }
            shortStem.append(character)
        }
        return (shortStem.isEmpty || shortStem == "." ? "研究回答" : shortStem) + ".md"
    }

    func download(_ download: WKDownload, willPerformHTTPRedirection response: HTTPURLResponse,
                  newRequest request: URLRequest, decisionHandler: @escaping (WKDownload.RedirectPolicy) -> Void) {
        decisionHandler(.cancel)
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let record = exports.removeValue(forKey: ObjectIdentifier(download)),
              let staging = record.temporaryDirectory, let destination = record.destination else { return }
        defer { try? FileManager.default.removeItem(at: staging) }
        do {
            let source = staging.appendingPathComponent("document.md")
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: source.path)
            if record.replacingExisting {
                _ = try FileManager.default.replaceItemAt(destination, withItemAt: source)
            } else { try FileManager.default.moveItem(at: source, to: destination) }
        } catch { showNativeError("Markdown 导出未完成，请重试或换一个保存位置。") }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        guard let record = exports.removeValue(forKey: ObjectIdentifier(download)) else { return }
        if let staging = record.temporaryDirectory { try? FileManager.default.removeItem(at: staging) }
        if (error as NSError).code != NSURLErrorCancelled { showNativeError("Markdown 导出未完成，请重试。") }
    }

    private func launchHelper() throws {
        guard let resource = Bundle.main.resourceURL else { throw DesktopError(message: "缺少音频组件。") }
        let process = Process(), input = Pipe(), output = Pipe()
        process.executableURL = resource.appendingPathComponent("ZSJHAudioHelper.app/Contents/MacOS/AudioHelper")
        process.environment = childEnvironment
        process.standardInput = input; process.standardOutput = output; process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] _ in Task { @MainActor in
            guard let self, !self.closing else { return }
            self.failBridge("音频组件已退出，收听已停止。")
            self.startItem.isEnabled = false; self.modelItem.isEnabled = false; self.installItem.isEnabled = false
        } }
        try process.run()
        helper = process; helperInput = input.fileHandleForWriting
        LineReader(output.fileHandleForReading).start(line: { [weak self] value in deliverToMain { self?.audioLine(value) } },
            ended: { [weak self] in Task { @MainActor in
                guard let self, !self.closing, self.helper?.isRunning == true else { return }
                self.status = "音频显示连接中断，录音与关键事件仍在本机保存。"
            } })
    }

    private func sendHelper(_ command: [String: Any]) {
        guard helper?.isRunning == true, let input = helperInput,
              var data = try? JSONSerialization.data(withJSONObject: command) else { return }
        data.append(10)
        do { try input.write(contentsOf: data) }
        catch { failBridge("音频控制通道不可用，收听已停止。") }
    }

    private func formatDuration(_ milliseconds: Int) -> String {
        let total = max(0, milliseconds / 1000)
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        let seconds = total % 60
        return hours > 0 ? String(format: "%d:%02d:%02d", hours, minutes, seconds)
            : String(format: "%02d:%02d", minutes, seconds)
    }

    private func validMeetingID(_ id: String) -> Bool {
        !id.isEmpty && id.utf8.count <= 120
            && id.allSatisfy { $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-" || $0 == "_") }
    }

    private func selectedMeeting() async throws -> String {
        guard pageLoaded, isWorkbench(webView.url) else { throw DesktopError(message: "请等待会议页面完成连接。") }
        let value = try await webView.evaluateJavaScript("document.getElementById('meeting-select')?.value || ''")
        guard let id = value as? String, validMeetingID(id) else {
            throw DesktopError(message: "请先在应用选择一场进行中的会议。")
        }
        return id
    }

    private func request(_ path: String, body: Data? = nil, method: String? = nil, owner: Bool = false,
                         speech: Bool = false) async throws -> Data {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port)\(path)")!)
        request.setValue("Bearer " + (owner ? ownerToken : agentToken), forHTTPHeaderField: "Authorization")
        if speech {
            guard !speechToken.isEmpty else { throw DesktopError(message: "语音组件尚未连接。") }
            request.setValue(speechToken, forHTTPHeaderField: "X-ZSJH-Speech-Token")
        }
        if let body { request.httpMethod = "POST"; request.httpBody = body; request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        if let method { request.httpMethod = method }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw DesktopError(message: "会议服务响应无效。") }
        guard (200...299).contains(http.statusCode) else {
            if owner || speech, let error = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let message = error["error"] as? String { throw DesktopError(message: message) }
            throw HTTPFailure(status: http.statusCode)
        }
        return data
    }

    private func refreshSpeechConfiguration() async throws {
        let data = try await request("/live/v2/settings", owner: true)
        guard let settings = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let value = settings["speech"] as? [String: Any],
              let revision = settings["revision"] as? Int, revision >= speechRevision else { return }
        speechRevision = revision
        speechLiveProvider = value["live_provider"] as? String ?? "apple_local"
        speechFileProvider = value["file_provider"] as? String ?? "apple_local"
        let statuses = value["verification"] as? [String: [String: [String: Any]]] ?? [:]
        speechLiveReady = statuses[speechLiveProvider]?["live"]?["status"] as? String == "passed"
        speechFileReady = statuses[speechFileProvider]?["file"]?["status"] as? String == "passed"
        publishNativeState()
    }

    private func startSpeechProbe(_ testID: String) {
        guard hostReady, speechTestID == nil else { return }
        speechTestID = testID; speechTestError = nil
        Task {
            do {
                let path = "/live/v2/settings/speech/tests/\(testID)"
                let test = try await request(path, owner: true)
                guard let info = try JSONSerialization.jsonObject(with: test) as? [String: Any],
                      let kind = info["kind"] as? String, ["live", "file"].contains(kind) else {
                    throw DesktopError(message: "找不到待执行的转写测试。")
                }
                let data = try await request(path + "/session", body: Data("{}".utf8), owner: true, speech: true)
                guard var candidate = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let sampleVersion = candidate.removeValue(forKey: "sample_version") as? String else {
                    throw DesktopError(message: "语音测试配置不完整，请重新安装应用。")
                }
                sendHelper(["command": "speech_probe", "test_id": testID, "kind": kind,
                            "speech": candidate, "sample_version": sampleVersion])
                try? await Task.sleep(for: .seconds(30))
                if speechTestID == testID {
                    sendHelper(["command": "cancel_speech_probe", "test_id": testID])
                    speechTestID = nil; speechTestError = "语音转写测试超时。"; publishNativeState()
                }
            } catch {
                speechTestID = nil; speechTestError = error.localizedDescription
                publishNativeState()
            }
        }
    }

    private func finishSpeechProbe(_ event: [String: Any]) {
        guard let testID = event["test_id"] as? String, speechTestID == testID else { return }
        var report = event
        report.removeValue(forKey: "type")
        report.removeValue(forKey: "test_id")
        Task {
            defer { if speechTestID == testID { speechTestID = nil }; publishNativeState() }
            do {
                let body = try JSONSerialization.data(withJSONObject: report)
                _ = try await request("/live/v2/settings/speech/tests/\(testID)/result", body: body, owner: true, speech: true)
            } catch { speechTestError = error.localizedDescription }
        }
    }

    private func speechSession(meeting: String, epoch: String, kind: String,
                               sources: [AudioSourceKind]) async throws -> [String: Any] {
        let body = try speechRequest(epoch: epoch, kind: kind, sources: sources)
        let issuedAt = ContinuousClock.now
        let data = try await request("/live/v2/meetings/\(meeting)/speech-session", body: body, speech: true)
        guard var result = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let seconds = result["lease_seconds"] as? Double else {
            throw DesktopError(message: "云端转写授权响应无效。")
        }
        result["lease_expires_uptime"] = ProcessInfo.processInfo.systemUptime + max(0, seconds - secondsSince(issuedAt))
        result["lease_seconds"] = max(0, seconds - secondsSince(issuedAt))
        return result
    }

    private func speechRequest(epoch: String, kind: String, sources: [AudioSourceKind]) throws -> Data {
        try JSONSerialization.data(withJSONObject: ["task_id": epoch, "kind": kind, "sources": sources.map(\.rawValue)])
    }

    private func secondsSince(_ start: ContinuousClock.Instant) -> Double {
        let elapsed = start.duration(to: .now).components
        return Double(elapsed.seconds) + Double(elapsed.attoseconds) / 1e18
    }

    private func renewSpeechLease(meeting: String, epoch: String, kind: String, sources: [AudioSourceKind]) {
        speechLeaseTask?.cancel()
        speechLeaseTask = Task { [weak self] in
            var consecutiveFailures = 0
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(1)) } catch { return }
                guard let self, self.activeMeeting == meeting, self.activeEpoch == epoch else { return }
                let issuedAt = ContinuousClock.now
                do {
                    let body = try self.speechRequest(epoch: epoch, kind: kind, sources: sources)
                    let response = try await self.request("/live/v2/meetings/\(meeting)/speech-session/lease", body: body, speech: true)
                    guard !Task.isCancelled, self.activeEpoch == epoch else { return }
                    let value = try JSONSerialization.jsonObject(with: response) as? [String: Any]
                    guard let seconds = value?["lease_seconds"] as? Double else { throw DesktopError(message: "语音续租响应无效。") }
                    let remaining = max(0, seconds - self.secondsSince(issuedAt))
                    consecutiveFailures = 0
                    if kind == "live" {
                        self.sendHelper(["command": "speech_lease", "capture_epoch": epoch,
                                         "lease_expires_uptime": ProcessInfo.processInfo.systemUptime + remaining])
                    } else { self.fileLease?.renew(seconds: remaining) }
                } catch {
                    guard !Task.isCancelled, self.activeEpoch == epoch else { return }
                    consecutiveFailures += 1
                    if consecutiveFailures >= 3 {
                        self.revokeSpeech()
                        return
                    }
                    // A transient local-server or network delay must not abort a long import.
                    continue
                }
            }
        }
    }

    private func revokeSpeech() {
        speechLeaseTask?.cancel(); speechLeaseTask = nil
        if let activeEpoch, activeInput == "live" {
            sendHelper(["command": "speech_revoke", "capture_epoch": activeEpoch])
        }
        fileLease?.revoke()
    }

    private func meetingSnapshot(_ id: String) async throws -> [String: Any] {
        guard validMeetingID(id) else { throw DesktopError(message: "会议标识无效。") }
        let data = try await request("/live/v2/meetings/\(id)/snapshot")
        guard let value = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              (value["meeting"] as? [String: Any])?["id"] as? String == id else {
            throw DesktopError(message: "会议服务返回了不匹配的会议。")
        }
        return value
    }

    private func meetingState(_ id: String) async throws -> String {
        (try await meetingSnapshot(id)["meeting"] as? [String: Any])?["state"] as? String ?? "unknown"
    }

    private func validateCaptureMeeting(_ id: String, inputKind: String = "live") async throws {
        let homeData = try await request("/live/v2/home")
        guard let home = try JSONSerialization.jsonObject(with: homeData) as? [String: Any],
              let member = home["member"] as? [String: Any], member["role"] as? String == "agent",
              member["enabled"] as? Bool == true, let projectID = member["project_id"] as? String,
              !projectID.isEmpty else { throw DesktopError(message: "本机采音身份不可用，请重新打开应用。") }
        let snapshot = try await meetingSnapshot(id)
        guard let meeting = snapshot["meeting"] as? [String: Any], meeting["state"] as? String == "active" else {
            throw DesktopError(message: "当前会议已结束，请新建或选择会议。")
        }
        guard meeting["project_id"] as? String == projectID,
              (snapshot["capabilities"] as? [String: Any])?["audio_enabled"] as? Bool == true else {
            throw DesktopError(message: "当前会议未启用本机采音。")
        }
        guard (meeting["input_kind"] as? String ?? "live") == inputKind else {
            throw DesktopError(message: "会议输入方式不匹配，请从对应入口开始。")
        }
        guard inputKind == "audio_file" || meeting["data_scope"] as? String == "real" else {
            throw DesktopError(message: "示例会议不收听真实音频，请创建一场新会议。")
        }
    }

    private func postAudio(_ meeting: String, _ data: Data) async throws {
        _ = try await request("/internal/live/v2/meetings/\(meeting)/audio-events", body: data)
    }

    @objc private func startCapture() { beginCapture(expectedMeeting: nil) }

    private func chooseAudioFile() {
        guard hostReady, activeMeeting == nil, !beginningCapture, let window else {
            selectedAudioError = "请先停止当前收听或导入。"; publishNativeState(); return
        }
        let panel = NSOpenPanel()
        panel.title = "导入会议音频"
        panel.prompt = "选择音频"
        panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        panel.allowedContentTypes = AudioFileInfo.extensions.compactMap { UTType(filenameExtension: $0) }
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self else { return }
            guard response == .OK, let url = panel.url else { return }
            self.audioSelectionTask?.cancel()
            self.selectedAudio = nil; self.selectedAudioError = ""
            self.audioSelectionTask = Task { [weak self] in
                guard let self else { return }
                do {
                    let info = try await AudioFileInfo.inspect(url)
                    try Task.checkCancellation()
                    guard info.name.count <= 255 else { throw DesktopError(message: "文件名过长，请缩短后重试。") }
                    self.selectedAudio = SelectedAudio(id: UUID().uuidString, url: url, info: info)
                } catch {
                    guard !Task.isCancelled else { return }
                    self.selectedAudioError = error.localizedDescription
                }
                self.audioSelectionTask = nil
                self.publishNativeState()
            }
            self.publishNativeState()
        }
    }

    private func useMeetingAudio(meeting: String, fileID: String) {
        audioSelectionTask?.cancel()
        audioSelectionTask = Task {
            do {
                let snapshot = try await meetingSnapshot(meeting)
                guard let source = snapshot["meeting"] as? [String: Any],
                      let recordings = source["recordings"] as? [[String: Any]],
                      let record = recordings.first(where: { $0["id"] as? String == fileID }),
                      ["saved", "partial"].contains(record["state"] as? String ?? ""),
                      let relative = record["path"] as? String else { throw DesktopError(message: "找不到已保存的录音。") }
                let url = try meetingFileURL(relative, meeting: source)
                let info = try await AudioFileInfo.inspect(url)
                guard !Task.isCancelled, activeMeeting == nil, !beginningCapture else { return }
                selectedAudio = SelectedAudio(id: UUID().uuidString, url: url, info: info)
                selectedAudioError = ""
            } catch {
                guard !Task.isCancelled else { return }
                selectedAudio = nil; selectedAudioError = error.localizedDescription
            }
            publishNativeState()
        }
    }

    private func beginFileImport(meeting: String, fileID: String, resume: Bool = false) {
        guard let selection = selectedAudio, selection.id == fileID, (resume || canImport) else {
            selectedAudioError = "所选文件转写服务尚未就绪，或当前已有任务。"; publishNativeState(); return
        }
        beginningCapture = true
        fileTask = Task { [self] in
            var registered = false
            var epoch = UUID().uuidString
            var startMS = 0
            var revision = 1
            var initialSequence = 0
            do {
                guard try await selectedMeeting() == meeting else { throw DesktopError(message: "会议已切换，请重新导入。") }
                try await validateCaptureMeeting(meeting, inputKind: "audio_file")
                guard hostReady, !quitting, try await selectedMeeting() == meeting, activeMeeting == nil else {
                    throw DesktopError(message: "应用状态已变化，请重新导入。")
                }
                try await refreshSpeechConfiguration()
                let info = try await AudioFileInfo.inspect(selection.url)
                var provider = speechFileProvider
                let registeredData: Data
                if resume {
                    let snapshot = try await meetingSnapshot(meeting)
                    guard let source = snapshot["meeting"] as? [String: Any],
                          let existing = source["audio_import"] as? [String: Any],
                          let existingID = existing["import_id"] as? String,
                          let existingProvider = existing["provider"] as? String,
                          ["failed", "cancelled", "interrupted"].contains(existing["state"] as? String ?? "") else {
                        throw DesktopError(message: "当前会议没有可恢复的导入任务。")
                    }
                    epoch = existingID
                    provider = existingProvider
                    let body = try JSONSerialization.data(withJSONObject: ["import_id": epoch])
                    registeredData = try await request("/internal/live/v2/meetings/\(meeting)/audio-import/resume", body: body, speech: true)
                } else {
                    guard provider == "apple_local" ? fileModelState == "installed" : speechFileReady else {
                        throw DesktopError(message: "所选文件转写服务尚未就绪。")
                    }
                    let body = try JSONSerialization.data(withJSONObject: ["import_id": epoch, "file_name": info.name,
                        "duration_ms": info.durationMS, "bytes": info.bytes, "provider": provider,
                        "settings_revision": speechRevision])
                    registeredData = try await request("/internal/live/v2/meetings/\(meeting)/audio-import", body: body, speech: true)
                }
                registered = true
                guard let registeredMeeting = try JSONSerialization.jsonObject(with: registeredData) as? [String: Any],
                      let job = registeredMeeting["audio_import"] as? [String: Any],
                      let relative = job["original_path"] as? String else { throw DesktopError(message: "音频归档位置不可用。") }
                if let resumeFrom = job["resume_from_ms"] as? Int { startMS = max(0, resumeFrom) }
                if let resumeCount = job["resume_count"] as? Int { revision = max(1, resumeCount + 1) }
                if let lastSequence = job["last_event_seq"] as? Int { initialSequence = max(0, lastSequence) }
                let destination = try meetingFileURL(relative, meeting: registeredMeeting)
                activeInput = "audio_file"; captureProblem = nil
                sourceLevels = [:]; sourceFrames = [:]; transcription = [:]
                captureFinish = nil; captureFinishMeetingId = meeting
                activeEpoch = epoch; activeMeeting = meeting; lastMeeting = meeting; captureState = "importing"
                beginningCapture = false
                if resume {
                    status = "正在从 \(formatDuration(startMS)) 继续转写…"
                } else {
                    status = "正在保存原始音频副本…"
                    try await Task.detached(priority: .utility) {
                        let part = destination.appendingPathExtension("part")
                        guard !FileManager.default.fileExists(atPath: destination.path), !FileManager.default.fileExists(atPath: part.path) else {
                            throw DesktopError(message: "音频归档文件已存在，保留原文件。")
                        }
                        try FileManager.default.copyItem(at: selection.url, to: part)
                        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: part.path)
                        try FileManager.default.moveItem(at: part, to: destination)
                    }.value
                    var recordingEvent = AudioEvent(meetingID: meeting, captureEpoch: epoch, streamID: "archive-\(epoch)",
                        seq: 1, type: "audio.recording", source: .file)
                    recordingEvent.state = "saved"; recordingEvent.audioEndMS = info.durationMS
                    try await postAudio(meeting, JSONEncoder().encode(recordingEvent))
                }
                try Task.checkCancellation()
                status = resume ? "已找到原始音频，正在继续转写…" : "原始音频已保存，正在转写…"
                let worker = FileTranscription(); fileTranscriber = worker
                worker.onEvent = { [weak self] event in
                    guard let self, !self.closing else { throw CancellationError() }
                    let data = try JSONEncoder().encode(event)
                    try await self.postAudio(meeting, data)
                }
                if provider == "apple_local" {
                    try await worker.run(url: destination, meetingID: meeting, epoch: epoch, startMS: startMS, revision: revision)
                } else {
                    let privateConfig = try await speechSession(meeting: meeting, epoch: epoch, kind: "file", sources: [.file])
                    let config = try JSONDecoder().decode(SpeechSessionConfig.self, from: JSONSerialization.data(withJSONObject: privateConfig))
                    let lease = SpeechLease(seconds: config.leaseSeconds ?? 0)
                    fileLease = lease
                    renewSpeechLease(meeting: meeting, epoch: epoch, kind: "file", sources: [.file])
                    try await worker.run(url: destination, meetingID: meeting, epoch: epoch, speech: config,
                                        lease: lease, startMS: startMS, revision: revision, initialSequence: initialSequence)
                }
                try Task.checkCancellation()
                captureState = "stopping"; status = "正在保存完整转写…"
                let finish = try JSONSerialization.data(withJSONObject: ["import_id": epoch, "state": "completed"])
                _ = try await request("/internal/live/v2/meetings/\(meeting)/audio-import/finish", body: finish)
                status = "文件转写已完成，查证可继续进行"
                captureState = "stopped"; captureFinish = "complete"
            } catch {
                let cancelled = error is CancellationError || Task.isCancelled
                if registered, !closing {
                    let message = String(error.localizedDescription.prefix(300))
                    let finish = try? JSONSerialization.data(withJSONObject: ["import_id": epoch,
                        "state": cancelled ? "cancelled" : "failed",
                        "error": cancelled ? "导入已停止，已有转写保留。" : message])
                    let save = Task { [self] in
                        guard let finish else { return false }
                        return (try? await request("/internal/live/v2/meetings/\(meeting)/audio-import/finish", body: finish)) != nil
                    }
                    captureFinish = await save.value ? "with_gaps" : "failed"
                }
                captureState = cancelled && captureFinish != "failed" ? "stopped" : "error"
                status = cancelled ? "导入已停止，已有转写保留" : "导入失败：" + error.localizedDescription
            }
            revokeSpeech(); fileLease = nil; fileTranscriber = nil; fileTask = nil; beginningCapture = false
            activeEpoch = nil; activeMeeting = nil; selectedAudio = nil
            publishNativeState()
        }
    }

    private func selectInput(_ input: LiveAudioInput) {
        selectedInput = input
        UserDefaults.standard.set(input.rawValue, forKey: "liveAudioInput")
        publishNativeState()
    }

    private func beginCapture(expectedMeeting: String?, input: LiveAudioInput? = nil) {
        guard canStart else { publishNativeState(); return }
        let requestedInput = input ?? selectedInput
        beginningCapture = true
        status = "正在准备会议…"
        Task { [self] in
            defer { beginningCapture = false }
            var unopened: (CaptureEventOutbox.Manifest, URL)?
            do {
                let meeting = try await selectedMeeting()
                guard expectedMeeting == nil || expectedMeeting == meeting else { throw DesktopError(message: "会议已切换，请重新开始收听。") }
                try await validateCaptureMeeting(meeting)
                try await refreshSpeechConfiguration()
                let provider = speechLiveProvider
                let sources = requestedInput.sources
                guard hostReady, (provider == "apple_local" ? modelState == "installed" : speechLiveReady),
                      !quitting, try await selectedMeeting() == meeting else { throw DesktopError(message: "会议或语音服务未就绪。") }
                let epoch = UUID().uuidString
                let manifest = CaptureEventOutbox.Manifest(meetingID: meeting, epoch: epoch, sources: sources)
                let outbox = outboxRoot.appendingPathComponent(epoch, isDirectory: true)
                try manifest.write(to: outbox)
                unopened = (manifest, outbox)
                let body = try JSONSerialization.data(withJSONObject: ["recording_id": epoch, "sources": sources.map(\.rawValue),
                    "provider": provider, "settings_revision": speechRevision])
                let registration = try await request("/internal/live/v2/meetings/\(meeting)/recordings", body: body, speech: true)
                let snapshot = try await meetingSnapshot(meeting)
                guard let registered = try JSONSerialization.jsonObject(with: registration) as? [String: Any],
                      let directory = registered["recording_directory"] as? String,
                      let savedMeeting = snapshot["meeting"] as? [String: Any],
                      let relative = savedMeeting["directory"] as? String,
                      directory == (try meetingFileURL(relative, meeting: savedMeeting)).path else {
                    throw DesktopError(message: "录音保存位置不可用，未开始收听。")
                }
                var command: [String: Any] = ["command": "start", "meeting_id": meeting, "sources": sources.map(\.rawValue),
                    "locale": "zh-CN", "capture_epoch": epoch, "recording_directory": directory, "outbox": outbox.path]
                if provider != "apple_local" {
                    var speech = try await speechSession(meeting: meeting, epoch: epoch, kind: "live", sources: sources)
                    command["lease_expires_uptime"] = speech.removeValue(forKey: "lease_expires_uptime")
                    command["speech"] = speech
                }
                guard hostReady, !quitting, try await selectedMeeting() == meeting, activeMeeting == nil else {
                    throw DesktopError(message: "会议已变化，未开始收听。")
                }
                captureGeneration += 1
                selectInput(requestedInput)
                sourceLevels = [:]; sourceFrames = [:]; transcription = [:]
                captureState = "starting"; captureProblem = nil
                captureFinish = nil; captureFinishMeetingId = meeting
                activeInput = "live"; activeEpoch = epoch; activeOutbox = outbox
                activeMeeting = meeting; lastMeeting = meeting
                status = "正在连接音频…"
                unopened = nil
                sendHelper(command)
                processActiveOutbox(manifest, directory: outbox)
                if provider != "apple_local" { renewSpeechLease(meeting: meeting, epoch: epoch, kind: "live", sources: sources) }
                monitorMeeting(meeting: meeting, epoch: epoch)
            } catch {
                if let (manifest, outbox) = unopened {
                    // A returned error cannot prove registration didn't commit.
                    // Persist a no-device-opened record and acknowledge it idempotently.
                    let writer = CaptureEventOutbox.Writer(directory: outbox, manifest: manifest)
                    writer.seal()
                    do { try await writer.flush() }
                    catch { status = "收尾记录写入失败，请检查磁盘空间和目录权限后重试。" }
                    // If the API rejected registration, there is no epoch to close.
                    // An unavailable API leaves the journal available for replay.
                    if let current = try? await meetingSnapshot(manifest.meetingID),
                       let row = current["meeting"] as? [String: Any],
                       (row["capture_epochs"] as? [String: Any])?[manifest.epoch] == nil {
                        try? FileManager.default.removeItem(at: outbox)
                    } else { _ = try? await drainOutbox(manifest, directory: outbox) }
                }
                captureState = "error"; status = error.localizedDescription
            }
        }
    }

    private func monitorMeeting(meeting: String, epoch: String) {
        healthTask = Task { [weak self] in
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(1)) } catch { return }
                guard let self, !self.closing, self.activeEpoch == epoch else { return }
                if let selected = try? await self.selectedMeeting(), selected != meeting {
                    self.stopWithStatus("会议切换，正在保存录音和转写…"); return
                }
                if let state = try? await self.meetingState(meeting), state != "active", !Task.isCancelled {
                    self.stopWithStatus("会议已改变，正在保存录音…"); return
                }
            }
        }
    }

    @objc private func stopCapture() { stopWithStatus("正在停止…") }

    private func stopRequestedCapture(expectedMeeting: String) {
        guard canStop else { publishNativeState(); return }
        let generation = captureGeneration
        Task {
            do {
                guard activeMeeting == expectedMeeting, try await selectedMeeting() == expectedMeeting else {
                    throw DesktopError(message: "当前收听的会议已变化，请检查会议状态。")
                }
                // Ended/expired meetings must still be stoppable. The authenticated
                // local snapshot and exact binding are required in every case.
                let state = try await meetingState(expectedMeeting)
                guard ["active", "ended", "expired"].contains(state), activeMeeting == expectedMeeting,
                      try await selectedMeeting() == expectedMeeting else {
                    throw DesktopError(message: "会议已切换，请等待收听安全停止。")
                }
                guard !closing, captureGeneration == generation else { return }
                stopWithStatus("正在停止…")
            } catch {
                // A failed verification never leaves an active input running.
                if !closing, activeMeeting == expectedMeeting, captureGeneration == generation { stopWithStatus("会议连接已变化，正在停止收听…") }
                else { publishNativeState() }
            }
        }
    }

    private func stopWithStatus(_ message: String) {
        guard activeMeeting != nil, captureState != "stopping", captureState != "stopped" else { return }
        if activeInput == "audio_file" {
            captureState = "stopping"; status = "正在停止导入并保存已有转写…"
            fileTask?.cancel()
            Task { await fileTranscriber?.cancel() }
            return
        }
        captureFinish = nil
        healthTask?.cancel(); captureState = "stopping"; stopItem.isEnabled = false
        status = message
        if let directory = activeOutbox, let manifest = try? CaptureEventOutbox.Manifest.read(from: directory) {
            processActiveOutbox(manifest, directory: directory)
        }
        sendHelper(["command": "stop"])
        stopWatchdog?.cancel()
        stopWatchdog = Task { [weak self] in
            do { try await Task.sleep(for: .seconds(10)) } catch { return }
            guard let self, self.activeMeeting != nil else { return }
            self.captureFinish = "failed"
            self.captureState = "error"
            self.status = "录音或转写收尾未确认。本机记录已保留；请检查磁盘空间和目录权限，再点击“重试保存”。"
            if !self.captureInputsStopped, let directory = self.activeOutbox,
               (try? CaptureEventOutbox.Seal.read(from: directory)) == nil {
                self.revokeSpeech()
                // A source that has not acknowledged Stop still needs EOF.
                // A stopped helper must remain alive to retry a disk failure.
                try? self.helperInput?.close(); self.helperInput = nil
            }
            self.publishNativeState()
        }
    }

    @objc private func checkModel() {
        sendHelper(["command": "model_status", "locale": "zh-CN"])
        Task { fileModelState = await LocalSpeechModels.status(locale: "zh-CN") }
    }

    @objc private func installModel() {
        let alert = NSAlert()
        alert.messageText = "安装 Apple 本地中文识别模型？"
        alert.informativeText = "系统可能从 Apple 下载资源。会议音频仍只在本机处理。"
        alert.addButton(withTitle: "安装"); alert.addButton(withTitle: "取消")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        status = "正在安装中文模型…"
        fileModelState = "downloading"
        Task {
            do {
                // Apple tracks availability for each application identity.
                // File analysis runs here; live capture runs in AudioHelper.
                try await LocalSpeechModels.install(locale: "zh-CN")
                fileModelState = await LocalSpeechModels.status(locale: "zh-CN")
                status = fileModelState == "installed" ? "文件转写已就绪" : "请重新检查中文模型"
            } catch {
                fileModelState = await LocalSpeechModels.status(locale: "zh-CN")
                status = "中文模型安装失败，请检查网络后重试。"
            }
        }
        if modelState != "installed" { modelState = "downloading" }
        sendHelper(["command": "install_model", "locale": "zh-CN", "allow_download": true])
    }

    private func audioLine(_ data: Data) {
        guard !closing, let event = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = event["type"] as? String else { return }
        if type == "speech.probe" { finishSpeechProbe(event); return }
        let meeting = event["meeting_id"] as? String ?? ""
        if type == "audio.error", event["code"] as? String == "model_install_failed" {
            // A failed download emits no model_status event. Clear the optimistic
            // downloading state so Settings can offer a retry without restarting.
            modelState = "unknown"
            checkModel()
            showNativeError("实时转写的中文模型安装失败，请检查网络后在设置中重试。")
            return
        }
        if type == "audio.model_status" {
            let state = event["state"] as? String ?? "unknown"
            let names = ["installed": "未在收听", "downloadable": "请先安装中文识别模型", "downloading": "正在下载中文模型…", "unsupported": "此设备不支持本地中文识别"]
            modelItem.title = state == "installed" ? "中文模型：已安装（重新检查）" : "检查中文模型"
            modelState = state
            if activeMeeting == nil { status = names[state] ?? "中文模型状态未知" }
            return // Model diagnostics never belong to a meeting, even after stop.
        }
        // The helper owns live devices only. Its delayed control events must
        // never change a file import's binding or progress.
        guard activeInput != "audio_file" else { return }
        if meeting.isEmpty {
            if type == "audio.error" {
                let message = "音频组件：" + (event["code"] as? String ?? "错误")
                if activeMeeting != nil { captureProblem = message; stopWithStatus(message + " · 正在停止收听…") }
                else { status = message }
            }
            return
        }
        guard meeting == activeMeeting, event["capture_epoch"] as? String == activeEpoch else { return }
        // The outbox delivers authoritative events. A delayed pipe message
        // from a previous epoch never changes a newer capture's state.
        if type == "transcript.interim", captureState == "listening",
           let stream = event["stream_id"] as? String, let segment = event["segment_id"] as? String {
            let key = stream + ":" + segment
            if pendingInterims[key] != nil || pendingInterims.count < 24 {
                pendingInterims[key] = (meeting, data)
                pumpInterims()
            }
        }
        switch type {
        case "audio.status" where event["state"] as? String == "running":
            // The core reports running only after every selected source starts.
            if captureState != "stopping" { captureState = "listening"; status = "正在收听" }
        case "audio.level":
            if let source = event["source"] as? String, selectedInput.sources.map(\.rawValue).contains(source) {
                sourceLevels[source] = (event["rms"] as? NSNumber)?.floatValue ?? 0
                sourceFrames[source] = (event["frames_received"] as? NSNumber)?.uint64Value ?? 0
                publishNativeState()
            }
        case "audio.stopped":
            // A stream stops before the final control status. Keep the binding
            // until the latter so its tail cannot race a new capture.
            status = "正在保存转写…"
        case "audio.status" where event["state"] as? String == "stopped":
            captureInputsStopped = true
            healthTask?.cancel(); captureState = "stopping"
        case "asr.status", "asr.gap", "asr.error":
            if let source = event["source"] as? String {
                var value = transcription[source] ?? [:]
                if type != "asr.gap" {
                    value["state"] = event["state"] as? String ?? event["status"] as? String ?? (type == "asr.error" ? "error" : "unknown")
                    value["code"] = event["code"] as? String
                    value["detail"] = event["detail"] as? String
                }
                if let provider = event["provider"] as? String { value["provider"] = provider }
                transcription[source] = value
                publishNativeState()
            }
        case "audio.gap", "audio.error":
            let code = event["code"] as? String ?? type
            let messages = ["microphone_denied": "麦克风权限未开启。请打开系统设置，允许真实交互使用麦克风后重试。",
                            "device_changed": "音频设备已切换，收听已停止。请确认系统输入设备后重新开始。",
                            "outbox_seal_failed": "录音已停止，但收尾记录写入失败。请检查磁盘空间和会议目录权限；本机文件已保留。",
                            "capture_failed": "音源启动失败。请检查麦克风是否可用、系统输入设备及录音权限后重试。"]
            captureProblem = messages[code] ?? "收听未完成（\(code)），请检查音源和系统权限后重试。"
            if code == "outbox_seal_failed" {
                // The inputs have already stopped. Sending Stop here would
                // reset the helper's bounded seal attempts after a watchdog
                // error; only the user's explicit save retry may do that.
                healthTask?.cancel(); stopWatchdog?.cancel()
                captureFinish = "failed"; captureFinishMeetingId = meeting
                captureState = "error"; status = captureProblem! + " 修复后点击“重试保存”。"
            } else if activeMeeting != nil { stopWithStatus("收听出现问题，正在停止…") }
            else { captureState = "error"; status = captureProblem! }
            publishNativeState()
        default: break
        }
    }

    private var outboxRoot: URL { stateDirectory.appendingPathComponent("capture-outbox", isDirectory: true) }

    private func pumpInterims() {
        guard !postingInterims, captureState == "listening" else { return }
        postingInterims = true
        Task {
            defer { postingInterims = false }
            while captureState == "listening", let key = pendingInterims.keys.first,
                  let (meeting, data) = pendingInterims.removeValue(forKey: key), meeting == activeMeeting {
                // A final may already have reached the backend. Superseded or
                // unavailable interim text never blocks durable delivery.
                try? await postAudio(meeting, data)
            }
        }
    }

    private func drainOutbox(_ manifest: CaptureEventOutbox.Manifest, directory: URL) async throws -> String? {
        for record in try CaptureEventOutbox.pending(in: directory) {
            let data = try Data(contentsOf: record)
            try await postAudio(manifest.meetingID, data)
            try FileManager.default.removeItem(at: record)
        }
        guard FileManager.default.fileExists(atPath: directory.appendingPathComponent("seal.json").path) else { return nil }
        let seal = try CaptureEventOutbox.Seal.read(from: directory)
        guard try CaptureEventOutbox.pending(in: directory).isEmpty else { return nil }
        let body = try JSONSerialization.data(withJSONObject: ["capture_epoch": manifest.epoch,
            "saved": seal.saved, "no_audio": seal.saved && !seal.hadAudio])
        let data = try await request("/live/v2/meetings/\(manifest.meetingID)/capture/finish", body: body, speech: true)
        let value = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        guard let result = value?["state"] as? String, ["complete", "with_gaps", "failed", "no_audio"].contains(result) else {
            throw DesktopError(message: "会议服务未确认收尾结果。")
        }
        if result != "failed" { try? FileManager.default.removeItem(at: directory) }
        return result == "no_audio" ? "complete" : result
    }

    private func processActiveOutbox(_ manifest: CaptureEventOutbox.Manifest, directory: URL) {
        outboxTask?.cancel()
        outboxTask = Task {
            var delayed = false
            while !Task.isCancelled, !closing, activeEpoch == manifest.epoch {
                do {
                    if let result = try await drainOutbox(manifest, directory: directory) {
                        guard activeEpoch == manifest.epoch else { return }
                        stopWatchdog?.cancel(); healthTask?.cancel(); revokeSpeech()
                        captureFinish = result; captureFinishMeetingId = manifest.meetingID
                        if result == "failed" {
                            captureState = "error"
                            status = "部分录音尚未确认保存。本机记录已保留；请检查磁盘和目录后，再点击“重试保存”。"
                            return
                        }
                        pendingInterims.removeAll(); activeOutbox = nil; activeEpoch = nil; activeMeeting = nil
                        captureState = result == "failed" ? "error" : "stopped"
                        switch result {
                        case "complete": status = captureProblem.map { "收听已停止 · " + $0 } ?? "录音和转写已保存"
                        case "with_gaps": status = "已保存可用录音。请查看各音源的录音和转写状态。"
                        default: status = "收尾保存失败。已保留本机记录，会议仍在进行中。"
                        }
                        return
                    }
                    if delayed, captureState == "listening" { status = "正在收听" }
                    delayed = false
                } catch {
                    if !delayed, captureState == "listening" { status = "录音继续，转写等待保存…" }
                    if captureState == "stopping" || captureState == "error" {
                        status = "录音已停止，收尾保存尚未确认。请检查磁盘空间、目录权限和本机服务；正在重试。"
                    }
                    delayed = true
                }
                do { try await Task.sleep(for: delayed ? .seconds(1) : .milliseconds(200)) } catch { return }
            }
        }
    }

    private func replaySavedCaptures() {
        Task {
            guard let directories = try? FileManager.default.contentsOfDirectory(at: outboxRoot, includingPropertiesForKeys: nil) else { return }
            for directory in directories {
                guard let manifest = try? CaptureEventOutbox.Manifest.read(from: directory), validMeetingID(manifest.meetingID),
                      UUID(uuidString: manifest.epoch) != nil, directory.lastPathComponent == manifest.epoch else { continue }
                // A sealed journal can be replayed after an app or backend
                // restart. An unsealed journal must never authorize End.
                _ = try? await drainOutbox(manifest, directory: directory)
            }
        }
    }

    private func failBridge(_ message: String) {
        guard !closing else { return }
        bridgeFailed = true; healthTask?.cancel(); stopWatchdog?.cancel()
        if helper?.isRunning == true {
            // EOF synchronously releases the capture inputs in AudioHelper.
            try? helperInput?.close(); helperInput = nil
            let process = helper
            Task { try? await Task.sleep(for: .seconds(6)); if process?.isRunning == true { process?.terminate() } }
        }
        revokeSpeech()
        pendingInterims.removeAll()
        if activeOutbox != nil {
            captureFinish = "failed"; captureFinishMeetingId = activeMeeting
            // The helper may have sealed the outbox before it exited; retain
            // the binding for the delivery task to finish or retry on restart.
        } else { activeMeeting = nil }
        captureState = "error"
        status = message; stopItem.isEnabled = false; startItem.isEnabled = false
        if !pageLoaded { showStartupFailure(message) }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window?.makeKeyAndOrderFront(nil)
        sender.activate(ignoringOtherApps: true)
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard activeMeeting != nil || beginningCapture else { return .terminateNow }
        guard !quitting else { return .terminateLater }
        quitting = true
        let meeting = activeMeeting
        stopWithStatus("正在保存录音和转写后退出…")
        Task {
            let deadline = Date().addingTimeInterval(10)
            while (activeMeeting != nil || beginningCapture) && Date() < deadline {
                try? await Task.sleep(for: .milliseconds(100))
            }
            if let meeting, activeMeeting == nil, activeInput == "live", captureFinishMeetingId == meeting,
               ["complete", "with_gaps"].contains(captureFinish ?? ""), (try? await meetingState(meeting)) == "active" {
                let data = try? JSONSerialization.data(withJSONObject: ["action": "end"])
                if let data { _ = try? await request("/live/v2/meetings/\(meeting)/controls", body: data) }
            }
            sender.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        closing = true; healthTask?.cancel(); stopWatchdog?.cancel()
        updateTask?.cancel(); updateReadinessTask?.cancel()
        outboxTask?.cancel(); speechLeaseTask?.cancel(); fileLease?.revoke()
        fileTask?.cancel()
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "workbench")
        for record in exports.values {
            record.download.cancel(nil)
            if let staging = record.temporaryDirectory { try? FileManager.default.removeItem(at: staging) }
        }
        exports.removeAll()
        try? helperInput?.close(); helperInput = nil
        if helper?.isRunning == true { helper?.terminate() }
        if backend?.isRunning == true { backend?.terminate() }
        session.invalidateAndCancel()
        ownerToken = ""; agentToken = ""; speechToken = ""
    }
}

@main
struct DesktopHostMain {
    @MainActor static func main() {
        signal(SIGPIPE, SIG_IGN)
        if CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--install-update" {
            exit(VeraUpdateInstallPlan.runHelper(planURL: URL(fileURLWithPath: CommandLine.arguments[2])))
        }
        let app = NSApplication.shared
        let delegate = DesktopController()
        app.setActivationPolicy(.regular)
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
