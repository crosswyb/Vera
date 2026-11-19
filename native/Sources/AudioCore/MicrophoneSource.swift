import AVFoundation

protocol CapturedAudioSource: AnyObject {
    var onAudio: ((AVAudioPCMBuffer) -> Void)? { get set }
    var onRateChange: (() -> Void)? { get set }
    var onFailure: ((String) -> Void)? { get set }
    func start() throws
    func stop()
}

/// Minimal default-input source. No device selection, voiceprints, or echo suppression.
final class MicrophoneSource: CapturedAudioSource {
    var onAudio: ((AVAudioPCMBuffer) -> Void)?
    var onRateChange: (() -> Void)?
    var onFailure: ((String) -> Void)?
    private let engine = AVAudioEngine()
    private var installed = false
    private var observer: NSObjectProtocol?

    func start() throws {
        stop()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw AudioCoreError.invalidConfig("No microphone input format.")
        }
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, _ in
            guard let self else { return }
            // The framework owns its buffer; copy before leaving this callback.
            guard let copy = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: buffer.frameLength) else {
                self.onFailure?("Cannot allocate microphone buffer."); return
            }
            copy.frameLength = buffer.frameLength
            let source = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
            let target = UnsafeMutableAudioBufferListPointer(copy.mutableAudioBufferList)
            for (from, to) in zip(source, target) {
                guard let src = from.mData, let dst = to.mData else { continue }
                memcpy(dst, src, min(Int(from.mDataByteSize), Int(to.mDataByteSize)))
            }
            self.onAudio?(copy)
        }
        installed = true
        observer = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange,
                                                          object: engine, queue: .main) { [weak self] _ in
            self?.onRateChange?()
        }
        do { try engine.start() } catch { stop(); throw error }
    }

    func stop() {
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        engine.stop()
        if installed { engine.inputNode.removeTap(onBus: 0) }
        installed = false
    }
    deinit { stop() }
}
