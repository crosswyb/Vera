"""Cloud speech settings and short test receipts, independent of model settings."""

import hashlib
import json
import re
import time
import unicodedata
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator

from live.store import require, uid
from live.workbench_contracts import Contract


SpeechProvider = Literal['apple_local', 'dashscope', 'deepseek']
CloudProvider = Literal['dashscope', 'deepseek']
SpeechKind = Literal['live', 'file']
PROVIDER_KINDS = {'apple_local': ('live', 'file'), 'dashscope': ('live', 'file'), 'deepseek': ('file',)}
DEEPSEEK_ENDPOINT = 'https://api.deepseek.com/v1/tools/asr'
DASHSCOPE_LIVE_PATH = '/api-ws/v1/inference'
DASHSCOPE_FILE_PATH = '/api/v1/services/aigc/multimodal-generation/generation'
# SHA-256 of the bundled synthetic Chinese probe WAV.
TEST_SAMPLE_VERSION = "381dcd158ed6f6e17effd0676cfc4664c9aabf4911da2597a46d71ce6bb3a2d4"
TEST_TIMEOUT_SECONDS = 30
TEST_RETENTION_SECONDS = 300
MAX_TESTS = 64


def dashscope_base_url(value):
    if not value.strip():
        return ''
    value = value.strip().rstrip('/')
    if any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError('请选择百炼官方服务地址')
    parts = urlsplit(value)
    host = parts.hostname or ''
    official = host in {'dashscope.aliyuncs.com', 'dashscope-intl.aliyuncs.com'} or re.fullmatch(
        r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com', host)
    if (not official or parts.username is not None or parts.password is not None or parts.port not in (None, 443)
            or parts.query or parts.fragment or (parts.scheme, parts.path) not in {
                ('https', ''), ('https', '/api/v1'), ('https', DASHSCOPE_FILE_PATH),
                # The Bailian console also shows the OpenAI-compatible base URL
                # for the same workspace. Fun-ASR uses its native endpoints,
                # so keep only the approved host and derive those endpoints
                # below instead of sending requests to the compatible-mode path.
                ('https', '/compatible-mode/v1'),
                ('wss', DASHSCOPE_LIVE_PATH),
            }):
        raise ValueError('请选择百炼官方服务地址')
    return 'https://' + host


class PrivateSpeechConfig(Contract):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    api_key: str = Field(default='', max_length=4096, repr=False)

    @field_validator('api_key')
    @classmethod
    def api_key_text(cls, value):
        value = value.strip()
        if any(not 32 < ord(c) < 127 for c in value):
            raise ValueError('API Key 格式无效')
        return value


class DashScopeConfig(PrivateSpeechConfig):
    base_url: str = Field(default='', max_length=1000)
    live_model: str = Field(default='fun-asr-realtime', max_length=160)
    file_model: str = Field(default='qwen-audio-3.1-asr-flash', max_length=160)

    _normalize = field_validator('base_url')(dashscope_base_url)

    @field_validator('live_model', 'file_model')
    @classmethod
    def model_name(cls, value):
        value = value.strip()
        if value and not re.fullmatch(r'[a-zA-Z0-9._-]+', value):
            raise ValueError('语音模型名称无效')
        return value


class SpeechCloudConfig(PrivateSpeechConfig):
    """Draft patch. Provider-specific fields are validated after merging."""
    base_url: str | None = Field(default=None, max_length=1000)
    live_model: str | None = Field(default=None, max_length=160)
    file_model: str | None = Field(default=None, max_length=160)
    clear_api_key: bool = False

    @field_validator('api_key', mode='before')
    @classmethod
    def omitted_key(cls, value):
        return '' if value is None else value


class SpeechProviders(Contract):
    model_config = ConfigDict(frozen=True)
    dashscope: DashScopeConfig = Field(default_factory=DashScopeConfig)
    deepseek: PrivateSpeechConfig = Field(default_factory=PrivateSpeechConfig)


class Verification(Contract):
    model_config = ConfigDict(frozen=True)
    status: Literal['passed'] = 'passed'
    text: str = Field(max_length=5000)
    duration_ms: int = Field(ge=0, le=600_000)
    tested_at: float
    sample_version: str = Field(default="", max_length=64)


class SpeechSettings(Contract):
    model_config = ConfigDict(frozen=True)
    live_provider: SpeechProvider = 'apple_local'
    file_provider: SpeechProvider = 'apple_local'
    fallback_to_apple: bool = True
    providers: SpeechProviders = Field(default_factory=SpeechProviders)
    verification: dict[CloudProvider, dict[SpeechKind, Verification]] = Field(default_factory=dict)

    def session(self, provider, kind):
        require(kind in PROVIDER_KINDS[provider], '这个服务不支持所选转写用途', 400)
        if provider == 'apple_local':
            return {'provider': provider, 'endpoint': '', 'model': '', 'api_key': ''}
        config = getattr(self.providers, provider)
        if provider == 'deepseek':
            endpoint, model = DEEPSEEK_ENDPOINT, ''
        else:
            endpoint = ('wss://' + config.base_url.removeprefix('https://') + DASHSCOPE_LIVE_PATH
                        if kind == 'live' else config.base_url + DASHSCOPE_FILE_PATH) if config.base_url else ''
            model = config.live_model if kind == 'live' else config.file_model
        return {'provider': provider, 'endpoint': endpoint, 'model': model, 'api_key': config.api_key}

    def available(self, provider, kind):
        if kind not in PROVIDER_KINDS[provider]:
            return False
        if provider == 'apple_local':
            return True
        session = self.session(provider, kind)
        receipt = self.verification.get(provider, {}).get(kind)
        return self._configured(session) and receipt is not None and receipt.sample_version == TEST_SAMPLE_VERSION

    @staticmethod
    def _configured(session):
        return bool(session['api_key'] and session['endpoint'] and (session['provider'] != 'dashscope' or session['model']))

    def public(self):
        providers = {}
        verification = {}
        for provider in ('dashscope', 'deepseek'):
            config = getattr(self.providers, provider)
            providers[provider] = {
                **config.model_dump(exclude={'api_key'}), 'api_key_configured': bool(config.api_key),
                'destinations': {kind: self.session(provider, kind)['endpoint'] for kind in PROVIDER_KINDS[provider]},
            }
            verification[provider] = {}
            for kind in PROVIDER_KINDS[provider]:
                receipt = self.verification.get(provider, {}).get(kind)
                verification[provider][kind] = receipt.model_dump() if self.available(provider, kind) else {'status': 'not_tested'}
        return {'live_provider': self.live_provider, 'file_provider': self.file_provider,
                'fallback_to_apple': self.fallback_to_apple, 'providers': providers, 'verification': verification}

    def edited(self, provider, patch):
        previous = getattr(self.providers, provider)
        if patch is None:
            return self
        fields = patch.model_dump(exclude={'api_key', 'clear_api_key'}, exclude_none=True)
        require(not (provider == 'deepseek' and fields), 'DeepSeek 语音转写使用固定的服务地址', 400)
        require(not (patch.clear_api_key and patch.api_key), '清除与替换 API Key 不能同时进行', 400)
        private = type(previous).model_validate({**previous.model_dump(), **fields})
        address_changed = provider == 'dashscope' and private.base_url != previous.base_url
        api_key = patch.api_key or ('' if patch.clear_api_key or address_changed else previous.api_key)
        private = private.model_copy(update={'api_key': api_key})
        candidate = self.model_copy(update={'providers': self.providers.model_copy(update={provider: private})})
        kept = {kind: receipt for kind, receipt in self.verification.get(provider, {}).items()
                if kind in PROVIDER_KINDS[provider] and self.session(provider, kind) == candidate.session(provider, kind)}
        return candidate.model_copy(update={'verification': {**self.verification, provider: kept}})


class SpeechTestRequest(Contract):
    provider: CloudProvider
    kind: SpeechKind
    revision: int = Field(ge=0)
    config: SpeechCloudConfig = Field(default_factory=SpeechCloudConfig)


class SpeechTestResult(Contract):
    text: str = Field(default='', max_length=5000)
    succeeded: bool
    sample_version: str = Field(default="", max_length=64)
    duration_ms: int = Field(default=0, ge=0, le=600_000)
    error: str = Field(default='', max_length=500)


class SpeechSettingsUpdate(Contract):
    revision: int = Field(ge=0)
    provider: CloudProvider | None = None
    config: SpeechCloudConfig | None = None
    live_provider: SpeechProvider
    file_provider: SpeechProvider
    fallback_to_apple: bool
    test_ids: dict[SpeechKind, str] = Field(default_factory=dict)


class SpeechTests:
    """Owned by one local server. Call under ProviderSettings.lock."""

    def __init__(self):
        self.jobs = {}

    @staticmethod
    def fingerprint(session):
        return hashlib.sha256(json.dumps(session, sort_keys=True).encode()).digest()

    def prune(self):
        now = time.monotonic()
        for test_id, job in list(self.jobs.items()):
            elapsed = now - job['created']
            if elapsed >= TEST_RETENTION_SECONDS:
                self.jobs.pop(test_id)
            elif elapsed >= TEST_TIMEOUT_SECONDS and job['status'] in {'pending', 'running'}:
                job.update(status='failed', error='测试超时，请重试')
                job.pop('session', None)

    def find(self, test_id, principal):
        self.prune()
        job = self.jobs.get(test_id)
        require(job is not None and job['owner'] == principal, '测试不存在或已过期', 404)
        return job

    def start(self, settings, request, owner):
        self.prune()
        require(settings.revision == request.revision, '设置已更新，请重新载入', 409)
        require(not any(job['status'] in {'pending', 'running'} for job in self.jobs.values()), '已有测试在进行', 409)
        require(len(self.jobs) < MAX_TESTS, '测试过于频繁，请稍后再试', 429)
        speech = settings.speech.edited(request.provider, request.config)
        session = speech.session(request.provider, request.kind)
        require(speech._configured(session), '请填写语音服务地址、模型和 API Key', 400)
        test_id = uid('speech-test')
        self.jobs[test_id] = {
            'provider': request.provider, 'kind': request.kind, 'owner': owner,
            'created': time.monotonic(), 'status': 'pending', 'text': '', 'duration_ms': 0, 'error': '',
            'fingerprint': self.fingerprint(session),
            'session': {**session, 'fallback_to_apple': False, 'revision': settings.revision},
        }
        return {'test_id': test_id}

    @staticmethod
    def public(job):
        return {key: job[key] for key in ('status', 'provider', 'kind', 'text', 'duration_ms', 'error')}

    def claim(self, job):
        require(job['status'] == 'pending', '测试已领取或已完成', 409)
        job['status'] = 'running'
        return {**job['session'], 'sample_version': TEST_SAMPLE_VERSION}

    def finish(self, job, result):
        require(job['status'] == 'running', '测试未领取或已完成', 409)
        key = job.pop('session')['api_key']
        text = result.text.replace(key, '[已隐藏]').strip()
        error = result.error.replace(key, '[已隐藏]').strip()
        if result.succeeded and result.sample_version != TEST_SAMPLE_VERSION:
            error = '测试音频与当前版本不一致，请重新安装应用。'
        elif result.succeeded:
            normalized = ''.join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith('P'))
            first = normalized.find('语音转写')
            last = normalized.find('完整识别', first + len('语音转写')) if first >= 0 else -1
            error = '' if last >= 0 else ('未返回文本' if not text else '未按顺序识别出“语音转写”和“完整识别”')
        else:
            error = error or '未收到完整转写结果'
        job.update(text=text, duration_ms=result.duration_ms, error=error, status='failed' if error else 'passed',
                   tested_at=time.time())
        return self.public(job)

    def save(self, settings, request, owner):
        require(settings.revision == request.revision, '设置已更新，请重新载入', 409)
        require(request.provider is not None or (request.config is None and not request.test_ids), '请选择配置服务', 400)
        speech = settings.speech
        if request.provider:
            speech = speech.edited(request.provider, request.config)
            receipts = dict(speech.verification.get(request.provider, {}))
            for kind, test_id in request.test_ids.items():
                job = self.find(test_id, owner)
                session = speech.session(request.provider, kind)
                require(job['status'] == 'passed' and job['provider'] == request.provider and job['kind'] == kind
                        and job['fingerprint'] == self.fingerprint(session), '请先用当前配置通过对应的语音转写测试', 409)
                receipts[kind] = Verification(text=job['text'], duration_ms=job['duration_ms'],
                                              tested_at=job['tested_at'], sample_version=TEST_SAMPLE_VERSION)
            speech = speech.model_copy(update={'verification': {**speech.verification, request.provider: receipts}})
        for kind, provider in [('live', request.live_provider), ('file', request.file_provider)]:
            require(kind in PROVIDER_KINDS[provider], '这个服务不支持所选转写用途', 400)
            require(speech.available(provider, kind), '请先用当前配置通过对应的语音转写测试', 409)
        if request.config is not None and request.provider:
            previous = getattr(settings.speech.providers, request.provider)
            configured = getattr(speech.providers, request.provider)
            require(configured == previous or not configured.api_key or speech.verification.get(request.provider),
                    '请先测试语音服务，再保存配置', 409)
        speech = speech.model_copy(update={'live_provider': request.live_provider, 'file_provider': request.file_provider,
                                          'fallback_to_apple': request.fallback_to_apple})
        return settings.model_copy(update={'revision': settings.revision + 1, 'speech': speech})
