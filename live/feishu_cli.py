"""Feishu desktop integration. Only this adapter starts the official CLI.

FeishuCLI.status(verify=True) -> {available,version,configured,connected,account_name,account_key,message}
FeishuCLI.auth_start() -> {verification_url,expires_in}; auth_poll() -> {state,message}
FeishuCLI.validate_folder(value) -> {token,name}; search_chats(query) -> [{id,name}]
FeishuCLI.validate_chat(chat_id) -> {id,name}
FeishuCLI.create_document(folder_token,title,markdown) -> {document_id,url}
FeishuCLI.send_message(chat_id,text,idempotency_key) -> {message_id}
FeishuError(LiveError): kind, uncertain. No raw CLI output reaches UI/logs.
"""

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from live.store import LiveError


MIN_VERSION = (1, 0, 100)
SCOPES = (
    "docx:document:create drive:drive.metadata:readonly im:chat:read im:message.send_as_user im:message offline_access"
)
SETUP = "请先在终端运行 lark-cli config init 配置自己的飞书应用，再回到这里连接账号。"
FOLDER = re.compile(r"[A-Za-z0-9]{8,100}")
CHAT = re.compile(r"oc_[A-Za-z0-9_-]{4,128}")
DOC = re.compile(r"[A-Za-z0-9]{5,128}")


class FeishuError(LiveError):
    def __init__(self, message, kind="invalid", uncertain=False):
        super().__init__(message, 400 if kind == "invalid" else 503, kind=kind, uncertain=uncertain)
        self.kind = kind
        self.uncertain = uncertain


def official_url(raw):
    if not isinstance(raw, str) or len(raw) > 4000 or re.search(r"[\s\\\x00-\x1f\x7f]", raw):
        return False
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        return (
            parts.scheme == "https"
            and not parts.username
            and not parts.password
            and parts.port in (None, 443)
            and any(
                host == base or host.endswith("." + base) for base in ("feishu.cn", "larksuite.com", "larkoffice.com")
            )
        )
    except ValueError:
        return False


def folder_token(raw):
    if not isinstance(raw, str):
        raise FeishuError("请输入飞书云盘文件夹链接。")
    value = raw.strip()
    if FOLDER.fullmatch(value):
        return value
    if official_url(value):
        match = re.fullmatch(r"/(?:drive/)?folder/([A-Za-z0-9]{8,100})/?", urlsplit(value).path)
        if match:
            return match[1]
    raise FeishuError("请输入飞书云盘文件夹的链接或标识；单篇文档和群邀请链接无法作为保存位置。")


def _native_binary():
    # A Finder-launched app has no npm PATH. Only consider known install prefixes,
    # and an actual, executable Mach-O owned by root/current user, never a JS wrapper.
    user = Path.home()
    nvm_prefixes = sorted((user / ".nvm/versions/node").glob("v[0-9]*"), reverse=True)
    for prefix in (user / ".local", user / ".npm-global", Path("/opt/homebrew"), Path("/usr/local"), *nvm_prefixes):
        for candidate in (prefix / "lib/node_modules/@larksuite/cli/bin/lark-cli", prefix / "bin/lark-cli"):
            try:
                path = candidate.resolve(strict=True)
                st = path.stat()
                if not path.is_relative_to(prefix.resolve()) or not stat.S_ISREG(st.st_mode):
                    continue
                if st.st_uid not in (0, os.getuid()) or st.st_mode & 0o022 or not os.access(path, os.X_OK):
                    continue
                with path.open("rb") as stream:
                    if stream.read(4) in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
                        return path
            except (OSError, RuntimeError):
                continue
    raise FeishuError("未找到飞书 CLI 原生程序。请安装官方 lark-cli 1.0.100 或以上版本。", "unavailable")


def _safe_text(raw, limit):
    return "".join(ch for ch in str(raw or "") if ord(ch) >= 32 and ch != "\x7f")[:limit].strip()


def _plain_markdown(value):
    # Feishu Markdown accepts local/remote media and DocxXML elements. Preserve
    # text, headings, blockquotes and regular links, disabling resource syntax.
    return value.replace("<", "&lt;").replace("![", "&#33;[")


