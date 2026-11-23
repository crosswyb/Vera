"""Feishu outbox delivery boundaries, using temporary SQLite and synthetic receipts."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from live.feishu_cli import FeishuError
from live.feishu_sync import FeishuSync, JOB
from live.store import LiveError, LiveStore


class DeliveryQueueTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = LiveStore(Path(temp.name) / 'state')
        members = self.store.seed(fixture=True)['members']
        self.owner = self.store.authenticate(members[0]['token'])
        self.member = self.store.authenticate(members[1]['token'])
        self.cli = Mock()
        self.cli.status.return_value = dict(connected=True, account_key='fixture', account_name='合成账号')
        self.cli.validate_folder.side_effect = lambda token: dict(token=token, name='测试文件夹')
        self.cli.validate_chat.side_effect = lambda cid: dict(id=cid, name='测试群')
        self.cli.create_document.return_value = {'document_id':'synthetic', 'url':'https://example.feishu.cn/docx/synthetic'}
        self.cli.send_message.return_value = {'message_id':'om_synthetic'}
        self.sync = FeishuSync(self.store, self.cli)
        self.meeting = dict(id='M', project_id=self.owner['project_id'], state='active', title='合成会议')
        self.question = dict(id='Q', title='合成问题', state='insufficient', version=1, generation=1,
                             result={'brief': {'short_answer':'需要原文才能确定'}})
        self.doc = dict(id='D', state='saved', hash='fixture', question_version=1, question_generation=1, content='合成查证正文。')

    def configure(self, enabled=True, folder='original', chat='oc_synthetic'):
        settings = self.sync.settings(self.owner)
        return self.sync.save(self.owner, dict(revision=settings['revision'], enabled=enabled, folder=folder, chat_id=chat))

    def publish(self):
        with self.store.transaction() as db:
            for kind, value in [('wb_meeting', self.meeting), ('wb_question', self.question), ('wb_document', self.doc)]:
                self.store._put(db, kind, value['id'], value)
            self.sync.enqueue_saved(db, self.meeting, self.question, self.doc)

    def latest(self):
        return self.sync.recent(self.owner)[0]

    def test_disabled_history_and_local_conflicts_never_send(self):
        self.publish()
        self.configure()
        self.assertFalse(self.sync.process_one())
        self.doc['state'] = 'conflict'
        self.publish()
        self.assertFalse(self.sync.process_one())
        self.cli.create_document.assert_not_called()

    def test_saved_result_only_once_after_restart_notification_retry_uses_receipt(self):
        self.configure(); self.publish(); self.publish()
        self.assertEqual(len(self.sync.recent(self.owner)), 1)
        self.sync.process_one()
        first = self.latest()
        self.assertEqual(first['stage'], 'message')
        self.assertEqual(first['state'], 'queued')
        self.sync = FeishuSync(self.store, self.cli)
        self.cli.send_message.side_effect = FeishuError('飞书群未授权', 'permission')
        self.sync.process_one()
        self.assertEqual(self.latest()['state'], 'failed')
        self.assertIn('未授权', self.latest()['error'])
        self.sync.retry(self.owner, first['id'])
        self.cli.send_message.side_effect = None
        self.sync.process_one()
        self.assertEqual(self.latest()['state'], 'sent')
        self.assertIn('依据不足', self.cli.send_message.call_args.args[1])
        self.assertEqual(self.cli.send_message.call_args_list[0], self.cli.send_message.call_args_list[1])
        self.assertEqual(self.cli.create_document.call_count, 1)
        self.assertFalse(self.sync.process_one())

    def test_new_destination_never_redirects_already_saved_document(self):
        self.configure(); self.publish(); self.sync.process_one(); self.configure(folder='different')
        self.assertFalse(self.sync.process_one())
        self.assertEqual(self.latest()['state'], 'cancelled')
        self.cli.send_message.assert_not_called()
        self.configure(False)
        with self.assertRaises(LiveError):
            self.sync.enqueue_test(self.owner)

    def rerun(self):
        self.question['generation'] += 1
        self.doc['question_generation'] = self.question['generation']
        self.doc['hash'] = 'new-' + str(self.question['generation'])
        self.publish()

    def test_repeated_answer_and_sources_ignore_new_retrieval_time(self):
        self.configure()
        source = {'id':'source-1', 'url':'https://example.org', 'retrieved_at':'first'}
        self.question['result']['sources'] = [source]
        self.publish(); self.sync.process_one(); self.sync.process_one()
        receipt = self.latest()
        source['retrieved_at'] = 'second'
        self.rerun()
        self.assertFalse(self.sync.process_one())
        self.assertEqual(self.latest()['message_id'], receipt['message_id'])
        self.assertEqual(self.latest()['id'], receipt['id'])
        self.assertEqual(self.cli.create_document.call_count, 1)
        self.assertEqual(self.cli.send_message.call_count, 1)

    def test_changed_source_creates_revision_linked_only_within_same_destination(self):
        self.configure(); self.publish(); self.sync.process_one(); self.sync.process_one()
        first = self.latest()
        self.question['result']['sources'] = [{'title':'新增来源', 'url':'https://example.org/source'}]
        self.rerun(); self.sync.process_one(); self.sync.process_one()
        _, title, content = self.cli.create_document.call_args.args
        self.assertIn('修订版', title)
        self.assertIn(first['url'], content)
        self.assertIn('修订版', self.cli.send_message.call_args.args[1])
        self.configure(folder='new-destination'); self.rerun(); self.sync.process_one()
        dest, title, content = self.cli.create_document.call_args.args
        self.assertEqual(dest, 'new-destination')
        self.assertNotIn(first['url'], content)
        self.assertNotIn('修订版', title)

    def test_same_answer_during_auth_preflight_preserves_inflight_delivery(self):
        self.configure(); self.publish()
        def rerun_while_authenticating(**kwargs):
            self.rerun()
            return dict(connected=True, account_key='fixture')
        self.cli.status.side_effect = rerun_while_authenticating
        self.sync.process_one()
        self.cli.status.side_effect = None
        self.sync.process_one()
        self.assertEqual(self.latest()['state'], 'sent')
        self.assertEqual(self.cli.create_document.call_count, 1)
        self.assertEqual(self.cli.send_message.call_count, 1)

    def test_revised_and_deleted_documents_do_not_leave_machine(self):
        self.configure(); self.publish()
        with self.store.transaction() as db:
            self.question.update(generation=2)
            self.store._put(db, 'wb_question', 'Q', self.question)
        self.sync.process_one()
        self.assertEqual(self.latest()['state'], 'cancelled')
        self.doc['question_generation'] = 2
        self.publish()
        with self.store.transaction() as db:
            db.execute("DELETE FROM records WHERE kind='wb_meeting' AND id='M'")
        self.sync.process_one()
        self.assertEqual(self.latest()['state'], 'cancelled')
        self.cli.create_document.assert_not_called()

    def test_ambiguous_document_outcome_never_automatically_creates_duplicate(self):
        self.configure(chat=''); self.publish()
        self.cli.create_document.side_effect = FeishuError('可能已生成', 'timeout', True)
        self.sync.process_one()
        self.assertFalse(self.sync.process_one())
        self.assertEqual(self.latest()['state'], 'unknown')
        with self.assertRaises(LiveError):
            self.sync.retry(self.owner, self.latest()['id'])
        self.sync.retry(self.owner, self.latest()['id'], confirm_unknown=True)
        self.cli.create_document.side_effect = None
        self.sync.process_one()
        self.assertEqual(self.latest()['state'], 'sent')
        self.cli.send_message.assert_not_called()

    def test_real_delivery_rechecks_restriction_added_after_queueing(self):
        self.configure()
        self.meeting['data_scope'] = 'real'
        self.question.update(evidence_need='public', state='completed', public_query='公开技术文档',
                             source_refs=[{'segment_id': 'basis', 'revision': 1}])
        self.question['result'].update(evidence_status='supported', claims=[{'text': '合成公开资料'}])
        with self.store.transaction() as db:
            self.store._put(db, 'wb_segment', 'basis', dict(id='basis', meeting_id='M', type='transcript.final',
                            revision=1, event_seq=2, text='请核对这项技术的公开文档。'))
        self.publish()
        self.assertEqual(self.latest()['state'], 'queued')
        with self.store.transaction() as db:
            self.store._put(db, 'wb_segment', 'limit', dict(id='limit', meeting_id='M', type='transcript.final',
                            revision=1, event_seq=3, text='这个事项不要对外传播。'))
        self.assertTrue(self.sync.process_one())
        self.assertEqual(self.latest()['state'], 'cancelled')
        self.cli.create_document.assert_not_called()

    def test_crash_after_sending_without_receipt_is_unknown(self):
        self.configure(); job = self.sync.enqueue_test(self.owner)
        self.sync._claim()
        with self.store.transaction() as db:
            db.execute("UPDATE records SET data=json_set(data,'$.updated_at',?) WHERE kind=? AND id=?", (time.time()-700, JOB, job['id']))
        self.sync = FeishuSync(self.store, self.cli)
        self.assertTrue(self.sync.process_one())
        self.assertEqual(self.latest()['state'], 'unknown')
        self.cli.create_document.assert_not_called()

    def test_identity_change_and_member_actions_fail_closed(self):
        self.configure(); self.publish()
        for action in (lambda:self.sync.settings(self.member), lambda:self.sync.recent(self.member), lambda:self.sync.enqueue_test(self.member)):
            with self.assertRaises(LiveError):
                action()
        self.cli.status.return_value.update(account_key='different')
        self.sync.process_one()
        self.assertEqual(self.latest()['state'], 'failed')
        self.cli.create_document.assert_not_called()

    def test_slow_creation_does_not_hold_meeting_write_lock_or_duplicate(self):
        self.configure(); self.publish()
        entered, release = threading.Event(), threading.Event()
        receipt = self.cli.create_document.return_value
        def slow(*_):
            entered.set()
            release.wait(5)
            return receipt
        self.cli.create_document.side_effect = slow
        runner = threading.Thread(target=self.sync.process_one)
        runner.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertFalse(self.sync.process_one())
            # The settings transaction can commit even while the remote write is blocked.
            self.configure(False)
        finally:
            release.set(); runner.join(5)
        self.assertEqual(self.latest()['state'], 'cancelled')
        self.assertTrue(self.latest()['url'])
        self.cli.send_message.assert_not_called()


if __name__ == '__main__':
    unittest.main()
