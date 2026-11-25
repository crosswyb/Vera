"""Bounded failure facts and remedies; never retain exception messages or payloads."""

import errno
import json
import sqlite3

import requests


STAGES = {"detecting": "问题识别", "synthesizing": "整理回答", "searching": "搜索资料",
          "reading": "读取原文", "summarizing": "生成纪要", "publishing": "保存文档"}
REASONS = {
    "read_timeout": ("响应超时", "等待服务响应时发生读取超时，未收到完整结果。", "对照实际耗时检查网络和服务状态；读取超时不等同于上游限流。"),
    "connect_timeout": ("连接超时", "在 {timeout} 秒连接时限内未能建立服务连接。", "检查网络和服务地址后重试。"),
    "tls_handshake_timeout": ("安全连接握手超时", "在 {timeout} 秒连接时限内未完成 HTTPS 安全握手，尚未取得模型响应。", "检查网络、VPN 和服务接入节点；这不是模型生成超时或限流的证据。"),
    "tls_error": ("安全连接失败", "TLS 证书或安全连接校验失败。", "检查系统时间、证书和服务地址后重试。"),
    "dns_error": ("域名解析失败", "无法解析服务地址的域名。", "检查 BaseURL 和本机 DNS 或网络连接。"),
    "connection_refused": ("连接被拒绝", "目标服务拒绝建立连接。", "确认服务已启动，端口和 BaseURL 正确。"),
    "network_unreachable": ("网络不可达", "本机无法到达目标服务所在的网络。", "检查网络、VPN、路由和服务地址后重试。"),
    "proxy_error": ("代理连接失败", "无法通过代理连接服务。", "检查代理设置和网络连接。"),
    "connection_interrupted": ("连接中断", "接收响应时连接中断，未收到完整结果。", "检查网络或服务状态后手动重试。"),
    "connection_error": ("连接失败", "无法完成与服务的连接。", "检查网络、BaseURL 和服务状态后重试。"),
    "request_timeout": ("请求超时", "请求等待超时。", "检查服务状态和等待时限后重试。"),
    "response_too_large": ("响应过大", "响应超过应用的 1 MB 接收上限。", "缩小问题范围，或使用输出更简洁的模型。"),
    "response_deadline": ("响应接收超时", "达到应用的 {timeout} 秒总等待时限，响应尚未接收完成。", "若仍在持续接收推理，可增加等待时限，或选择响应更快的模型/推理强度；这不是限流的证据。"),
    "request_interrupted": ("请求被中断", "应用退出或执行器停止时，请求尚未完成。", "重新打开应用后，可手动重试此问题。"),
    "unknown_outcome": ("未收到完整结果", "旧记录未保留具体错误，无法进一步判断原因。", "可手动重试；新请求会保留具体错误和失败环节。"),
    "budget_exhausted": ("调用额度已用完", "已达到本机设置的调用次数上限。", "在设置中检查调用额度。"),
    "unexpected_error": ("应用处理异常", "应用处理请求时发生异常。", "查看错误详情；重试仍失败时请保留运行记录。"),
}


class RequestOutcomeUnknown(RuntimeError):
    def __init__(self, message="request outcome unknown", *, diagnostic=None):
        super().__init__("request outcome unknown")
        self.diagnostic = diagnostic


def request_details(code, service="model", *, timeout=None, elapsed_ms=None, request_id=None):
    code = code if code in REASONS else "unexpected_error"
    label, message, suggestion = REASONS[code]
    name = "搜索服务" if service == "search" else "模型服务"
    result = {"category": "request", "code": code, "service": service, "label": name + label,
              "message": name + message.format(timeout=timeout if type(timeout) is int else "配置的"),
              "suggestion": suggestion}
    for field, value in (("timeout_seconds", timeout), ("elapsed_ms", elapsed_ms), ("request_id", request_id)):
        if type(value) is int and value >= 0:
            result[field] = value
    if service == "search" and code in {"read_timeout", "response_deadline"}:
        result["suggestion"] = "检查搜索服务状态和网络连接后重试此问题。"
    return result


