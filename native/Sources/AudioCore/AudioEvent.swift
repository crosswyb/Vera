import Foundation

public enum AudioSourceKind: String, Codable, CaseIterable, Sendable {
    case system
    case microphone
    case file
}

/// Live input choices deliberately exclude file import and unknown values.
public enum LiveAudioInput: String, CaseIterable, Sendable {
    case system, microphone, both

    public var sources: [AudioSourceKind] {
        switch self {
        case .system: return [.system]
        case .microphone: return [.microphone]
        case .both: return [.system, .microphone]
        }
    }
}

/// Wire format v1. Audio content never appears in this event stream.
public struct AudioEvent: Codable, Sendable {
    public let protocolVersion: Int
    public let meetingID: String
    public let captureEpoch: String
    public let streamID: String
    public let seq: Int
    public let type: String
    public let source: AudioSourceKind
    public let occurredAt: String
    public var segmentID: String?
    public var revision: Int?
    public var supersedesRevision: Int?
    public var audioStartMS: Int?
    public var audioEndMS: Int?
    public var text: String?
    public var state: String?
    public var code: String?
    public var detail: String?
    public var rms: Float?
    public var framesReceived: UInt64?
    public var nonSilentFrames: UInt64?
    public var sampleRate: Double?

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version", meetingID = "meeting_id"
        case captureEpoch = "capture_epoch", streamID = "stream_id"
        case segmentID = "segment_id", supersedesRevision = "supersedes_revision"
        case audioStartMS = "audio_start_ms", audioEndMS = "audio_end_ms"
        case occurredAt = "occurred_at", framesReceived = "frames_received"
        case nonSilentFrames = "non_silent_frames", sampleRate = "sample_rate"
        case seq, type, source, revision, text, state, code, detail, rms
    }

    public init(meetingID: String, captureEpoch: String, streamID: String,
                seq: Int, type: String, source: AudioSourceKind, now: Date = Date()) {
        self.protocolVersion = 1
        self.meetingID = meetingID
        self.captureEpoch = captureEpoch
        self.streamID = streamID
        self.seq = seq
        self.type = type
        self.source = source
        self.occurredAt = ISO8601DateFormatter().string(from: now)
    }
}

public struct AudioConfig: Equatable, Sendable {
    public let meetingID: String
    public let sources: [AudioSourceKind]
    public let locale: String
    public let transcribe: Bool
    public let includedProcessID: Int32?
    public let recordingDirectory: URL?
    public let captureEpoch: String?
    public init(meetingID: String, sources: [AudioSourceKind] = [.system], locale: String = "zh-CN",
                transcribe: Bool = true, includedProcessID: Int32? = nil,
                recordingDirectory: URL? = nil, captureEpoch: String? = nil) {
        self.meetingID = meetingID
        self.sources = sources
        self.locale = locale
        self.transcribe = transcribe
        self.includedProcessID = includedProcessID
        self.recordingDirectory = recordingDirectory
        self.captureEpoch = captureEpoch
    }
}

public enum AudioCoreError: Error, LocalizedError {
    case invalidConfig(String), alreadyRunning, modelUnavailable(String), microphoneDenied
    case unsupportedMachine, conversionFailed, inputOverflow, outputOverflow

    public var errorDescription: String? {
        switch self {
        case .invalidConfig(let reason): return reason
        case .alreadyRunning: return "Stop the current meeting before starting another configuration."
        case .modelUnavailable(let state): return "Local speech model is \(state). Use model_status; model installation is explicit."
        case .microphoneDenied: return "Allow microphone access for ZSJH Audio Helper in System Settings."
        case .unsupportedMachine: return "AudioCore requires Apple Silicon and macOS 26 or later."
        case .conversionFailed: return "Could not convert captured audio to the local speech format."
        case .inputOverflow: return "Audio input buffer exhausted; capture stopped instead of silently dropping audio."
        case .outputOverflow: return "Speech result buffer exhausted; capture stopped instead of losing transcript revisions."
        }
    }
}
