"""Local meeting deliverables, independent of question detection and search."""

import json
import re
import time
from datetime import datetime
from difflib import SequenceMatcher

from live.artifact_writer import digest
from live.recording_archive import RecordingArchive, identity
from live.store import LiveError, require, uid
from live.workbench_contracts import MeetingSummary, SUMMARY_SECTIONS


SOURCE_LABELS = {"system": "电脑声音", "microphone": "麦克风", "file": "导入音频"}


class SummaryValidationError(LiveError, ValueError):
    """A summary contract error that can be corrected before publication."""


def _summary_require(condition, message, code):
    if not condition:
        raise SummaryValidationError(message, 409, code=code)


def distinct_utterances(segments):
    """Filter likely speaker bleed in model input while keeping all archived originals."""
    kept = {}
    tracks = {}
    for segment in sorted(segments, key=lambda s: s.get('event_seq', 0)):
        source = segment.get('source')
        epoch = segment.get('capture_epoch')
        start, end = segment.get('audio_start_ms'), segment.get('audio_end_ms')
        duplicate = False
        if source in {'microphone', 'system'} and epoch and start is not None and end is not None and end > start:
            raw = segment['text'].lower()
            text = re.sub(r'[^\w]+', '', raw)
            qualifiers = re.findall(r'[a-z0-9]+|没有|未|不|无|可|只|仅|除外', raw)
            other_source = 'system' if source == 'microphone' else 'microphone'
            for other_start, other_end, other_text, other_qualifiers in reversed(tracks.get((epoch, other_source), [])):
                overlap = min(end, other_end) - max(start, other_start)
                if overlap < .65 * min(end - start, other_end - other_start) or min(len(text), len(other_text)) < 12:
                    continue
                if qualifiers == other_qualifiers and SequenceMatcher(None, text, other_text, autojunk=False).ratio() >= .88:
                    duplicate = True
                    break
            if not duplicate:
                tracks.setdefault((epoch, source), []).append((start, end, text, qualifiers))
        if not duplicate:
            kept[segment['id']] = segment
    return [s for s in segments if s['id'] in kept]


def final_segments(workbench, db, meeting):
    recordings = {r["id"]: r for r in meeting.get("recordings", [])}
    segments = [s for s in workbench._items(db, "wb_segment", meeting["id"]) if s["type"] == "transcript.final"]
    def position(s):
        recording = recordings.get(s["capture_epoch"] + ":" + s["source"], {})
        return (recording.get("started_at", s["received_at"]) + s.get("audio_start_ms", 0) / 1000,
                s.get("event_seq", 0)) if s["source"] != "file" else (s.get("audio_start_ms", 0) / 1000, s.get("event_seq", 0))
    return sorted(segments, key=position)


def transcript_fingerprint(segments):
    return digest(json.dumps([(s["id"], s["revision"], s["text"]) for s in segments], ensure_ascii=False))


def clock(ms):
    seconds = int(ms or 0) // 1000
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def transcript_anchor(segment_id, revision):
    """Return a stable, renderer-safe anchor for one transcript revision."""
    return "transcript-" + digest(f"{segment_id}:{revision}")[:16]


def requires_expanded_overview(segments):
    """Require the expanded overview when there is enough transcript to summarize."""
    return len(segments) >= 3 and sum(len(str(s.get("text", ""))) for s in segments) >= 240


def render_transcript(meeting, segments):
    lines = ["# " + meeting["title"] + " · 全量转录", "", "> 语音识别结果可能有误，请结合原始音频核对。撤回内容不计入正文。", "",
             f"- 有效最终片段：{len(segments)}", f"- 会议 ID：{meeting['id']}", ""]
    job = meeting.get("audio_import", {})
    if job:
        lines += ["音频：" + job["file_name"], "", "导入状态：" + job["state"], ""]
    if not segments:
        lines += ["本场没有已确认的转录文字。请检查录音与识别状态；此文件不代表识别成功。", ""]
    for index, s in enumerate(segments, 1):
        source = "文字补充" if s["capture_epoch"] == "manual" else SOURCE_LABELS[s["source"]]
        service = {"apple_local": "Apple 中文识别", "dashscope": "阿里云百炼", "deepseek": "DeepSeek 官方 ASR"}
        label = service.get(s.get("provider"), "Apple 中文识别") if s["capture_epoch"] != "manual" else "手动"
        lines += [f"<a id=\"{transcript_anchor(s['id'], s['revision'])}\"></a>",
                  f"## S{index:04d} · {clock(s.get('audio_start_ms'))} · {source}", "",
                  f"录音段：{s['capture_epoch']} · 识别服务：{label} · 识别时间：{s.get('occurred_at', '')}", "", s["text"], ""]
    return "\n".join(lines)


