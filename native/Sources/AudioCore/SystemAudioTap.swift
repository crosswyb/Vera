// Adapted from Cicada SystemAudioTap.swift at 31574edbd43486eb5c4b958f82b503a63bb4697d.
// Copyright 2026 Cicada contributors. Licensed under Apache-2.0; see third_party/cicada.
import AVFoundation
import AudioToolbox
import CoreAudio
import CoreMedia

// MARK: - System audio tap

/// Captures the audio that is currently going to the default output device.
/// This uses Core Audio process taps, so it only needs the "System Audio
/// Recording" privacy grant and never asks for Screen Recording.
/// Silence does not establish permission denial. No private permission APIs are used.
final class SystemAudioTap: CapturedAudioSource {
    /// Diagnostic probes can select only their synthetic playback process.
    var includedProcessID: pid_t?
    /// Called on the real-time audio thread. Must not block.
    var onAudio: ((AVAudioPCMBuffer) -> Void)?
    /// Called on the main queue when the output device changes its sample rate. A Bluetooth headset
    /// drops to 16 or 24 kHz when a meeting opens its mic: the tap's format no longer matches and it
    /// hears nothing, so it must start again.
    var onRateChange: (() -> Void)?
    var onFailure: ((String) -> Void)?
    var onDiagnostic: ((String) -> Void)?
    private var rateListener: (device: AudioObjectID, block: AudioObjectPropertyListenerBlock)?
    private var outputListener: AudioObjectPropertyListenerBlock?
    private static let outputAddress = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    private static let rateAddress = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyNominalSampleRate,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )

    private var audioUnit: AudioUnit?
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var clientFormat: AVAudioFormat?
    private var monoFormat: AVAudioFormat?
    private var renderBuffer: AVAudioPCMBuffer?

    static func defaultOutputDevice() -> AudioObjectID? {
        var device = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &device)
        if status != noErr || device == kAudioObjectUnknown { return nil }
        return device
    }

    private static func sampleRate(_ device: AudioObjectID) -> Float64? {
        var rate: Float64 = 0
        var size = UInt32(MemoryLayout<Float64>.size)
        var addr = rateAddress
        let status = AudioObjectGetPropertyData(device, &addr, 0, nil, &size, &rate)
        return status == noErr && rate > 0 ? rate : nil
    }

    static func ownProcessObject() -> AudioObjectID? {
        processObject(for: getpid())
    }

    static func processObject(for processID: pid_t) -> AudioObjectID? {
        var pid = processID
        var obj = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr,
                                                UInt32(MemoryLayout<pid_t>.size), &pid, &size, &obj)
        return status == noErr && obj != kAudioObjectUnknown ? obj : nil
    }

    /// How many input channels a device has: a headset, AirPods or a virtual machine's sound
    /// device has a microphone on the same device as its speakers.
    static func inputChannels(_ device: AudioObjectID) -> Int {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamConfiguration,
            mScope: kAudioObjectPropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(device, &addr, 0, nil, &size) == noErr, size > 0 else { return 0 }
        let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { raw.deallocate() }
        guard AudioObjectGetPropertyData(device, &addr, 0, nil, &size, raw) == noErr else { return 0 }
        let list = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
        return list.reduce(0) { $0 + Int($1.mNumberChannels) }
    }

    private static func defaultOutputUID() -> String? {
        guard let device = defaultOutputDevice() else { return nil }
        var uid: CFString = "" as CFString
        var size = UInt32(MemoryLayout<CFString>.size)
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceUID,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let status = withUnsafeMutablePointer(to: &uid) { ptr in
            AudioObjectGetPropertyData(device, &addr, 0, nil, &size, ptr)
        }
        if status != noErr { return nil }
        return uid as String
    }

    private func fail(_ code: Int, _ message: String) -> any Error {
        stop()
        if code == Int(kAudioDevicePermissionsError) { return AudioCoreError.systemAudioDenied }
        return NSError(domain: "ZSJHAudio", code: code, userInfo: [NSLocalizedDescriptionKey: message])
    }

    func start() throws {
        stop()

        // Exclude this host only. This does not establish future AI playback-loop safety.
        let desc: CATapDescription
        if let includedProcessID {
            guard let process = Self.processObject(for: includedProcessID) else {
                throw fail(-4, "Synthetic playback process is not an active Core Audio process.")
            }
            desc = CATapDescription(stereoMixdownOfProcesses: [process])
        } else {
            desc = CATapDescription(stereoGlobalTapButExcludeProcesses: Self.ownProcessObject().map { [$0] } ?? [])
        }
        desc.name = "ZSJH AudioCore"
        desc.isPrivate = true

        var tap = AudioObjectID(kAudioObjectUnknown)
        var status = AudioHardwareCreateProcessTap(desc, &tap)
        guard status == noErr else { throw fail(Int(status), "Cannot create system audio tap (\(status)).") }
        tapID = tap

        guard let outputUID = Self.defaultOutputUID() else { throw fail(-1, "No default output device.") }

        let aggregateUID = "com.zsjh.audiocore.\(UUID().uuidString)"
        let aggregate: [String: Any] = [
            kAudioAggregateDeviceNameKey: "ZSJH Audio Tap",
            kAudioAggregateDeviceUIDKey: aggregateUID,
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
            kAudioAggregateDeviceTapListKey: [[
                kAudioSubTapUIDKey: desc.uuid.uuidString,
                kAudioSubTapDriftCompensationKey: true,
            ]],
        ]
        var aggregateID = AudioObjectID(kAudioObjectUnknown)
        status = AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &aggregateID)
        guard status == noErr else { throw fail(Int(status), "Cannot create aggregate audio device (\(status)).") }
        self.aggregateID = aggregateID

        var component = AudioComponentDescription(
            componentType: kAudioUnitType_Output,
            componentSubType: kAudioUnitSubType_HALOutput,
            componentManufacturer: kAudioUnitManufacturer_Apple,
            componentFlags: 0,
            componentFlagsMask: 0
        )
        guard let found = AudioComponentFindNext(nil, &component) else { throw fail(-2, "HAL audio component unavailable.") }
        var unit: AudioUnit?
        status = AudioComponentInstanceNew(found, &unit)
        guard status == noErr, let unit else { throw fail(Int(status), "Cannot create audio unit (\(status)).") }
        audioUnit = unit

        var disableOutput: UInt32 = 0
        status = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0,
                                      &disableOutput, UInt32(MemoryLayout<UInt32>.size))
        guard status == noErr else { throw fail(Int(status), "Cannot disable audio output (\(status)).") }
        var enableInput: UInt32 = 1
        status = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1,
                                      &enableInput, UInt32(MemoryLayout<UInt32>.size))
        guard status == noErr else { throw fail(Int(status), "Cannot enable tap input (\(status)).") }

        var device = aggregateID
        status = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
                                      &device, UInt32(MemoryLayout<AudioObjectID>.size))
        guard status == noErr else { throw fail(Int(status), "Cannot attach aggregate audio device (\(status)).") }

        // Two rates. The unit can't convert on its input side, so the client format takes the rate
        // the unit reports, or every render fails with -10863. But the samples come at the
        // aggregate's rate, which the output device sets: a headset in a call runs it at 16 kHz while
        // the unit still says 48, and read as 48 kHz those samples are a third of the speed and
        // nothing is recognized. AudioRouter resamples from the aggregate's rate.
        var hardware = AudioStreamBasicDescription()
        var hardwareSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        let read = AudioUnitGetProperty(unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 1,
                                        &hardware, &hardwareSize)
        let sampleRate = read == noErr && hardware.mSampleRate > 0 ? hardware.mSampleRate : 48_000
        let samplesRate = Self.sampleRate(aggregateID) ?? sampleRate
        onDiagnostic?("Audio unit \(Int(sampleRate)) Hz; captured samples \(Int(samplesRate)) Hz.")
        var asbd = AudioStreamBasicDescription(
            mSampleRate: sampleRate,
            mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked | kAudioFormatFlagIsNonInterleaved,
            mBytesPerPacket: 4,
            mFramesPerPacket: 1,
            mBytesPerFrame: 4,
            mChannelsPerFrame: 2,
            mBitsPerChannel: 32,
            mReserved: 0
        )
        guard let clientFormat = AVAudioFormat(streamDescription: &asbd) else { throw fail(-3, "Invalid capture format.") }
        self.clientFormat = clientFormat
        self.monoFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: samplesRate, channels: 1, interleaved: false)
        // Pre-allocate so the real-time callback normally doesn't allocate.
        self.renderBuffer = AVAudioPCMBuffer(pcmFormat: clientFormat, frameCapacity: 4096)

        status = AudioUnitSetProperty(unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Output, 1,
                                      &asbd, UInt32(MemoryLayout<AudioStreamBasicDescription>.size))
        guard status == noErr else { throw fail(Int(status), "Cannot configure audio format (\(status)).") }

        // The aggregate's inputs are the output device's own inputs first, then the tap. When the
        // output device has a microphone (a headset, AirPods, a virtual machine's sound device),
        // the first two channels are that microphone, not what the Mac plays: skip past them.
        let mics = Self.defaultOutputDevice().map(Self.inputChannels) ?? 0
        if mics > 0 {
            var map: [Int32] = [Int32(mics), Int32(mics + 1)]
            status = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_ChannelMap, kAudioUnitScope_Output, 1,
                                          &map, UInt32(MemoryLayout<Int32>.size * map.count))
            guard status == noErr else { throw fail(Int(status), "Cannot map system tap channels (\(status)).") }
        }

        var callback = AURenderCallbackStruct(
            inputProc: { refCon, _, timestamp, bus, frames, _ in
                let tap = Unmanaged<SystemAudioTap>.fromOpaque(refCon).takeUnretainedValue()
                return tap.render(timestamp: timestamp, bus: bus, frames: frames)
            },
            inputProcRefCon: Unmanaged.passUnretained(self).toOpaque()
        )
        status = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_SetInputCallback, kAudioUnitScope_Global, 0,
                                      &callback, UInt32(MemoryLayout<AURenderCallbackStruct>.size))
        guard status == noErr else { throw fail(Int(status), "Cannot configure audio callback (\(status)).") }

        status = AudioUnitInitialize(unit)
        guard status == noErr else { throw fail(Int(status), "Cannot initialize audio unit (\(status)).") }
        status = AudioOutputUnitStart(unit)
        guard status == noErr else { throw fail(Int(status), "Cannot start audio capture (\(status)).") }

        if let output = Self.defaultOutputDevice() {
            let was = Self.sampleRate(output) ?? 0
            let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
                guard let self, let now = Self.sampleRate(output), now != was else { return }
                self.onRateChange?()
            }
            var addr = Self.rateAddress
            if AudioObjectAddPropertyListenerBlock(output, &addr, .main, block) == noErr {
                rateListener = (output, block)
            }
        }
        let outputBlock: AudioObjectPropertyListenerBlock = { [weak self] _, _ in self?.onRateChange?() }
        var outputAddress = Self.outputAddress
        if AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &outputAddress, .main, outputBlock) == noErr {
            outputListener = outputBlock
        }
    }

    private func render(timestamp: UnsafePointer<AudioTimeStamp>, bus: UInt32, frames: UInt32) -> OSStatus {
        guard let unit = audioUnit, let clientFormat else { return noErr }
        var pcm = renderBuffer
        if pcm == nil || pcm!.frameCapacity < frames {
            pcm = AVAudioPCMBuffer(pcmFormat: clientFormat, frameCapacity: frames)
            renderBuffer = pcm
        }
        guard let pcm else { onFailure?("Cannot allocate system capture buffer."); return noErr }
        pcm.frameLength = frames
        var flags = AudioUnitRenderActionFlags()
        let status = AudioUnitRender(unit, &flags, timestamp, bus, frames, pcm.mutableAudioBufferList)
        guard status == noErr else {
            onFailure?("AudioUnitRender failed (\(status)).")
            return status
        }

        guard let monoFormat, let mono = AVAudioPCMBuffer(pcmFormat: monoFormat, frameCapacity: frames),
              let src = pcm.floatChannelData, let dst = mono.floatChannelData else {
            onFailure?("Cannot allocate mono system capture buffer.")
            return noErr
        }
        mono.frameLength = frames
        let n = Int(frames)
        if pcm.format.channelCount >= 2 {
            for i in 0..<n {
                dst[0][i] = (src[0][i] + src[1][i]) * 0.5
            }
        } else {
            memcpy(dst[0], src[0], n * MemoryLayout<Float>.size)
        }
        onAudio?(mono)
        return noErr
    }

    func stop() {
        if let outputListener {
            var address = Self.outputAddress
            AudioObjectRemovePropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &address, .main, outputListener)
        }
        outputListener = nil
        if let (device, block) = rateListener {
            var addr = Self.rateAddress
            AudioObjectRemovePropertyListenerBlock(device, &addr, .main, block)
        }
        rateListener = nil
        if let unit = audioUnit {
            AudioOutputUnitStop(unit)
            AudioUnitUninitialize(unit)
            AudioComponentInstanceDispose(unit)
        }
        audioUnit = nil
        if aggregateID != kAudioObjectUnknown {
            AudioHardwareDestroyAggregateDevice(aggregateID)
        }
        aggregateID = kAudioObjectUnknown
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
        }
        tapID = kAudioObjectUnknown
        clientFormat = nil
        monoFormat = nil
        renderBuffer = nil
    }

    deinit { stop() }
}
