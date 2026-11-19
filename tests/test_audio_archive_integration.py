"""Exercise capture registration, the archive worker, and persisted meeting files."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote
import wave

from live.workbench_audio_codec import AudioCodecError
from live.workbench_contracts import AudioEvent, AudioImport, AudioImportFinish, NewMeeting, RecordingStart
from live.workbench_retention import purge
from live_api import create_app


class AudioArchiveIntegrationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.open_workbench()
        members = self.wb.store.seed(fixture=True)['members']
        self.owner = self.wb.store.authenticate(members[0]['token'])
        self.headers = {'Authorization': 'Bearer ' + members[0]['token']}
        self.agent = self.wb.store.authenticate(members[-1]['token'])
        self.mid = self.wb.create(self.owner, NewMeeting(title='Archive integration'))['id']

    def open_workbench(self):
        with patch.dict(os.environ, {}, clear=True):
            self.app = create_app(self.root / 'state.sqlite3', {
                'TESTING': True, 'WORKBENCH_ROOT': self.root / 'meetings',
                'WORKBENCH_SETTINGS_PATH': self.root / 'private' / 'providers.json',
                'WORKBENCH_CAPABILITIES': {'audio_enabled': True},
            })
        self.wb = self.app.extensions['workbench']

    def begin_capture(self):
        start = self.wb.start_recording(self.agent, self.mid, RecordingStart(recording_id='e1', sources=['microphone']))
        recording = self.meeting()['recordings'][0]
        path = self.wb.writer.path(recording['path'])
        self.assertEqual(path.parent, Path(start['recording_directory']))
        self.assertEqual(path.suffix, '.caf')
        return path

    def saved(self, mid, epoch='e1', source='microphone'):
        event = AudioEvent(meeting_id=mid, capture_epoch=epoch, stream_id=source + '-' + epoch, seq=1,
                           type='audio.recording', source=source, occurred_at='2026-11-18T03:00:00Z',
                           state='saved', audio_end_ms=1000)
        self.wb.audio(self.agent, mid, event)

    def finish_capture(self):
        self.saved(self.mid)
        self.assertIsNone(self.wb.next_audio_archive(), 'active meetings must keep capturing to CAF')
        self.wb.control(self.owner, self.mid, 'end')
        job = self.wb.next_audio_archive()
        self.assertEqual(job, (self.mid, 'e1:microphone'))
        return job

    def meeting(self):
        return self.wb.snapshot(self.owner, self.mid)['meeting']

    def index(self):
        return unquote(self.wb.writer.path(self.meeting()['index_path']).read_text(encoding='utf-8'))

    def keep_original(self, keep):
        settings = self.app.extensions['workbench_settings'].current.model_dump()
        response = self.app.test_client().put('/live/v2/settings', headers=self.headers,
                                               json={**settings, 'keep_original_caf': keep})
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.get_json()['keep_original_caf'], keep)

    def convert_fixture(self, capture):
        job = self.finish_capture()

        def encode(src, dest):
            self.assertEqual(Path(src).read_bytes(), capture.read_bytes())
            Path(dest).write_bytes(b'encoded fixture')

        with patch('live.recording_archive.encode_caf', side_effect=encode) as encoder:
            self.wb.archive_recording(*job)
            encoder.assert_called_once()
        r = self.meeting()['recordings'][0]
        primary = self.wb.writer.path(r['path'])
        self.assertEqual(primary.suffix, '.m4a')
        self.assertEqual(primary.read_bytes(), b'encoded fixture')
        self.assertEqual(r['state'], 'saved')
        self.assertIn('(' + primary.name + ')', self.index())
        self.assertIsNone(self.wb.next_audio_archive())
        return primary

    def test_archived_m4a_replaces_caf_in_storage_and_index_by_default(self):
        capture = self.begin_capture()
        capture.write_bytes(b'new live capture')
        self.convert_fixture(capture)
        self.assertFalse(capture.exists())
        self.assertNotIn('(' + capture.name + ')', self.index())

    def test_end_waits_for_late_native_save_before_scheduling(self):
        capture = self.begin_capture()
        capture.write_bytes(b'still writing')
        self.wb.control(self.owner, self.mid, 'end')
        self.assertIsNone(self.wb.next_audio_archive())
        self.saved(self.mid)
        self.assertEqual(self.wb.next_audio_archive(), (self.mid, 'e1:microphone'))

    def test_keep_preference_at_capture_start_survives_change_and_files_never_expire(self):
        self.keep_original(True)
        capture = self.begin_capture()
        capture.write_bytes(b'keep this original')
        self.keep_original(False)
        primary = self.convert_fixture(capture)
        m = self.meeting()
        self.assertEqual(self.wb.writer.path(m['recordings'][0]['raw_path']), capture)
        self.assertEqual(capture.read_bytes(), b'keep this original')
        foreign = capture.parent / 'personal.caf'
        foreign.write_bytes(b'user file')
        result = purge(self.wb, now=m['created_at'] + 8 * 86400, apply=True)
        self.assertEqual(result['expired_meetings'], [])
        self.assertTrue(capture.exists())
        self.assertTrue(primary.exists())
        self.assertEqual(foreign.read_bytes(), b'user file')

    def test_encoder_failure_keeps_caf_readable_and_indexed_without_automatic_retries(self):
        capture = self.begin_capture()
        capture.write_bytes(b'original after failure')
        job = self.finish_capture()
        with patch('live.recording_archive.encode_caf', side_effect=AudioCodecError('fixture failure')):
            self.wb.archive_recording(*job)
        r = self.meeting()['recordings'][0]
        self.assertEqual((r['archive_state'], r['state']), ('failed', 'saved'))
        self.assertEqual(self.wb.writer.path(r['path']), capture)
        self.assertEqual(capture.read_bytes(), b'original after failure')
        self.assertIn('(' + capture.name + ')', self.index())
        self.assertIsNone(self.wb.next_audio_archive())

    def test_restart_after_m4a_commit_finishes_cleanup_without_reencoding(self):
        capture = self.begin_capture()
        capture.write_bytes(b'original for recovery')
        job = self.finish_capture()
        unlink = Path.unlink

        def interrupt_before_unlink(path, *args, **kwargs):
            if path == capture:
                raise SystemExit('interrupt before removing CAF')
            return unlink(path, *args, **kwargs)

        with patch('live.recording_archive.encode_caf', side_effect=lambda src, dst: dst.write_bytes(b'archived')) as encode:
            with patch.object(Path, 'unlink', interrupt_before_unlink), self.assertRaisesRegex(SystemExit, 'removing CAF'):
                self.wb.archive_recording(*job)
            self.open_workbench()
            self.wb.recover()
            r = self.meeting()['recordings'][0]
            primary = self.wb.writer.path(r['path'])
            self.assertEqual(primary.suffix, '.m4a')
            self.assertEqual(primary.read_bytes(), b'archived')
            self.assertEqual(r['archive_state'], 'cleanup')
            self.assertEqual(capture.read_bytes(), b'original for recovery')
            pending = self.wb.next_audio_archive()
            self.assertEqual(pending, job)
            self.wb.archive_recording(*pending)
            encode.assert_called_once()
        r = self.meeting()['recordings'][0]
        self.assertEqual(r['archive_state'], 'saved')
        self.assertNotIn('raw_path', r)
        self.assertFalse(capture.exists())
        self.assertEqual(primary.read_bytes(), b'archived')
        self.assertIn('(' + primary.name + ')', self.index())
        self.assertIsNone(self.wb.next_audio_archive())

    def test_conflicting_m4a_is_untouched_and_owner_can_retry_after_moving_it(self):
        capture = self.begin_capture()
        capture.write_bytes(b'original awaiting archive')
        job = self.finish_capture()
        primary = capture.with_suffix('.m4a')
        primary.write_bytes(b'user-owned file')
        with patch('live.recording_archive.encode_caf', side_effect=lambda src, dst: dst.write_bytes(b'new archive')):
            self.wb.archive_recording(*job)
            r = self.meeting()['recordings'][0]
            self.assertEqual((r['archive_state'], self.wb.writer.path(r['path'])), ('failed', capture))
            self.assertEqual(primary.read_bytes(), b'user-owned file')
            self.assertEqual(capture.read_bytes(), b'original awaiting archive')
            self.assertIn('(' + capture.name + ')', self.index())
            self.assertIsNone(self.wb.next_audio_archive())

            moved = primary.rename(primary.with_name('personal.m4a'))
            self.wb.retry_archive(self.owner, self.mid)
            pending = self.wb.next_audio_archive()
            self.assertEqual(pending, job)
            self.wb.archive_recording(*pending)
        r = self.meeting()['recordings'][0]
        self.assertEqual((r['archive_state'], self.wb.writer.path(r['path'])), ('saved', primary))
        self.assertEqual(primary.read_bytes(), b'new archive')
        self.assertEqual(moved.read_bytes(), b'user-owned file')
        self.assertFalse(capture.exists())
        self.assertIn('(' + primary.name + ')', self.index())
        self.assertIsNone(self.wb.next_audio_archive())

    def test_preexisting_live_caf_and_imported_caf_are_never_queued(self):
        # This is the exact pre-archive recording shape in the persisted store.
        with self.wb.store.transaction() as db:
            m = self.wb.store._get(db, 'wb_meeting', self.mid)
            path = m['directory'] + '/01-原始音频-microphone-old.caf'
            capture = self.wb.writer.path(path)
            capture.write_bytes(b'historical capture')
            m['recordings'] = [{'id': 'old:microphone', 'epoch': 'old', 'source': 'microphone', 'path': path,
                                'state': 'saved', 'started_at': m['created_at'], 'bytes': capture.stat().st_size,
                                'duration_ms': 1000, 'finished_at': m['created_at'] + 1}]
            self.wb._save(db, m)
        self.wb.control(self.owner, self.mid, 'end')

        mid = self.wb.create(self.owner, NewMeeting(title='CAF import', input_kind='audio_file'))['id']
        data = b'imported original'
        m = self.wb.begin_import(self.agent, mid, AudioImport(import_id='import1', file_name='original.caf',
                                                               duration_ms=1000, bytes=len(data)))
        imported = self.wb.writer.path(m['audio_import']['original_path'])
        imported.write_bytes(data)
        self.saved(mid, 'import1', 'file')
        self.wb.finish_import(self.agent, mid, AudioImportFinish(import_id='import1', state='completed'))
        self.wb.control(self.owner, mid, 'end')
        self.wb.recover()
        self.assertIsNone(self.wb.next_audio_archive())
        self.assertEqual(capture.read_bytes(), b'historical capture')
        self.assertEqual(imported.read_bytes(), data)

    @unittest.skipUnless(Path('/usr/bin/afconvert').is_file(), 'requires macOS Core Audio')
    def test_real_caf_archive_is_playable_after_original_cleanup(self):
        capture = self.begin_capture()
        wav = self.root / 'input.wav'
        with wave.open(str(wav), 'wb') as pcm:
            pcm.setparams((1, 2, 48000, 0, 'NONE', ''))
            pcm.writeframes((b'\x70\x17' * 50 + b'\x90\xe8' * 50) * 480)
        subprocess.run(['/usr/bin/afconvert', '-f', 'caff', '-d', 'LEF32', wav, capture], check=True)
        self.wb.archive_recording(*self.finish_capture())
        primary = self.wb.writer.path(self.meeting()['recordings'][0]['path'])
        self.assertEqual(primary.suffix, '.m4a')
        self.assertFalse(capture.exists())
        decoded = self.root / 'decoded.wav'
        subprocess.run(['/usr/bin/afconvert', '-f', 'WAVE', '-d', 'LEI16', primary, decoded], check=True)
        with wave.open(str(decoded), 'rb') as pcm:
            self.assertEqual((pcm.getnchannels(), pcm.getframerate(), pcm.getnframes()), (1, 48000, 48000))
