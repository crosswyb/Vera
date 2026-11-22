import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from live.workbench_contracts import NewMeeting, Utterance
from live.workbench_runtime import TextModel, WorkbenchRuntime
from live_api import create_app


class GroundedDetectionTest(unittest.TestCase):
    def setUp(self):
        self.model = TextModel.__new__(TextModel)
        self.market = '现在原料涨了，成品的价格有跟上吗？'
        self.concept = '边际成本是什么意思？'
        self.work = {'cursor': 3, 'meeting': {'coordinator_cursor': 1}, 'questions': [], 'segments': [
            {'id': 'old', 'revision': 1, 'event_seq': 1, 'text': '上次已经核过这款产品的报价。'},
            {'id': 's2', 'revision': 2, 'event_seq': 2, 'text': '我们想管好采购。' + self.market},
            {'id': 's3', 'revision': 1, 'event_seq': 3, 'text': self.concept}]}
        self.fact = {'kind': 'candidate', 'title': '当期原料与成品价格走势', 'evidence_need': 'public',
                     'external_fact': '同期成品涨价是否跟上原料', 'grounding_quote': self.market,
                     'source_refs': [{'segment_id': 's2', 'revision': 2}], 'public_query': '原料 成品价格走势'}
        self.explanation = {'kind': 'create_question', 'title': self.concept, 'evidence_need': 'explanation',
                            'grounding_quote': self.concept, 'source_refs': [{'segment_id': 's3', 'revision': 1}]}

    def run_batch(self, actions):
        self.model.call = Mock(return_value={'actions': actions})
        result = self.model.decisions(self.work)
        self.model.call.assert_called_once()
        return result.actions

    def test_grounded_public_fact_and_explanation_can_coexist(self):
        public, explanation = self.run_batch([self.fact, self.explanation])
        self.assertEqual(public.external_fact, self.fact['external_fact'])
        self.assertEqual(explanation.grounding_quote, self.concept)
        self.assertEqual(explanation.public_query, '')

    def test_grounding_failures_do_not_drop_valid_siblings(self):
        for invalid in [dict(grounding_quote='改写的会议引文'), dict(external_fact=' '),
                        dict(grounding_quote=self.concept), dict(source_refs=[{'segment_id': 's2', 'revision': 1}]),
                        dict(grounding_quote='上次已经核过', source_refs=[{'segment_id': 'old', 'revision': 1},
                                                                      {'segment_id': 's2', 'revision': 2}]),
                        dict(evidence_need=None)]:
            with self.subTest(invalid=invalid):
                self.model.call = Mock(return_value={'actions': [{**self.fact, **invalid}, self.explanation]})
                batch = self.model.decisions(self.work)
                self.assertEqual([a.title for a in batch.actions], [self.concept])
                self.assertEqual(len(batch.failures), 1)
                self.assertEqual(batch.failures[0]['proposal']['title'], self.fact['title'])
                self.assertEqual(batch.raw_count, 2)
                self.model.call.assert_called_once()

    def test_only_changed_sentence_ending_uses_original_text_without_model_retry(self):
        (decision,) = self.run_batch([{**self.fact, 'grounding_quote': self.market[:-1] + '。'}])
        self.assertEqual(decision.grounding_quote, self.market)

    def test_unrepaired_proposal_or_omission_never_commits_as_an_empty_batch(self):
        bad = {**self.fact, 'grounding_quote': '未经讨论的新问题'}
        for correction in ([bad], [], [{'kind': 'no_action'}]):
            with self.subTest(correction=correction):
                self.model.call = Mock(return_value={'actions': correction})
                work = {**self.work, 'detection_task': {'kind': 'correction', 'proposal': bad, 'issues': []}}
                batch = self.model.decisions(work)
                self.assertFalse(batch.actions)
                self.assertTrue(batch.failures)
                self.model.call.assert_called_once()

    def test_internal_and_explanation_never_carry_accidental_public_payload(self):
        for need in ['internal', 'explanation']:
            with self.subTest(need=need):
                decision, = self.run_batch([{**self.explanation, 'evidence_need': need, 'external_fact': '多余',
                                             'public_query': '多余'}])
                self.assertEqual((decision.external_fact, decision.public_query), ('', ''))

    def test_unrepaired_quote_is_deferred_without_stopping_the_coordinator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wb = create_app(root / 'audit.sqlite3', {'TESTING': True, 'WORKBENCH_ROOT': root / 'meetings',
                'WORKBENCH_CAPABILITIES': {'model_enabled': True, 'debounce_seconds': 0}}).extensions['workbench']
            members = wb.store.seed(fixture=True)['members']
            owner, agent = [wb.store.authenticate(m['token']) for m in (members[0], members[-1])]
            mid = wb.create(owner, NewMeeting(title='引用校验', data_scope='synthetic'))['id']
            wb.utterance(owner, mid, Utterance(text=self.concept))
            work = wb.coordinator_work(agent)
            segment = work['segments'][0]
            proposal = {**self.explanation, 'source_refs': [{'segment_id': segment['id'], 'revision': segment['revision']}]}
            self.model.call = Mock(return_value={'actions': [{**proposal, 'grounding_quote': '转写中没有的词'}]})
            self.model.sessions = Mock()
            runtime = WorkbenchRuntime(wb, model=self.model, researcher=Mock())
            try:
                runtime._coordinate(agent, work)
                snapshot = wb.snapshot(owner, mid)
                meeting = snapshot['meeting']
                self.assertEqual(self.model.call.call_count, 1)
                self.assertEqual(meeting['coordinator_state'], 'idle')
                self.assertEqual(meeting['coordinator_cursor'], work['cursor'])
                self.assertIsNone(meeting['coordinator_error'])
                self.assertFalse(snapshot['questions'])
                self.assertEqual(len(snapshot['detection_tasks']), 1)
                retry = wb.detection_recovery_work(agent)
                self.assertEqual(retry['cursor'], work['cursor'])
                self.model.call.return_value = {'actions': [proposal]}
                runtime._coordinate(agent, retry)
                result = wb.snapshot(owner, mid)
                self.assertEqual(result['meeting']['coordinator_cursor'], work['cursor'])
                self.assertEqual([q['title'] for q in result['questions']], [self.concept])
            finally:
                runtime.close()