def render_summary(meeting, segments, result=None, status="local_only", *, overview_needs_review=False):
    lines = ["# " + meeting["title"] + " · 会议纪要", "", "> 根据会议转录整理，供参会者核对；不替代正式确认。", "",
             "- 会议日期：" + datetime.fromtimestamp(meeting["created_at"]).astimezone().strftime("%Y-%m-%d %H:%M"), ""]
    if result is not None:
        lines += ["## 会议概述", ""]
        if overview_needs_review:
            lines += ["> 概述待完善，可在应用中单独补写。", ""]
        if overview := result.get("overview", "").strip():
            lines += [overview, ""]
        for key, title in (("topics", "讨论要点"), ("decisions", "明确决定"), ("actions", "后续行动"), ("open_questions", "未决问题")):
            lines += ["## " + title, ""]
            items = result.get(key, [])
            if items:
                lines.extend("- " + item["text"] for item in items)
            else:
                lines += ["转录中未提取到明确内容。"]
            lines += [""]
    else:
        statuses = {"pending": "AI 总结正在等待生成。",
                    "running": "AI 总结正在生成。",
                    "failed": "AI 总结生成失败，可在应用中重试。",
                    "outcome_unknown": "AI 总结请求被中断，结果未知；未自动重复调用，可在应用中重试。",
                    "local_only": "本场未启用 AI 总结。",
                    "permission_revoked": "本场模型授权已关闭，已停止后续总结请求。"}
        if meeting["state"] == "active" and meeting.get("permissions", {}).get("model"):
            statuses["local_only"] = "会议尚未结束，结束后将整理 AI 总结。"
        lines += [statuses.get(status, statuses["local_only"]), ""]
        if not segments:
            lines += ["没有已确认的转录内容，请检查录音和识别状态。", ""]
    return "\n".join(lines)


def overview_needs_review(overview, segments):
    length = len(re.sub(r"\s+", "", overview))
    return length == 0 or (requires_expanded_overview(segments) and not 180 <= length <= 240)


def validate_overview(overview, segments):
    _summary_require(not overview_needs_review(overview, segments), "会议概述为空或篇幅不符合要求", "overview_length")
    return overview


def validate_summary(result, segments):
    result = MeetingSummary.model_validate(result).model_dump()
    visible = {(s["id"], s["revision"]): s["text"] for s in segments}
    for key in SUMMARY_SECTIONS:
        for item in result[key]:
            for ref in item["source_refs"]:
                identity = (ref["segment_id"], ref["revision"])
                _summary_require(identity in visible, "纪要依据不在本次转录中", "source_ref_not_in_transcript")
                if ref["quote"]:
                    _summary_require(ref["quote"] in visible[identity],
                                     "纪要引文与转录不一致", "summary_quote_not_in_transcript")
                else:
                    del ref["quote"]  # Preserve the archived shape of older results.
    return result


