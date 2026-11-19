"""Provider transport metadata and bounded, allowlisted HTTP diagnostics."""

import hashlib
import json
import threading
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

class ModelHTTPError(RuntimeError):
    """HTTP status shared by transport diagnostics, independent of legacy agents."""

    def __init__(self, status):
        self.status = status
        super().__init__("model HTTP " + str(status))


GO_PATHS = {"/zen/go/v1", "/inference/go/v1", "/inference/go/openai/v1"}
ERROR_CODES = {"MissingSessionID", "api_key_expired", "invalid_api_key", "authentication_error",
               "invalid_request_error", "unsupported_parameter", "invalid_parameter", "model_not_found",
               "permission_denied", "rate_limit_exceeded", "insufficient_quota"}
PARAMETERS = {"model": "模型名称", "max_tokens": "单次输出上限", "max_completion_tokens": "单次输出上限",
              "reasoning_effort": "推理强度", "response_format": "JSON 输出格式", "messages": "消息格式",
              "thinking": "思考开关", "stream": "流式输出"}


class ModelSessions:
    """Reuse urllib3 connections per worker; never share mutable Sessions.

    Retiring a configuration closes idle pools immediately. A pinned in-flight
    task may still finish its calls, with its sessions closed on release.
    """
    def __init__(self):
        self.local = threading.local()
        self.lock = threading.Lock()
        self.sessions, self.busy = set(), set()
        self.retired = False

    @contextmanager
    def acquire(self):
        with self.lock:
            session = getattr(self.local, "session", None)
            if session not in self.sessions or self.retired:
                session = requests.Session()
                session.trust_env = False
                # Adapter defaults disable implicit retries of paid POSTs.
                self.sessions.add(session)
                self.local.session = session
            self.busy.add(session)
        session.cookies.clear()
        try:
            yield session
        finally:
            with self.lock:
                self.busy.discard(session)
                closing = self.retired
                if closing:
                    self.sessions.discard(session)
            if closing:
                session.close()

    def close(self):
        with self.lock:
            self.retired = True
            idle = self.sessions - self.busy
            self.sessions.difference_update(idle)
        for session in idle:
            session.close()


def retry_after_seconds(response):
    """Read only the standard backpressure header, bounded to five minutes."""
    value = getattr(response, "headers", {}).get("Retry-After", "")
    try:
        seconds = int(value) if value.isdigit() else int(parsedate_to_datetime(value).timestamp() - time.time())
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None
    return min(300, max(0, seconds))


def model_headers(config, conversation_id):
    headers = {"Authorization": "Bearer " + config.api_key} if config.api_key else {}
    url = urlsplit(config.base_url)
    if (url.scheme == "https" and url.hostname == "opencode.ai" and url.port in {None, 443}
            and url.path.rstrip("/") in GO_PATHS):
        # Stable across tasks, retries, settings changes and app restarts. Never
        # put a meeting title, transcript, credential or user name in a header.
        session_id = hashlib.sha256(("ZSJHWorkbench:" + conversation_id).encode()).hexdigest()
        headers.update({"User-Agent": "ZSJHWorkbench/0.1.0", "x-opencode-session": "zsjh-" + session_id})
    return headers


class ModelRequestError(ModelHTTPError):
    def __init__(self, status, code="", parameter=""):
        super().__init__(status)
        self.code = code if isinstance(code, str) and code in ERROR_CODES else ""
        self.parameter = parameter if isinstance(parameter, str) and parameter in PARAMETERS else ""

    def public_message(self):
        if self.code == "MissingSessionID":
            reason = "请求缺少 OpenCode Go 会话标识 x-opencode-session，请更新客户端适配。"
        elif self.code == "api_key_expired":
            reason = "APIKey 已过期，请在 OpenCode 控制台创建新密钥并替换；重新登录不会续期旧密钥。"
        elif self.code in {"invalid_api_key", "authentication_error"} or self.status == 401:
            reason = "APIKey 校验失败，请检查密钥是否有效或已过期。"
        elif self.status == 400:
            reason = (f"服务拒绝了参数 {self.parameter}（{PARAMETERS[self.parameter]}），请按服务商支持范围调整。"
                      if self.parameter else "请求参数被服务拒绝，请检查接口类型与高级参数。")
        elif self.status == 403:
            reason = "服务拒绝访问，请检查模型权限与账户策略。"
        elif self.status == 404:
            reason = "接口地址或模型不存在，请检查 BaseURL 和模型名称。"
        elif self.status == 429:
            reason = "已达到服务的速率或用量限制，请稍后重试并检查账户额度。"
        elif 500 <= self.status < 600:
            reason = "服务端暂时出错，请稍后重试。"
        else:
            reason = "服务未接受请求，请核对接口地址与服务状态。"
        code = f"（{self.code}）" if self.code else ""
        return f"服务返回 HTTP {self.status}{code}：{reason}"


def model_http_error(response):
    """Never retain raw messages: some gateways echo keys or private prompts."""
    fallback = ModelRequestError(response.status_code)
    if not 400 <= response.status_code < 600:
        return fallback
    try:
        chunks, size, started = [], 0, time.monotonic()
        for chunk in response.iter_content(2048):
            size += len(chunk)
            if size > 8192 or time.monotonic() - started > 5:
                return fallback
            chunks.append(chunk)
        data = json.loads(b"".join(chunks))
    except (ValueError, requests.RequestException):
        # An HTTP error was received even when its optional body is unreadable.
        return fallback
    if not isinstance(data, dict):
        return fallback
    error = data.get("error")
    error = error if isinstance(error, dict) else data
    codes = (error.get("code"), error.get("type"), data.get("code"))
    code = next((value for value in codes if isinstance(value, str) and value in ERROR_CODES), "")
    return ModelRequestError(response.status_code, code, error.get("param"))
