"""Prepare a separate, paused database for an older detection runtime.

The upgraded database is never modified. Old binaries do not understand gap
tasks, so they must not be pointed directly at it. Unresolved meetings in the
export require an explicit review/retry before the old scheduler can run.
"""

import argparse
import json
import sqlite3
from pathlib import Path


def prepare_rollback(source, destination):
    source = Path(source).resolve(strict=True)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('回退副本已存在，请使用新的文件名')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the output privately and exclusively before SQLite opens it.
    with destination.open('xb'):
        destination.chmod(0o600)
    original = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    copy = sqlite3.connect(destination)
    try:
        original.backup(copy)
        with copy:
            meetings = copy.execute("SELECT id,data FROM records WHERE kind='wb_meeting'").fetchall()
            guarded = []
            for mid, data in meetings:
                meeting = json.loads(data)
                tasks = [json.loads(row[0]) for row in copy.execute(
                    "SELECT data FROM records WHERE kind='wb_detection_task' AND json_extract(data,'$.meeting_id')=?",
                    (mid,))]
                unresolved = [task for task in tasks if task['state'] not in {'completed', 'superseded'}]
                if not unresolved:
                    continue
                segments = {json.loads(row[0])['id']: json.loads(row[0]) for row in copy.execute(
                    "SELECT data FROM records WHERE kind='wb_segment' AND json_extract(data,'$.meeting_id')=?",
                    (mid,))}
                sequences = [segments[ref['segment_id']]['event_seq']
                    for task in unresolved for ref in task['source_refs']
                    if ref['segment_id'] in segments]
                previous_cursor = meeting['coordinator_cursor']
                meeting.update(
                    research_paused=True,
                    generation=meeting['generation'] + 1,
                    coordinator_state='outcome_unknown',
                    coordinator_error='outcome_unknown',
                    coordinator_error_details=None,
                    coordinator_error_message='新版仍有未确认的识别范围；此回退副本已暂停，请核对后显式重试。',
                    coordinator_cursor=min([previous_cursor, *[seq - 1 for seq in sequences]]),
                    coordinator_continuations=0,
                )
                meeting.pop('coordinator_retry', None)
                meeting.pop('detection_service_retry', None)
                meeting.pop('detection_service_error', None)
                copy.execute("UPDATE records SET data=? WHERE kind='wb_meeting' AND id=?",
                    (json.dumps(meeting, ensure_ascii=False), mid))
                guarded.append({'meeting_id': mid, 'unresolved_tasks': len(unresolved),
                    'previous_cursor': previous_cursor, 'replay_cursor': meeting['coordinator_cursor']})
        return {'source_modified': False, 'output': str(destination), 'paused_meetings': guarded}
    finally:
        original.close()
        copy.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare_rollback(args.source, args.output), ensure_ascii=False, indent=2))
