"""Read OpenAI-compatible SSE without exposing raw reasoning or partial JSON.

Progress is advisory. Only the normal complete-response validation can publish
an answer. Providers returning one JSON response remain supported in-place.
"""

import json
import time

import requests
from pydantic_core import from_json

DECODED_LIMIT = 1_000_000
STREAM_WIRE_LIMIT = 16_000_000


class CompletionLimit(RuntimeError):
    def __init__(self, kind, limit):
        super().__init__("completion size limit")
        self.kind, self.limit = kind, limit


def short_answer_preview(content):
    value = content.lstrip()
    if value.startswith("```json"):
        value = value[7:].lstrip()
    elif value.startswith("```"):
        value = value[3:].lstrip()
    try:
        partial = from_json(value, allow_partial="trailing-strings")
    except ValueError:
        return ""
    brief = partial.get("brief") if isinstance(partial, dict) else None
    answer = brief.get("short_answer") if isinstance(brief, dict) else None
    return answer[:80] if isinstance(answer, str) else ""


def read_completion(response, check, progress=None):
    headers = getattr(response, "headers", {})
    streaming = "text/event-stream" in str(headers.get("Content-Type", "")).lower()
    if not streaming:
        if progress:
            progress("model_response", {"mode": "buffered", "last_response_at": time.time()})
        chunks = []
        for chunk in response.iter_content(32768):
            check(len(chunk))
            chunks.append(chunk)
        return json.loads(b"".join(chunks))

    content, reasoning_chars, finish_reason, usage = "", 0, None, {}
    decoded_bytes = 0
    last_emit, last_phase = 0.0, ""
    data = []

    def emit(force=False):
        nonlocal last_emit, last_phase
        now = time.monotonic()
        phase = "answering" if content else "thinking" if reasoning_chars else "waiting"
        if progress and (force or phase != last_phase or now - last_emit >= .4):
            progress("model_delta", {"mode": "stream", "phase": phase, "answer_chars": len(content),
                "reasoning_chars": reasoning_chars, "draft": short_answer_preview(content),
                "last_response_at": time.time()})
            last_emit, last_phase = now, phase

    def consume():
        nonlocal content, reasoning_chars, finish_reason, usage, decoded_bytes
        if not data:
            emit()
            return False
        raw = b"\n".join(data)
        data.clear()
        if len(raw) > DECODED_LIMIT:
            raise CompletionLimit("event", DECODED_LIMIT)
        if raw.strip() == b"[DONE]":
            return True
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("error"):
            raise ValueError("model response contract rejected")
        if isinstance(value.get("usage"), dict):
            usage = value["usage"]
        for choice in value.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if isinstance(text, str):
                content += text
                decoded_bytes += len(text.encode("utf-8"))
            # Count activity, never persist or send the model's private reasoning.
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str):
                reasoning_chars += len(reasoning)
                decoded_bytes += len(reasoning.encode("utf-8"))
            if decoded_bytes > DECODED_LIMIT:
                raise CompletionLimit("decoded", DECODED_LIMIT)
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
        emit()
        return False

    if progress:
        progress("model_response", {"mode": "stream", "last_response_at": time.time()})
    def lines():
        pending = bytearray()
        # Small reads matter: buffering 512 bytes can hide an entire slow SSE
        # event. Check bytes before line assembly, including newline-free data.
        for chunk in response.iter_content(chunk_size=1):
            check(len(chunk))
            pending.extend(chunk)
            while b"\n" in pending:
                end = pending.index(b"\n")
                if end > DECODED_LIMIT:
                    raise CompletionLimit("event", DECODED_LIMIT)
                yield bytes(pending[:end]).rstrip(b"\r")
                del pending[:end + 1]
            if len(pending) > DECODED_LIMIT:
                raise CompletionLimit("event", DECODED_LIMIT)
        if pending:
            yield bytes(pending)

    for line in lines():
        if not line:
            if consume():
                break
        elif line.startswith(b"data:"):
            data.append(line[5:].lstrip(b" "))
        elif line.startswith(b":"):
            emit()
    if data:
        consume()
    emit(force=True)
    if not finish_reason:
        raise requests.exceptions.ChunkedEncodingError("incomplete completion stream")
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}], "usage": usage}
