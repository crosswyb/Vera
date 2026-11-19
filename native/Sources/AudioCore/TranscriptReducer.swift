import Foundation

public struct SpeechPiece: Sendable {
    public let text: String
    public let isFinal: Bool
    public let startMS: Int
    public let endMS: Int
    public init(text: String, isFinal: Bool, startMS: Int, endMS: Int) {
        self.text = text; self.isFinal = isFinal
        self.startMS = startMS; self.endMS = endMS
    }
}

public struct TranscriptChange: Equatable, Sendable {
    public let type: String
    public let segmentID: String
    public let revision: Int
    public let supersedesRevision: Int?
    public let text: String
    public let startMS: Int
    public let endMS: Int
}

public enum TranscriptReducerError: Error, LocalizedError {
    case invalidRange, outsideCorrectionWindow
    public var errorDescription: String? {
        switch self {
        case .invalidRange: return "Invalid speech result time range."
        case .outsideCorrectionWindow: return "Speech result predates the retained correction window; capture must restart to avoid ambiguous revisions."
        }
    }
}

/// SpeechAnalyzer supplies time ranges, not segment IDs. Matching range starts keep
/// an ID stable; overlapping replacement ranges explicitly retract displaced text.
/// Only a bounded recent correction window is retained, not an entire meeting.
public struct TranscriptReducer {
    private struct Segment {
        var id: String
        var revision: Int
        var text: String
        var start: Int
        var end: Int
        var final: Bool
    }
    private var segments: [Segment] = []
    private var nextID = 0
    private var trimmedThroughMS = -1
    private let retainedSegments: Int

    public init(retainedSegments: Int = 256) {
        self.retainedSegments = max(2, retainedSegments)
    }

    public mutating func apply(_ piece: SpeechPiece) throws -> [TranscriptChange] {
        guard piece.startMS >= 0, piece.endMS >= piece.startMS else { throw TranscriptReducerError.invalidRange }
        guard piece.startMS >= trimmedThroughMS else { throw TranscriptReducerError.outsideCorrectionWindow }
        let text = piece.text.trimmingCharacters(in: .whitespacesAndNewlines)
        let matching = segments.firstIndex { abs($0.start - piece.startMS) <= 2 }
        let matchingID = matching.map { segments[$0].id }
        if let matching, segments[matching].final && !piece.isFinal && !text.isEmpty { return [] }
        let displaced = segments.filter {
            $0.id != matchingID && $0.start < piece.endMS && piece.startMS < $0.end
        }
        var changes = displaced.map { retraction($0) }
        let displacedIDs = Set(displaced.map(\.id))
        segments.removeAll { displacedIDs.contains($0.id) }

        if let id = matchingID, let index = segments.firstIndex(where: { $0.id == id }) {
            let prior = segments[index]
            if text.isEmpty {
                changes.append(retraction(prior))
                segments.remove(at: index)
                return changes
            }
            if prior.text == text && prior.final == piece.isFinal && prior.end == piece.endMS {
                return changes
            }
            var updated = prior
            updated.revision += 1; updated.text = text
            updated.end = piece.endMS; updated.final = piece.isFinal
            segments[index] = updated
            changes.append(change(updated, supersedes: prior.revision))
        } else if !text.isEmpty {
            nextID += 1
            let segment = Segment(id: "s\(nextID)", revision: 1, text: text,
                                  start: piece.startMS, end: piece.endMS, final: piece.isFinal)
            segments.append(segment)
            changes.append(change(segment, supersedes: nil))
        }
        if segments.count > retainedSegments {
            let count = segments.count - retainedSegments
            trimmedThroughMS = max(trimmedThroughMS, segments.prefix(count).map(\.end).max() ?? -1)
            segments.removeFirst(count)
        }
        return changes
    }

    public mutating func retractOpen() -> [TranscriptChange] {
        let changes = segments.filter { !$0.final }.map { retraction($0) }
        segments.removeAll { !$0.final }
        return changes
    }

    private func change(_ segment: Segment, supersedes: Int?) -> TranscriptChange {
        .init(type: segment.final ? "transcript.final" : "transcript.interim", segmentID: segment.id,
              revision: segment.revision, supersedesRevision: supersedes, text: segment.text,
              startMS: segment.start, endMS: segment.end)
    }
    private func retraction(_ segment: Segment) -> TranscriptChange {
        .init(type: "transcript.retracted", segmentID: segment.id, revision: segment.revision + 1,
              supersedesRevision: segment.revision, text: "", startMS: segment.start, endMS: segment.end)
    }
}
