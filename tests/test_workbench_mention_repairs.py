import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

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
