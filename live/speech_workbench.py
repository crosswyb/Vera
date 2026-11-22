"""Meeting audio upload scope, short-lived permissions, and capture completion."""

import json
import time

from live.recording_archive import identity
from live.speech_settings import PROVIDER_KINDS
from live.store import require


SPEECH_LEASE_SECONDS = 30
CAPTURE_SAVED = {'complete', 'with_gaps', 'no_audio'}


class SpeechWorkbench:
    def _speech_gaps(self, db, meeting):
        """Join saved gaps to their durable ASR events, including older meetings."""
        gaps = [{**gap, 'capture_epoch': epoch_id}
                for epoch_id, epoch in meeting.get('capture_epochs', {}).items()
                for gap in epoch.get('gaps', [])]
        job = meeting.get('audio_import')
        if job:
            gaps.extend({**gap, 'capture_epoch': job['import_id']}
                        for gap in meeting.get('transcription', {}).get('file', {}).get('gaps', []))
        if not gaps:
            return []
        bindings = {(gap['capture_epoch'], gap.get('source')) for gap in gaps}
        failures, diagnostics = {}, {}
        rows = db.execute("SELECT data FROM events WHERE meeting=? AND json_extract(data,'$.type') "
                          "IN ('asr.error','asr.status','asr.gap') ORDER BY seq", (meeting['id'],))
        for row in rows:
            event = json.loads(row[0])
            value = event['payload']
            binding = (value.get('capture_epoch'), value.get('source'))
            if binding not in bindings:
                continue
            causes = failures.setdefault(binding, [])
            if event['type'] == 'asr.error':
                cause = {key: value.get(key) for key in ('provider', 'code', 'detail')}
                if not causes or causes[-1] != cause:
                    causes.append(cause)
            elif event['type'] == 'asr.status' and (value.get('state') or value.get('status')) in {
                    'running', 'ready', 'recognizing', 'local', 'degraded'}:
                causes.clear()
            elif event['type'] == 'asr.gap':
                key = (*binding, value.get('audio_start_ms'), value.get('audio_end_ms'), value.get('code'))
                diagnostics[key] = {'provider': value.get('provider'), 'detail': value.get('detail'),
                                    'causes': list(causes)}
        return [{**gap, **diagnostics.get((gap['capture_epoch'], gap.get('source'), gap.get('start_ms'),
                                          gap.get('end_ms'), gap.get('code')), {})} for gap in gaps]

    def speech_authorize(self, principal, mid, value):
        with self.store.transaction() as db:
            meeting = self._meeting(db, principal, mid, True)
            self.store._acl(db, principal, meeting['project_id'], {'owner'})
            require(meeting['state'] == 'active', '会议已结束', 409)
            previous = meeting.get('speech_authorization', {})
            version = previous.get('version', 0) + 1
            if value.revoke:
                scope = {'state': 'revoked', 'version': version}
            else:
                settings = self.provider_settings.current
                require(value.revision == settings.revision, '设置已更新，请重新确认音频发送地址', 409)
                kind = 'file' if meeting.get('input_kind') == 'audio_file' else 'live'
                require(value.kind == kind and kind in PROVIDER_KINDS[value.provider], '服务不支持本场转写用途', 400)
                require(meeting.get('data_scope') == 'real', '此会议不允许上传音频', 409)
                require(settings.speech.available(value.provider, kind), '语音服务还未通过测试', 409)
                destination = settings.speech.session(value.provider, kind)['endpoint']
                scope = {'state': 'authorized', 'provider': value.provider, 'kind': kind,
                         'destination': destination, 'sources': sorted(value.sources), 'version': version}
                # Showing the same scope twice must not invalidate a running lease.
                if all(previous.get(key) == scope[key] for key in ('state', 'provider', 'kind', 'destination', 'sources')):
                    return previous
            scope.update(updated_at=time.time(), actor_id=principal['id'])
            meeting['speech_authorization'] = scope
            self._event(db, meeting, 'speech.authorization', scope)
            self._save(db, meeting)
            return scope

    def _configured_speech(self, meeting, provider, kind, sources, revision):
        """Validate registration while the meeting transaction is held."""
        settings = self.provider_settings.current
        require(settings.revision == revision, '设置已更新，请重新选择语音转写服务', 409)
        require(kind in PROVIDER_KINDS[provider], '服务不支持本场转写用途', 400)
        if provider == 'apple_local':
            return {'provider': provider}
        require(meeting.get('data_scope') == 'real', '此会议不允许上传音频', 409)
        require(settings.speech.available(provider, kind), '语音服务还未通过测试', 409)
        session = settings.speech.session(provider, kind)
        scope = meeting.get('speech_authorization', {})
        require(scope.get('state') == 'authorized' and scope.get('provider') == provider and scope.get('kind') == kind
                and scope.get('destination') == session['endpoint'] and set(sources) <= set(scope.get('sources', [])),
                '请主持人确认本场音频上传服务和音源', 409)
        return {'provider': provider, 'authorization_version': scope['version'], 'destination': session['endpoint'],
                'settings_revision': settings.revision}

    def _register_speech_capture(self, meeting, value):
        epochs = meeting.setdefault('capture_epochs', {})
        require(all(epoch['state'] in CAPTURE_SAVED for epoch in epochs.values()), '请先完成上一轮录音收尾', 409)
        require(value.recording_id not in epochs, '录音标识已使用', 409)
        speech = self._configured_speech(meeting, value.provider, 'live', value.sources, value.settings_revision)
        epochs[value.recording_id] = {**speech, 'state': 'recording', 'sources': sorted(value.sources), 'gaps': [],
                                      'started_at': time.time()}

    def speech_session(self, principal, mid, value, *, renew):
        with self.store.transaction() as db:
            meeting = self._meeting(db, principal, mid, True)
            self.store._acl(db, principal, meeting['project_id'], {'agent'})
            require(meeting['state'] == 'active', '会议已结束', 409)
            if value.kind == 'live':
                task = meeting.get('capture_epochs', {}).get(value.task_id, {})
                require(task.get('state') == 'recording' and sorted(value.sources) == task.get('sources'),
                        '不是当前采音轮次或音源不匹配', 409)
            else:
                task = meeting.get('audio_import', {})
                require(task.get('import_id') == value.task_id and task.get('state') == 'processing' and value.sources == ['file'],
                        '不是当前音频导入任务或音源不匹配', 409)
            require(task.get('provider') in {'dashscope', 'deepseek'}, '本任务没有云端会话', 409)
            scope = meeting.get('speech_authorization', {})
            require(scope.get('state') == 'authorized' and scope.get('version') == task.get('authorization_version'),
                    '音频上传授权已撤回或变更', 409)
            if renew:
                # Renew the exact scope originally granted, across later settings saves.
                require(task.get('lease_until', 0) > time.time(), '音频上传授权已到期，请重新开始', 409)
                result = {}
            else:
                settings = self.provider_settings.current
                require(not task.get('lease_until'), '本任务已领取会话', 409)
                require(settings.revision == task['settings_revision'] and settings.speech.available(task['provider'], value.kind),
                        '语音设置已更新，请重试', 409)
                result = {**settings.speech.session(task['provider'], value.kind), 'revision': settings.revision,
                          'fallback_to_apple': settings.speech.fallback_to_apple}
            task['lease_until'] = time.time() + SPEECH_LEASE_SECONDS
            self._save(db, meeting)
            return {**result, 'lease_seconds': SPEECH_LEASE_SECONDS}

    def capture_finish(self, principal, mid, value):
        with self.store.transaction() as db:
            meeting = self._meeting(db, principal, mid, True)
            self.store._acl(db, principal, meeting['project_id'], {'agent'})
            epoch = meeting.get('capture_epochs', {}).get(value.capture_epoch)
            require(epoch is not None, '未登记此轮录音', 409)
            if epoch['state'] in CAPTURE_SAVED:
                return {'state': epoch['state'], 'gaps': epoch['gaps']}
            require(meeting['state'] == 'active', '会议已结束', 409)
            recordings = [r for r in meeting.get('recordings', []) if r['epoch'] == value.capture_epoch]
            state = 'failed'
            if value.no_audio and all(r['state'] != 'failed' for r in recordings):
                has_audio = any(r.get('frames_received', 0) or r.get('bytes', 0) or self.writer_for(meeting).path(r['path']).exists()
                                for r in recordings)
                require(not has_audio and not epoch.get('observed_audio'), '本轮已有录音内容，必须先保存', 409)
                for recording in recordings:
                    recording['state'] = 'missing'
                state = 'no_audio'
            elif not value.no_audio and {r['source'] for r in recordings} == set(epoch['sources']):
                confirmed = [r for r in recordings if self._recording_still_present(meeting, r)]
                empty = [r for r in recordings if self._recording_closed_without_audio(meeting, r)]
                if confirmed and len(confirmed) + len(empty) == len(recordings):
                    # A journal failure does not invalidate independently closed,
                    # identity-checked recordings. Preserve the warning instead
                    # of fencing this otherwise saved meeting forever.
                    if not value.saved:
                        epoch['gaps'].extend({'source': r['source'], 'start_ms': None, 'end_ms': None,
                                              'code': 'delivery_incomplete'} for r in confirmed)
                    incomplete = not value.saved or empty or epoch['gaps'] or any(r['state'] == 'partial' for r in confirmed)
                    state = 'with_gaps' if incomplete else 'complete'
                elif len(empty) == len(recordings) and not epoch.get('observed_audio'):
                    # Every selected source explicitly closed before receiving
                    # audio. Unknown/missing files alone never establish this.
                    state = 'no_audio'
            epoch.update(state=state, finished_at=time.time())
            self._event(db, meeting, 'capture.finish', {'capture_epoch': value.capture_epoch, 'state': state})
            self._save(db, meeting)
            return {'state': state, 'gaps': epoch['gaps']}

    def _recording_still_present(self, meeting, recording):
        if recording['state'] not in {'saved', 'partial'} or not recording.get('closed_identity'):
            return False
        path = self.writer_for(meeting).path(recording['path'])
        try:
            return path.is_file() and identity(path) == recording['closed_identity']
        except OSError:
            return False

    def _recording_closed_without_audio(self, meeting, recording):
        # Recovery can also mark a track missing. Only the helper can confirm
        # that the source closed cleanly without receiving any frames.
        if recording['state'] != 'missing' or not recording.get('closed_without_audio'):
            return False
        try:
            self.writer_for(meeting).path(recording['path']).stat()
        except FileNotFoundError:
            return True
        except OSError:
            pass
        return False

    def _capture_event_binding(self, meeting, value):
        epochs = meeting.get('capture_epochs', {})
        epoch = epochs.get(value.capture_epoch)
        if epoch is None:
            speech_event = value.type.startswith('asr.') or value.provider in {'dashscope', 'deepseek'}
            legacy_recording = any(r['epoch'] == value.capture_epoch for r in meeting.get('recordings', []))
            require(not speech_event and (not epochs or legacy_recording), '不是已登记的采音轮次', 409)
            return None
        require(epoch['state'] not in CAPTURE_SAVED, '本轮录音已收尾', 409)
        control = value.stream_id == 'control-' + value.capture_epoch and value.type.startswith('audio.')
        require(control or value.source in epoch['sources'], '音源不属于本轮录音', 409)
        if value.type.startswith('transcript.') or value.frames_received or value.audio_end_ms:
            epoch['observed_audio'] = True
        return epoch

    def _apply_speech_event(self, meeting, value, epoch):
        normalized = value.state or value.status or value.type.removeprefix('asr.')
        source = meeting.setdefault('transcription', {}).setdefault(value.source, {})
        source.setdefault('gaps', [])
        if source.get('capture_epoch') != value.capture_epoch:
            source.clear()
            source.update(capture_epoch=value.capture_epoch, gaps=[])
        if value.provider is not None:
            source['provider'] = value.provider
        source['updated_at'] = time.time()
        if value.type == 'asr.gap':
            gap = {'source': value.source, 'start_ms': value.audio_start_ms, 'end_ms': value.audio_end_ms, 'code': value.code}
            if epoch is not None:
                epoch['gaps'].append(gap)
            source['gaps'].append(gap)
        else:
            source.update(status=normalized, code=value.code, detail=value.detail)
