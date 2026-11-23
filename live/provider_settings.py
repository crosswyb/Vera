"""Private provider configuration, separate from meeting content and permissions.

The environment is a one-time migration source. Saved settings are immutable
snapshots: one paid task keeps one snapshot even when settings change mid-call.
"""

import ipaddress
import json
import os
import re
import stat
import tempfile
import threading
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from live.speech_settings import SpeechSettings
from live.store import LiveError, require


def loopback_url(value):
    host = urlsplit(value).hostname or ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def normalize_base_url(value):
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("服务地址无效") from None
    if (len(value) > 1000 or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(c.isspace() or ord(c) < 32 for c in value)
            or parsed.scheme not in {"https", "http"} or port == 0
            or (parsed.scheme == "http" and not loopback_url(value))):
        raise ValueError("请使用 HTTPS BaseURL；本机服务可使用 http://localhost")
    path = parsed.path.rstrip("/")
    # Accept the common copied completion URL without appending it a second time.
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), path, "", ""))


def origin(value):
    parsed = urlsplit(value)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


class ModelService(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    provider: Literal["deepseek", "openai_compatible"] = "deepseek"
    base_url: str = "https://api.deepseek.com"
    name: str = Field(default="deepseek-flash", max_length=160)
    api_key: str = Field(default="", max_length=4096, repr=False)
    enabled: bool = True
    json_mode: bool = True
    reasoning_effort: Literal["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"] = "default"
    thinking: Literal["disabled", "enabled"] = "disabled"
    # Null selects an application budget; saved numeric overrides stay explicit.
    max_tokens: int | None = Field(default=None, ge=256, le=65536)
    timeout_seconds: int = Field(default=180, ge=15, le=600)
    token_parameter: Literal["auto", "max_tokens", "max_completion_tokens"] = "auto"
    detection_mode: Literal["fast", "inherit"] = "fast"

    _base_url = field_validator("base_url")(normalize_base_url)

    @field_validator("name", "api_key")
    @classmethod
    def plain_field(cls, value):
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("字段不能包含控制字符")
        return value.strip()

    @property
    def configured(self):
        return bool(self.name and (self.api_key or loopback_url(self.base_url)))

    def public(self):
        return {**self.model_dump(exclude={"api_key"}), "api_key_configured": bool(self.api_key)}

    def official_deepseek(self):
        return (self.provider == "deepseek" and origin(self.base_url) == ("https", "api.deepseek.com", 443)
                and urlsplit(self.base_url).path in {"", "/v1"})

    def profile_key(self):
        url = "https://api.deepseek.com" if self.official_deepseek() else self.base_url
        return self.provider, url, self.name

    @property
    def reasoning_model(self):
        # Gateways may prefix the upstream model with a provider name.
        name = self.name.rsplit("/", 1)[-1].lower()
        return bool(re.match(r"(?:o[134](?:-|$)|gpt-[56](?:[.-]|$))", name))

    @property
    def output_token_parameter(self):
        if self.token_parameter != "auto":
            return self.token_parameter
        if self.provider == "deepseek":
            return "max_tokens"
        if self.reasoning_model or origin(self.base_url) == ("https", "api.openai.com", 443):
            return "max_completion_tokens"
        return "max_tokens"

    def payload(self, system, content, *, summary=False, detection=False, routing=False):
        fast = (detection or routing) and self.detection_mode == "fast"
        thinking = "disabled" if fast else self.thinking
        effort = self.reasoning_effort
        if fast and detection and effort in {"high", "xhigh", "max"}:
            effort = "medium"
        elif fast and routing and effort in {"medium", "high", "xhigh", "max"}:
            effort = "low"
        tokens = self.max_tokens
        if tokens is None:
            tokens = 8192
            if not fast:
                if self.provider == "deepseek" and thinking == "enabled":
                    tokens = 65536 if self.official_deepseek() else 32768
                elif self.provider != "deepseek" and effort != "none":
                    name = self.name.rsplit("/", 1)[-1].lower()
                    legacy_openai = name.startswith(("gpt-4", "gpt-3.5"))
                    if (self.reasoning_model or effort != "default"
                            or (self.output_token_parameter == "max_completion_tokens" and not legacy_openai)):
                        tokens = 32768
        elif fast:
            tokens = min(max(tokens, 8192), 16384)
        elif summary:
            tokens = max(tokens, 8192)
        payload = {"model": self.name, "messages": [{"role": "system", "content": system},
                   {"role": "user", "content": json.dumps(content, ensure_ascii=False)}],
                   self.output_token_parameter: tokens}
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.provider == "deepseek":
            payload["thinking"] = {"type": thinking}
        if effort != "default" and (self.provider != "deepseek" or thinking == "enabled"):
            payload["reasoning_effort"] = effort
        return payload


class SearchService(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    provider: Literal["deepseek", "tavily"] = "deepseek"
    base_url: str = "https://api.deepseek.com"
    api_key: str = Field(default="", max_length=4096, repr=False)
    enabled: bool = True
    use_model_key: bool = True

    _base_url = field_validator("base_url")(normalize_base_url)
    _plain_key = field_validator("api_key")(ModelService.plain_field.__func__)

    def key(self, model):
        if self.use_model_key:
            if self.provider != "deepseek" or model.provider != "deepseek" or origin(self.base_url) != origin(model.base_url):
                return ""
            return model.api_key
        return self.api_key


class Services(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    revision: int = Field(default=0, ge=0)
    model: ModelService
    search: SearchService
    keep_original_caf: bool = False
    speech: SpeechSettings = Field(default_factory=SpeechSettings)

    def public(self):
        return {"revision": self.revision, "keep_original_caf": self.keep_original_caf,
                "model": self.model.public(), "speech": self.speech.public(),
                "search": {**self.search.model_dump(exclude={"api_key"}), "api_key_configured": bool(self.search.api_key),
                           "effective_key_configured": bool(self.search.key(self.model))}}

    def capabilities(self):
        return {"model_configured": self.model.configured,
                "model_enabled": self.model.enabled and self.model.configured,
                "search_configured": bool(self.search.key(self.model)),
                "search_enabled": self.search.enabled and bool(self.search.key(self.model)),
                "model_provider": self.model.provider, "model_name": self.model.name,
                "model_destination": self.model.base_url, "search_provider": self.search.provider,
                "search_destination": self.search.base_url, "speech_provider": self.speech.live_provider,
                "speech_file_provider": self.speech.file_provider,
                "settings_revision": self.revision}


class SavedServices(Services):
    # Older files contain only the active model. Keep inactive credentials out
    # of the runtime configuration and expose only masked metadata to the UI.
    model_profiles: list[ModelService] = Field(default_factory=list)


class ModelUpdate(ModelService):
    api_key: str | None = Field(default=None, max_length=4096, repr=False)
    clear_api_key: bool = False

    @field_validator("api_key", mode="before")
    @classmethod
    def optional_key(cls, value):
        return "" if value is None else value


class SearchUpdate(SearchService):
    api_key: str | None = Field(default=None, max_length=4096, repr=False)
    clear_api_key: bool = False

    @field_validator("api_key", mode="before")
    @classmethod
    def optional_key(cls, value):
        return "" if value is None else value


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=0)
    model: ModelUpdate
    search: SearchUpdate
    keep_original_caf: bool = False
    # Ignore echoed fields from clients using Services.model_dump(). ASR has its own write path.
    speech: dict | None = Field(default=None, exclude=True, repr=False)


def from_environment(env=None):
    env = dict(os.environ) if env is None else env
    yes = lambda key: env.get(key, "true").lower() in {"1", "true", "yes"}
    provider = "deepseek" if env.get("ZSJH_LIVE_MODEL_PROVIDER", "deepseek") == "deepseek" else "openai_compatible"
    token_setting = env.get("ZSJH_LIVE_MODEL_MAX_TOKENS", "auto").strip().lower()
    max_tokens = None if token_setting in {"", "auto"} else int(token_setting)
    token_parameter = "auto"
    if max_tokens is not None:
        token_parameter = "max_tokens" if provider == "deepseek" else "max_completion_tokens"
    model = ModelService(provider=provider, base_url=env.get("ZSJH_LIVE_MODEL_ENDPOINT") or "https://api.deepseek.com",
        name=env.get("ZSJH_LIVE_MODEL_NAME", "deepseek-flash"), api_key=env.get("ZSJH_LIVE_MODEL_API_KEY", ""),
        enabled=yes("ZSJH_WB_MODEL_APPROVED"), json_mode=provider == "deepseek",
        thinking=env.get("ZSJH_LIVE_MODEL_THINKING", "disabled"),
        reasoning_effort=env.get("ZSJH_LIVE_MODEL_REASONING_EFFORT", "default"),
        max_tokens=max_tokens,
        timeout_seconds=int(env.get("ZSJH_LIVE_MODEL_TIMEOUT_SECONDS", "180")),
        token_parameter=token_parameter)
    search = SearchService(provider=env.get("ZSJH_WB_SEARCH_PROVIDER", "deepseek"),
        base_url=env.get("ZSJH_WB_SEARCH_ENDPOINT") or "https://api.deepseek.com",
        api_key=env.get("ZSJH_WB_SEARCH_API_KEY", ""), enabled=yes("ZSJH_WB_SEARCH_APPROVED"),
        use_model_key=not bool(env.get("ZSJH_WB_SEARCH_API_KEY")) and env.get("ZSJH_WB_SEARCH_PROVIDER", "deepseek") == "deepseek")
    return Services(model=model, search=search)


class ProviderSettings:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.current = None
        self.model_profiles = []
        self.load_error = ""
        if self.path and (self.path.exists() or self.path.is_symlink()):
            try:
                self._check_file()
                require(self.path.stat().st_size <= 4 * 1024 * 1024, "设置文件过大", 409)
                saved = SavedServices.model_validate_json(self.path.read_text(encoding="utf-8"))
                self.current = Services.model_validate(saved.model_dump(exclude={"model_profiles"}))
                self.model_profiles = saved.model_profiles
                self.path.chmod(0o600)
            except (OSError, ValueError, LiveError):
                self.load_error = "已保存的服务设置无法读取，联网能力已关闭；请检查后重新保存。"
        if self.current is None:
            try:
                self.current = from_environment()
            except ValueError:
                self.load_error = self.load_error or "旧服务配置格式无效，联网能力已关闭；请重新填写设置。"
                self.current = Services(model=ModelService(), search=SearchService())
            if self.load_error:
                self.current = self.current.model_copy(update={
                    "model": self.current.model.model_copy(update={"enabled": False}),
                    "search": self.current.search.model_copy(update={"enabled": False})})
        self.model_profiles = self._profiles_for(self.current.model)

    def _profiles_for(self, active):
        profiles = [active] if active.name else []
        for profile in self.model_profiles:
            if profile.profile_key() == active.profile_key():
                continue
            # Rotating or clearing the official account key applies to Flash and Pro.
            if active.official_deepseek() and profile.official_deepseek():
                profile = profile.model_copy(update={"api_key": active.api_key})
            profiles.append(profile)
        return profiles

    def _check_file(self):
        info = self.path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(), "设置文件必须是当前用户的普通文件", 409)

    def public(self):
        with self.lock:
            return {**self.current.public(), "model_profiles": [p.public() for p in self.model_profiles],
                    "editable": self.path is not None, "warning": self.load_error}

    @staticmethod
    def _key(update, previous):
        require(not (update.clear_api_key and update.api_key), "清除与替换密钥不能同时进行", 400)
        if update.clear_api_key:
            return ""
        if update.api_key:
            require(all(32 < ord(c) < 127 for c in update.api_key), "APIKey 格式无效", 400)
            return update.api_key
        if previous and update.provider == previous.provider and origin(update.base_url) == origin(previous.base_url):
            return previous.api_key
        # Never forward an old credential to a newly selected destination.
        return ""

    def preview(self, update):
        with self.lock:
            require(update.revision == self.current.revision, "设置已在另一处更新，请重新载入后再保存。", 409)
            selected = next((p for p in self.model_profiles if p.profile_key() == update.model.profile_key()), None)
            if update.model.official_deepseek():
                # Official root and /v1 use the same credential across model names.
                selected = next((p for p in self.model_profiles if p.official_deepseek()), None)
            if (selected is None and update.model.provider == self.current.model.provider
                    and update.model.base_url == self.current.model.base_url):
                # A new model name at the same service can use the current account.
                # Keep complete paths distinct: gateways can route tenants by path.
                selected = self.current.model
            model = ModelService(**update.model.model_dump(exclude={"api_key", "clear_api_key"}),
                                 api_key=self._key(update.model, selected))
            search = SearchService(**update.search.model_dump(exclude={"api_key", "clear_api_key"}),
                                   api_key=self._key(update.search, self.current.search))
            if search.use_model_key:
                require(search.provider == model.provider == "deepseek" and origin(search.base_url) == origin(model.base_url),
                        "共用密钥仅适用于同一地址的 DeepSeek 模型与搜索。", 400)
            # Clients predating the archive preference save only model/search settings.
            keep_caf = update.keep_original_caf if "keep_original_caf" in update.model_fields_set else self.current.keep_original_caf
            return Services(revision=self.current.revision + 1, model=model, search=search,
                            keep_original_caf=keep_caf, speech=self.current.speech)

    def commit(self, candidate):
        with self.lock:
            require(self.path is not None, "当前服务未启用持久化设置", 409)
            require(candidate.revision == self.current.revision + 1, "设置版本已变更，请重新载入", 409)
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.exists() or self.path.is_symlink():
                self._check_file()
            profiles = self._profiles_for(candidate.model)
            # The active model is already persisted by Services itself.
            inactive = [p.model_dump() for p in profiles if p.profile_key() != candidate.model.profile_key()]
            encoded = json.dumps({**candidate.model_dump(), "model_profiles": inactive}, ensure_ascii=False, indent=2) + "\n"
            require(len(encoded.encode("utf-8")) <= 4 * 1024 * 1024, "模型配置列表过大", 409)
            fd, temporary = tempfile.mkstemp(prefix=".providers-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self.current = candidate
            self.model_profiles = profiles
            self.load_error = ""
