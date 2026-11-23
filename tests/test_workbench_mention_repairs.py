import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from live.source_reader import Researcher
from live.workbench_contracts import MeetingPermissions, NewMeeting, QuestionAction, Utterance
from live.workbench_runtime import TextModel
from live_api import create_app


class MentionRepairsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.wb = create_app(root / 'state.db', {'TESTING': True, 'WORKBENCH_ROOT': root / 'meetings',
            'WORKBENCH_CAPABILITIES': {'model_enabled': True, 'search_enabled': True, 'debounce_seconds': 0}}).extensions['workbench']
        members = self.wb.store.seed(fixture=True)['members']
        self.owner, self.agent = (self.wb.store.authenticate(m['token']) for m in (members[0], members[-1]))
        self.mid = self.wb.create(self.owner, NewMeeting(title='离线查证场次'))['id']
        self.model = TextModel.__new__(TextModel)

    def questions(self):
        return self.wb.snapshot(self.owner, self.mid)['questions']

    def batch(self, text, *actions):
        self.wb.utterance(self.owner, self.mid, Utterance(text=text))
        work = self.wb.coordinator_work(self.agent)
        ref = {'segment_id': work['segments'][-1]['id'], 'revision': 1}
        self.model.call = Mock(return_value={'actions': [dict({'kind': 'create_question', 'title': '利润口径',
            'grounding_quote': text, 'source_refs': [ref]}, **a) for a in actions]})
        self.wb.commit_decisions(self.agent, work, self.model.decisions(work))

    def mention(self, question, need, query=''):
        return dict(kind='revise_question', question_id=question['id'], expected_version=question['version'],
                    scope_changed=False, evidence_need=need, external_fact='公开利润口径', public_query=query)

    def test_grounded_aside_does_not_roll_back_valid_sibling_or_erase_owner_term(self):
        self.batch('核对利润口径。', dict(evidence_need='public', external_fact='公开利润口径', public_query='利润'))
        q, = self.questions()
        self.wb.action(self.owner, q['id'], QuestionAction(action='approve', expected_version=1, public_query='公开利润定义'))
        self.batch('再解释一下。', dict(title='其他解释', evidence_need='explanation'), self.mention(q, 'explanation'))
        first, sibling = self.questions()
        self.assertEqual((first['public_query'], first['public_query_origin'], first['evidence_need']), ('公开利润定义', 'owner', 'public'))
        self.assertEqual(len(first['source_refs']), 2)
        self.assertEqual(sibling['title'], '其他解释')
        self.assertIsNone(self.wb.coordinator_work(self.agent))

    def test_promoted_internal_question_waits_for_owner_without_disclosing_basis(self):
        self.batch('只要内部成本。', dict(evidence_need='internal'))
        q, = self.questions()
        self.batch('再核公开口径。', self.mention(q, 'public'))
        q, = self.questions()
        self.assertEqual((q['evidence_need'], q['state'], q['public_query']), ('mixed', 'needs_query', ''))
        self.assertIsNone(self.wb.claim(self.agent))
        self.wb.action(self.owner, q['id'], QuestionAction(action='approve', expected_version=1, public_query='行业成本'))
        claimed = self.wb.claim(self.agent)['question']
        self.assertEqual((claimed['public_query'], claimed['public_query_origin']), ('行业成本', 'owner'))

    def test_authorized_followup_can_fill_missing_query(self):
        self.batch('先核内部口径。', dict(evidence_need='internal'))
        q, = self.questions()
        self.batch('再核公开口径。', self.mention(q, 'public'))
        self.batch('核公开成本数据。', self.mention(q, 'public', '公开成本'))
        claimed = self.wb.claim(self.agent)['question']
        self.assertEqual((claimed['evidence_need'], claimed['public_query'], claimed['public_query_origin']), ('mixed', '公开成本', 'model'))

    def test_auto_search_disabled_keeps_model_term_local(self):
        self.wb.permissions(self.owner, self.mid, MeetingPermissions(model=True, search=True))
        self.batch('先核内部口径。', dict(evidence_need='internal'))
        q, = self.questions()
        self.batch('核公开成本数据。', self.mention(q, 'public', '公开成本'))
        self.assertIsNone(self.wb.claim(self.agent))
        q, = self.questions()
        self.assertEqual((q['state'], q['public_query']), ('needs_query', ''))

    def test_restart_recovers_legacy_mixed_question_without_replay(self):
        self.batch('核对公开成本。', dict(evidence_need='mixed', external_fact='公开成本', public_query=''))
        q, = self.questions()
        with self.wb.store.transaction() as db:
            self.wb.store._put(db, 'wb_question', q['id'], {**q, 'state': 'queued', 'stage': '等待研究'})
        self.wb.recover(publish=False)
        q, = self.questions()
        self.assertEqual(q['state'], 'needs_query')
        self.assertIsNone(self.wb.claim(self.agent))

    def test_first_internal_answer_uses_existing_final_turn_and_tracks_its_revision(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text='目前只验证了创建，没有运行真实负载。', segment_id='answer'))
        self.batch('此前做过真实负载测试吗？', dict(evidence_need='internal'))
        work = self.wb.claim(self.agent)
        search = Mock()
        def answer(_prompt, content, **_kwargs):
            proof = next(row for row in content['utterances'] if '没有运行真实负载' in row['quote'])
            return {'status': 'answered', 'short_answer': '会上确认，只验证了创建。',
                    'evidence_ids': [proof['id']]}
        self.model.call = Mock(side_effect=answer)
        result = Researcher(search, self.model).run(work['meeting'], work['question'], lambda _: None)
        saved = self.wb.finish(self.agent, work, result)
        self.assertEqual(saved['state'], 'completed')
        self.assertEqual(saved['result']['meeting_evidence'][0]['quote'], '目前只验证了创建，没有运行真实负载。')
        search.search.assert_not_called()
        self.model.call.assert_called_once()
        self.wb.utterance(self.owner, self.mid, Utterance(text='更正，测试时使用了模拟负载。', segment_id='answer', revision=2))
        self.assertTrue(self.questions()[0]['basis_stale'])

    def test_later_evidence_updates_original_question_and_enters_summary(self):
        self.batch('要怎么查出真实使用比例？', dict(evidence_need='internal'))
        claimed = self.wb.claim(self.agent)
        question_id = claimed['question']['id']
        self.model.call = Mock(return_value={'status': 'missing', 'short_answer': '比例还未核实。', 'gap': '缺少高峰时段日志。'})
        engine = Researcher(None, self.model)
        result = engine.run(claimed['meeting'], claimed['question'], lambda _: None)
        q = self.wb.finish(self.agent, claimed, result)
        self.assertEqual(q['state'], 'insufficient')
        action = dict(self.mention(q, 'internal'), recheck_meeting=True)
        evidence = '先取高峰期一小时的访问日志，再统计真实使用比例。'
        self.batch(evidence, action)
        self.assertEqual(len(self.questions()), 1)
        revised = self.wb.claim(self.agent)
        self.assertEqual(revised['question']['id'], question_id)
        self.assertEqual(revised['question']['anchor_ref'], claimed['question']['anchor_ref'])
        def answer(_prompt, content, **_kwargs):
            proof = next(row for row in content['utterances'] if row['quote'] == evidence)
            return {'status': 'follow_up', 'short_answer': '取高峰期一小时的日志。', 'evidence_ids': [proof['id']]}
        self.model.call = Mock(side_effect=answer)
        result = engine.run(revised['meeting'], revised['question'], lambda _: None)
        self.wb.finish(self.agent, revised, result)
        self.wb.control(self.owner, self.mid, 'end')
        work = self.wb.summary_work(self.agent)
        self.assertEqual(work['follow_ups'][0]['text'], result['answer'])
        self.assertEqual(len(work['follow_ups'][0]['source_refs']), 1)