def transport_details(exc, service, *, timeout, elapsed_ms, request_id, connect_timeout=5, response_received=False):
    # urllib3 often wraps DNS errors/read timeouts in ConnectionError. Inspect
    # exception types and links only; str(exc) may contain keys, URLs or prompts.
    pending, names, seen, errnos, frames = [exc], set(), set(), set(), set()
    while pending and len(seen) < 20:
        item = pending.pop()
        if id(item) in seen or not isinstance(item, BaseException):
            continue
        seen.add(id(item)); names.add(type(item).__name__)
        if type(getattr(item, "errno", None)) is int:
            errnos.add(item.errno)
        trace = item.__traceback__
        for _ in range(64):
            if trace is None:
                break
            # Function identities distinguish handshake vs response timeouts.
            # Never retain traceback text, locals, URLs or exception messages.
            frames.add(trace.tb_frame.f_code.co_name)
            trace = trace.tb_next
        pending.extend([item.__cause__, item.__context__, getattr(item, "reason", None)])
        pending.extend(arg for arg in item.args if isinstance(arg, BaseException))
    handshake = not response_received and bool(frames & {"do_handshake", "_validate_conn", "_ssl_wrap_socket_and_match_hostname", "ssl_wrap_socket"})
    code = ("tls_handshake_timeout" if handshake and names & {"ReadTimeout", "ReadTimeoutError"} else
            "tls_error" if isinstance(exc, requests.exceptions.SSLError) else
            "connect_timeout" if isinstance(exc, requests.ConnectTimeout) else
            "read_timeout" if isinstance(exc, requests.ReadTimeout) or "ReadTimeoutError" in names else
            "dns_error" if names & {"gaierror", "NameResolutionError"} else
            "network_unreachable" if errnos & {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN} else
            "connection_refused" if "ConnectionRefusedError" in names or errno.ECONNREFUSED in errnos else
            "proxy_error" if isinstance(exc, requests.exceptions.ProxyError) else
            "connection_interrupted" if isinstance(exc, requests.exceptions.ChunkedEncodingError) else
            "request_timeout" if isinstance(exc, requests.Timeout) else "connection_error")
    phase = ("tls_handshake" if code in {"tls_handshake_timeout", "tls_error"} else
             "resolving" if code == "dns_error" else "connecting" if code in {"connect_timeout", "connection_refused", "network_unreachable"} else
             "receiving" if response_received else "waiting_response")
    details = request_details(code, service,
        timeout=connect_timeout if code in {"connect_timeout", "tls_handshake_timeout"} else timeout,
        elapsed_ms=elapsed_ms, request_id=request_id)
    details.update(transport_phase=phase, connect_timeout_seconds=connect_timeout)
    return details


def describe_failure(exc, stage, service="model"):
    from live.output_validation import failure_details
    diagnostic = getattr(exc, "diagnostic", None)
    if isinstance(diagnostic, dict):
        return {**diagnostic, "stage": stage}
    contract_details = getattr(exc, "details", None)
    if isinstance(exc, ValueError) and isinstance(contract_details, dict) and contract_details.get("code"):
        return failure_details(exc, stage)
    status = getattr(exc, "status", None)
    if type(status) is int and 100 <= status <= 599:
        labels = {401: "鉴权失败", 403: "访问被拒绝", 404: "接口或模型不存在", 429: "限流或额度不足"}
        details = {"category": "request", "code": "http_error", "service": service, "stage": stage,
                   "http_status": status, "label": ("搜索" if service == "search" else "模型") + labels.get(status, f"服务错误 HTTP {status}"),
                   "message": exc.public_message() if callable(getattr(exc, "public_message", None)) else f"服务返回 HTTP {status}，未接受本次请求。",
                   "suggestion": "在设置中检查服务配置后，重试此问题。"}
        if getattr(exc, "code", ""):
            details["provider_code"] = exc.code
        return details
    if isinstance(exc, ValueError):
        return failure_details(exc, stage)
    code = "unknown_outcome" if isinstance(exc, RequestOutcomeUnknown) else "budget_exhausted" if isinstance(exc, RuntimeError) and "limit reached" in str(exc) else "unexpected_error"
    return {**request_details(code, service), "stage": stage}


def historical_question_details(question, events, ledger):
    """Read-only enrichment, only when all matching historical facts agree.

    Old accounting had no question/request link. Never invent an association
    or display a request ID from a concurrent call. Mixed/absent evidence stays
    explicitly unknown. No historical state is rewritten and no call is sent.
    """
    failures = [e for e in events if e["type"] == "research.failed" and e["payload"].get("question_id") == question["id"]]
    if not failures:
        return None
    failed = failures[-1]
    if abs(failed["received_at"] - question.get("updated_at", 0)) > 1:
        return None
    previous = [e for e in events if e["seq"] < failed["seq"] and e["payload"].get("question_id") == question["id"]]
    phase = next((e for e in reversed(previous) if e["type"] in {"research.synthesizing", "research.searching", "research.started"}), None)
    if not phase or phase["type"] != "research.synthesizing":
        return None
    start, end = phase["received_at"], failed["received_at"]
    # A reservation immediately follows the stage transition in the old
    # synchronous call path. A wider window would include unrelated calls.
    candidates = [r for r in ledger if start - .02 <= r["created"] <= start + .1]
    if not candidates:
        return None
    if question["state"] == "outcome_unknown" and 44 <= end - start <= 49 and all(r["usage"].get("error_kind") == "ReadTimeout" for r in candidates):
        return {**request_details("read_timeout", timeout=45, elapsed_ms=round((end - start) * 1000)),
                "stage": "synthesizing", "provenance": "historical_ledger"}
    details = question.get("error_details") or {}
    if (question["state"] == "failed" and any(i.get("code") == "output_incomplete" for i in details.get("issues", []))
            and all(r["usage"].get("completion_tokens") == 2048 for r in candidates)):
        return {**details, "response": {"completion_tokens": 2048}, "provenance": "historical_ledger"}
    return None


def read_historical_ledger(path):
    if not path.is_file():
        return []
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.2) as db:
            result = []
            for row in db.execute("SELECT created,usage FROM requests ORDER BY id DESC LIMIT 10000"):
                usage = json.loads(row[1]) if row[1] else {}
                if type(row[0]) in {int, float} and isinstance(usage, dict):
                    result.append({"created": row[0], "usage": usage})
            return result
    except (sqlite3.Error, ValueError, OSError):
        return []