class MeetingArchive(RecordingArchive):
    def start_recording(self, principal, mid, value):
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid, True)
            self.store._acl(db, principal, m["project_id"], {"agent"})
            require(m["state"] == "active" and m.get("input_kind", "live") == "live", "请先创建实时会议", 409)
            require(len(set(value.sources)) == len(value.sources), "音源重复", 400)
            recordings = m.setdefault("recordings", [])
            require(not any(r["state"] == "recording" for r in recordings), "请先停止当前录音", 409)
            require(not any(r["epoch"] == value.recording_id for r in recordings), "录音标识已使用", 409)
            if value.settings_revision is not None or value.provider != "apple_local":
                self._register_speech_capture(m, value)
            for source in value.sources:
                path = f"{m['directory']}/01-原始音频-{source}-{value.recording_id}.caf"
                require(not self.writer_for(m).path(path).exists(), "录音文件已存在", 409)
                recordings.append({"id": value.recording_id + ":" + source, "epoch": value.recording_id,
                    "keep_original_caf": self.provider_settings.current.keep_original_caf,
                    "archive_state": "pending",
                    "source": source, "path": path, "state": "recording", "started_at": time.time()})
            self._event(db, m, "recording.started", {"recording_id": value.recording_id, "sources": value.sources})
            self._save(db, m)
            return {"recording_directory": str(self.writer_for(m).path(m["directory"])), "capture_epoch": value.recording_id}

    def _recording_event(self, m, value):
        recording = next((r for r in m.get("recordings", []) if r["epoch"] == value.capture_epoch and r["source"] == value.source), None)
        require(recording is not None, "录音未登记", 409)
        require(value.state in {"saved", "partial", "missing", "failed"}, "录音状态无效", 400)
        file_identity = None
        closed_without_audio = False
        try:
            path = self.writer_for(m).path(recording["path"])
            file_identity = identity(path) if path.is_file() else None
            closed_without_audio = value.state == "missing" and value.frames_received == 0 and not path.exists()
        except (OSError, LiveError):
            if value.state != "failed":
                raise
        tracked_capture = value.capture_epoch in m.get("capture_epochs", {})
        size = file_identity[2] if file_identity is not None else 0
        saved = value.state in {"saved", "partial"} and size > 0 and (not tracked_capture or bool(value.frames_received))
        state = value.state if saved or value.state == "failed" else "missing"
        recording.update(state=state, bytes=size, duration_ms=value.audio_end_ms or 0,
                         finished_at=time.time(), frames_received=value.frames_received)
        if state == "failed":
            recording["error"] = value.detail or "录音关闭后无法确认保存。"
        else:
            recording.pop("error", None)
        if tracked_capture:
            recording.update(closed_identity=file_identity if saved else None, closed_without_audio=closed_without_audio)

    def _archive_file(self, db, m, kind, content, name):
        m.setdefault("base_files", {})
        path = m["directory"] + "/" + name + ".md"
        record = {"path": path, "state": "saved", "hash": digest(content), "updated_at": time.time()}
        try:
            if self.writer_for(m).path(path).exists() and digest(content) != digest(self.writer_for(m).path(path).read_text(encoding="utf-8")):
                version = 2
                while self.writer_for(m).path(m["directory"] + "/" + name + f"-v{version}.md").exists():
                    version += 1
                path = m["directory"] + "/" + name + f"-v{version}.md"
                record["path"] = path
            self.writer_for(m).write(path, content)
        except (OSError, LiveError) as exc:
            record.update(state="save_failed", error=str(exc) if isinstance(exc, LiveError) else "无法写入目录")
        m["base_files"][kind] = record
        m.setdefault("base_file_paths", [])
        if path not in m["base_file_paths"]:
            m["base_file_paths"].append(path)
        return record

    def _saved_file_exists(self, meeting, record):
        try:
            return record.get("state") == "saved" and self.writer_for(meeting).path(record["path"]).is_file()
        except (OSError, LiveError):
            return False

    def archive_meeting(self, mid):
        with self.store.transaction() as db:
            m = self.store._get(db, "wb_meeting", mid)
            require(m["state"] in {"active", "ended"}, "会议已清理或正在删除", 409)
            segments = final_segments(self, db, m)
            fingerprint = transcript_fingerprint(segments)
            old = m.get("summary", {})
            transcript = m.get("base_files", {}).get("transcript", {})
            summary_file = m.get("base_files", {}).get("summary", {})
            can_start = m["state"] == "ended" and old.get("state") == "local_only" and bool(segments) and m.get("permissions", {}).get("model") and self.capabilities.get("model_enabled")
            if old.get("fingerprint") == fingerprint and not can_start:
                if not self._saved_file_exists(m, transcript):
                    self._archive_file(db, m, "transcript", render_transcript(m, segments), "02-全量转录")
                if not self._saved_file_exists(m, summary_file):
                    self._archive_file(db, m, "summary", render_summary(m, segments, old.get("result"), old["state"],
                        overview_needs_review=old.get("overview_needs_review", False)),
                        "03-会议纪要" if old.get("result") else "03-会议纪要-状态")
                self._save(db, m)
            else:
                self._archive_file(db, m, "transcript", render_transcript(m, segments), "02-全量转录")
                permitted = bool(m.get("permissions", {}).get("model") and self.capabilities.get("model_enabled"))
                state = "pending" if permitted and segments and m["state"] == "ended" else "local_only"
                m["summary"] = {"state": state, "fingerprint": fingerprint, "generation": old.get("generation", 0) + 1}
                self._archive_file(db, m, "summary", render_summary(m, segments, status=state), "03-会议纪要-状态")
                self._event(db, m, "meeting.archived", {"segment_count": len(segments)})
                self._save(db, m)
        self.publish_index(mid)
        return m

    def summary_work(self, principal):
        with self.store.transaction() as db:
            self.store._acl(db, principal, principal["project_id"], {"agent"})
            for m in self.store._all(db, "wb_meeting"):
                summary = m.get("summary", {})
                if m["project_id"] != principal["project_id"] or m["state"] != "ended" or summary.get("state") != "pending":
                    continue
                if not m.get("permissions", {}).get("model") or not self.capabilities.get("model_enabled"):
                    summary["state"] = "completed" if summary.get("result") else "local_only"
                    self._save(db, m)
                    continue
                segments = final_segments(self, db, m)
                summary.update(state="running", lease_id=uid("summary"))
                work = {"meeting": m, "segments": segments, "fingerprint": summary["fingerprint"], "lease_id": summary["lease_id"]}
                if summary.get("overview_needs_review") and summary.get("result") and not summary.get("regenerate"):
                    work["overview_draft"] = summary["result"]
                else:
                    current = {(s['id'], s['revision']) for s in segments}
                    follow_ups = []
                    for question in self._items(db, 'wb_question', m['id']):
                        result = question.get('result') or {}
                        if question.get('basis_stale') or result.get('meeting_resolution') != 'follow_up':
                            continue
                        refs = result.get('meeting_evidence', [])
                        if refs and all((ref['segment_id'], ref['revision']) in current for ref in refs):
                            follow_ups.append({'text': result['answer'], 'source_refs': [
                                {key: ref[key] for key in ('segment_id', 'revision')} for ref in refs]})
                    work["follow_ups"] = follow_ups
                self._save(db, m)
                return work
        return None

    def check_summary_work(self, principal, work):
        """Recheck immediately before every outbound summary request."""
        with self.store.transaction() as db:
            m = self._meeting(db, principal, work["meeting"]["id"], True)
            self.store._acl(db, principal, m["project_id"], {"agent"})
            summary = m.get("summary", {})
            require(m.get("permissions", {}).get("model") and self.capabilities.get("model_enabled"),
                    "本场模型授权已关闭，已停止后续总结请求", 409, code="summary_permission_revoked")
            require(m["state"] == "ended" and summary.get("state") == "running"
                    and summary.get("lease_id") == work["lease_id"]
                    and summary.get("fingerprint") == work["fingerprint"]
                    and transcript_fingerprint(final_segments(self, db, m)) == work["fingerprint"],
                    "纪要上下文已变化，已停止后续总结请求", 409)

    def finish_summary(self, principal, work, result=None, error=None, error_details=None):
        if result is not None:
            result = validate_summary(result, work["segments"])
        with self.store.transaction() as db:
            m = self._meeting(db, principal, work["meeting"]["id"], True)
            self.store._acl(db, principal, m["project_id"], {"agent"})
            summary = m.get("summary", {})
            require(m["state"] == "ended" and summary.get("state") == "running" and summary.get("lease_id") == work["lease_id"]
                    and summary["fingerprint"] == work["fingerprint"]
                    and transcript_fingerprint(final_segments(self, db, m)) == work["fingerprint"], "纪要上下文已变化，忽略旧结果", 409)
            if not m.get("permissions", {}).get("model") or not self.capabilities.get("model_enabled"):
                result, error, error_details = None, "permission_revoked", None
            kept_draft = result is None and bool(summary.get("result"))
            summary.pop("regenerate", None)
            if not kept_draft:
                summary["result"] = result
                summary["overview_needs_review"] = result is not None and overview_needs_review(result["overview"], work["segments"])
            summary.update(state="completed" if summary["result"] is not None else "permission_revoked" if error == "permission_revoked" else "outcome_unknown" if error == "outcome_unknown" else "failed",
                           error=error, error_details=error_details, finished_at=time.time())
            if not kept_draft:
                self._archive_file(db, m, "summary", render_summary(m, work["segments"], result, summary["state"],
                    overview_needs_review=summary["overview_needs_review"]),
                    "03-会议纪要" if result is not None else "03-会议纪要-状态")
            self._event(db, m, "summary." + summary["state"], {})
            self._save(db, m)
        self.publish_index(m["id"])

    def retry_summary(self, principal, mid, *, regenerate=False):
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid, True)
            self.store._acl(db, principal, m["project_id"], {"owner"})
            require(m["state"] == "ended", "请先结束会议", 409)
            require(m.get("permissions", {}).get("model") and self.capabilities.get("model_enabled"), "本场未启用模型总结", 409)
            require(m.get("summary", {}).get("state") not in {"pending", "running"}, "纪要正在生成", 409)
            segments = final_segments(self, db, m)
            require(bool(segments), "没有可总结的转录内容", 409)
            old = m.get("summary", {})
            fingerprint = transcript_fingerprint(segments)
            m["summary"] = {"state": "pending", "fingerprint": fingerprint, "generation": old.get("generation", 0) + 1}
            if old.get("result") and old.get("fingerprint") == fingerprint:
                m["summary"].update(result=old["result"], overview_needs_review=old.get("overview_needs_review", False))
                if regenerate:
                    m["summary"]["regenerate"] = True
            else:
                self._archive_file(db, m, "transcript", render_transcript(m, segments), "02-全量转录")
                self._archive_file(db, m, "summary", render_summary(m, segments, status="pending"), "03-会议纪要-状态")
            self._event(db, m, "summary.requested", {})
            self._save(db, m)
        self.publish_index(mid)
        return m

    def retry_archive(self, principal, mid):
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid, True)
            self.store._acl(db, principal, m["project_id"], {"owner"})
            require(m["state"] == "ended", "请先结束会议", 409)
            for r in m.get("recordings", []):
                if r.get("archive_state") in {"failed", "cleanup_failed"}:
                    r['archive_state'] = 'pending' if r['archive_state'] == 'failed' else 'cleanup'
                    r.pop('archive_error', None)
            self._save(db, m)
        return self.archive_meeting(mid)

    def meeting_file(self, principal, mid, kind):
        require(kind in {"transcript", "summary"}, "未知会议文件", 404)
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid)
            require(m["state"] != "expired", "会议文件已清理", 409)
            record = m.get("base_files", {}).get(kind)
            require(record is not None, "结束会议后自动生成会议文件", 404)
            path = self.writer_for(m).path(record["path"])
            require(path.is_file(), "文件未成功保存，请检查目录权限后重试", 409)
            content = path.read_text(encoding="utf-8")
            filename = path.name
            # Older generated minutes recorded a segment count in the header.
            # Render from the saved result only when the file still matches the archive.
            if kind == "summary" and "\n- 转录片段：" in content and record.get("hash") == digest(content):
                summary = m.get("summary", {})
                result = summary.get("result")
                if result is not None or "\n## 讨论摘录\n" in content:
                    segments = final_segments(self, db, m) if result is None else []
                    content = render_summary(m, segments, result, summary.get("state", "local_only"),
                                             overview_needs_review=summary.get("overview_needs_review", False))
                    filename = filename.replace("03-会议纪要-本机摘录", "03-会议纪要-状态", 1)
            return {"content": content, "filename": filename, "path": str(path), "state": record["state"]}
