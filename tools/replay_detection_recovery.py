"""Replay a permitted meeting in isolated stores, with the selected real model.

Only detection is invoked: no audio capture, search, research or messaging.
The original SQLite connection is read-only; provider credentials stay in memory.
"""

import argparse
import copy
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from live.model_budget import ModelBudget
from live.provider_settings import SavedServices
from live.store import LiveStore
from live.workbench_runtime import TextModel, WorkbenchRuntime
from live_api import create_app


def read_source(path, mid):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute("SELECT data FROM records WHERE kind='wb_meeting' AND id=?", (mid,)).fetchone()
        meeting = json.loads(row[0])
        records = [(kind, key, json.loads(data)) for kind, key, data in db.execute(
            "SELECT kind,id,data FROM records WHERE json_extract(data,'$.meeting_id')=?", (mid,))]
    return meeting, records


def replay(source, settings, mid, output):
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    original, records = read_source(source, mid)
    if not original.get('permissions', {}).get('model'):
        raise ValueError('原会议没有文字模型授权，不能发送回放输入')
    services = SavedServices.model_validate_json(settings.read_text(encoding='utf-8'))
    if not services.model.enabled or not services.model.configured:
        raise ValueError('当前文字模型未启用')
    original_digest = hashlib.sha256(json.dumps([original, records], sort_keys=True).encode()).hexdigest()
    results = []
    budget = ModelBudget(output / 'model-usage.sqlite3', limit='40')
    for phase in ['checkpoint', 'full']:
        folder = output / phase
        folder.mkdir(mode=0o700)
        store = LiveStore(folder / 'state.sqlite3')
        members = store.seed(fixture=True)['members']
        owner, agent = [store.authenticate(m['token']) for m in (members[0], members[-1])]
        app = create_app(store.path, {'TESTING': True, 'WORKBENCH_ROOT': folder / 'meetings',
            'WORKBENCH_CAPABILITIES': {**services.capabilities(), 'debounce_seconds': 0}})
        wb = app.extensions['workbench']
        meeting = copy.deepcopy(original)
        meeting.update(project_id=owner['project_id'], output_root=str((folder / 'meetings').resolve()),
            directory='meeting-copy', research_paused=False, coordinator_state='pending',
            coordinator_error=None, coordinator_error_details=None, coordinator_error_message=None,
            coordinator_continuations=0, generation=original['generation'] + 1,
            coordinator_cursor=original['coordinator_cursor'] if phase == 'checkpoint' else 0)
        meeting.pop('coordinator_retry', None)
        initial_cursor = meeting['coordinator_cursor']
        segments = [value for kind, _, value in records if kind == 'wb_segment' and value['type'] == 'transcript.final']
        target_refs = {(s['id'], s['revision']) for s in segments if s['event_seq'] > initial_cursor}
        with store.transaction() as db:
            store._put(db, 'wb_meeting', mid, meeting)
            for kind, key, value in records:
                if kind == 'wb_segment' or (phase == 'checkpoint' and kind in {'wb_question', 'wb_document'}):
                    store._put(db, kind, key, value)
        model = TextModel(budget, services.model)
        runtime = WorkbenchRuntime(wb, model=model)
        calls = []
        observed = set()
        started = time.monotonic()
        try:
            for _ in range(50):
                work = wb.coordinator_work(agent, lane='background') or wb.detection_recovery_work(agent)
                if not work:
                    break
                for s in work['segments']:
                    if s.get('is_new', s['event_seq'] > work['meeting']['coordinator_cursor']):
                        observed.add((s['id'], s['revision']))
                call_start = time.monotonic()
                runtime._coordinate(agent, work)
                snap = wb.snapshot(owner, mid)
                entry = {'lane': work['lane'], 'input_segments': len(work['segments']),
                    'seconds': round(time.monotonic() - call_start, 3), 'progress': snap['automation_progress']}
                calls.append(entry)
                print(json.dumps({'phase': phase, 'call': len(calls), **entry}, ensure_ascii=False), flush=True)
            snap = wb.snapshot(owner, mid)
            with store.transaction() as db:
                tasks = wb._detection_tasks(db, mid)
            pending_refs = {(r['segment_id'], r['revision']) for task in tasks
                if task['state'] not in {'completed', 'superseded'} for r in task['source_refs']}
            target_seen = target_refs.intersection(observed)
            result = {'phase': phase, 'input_final_segments': len(segments),
                'expected_new_segments': len(target_refs), 'observed_new_segments': len(target_seen),
                'every_target_observed': target_refs.issubset(observed),
                'unresolved_target_segments': len(target_refs.intersection(pending_refs)),
                'progress': snap['automation_progress'], 'question_count': len(snap['questions']),
                'main_failure': snap['meeting'].get('coordinator_error'),
                'service_failure': (snap['meeting'].get('detection_service_error') or {}).get('code'),
                'calls': calls, 'seconds': round(time.monotonic() - started, 3)}
            # Keep model-visible question/quote pairs locally for a semantic review;
            # these are not a human-labelled recall benchmark.
            (folder / 'questions-private.json').write_text(json.dumps([
                {k: q.get(k) for k in ['id', 'title', 'grounding_quote', 'source_refs', 'evidence_need']}
                for q in snap['questions']], ensure_ascii=False, indent=2), encoding='utf-8')
            (folder / 'questions-private.json').chmod(0o600)
            results.append(result)
        finally:
            runtime.close()
    after_meeting, after_records = read_source(source, mid)
    unchanged = original_digest == hashlib.sha256(json.dumps([after_meeting, after_records], sort_keys=True).encode()).hexdigest()
    report = {'original_records_unchanged': unchanged, 'original_open_mode': 'read_only',
        'search_calls': 0, 'production_outputs_written': 0, 'model': services.model.name,
        'semantic_recall_status': 'not_human_labelled', 'phases': results}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--meeting', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = replay(args.source, args.settings, args.meeting, args.output.resolve())
    print(json.dumps({k: v for k, v in result.items() if k != 'phases'}, ensure_ascii=False))