def _failure(result, ambiguous):
    if not isinstance(result, dict):
        result = {}
    kind = result.get("type", "")
    sub = result.get("subtype", "")
    message = str(result.get("message", "")).lower()
    code = result.get("code")
    suffix = f"（飞书 {code}）" if isinstance(code, int) and code != 0 else ""
    if sub == "not_configured":
        return FeishuError(SETUP, "not_configured")
    if kind in ("authentication", "auth") or sub in ("token_expired", "no_user_token"):
        return FeishuError("飞书用户授权已失效或未完成，请重新连接飞书。" + suffix, "authorization")
    if (
        sub == "missing_scope"
        or kind in ("authorization", "permission")
        or "forbidden" in message
        or "permission" in message
    ):
        scopes = [
            s for s in (result.get("missing_scopes") or []) if isinstance(s, str) and re.fullmatch(r"[a-z0-9_.:]+", s)
        ]
        hint = " 缺少权限：" + "、".join(scopes[:5]) if scopes else ""
        return FeishuError(
            "飞书权限不足，请检查应用授权、目标文件夹的编辑权限和群成员身份。" + suffix + hint, "permission"
        )
    if "out of the chat" in message or "not in" in message and "chat" in message:
        return FeishuError("当前用户不在目标群，请加入该群或重新选择。" + suffix, "permission")
    if kind in ("network", "transport", "timeout") or any(
        s in message
        for s in ("timeout", "timed out", "connection reset", "unexpected eof", "tls handshake", "deadline exceeded")
    ):
        return FeishuError("飞书连接中断或超时，请检查网络。" + suffix, "network", ambiguous)
    # Includes inconclusive API-side failures, async creation failures and bad output.
    if kind == "validation":
        return FeishuError("飞书拒绝了请求参数，请核对文件夹、群聊或 CLI 版本。" + suffix, "invalid")
    return FeishuError("飞书调用未成功，请检查账号及目标权限。" + suffix, "upstream", ambiguous)


