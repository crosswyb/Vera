// Adapted from Cicada AudioFiles.swift at 31574edbd43486eb5c4b958f82b503a63bb4697d.
// Copyright 2026 Cicada contributors. Licensed under Apache-2.0; see third_party/cicada.
import AVFoundation

/// Confined to one stream consumer. No conversion work happens on the audio callback.
final class PCMConverter {
    private var converter: AVAudioConverter?

    func convert(_ input: AVAudioPCMBuffer, to format: AVAudioFormat, allowEmpty: Bool = false) -> AVAudioPCMBuffer? {
        if input.format == format { return input }
        if converter == nil || converter!.inputFormat != input.format || converter!.outputFormat != format {
            converter = AVAudioConverter(from: input.format, to: format)
        }
        guard let converter else { return nil }
        let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * format.sampleRate / input.format.sampleRate)) + 64
        guard let output = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else { return nil }
        var consumed = false
        var error: NSError?
        let status = converter.convert(to: output, error: &error) { _, state in
            if consumed { state.pointee = .noDataNow; return nil }
            consumed = true; state.pointee = .haveData
            return input
        }
        return status == .error || (!allowEmpty && output.frameLength == 0) ? nil : output
    }

    /// A streaming resampler can retain valid audio until it sees end-of-stream.
    func drain(_ receive: (AVAudioPCMBuffer) -> Void) throws {
        guard let converter else { return }
        self.converter = nil
        while true {
            guard let output = AVAudioPCMBuffer(pcmFormat: converter.outputFormat, frameCapacity: 4096) else {
                throw AudioCoreError.conversionFailed
            }
            var error: NSError?
            let status = converter.convert(to: output, error: &error) { _, state in
                state.pointee = .endOfStream
                return nil
            }
            guard status != .error else { throw AudioCoreError.conversionFailed }
            if output.frameLength > 0 { receive(output) }
            if status == .endOfStream || output.frameLength == 0 { return }
        }
    }
}
