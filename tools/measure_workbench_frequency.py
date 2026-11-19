"""Summarize local meeting events without exporting text, queries, or titles."""

import argparse
import json
import math
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path


def percentiles(values):
    ordered = sorted(values)
    return {"n": len(ordered), **{f"p{p}_seconds": round(ordered[math.ceil(len(ordered) * p / 100) - 1], 2)
                                  if ordered else None for p in (50, 90)}}


def measure(db, mid):
    meeting = db.execute("""SELECT data->>'$.input_kind', data->>'$.mode', data->>'$.created_at', data->>'$.ended_at'
                            FROM records WHERE kind='wb_meeting' AND id=?""", (mid,)).fetchone()
    if not meeting:
        raise ValueError(f"No such meeting: {mid}")
    kind, mode, created, ended = meeting
    seconds = ended - created if ended and ended > created else None
    questions = {qid: (version, json.loads(refs)) for qid, version, refs in db.execute("""SELECT id,
        data->>'$.version', data->'$.source_refs' FROM records WHERE kind='wb_question'
        AND data->>'$.meeting_id'=? AND data->>'$.origin'='coordinator'""", (mid,))}
    # A legacy question's current basis may have changed after its creation.
    changed = {row[0] for row in db.execute("""SELECT DISTINCT data->>'$.payload.question_id' FROM events
        WHERE meeting=? AND data->>'$.type' IN ('question.mentioned','question.revised')""", (mid,))}
    final_times, created_at, started_at = {}, {}, {}
    detections, queues, runs = [], [], []
    queue_measured, failures = set(), set()
    counts, failure_codes = Counter(), Counter()
    rows = db.execute("""SELECT data->>'$.type',data->>'$.received_at',data->>'$.payload.question_id',
        data->>'$.payload.capture_epoch',data->>'$.payload.stream_id',data->>'$.payload.segment_id',
        data->>'$.payload.revision',data->'$.payload.source_refs',
        coalesce(data->>'$.payload.error_details.code',data->>'$.payload.code','unclassified')
        FROM events WHERE meeting=? AND data->>'$.type' IN ('transcript.final','coordinator.processed',
        'coordinator.failed','question.created','question.mentioned','research.started','research.completed',
        'research.failed') ORDER BY seq""", (mid,))
    for event, recv, qid, epoch, stream, segment, revision, raw_refs, code in rows:
        if qid and qid not in questions:
            continue
        counts[event] += 1
        if event == 'transcript.final':
            final_times[(f'{epoch}:{stream}:{segment}', revision)] = recv
        elif event == 'question.created':
            created_at[qid] = recv
            version, current_refs = questions[qid]
            refs = json.loads(raw_refs) if raw_refs is not None else current_refs if version == 1 and qid not in changed else []
            times = [final_times.get((r['segment_id'], r['revision'])) for r in refs]
            if times and all(t is not None and t <= recv for t in times):
                detections.append(recv - max(times))
        elif event == 'research.started':
            started_at[qid] = recv
            if qid in created_at and qid not in queue_measured and recv >= created_at[qid]:
                queues.append(recv - created_at[qid])
                queue_measured.add(qid)
        elif event in ('research.completed', 'research.failed'):
            start = started_at.pop(qid, None)
            if start is not None and recv >= start:
                runs.append(recv - start)
            if event == 'research.failed':
                failures.add(qid)
                failure_codes[code] += 1
    return {"meeting_id": mid, "input_kind": kind or "live", "mode": mode,
            "meeting_wall_minutes": round(seconds / 60, 2) if seconds else None,
            "automatic_questions": len(created_at),
            "questions_per_wall_hour": round(len(created_at) * 3600 / seconds, 1) if seconds else None,
            "final_to_creation": percentiles(detections), "creation_to_first_start": percentiles(queues),
            "start_to_completed_or_failed_attempt": percentiles(runs), "events": dict(counts),
            "failed_questions": len(failures), "failure_codes": dict(failure_codes)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('meeting_ids', nargs='+')
    args = parser.parse_args()
    with closing(sqlite3.connect(args.database.expanduser().resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('BEGIN')  # All queries see the same snapshot, including on a live database.
        print(json.dumps([measure(db, mid) for mid in args.meeting_ids], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
