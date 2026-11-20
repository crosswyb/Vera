"""Exercise the same API, publisher and worker as a meeting with synthetic recipients."""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from live.source_reader import DemoResearcher
from live.workbench_contracts import NewMeeting, NewQuestion
from live_api import create_app


class SyntheticFeishu:
    def __init__(self):
        self.created = []
        self.sent = []
        self.delivered = threading.Event()

    def status(self, verify=True):
        return dict(available=True, version='1.0.100', configured=True, connected=True,
                    account_key='fixture-user', account_name='合成用户', message='已授权')

    def validate_folder(self, value):
        return {'token': 'fldcnFixture', 'name': '合成资料'}

    def validate_chat(self, chat_id):
        return {'id': chat_id, 'name': '合成通知群'}

    def search_chats(self, query):
        return [self.validate_chat('oc_00000000000000000000000000000000')]

    def create_document(self, folder_token, title, markdown):
        self.created.append((folder_token, title, markdown))
        return {'document_id': 'SyntheticDoc', 'url': 'https://example.feishu.cn/docx/SyntheticDoc'}

    def send_message(self, chat_id, text, idempotency_key):
        self.sent.append((chat_id, text, idempotency_key))
        self.delivered.set()
        return {'message_id': 'om_synthetic'}


class FeishuMeetingIntegration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.cli = SyntheticFeishu()
        self.app = create_app(root / 'workspace.sqlite3', {'TESTING': True, 'WORKBENCH_ROOT': root / 'meetings',
                              'WORKBENCH_FEISHU_CLIENT': self.cli,
                              'WORKBENCH_CAPABILITIES': {'model_enabled': True, 'search_enabled': True}})
        self.client = self.app.test_client()
        self.wb = self.app.extensions['workbench']
        users = self.wb.store.seed(fixture=True)['members']
        self.owner = self.wb.store.authenticate(users[0]['token'])
        self.agent = self.wb.store.authenticate(users[-1]['token'])
        self.headers = {'Authorization': 'Bearer ' + users[0]['token']}
        self.member_headers = {'Authorization': 'Bearer ' + users[1]['token']}
        self.base = '/live/v2/feishu/'

    def configure(self):
        ss = self.client.get(self.base + 'settings', headers=self.headers).json['settings']
        self.assertFalse(ss['enabled'])
        group = self.client.post(self.base + 'chats', headers=self.headers, json={'query': '合成'}).json['items'][0]
        response = self.client.put(self.base + 'settings', headers=self.headers, json={
            'revision': ss['revision'], 'enabled': True, 'folder': 'fldcnFixture',
            'chat_id': group['id'], 'chat_name': '不采信客户端名称'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['settings']['chat_name'], '合成通知群')
        return response.json['settings']

    def test_local_saved_research_is_delivered_during_meeting(self):
        self.configure()
        worker = self.app.extensions['feishu_worker']
        worker.start()
        self.addCleanup(worker.close)
        meeting = self.wb.create(self.owner, NewMeeting(title='合成飞书验收', data_scope='synthetic'))
        self.wb.question(self.owner, meeting['id'], NewQuestion(title='合成研究问题', public_query='synthetic workflow'))
        work = self.wb.claim(self.agent)
        result = DemoResearcher().run(dict(work['meeting'], mode='demo'), work['question'], lambda _: None)
        question = self.wb.finish(self.agent, work, result)
        self.assertTrue(question['artifact_id'])
        self.assertTrue(self.cli.delivered.wait(8), '保存后的文档没有送达独立工作线程')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            jobs = self.client.get(self.base + 'settings', headers=self.headers).json['jobs']
            if jobs and jobs[0]['state'] == 'sent':
                break
            time.sleep(.05)
        self.assertEqual(jobs[0]['state'], 'sent')
        self.assertEqual(len(self.cli.created), 1)
        self.assertIn(result['brief']['short_answer'], self.cli.created[0][2])
        self.assertIn(jobs[0]['url'], self.cli.sent[0][1])
        self.assertNotEqual(self.wb.snapshot(self.owner, meeting['id'])['meeting']['state'], 'ended')

    def test_routes_require_owner_and_valid_origin(self):
        for route in ('settings', 'status'):
            self.assertEqual(self.client.get(self.base + route).status_code, 401)
            self.assertEqual(self.client.get(self.base + route, headers=self.member_headers).status_code, 403)
        for route in ('test', 'folder', 'auth/start', 'auth/poll', 'chats', 'jobs/abc/retry'):
            self.assertEqual(self.client.post(self.base + route, headers=self.member_headers, json={}).status_code, 403)
        self.assertEqual(self.client.post(self.base + 'test', headers={**self.headers, 'Origin': 'https://example.org'}, json={}).status_code, 403)
        self.assertEqual(self.cli.created, [])
