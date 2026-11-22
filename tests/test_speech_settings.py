"""Offline verification of the exact configuration enabled by a desktop short test."""

import json
import os
import stat
import tempfile
import unittest

from live.speech_settings import TEST_SAMPLE_VERSION
from pathlib import Path
from unittest.mock import patch

from live_api import create_app


class SpeechSettingsTest(unittest.TestCase):
    key = 'fixture-speech-key'
    dashscope = {'base_url': 'https://dashscope.aliyuncs.com', 'api_key': key}
    deepseek = {'api_key': 'fixture-deepseek-key'}
    owner = {'Authorization': 'Bearer fixture-owner-0'}
    native = {**owner, 'X-ZSJH-Speech-Token': 'fixture-native-session'}
    settings_url = '/live/v2/settings'
    tests_url = settings_url + '/speech/tests'
    sample = '请确认语音转写。现在检查完整识别。'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / 'private' / 'providers.json'
        with patch.dict(os.environ, {'ZSJH_WB_MODEL_APPROVED': 'false', 'ZSJH_WB_SEARCH_APPROVED': 'false'}, clear=True):
            app = create_app(self.root / 'test.sqlite3', {
                'TESTING': True, 'WORKBENCH_SETTINGS_PATH': self.path,
                'WORKBENCH_ROOT': self.root / 'meetings', 'WORKBENCH_SPEECH_TOKEN': 'fixture-native-session',
            })
        app.extensions['live_store'].seed(fixture=True)
        self.settings = app.extensions['workbench_settings']
        self.client = app.test_client()

    def start(self, provider, kind, config, **extra):
        return self.client.post(self.tests_url, headers=self.owner, json={
            'provider': provider, 'kind': kind, 'config': config, 'revision': self.settings.current.revision, **extra,
        })

    def probe(self, provider='dashscope', kind='live', config=None, **result):
        if config is None:
            config = getattr(self, provider)
        created = self.start(provider, kind, config)
        self.assertEqual(created.status_code, 201, created.get_json())
        test_id = created.get_json()['test_id']
        url = f'{self.tests_url}/{test_id}'
        claim = self.client.post(url + '/session', json={}, headers=self.native)
        self.assertEqual(claim.status_code, 200, claim.get_json())
        finished = self.client.post(url + '/result', headers=self.native, json={
            'text': self.sample, 'succeeded': True, 'duration_ms': 1000, 'sample_version': TEST_SAMPLE_VERSION, **result,
        })
        self.assertEqual(finished.status_code, 200, finished.get_json())
        return test_id, finished.get_json()

    def save(self, **extra):
        return self.client.put(self.settings_url + '/speech', headers=self.owner, json={
            'revision': self.settings.current.revision, 'live_provider': 'apple_local', 'file_provider': 'apple_local',
            'fallback_to_apple': True, **extra,
        })

    def test_legacy_default_and_independent_model_save(self):
        settings = self.client.get(self.settings_url, headers=self.owner).get_json()
        speech = settings['speech']
        self.assertEqual((speech['live_provider'], speech['file_provider']), ('apple_local', 'apple_local'))
        self.assertEqual(speech['providers']['dashscope']['live_model'], 'fun-asr-realtime')
        self.assertEqual(speech['providers']['dashscope']['file_model'], 'qwen-audio-3.1-asr-flash')
        self.assertFalse(settings['model']['enabled'])
        self.assertFalse(settings['search']['enabled'])

        test_id, result = self.probe()
        self.assertEqual(result['status'], 'passed')
        self.assertFalse(self.path.exists())
        self.assertEqual(self.save(provider='dashscope', config=self.dashscope,
                                   live_provider='dashscope', test_ids={'live': test_id}).status_code, 200)
        legacy = self.settings.current.model_dump(exclude={'speech'})
        response = self.client.put(self.settings_url, headers=self.owner, json=legacy)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['speech']['live_provider'], 'dashscope')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.path.read_text())['speech']['providers']['dashscope']['api_key'], self.key)

    def test_claim_and_result_require_native_token_and_public_results_mask_key(self):
        created = self.start('dashscope', 'live', self.dashscope)
        self.assertEqual(created.status_code, 201)
        url = f"{self.tests_url}/{created.get_json()['test_id']}"
        result = {'succeeded': True, 'text': self.sample + ' ' + self.key, 'sample_version': TEST_SAMPLE_VERSION}
        self.assertEqual(self.client.post(url + '/session', json={}, headers=self.owner).status_code, 403)
        self.assertEqual(self.client.post(url + '/result', json=result, headers=self.owner).status_code, 403)
        self.assertEqual(self.client.post(url + '/result', json=result, headers=self.native).status_code, 409)
        claim = self.client.post(url + '/session', json={}, headers=self.native)
        self.assertEqual(claim.get_json()['api_key'], self.key)
        self.assertEqual(self.client.post(url + '/session', json={}, headers=self.native).status_code, 409)
        finished = self.client.post(url + '/result', json=result, headers=self.native).get_json()
        self.assertEqual(finished['status'], 'passed')
        self.assertIn('[已隐藏]', finished['text'])
        public = self.client.get(url, headers=self.owner).get_json()
        self.assertNotIn(self.key, json.dumps(public))
        self.assertNotIn('api_key', public)
        saved = self.save(provider='dashscope', config=self.dashscope, live_provider='dashscope',
                          test_ids={'live': created.get_json()['test_id']})
        self.assertEqual(saved.status_code, 200)
        self.assertNotIn(self.key, json.dumps(saved.get_json()))

    def test_incomplete_text_or_missing_terminal_success_cannot_be_enabled(self):
        for text, succeeded in [('', True), ('完整识别。', True), ('完整识别，语音转写。', True), (self.sample, False)]:
            with self.subTest(text=text, succeeded=succeeded):
                test_id, result = self.probe(text=text, succeeded=succeeded)
                self.assertEqual(result['status'], 'failed')
                saved = self.save(provider='dashscope', config=self.dashscope, live_provider='dashscope',
                                  test_ids={'live': test_id})
                self.assertEqual(saved.status_code, 409, saved.get_json())
        self.assertEqual(self.settings.current.revision, 0)
        self.assertFalse(self.path.exists())

    def test_other_sample_version_cannot_enable_provider(self):
        test_id, result = self.probe(sample_version='0' * 64)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('测试音频', result['error'])
        saved = self.save(provider='dashscope', config=self.dashscope, live_provider='dashscope',
                          test_ids={'live': test_id})
        self.assertEqual(saved.status_code, 409)

    def test_receipt_requires_same_provider_purpose_fields_and_current_save_revision(self):
        test_id, result = self.probe()
        self.assertEqual(result['status'], 'passed')
        for changes in [
            {'provider': 'deepseek', 'config': self.deepseek, 'test_ids': {'file': test_id}},
            {'provider': 'dashscope', 'config': self.dashscope, 'test_ids': {'file': test_id}},
            {'provider': 'dashscope', 'config': {**self.dashscope, 'live_model': 'another-model'}, 'test_ids': {'live': test_id}},
            {'provider': 'dashscope', 'config': {**self.dashscope, 'api_key': 'fixture-changed-key'}, 'test_ids': {'live': test_id}},
        ]:
            with self.subTest(provider=changes['provider'], kind=list(changes['test_ids'])):
                self.assertEqual(self.save(**changes).status_code, 409)
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.save(revision=0, provider='dashscope', config=self.dashscope,
                                   live_provider='dashscope', test_ids={'live': test_id}).status_code, 409)
        self.assertEqual(self.settings.current.speech.live_provider, 'apple_local')

    def test_address_change_does_not_reuse_key_and_invalidates_old_receipt(self):
        live, _ = self.probe()
        self.assertEqual(self.save(provider='dashscope', config=self.dashscope,
                                   live_provider='dashscope', test_ids={'live': live}).status_code, 200)
        changed = {'base_url': 'https://dashscope-intl.aliyuncs.com'}
        self.assertEqual(self.start('dashscope', 'live', changed).status_code, 400)
        self.assertEqual(self.save(provider='dashscope', config=changed).status_code, 200)
        speech = self.settings.current.speech.public()
        self.assertFalse(speech['providers']['dashscope']['api_key_configured'])
        self.assertEqual(speech['verification']['dashscope']['live']['status'], 'not_tested')

    def test_bailian_compatible_mode_base_url_is_normalized_for_fun_asr(self):
        compatible = {
            'base_url': 'https://lm-ikga8xjj082yg8x4.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
            'api_key': self.key,
            'live_model': 'fun-asr-realtime-2026-02-28',
            'file_model': 'fun-asr-flash-2026-06-15',
        }
        live = self.start('dashscope', 'live', compatible)
        self.assertEqual(live.status_code, 201, live.get_json())
        live_id = live.get_json()['test_id']
        live_session = self.client.post(
            f'{self.tests_url}/{live_id}/session', json={}, headers=self.native)
        self.assertEqual(live_session.status_code, 200, live_session.get_json())
        self.assertEqual(
            live_session.get_json()['endpoint'],
            'wss://lm-ikga8xjj082yg8x4.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference',
        )
        finished = self.client.post(
            f'{self.tests_url}/{live_id}/result', headers=self.native,
            json={'text': self.sample, 'succeeded': True, 'duration_ms': 1000,
                  'sample_version': TEST_SAMPLE_VERSION},
        )
        self.assertEqual(finished.status_code, 200, finished.get_json())

        file = self.start('dashscope', 'file', compatible)
        self.assertEqual(file.status_code, 201, file.get_json())
        file_id = file.get_json()['test_id']
        file_session = self.client.post(
            f'{self.tests_url}/{file_id}/session', json={}, headers=self.native)
        self.assertEqual(file_session.status_code, 200, file_session.get_json())
        self.assertEqual(
            file_session.get_json()['endpoint'],
            'https://lm-ikga8xjj082yg8x4.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation',
        )

    def test_deepseek_file_choice_is_independent_from_both_live_services(self):
        live, _ = self.probe()
        self.assertEqual(self.save(provider='dashscope', config=self.dashscope,
                                   live_provider='dashscope', test_ids={'live': live}).status_code, 200)
        self.assertEqual(self.start('deepseek', 'live', self.deepseek).status_code, 400)
        self.assertEqual(self.save(live_provider='deepseek').status_code, 400)
        file_id, result = self.probe('deepseek', 'file')
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(self.save(provider='deepseek', config=self.deepseek, live_provider='dashscope',
                                   file_provider='deepseek', test_ids={'file': file_id}).status_code, 200)
        replacement = {'api_key': 'fixture-replacement-deepseek-key'}
        next_test, _ = self.probe('deepseek', 'file', replacement)
        saved = self.save(provider='deepseek', config=replacement, live_provider='dashscope',
                          file_provider='deepseek', test_ids={'file': next_test})
        self.assertEqual(saved.status_code, 200)
        speech = saved.get_json()['speech']
        self.assertEqual(speech['providers']['deepseek']['destinations'], {'file': 'https://api.deepseek.com/v1/tools/asr'})
        self.assertEqual(speech['verification']['dashscope']['live']['status'], 'passed')
        self.assertEqual(speech['verification']['dashscope']['file']['status'], 'not_tested')
