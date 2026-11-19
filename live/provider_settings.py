"""Private provider configuration, separate from meeting content and permissions.

The environment is a one-time migration source. Saved settings are immutable
snapshots: one paid task keeps one snapshot even when settings change mid-call.
"""

import ipaddress
import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    max_tokens: int = Field(default=8192, ge=256, le=65536)
    timeout_seconds: int = Field(default=180, ge=15, le=600)
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
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

    def payload(self, system, content, *, summary=False, detection=False):
        fast = detection and self.detection_mode == "fast"
        tokens = min(max(self.max_tokens, 8192), 16384) if fast else max(self.max_tokens, 8192) if summary else self.max_tokens
        payload = {"model": self.name, "messages": [{"role": "system", "content": system},
                   {"role": "user", "content": json.dumps(content, ensure_ascii=False)}],
                   self.token_parameter: tokens}
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        thinking = "disabled" if fast else self.thinking
        effort = "low" if fast and self.reasoning_effort in {"medium", "high", "xhigh", "max"} else self.reasoning_effort
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

    def public(self):
        return {"revision": self.revision, "keep_original_caf": self.keep_original_caf,
                "model": {**self.model.model_dump(exclude={"api_key"}), "api_key_configured": bool(self.model.api_key)},
                "search": {**self.search.model_dump(exclude={"api_key"}), "api_key_configured": bool(self.search.api_key),
                           "effective_key_configured": bool(self.search.key(self.model))}}

    def capabilities(self):
        return {"model_configured": self.model.configured,
                "model_enabled": self.model.enabled and self.model.configured,
                "search_configured": bool(self.search.key(self.model)),
                "search_enabled": self.search.enabled and bool(self.search.key(self.model)),
                "model_provider": self.model.provider, "model_name": self.model.name,
                "model_destination": self.model.base_url, "search_provider": self.search.provider,
                "search_destination": self.search.base_url, "speech_provider": "apple_local",
                "settings_revision": self.revision}


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


def from_environment(env=None):
    env = dict(os.environ) if env is None else env
    yes = lambda key: env.get(key, "true").lower() in {"1", "true", "yes"}
    provider = "deepseek" if env.get("ZSJH_LIVE_MODEL_PROVIDER", "deepseek") == "deepseek" else "openai_compatible"
    model = ModelService(provider=provider, base_url=env.get("ZSJH_LIVE_MODEL_ENDPOINT") or "https://api.deepseek.com",
        name=env.get("ZSJH_LIVE_MODEL_NAME", "deepseek-flash"), api_key=env.get("ZSJH_LIVE_MODEL_API_KEY", ""),
        enabled=yes("ZSJH_WB_MODEL_APPROVED"), json_mode=provider == "deepseek",
        thinking=env.get("ZSJH_LIVE_MODEL_THINKING", "disabled"),
        reasoning_effort=env.get("ZSJH_LIVE_MODEL_REASONING_EFFORT", "default"),
        max_tokens=int(env.get("ZSJH_LIVE_MODEL_MAX_TOKENS", "8192")),
        timeout_seconds=int(env.get("ZSJH_LIVE_MODEL_TIMEOUT_SECONDS", "180")),
        token_parameter="max_tokens" if provider == "deepseek" else "max_completion_tokens")
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
        self.load_error = ""
        if self.path and (self.path.exists() or self.path.is_symlink()):
            try:
                self._check_file()
                require(self.path.stat().st_size <= 65536, "设置文件过大", 409)
                self.current = Services.model_validate_json(self.path.read_text(encoding="utf-8"))
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

    def _check_file(self):
        info = self.path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(), "设置文件必须是当前用户的普通文件", 409)

    def public(self):
        with self.lock:
            return {**self.current.public(), "editable": self.path is not None, "warning": self.load_error}

    @staticmethod
    def _key(update, previous):
        require(not (update.clear_api_key and update.api_key), "清除与替换密钥不能同时进行", 400)
        if update.clear_api_key:
            return ""
        if update.api_key:
            require(all(32 < ord(c) < 127 for c in update.api_key), "APIKey 格式无效", 400)
            return update.api_key
        if origin(update.base_url) == origin(previous.base_url) and update.provider == previous.provider:
            return previous.api_key
        # Never forward an old credential to a newly selected destination.
        return ""

    def preview(self, update):
        with self.lock:
            require(update.revision == self.current.revision, "设置已在另一处更新，请重新载入后再保存。", 409)
            model = ModelService(**update.model.model_dump(exclude={"api_key", "clear_api_key"}),
                                 api_key=self._key(update.model, self.current.model))
            search = SearchService(**update.search.model_dump(exclude={"api_key", "clear_api_key"}),
                                   api_key=self._key(update.search, self.current.search))
            if search.use_model_key:
                require(search.provider == model.provider == "deepseek" and origin(search.base_url) == origin(model.base_url),
                        "共用密钥仅适用于同一地址的 DeepSeek 模型与搜索。", 400)
            # Clients predating the archive preference save only model/search settings.
            keep_caf = update.keep_original_caf if "keep_original_caf" in update.model_fields_set else self.current.keep_original_caf
            return Services(revision=self.current.revision + 1, model=model, search=search, keep_original_caf=keep_caf)

    def commit(self, candidate):
        with self.lock:
            require(self.path is not None, "当前服务未启用持久化设置", 409)
            require(candidate.revision == self.current.revision + 1, "设置版本已变更，请重新载入", 409)
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.exists() or self.path.is_symlink():
                self._check_file()
            fd, temporary = tempfile.mkstemp(prefix=".providers-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(candidate.model_dump(), output, ensure_ascii=False, indent=2)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self.current = candidate
            self.load_error = ""
