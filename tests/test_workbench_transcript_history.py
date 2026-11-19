import tempfile
import unittest
from pathlib import Path

from live.workbench_contracts import AudioEvent, NewMeeting
from live_api import create_app


class TranscriptHistoryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        app = create_app(root / 'state.db', {'TESTING': True, 'WORKBENCH_ROOT': root / 'out'})
        self.client = app.test_client()
        self.store = app.extensions['live_store']
        self.wb = app.extensions['workbench']
        users = self.store.seed(fixture=True)['members']
        self.owner, _, _, self.agent = [self.store.authenticate(u['token']) for u in users]
        self.mid = self.wb.create(self.owner, NewMeeting(title='合成历史', data_scope='synthetic'))['id']
        self.uri = '/live/v2/meetings/' + self.mid
        self.headers = {'Authorization': 'Bearer ' + users[0]['token']}

    def ingest(self, n, segment, revision=1, kind='final'):
        self.wb.audio(self.agent, self.mid, AudioEvent(meeting_id=self.mid, capture_epoch='demo',
            stream_id='mic', seq=n, source='microphone', type='transcript.' + kind,
            segment_id=str(segment), revision=revision, occurred_at='2026-11-18T00:00:00Z',
            audio_start_ms=n, audio_end_ms=n+1, text='' if kind == 'retracted' else '本机合成发言'))

    def test_page_to_start_without_gaps_then_retract_an_old_final(self):
        for n in range(1, 246):
            self.ingest(n, n)
        snap = self.client.get(self.uri + '/snapshot', headers=self.headers).get_json()
        head, page = snap['segments'], snap['transcript_page']
        self.assertEqual((len(head), page['has_more'], page['revision_epoch']), (120, True, 0))
        ids = [s['id'] for s in head]
        for length in (120, 5):
            page = self.client.get(self.uri + '/segments?before=' + str(page['next_before']), headers=self.headers).get_json()
            self.assertEqual(len(page['segments']), length)
            ids = [s['id'] for s in page['segments']] + ids
        self.assertEqual(ids, ['demo:mic:' + str(n) for n in range(1, 246)])
        self.assertFalse(page['has_more'])
        self.assertIsNone(page['next_before'])
        after_read = self.client.get(self.uri + '/snapshot', headers=self.headers).get_json()
        self.assertEqual((snap['meeting']['seq'], snap['questions']), (after_read['meeting']['seq'], after_read['questions']))
        self.ingest(246, 3, 2, 'retracted')
        self.ingest(247, 4, 2, 'final')
        after_edit = self.client.get(self.uri + '/snapshot', headers=self.headers).get_json()
        self.assertEqual(after_edit['transcript_page']['revision_epoch'], 2)
        self.assertEqual(after_edit['transcript_page']['changes'], [
            {'epoch': 1, 'segment_id': 'demo:mic:3', 'revision': 2},
            {'epoch': 2, 'segment_id': 'demo:mic:4', 'revision': 2},
        ])
        self.assertEqual(after_edit['segments'][-1]['id'], 'demo:mic:4')
        older = self.client.get(self.uri + '/segments?before=15', headers=self.headers).get_json()
        self.assertEqual(older['revision_epoch'], 2)
        self.assertFalse({'demo:mic:3', 'demo:mic:4'} & {s['id'] for s in older['segments']})

    def test_cursor_is_bounded_and_other_project_cannot_read(self):
        for query in ('', '?before=0', '?before=q', '?before=9223372036854775808'):
            self.assertEqual(self.client.get(self.uri + '/segments' + query, headers=self.headers).status_code, 400)
        path = self.uri + '/segments?before=999'
        self.assertEqual(self.client.get(path).status_code, 401)
        other = self.store.seed()['members'][0]['token']
        self.assertEqual(self.client.get(path, headers={'Authorization': 'Bearer ' + other}).status_code, 403)
