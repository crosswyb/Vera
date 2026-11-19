"""Local meeting deliverables, independent of question detection and search."""

import json
import time
from datetime import datetime
from pathlib import Path

from live.artifact_writer import digest
from live.recording_archive import RecordingArchive
from live.store import LiveError, require, uid
from live.workbench_contracts import MeetingSummary


SOURCE_LABELS = {"system": "电脑声音", "microphone": "麦克风", "file": "导入音频"}


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


def render_transcript(meeting, segments):
    lines = ["# " + meeting["title"] + " · 全量转录", "", "> 本机语音识别结果，可能有误；请结合原始音频核对。撤回内容不计入正文。", "",
             f"- 有效最终片段：{len(segments)}", f"- 会议 ID：{meeting['id']}", ""]
    job = meeting.get("audio_import", {})
    if job:
        lines += ["音频：" + job["file_name"], "", "导入状态：" + job["state"], ""]
    if not segments:
        lines += ["本场没有已确认的转录文字。请检查录音与识别状态；此文件不代表识别成功。", ""]
    for index, s in enumerate(segments, 1):
        source = "文字补充" if s["capture_epoch"] == "manual" else SOURCE_LABELS[s["source"]]
        lines += [f"## S{index:04d} · {clock(s.get('audio_start_ms'))} · {source}", "",
                  f"录音段：{s['capture_epoch']}；识别时间：{s.get('occurred_at', '')}", "", s["text"], ""]
    return "\n".join(lines)


def render_summary(meeting, segments, result=None, status="local_only"):
    labels = {s["id"]: f"S{i:04d}" for i, s in enumerate(segments, 1)}
    lines = ["# " + meeting["title"] + " · 会议纪要", "", "> 根据会议转录整理，供参会者核对；不替代正式确认。", "",
             "- 会议日期：" + datetime.fromtimestamp(meeting["created_at"]).astimezone().strftime("%Y-%m-%d %H:%M"),
             f"- 转录片段：{len(segments)}", ""]
    transcript = meeting.get("base_files", {}).get("transcript", {}).get("path")
    if transcript:
        from urllib.parse import quote
        lines += ["[查看全量转录](" + quote(Path(transcript).name) + ")", ""]
    if result is not None:
        lines += ["## 会议概述", "", result["overview"], ""]
        for key, title in (("topics", "讨论要点"), ("decisions", "明确决定"), ("actions", "后续行动"), ("open_questions", "未决问题")):
            lines += ["## " + title, ""]
            for item in result[key]:
                refs = "、".join(labels[r["segment_id"]] for r in item["source_refs"])
                lines += ["- " + item["text"] + "（依据：" + refs + "）"]
            if not result[key]:
                lines += ["转录中未提取到明确内容。"]
            lines += [""]
    else:
        statuses = {"pending": "AI 总结正在等待生成。以下为本机原话摘录，尚未归纳决定或待办。",
                    "running": "AI 总结正在生成。以下为本机原话摘录。",
                    "failed": "AI 总结生成失败；已保留本机摘录，可在应用中重试。",
                    "outcome_unknown": "AI 总结请求被中断，结果未知；未自动重复调用，可在应用中重试。",
                    "local_only": "未启用本场模型处理。以下为本机原话摘录，未生成 AI 归纳。",
                    "permission_revoked": "本场模型授权已关闭，已停止后续总结请求。以下为本机原话摘录。"}
        if meeting["state"] == "active" and meeting.get("permissions", {}).get("model"):
            statuses["local_only"] = "会议尚未结束。以下为本机原话摘录，结束会议后整理 AI 总结。"
        lines += [statuses.get(status, statuses["local_only"]), "", "## 讨论摘录", ""]
        selected = sorted({round(i * (len(segments) - 1) / min(11, len(segments) - 1)) for i in range(min(12, len(segments)))}) if len(segments) > 1 else list(range(len(segments)))
        for i in selected:
            lines += [f"- {segments[i]['text']}（原话：S{i + 1:04d}）"]
        if not segments:
            lines += ["没有已确认的转录内容，无法生成有依据的纪要。请先检查录音和识别状态。"]
        lines += ["", "## 决定与后续行动", "", "尚未归纳；请结合全量转录确认，未推断负责人、期限或会议结论。", ""]
    return "\n".join(lines)


