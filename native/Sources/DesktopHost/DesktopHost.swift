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
    private var backendFailureKind = ""
    private var activeMeeting: String? { didSet { publishNativeState() } }
    private var lastMeeting: String?
    private var captureState = "idle" { didSet { publishNativeState() } }
    private var modelState = "unknown" { didSet { publishNativeState() } }
    private var fileModelState = "unknown" { didSet { publishNativeState() } }
    private var themePreference = UserDefaults.standard.string(forKey: "themePreference") ?? "system"
    private var beginningCapture = false { didSet { publishNativeState() } }
    private var captureGeneration = 0
    private var pageLoaded = false
    private var captureProblem: String?
    private var selectedInput = LiveAudioInput(rawValue: UserDefaults.standard.string(forKey: "liveAudioInput") ?? "system") ?? .system
    private var sourceLevels: [String: Float] = [:]
    private var sourceFrames: [String: UInt64] = [:]
    private var meetingFinished = false
    private var pending: [(String, Data)] = []
    private var posting = false
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
        failBridge(detail.isEmpty ? "本机服务已停止，收听已停止。" : "本机服务已停止（\(detail)），尚未收听。")
        startItem.isEnabled = false
    }

    private func loadWorkbench() {
        let encoded = String(data: try! JSONSerialization.data(withJSONObject: [ownerToken]), encoding: .utf8)!
        let theme = String(data: try! JSONSerialization.data(withJSONObject: [themePreference]), encoding: .utf8)!
        let script = "if(location.origin==='http://127.0.0.1:\(port)' && location.pathname==='/workbench'){sessionStorage.setItem('wb_access',\(encoded)[0]);window.__ZSJH_THEME_PREFERENCE__=\(theme)[0];}"
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
        hostReady && pageLoaded && modelState == "installed" && activeMeeting == nil
            && !beginningCapture && !posting && pending.isEmpty
    }

    private var canStop: Bool {
        activeMeeting != nil && captureState != "stopping" && captureState != "stopped" && !closing
    }

    private var canImport: Bool {
        hostReady && pageLoaded && fileModelState == "installed" && activeMeeting == nil
            && !beginningCapture && !posting && pending.isEmpty
    }

    private func applyThemePreference(_ value: String) {
        let preference = ["system", "light", "dark"].contains(value) ? value : "system"
        themePreference = preference
        UserDefaults.standard.set(preference, forKey: "themePreference")
        NSApp.appearance = preference == "system" ? nil : NSAppearance(named: preference == "dark" ? .darkAqua : .aqua)
    }

    private func publishNativeState() {
        startItem.isEnabled = canStart; stopItem.isEnabled = canStop
        modelItem.isEnabled = hostReady
        installItem.isEnabled = hostReady && activeMeeting == nil && !beginningCapture
            && ![modelState, fileModelState].contains("downloading")
            && [modelState, fileModelState].contains(where: { ["downloadable", "unknown"].contains($0) })
        guard let webView, pageLoaded, isWorkbench(webView.url), !closing else { return }
        let payload: [String: Any] = [
            "ready": hostReady, "captureState": captureState, "status": status,
            "themePreference": themePreference,
            "meetingId": activeMeeting.map { $0 as Any } ?? NSNull(), "modelState": modelState,
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
              body.count <= 5, Set(body.keys).isSubset(of: ["action", "meetingId", "fileId", "audioSource", "themePreference"]),
              let action = body["action"] as? String else { return }
        switch action {
        case "requestState": publishNativeState()
        case "setThemePreference":
            guard let preference = body["themePreference"] as? String, ["system", "light", "dark"].contains(preference) else { return }
            applyThemePreference(preference)
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
        case "importAudioFile":
            guard let id = body["meetingId"] as? String, validMeetingID(id),
                  let fileID = body["fileId"] as? String, fileID == selectedAudio?.id else { return }
            beginFileImport(meeting: id, fileID: fileID)
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
                self.failBridge("音频事件通道中断，收听已停止。")
            } })
    }

    private func sendHelper(_ command: [String: Any]) {
        guard helper?.isRunning == true, let input = helperInput,
              var data = try? JSONSerialization.data(withJSONObject: command) else { return }
        data.append(10)
        do { try input.write(contentsOf: data) }
        catch { failBridge("音频控制通道不可用，收听已停止。") }
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

    private func request(_ path: String, body: Data? = nil, method: String? = nil, owner: Bool = false) async throws -> Data {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port)\(path)")!)
        request.setValue("Bearer " + (owner ? ownerToken : agentToken), forHTTPHeaderField: "Authorization")
        if let body { request.httpMethod = "POST"; request.httpBody = body; request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        if let method { request.httpMethod = method }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw DesktopError(message: "会议服务响应无效。") }
        guard (200...299).contains(http.statusCode) else {
            if owner, let error = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let message = error["error"] as? String { throw DesktopError(message: message) }
            throw HTTPFailure(status: http.statusCode)
        }
        return data
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
        let type = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["type"] as? String ?? ""
        if meetingFinished, type != "transcript.final", type != "transcript.retracted", type != "audio.recording" { return }
        do { _ = try await request("/internal/live/v2/meetings/\(meeting)/audio-events", body: data) }
        catch let failure as HTTPFailure where failure.status == 409 {
            let state = try await meetingState(meeting)
            guard state == "ended" || state == "expired" else { throw failure }
            meetingFinished = true
            stopWithStatus("会议已结束，正在停止收听…")
            // Ended meetings accept only their existing segments' brief tail;
            // an expected rejection does not mean that the backend is offline.
        }
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

    private func beginFileImport(meeting: String, fileID: String) {
        guard canImport, let selection = selectedAudio, selection.id == fileID else {
            selectedAudioError = "请先安装中文模型，或等待当前任务结束。"; publishNativeState(); return
        }
        beginningCapture = true
        fileTask = Task { [self] in
            var registered = false
            let epoch = UUID().uuidString
            do {
                guard try await selectedMeeting() == meeting else { throw DesktopError(message: "会议已切换，请重新导入。") }
                try await validateCaptureMeeting(meeting, inputKind: "audio_file")
                guard hostReady, try await selectedMeeting() == meeting, activeMeeting == nil else {
                    throw DesktopError(message: "应用状态已变化，请重新导入。")
                }
                let info = try await AudioFileInfo.inspect(selection.url)
                let body = try JSONSerialization.data(withJSONObject: ["import_id": epoch, "file_name": info.name,
                    "duration_ms": info.durationMS, "bytes": info.bytes])
                let registeredData = try await request("/internal/live/v2/meetings/\(meeting)/audio-import", body: body)
                registered = true
                guard let registeredMeeting = try JSONSerialization.jsonObject(with: registeredData) as? [String: Any],
                      let job = registeredMeeting["audio_import"] as? [String: Any],
                      let relative = job["original_path"] as? String else { throw DesktopError(message: "音频归档位置不可用。") }
                let destination = try meetingFileURL(relative, meeting: registeredMeeting)
                activeInput = "audio_file"; captureProblem = nil; meetingFinished = false
                activeMeeting = meeting; lastMeeting = meeting; captureState = "importing"
                beginningCapture = false; status = "正在保存原始音频副本…"
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
                try Task.checkCancellation()
                status = "原始音频已保存，正在转写…"
                let worker = FileTranscription(); fileTranscriber = worker
                worker.onEvent = { [weak self] event in
                    guard let self, !self.closing else { throw CancellationError() }
                    let data = try JSONEncoder().encode(event)
                    try await self.postAudio(meeting, data)
                }
                try await worker.run(url: destination, meetingID: meeting, epoch: epoch)
                try Task.checkCancellation()
                captureState = "stopping"; status = "正在保存完整转写…"
                let finish = try JSONSerialization.data(withJSONObject: ["import_id": epoch, "state": "completed"])
                _ = try await request("/internal/live/v2/meetings/\(meeting)/audio-import/finish", body: finish)
                status = "文件转写已完成，查证可继续进行"
                captureState = "stopped"
            } catch {
                let cancelled = error is CancellationError || Task.isCancelled
                if registered, !closing {
                    // Detached cancellation state must not prevent saving the
                    // partial transcript through the local backend.
                    let finish = try? JSONSerialization.data(withJSONObject: ["import_id": epoch,
                        "state": cancelled ? "cancelled" : "failed",
                        "error": cancelled ? "导入已停止，已有转写保留。" : "音频识别或保存失败，已有转写保留。"])
                    let save = Task { [self] in
                        if let finish { _ = try? await request("/internal/live/v2/meetings/\(meeting)/audio-import/finish", body: finish) }
                    }
                    await save.value
                }
                captureState = cancelled ? "stopped" : "error"
                status = cancelled ? "导入已停止，已有转写保留" : "导入失败：" + error.localizedDescription
            }
            fileTranscriber = nil; fileTask = nil; beginningCapture = false
            activeMeeting = nil; selectedAudio = nil
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
            var registeredEpoch: String?
            var registeredMeeting: String?
            do {
                let meeting = try await selectedMeeting()
                guard expectedMeeting == nil || expectedMeeting == meeting else { throw DesktopError(message: "会议已切换，请重新开始收听。") }
                try await validateCaptureMeeting(meeting)
                let selected = try await selectedMeeting()
                guard hostReady, modelState == "installed", selected == meeting else { throw DesktopError(message: "会议或音频连接已变化，请重新开始收听。") }
                captureGeneration += 1
                let generation = captureGeneration
                selectInput(requestedInput)
                sourceLevels = [:]; sourceFrames = [:]
                captureState = "starting"; captureProblem = nil; meetingFinished = false
                status = "正在连接音频…"
                let epoch = UUID().uuidString
                let body = try JSONSerialization.data(withJSONObject: ["recording_id": epoch, "sources": requestedInput.sources.map(\.rawValue)])
                let registration = try await request("/internal/live/v2/meetings/\(meeting)/recordings", body: body)
                registeredEpoch = epoch; registeredMeeting = meeting
                let snapshot = try await meetingSnapshot(meeting)
                guard let registered = try JSONSerialization.jsonObject(with: registration) as? [String: Any],
                      let directory = registered["recording_directory"] as? String,
                      let savedMeeting = snapshot["meeting"] as? [String: Any],
                      let relative = savedMeeting["directory"] as? String,
                      directory == (try meetingFileURL(relative, meeting: savedMeeting)).path else {
                    throw DesktopError(message: "录音保存位置不可用，未开始收听。")
                }
                guard hostReady, try await selectedMeeting() == meeting, activeMeeting == nil else {
                    throw DesktopError(message: "会议已变化，未开始收听。")
                }
                activeInput = "live"; activeMeeting = meeting; lastMeeting = meeting
                sendHelper(["command": "start", "meeting_id": meeting, "sources": requestedInput.sources.map(\.rawValue), "locale": "zh-CN",
                            "capture_epoch": epoch, "recording_directory": directory])
                healthTask = Task { [weak self] in
                    while !Task.isCancelled {
                        try? await Task.sleep(for: .seconds(1))
                        guard let self, !Task.isCancelled, self.activeMeeting == meeting,
                              self.captureGeneration == generation else { return }
                        do {
                            let selected = try await self.selectedMeeting()
                            let state = try await self.meetingState(meeting)
                            guard !Task.isCancelled, self.activeMeeting == meeting,
                                  self.captureGeneration == generation else { return }
                            self.meetingFinished = state == "ended" || state == "expired"
                            guard selected == meeting, state == "active" else {
                                self.stopWithStatus("会议切换或结束，正在停止收听…"); return
                            }
                        } catch {
                            guard !Task.isCancelled, self.activeMeeting == meeting,
                                  self.captureGeneration == generation else { return }
                            self.stopWithStatus("会议连接中断，正在停止收听…"); return
                        }
                    }
                }
            } catch {
                if let epoch = registeredEpoch, let meeting = registeredMeeting {
                    for source in requestedInput.sources {
                        var event = AudioEvent(meetingID: meeting, captureEpoch: epoch,
                            streamID: "archive-error-\(source.rawValue)-\(epoch)", seq: 1, type: "audio.recording", source: source)
                        event.state = "missing"
                        if let data = try? JSONEncoder().encode(event) { try? await postAudio(meeting, data) }
                    }
                }
                activeMeeting = nil; captureState = "error"; status = error.localizedDescription
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
        healthTask?.cancel(); captureState = "stopping"; stopItem.isEnabled = false
        status = message
        sendHelper(["command": "stop"])
        stopWatchdog?.cancel()
        stopWatchdog = Task { [weak self] in
            try? await Task.sleep(for: .seconds(7))
            guard let self, !Task.isCancelled, self.activeMeeting != nil else { return }
            self.helper?.terminate(); self.failBridge("音频组件停止超时，已终止采音；尾部可能缺失。")
            self.startItem.isEnabled = false
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
        guard meeting == (activeMeeting ?? lastMeeting) else { failBridge("音频会议绑定不一致，收听已停止。"); return }
        guard !bridgeFailed else { return }
        guard pending.count < 256 else { failBridge("音频事件积压，收听已停止；未确认的事件可能缺失。"); return }
        pending.append((meeting, data)); pumpEvents()
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
            stopWatchdog?.cancel(); healthTask?.cancel(); captureState = "stopped"
        case "audio.gap", "audio.error":
            let code = event["code"] as? String ?? type
            let messages = ["microphone_denied": "麦克风权限未开启。请打开系统设置，允许真实交互使用麦克风后重试。",
                            "device_changed": "音频设备已切换，收听已停止。请确认系统输入设备后重新开始。",
                            "capture_failed": "音源启动失败。请检查麦克风是否可用、系统输入设备及录音权限后重试。"]
            captureProblem = messages[code] ?? "收听未完成（\(code)），请检查音源和系统权限后重试。"
            if activeMeeting != nil { stopWithStatus("收听出现问题，正在停止…") }
            else { captureState = "error"; status = captureProblem! }
            publishNativeState()
        default: break
        }
    }

    private func pumpEvents() {
        guard !posting, !pending.isEmpty, !bridgeFailed else { return }
        posting = true
        Task {
            while !pending.isEmpty, !bridgeFailed, !closing {
                let (meeting, data) = pending[0]
                var accepted = false
                for attempt in 0..<3 {
                    do { try await postAudio(meeting, data); accepted = true; break }
                    catch {
                        if attempt < 2 { try? await Task.sleep(for: .milliseconds(100 * (attempt + 1))) }
                    }
                }
                guard accepted else { failBridge("转写未获服务确认，已停止收听；未确认事件可能缺失。"); break }
                if !pending.isEmpty { pending.removeFirst() }
            }
            posting = false
            if captureState == "stopped", pending.isEmpty, !bridgeFailed {
                activeMeeting = nil
                status = captureProblem.map { "已停止 · " + $0 } ?? "已停止"
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
        pending.removeAll(); activeMeeting = nil; captureState = "error"
        status = message; stopItem.isEnabled = false; startItem.isEnabled = false
        if !pageLoaded { showStartupFailure(message) }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard activeMeeting != nil || posting else { return .terminateNow }
        guard !quitting else { return .terminateLater }
        quitting = true
        let meeting = activeMeeting
        stopWithStatus("正在保存录音和转写后退出…")
        Task {
            let deadline = Date().addingTimeInterval(10)
            while (activeMeeting != nil || posting) && Date() < deadline {
                try? await Task.sleep(for: .milliseconds(100))
            }
            if let meeting, activeMeeting == nil, !posting, !bridgeFailed,
               (try? await meetingState(meeting)) == "active" {
                let data = try? JSONSerialization.data(withJSONObject: ["action": "end"])
                if let data { _ = try? await request("/live/v2/meetings/\(meeting)/controls", body: data) }
            }
            sender.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        closing = true; healthTask?.cancel(); stopWatchdog?.cancel()
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
        ownerToken = ""; agentToken = ""
    }
}

@main
struct DesktopHostMain {
    @MainActor static func main() {
        signal(SIGPIPE, SIG_IGN)
        let app = NSApplication.shared
        let delegate = DesktopController()
        app.setActivationPolicy(.regular)
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
