"""Acceptance of per-item detection failures using the actual store/runtime seams."""

import json
import hashlib
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from live.detection import parse_detection, quote_options
from live.store import LiveError
from live.workbench_contracts import EditQuestion, MeetingPermissions, NewMeeting, Utterance
from live.workbench_runtime import TextModel, WorkbenchRuntime
from live_api import create_app


class DetectionRecoveryTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.config = {'TESTING': True, 'WORKBENCH_ROOT': self.root / 'meetings',
            'WORKBENCH_CAPABILITIES': {'model_enabled': True, 'debounce_seconds': 0}}
        self.app = create_app(self.root / 'state.sqlite3', self.config)
        self.wb = self.app.extensions['workbench']
        members = self.wb.store.seed(fixture=True)['members']
        self.owner, self.agent = [self.wb.store.authenticate(m['token']) for m in (members[0], members[-1])]
        self.mid = self.wb.create(self.owner, NewMeeting(title='隔离恢复验收', data_scope='synthetic'))['id']
        self.model = TextModel.__new__(TextModel)
        self.model.call = Mock()
        self.model.sessions = Mock()
        self.runtime = WorkbenchRuntime(self.wb, model=self.model, researcher=Mock())
        self.addCleanup(self.runtime.close)

    def say(self, text, sid='', revision=1):
        self.wb.utterance(self.owner, self.mid, Utterance(text=text, segment_id=sid, revision=revision))

    def snapshot(self):
        return self.wb.snapshot(self.owner, self.mid)

    def work(self):
        return self.wb.coordinator_work(self.agent, lane='realtime')

    def action(self, work, title, index=0):
        options = quote_options(work['segments'], work['meeting']['coordinator_cursor'])
        quote = options[index]
        return {'kind': 'candidate', 'title': title, 'evidence_need': 'explanation',
            'grounding_quote_id': quote['id'],
            'source_refs': [{'segment_id': quote['segment_id'], 'revision': quote['revision']}]}

    def run_work(self, work, actions):
        self.model.call.return_value = {'actions': actions}
        self.runtime._coordinate(self.agent, work)

    def bad_batch(self):
        self.say('边际成本是什么？固定成本是什么？', 'first')
        work = self.work()
        good = self.action(work, '边际成本')
        bad = {**self.action(work, '固定成本', 1), 'grounding_quote_id': 'absent'}
        self.run_work(work, [good, bad])
        return work, good, bad

    def test_good_bad_then_new_question_without_manual_retry(self):
        self.bad_batch()
        first = self.snapshot()
        self.assertEqual([q['title'] for q in first['questions']], ['边际成本'])
        self.assertEqual(first['meeting']['coordinator_state'], 'idle')
        self.assertFalse(first['automation_progress']['complete'])
        self.say('现金流是什么？', 'next')
        live = self.work()
        self.run_work(live, [self.action(live, '现金流')])
        repair = self.wb.detection_recovery_work(self.agent)
        self.assertEqual(repair['lane'], 'recovery')
        self.run_work(repair, [self.action(repair, '固定成本', 1)])
        final = self.snapshot()
        self.assertEqual([q['title'] for q in final['questions']], ['边际成本', '现金流', '固定成本'])
        self.assertTrue(final['automation_progress']['complete'])
        self.assertFalse(final['detection_tasks'])
        self.assertEqual(self.model.call.call_count, 3)

    def test_invalid_field_does_not_reject_valid_sibling(self):
        for invalid in [7, float('nan')]:
            with self.subTest(invalid=invalid):
                self.mid = self.wb.create(self.owner, NewMeeting(title='错误字段隔离', data_scope='synthetic'))['id']
                self.say('什么是成本？')
                work = self.work()
                self.run_work(work, [{**self.action(work, '无效'), 'title': invalid}, self.action(work, '有效')])
                self.assertEqual([q['title'] for q in self.snapshot()['questions']], ['有效'])
                self.assertTrue(self.snapshot()['detection_tasks'])
                with self.wb.store.transaction() as db:
                    self.assertFalse(db.execute('SELECT id FROM records WHERE NOT json_valid(data)').fetchall())

    def test_unrepairable_or_omitted_item_becomes_visible_review(self):
        for raw in [[], [{'kind': 'no_action'}]]:
            with self.subTest(raw=raw):
                self.mid = self.wb.create(self.owner, NewMeeting(title='遗漏修正', data_scope='synthetic'))['id']
                self.bad_batch()
                retry = self.wb.detection_recovery_work(self.agent)
                self.run_work(retry, raw)
                snap = self.snapshot()
                self.assertEqual(snap['detection_tasks'][0]['state'], 'needs_review')
                self.assertFalse(snap['automation_progress']['complete'])
                self.assertIsNone(self.wb.detection_recovery_work(self.agent))

    def test_duplicate_main_and_recovery_callbacks_are_noops(self):
        work, good, bad = self.bad_batch()
        parsed = parse_detection({'actions': [good, bad]}, work, quote_options(work['segments'], 0))
        self.wb.commit_detection(self.agent, work, parsed)
        retry = self.wb.detection_recovery_work(self.agent)
        action = self.action(retry, '固定成本', 1)
        result = parse_detection({'actions': [action]}, retry, quote_options(retry['segments'], retry['meeting']['coordinator_cursor']))
        self.wb.commit_detection(self.agent, retry, result)
        self.wb.commit_detection(self.agent, retry, result)
        self.assertEqual(len(self.snapshot()['questions']), 2)
        self.assertIsNone(self.wb.detection_recovery_work(self.agent))

    def test_database_failure_rolls_back_questions_tasks_and_cursor(self):
        self.say('边际成本是什么？固定成本是什么？')
        work = self.work()
        result = parse_detection({'actions': [self.action(work, '正常'), {**self.action(work, '坏项', 1), 'grounding_quote_id': 'bad'}]},
            work, quote_options(work['segments'], 0))
        original = self.wb.store._put
        def fail_task(db, kind, key, value):
            if kind == 'wb_detection_task':
                raise OSError('synthetic disk failure')
            return original(db, kind, key, value)
        with patch.object(self.wb.store, '_put', side_effect=fail_task), self.assertRaises(OSError):
            self.wb.commit_detection(self.agent, work, result)
        snap = self.snapshot()
        self.assertFalse(snap['questions'])
        self.assertFalse(snap['detection_tasks'])
        self.assertEqual(snap['meeting']['coordinator_cursor'], 0)

    def test_saturated_batch_preserves_fifth_question_and_does_not_duplicate(self):
        self.say('问题一？问题二？问题三？问题四？问题五？')
        work = self.work()
        actions = [self.action(work, f'题{i + 1}', i) for i in range(4)]
        actions[3]['grounding_quote_id'] = 'bad'
        self.run_work(work, actions)
        self.assertEqual(len(self.snapshot()['questions']), 3)
        self.assertTrue(any(t['kind'] == 'scan' for t in self.snapshot()['detection_tasks']))
        for _ in range(3):
            retry = self.wb.detection_recovery_work(self.agent)
            if retry is None:
                break
            if retry['detection_task']['kind'] == 'correction':
                self.run_work(retry, [self.action(retry, '题4', 3)])
            else:
                self.assertEqual({a['title'] for a in retry['handled_detection_actions']}, {'题1', '题2', '题3', '题4'})
                self.run_work(retry, [self.action(retry, '题1', 0), self.action(retry, '题5', 4)])
        self.assertEqual({q['title'] for q in self.snapshot()['questions']}, {f'题{i}' for i in range(1, 6)})
        self.assertEqual(len(self.snapshot()['questions']), 5)
        self.assertTrue(self.snapshot()['automation_progress']['complete'])

    def test_short_question_gets_literal_option(self):
        self.say('为什么？')
        work = self.work()
        self.run_work(work, [self.action(work, '原因')])
        self.assertEqual(self.snapshot()['questions'][0]['grounding_quote'], '为什么？')

    def test_correction_cannot_replace_title_kind_or_evidence_scope(self):
        for changed in [{'title': '另一个问题'}, {'kind': 'create_question'}, {'evidence_need': 'internal'}]:
            with self.subTest(changed=changed):
                self.mid = self.wb.create(self.owner, NewMeeting(title='不允许换题', data_scope='synthetic'))['id']
                self.bad_batch()
                retry = self.wb.detection_recovery_work(self.agent)
                self.run_work(retry, [{**self.action(retry, '固定成本', 1), **changed}])
                snap = self.snapshot()
                self.assertEqual(len(snap['questions']), 1)
                self.assertEqual(snap['detection_tasks'][0]['issues'][0]['code'], 'correction_changed_intent')
                self.assertEqual(snap['detection_tasks'][0]['state'], 'needs_review')

    def test_scan_rewording_is_visible_instead_of_creating_a_duplicate(self):
        self.say('问题一？问题二？问题三？问题四？问题五？')
        work = self.work()
        self.run_work(work, [self.action(work, f'题{i + 1}', i) for i in range(4)])
        for _ in range(2):
            scan = self.wb.detection_recovery_work(self.agent)
            self.run_work(scan, [self.action(scan, '题一的改写', 0), self.action(scan, '题5', 4)])
        snap = self.snapshot()
        self.assertEqual(len(snap['questions']), 5)
        self.assertEqual(snap['detection_tasks'][0]['issues'][0]['code'], 'overlapping_proposal')
        self.assertEqual(snap['automation_progress']['needs_review'], 1)

    def test_two_independent_questions_can_share_one_utterance(self):
        self.say('请解释边际成本和固定成本的含义')
        work = self.work()
        self.run_work(work, [self.action(work, '边际成本'), self.action(work, '固定成本')])
        self.assertEqual(len(self.snapshot()['questions']), 2)

    def test_commit_isolates_a_concurrent_revision_from_valid_sibling(self):
        self.say('成本是什么？', 'cost')
        self.say('收入是什么？', 'revenue')
        work = self.work()
        result = parse_detection({'actions': [self.action(work, '成本'), self.action(work, '收入', 1)]},
            work, quote_options(work['segments'], 0))
        self.say('更正收入的定义？', 'revenue', revision=2)
        self.wb.commit_detection(self.agent, work, result)
        snap = self.snapshot()
        self.assertEqual([q['title'] for q in snap['questions']], ['成本'])
        self.assertEqual(snap['automation_progress']['pending'], 1)
        self.assertIsNone(self.wb.detection_recovery_work(self.agent))
        self.assertEqual(self.work()['segments'][-1]['revision'], 2)

    def test_partially_revised_scan_keeps_unchanged_targets(self):
        for i in range(4):
            self.say(f'待识别问题{i}？', f's{i}')
        work = self.work()
        self.model.call.side_effect = json.JSONDecodeError('synthetic', '', 0)
        self.runtime._coordinate(self.agent, work)
        self.model.call.side_effect = None
        self.say('更正后的问题？', 's0', revision=2)
        retry = self.wb.detection_recovery_work(self.agent)
        targets = [s['id'] for s in retry['segments'] if s['is_new']]
        self.assertEqual(set(targets), {s['id'] for s in work['segments'][1:]})
        self.run_work(retry, [])
        self.assertEqual(self.snapshot()['automation_progress']['pending'], 1)
        current = self.work()
        self.run_work(current, [self.action(current, '更正后的问题')])
        self.assertTrue(self.snapshot()['automation_progress']['complete'])

    def test_recovery_gets_a_worker_while_realtime_input_is_running(self):
        self.bad_batch()
        self.say('实时问题？', 'live')
        entered = {name: threading.Event() for name in ['live', 'recovery']}
        release = threading.Event()
        def respond(_system, content, **_kwargs):
            name = 'recovery' if 'previous_proposal' in content else 'live'
            entered[name].set()
            if not release.wait(3):
                raise TimeoutError('synthetic gate')
            option = content['grounding_options'][1 if name == 'recovery' else 0]
            return {'actions': [{'kind': 'candidate', 'title': '固定成本' if name == 'recovery' else '实时问题',
                'evidence_need': 'explanation', 'grounding_quote_id': option['id'],
                'source_refs': [{'segment_id': option['segment_id'], 'revision': option['revision']}]}]}
        self.model.call.side_effect = respond
        try:
            with patch.object(self.wb, 'claim', return_value=None):
                self.runtime.tick()
            self.assertTrue(entered['live'].wait(2))
            self.assertTrue(entered['recovery'].wait(2), 'continuous realtime work starved recovery')
            self.say('后续仍继续保存', 'next-live')
        finally:
            release.set()
            for future in list(self.runtime.worker_tasks):
                future.result(timeout=3)
        self.assertEqual(len(self.snapshot()['questions']), 3)
        self.assertEqual(self.snapshot()['automation_progress']['pending'], 1)

    def test_service_failure_has_one_bounded_backoff_for_all_gaps(self):
        self.bad_batch()
        self.say('另一个需要补充识别的问题？', 'another-gap')
        work = self.work()
        self.run_work(work, [{**self.action(work, '另一个问题'), 'grounding_quote_id': 'missing'}])
        self.assertEqual(len(self.snapshot()['detection_tasks']), 2)
        details = {'service': 'model', 'code': 'http_error', 'http_status': 429,
            'message': '合成限流', 'retry_after_seconds': 30}
        for attempt in range(4):
            task = self.wb.detection_recovery_work(self.agent)
            self.assertIsNotNone(task)
            self.wb.coordinator_failed(self.agent, task, details=details)
            snap = self.snapshot()
            self.assertIsNone(self.wb.detection_recovery_work(self.agent))
            if attempt < 3:
                retry = snap['meeting']['detection_service_retry']
                self.assertEqual(retry['delay_seconds'], 30)
                self.assertEqual(retry['attempts'], attempt + 1)
                self.say(f'限流期间的新发言 {attempt}', f'transport-{attempt}')
                self.assertIsNone(self.work(), 'new input bypassed service backoff')
                with self.wb.store.transaction() as db:
                    m = self.wb.store._get(db, 'wb_meeting', self.mid)
                    m['detection_service_retry']['next_at'] = 0
                    self.wb._save(db, m)
        self.assertEqual(snap['meeting']['detection_service_retry']['state'], 'exhausted')
        self.assertTrue(any(t['state'] == 'needs_review' for t in snap['detection_tasks']))
        self.assertFalse(snap['automation_progress']['complete'])

    def test_main_transport_incident_blocks_recovery_until_main_succeeds(self):
        self.bad_batch()
        self.say('服务故障期间的新问题？', 'new-input')
        work = self.work()
        self.wb.coordinator_failed(self.agent, work, details={'category': 'request', 'stage': 'detecting', 'service': 'model', 'code': 'http_error',
            'http_status': 503, 'message': '合成服务不可用'})
        self.assertIsNone(self.wb.detection_recovery_work(self.agent))
        retry_at = self.snapshot()['meeting']['coordinator_retry']['next_at'] + 1
        with patch('live.workbench.time.time', return_value=retry_at):
            main = self.work()
        self.assertIsNotNone(main)
        self.assertIsNone(self.wb.detection_recovery_work(self.agent))
        self.run_work(main, [])
        self.assertIsNotNone(self.wb.detection_recovery_work(self.agent))

    def test_deleting_meeting_discards_late_task_failure(self):
        self.bad_batch()
        task = self.wb.detection_recovery_work(self.agent)
        with self.wb.store.transaction() as db:
            db.execute("DELETE FROM records WHERE kind='wb_detection_task'")
            db.execute("DELETE FROM records WHERE kind='wb_meeting' AND id=?", (self.mid,))
        self.wb.coordinator_failed(self.agent, task, details={'service': 'local', 'code': 'unknown_outcome'})

    def test_rollback_export_pauses_old_runtime_and_keeps_original_untouched(self):
        from tools.prepare_detection_rollback import prepare_rollback
        self.bad_batch()
        source = self.wb.store.path
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        target = self.root / 'rollback.sqlite3'
        report = prepare_rollback(source, target)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        self.assertEqual(len(report['paused_meetings']), 1)
        old = create_app(target, self.config).extensions['workbench']
        meeting = old.snapshot(self.owner, self.mid)['meeting']
        self.assertTrue(meeting['research_paused'])
        self.assertEqual(meeting['coordinator_state'], 'outcome_unknown')
        self.assertLess(meeting['coordinator_cursor'], self.snapshot()['meeting']['coordinator_cursor'])
        self.assertIsNone(old.coordinator_work(self.agent))
        self.assertEqual(len(old.snapshot(self.owner, self.mid)['detection_tasks']), 1)
        with self.assertRaises(ValueError):
            prepare_rollback(source, target)

    def test_rollback_export_keeps_dismissals_terminal_for_older_runtime(self):
        from tools.prepare_detection_rollback import prepare_rollback
        self.bad_batch()
        self.run_work(self.wb.detection_recovery_work(self.agent), [])
        task, = self.snapshot()['detection_tasks']
        self.wb.dismiss_detection_task(self.owner, self.mid, task['id'])
        with self.wb.store.transaction() as db:
            original = self.wb.store._get(db, 'wb_detection_task', task['id'])
        source = self.wb.store.path
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        target = self.root / 'dismissed-rollback.sqlite3'
        report = prepare_rollback(source, target)
        self.assertFalse(report['paused_meetings'])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        with sqlite3.connect(target) as copy:
            exported = json.loads(copy.execute(
                "SELECT data FROM records WHERE kind='wb_detection_task' AND id=?", (task['id'],)).fetchone()[0])
        self.assertEqual(exported, {**original, 'state': 'superseded'})
        self.assertEqual(original['state'], 'dismissed')
        self.assertEqual(exported['dismissed_by'], self.owner['id'])
        self.assertGreater(exported['dismissed_at'], 0)

    def test_entire_invalid_json_is_scanned_in_bounded_windows(self):
        self.say('需要补扫的范围？')
        work = self.work()
        self.model.call.side_effect = json.JSONDecodeError('synthetic', '', 0)
        self.runtime._coordinate(self.agent, work)
        self.assertEqual(self.snapshot()['meeting']['coordinator_state'], 'idle')
        for _ in range(2):
            retry = self.wb.detection_recovery_work(self.agent)
            self.assertIsNotNone(retry)
            self.runtime._coordinate(self.agent, retry)
        self.assertIsNone(self.wb.detection_recovery_work(self.agent))
        self.assertEqual(self.snapshot()['automation_progress']['needs_review'], 1)

    def test_restart_preserves_tasks_and_pauses_scheduling(self):
        self.bad_batch()
        reopened = create_app(self.root / 'state.sqlite3', self.config).extensions['workbench']
        reopened.recover(publish=False)
        snap = reopened.snapshot(self.owner, self.mid)
        self.assertTrue(snap['meeting']['research_paused'])
        self.assertEqual(len(snap['detection_tasks']), 1)
        self.assertIsNone(reopened.detection_recovery_work(self.agent))
        reopened.control(self.owner, self.mid, 'resume_research')
        self.assertIsNotNone(reopened.detection_recovery_work(self.agent))

    def test_pause_end_and_revocation_fence_recovery_results(self):
        for operation in ['pause_research', 'end', 'revoke']:
            with self.subTest(operation=operation):
                self.mid = self.wb.create(self.owner, NewMeeting(title=operation, data_scope='real'))['id']
                self.bad_batch()
                retry = self.wb.detection_recovery_work(self.agent)
                result = parse_detection({'actions': [self.action(retry, '固定成本', 1)]}, retry,
                    quote_options(retry['segments'], retry['meeting']['coordinator_cursor']))
                if operation == 'revoke':
                    self.wb.permissions(self.owner, self.mid, MeetingPermissions())
                else:
                    self.wb.control(self.owner, self.mid, operation)
                with self.assertRaises(LiveError):
                    self.wb.commit_detection(self.agent, retry, result)
                self.assertEqual(len(self.snapshot()['questions']), 1)

    def test_revised_transcript_supersedes_old_task_and_is_unread(self):
        self.bad_batch()
        self.say('更正后的新问题？', 'first', revision=2)
        self.assertIsNone(self.wb.detection_recovery_work(self.agent))
        self.assertFalse(self.snapshot()['detection_tasks'])
        work = self.work()
        self.assertEqual(work['segments'][-1]['revision'], 2)

    def test_revision_invalidates_review_and_running_tasks_immediately(self):
        for state in ['review', 'running']:
            with self.subTest(state=state):
                self.mid = self.wb.create(self.owner, NewMeeting(title=state, data_scope='synthetic'))['id']
                self.bad_batch()
                task = self.wb.detection_recovery_work(self.agent)
                if state == 'review':
                    self.run_work(task, [])
                    self.assertEqual(self.snapshot()['automation_progress']['needs_review'], 1)
                self.say('更正后的问题？', 'first', revision=2)
                self.assertFalse(self.snapshot()['detection_tasks'])
                if state == 'running':
                    self.run_work(task, [self.action(task, '固定成本', 1)])
                self.assertEqual(len(self.snapshot()['questions']), 1)
                self.assertEqual(self.snapshot()['automation_progress']['pending'], 1)

    def test_targeted_retry_requires_auth_and_does_not_restart_other_work(self):
        self.bad_batch()
        retry = self.wb.detection_recovery_work(self.agent)
        self.run_work(retry, [])
        tid = retry['detection_task']['id']
        client = self.app.test_client()
        path = f'/live/v2/meetings/{self.mid}/detection-tasks/{tid}/retry'
        self.assertEqual(client.post(path, json={}).status_code, 401)
        headers = {'Authorization': 'Bearer fixture-owner-0'}
        self.assertEqual(client.post(path, json={}, headers={**headers, 'Origin': 'https://evil.invalid'}).status_code, 403)
        self.assertEqual(client.post(path, json={}, headers=headers).status_code, 200)
        self.assertEqual(client.post(path, json={}, headers=headers).status_code, 409)
        self.assertEqual(self.snapshot()['meeting']['coordinator_state'], 'idle')

    def test_manual_retry_refreshes_nearby_context_but_rejects_obsolete_question_version(self):
        self.say('当前适用范围？', 'question')
        work = self.work()
        self.run_work(work, [self.action(work, '适用范围')])
        q, = self.snapshot()['questions']
        self.say('还要包括新范围。', 'change')
        work = self.work()
        self.run_work(work, [{**self.action(work, '包含附加范围'),
            'kind': 'revise_question', 'question_id': q['id'], 'expected_version': 1, 'grounding_quote_id': 'missing'}])
        first = self.wb.detection_recovery_work(self.agent)
        self.run_work(first, [])
        self.say('补充前一条的适用环境。', 'context')
        tid = first['detection_task']['id']
        self.wb.retry_detection_task(self.owner, self.mid, tid)
        retry = self.wb.detection_recovery_work(self.agent)
        self.assertEqual(retry['segments'][-1]['text'], '补充前一条的适用环境。')
        self.assertFalse(retry['segments'][-1]['is_new'])
        self.run_work(retry, [])
        self.wb.edit(self.owner, q['id'], EditQuestion(expected_version=1, title='人工确认了另一范围'))
        with self.assertRaisesRegex(LiveError, '关联问题版本或状态已变化'):
            self.wb.retry_detection_task(self.owner, self.mid, tid)


if __name__ == '__main__':
    unittest.main()
