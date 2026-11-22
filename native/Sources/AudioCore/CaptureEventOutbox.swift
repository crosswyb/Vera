import Foundation

/// A capture's reliable events stay on disk until the desktop receives a
/// durable backend acknowledgement. The directory contains no audio or keys.
public enum CaptureEventOutbox {
    public struct Manifest: Codable, Sendable {
        public let meetingID: String
        public let epoch: String
        public let sources: [AudioSourceKind]

        public init(meetingID: String, epoch: String, sources: [AudioSourceKind]) {
            self.meetingID = meetingID
            self.epoch = epoch
            self.sources = sources
        }

        public func write(to directory: URL) throws {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                     attributes: [.posixPermissions: 0o700])
            try JSONEncoder().encode(self).write(to: directory.appendingPathComponent("capture.json"), options: .atomic)
        }

        public static func read(from directory: URL) throws -> Self {
            try JSONDecoder().decode(Self.self, from: Data(contentsOf: directory.appendingPathComponent("capture.json")))
        }
    }

    public struct Seal: Codable, Sendable {
        public let saved: Bool
        public let hadAudio: Bool

        public static func read(from directory: URL) throws -> Self {
            try JSONDecoder().decode(Self.self, from: Data(contentsOf: directory.appendingPathComponent("seal.json")))
        }
    }

    public static func pending(in directory: URL) throws -> [URL] {
        try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            .filter { $0.lastPathComponent.hasSuffix(".event.json") }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    public static func replaceable(_ type: String) -> Bool {
        type == "transcript.interim" || type == "audio.level"
    }

    public final class Writer: @unchecked Sendable {
        private let directory: URL
        private let manifest: Manifest
        private let queue = DispatchQueue(label: "com.zsjh.audio.outbox", qos: .utility)
        private let lock = NSLock()
        private let eventLimit: Int
        private let queueLimit: Int
        private let onBacklog: @Sendable () -> Void
        private var queuedFinals = 0
        private var queuedControls = 0
        private var nextRecord = 0
        private var dropped: [AudioSourceKind: ClosedRange<Int>] = [:]
        private var backlogReported = false
        private var saved = true
        private var hadAudio = false
        private var sealing = false

        public init(directory: URL, manifest: Manifest, eventLimit: Int = 768, queueLimit: Int = 128,
                    onBacklog: @escaping @Sendable () -> Void = {}) {
            self.directory = directory
            self.manifest = manifest
            self.eventLimit = eventLimit
            self.queueLimit = queueLimit
            self.onBacklog = onBacklog
        }

        public func submit(_ event: AudioEvent) {
            guard event.meetingID == manifest.meetingID, event.captureEpoch == manifest.epoch else { return }
            lock.lock()
            if (event.framesReceived ?? 0) > 0 || event.type == "transcript.final" { hadAudio = true }
            if sealing || CaptureEventOutbox.replaceable(event.type) { lock.unlock(); return }
            let final = event.type == "transcript.final"
            // Reserve independent space for close/recording/error events even
            // when a recognizer returns a burst of final text.
            if final && queuedFinals >= queueLimit {
                let notify = markDropped(event)
                lock.unlock()
                if notify { onBacklog() }
                return
            }
            if !final && queuedControls >= 128 {
                saved = false
                lock.unlock()
                return
            }
            if final { queuedFinals += 1 } else { queuedControls += 1 }
            queue.async { [self] in
                defer {
                    lock.lock()
                    if final { queuedFinals -= 1 } else { queuedControls -= 1 }
                    lock.unlock()
                }
                do {
                    let limit = eventLimit + (final ? 0 : 128)
                    if (try CaptureEventOutbox.pending(in: directory)).count >= limit {
                        recordDeliveryFailure(event, final: final)
                    } else {
                        try record(event)
                    }
                } catch {
                    recordDeliveryFailure(event, final: final)
                }
            }
            lock.unlock()
        }

        /// Call after the audio core has closed the sources. Atomic publication
        /// of the seal follows every queued event and any collapsed loss range.
        public func seal() {
            lock.lock()
            guard !sealing else { lock.unlock(); return }
            sealing = true
            queue.async { [self] in
                lock.lock()
                let losses = dropped
                var complete = saved
                let recordedAudio = hadAudio
                lock.unlock()
                for (source, range) in losses {
                    var gap = AudioEvent(meetingID: manifest.meetingID, captureEpoch: manifest.epoch,
                                         streamID: "outbox-\(source.rawValue)-\(manifest.epoch)",
                                         seq: 1, type: "asr.gap", source: source)
                    gap.code = "delivery_backlog"
                    gap.audioStartMS = range.lowerBound
                    gap.audioEndMS = range.upperBound
                    do { try record(gap) } catch { complete = false }
                }
                let seal = Seal(saved: complete, hadAudio: recordedAudio)
                try? JSONEncoder().encode(seal).write(to: directory.appendingPathComponent("seal.json"), options: .atomic)
            }
            lock.unlock()
        }

        public func flush() async {
            await withCheckedContinuation { continuation in queue.async { continuation.resume() } }
        }

        /// Called under lock. Coalesce loss intervals by source so a slow or
        /// disconnected backend cannot cause growing memory or disk usage.
        private func markDropped(_ event: AudioEvent) -> Bool {
            if let start = event.audioStartMS, let end = event.audioEndMS, start <= end {
                let old = dropped[event.source]
                dropped[event.source] = min(old?.lowerBound ?? start, start)...max(old?.upperBound ?? end, end)
            } else {
                saved = false
            }
            guard !backlogReported else { return false }
            backlogReported = true
            return true
        }

        private func recordDeliveryFailure(_ event: AudioEvent, final: Bool) {
            lock.lock()
            let notify = final && markDropped(event)
            if !final { saved = false }
            lock.unlock()
            if notify { onBacklog() }
        }

        private func record(_ event: AudioEvent) throws {
            nextRecord += 1
            let name = String(format: "%012d.event.json", nextRecord)
            try JSONEncoder().encode(event).write(to: directory.appendingPathComponent(name), options: .atomic)
        }
    }
}
