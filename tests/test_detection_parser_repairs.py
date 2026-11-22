import tempfile
import unittest
from pathlib import Path

from live.detection import parse_detection, quote_options
from live.workbench_contracts import NewMeeting, Utterance
from live_api import create_app


class DetectionParserRepairsTest(unittest.TestCase):
    def setUp(self):
        self.work = {'cursor': 1, 'meeting': {'coordinator_cursor': 0}, 'segments': [
            {'id': 's1', 'revision': 1, 'event_seq': 1, 'text': '？！谁？'}]}
        self.quote = {'kind': 'candidate', 'title': '这条标准由谁发布？', 'evidence_need': 'public',
            'external_fact': '标准的发布机构', 'public_query': '测试标准 发布机构',
            'original_target': {'kind': 'standard', 'identifier': '示例'},
            'grounding_quote': '谁？', 'source_refs': [{'segment_id': 's1', 'revision': 1}]}

    def repair(self, previous, action):
        work = {**self.work, 'detection_task': {'kind': 'correction', 'proposal': previous}}
        return parse_detection({'actions': [action]}, work, quote_options(work['segments'], 0))

    def test_quote_repair_preserves_public_fact_query_and_structured_target(self):
        previous = {**self.quote, 'grounding_quote': '未说过的原话'}
        for field, replacement in [('external_fact', '标准的起草时间'), ('public_query', '另一份标准'),
                ('original_target', {'kind': 'policy', 'identifier': '示例'}), ('original_target', None)]:
            with self.subTest(field=field, replacement=replacement):
                batch = self.repair(previous, {**self.quote, field: replacement})
                self.assertFalse(batch.actions)
                self.assertEqual(batch.failures[0]['issues'], [
                    {'field': f'actions.0.{field}', 'code': 'correction_changed_intent'}])
        equivalent = {**self.quote['original_target'], 'section': '', 'issuer': ''}
        batch = self.repair(previous, {**self.quote, 'original_target': equivalent})
        self.assertFalse(batch.failures)
        self.assertEqual(batch.actions[0].grounding_quote, '谁？')

    def test_invalid_prior_fields_can_be_repaired_without_weakening_valid_fields(self):
        for field in ('kind', 'title', 'question_id', 'scope_changed', 'expected_version',
                      'evidence_need', 'original_target', 'external_fact', 'public_query'):
            with self.subTest(field=field):
                batch = self.repair({**self.quote, field: []}, self.quote)
                self.assertFalse(batch.failures)
                self.assertEqual(batch.actions[0].title, self.quote['title'])
        previous = {**self.quote, 'title': '长' * 301}
        self.assertEqual(self.repair(previous, self.quote).actions[0].title, self.quote['title'])
        self.assertFalse(self.repair(previous, {**self.quote, 'external_fact': '不同事实'}).actions)

    def test_punctuation_cannot_ground_a_proposal_but_one_chinese_character_can(self):
        options = quote_options(self.work['segments'], 0)
        self.assertEqual([option['quote'] for option in options], ['谁？'])
        stale_option = {**options[0], 'id': 'stale', 'quote': '？！', 'start': 0, 'end': 2}
        actions = [{**self.quote, 'grounding_quote_id': q['id']} for q in [stale_option, options[0]]]
        batch = parse_detection({'actions': actions}, self.work, [stale_option, *options])
        self.assertEqual([a.grounding_quote for a in batch.actions], ['谁？'])
        self.assertEqual(batch.failures[0]['issues'][0]['code'], 'quote_not_in_new_segment')

    def test_live_action_limit_leaves_overflow_for_durable_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            wb = create_app(root / 'state.sqlite3', {'TESTING': True, 'WORKBENCH_ROOT': root / 'meetings',
                'WORKBENCH_CAPABILITIES': {'model_enabled': True, 'debounce_seconds': 0}}).extensions['workbench']
            members = wb.store.seed(fixture=True)['members']
            owner, agent = [wb.store.authenticate(m['token']) for m in (members[0], members[-1])]
            mid = wb.create(owner, NewMeeting(title='批次上限', data_scope='synthetic'))['id']
            wb.utterance(owner, mid, Utterance(text='甲？乙？丙？丁？戊？'))
            work = wb.coordinator_work(agent, lane='realtime')
            self.assertEqual(work['action_limit'], 4)
            options = quote_options(work['segments'], work['meeting']['coordinator_cursor'])
            actions = [{'kind': 'candidate', 'title': q['quote'], 'evidence_need': 'explanation',
                'grounding_quote_id': q['id'], 'source_refs': [{'segment_id': q['segment_id'], 'revision': q['revision']}]}
                for q in options]
            batch = parse_detection({'actions': actions}, work, options)
            self.assertEqual((batch.raw_count, len(batch.actions), batch.saturated), (5, 4, True))
            wb.commit_detection(agent, work, batch)
            self.assertEqual(len(wb.snapshot(owner, mid)['questions']), 4)
            scan = wb.detection_recovery_work(agent)
            self.assertEqual(scan['detection_task']['kind'], 'scan')
            wb.commit_detection(agent, scan, parse_detection({'actions': actions}, scan, options))
            snapshot = wb.snapshot(owner, mid)
            self.assertCountEqual([q['title'] for q in snapshot['questions']], [q['quote'] for q in options])
            self.assertFalse(snapshot['detection_tasks'])