def validate_summary(result, segments):
    result = MeetingSummary.model_validate(result).model_dump()
    visible = {(s["id"], s["revision"]) for s in segments}
    for key in ("topics", "decisions", "actions", "open_questions"):
        for item in result[key]:
            require(all((r["segment_id"], r["revision"]) in visible for r in item["source_refs"]), "纪要依据不在本次转录中", 409)
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
        require(value.state in {"saved", "partial", "missing"}, "录音状态无效", 400)
        path = self.writer_for(m).path(recording["path"])
        saved = path.is_file() and path.stat().st_size > 0
        recording.update(state=value.state if saved else "missing", bytes=path.stat().st_size if saved else 0,
                         duration_ms=value.audio_end_ms or 0, finished_at=time.time(), frames_received=value.frames_received)

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
                    self._archive_file(db, m, "summary", render_summary(m, segments, old.get("result"), old["state"]), "03-会议纪要" if old.get("result") else "03-会议纪要-本机摘录")
                self._save(db, m)
            else:
                self._archive_file(db, m, "transcript", render_transcript(m, segments), "02-全量转录")
                permitted = bool(m.get("permissions", {}).get("model") and self.capabilities.get("model_enabled"))
                state = "pending" if permitted and segments and m["state"] == "ended" else "local_only"
                m["summary"] = {"state": state, "fingerprint": fingerprint, "generation": old.get("generation", 0) + 1}
                self._archive_file(db, m, "summary", render_summary(m, segments, status=state), "03-会议纪要-本机摘录")
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
                    summary["state"] = "local_only"; self._save(db, m); continue
                segments = final_segments(self, db, m)
                summary.update(state="running", lease_id=uid("summary"))
                self._save(db, m)
                return {"meeting": m, "segments": segments, "fingerprint": summary["fingerprint"], "lease_id": summary["lease_id"]}
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

    def finish_summary(self, principal, work, result=None, error=None):
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
                result, error = None, "permission_revoked"
            summary.update(state="completed" if result is not None else "permission_revoked" if error == "permission_revoked" else "outcome_unknown" if error == "outcome_unknown" else "failed",
                           error=error, result=result, finished_at=time.time())
            self._archive_file(db, m, "summary", render_summary(m, work["segments"], result, summary["state"]), "03-会议纪要" if result is not None else "03-会议纪要-本机摘录")
            self._event(db, m, "summary." + summary["state"], {})
            self._save(db, m)
        self.publish_index(m["id"])

    def retry_summary(self, principal, mid):
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid, True)
            self.store._acl(db, principal, m["project_id"], {"owner"})
            require(m["state"] == "ended", "请先结束会议", 409)
            require(m.get("permissions", {}).get("model") and self.capabilities.get("model_enabled"), "本场未启用模型总结", 409)
            require(m.get("summary", {}).get("state") not in {"pending", "running"}, "纪要正在生成", 409)
            segments = final_segments(self, db, m)
            require(bool(segments), "没有可总结的转录内容", 409)
            self._archive_file(db, m, "transcript", render_transcript(m, segments), "02-全量转录")
            m["summary"] = {"state": "pending", "fingerprint": transcript_fingerprint(segments),
                            "generation": m.get("summary", {}).get("generation", 0) + 1}
            self._archive_file(db, m, "summary", render_summary(m, segments, status="pending"), "03-会议纪要-本机摘录")
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
            return {"content": path.read_text(encoding="utf-8"), "filename": path.name, "path": str(path), "state": record["state"]}
