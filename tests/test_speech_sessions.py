"""Exercise cloud scope and capture completion through the local backend."""

import json
import os
import sqlite3
import tempfile
import unittest

from live.speech_settings import TEST_SAMPLE_VERSION
from pathlib import Path
from unittest.mock import patch

from live.workbench_contracts import AudioEvent, AudioImport, AudioImportFinish, AudioImportResume, NewMeeting, SpeechAuthorization
from live_api import create_app


class SpeechSessionTest(unittest.TestCase):
    dash_key = 'fixture-dashscope-key'
    deepseek_key = 'fixture-deepseek-key'
    owner_headers = {'Authorization': 'Bearer fixture-owner-0'}
    now = '2026-11-21T02:00:00Z'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        settings_path = self.root / 'providers.json'
        verified = {'text': '语音转写，完整识别', 'duration_ms': 1200, 'tested_at': 1.0,
                    'sample_version': TEST_SAMPLE_VERSION}
        settings_path.write_text(json.dumps({'revision': 1, 'model': {}, 'search': {}, 'speech': {
            'live_provider': 'dashscope', 'file_provider': 'deepseek',
            'providers': {
                'dashscope': {'base_url': 'https://dashscope.aliyuncs.com', 'api_key': self.dash_key},
                'deepseek': {'api_key': self.deepseek_key},
            },
            'verification': {'dashscope': {'live': verified, 'file': verified}, 'deepseek': {'file': verified}},
        }}))
        with patch.dict(os.environ, {}, clear=True):
            app = create_app(self.root / 'state.sqlite3', {
                'TESTING': True, 'WORKBENCH_SETTINGS_PATH': settings_path,
                'WORKBENCH_ROOT': self.root / 'meetings', 'WORKBENCH_CAPABILITIES': {'audio_enabled': True},
                'WORKBENCH_SPEECH_TOKEN': 'fixture-speech-token',
            })
        self.wb = app.extensions['workbench']
        store = self.wb.store
        members = store.seed(fixture=True)['members']
        self.owner = store.authenticate(members[0]['token'])
        self.agent = store.authenticate(members[-1]['token'])
        self.agent_headers = {'Authorization': 'Bearer ' + members[-1]['token']}
        self.native_headers = {**self.agent_headers, 'X-ZSJH-Speech-Token': 'fixture-speech-token'}
        self.client = app.test_client()
        self.meeting = self.wb.create(self.owner, NewMeeting(title='语音转写测试'))
        self.mid = self.meeting['id']

    def url(self, action, internal=False):
        return f"{'/internal' if internal else ''}/live/v2/meetings/{self.mid}/{action}"

    def authorize(self, provider='dashscope', kind='live', sources=('system',), **changes):
        return self.client.put(self.url('speech-authorization'), headers=self.owner_headers, json={
            'provider': provider, 'kind': kind, 'sources': list(sources),
            'revision': self.wb.provider_settings.current.revision, **changes,
        })

    def capture(self, epoch='run-1', provider='dashscope', sources=('system',), headers=None):
        return self.client.post(self.url('recordings', internal=True), headers=headers or self.native_headers, json={
            'provider': provider, 'recording_id': epoch, 'sources': list(sources),
            'settings_revision': self.wb.provider_settings.current.revision,
        })

    def session(self, epoch='run-1', kind='live', sources=('system',), renew=False, headers=None):
        return self.client.post(self.url('speech-session/lease' if renew else 'speech-session'),
                                headers=headers or self.native_headers,
                                json={'kind': kind, 'task_id': epoch, 'sources': list(sources)})

    def send(self, event):
        return self.client.post(self.url('audio-events', internal=True), headers=self.agent_headers, json=event)

    def event(self, event_type, epoch='run-1', source='system', seq=1, **fields):
        return {'meeting_id': self.mid, 'capture_epoch': epoch, 'source': source, 'stream_id': source,
                'seq': seq, 'occurred_at': self.now, 'type': event_type, **fields}

    def seal(self, epoch='run-1', saved=True, no_audio=False):
        return self.client.post(self.url('capture/finish'), headers=self.native_headers,
                                json={'capture_epoch': epoch, 'saved': saved, 'no_audio': no_audio})

    def end(self):
        return self.client.post(self.url('controls'), json={'action': 'end'}, headers=self.owner_headers)

    def snapshot(self):
        return self.wb.snapshot(self.owner, self.mid)

    def confirm_recording(self, source, epoch='run-1', seq=1):
        recording = next(r for r in self.snapshot()['meeting']['recordings']
                         if r['source'] == source and r['epoch'] == epoch)
        path = self.wb.writer_for(self.meeting).path(recording['path'])
        path.write_bytes(b'fixture closed recording')
        response = self.send(self.event('audio.recording', epoch=epoch, source=source, seq=seq,
                                        state='saved', frames_received=16_000, audio_end_ms=1000))
        self.assertEqual(response.status_code, 200, response.get_json())
        return path


    def _failed_file_import(self, provider='deepseek'):
        meeting = self.wb.create(self.owner, NewMeeting(title='可恢复文件', input_kind='audio_file'))
        mid = meeting['id']
        revision = self.wb.provider_settings.current.revision
        self.wb.speech_authorize(self.owner, mid, SpeechAuthorization(
            provider=provider, kind='file', sources=['file'], revision=revision))
        job = AudioImport(import_id='resume-file', file_name='会议.m4a', duration_ms=120000,
                          bytes=4096, provider=provider, settings_revision=revision)
        self.wb.begin_import(self.agent, mid, job)
        current = self.wb.snapshot(self.owner, mid)['meeting']
        path = self.wb.writer_for(current).path(current['audio_import']['original_path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'original audio')
        agent = self.agent
        self.wb.audio(agent, mid, AudioEvent(
            meeting_id=mid, capture_epoch='resume-file', stream_id='archive-resume-file', seq=1,
            type='audio.recording', source='file', occurred_at=self.now, state='saved',
            audio_end_ms=120000, frames_received=1920000))
        self.wb.audio(agent, mid, AudioEvent(
            meeting_id=mid, capture_epoch='resume-file', stream_id='file-resume-file', seq=1,
            type='transcript.final', source='file', occurred_at=self.now, segment_id='chunk-0-0',
            revision=1, text='已确认的合成转写。', audio_start_ms=0, audio_end_ms=60000))
        self.wb.audio(agent, mid, AudioEvent(
            meeting_id=mid, capture_epoch='resume-file', stream_id='file-resume-file', seq=2,
            type='import.progress', source='file', occurred_at=self.now, audio_end_ms=60000))
        self.wb.finish_import(agent, mid, AudioImportFinish(import_id='resume-file', state='failed', error='网络中断'))
        return mid, agent, path

    def test_file_import_resume_refreshes_authorization_and_cleans_checkpoint(self):
        mid, agent, _ = self._failed_file_import()
        settings = self.wb.provider_settings.current
        self.wb.provider_settings.commit(settings.model_copy(update={'revision': settings.revision + 1}))
        current_revision = self.wb.provider_settings.current.revision
        self.wb.speech_authorize(self.owner, mid, SpeechAuthorization(revoke=True))
        self.wb.speech_authorize(self.owner, mid, SpeechAuthorization(
            provider='deepseek', kind='file', sources=['file'], revision=current_revision))
        resumed = self.wb.resume_import(agent, mid, AudioImportResume(import_id='resume-file'))
        job = resumed['audio_import']
        self.assertEqual(job['state'], 'processing')
        self.assertEqual(job['processed_ms'], 60000)
        self.assertEqual(job['resume_from_ms'], 60000)
        self.assertEqual(job['resume_count'], 1)
        self.assertEqual(job['settings_revision'], current_revision)
        self.assertEqual(job['authorization_version'], 3)
        self.wb.audio(agent, mid, AudioEvent(
            meeting_id=mid, capture_epoch='resume-file', stream_id='file-resume-file', seq=3,
            type='import.progress', source='file', occurred_at=self.now, audio_end_ms=120000))
        completed = self.wb.finish_import(agent, mid, AudioImportFinish(import_id='resume-file', state='completed'))
        self.assertEqual(completed['audio_import']['state'], 'completed')
        self.assertNotIn('failed_at_ms', completed['audio_import'])
        self.assertNotIn('resume_from_ms', completed['audio_import'])

    def test_resumed_native_sequence_continues_after_last_acknowledged_event(self):
        mid, agent, _ = self._failed_file_import()
        # Old imports have persisted transcript events but no sequence checkpoint.
        with self.wb.store.transaction() as db:
            meeting = self.wb.store._get(db, 'wb_meeting', mid)
            meeting['audio_import'].pop('last_event_seq', None)
            self.wb.store._put(db, 'wb_meeting', mid, meeting)
        job = self.wb.resume_import(agent, mid, AudioImportResume(import_id='resume-file'))['audio_import']
        sequence = job.get('last_event_seq', 0) + 1
        self.wb.audio(agent, mid, AudioEvent(
            meeting_id=mid, capture_epoch='resume-file', stream_id='file-resume-file', seq=sequence,
            type='transcript.final', source='file', occurred_at=self.now, segment_id='chunk-60000-0',
            revision=2, text='继续识别的合成转写。', audio_start_ms=60000, audio_end_ms=120000))
        self.assertEqual(self.wb.transcript(self.owner, mid)['segment_count'], 2)

    def test_resume_preserves_partial_transcript_and_saves_complete_version(self):
        mid, agent, _ = self._failed_file_import()
        first = self.wb.snapshot(self.owner, mid)['meeting']['audio_import']['transcript_path']
        writer = self.wb.writer_for(self.wb.snapshot(self.owner, mid)['meeting'])
        partial = writer.path(first).read_bytes()
        self.wb.resume_import(agent, mid, AudioImportResume(import_id='resume-file'))
        self.wb.audio(agent, mid, AudioEvent(
            meeting_id=mid, capture_epoch='resume-file', stream_id='file-resume-file', seq=3,
            type='transcript.final', source='file', occurred_at=self.now, segment_id='chunk-60000-0',
            revision=2, text='恢复后的合成转写。', audio_start_ms=60000, audio_end_ms=120000))
        self.wb.audio(agent, mid, AudioEvent(
            meeting_id=mid, capture_epoch='resume-file', stream_id='file-resume-file', seq=4,
            type='import.progress', source='file', occurred_at=self.now, audio_end_ms=120000))
        result = self.wb.finish_import(agent, mid, AudioImportFinish(import_id='resume-file', state='completed'))
        job = result['audio_import']
        self.assertEqual(job['transcript_state'], 'saved')
        self.assertNotEqual(job['transcript_path'], first)
        self.assertEqual(writer.path(first).read_bytes(), partial)
        self.assertIn('恢复后的合成转写', writer.path(job['transcript_path']).read_text())

    def test_file_import_resume_rejects_local_provider_and_missing_original(self):
        meeting = self.wb.create(self.owner, NewMeeting(title='本机文件', input_kind='audio_file'))
        mid = meeting['id']
        job = AudioImport(import_id='local-file', file_name='会议.m4a', duration_ms=120000,
                          bytes=4096, provider='apple_local')
        agent = self.agent
        self.wb.begin_import(agent, mid, job)
        with self.assertRaises(Exception):
            self.wb.resume_import(agent, mid, AudioImportResume(import_id='local-file'))

    def test_resume_can_finalize_an_import_already_acknowledged_through_eof(self):
        mid, agent, _ = self._failed_file_import()
        self.wb.resume_import(agent, mid, AudioImportResume(import_id='resume-file'))
        self.wb.audio(agent, mid, AudioEvent(
            meeting_id=mid, capture_epoch='resume-file', stream_id='file-resume-file', seq=3,
            type='import.progress', source='file', occurred_at=self.now, audio_end_ms=120000))
        self.wb.finish_import(agent, mid, AudioImportFinish(import_id='resume-file', state='failed', error='收尾请求中断'))
        job = self.wb.resume_import(agent, mid, AudioImportResume(import_id='resume-file'))['audio_import']
        self.assertEqual(job['resume_from_ms'], 120000)
        completed = self.wb.finish_import(agent, mid, AudioImportFinish(import_id='resume-file', state='completed'))
        self.assertEqual(completed['audio_import']['transcript_state'], 'saved')

    def test_scope_checks_native_token_provider_destination_and_sources(self):
        self.assertEqual(self.authorize(provider='deepseek').status_code, 400)
        self.assertEqual(self.capture(provider='deepseek').status_code, 400)
        self.assertEqual(self.capture().status_code, 409)
        allowed = self.authorize()
        self.assertEqual(allowed.status_code, 200, allowed.get_json())
        self.assertEqual(self.capture(sources=('system', 'microphone')).status_code, 409)
        self.assertEqual(self.capture(headers=self.agent_headers).status_code, 403)
        # Move to another official endpoint, then prove the earlier consent
        # cannot be used to upload to it, even when the new endpoint is ready.
        settings = self.wb.provider_settings.current
        speech = settings.speech
        dash = speech.providers.dashscope.model_copy(update={'base_url': 'https://dashscope-intl.aliyuncs.com'})
        speech = speech.model_copy(update={'providers': speech.providers.model_copy(update={'dashscope': dash})})
        self.wb.provider_settings.commit(settings.model_copy(update={'revision': 2, 'speech': speech}))
        self.assertEqual(self.capture().status_code, 409)
        self.assertEqual(self.authorize().status_code, 200)
        self.assertEqual(self.capture().status_code, 200)
        self.assertEqual(self.session(epoch='other').status_code, 409)
        self.assertEqual(self.session(sources=('microphone',)).status_code, 409)
        self.assertEqual(self.session(headers=self.agent_headers).status_code, 403)
        private = self.session()
        self.assertEqual(private.status_code, 200)
        self.assertEqual(private.get_json()['api_key'], self.dash_key)
        self.assertEqual(private.get_json()['endpoint'], 'wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference')
        self.assertNotIn(self.dash_key, json.dumps(self.snapshot()))
        self.assertEqual(self.session().status_code, 409)

    def test_withdrawal_and_lease_expiry_stop_renewal_but_preserve_capture(self):
        self.assertEqual(self.authorize().status_code, 200)
        self.assertEqual(self.capture().status_code, 200)
        with patch('live.speech_workbench.time.time', return_value=100.0):
            first = self.session()
            self.assertEqual(first.status_code, 200)
        with patch('live.speech_workbench.time.time', return_value=102.0):
            self.assertEqual(self.authorize().get_json()['version'], 1)
            renewed = self.session(renew=True)
            self.assertEqual(renewed.get_json(), {'lease_seconds': 30})
        with patch('live.speech_workbench.time.time', return_value=132.1):
            self.assertEqual(self.session(renew=True).status_code, 409)
            self.assertEqual(self.session().status_code, 409)
        # Renewal is also denied on revocation while the lease would still be valid.
        denied = self.client.put(self.url('speech-authorization'), headers=self.owner_headers, json={'revoke': True})
        self.assertEqual(denied.get_json()['state'], 'revoked')
        with patch('live.speech_workbench.time.time', return_value=104.0):
            self.assertEqual(self.session(renew=True).status_code, 409)
        path = self.confirm_recording('system')
        self.assertTrue(path.is_file())
        self.assertEqual(self.seal().get_json()['state'], 'complete')

    def test_other_window_cannot_end_until_both_tracks_and_tail_are_acknowledged(self):
        self.assertEqual(self.authorize(sources=('system', 'microphone')).status_code, 200)
        self.assertEqual(self.capture(sources=('system', 'microphone')).status_code, 200)
        self.assertEqual(self.end().status_code, 409)
        self.assertEqual(self.send(self.event('audio.ready', state='ready')).status_code, 200)
        self.assertEqual(self.send(self.event('asr.error', seq=2, provider='dashscope', code='connection')).status_code, 200)
        self.assertEqual(self.snapshot()['meeting']['capture']['status'], 'ready')
        gap = self.event('asr.gap', seq=3, audio_start_ms=20, audio_end_ms=150, code='connection')
        self.assertEqual(self.send(gap).status_code, 200)
        self.assertEqual(self.snapshot()['meeting']['transcription']['system']['provider'], 'dashscope')
        tail = self.event('transcript.final', source='microphone', provider='apple_local', segment_id='last',
                          revision=1, audio_start_ms=120, audio_end_ms=190, text='最后一句已保存')
        self.assertEqual(self.send(tail).status_code, 200)
        system = self.confirm_recording('system', seq=4)
        self.assertEqual(self.seal().get_json()['state'], 'failed')
        self.assertEqual(self.end().status_code, 409)
        mic = self.confirm_recording('microphone', seq=2)
        # Failed final DB acknowledgement must leave the recorded files and
        # original capture fence intact so replay stays safe.
        with patch.object(self.wb, '_save', side_effect=sqlite3.OperationalError('fixture write failure')):
            self.assertEqual(self.seal().status_code, 500)
        self.assertTrue(system.is_file() and mic.is_file())
        self.assertEqual(self.end().status_code, 409)
        result = self.seal().get_json()
        self.assertEqual(result, {'state': 'with_gaps', 'gaps': [{'source': 'system', 'start_ms': 20,
                                                               'end_ms': 150, 'code': 'connection'}]})
        self.assertEqual(self.send(tail).status_code, 200)
        self.assertEqual(self.snapshot()['segment_count'], 1)
        self.assertEqual(self.end().status_code, 200)
        transcript = self.wb.transcript(self.owner, self.mid)['content']
        self.assertIn('最后一句已保存', transcript)
        self.assertIn('Apple', transcript)

    def test_truncated_recording_cannot_be_confirmed_as_saved(self):
        self.assertEqual(self.capture(provider='apple_local').status_code, 200)
        path = self.confirm_recording('system')
        path.write_bytes(b'truncated')
        self.assertEqual(self.seal().get_json()['state'], 'failed')
        self.assertEqual(self.end().status_code, 409)
        self.assertEqual(path.read_bytes(), b'truncated')

    def test_snapshot_recovers_speech_failure_chain_from_persisted_events(self):
        self.assertEqual(self.authorize().status_code, 200)
        self.assertEqual(self.capture().status_code, 200)
        events = [
            self.event('asr.error', seq=1, provider='dashscope', code='speech_input_overflow',
                       detail='待转写的音频积压，已停止当前识别。'),
            self.event('asr.status', seq=2, provider='apple_local', state='connecting'),
            self.event('asr.error', seq=3, provider='apple_local', code='speech_model_unavailable',
                       detail='Apple 中文识别无法启动。'),
            self.event('asr.status', seq=4, provider='apple_local', state='paused',
                       code='speech_model_unavailable', detail='转写暂停，录音继续。'),
            self.event('asr.gap', seq=5, provider='apple_local', code='speech_model_unavailable',
                       audio_start_ms=100, audio_end_ms=900),
        ]
        for event in events:
            response = self.send(event)
            self.assertEqual(response.status_code, 200, response.get_json())
        self.confirm_recording('system', seq=6)
        self.assertEqual(self.seal().get_json()['state'], 'with_gaps')
        # The stored gaps use the old shape. Diagnostics must work for existing
        # meetings without rewriting their stored data or relying on 40 recent events.
        with self.wb.store.transaction() as db:
            current = self.wb.store._get(db, 'wb_meeting', self.mid)
            for index in range(45):
                self.wb._event(db, current, 'fixture.event', {'index': index})
            self.wb._save(db, current)
        snapshot = self.snapshot()
        self.assertFalse(any(event['type'] == 'asr.error' for event in snapshot['activity']))
        gap, = snapshot['speech_gaps']
        self.assertEqual((gap['capture_epoch'], gap['source'], gap['start_ms'], gap['end_ms']),
                         ('run-1', 'system', 100, 900))
        self.assertEqual([(cause['provider'], cause['code'], cause['detail']) for cause in gap['causes']], [
            ('dashscope', 'speech_input_overflow', events[0]['detail']),
            ('apple_local', 'speech_model_unavailable', events[2]['detail']),
        ])
        stored_gap, = snapshot['meeting']['capture_epochs']['run-1']['gaps']
        self.assertEqual(set(stored_gap), {'source', 'start_ms', 'end_ms', 'code'})

    def test_speech_gap_causes_do_not_cross_sources_or_successful_recovery(self):
        self.assertEqual(self.capture(provider='apple_local', sources=('system', 'microphone')).status_code, 200)
        events = [
            self.event('asr.error', seq=1, provider='apple_local', code='speech_input_failed', detail='电脑音源识别失败'),
            self.event('asr.error', seq=1, source='microphone', provider='apple_local',
                       code='speech_recognition_failed', detail='麦克风识别失败'),
            self.event('asr.gap', seq=2, code='speech_input_failed', audio_start_ms=0, audio_end_ms=100),
            self.event('asr.status', seq=3, provider='apple_local', state='running'),
            self.event('asr.gap', seq=4, code='speech_finalization_interrupted', audio_start_ms=200, audio_end_ms=300),
            self.event('asr.gap', seq=2, source='microphone', code='speech_recognition_failed',
                       audio_start_ms=0, audio_end_ms=300),
        ]
        for event in events:
            self.assertEqual(self.send(event).status_code, 200)
        system, recovered, microphone = self.snapshot()['speech_gaps']
        self.assertEqual([cause['detail'] for cause in system['causes']], ['电脑音源识别失败'])
        self.assertEqual(recovered['causes'], [])
        self.assertEqual([cause['detail'] for cause in microphone['causes']], ['麦克风识别失败'])

    def test_incomplete_journal_with_verified_recording_finishes_with_explicit_gap(self):
        self.assertEqual(self.capture(provider='apple_local').status_code, 200)
        path = self.confirm_recording('system')
        result = self.seal(saved=False).get_json()
        self.assertEqual(result['state'], 'with_gaps')
        self.assertEqual([gap['code'] for gap in result['gaps']], ['delivery_incomplete'])
        self.assertEqual(self.seal(saved=False).get_json(), result)
        self.assertEqual(self.end().status_code, 200)
        self.assertTrue(path.is_file())

    def test_incomplete_journal_does_not_bypass_unclosed_or_changed_recording(self):
        self.assertEqual(self.capture(provider='apple_local', sources=('system', 'microphone')).status_code, 200)
        system = self.confirm_recording('system')
        self.assertEqual(self.seal(saved=False).get_json()['state'], 'failed')
        self.assertEqual(self.end().status_code, 409)
        self.confirm_recording('microphone')
        system.write_bytes(b'changed after close')
        self.assertEqual(self.seal(saved=False).get_json()['state'], 'failed')
        self.assertEqual(self.end().status_code, 409)
        system.unlink()
        self.assertEqual(self.seal(saved=False).get_json()['state'], 'failed')
        self.assertEqual(self.end().status_code, 409)

    def test_incomplete_journal_accepts_only_explicitly_closed_empty_tracks(self):
        self.assertEqual(self.capture(provider='apple_local', sources=('system', 'microphone')).status_code, 200)
        self.assertEqual(self.seal(saved=False).get_json()['state'], 'failed')
        for source in ('system', 'microphone'):
            self.assertEqual(self.send(self.event('audio.recording', source=source, state='missing',
                                                 frames_received=0, audio_end_ms=0)).status_code, 200)
        self.assertEqual(self.seal(saved=False).get_json()['state'], 'no_audio')
        self.assertEqual(self.end().status_code, 200)

    def test_outbox_replay_after_acknowledgement_loss_is_idempotent(self):
        self.assertEqual(self.capture(provider='apple_local').status_code, 200)
        gap = self.event('asr.gap', code='connection', audio_start_ms=0, audio_end_ms=100)
        self.assertEqual(self.send(gap).status_code, 200)
        self.confirm_recording('system', seq=2)
        self.assertEqual(self.seal().get_json()['state'], 'with_gaps')
        self.assertEqual(self.send(gap).status_code, 200)
        self.assertEqual(len(self.snapshot()['meeting']['capture_epochs']['run-1']['gaps']), 1)
        conflicting = {**gap, 'code': 'another-error'}
        self.assertEqual(self.send(conflicting).status_code, 409)

    def test_explicit_empty_tracks_cannot_erase_observed_audio(self):
        self.assertEqual(self.capture(provider='apple_local').status_code, 200)
        final = self.event('transcript.final', segment_id='heard', revision=1,
                           audio_start_ms=0, audio_end_ms=100, text='确认听到的合成内容')
        self.assertEqual(self.send(final).status_code, 200)
        self.assertEqual(self.send(self.event('audio.recording', seq=2, state='missing',
                                             frames_received=0, audio_end_ms=0)).status_code, 200)
        self.assertEqual(self.seal(saved=False).get_json()['state'], 'failed')
        self.assertEqual(self.end().status_code, 409)

    def test_failed_recording_acknowledges_error_without_allowing_end(self):
        self.assertEqual(self.capture(provider='apple_local', sources=('system', 'microphone')).status_code, 200)
        path = self.confirm_recording('system')
        failure = self.event('audio.recording', source='microphone', state='failed', frames_received=0,
                             code='recording_failed', detail='录音写入失败，无法确认保存。')
        self.assertEqual(self.send(failure).status_code, 200)
        self.assertEqual(self.send(failure).status_code, 200)
        self.assertEqual(self.seal().get_json()['state'], 'failed')
        microphone = next(r for r in self.snapshot()['meeting']['recordings'] if r['source'] == 'microphone')
        self.assertEqual(microphone['state'], 'failed')
        self.assertEqual(microphone['error'], failure['detail'])
        self.assertEqual(self.end().status_code, 409)
        self.assertTrue(path.is_file())

    def test_empty_source_requires_explicit_close(self):
        self.assertEqual(self.capture(provider='apple_local', sources=('system', 'microphone')).status_code, 200)
        path = self.confirm_recording('system')
        self.assertEqual(self.seal().get_json()['state'], 'failed')
        self.assertEqual(self.end().status_code, 409)
        self.wb.recover()
        self.assertEqual(self.seal().get_json()['state'], 'failed')
        empty = self.event('audio.recording', source='microphone', state='missing', frames_received=0, audio_end_ms=0)
        self.assertEqual(self.send(empty).status_code, 200)
        self.assertEqual(self.seal().get_json()['state'], 'with_gaps')
        self.assertEqual(self.end().status_code, 200)
        self.assertTrue(path.is_file())

    def test_write_failure_without_frames_is_not_a_successful_empty_capture(self):
        self.assertEqual(self.capture(provider='apple_local').status_code, 200)
        failure = self.event('audio.recording', state='failed', frames_received=0, code='recording_failed')
        self.assertEqual(self.send(failure).status_code, 200)
        self.assertEqual(self.seal(no_audio=True).get_json()['state'], 'failed')
        self.assertEqual(self.end().status_code, 409)

    def test_old_capture_completion_cannot_release_next_round_and_empty_round_can_end(self):
        self.assertEqual(self.capture(provider='apple_local').status_code, 200)
        self.assertEqual(self.seal(saved=False, no_audio=True).status_code, 400)
        self.assertEqual(self.seal(no_audio=True).get_json()['state'], 'no_audio')
        self.assertEqual(self.capture(epoch='run-2', provider='apple_local').status_code, 200)
        self.assertEqual(self.send(self.event('asr.status', epoch='unknown', provider='dashscope')).status_code, 409)
        self.assertEqual(self.seal(no_audio=True).get_json()['state'], 'no_audio')
        self.assertEqual(self.end().status_code, 409)
        self.assertEqual(self.send(self.event('asr.status', state='ready')).status_code, 409)
        self.assertEqual(self.seal(epoch='run-2', no_audio=True).get_json()['state'], 'no_audio')
        self.assertEqual(self.end().status_code, 200)

    def test_deepseek_import_keeps_original_partial_transcript_and_failed_position(self):
        self.meeting = self.wb.create(self.owner, NewMeeting(title='文件转写测试', input_kind='audio_file'))
        self.mid = self.meeting['id']
        self.assertEqual(self.authorize(provider='deepseek', kind='file', sources=('file',)).status_code, 200)
        self.assertEqual(self.client.post(self.url('audio-import', internal=True), headers=self.native_headers, json={
            'provider': 'deepseek', 'import_id': 'job-1', 'file_name': '录音.wav',
            'duration_ms': 130_000, 'bytes': 2048, 'settings_revision': 1,
        }).status_code, 200)
        config = self.session(epoch='job-1', kind='file', sources=('file',)).get_json()
        self.assertEqual(config['endpoint'], 'https://api.deepseek.com/v1/tools/asr')
        self.assertEqual(config['api_key'], self.deepseek_key)
        job = self.snapshot()['meeting']['audio_import']
        original = self.wb.writer_for(self.meeting).path(job['original_path'])
        original.write_bytes(b'fixture imported audio')
        self.assertEqual(self.send(self.event('transcript.final', epoch='job-1', source='file', provider='deepseek',
                                             segment_id='chunk-1', revision=1, audio_start_ms=0, audio_end_ms=90_000,
                                             text='已完成的第一段')).status_code, 200)
        self.assertEqual(self.send(self.event('import.progress', epoch='job-1', source='file',
                                             seq=2, audio_end_ms=90_000)).status_code, 200)
        self.assertEqual(self.end().status_code, 409)
        finish_url = self.url('audio-import/finish', internal=True)
        self.assertEqual(self.client.post(finish_url, headers=self.native_headers,
                                         json={'import_id': 'job-1', 'state': 'completed'}).status_code, 409)
        response = self.client.post(finish_url, headers=self.native_headers,
                                    json={'import_id': 'job-1', 'state': 'failed', 'error': '模拟第二段不可用'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['audio_import']['failed_at_ms'], 90_000)
        self.assertTrue(original.is_file())
        transcript = self.wb.transcript(self.owner, self.mid)['content']
        self.assertIn('已完成的第一段', transcript)
        self.assertIn('DeepSeek', transcript)
