"""An update must not terminate active meetings, requests or unsaved work."""

import tempfile
import unittest
from pathlib import Path

from live.workbench_contracts import NewMeeting, NewQuestion, QuestionAction
from live_api import create_app


class UpdateReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        app = create_app(root / 'state.sqlite3', {'TESTING': True,
            'WORKBENCH_ROOT': root / 'meetings',
            'WORKBENCH_CAPABILITIES': {'model_enabled': True, 'search_enabled': True}})
        self.wb = app.extensions['workbench']
        seed = self.wb.store.seed(fixture=True)
        self.owner = self.wb.store.authenticate(seed['members'][0]['token'])

    def ready(self):
        return self.wb.home(self.owner)['update_readiness']

    def test_active_meeting_blocks_install_and_ended_idle_meeting_allows_it(self):
        self.assertTrue(self.ready()['can_install'])
        meeting = self.wb.create(self.owner, NewMeeting(title='合成更新测试会议'))
        self.assertFalse(self.ready()['can_install'])
        self.assertIn('会议', self.ready()['reason'])
        self.wb.control(self.owner, meeting['id'], 'end')
        self.assertEqual(self.ready(), {'can_install': True, 'reason': ''})

    def test_running_and_queued_research_block_install_even_after_meeting_ends(self):
        meeting = self.wb.create(self.owner, NewMeeting(title='合成更新测试会议'))
        question = self.wb.question(self.owner, meeting['id'], NewQuestion(title='合成查证问题'))
        self.wb.action(self.owner, question['id'], QuestionAction(action='approve', expected_version=1))
        self.wb.control(self.owner, meeting['id'], 'end')
        self.wb.control(self.owner, meeting['id'], 'resume_research')
        self.assertFalse(self.ready()['can_install'])
        self.assertIn('查证', self.ready()['reason'])
        with self.wb.store.transaction() as db:
            value = self.wb.store._get(db, 'wb_question', question['id'])
            value['state'] = 'running'
            self.wb.store._put(db, 'wb_question', question['id'], value)
        self.wb.control(self.owner, meeting['id'], 'pause_research')
        self.assertFalse(self.ready()['can_install'])
        with self.wb.store.transaction() as db:
            value = self.wb.store._get(db, 'wb_question', question['id'])
            value['state'] = 'failed'
            self.wb.store._put(db, 'wb_question', question['id'], value)
        self.assertTrue(self.ready()['can_install'])

    def test_summary_and_unsaved_documents_block_install(self):
        meeting = self.wb.create(self.owner, NewMeeting(title='合成更新测试会议'))
        self.wb.control(self.owner, meeting['id'], 'end')
        with self.wb.store.transaction() as db:
            value = self.wb.store._get(db, 'wb_meeting', meeting['id'])
            value['summary'] = {'state': 'running'}
            self.wb.store._put(db, 'wb_meeting', meeting['id'], value)
        self.assertIn('总结', self.ready()['reason'])
        with self.wb.store.transaction() as db:
            value = self.wb.store._get(db, 'wb_meeting', meeting['id'])
            value['summary'] = {'state': 'complete'}
            self.wb.store._put(db, 'wb_meeting', meeting['id'], value)
        self.assertFalse(self.wb._update_readiness([value], [], [{'state': 'save_failed'}])['can_install'])
        self.assertTrue(self.wb._update_readiness([value], [], [{'state': 'saved'}])['can_install'])