class FeishuCLI:
    def __init__(self):
        self._auth_lock = threading.Lock()
        self._auth = None

    def _run(self, args, *, content=None, timeout=25, ambiguous=False, plain=False):
        binary = _native_binary()
        env = {
            k: v
            for k, v in os.environ.items()
            if k in ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "__CF_USER_TEXT_ENCODING")
        }
        env.update(
            PATH="/usr/bin:/bin:/usr/sbin:/sbin",
            LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1",
            LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1",
        )
        try:
            with tempfile.TemporaryDirectory(prefix="zsjh-feishu-") as cwd:
                p = subprocess.run(
                    [str(binary), *args],
                    cwd=cwd,
                    env=env,
                    input=content or "",
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    encoding="utf-8",
                    errors="replace",
                )
        except subprocess.TimeoutExpired:
            raise FeishuError("等待飞书响应超时，请检查网络和同步状态。", "timeout", ambiguous) from None
        except OSError:
            raise FeishuError("无法启动飞书 CLI，请检查本机安装。", "unavailable") from None
        if plain and not p.returncode:
            return p.stdout.strip()
        value = {}
        for stream in (p.stderr, p.stdout) if p.returncode else (p.stdout,):
            # Some CLI versions prefix the JSON error envelope with a diagnostic line.
            for marker in re.finditer(r"^\s*\{", stream, re.M):
                try:
                    parsed, _ = json.JSONDecoder().raw_decode(stream[marker.start() :].lstrip())
                except ValueError:
                    continue
                if isinstance(parsed, dict) and (not p.returncode or "error" in parsed):
                    value = parsed
                    break
            if value:
                break
        if p.returncode or value.get("ok") is False:
            raise _failure(value.get("error") or {}, ambiguous)
        if not value:
            raise FeishuError("飞书未返回可确认的结果。", "upstream", ambiguous)
        return value

    def _version(self):
        version = self._run(["--version"], timeout=5, plain=True)
        match = re.fullmatch(r"lark-cli version (\d+)\.(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < MIN_VERSION:
            raise FeishuError("飞书 CLI 版本过低或不受支持，请升级到 1.0.100 或以上稳定版。", "version")
        return ".".join(match.groups())

    def status(self, verify=True):
        state = dict(
            available=False, version="", configured=False, connected=False, account_key="", account_name="", message=""
        )
        try:
            state["version"] = self._version()
            state["available"] = True
            result = self._run(["auth", "status", "--json"] + (["--verify"] if verify else []))
        except FeishuError as exc:
            state["message"] = str(exc)
            return state
        app = result.get("appId")
        state["configured"] = bool(app)
        if not app:
            state["message"] = SETUP
            return state
        user = (result.get("identities") or {}).get("user") or {}
        oid = user.get("openId")
        if not oid or not user.get("available") or (verify and user.get("verified") is not True):
            state["message"] = (
                "暂时无法核实飞书用户身份，请检查网络或重新授权。"
                if user.get("available")
                else "飞书应用已配置，请连接你的飞书账号。"
            )
            return state
        account_key = hashlib.sha256(f"{result.get('brand')}:{app}:{oid}".encode()).hexdigest()
        state.update(
            connected=True,
            account_name=_safe_text(user.get("userName"), 150) or "飞书用户",
            account_key=account_key,
            message="飞书用户已连接。",
        )
        return state

    def auth_start(self):
        with self._auth_lock:
            existing = self._auth or {}
            if existing.get("state") == "pending" and existing["deadline"] > time.monotonic():
                return {"verification_url": existing["url"], "expires_in": int(existing["deadline"] - time.monotonic())}
            self._version()
            res = self._run(["auth", "login", "--scope", SCOPES, "--no-wait", "--json"])
            url, code = res.get("verification_url"), res.get("device_code")
            if not official_url(url) or not isinstance(code, str) or not code or len(code) > 2048:
                raise FeishuError("飞书未返回有效的授权链接，请重新连接。", "authorization")
            try:
                seconds = max(1, min(int(res.get("expires_in", 300)), 3600))
            except (ValueError, TypeError):
                raise FeishuError("飞书授权有效期无效，请重新连接。", "authorization") from None
            flow = dict(state="pending", message="请在飞书页面完成授权。", url=url, deadline=time.monotonic() + seconds)
            self._auth = flow
            threading.Thread(
                target=self._finish_auth, args=(flow, code, seconds), daemon=True, name="feishu-login"
            ).start()
            return {"verification_url": url, "expires_in": seconds}

    def _finish_auth(self, flow, code, seconds):
        try:
            self._run(["auth", "login", "--device-code", code, "--json"], timeout=seconds + 5)
            result = self.status()
            state = "authorized" if result["connected"] else "failed"
            message = result["message"]
        except FeishuError as exc:
            state, message = "failed", str(exc)
        with self._auth_lock:
            if self._auth is flow:
                self._auth = {"state": state, "message": message}

    def auth_poll(self):
        with self._auth_lock:
            flow = self._auth or {"state": "idle", "message": "请先点击连接飞书。"}
            if flow["state"] == "pending" and flow["deadline"] <= time.monotonic():
                return {"state": "expired", "message": "授权链接已过期，请重新连接。"}
            return {"state": flow["state"], "message": flow["message"]}

    def validate_folder(self, value):
        token = folder_token(value)
        data = json.dumps({"request_docs": [{"doc_type": "folder", "doc_token": token}]})
        res = self._run(["drive", "metas", "batch_query", "--as", "user", "--data", data])
        for row in (res.get("data") or {}).get("metas", []):
            if row.get("doc_type") == "folder" and row.get("doc_token") == token and row.get("title"):
                return {"token": token, "name": _safe_text(row["title"], 240)}
        raise FeishuError("未能读取这个文件夹，请确认链接，并给当前飞书账号文件夹访问及编辑权限。", "permission")

    def search_chats(self, query):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 64:
            raise FeishuError("请输入 1 至 64 字的群名称。")
        res = self._run(
            [
                "im",
                "+chat-search",
                "--as",
                "user",
                "--query=" + query.strip(),
                "--search-types",
                "private,external,public_joined",
                "--page-size",
                "20",
                "--disable-search-by-user",
            ]
        )
        return [
            {"id": c["chat_id"], "name": _safe_text(c.get("name"), 180)}
            for c in (res.get("data") or {}).get("chats", [])
            if CHAT.fullmatch(c.get("chat_id", ""))
            and c.get("name")
            and c.get("chat_status") not in ("dissolved", "dissolved_save")
        ]

    def validate_chat(self, chat_id):
        if not isinstance(chat_id, str) or not CHAT.fullmatch(chat_id):
            raise FeishuError("请选择飞书群。")
        res = self._run(["im", "chats", "get", "--as", "user", "--chat-id", chat_id])
        data = res.get("data") or {}
        if not data.get("name") or data.get("chat_status") in ("dissolved", "dissolved_save"):
            raise FeishuError("群聊不可用，请重新选择。", "permission")
        return {"id": chat_id, "name": _safe_text(data["name"], 180)}

    def create_document(self, folder_token, title, markdown):
        if not FOLDER.fullmatch(folder_token) or not markdown.strip():
            raise FeishuError("目标文件夹或查证文档无效。")
        res = self._run(
            [
                "docs",
                "+create",
                "--as",
                "user",
                "--parent-token",
                folder_token,
                "--title=" + _safe_text(title, 230),
                "--doc-format",
                "markdown",
                "--content",
                "-",
            ],
            content=_plain_markdown(markdown),
            timeout=90,
            ambiguous=True,
        )
        doc = (res.get("data") or {}).get("document") or {}
        if not DOC.fullmatch(doc.get("document_id", "")) or not official_url(doc.get("url")):
            raise FeishuError("飞书可能已创建文档，但缺少可确认的文档链接，请先查看目标文件夹。", "unknown", True)
        return {"document_id": doc["document_id"], "url": doc["url"]}

    def send_message(self, chat_id, text, idempotency_key):
        if not CHAT.fullmatch(chat_id) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", idempotency_key):
            raise FeishuError("飞书群或消息标识无效。")
        res = self._run(
            [
                "im",
                "+messages-send",
                "--as",
                "user",
                "--chat-id",
                chat_id,
                "--text",
                "-",
                "--idempotency-key",
                idempotency_key,
            ],
            content=text,
            timeout=35,
            ambiguous=True,
        )
        mid = (res.get("data") or {}).get("message_id", "")
        if not isinstance(mid, str) or not mid.startswith("om_"):
            raise FeishuError("飞书未返回可确认的消息 ID，请检查目标群。", "unknown", True)
        return {"message_id": mid}
