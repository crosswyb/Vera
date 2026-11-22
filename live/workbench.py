"""V2 meeting research. One question owns its execution state and document.

The existing authenticated SQLite store remains the only writer. Research and
model calls run outside these transactions; version/generation fences decide
whether a returned result is still relevant.
"""

import json
import shutil
import time
from collections import Counter
from pathlib import Path

from live.artifact_writer import digest, render_document
from live.meeting_archive import MeetingArchive
from live.meeting_storage import MeetingStorage
from live.original_evidence import target_for, target_key, with_original_evidence
from live.output_validation import failure_message, output_presentation
from live.failure_diagnostics import historical_question_details, read_historical_ledger, request_details
from live.source_reader import is_grounded_explanation, needs_internal_records
from live.detection_recovery import DetectionRecovery
from live.speech_workbench import CAPTURE_SAVED, SpeechWorkbench
from live.store import LiveError, require, uid
from live.workbench_contracts import AudioEvent, MeetingPermissions


ACTIVE = {"queued", "running"}
UNSTARTED = {"candidate", "queued", "backlog", "needs_query"}
RESEARCH_MEETING_STATES = {"active", "ended"}
COORDINATOR_CONTEXT_SEGMENTS = 6
COORDINATOR_CONTEXT_CHARACTERS = 3000
COORDINATOR_RETRY_DELAYS = (2, 5, 15)
COORDINATOR_BATCH_SEGMENTS = 4
COORDINATOR_CATCHUP_SEGMENTS = 16
IMPORT_BATCH_CHARACTERS = 4000
IMPORT_MIN_CHARACTERS = 3000
IMPORT_MAX_SEGMENTS = 256
TRANSCRIPT_PAGE_SIZE = 120


def coordinator_lane(meeting):
    return "realtime" if meeting.get("input_kind", "live") != "audio_file" and meeting["state"] == "active" else "background"


def research_priority(question):
    if question.get("manual_requested") or question.get("priority", 0):
        return 0
    return 1 if question.get("request_kind") == "explicit" else 2


def wait_for_public_query(meeting, question):
    """Do not queue an external lookup without a permissible search term."""
    if (meeting['data_scope'] == 'real' and question['state'] in UNSTARTED
            and question.get('evidence_need') in {'public', 'mixed'} and not question['public_query']):
        question.update(state='needs_query', stage='请补充允许公开发送的检索词，再开始查证')


def transient_detection_failure(details):
    if not details or details.get("service") != "model":
        return False
    if details.get("code") == "http_error":
        return (details.get("http_status") in {408, 429, 500, 502, 503, 504}
                and details.get("provider_code") != "insufficient_quota")
    return details.get("code") in {"connect_timeout", "tls_handshake_timeout", "read_timeout",
        "dns_error", "connection_refused", "network_unreachable", "connection_interrupted",
        "connection_error", "request_timeout"}


class Workbench(DetectionRecovery, MeetingArchive, MeetingStorage, SpeechWorkbench):
    def __init__(self, store, root, capabilities=None):
        self.store = store
        self._init_storage(root)
        self.capabilities = capabilities or {}
        with store.transaction() as db:
            store._put(db, "wb_schema", "version", {"version": 1})

    def _meeting(self, db, principal, mid, write=False):
        m = self.store._get(db, "wb_meeting", mid)
        self.store._acl(db, principal, m["project_id"], {"owner", "member", "agent"} if write else None)
        if write:
            require(m["state"] not in {"deleting", "delete_failed"}, "会议正在删除或删除未完成，请在会议列表重试删除", 409)
        return m

    def _save(self, db, m):
        self.store._put(db, "wb_meeting", m["id"], m)

    def _event(self, db, m, kind, payload):
        self.store._append(db, m, uid("wb-event"), kind, "workbench", payload)

    def _items(self, db, kind, mid):
        return [v for v in self.store._all(db, kind) if v.get("meeting_id") == mid]

    def home(self, principal):
        with self.store.transaction() as db:
            self.store._acl(db, principal, principal["project_id"])
            meetings = [m for m in self.store._all(db, "wb_meeting") if m["project_id"] == principal["project_id"]]
            mids = {m["id"] for m in meetings}
            questions = {q["id"]: q for q in self.store._all(db, "wb_question") if q["meeting_id"] in mids}
            documents = [d for d in self.store._all(db, "wb_document") if d["meeting_id"] in mids]
            materials = []
            for d in documents:
                q = questions.get(d["question_id"])
                if q and d["state"] != "superseded":
                    materials.append({"id": d["id"], "meeting_id": d["meeting_id"], "question_id": q["id"],
                                      "title": q["title"], "version": d["question_version"], "state": d["state"],
                                      "basis_stale": d.get("basis_stale", False), "created_at": d["created_at"]})
            rows = []
            for m in meetings:
                qs = [q for q in questions.values() if q["meeting_id"] == m["id"]]
                ds = [d for d in documents if d["meeting_id"] == m["id"]]
                base_count = sum(f["state"] == "saved" for f in m.get("base_files", {}).values())
                audio_count = sum(r["state"] in {"saved", "partial"} for r in m.get("recordings", []))
                rows.append({**m, "question_count": len(qs), "document_count": sum(d["state"] == "saved" for d in ds) + base_count + audio_count,
                    "updated_at": max([m["created_at"], m.get("renamed_at", 0), m.get("ended_at", 0), m.get("capture", {}).get("updated_at", 0)]
                                               + [q.get("updated_at", q["created_at"]) for q in qs])})
            return {"member": principal, "project": self.store._get(db, "project", principal["project_id"]),
                    "meetings": sorted(rows, key=lambda m: m["updated_at"], reverse=True),
                    "materials": sorted(materials, key=lambda d: d["created_at"], reverse=True),
                    "capabilities": self.capabilities,
                    "update_readiness": self._update_readiness(meetings, questions.values(), documents)}

    def _update_readiness(self, meetings, questions, documents):
        reason = ""
        by_id = {m['id']: m for m in meetings}
        if any(m['state'] == 'active' for m in meetings):
            reason = "请先结束进行中的会议或音频导入，再安装更新。"
        elif any(m.get('coordinator_state') == 'running' or (
                not m.get('research_paused') and m.get('coordinator_state') in {'pending', 'retry_wait'})
                or m.get('summary', {}).get('state') in {'pending', 'running'} for m in meetings):
            reason = "问题识别或会议总结尚未完成，请完成后再安装更新。"
        elif any(q['state'] == 'running' or (q['state'] in {'queued', 'backlog', 'waiting_dependency'}
                 and not self._automation_held(by_id[q['meeting_id']], q)) for q in questions):
            reason = "查证任务尚未完成，请完成或暂停后再安装更新。"
        elif any(d['state'] in {'pending', 'save_failed'} for d in documents):
            reason = "还有文档未保存，请保存后再安装更新。"
        return {'can_install': not reason, 'reason': reason}

    def create(self, principal, value):
        with self.store.transaction() as db:
            self.store._acl(db, principal, principal["project_id"], {"owner", "member"})
            project = self.store._get(db, "project", principal["project_id"])
            require(value.data_scope != "synthetic" or project.get("data_scope") == "synthetic", "合成会议必须位于独立合成项目", 409)
            model_allowed = principal["role"] == "owner" and bool(self.capabilities.get("model_enabled"))
            search_allowed = model_allowed and bool(self.capabilities.get("search_enabled"))
            permissions = value.permissions if value.permissions is not None else MeetingPermissions(
                model=model_allowed, search=search_allowed, auto_search=search_allowed)
            if permissions.model or permissions.search or permissions.auto_search:
                self.store._acl(db, principal, project["id"], {"owner"})
            self._validate_permissions(permissions)
            m = {"id": uid("wb"), "project_id": project["id"], "title": value.title.strip(),
                 "data_scope": value.data_scope, "mode": "live", "input_kind": value.input_kind,
                 "state": "active", "research_paused": False,
                 "created_at": time.time(), "seq": 0, "semantic_seq": 0, "coordinator_cursor": 0,
                 "coordinator_state": "idle", "capture": {"status": "disconnected"}, "generation": 1,
                 "permissions": {**permissions.model_dump(), "updated_at": time.time(), "actor_id": principal["id"]}}
            m["output_root"] = self.store._get(db, "wb_storage", "default")["output_root"]
            writer = self.writer_for(m)
            m["directory"] = writer.meeting_dir(m)
            writer.path(m["directory"]).mkdir(mode=0o700)
            self._event(db, m, "meeting.created", {"data_scope": m["data_scope"]})
            self._save(db, m)
        self.publish_index(m["id"])
        return m

    def rename_meeting(self, principal, mid, title):
        title = title.strip()
        require(bool(title) and len(title) <= 120 and not any(ord(c) < 32 or ord(c) == 127 for c in title),
                "会议名称需为 1～120 个字符，不能包含换行或控制字符", 400)
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid, True)
            self.store._acl(db, principal, m["project_id"], {"owner"})
            require(m["state"] != "expired", "会议内容已清理，仅可删除记录", 409)
            m.update(title=title, renamed_at=time.time())
            self._event(db, m, "meeting.renamed", {})
            self._save(db, m)
        # Keep the directory stable: recordings and in-flight tasks own its path.
        self.publish_index(mid)
        return m

    def _deletion_directory(self, db, meeting):
        relative = meeting.get("directory", "")
        if not relative:
            return None
        require(len(Path(relative).parts) == 1 and relative.endswith("-" + meeting["id"][-8:]),
                "会议文件夹归属无法确认，已停止删除", 409)
        writer = self.writer_for(meeting)
        directory = writer.path(relative)
        require(directory.parent == writer.root and not directory.is_symlink()
                and (not directory.exists() or directory.is_dir()), "会议文件夹位置异常，已停止删除", 409)
        require(not any(m["id"] != meeting["id"] and m.get("directory")
                        and (Path(m["output_root"]) / m["directory"]).is_relative_to(directory)
                        for m in self.store._all(db, "wb_meeting")), "会议文件夹被其他会议引用，已停止删除", 409)
        return directory

    def deletion_preview(self, principal, mid):
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid)
            self.store._acl(db, principal, m["project_id"], {"owner"})
            directory = self._deletion_directory(db, m)
            files = sum(p.is_file() or p.is_symlink() for p in directory.rglob("*")) if directory and directory.exists() else 0
            return {"id": mid, "title": m["title"], "state": m["state"], "file_count": files,
                    "question_count": len(self._items(db, "wb_question", mid)),
                    "segment_count": len(self._items(db, "wb_segment", mid)),
                    "directory": str(directory) if directory else "",
                    "audio_busy": m.get("audio_import", {}).get("state") == "processing"
                                  or any(r["state"] in {"recording", "copying"} for r in m.get("recordings", []))}

    def delete_meeting(self, principal, mid, confirmation):
        require(confirmation == mid, "请确认要删除的会议后再操作", 400)
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid)
            self.store._acl(db, principal, m["project_id"], {"owner"})
            self._deletion_directory(db, m)
            require(m.get("audio_import", {}).get("state") != "processing"
                    and not any(r["state"] in {"recording", "copying"} for r in m.get("recordings", [])),
                    "请先停止本场收听或取消音频导入，等待音频保存完成后再删除", 409)
            # Commit the fence first. A crash or partial file failure must never
            # let recovery or a late model result recreate the meeting's files.
            m.update(state="deleting", research_paused=True, generation=m["generation"] + 1, coordinator_state="idle")
            for question in self._items(db, "wb_question", mid):
                if question["state"] in UNSTARTED | {"running", "waiting_dependency"}:
                    question.update(state="cancelled", stage="会议正在删除", generation=question["generation"] + 1)
                    self.store._put(db, "wb_question", question["id"], question)
            if m.get("summary", {}).get("state") in {"pending", "running"}:
                m["summary"]["state"] = "cancelled"
            self._save(db, m)
        error = None
        with self.store.transaction() as db:
            db.execute("PRAGMA secure_delete=ON")
            m = self._meeting(db, principal, mid)
            try:
                directory = self._deletion_directory(db, m)
                if m.get("directory"):
                    self.writer_for(m).clean_temporaries(m["directory"])
                if directory and directory.exists():
                    shutil.rmtree(directory)
            except (OSError, LiveError) as exc:
                error = ("没有权限清理会议文件夹，请检查文件夹权限后重试删除。" if isinstance(exc, PermissionError)
                         else str(exc) if isinstance(exc, LiveError) else "会议文件夹未能完全清理，请检查磁盘或文件占用后重试删除。")
                m.update(state="delete_failed", deletion_error=error)
                self._save(db, m)
            else:
                for kind in ("wb_segment", "wb_question", "wb_document", "wb_feishu_job", "wb_detection_task", "wb_detection_action"):
                    db.execute("DELETE FROM records WHERE kind=? AND json_extract(data,'$.meeting_id')=?", (kind, mid))
                db.execute("DELETE FROM events WHERE meeting=?", (mid,))
                db.execute("DELETE FROM records WHERE kind='wb_meeting' AND id=?", (mid,))
        require(error is None, (error or "") + " 部分文件可能已清理，会议记录暂时保留，后续任务已停止。", 409)
        return {"deleted": True, "id": mid}

    def earlier_segments(self, principal, mid, before):
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid)
            # Segment keys are meeting_id + ':' + segment_id; use the existing
            # (kind,id) index so reading one page never loads other meetings.
            rows = db.execute("""SELECT data FROM records WHERE kind='wb_segment'
                AND id>=? AND id<? AND json_extract(data,'$.type')='transcript.final'
                AND json_extract(data,'$.event_seq')<? ORDER BY json_extract(data,'$.event_seq') DESC LIMIT ?""",
                (mid + ':', mid + ';', before, TRANSCRIPT_PAGE_SIZE + 1)).fetchall()
            page = [json.loads(row[0]) for row in reversed(rows[:TRANSCRIPT_PAGE_SIZE])]
            more = len(rows) > TRANSCRIPT_PAGE_SIZE
            return {'segments': page, 'has_more': more, 'next_before': page[0]['event_seq'] if more else None,
                    'revision_epoch': m.get('transcript_revision_epoch', 0)}

    def search_meeting(self, principal, mid, query):
        query = query.strip()
        require(0 < len(query) <= 200, "请输入不超过 200 字的搜索词", 400)
        with self.store.transaction() as db:
            self._meeting(db, principal, mid)
            # Literal substring matching keeps punctuation and SQL wildcards
            # meaningful, and only scans this meeting's saved final utterances.
            rows = db.execute("""SELECT data FROM records WHERE kind='wb_segment'
                AND id>=? AND id<? AND json_extract(data,'$.type')='transcript.final'
                AND instr(lower(json_extract(data,'$.text')),lower(?))>0
                ORDER BY json_extract(data,'$.event_seq') DESC LIMIT 21""",
                (mid + ':', mid + ';', query)).fetchall()
            segments = [json.loads(row[0]) for row in rows[:20]]
            questions = db.execute("""SELECT data FROM records WHERE kind='wb_question'
                AND json_extract(data,'$.meeting_id')=?
                AND json_extract(data,'$.state')!='superseded'
                AND instr(lower(json_extract(data,'$.title')),lower(?))>0
                ORDER BY json_extract(data,'$.created_at') DESC LIMIT 21""", (mid, query)).fetchall()
            return {"segments": [{key: segment.get(key) for key in
                        ("segment_id", "revision", "event_seq", "text", "source", "occurred_at", "received_at", "audio_start_ms")}
                        for segment in segments],
                    "questions": [{key: question.get(key) for key in ("id", "title", "state", "version")}
                        for question in (json.loads(row[0]) for row in questions[:20])],
                    "more_segments": len(rows) > 20, "more_questions": len(questions) > 20}

    def snapshot(self, principal, mid):
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid)
            all_segments = sorted(self._items(db, "wb_segment", mid), key=lambda x: x["event_seq"])
            visible_segments = [s for s in all_segments if s["type"] != "transcript.retracted"]
            segments = visible_segments[-TRANSCRIPT_PAGE_SIZE:]
            has_earlier = any(s['type'] == 'transcript.final' for s in visible_segments[:-TRANSCRIPT_PAGE_SIZE])
            questions = sorted(self._items(db, "wb_question", mid), key=lambda q: q["created_at"])
            self._snapshot_failure_diagnostics(db, mid, questions)
            docs = self._items(db, "wb_document", mid)
            activity = [json.loads(r[0]) for r in db.execute("SELECT data FROM events WHERE meeting=? AND json_extract(data,'$.type') NOT IN ('transcript.interim','audio.level') ORDER BY seq DESC LIMIT 40", (mid,))][::-1]
            counts = dict(Counter(q["state"] for q in questions))
            counts["documents"] = sum(d["state"] == "saved" for d in docs)
            final_segments = [s for s in all_segments if s["type"] == "transcript.final"]
            processed = sum(s["event_seq"] <= m["coordinator_cursor"] for s in final_segments)
            recovery = self.detection_task_snapshot(db, m)
            return {"server_time": time.time(), "meeting": m, "segments": segments,
                    "transcript_page": {"has_more": has_earlier, "next_before": segments[0]['event_seq'] if has_earlier else None,
                                        "revision_epoch": m.get('transcript_revision_epoch', 0),
                                        "changes": m.get('transcript_revision_changes', [])},
                    "segment_count": len(visible_segments),
                    "questions": questions, "documents": docs,
                    "detection_tasks": recovery,
                    "automation_progress": {"processed": processed, "pending": len(final_segments) - processed,
                                            "pending_characters": sum(len(s["text"]) for s in final_segments if s["event_seq"] > m["coordinator_cursor"]),
                                            "strategy": "import" if m.get("input_kind") == "audio_file" else "live",
                                            "recovering": sum(t['state'] in {'pending', 'running'} for t in recovery),
                                            "needs_review": sum(t['state'] == 'needs_review' for t in recovery),
                                            "complete": processed == len(final_segments) and not recovery,
                                            "held_questions": sum(self._automation_held(m, q) for q in questions)},
                    "activity": activity, "speech_gaps": self._speech_gaps(db, m),
                    "counts": counts, "capabilities": self.capabilities,
                    "output_directory": str(Path(m["output_root"]) / m["directory"]) if m["directory"] else "已按期限清理"}

    def _snapshot_failure_diagnostics(self, db, mid, questions):
        legacy = [q for q in questions if q["state"] in {"failed", "outcome_unknown"}
                  and (not q.get("error_details") or (not q["error_details"].get("response") and any(i.get("code") == "output_incomplete" for i in q["error_details"].get("issues", []))))]
        if not legacy:
            return
        events = [json.loads(r[0]) for r in db.execute("SELECT data FROM events WHERE meeting=? AND json_extract(data,'$.type') IN ('research.failed','research.started','research.searching','research.synthesizing') ORDER BY seq", (mid,))]
        ledger = read_historical_ledger(self.store.path.with_name("model-usage.sqlite3").resolve())
        for q in legacy:
            details = historical_question_details(q, events, ledger)
            if details is None and q.get("error_details"):
                details = output_presentation(q["error_details"])
            if details is None:
                previous = next((e for e in reversed(events) if e["payload"].get("question_id") == q["id"] and e["type"] != "research.failed"), None)
                stage = previous["type"].removeprefix("research.") if previous else "detecting"
                details = {**request_details("unknown_outcome"), "stage": stage, "provenance": "legacy_incomplete"}
            q.update(error_details=details, error=failure_message(details))

    def begin_import(self, principal, mid, value):
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid, True)
            self.store._acl(db, principal, m["project_id"], {"agent"})
            require(m.get("input_kind") == "audio_file" and m["state"] == "active", "请先创建音频导入会议", 409)
            require(not m.get("audio_import"), "本会议已有导入记录，请新建一次导入，避免重复转写", 409)
            require(value.file_name.strip() and not any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in value.file_name), "音频名称无效", 400)
            speech = self._configured_speech(m, value.provider, 'file', ['file'], value.settings_revision) if value.settings_revision is not None or value.provider != "apple_local" else {}
            m["audio_import"] = {**value.model_dump(exclude={"settings_revision"}), **speech,
                                 "state": "processing", "processed_ms": 0, "started_at": time.time()}
            from pathlib import Path
            extension = Path(value.file_name).suffix.lower()
            require(extension in {".wav", ".m4a", ".mp3", ".aiff", ".aif", ".caf", ".flac"}, "音频格式不支持", 400)
            original = f"{m['directory']}/01-原始音频-{value.import_id}{extension}"
            m["audio_import"]["original_path"] = original
            m.setdefault("recordings", []).append({"id": value.import_id + ":file", "epoch": value.import_id,
                "source": "file", "path": original, "state": "copying", "started_at": time.time(), "duration_ms": value.duration_ms})
            m["capture"] = {"status": "importing", "source": "file", "updated_at": time.time()}
            self._event(db, m, "import.started", {"file_name": value.file_name})
            self._save(db, m)
            return m

    def resume_import(self, principal, mid, value):
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid, True)
            self.store._acl(db, principal, m["project_id"], {"agent"})
            require(m.get("input_kind") == "audio_file" and m["state"] == "active", "请先打开音频导入会议", 409)
            job = m.get("audio_import", {})
            require(job.get("import_id") == value.import_id, "不是当前音频导入任务", 409)
            require(job.get("state") in {"failed", "cancelled", "interrupted"}, "当前导入任务不需要恢复", 409)
            provider = job.get("provider")
            require(provider in {"dashscope", "deepseek"}, "本机转写任务不支持断点续传，请重新导入", 409)
            # Revalidate the current authorized scope. The owner may have
            # reconfirmed the same meeting after changing the provider
            # settings, which gives the resumed task a new authorization
            # version and endpoint snapshot.
            speech = self._configured_speech(m, provider, "file", ["file"], self.provider_settings.current.revision)
            recording = next((row for row in m.get("recordings", [])
                              if row.get("epoch") == value.import_id and row.get("source") == "file"), None)
            require(recording is not None and recording.get("state") in {"saved", "partial"},
                    "原始音频尚未完整保存，无法从断点继续", 409)
            path = self.writer_for(m).path(recording["path"])
            require(path.is_file() and path.stat().st_size > 0, "找不到已保存的原始音频", 409)
            duration = int(job.get("duration_ms") or recording.get("duration_ms") or 0)
            require(duration > 0, "音频时长记录无效，无法恢复", 409)
            start_ms = int(job.get("failed_at_ms") or job.get("processed_ms") or 0)
            require(0 <= start_ms <= duration, "音频断点超出范围", 409)
            # Older clients restarted this stream at seq=1, colliding with
            # durable transcript events. Also recover their sequence watermark.
            saved_seq = db.execute("""SELECT MAX(CAST(json_extract(data,'$.payload.seq') AS INTEGER))
                FROM events WHERE meeting=? AND json_extract(data,'$.payload.capture_epoch')=?
                AND json_extract(data,'$.payload.stream_id')=?""",
                (mid, value.import_id, "file-" + value.import_id)).fetchone()[0] or 0
            job.update(state="processing", processed_ms=start_ms, resume_from_ms=start_ms,
                       last_event_seq=max(int(job.get("last_event_seq") or 0), saved_seq),
                       resume_count=int(job.get("resume_count") or 0) + 1,
                       started_at=time.time(), error="", last_error="", lease_until=0, **speech)
            m["capture"] = {"status": "importing", "source": "file", "updated_at": time.time()}
            self._event(db, m, "import.resumed", {"from_ms": start_ms, "resume_count": job["resume_count"]})
            self._save(db, m)
            return m

    def finish_import(self, principal, mid, value):
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid, True)
            self.store._acl(db, principal, m["project_id"], {"agent"})
            job = m.get("audio_import", {})
            require(job.get("import_id") == value.import_id and job.get("state") == "processing", "导入已结束或不是当前任务", 409)
            if value.state == "completed" and job.get("provider") in {"dashscope", "deepseek"}:
                require(job["processed_ms"] >= job["duration_ms"], "仍有音频片段未确认，不能完成导入", 409)
            job.update(state=value.state, error=value.error, finished_at=time.time())
            attempt = int(job.get("resume_count") or 0)
            if value.state != "completed":
                job["failed_at_ms"] = job["processed_ms"]
                job["last_error"] = value.error
            else:
                job.pop("failed_at_ms", None)
                job.pop("resume_from_ms", None)
                job.pop("last_error", None)
            for recording in m.get("recordings", []):
                if recording["epoch"] == value.import_id and recording["state"] == "copying":
                    path = self.writer_for(m).path(recording["path"])
                    recording.update(state="partial" if path.is_file() and path.stat().st_size > 0 else "missing")
            if value.state == "completed":
                job["processed_ms"] = job["duration_ms"]
            # Keep the meeting open: coordinator and queued research must finish
            # after EOF, and the owner can still add or clarify questions.
            m["capture"] = {"status": value.state, "source": "file", "updated_at": time.time()}
            self._event(db, m, "import." + value.state, {})
            self._save(db, m)
        transcript = self.transcript(principal, mid)
        suffix = f"-r{attempt}" if attempt else ""
        path = m["directory"] + "/transcript-" + value.import_id + suffix + ".md"
        with self.store.transaction() as db:
            # File publication shares the meeting deletion transaction fence.
            m = self._meeting(db, principal, mid, True)
            job = m.get("audio_import", {})
            if (job.get("import_id") != value.import_id or int(job.get("resume_count") or 0) != attempt
                    or job.get("state") != value.state):
                return m
            try:
                self.writer_for(m).write(path, transcript["content"])
                save_state = "saved"
            except (OSError, LiveError):
                save_state = "save_failed"
            m["audio_import"].update(transcript_path=path, transcript_state=save_state)
            self._save(db, m)
        self.archive_meeting(mid)
        self.publish_index(mid)
        return self.snapshot(principal, mid)["meeting"]

    def transcript(self, principal, mid):
        from live.meeting_archive import final_segments, render_transcript
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid)
            require(m["state"] != "expired", "会议转写已按期限清理", 409)
            segments = final_segments(self, db, m)
            return {"content": render_transcript(m, segments), "filename": "会议转写-" + mid + ".md", "segment_count": len(segments)}

    def _validate_permissions(self, value):
        require(not value.model or self.capabilities.get("model_enabled"), "请先在设置中配置并启用文字模型", 409)
        require(not value.search or self.capabilities.get("search_enabled"), "请先在设置中配置并启用搜索服务", 409)
        require(not value.search or value.model, "查证需要同时允许文字模型", 400)
        require(not value.auto_search or value.search, "自动检索需要同时允许搜索服务", 400)

    def permissions(self, principal, mid, value):
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid, True)
            self.store._acl(db, principal, m["project_id"], {"owner"})
            require(m["state"] in RESEARCH_MEETING_STATES, "会议数据已清理，无法查证", 409)
            self._validate_permissions(value)
            m["permissions"] = {**value.model_dump(), "updated_at": time.time(), "actor_id": principal["id"]}
            for q in self._items(db, "wb_question", mid):
                automatic_query_revoked = not value.auto_search and q.get("public_query_origin") == "model"
                if automatic_query_revoked:
                    q.update(public_query="", public_query_origin="")
                if q["state"] == "running" and (not value.model or not value.search or automatic_query_revoked):
                    q.update(state="paused", generation=q["generation"] + 1,
                             stage="本场联网授权已变更，请确认后重试", updated_at=time.time())
                self.store._put(db, "wb_question", q["id"], q)
            m.update(generation=m["generation"] + 1, coordinator_error=None, coordinator_continuations=0)
            m["coordinator_state"] = "pending" if m["semantic_seq"] > m["coordinator_cursor"] else "idle"
            self._event(db, m, "meeting.permissions", value.model_dump())
            self._save(db, m)
            return m

    def utterance(self, principal, mid, value):
        now = time.time()
        event = AudioEvent(meeting_id=mid, capture_epoch="manual", stream_id=principal["id"],
                           seq=1, type="transcript.final", source="microphone", occurred_at=str(now),
                           segment_id=value.segment_id or uid("segment"), revision=value.revision,
                           audio_start_ms=0, audio_end_ms=0, text=value.text)
        return self.audio(principal, mid, event, manual=True, event_id=value.event_id)

    def audio(self, principal, mid, value, manual=False, event_id=""):
        require(value.meeting_id == mid, "音频会议 ID 不匹配", 400)
        data = value.model_dump(exclude_none=True)
        refresh_index = False
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid, True)
            if not manual:
                self.store._acl(db, principal, m["project_id"], {"agent"})
            source = f"audio:{value.capture_epoch}:{value.stream_id}:{value.seq}"
            if manual:
                source = "manual:" + (event_id or f"{principal['id']}:{value.segment_id}:{value.revision}")
                # The receive clock is not part of a manual event's identity.
                data.pop("occurred_at", None)
            previous = db.execute("SELECT data FROM events WHERE meeting=? AND source=?", (mid, source)).fetchone()
            if previous:
                prior = json.loads(previous[0])
                require(prior["payload"] == data, "相同事件 ID 携带不同内容", 409)
                return prior
            epoch = self._capture_event_binding(m, value) if not manual and value.source != "file" else None
            if value.source == "file":
                job = m.get("audio_import", {})
                require(m.get("input_kind") == "audio_file" and job.get("import_id") == value.capture_epoch
                        and job.get("state") == "processing", "不是当前音频导入任务", 409)
                if value.stream_id == "file-" + value.capture_epoch:
                    job["last_event_seq"] = max(int(job.get("last_event_seq") or 0), value.seq)
                if value.type.startswith("transcript."):
                    require(value.audio_end_ms <= job["duration_ms"], "转写时间超出音频范围", 400)
            elif not manual:
                require(m.get("input_kind", "live") == "live", "音频导入会议不接收实时采音", 409)
            is_transcript = value.type.startswith("transcript.")
            require(m["state"] in {"active", "ended"}, "会议数据已到期，不再接收内容", 409)
            if m["state"] == "ended":
                require((is_transcript and value.type != "transcript.interim" and time.time() - m["ended_at"] <= 5)
                        or (value.type == "audio.recording" and time.time() - m["ended_at"] <= 15),
                        "会议已经结束", 409)
            if is_transcript:
                sid = f"{value.capture_epoch}:{value.stream_id}:{value.segment_id}"
                key = mid + ":" + sid
                row = db.execute("SELECT data FROM records WHERE kind='wb_segment' AND id=?", (key,)).fetchone()
                previous_segment = json.loads(row[0]) if row else None
                require(m["state"] != "ended" or previous_segment is not None, "结束后只接受已有片段的尾部修订", 409)
                if previous_segment:
                    require(value.revision > previous_segment["revision"], "片段修订过期", 409)
                segment = {**data, "id": sid, "segment_id": sid, "meeting_id": mid,
                           "event_seq": m["seq"] + 1, "received_at": time.time()}
                self.store._put(db, "wb_segment", key, segment)
                if previous_segment and previous_segment["type"] != "transcript.interim":
                    self.invalidate_detection_tasks(db, m, sid, value.revision)
                    m['transcript_revision_epoch'] = m.get('transcript_revision_epoch', 0) + 1
                    # A short ID-only journal lets a reader keep already-loaded
                    # pages while removing edited or withdrawn rows.
                    m['transcript_revision_changes'] = (m.get('transcript_revision_changes', []) + [{
                        'epoch': m['transcript_revision_epoch'], 'segment_id': sid, 'revision': value.revision,
                    }])[-64:]
                    for q in self._items(db, "wb_question", mid):
                        if any(ref["segment_id"] == sid for ref in q["source_refs"]):
                            q.update(state="needs_review", generation=q["generation"] + 1, basis_stale=True,
                                     stage="依据已更正，请确认问题范围", updated_at=time.time())
                            self.store._put(db, "wb_question", q["id"], q)
                            refresh_index = self._mark_document_stale(db, q) or refresh_index
                if value.type != "transcript.interim" and not m.get("coordinator_error") and m["coordinator_state"] not in {"failed", "outcome_unknown", "running", "retry_wait"}:
                    if m["coordinator_state"] != "pending":
                        m["pending_since"] = time.time()
                    m["coordinator_state"] = "pending"
            elif value.type.startswith("asr."):
                self._apply_speech_event(m, value, epoch)
            else:
                if value.type == "audio.recording":
                    self._recording_event(m, value)
                    if value.source == "file":
                        m["audio_import"]["original_state"] = value.state
                if value.type == "import.progress":
                    require(value.source == "file" and value.audio_end_ms is not None, "导入进度无效", 400)
                    job = m["audio_import"]
                    require(job["processed_ms"] <= value.audio_end_ms <= job["duration_ms"], "导入进度超出范围", 400)
                    job["processed_ms"] = value.audio_end_ms
                    self._save(db, m)
                    return {"type": value.type, "accepted": True}
                if value.type == "audio.level":
                    m["capture"].update(updated_at=time.time(), level=value.level if value.level is not None else min(value.rms or 0, 1))
                    self._save(db, m)
                    return {"type": value.type, "accepted": True}
                if value.type != "audio.recording":
                    m["capture"] = {"status": value.state or value.status or value.type.removeprefix("audio."), "updated_at": time.time(),
                                    "source": value.source, "level": value.level if value.level is not None else min(value.rms or 0, 1), "code": value.code}
                if value.type == "audio.status" and value.state == "stopped":
                    for recording in m.get("recordings", []):
                        if recording["epoch"] == value.capture_epoch and recording["state"] == "recording":
                            path = self.writer_for(m).path(recording["path"])
                            recording.update(state="partial" if path.is_file() and path.stat().st_size > 0 else "missing")
            event = self.store._append(db, m, source, value.type, principal["id"], data,
                                             is_transcript and value.type != "transcript.interim")
            self._save(db, m)
        if refresh_index or value.type == "audio.recording":
            self.publish_index(mid)
        if is_transcript and value.type != "transcript.interim" and m["state"] == "ended":
            self.archive_meeting(mid)
        elif value.type == "audio.status" and value.state == "stopped":
            self.archive_meeting(mid)
        return event

    def _refs(self, db, m, refs):
        for ref in refs:
            s = self.store._get(db, "wb_segment", m["id"] + ":" + ref["segment_id"])
            require(s["revision"] == ref["revision"] and s["type"] == "transcript.final", "依据发言已被更正或撤回", 409)

    def _discussion_context(self, db, meeting, question):
        """Pin relevant final utterances to this run without sending the whole meeting."""
        segments = sorted((s for s in self._items(db, "wb_segment", meeting["id"])
                           if s["type"] == "transcript.final"), key=lambda s: s["event_seq"])
        basis_ids = {r["segment_id"] for r in question["source_refs"]}
        positions = [i for i, s in enumerate(segments) if s["id"] in basis_ids]
        if not positions:
            return []
        basis_limit = min(5000, 14000 // len(positions))
        neighbours = set(range(max(0, positions[0] - 2), positions[0]))
        neighbours.update(range(positions[-1] + 1, min(len(segments), positions[-1] + 2)))
        context = []
        for i in sorted(set(positions) | neighbours):
            segment = segments[i]
            is_basis = segment["id"] in basis_ids
            limit = basis_limit if is_basis else 1000
            context.append({"segment_id": segment["id"], "revision": segment["revision"],
                            "text": segment["text"][:limit], "is_basis": is_basis,
                            "truncated": len(segment["text"]) > limit})
        return context

    def _new_question(self, db, m, title, refs, origin="manual", public_query="", candidate=False,
                      original_target=None, anchor_ref=None, evidence_need=None, grounding=None, web_search_requested=False):
        require(m["state"] == "active" or (origin == "coordinator" and m["state"] == "ended"), "会议已结束", 409)
        self._refs(db, m, refs)
        title = title.strip()
        require(bool(title), "问题不能为空", 400)
        query = public_query.strip()
        require(not web_search_requested or query, "请填写允许发往搜索服务的公开检索词", 400)
        scope = target_key(target_for({"title": title, "public_query": query, "original_target": original_target}))
        for q in self._items(db, "wb_question", m["id"]):
            # Only ignore sentence-ending question marks for automatic proposals with the same nonempty query.
            if q["title"] != title and not (origin == "coordinator" and query and q["public_query"] == query
                    and q["title"].rstrip("?？") == title.rstrip("?？")):
                continue
            if origin == "coordinator" and scope != target_key(target_for(q)):
                continue
            if web_search_requested and not (q.get("web_search_requested") and q["public_query"] == query):
                continue  # A prior model answer does not satisfy an explicit request for sources.
            if (origin == "coordinator" and q["state"] in {"cancelled", "paused", "outcome_unknown"}
                    and q["public_query"] == query):
                return q  # New model output cannot override a user's stop or retry decision.
            if (q["state"] not in {"cancelled", "superseded"}
                    and (origin != "coordinator" or q["public_query"] == query)):
                return q
        waiting = sum(q["state"] == "queued" for q in self._items(db, "wb_question", m["id"]))
        needs_approval = candidate and not m.get("permissions", {}).get("auto_search")
        state = "candidate" if needs_approval else ("backlog" if waiting >= 20 else "queued")
        q = {"id": uid("question"), "meeting_id": m["id"], "project_id": m["project_id"], "title": title,
             "original_title": title, "version": 1, "generation": 1, "state": state, "stage": "等待研究",
             "origin": origin, "manual_requested": origin == "manual", "request_kind": "inferred" if candidate else "explicit",
             "source_refs": refs, "public_query": public_query.strip(),
             "public_query_origin": "model" if public_query.strip() and origin == "coordinator" else "owner" if public_query.strip() else "", "priority": 0,
             "created_at": time.time(), "updated_at": time.time(), "basis_stale": False,
             "history": [], "result": None, "artifact_id": None}
        if web_search_requested:
            q["web_search_requested"] = True
        if original_target:
            q["original_target"] = original_target
        if evidence_need:
            q["evidence_need"] = evidence_need
        if grounding:
            q.update(grounding)
        wait_for_public_query(m, q)
        if refs:
            q["anchor_ref"] = anchor_ref or refs[0]
        self.store._put(db, "wb_question", q["id"], q)
        self._event(db, m, "question.created", {"question_id": q["id"], "state": state, "source_refs": refs})
        return q

    def question(self, principal, mid, value):
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid, True)
            q = self._new_question(db, m, value.title, [r.model_dump() for r in value.source_refs],
                                   public_query=value.public_query, web_search_requested=value.web_search_requested)
            self._save(db, m)
            return q

    def edit(self, principal, qid, value):
        with self.store.transaction() as db:
            q = self.store._get(db, "wb_question", qid)
            m = self._meeting(db, principal, q["meeting_id"], True)
            require(m["state"] in RESEARCH_MEETING_STATES, "会议数据已清理，无法查证", 409)
            require(q["version"] == value.expected_version, "问题已有新版本，请刷新", 409)
            require(bool(value.title.strip()), "问题不能为空", 400)
            q["history"].append({"version": q["version"], "title": q["title"], "state": q["state"], "result": q["result"]})
            q.update(title=value.title.strip(), version=q["version"] + 1, generation=q["generation"] + 1,
                     state="queued", stage="范围已更新，等待研究", public_query="", result=None, error=None, error_details=None,
                     manual_requested=True,
                     updated_at=time.time())
            q.pop("progress", None)
            q.pop("original_target", None)
            q.pop("web_search_requested", None)
            q.pop("evidence_need", None)
            q.pop("grounding_quote", None)
            q.pop("external_fact", None)
            # Update basis to the latest surviving final; retracted evidence needs manual confirmation.
            refs = []
            for ref in q["source_refs"]:
                s = self.store._get(db, "wb_segment", m["id"] + ":" + ref["segment_id"])
                if s["type"] == "transcript.final":
                    refs.append({"segment_id": s["id"], "revision": s["revision"]})
            q.update(source_refs=refs, basis_stale=False)
            if q.get("anchor_ref"):
                q["anchor_ref"] = next((ref for ref in refs if ref["segment_id"] == q["anchor_ref"]["segment_id"]), refs[0] if refs else None)
            self._mark_document_stale(db, q)
            self.store._put(db, "wb_question", qid, q)
            self._event(db, m, "question.revised", {"question_id": qid, "version": q["version"]})
            self._save(db, m)
        self.publish_index(m["id"])
        return q

    def action(self, principal, qid, value):
        with self.store.transaction() as db:
            q = self.store._get(db, "wb_question", qid)
            m = self._meeting(db, principal, q["meeting_id"], True)
            require(q["version"] == value.expected_version, "问题已有新版本，请刷新", 409)
            require(m["state"] in RESEARCH_MEETING_STATES, "会议数据已清理，无法查证", 409)
            if value.web_search_requested:
                require(value.action == "approve" and value.public_query.strip(), "请确认允许外发的检索词后再联网查证", 400)
            if value.action == "prioritize":
                q["priority"] = 1
            elif value.action == "cancel":
                q.update(state="cancelled", stage="已取消", generation=q["generation"] + 1)
            else:
                require(not q["basis_stale"], "先修改问题并确认更正后的依据", 409)
                require(q["state"] != "running", "问题正在执行", 409)
                q.update(state="queued", stage="等待研究", generation=q["generation"] + 1, error=None, error_details=None,
                         manual_requested=True)
                q.pop("resume_queued", None)
                q.pop("progress", None)
                q.pop("started_at", None)
                if value.public_query:
                    q["public_query"] = value.public_query.strip()
                    q["public_query_origin"] = "owner"
                    q["web_search_requested"] = value.web_search_requested
            q["updated_at"] = time.time()
            self.store._put(db, "wb_question", qid, q)
            self._event(db, m, "question." + value.action, {"question_id": qid})
            self._save(db, m)
            return q

    def control(self, principal, mid, action):
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid, True)
            require(action in {"pause_research", "resume_research", "retry_coordinator", "end"}, "未知会议操作", 400)
            require(m["state"] in RESEARCH_MEETING_STATES, "会议数据已清理，无法查证", 409)
            if action == "end":
                require(m["state"] == "active", "会议已结束，可继续已有问题的查证", 409)
                require(all(epoch['state'] in CAPTURE_SAVED for epoch in m.get('capture_epochs', {}).values()),
                        "录音仍在收尾或保存失败，请确认已保存再结束会议", 409)
                require(m.get("audio_import", {}).get("state") != "processing", "请先等待导入保存完成", 409)
            if action == "retry_coordinator":
                require(m["coordinator_state"] != "running", "问题识别正在执行，请等待本次结果", 409)
                require(m["coordinator_state"] in {"failed", "outcome_unknown"} or m.get("coordinator_error") or m.get('detection_service_error'),
                        "问题识别没有失败，无需重试", 409)
                require(not m["research_paused"], "请先恢复自动查证，再重试问题识别", 409)
                m.update(coordinator_continuations=0, coordinator_error=None,
                         coordinator_error_details=None, coordinator_error_message=None,
                         coordinator_state="pending" if m["semantic_seq"] > m["coordinator_cursor"] else "idle")
                m.pop("coordinator_retry", None)
                self.clear_detection_service_error(m)
            elif action == "end":
                m["research_paused"] = True
                # Completed answers remain publishable after capture stops.
                # Carry only their existing publication fence across this transition;
                # permission changes, edits and cancellation still invalidate it.
                self._carry_completed_publications(db, m)
                m["generation"] += 1
                m["coordinator_state"] = "idle"
                m.pop("coordinator_retry", None)
                m.update(state="ended", ended_at=time.time(), capture={"status": "stopped"})
            else:
                paused = action == "pause_research"
                if m["research_paused"] == paused:
                    return m
                # Scheduling controls do not invalidate paid work already in
                # flight, its publication fence, or its committed input cursor.
                m["research_paused"] = paused
                if not paused and m["coordinator_state"] == "idle" and not m.get("coordinator_error"):
                    m["coordinator_state"] = "pending" if m["semantic_seq"] > m["coordinator_cursor"] else "idle"
            for q in self._items(db, "wb_question", mid):
                if action == "end" and q["state"] in ACTIVE | {"backlog"}:
                    q.update(resume_queued=True, state="paused", stage="会议已结束，查证已暂停，可继续查证",
                             generation=q["generation"] + 1, updated_at=time.time())
                    if q.get("progress"):
                        q["progress"].update(finished_at=time.time(), updated_at=time.time())
                    self.store._put(db, "wb_question", q["id"], q)
                elif (action == "resume_research" and q["state"] == "paused" and not q["basis_stale"]
                      and (m["state"] == "ended" or q.get("resume_queued", False))):
                    # An explicit post-meeting resume includes jobs interrupted
                    # by End in older versions. Failed/unknown outcomes remain
                    # separate and still need their own explicit retry.
                    q.update(state="backlog", stage="等待研究", updated_at=time.time())
                    if m["state"] == "ended":
                        q["manual_requested"] = False
                    q.pop("resume_queued", None)
                    q.pop("progress", None)
                    q.pop("started_at", None)
                    self.store._put(db, "wb_question", q["id"], q)
            self._event(db, m, "meeting." + action, {})
            self._save(db, m)
        if action == "end":
            return self.archive_meeting(mid)
        return m

    @staticmethod
    def _automation_held(meeting, question):
        manual = question.get("manual_requested", question.get("origin") == "manual")
        pending = question["state"] in {"queued", "backlog"} or (
            question["state"] == "candidate" and meeting.get("permissions", {}).get("auto_search"))
        return bool(meeting["research_paused"] and pending and not manual)

    def _mark_document_stale(self, db, q):
        if q.get("artifact_id"):
            doc = self.store._get(db, "wb_document", q["artifact_id"])
            if not doc.get("basis_stale"):
                doc.update(basis_stale=True)
                if doc["state"] == "pending":
                    doc["state"] = "superseded"
                self.store._put(db, "wb_document", doc["id"], doc)
                return True
        return False

    def claim(self, principal):
        with self.store.transaction() as db:
            self.store._acl(db, principal, principal["project_id"], {"agent"})
            meetings = {m["id"]: m for m in self.store._all(db, "wb_meeting")}
            running = [q for q in self.store._all(db, "wb_question") if q["state"] == "running"]
            candidates = [q for q in self.store._all(db, "wb_question") if q["project_id"] == principal["project_id"]]
            for q in candidates:
                m = meetings[q["meeting_id"]]
                if q['state'] in {'queued', 'backlog', 'candidate'}:
                    wait_for_public_query(m, q)
                    if q['state'] == 'needs_query':
                        self.store._put(db, 'wb_question', q['id'], q)
                if (q["state"] == "candidate" and q["origin"] == "coordinator" and not q["basis_stale"]
                        and m["state"] in RESEARCH_MEETING_STATES and not m["research_paused"] and m.get("permissions", {}).get("auto_search")):
                    q.update(state="backlog", stage="等待研究", updated_at=time.time())
                    self.store._put(db, "wb_question", q["id"], q)
                    self._event(db, m, "question.auto_queued", {"question_id": q["id"]})
                    self._save(db, m)
            eligible = [q for q in candidates if meetings[q["meeting_id"]]["state"] in RESEARCH_MEETING_STATES
                        and not self._automation_held(meetings[q["meeting_id"]], q)]
            queued_count = sum(q["state"] == "queued" for q in eligible)
            for q in sorted((q for q in eligible if q["state"] == "backlog"), key=lambda q: q["created_at"]):
                if queued_count >= 20:
                    break
                q["state"] = "queued"
                self.store._put(db, "wb_question", q["id"], q)
                queued_count += 1
            if len(running) >= 2:
                return None
            # Backlog is a display bound, not a barrier for an urgent request.
            qs = [q for q in eligible if q["state"] in {"queued", "backlog"}]
            qs.sort(key=lambda q: (research_priority(q) != 0,
                                  coordinator_lane(meetings[q["meeting_id"]]) != "realtime" if research_priority(q) else False,
                                  research_priority(q), -q["priority"], q["created_at"]))
            schedule_row = db.execute("SELECT data FROM records WHERE kind='wb_schedule' AND id=?", (principal["project_id"],)).fetchone()
            schedule = json.loads(schedule_row[0]) if schedule_row else {"priority_streak": 0}
            aged = [q for q in qs if research_priority(q) == 2 and time.time() - q["created_at"] >= 120]
            if schedule["priority_streak"] >= 3 and aged:
                aged.sort(key=lambda q: q["created_at"])
                aged_ids = {q["id"] for q in aged}
                qs = aged + [q for q in qs if q["id"] not in aged_ids]
            background_aged = [q for q in qs if research_priority(q) != 0
                               and coordinator_lane(meetings[q["meeting_id"]]) == "background"
                               and time.time() - q["created_at"] >= 120]
            if schedule.get("realtime_streak", 0) >= 3 and background_aged:
                oldest = min(background_aged, key=lambda q: q["created_at"])
                qs = [q for q in qs if research_priority(q) == 0] + [oldest] + [q for q in qs if research_priority(q) != 0 and q["id"] != oldest["id"]]
            realtime_open = any(coordinator_lane(m) == "realtime" and not m["research_paused"]
                                and not self.blocked_reason(m) for m in meetings.values())
            background_running = sum(coordinator_lane(meetings[q["meeting_id"]]) == "background" for q in running)
            for q in qs:
                m = meetings[q["meeting_id"]]
                # While a live meeting is open, imported automatic research may
                # occupy only one slot. Manual requests remain explicit overrides.
                if realtime_open and coordinator_lane(m) == "background" and background_running >= 1 and research_priority(q) != 0:
                    continue
                reason = self.blocked_reason(m, q)
                if reason:
                    q["stage"] = reason
                    self.store._put(db, "wb_question", q["id"], q)
                    continue
                self._refs(db, m, q["source_refs"])
                schedule["priority_streak"] = min(3, schedule["priority_streak"] + 1) if research_priority(q) < 2 else 0
                schedule["realtime_streak"] = min(3, schedule.get("realtime_streak", 0) + 1) if coordinator_lane(m) == "realtime" else 0
                self.store._put(db, "wb_schedule", principal["project_id"], schedule)
                q.update(state="running", stage="searching", started_at=time.time(), lease_id=uid("lease"), error=None, error_details=None)
                q["progress"] = {"generation": q["generation"], "version": q["version"],
                    "started_at": q["started_at"], "updated_at": q["started_at"], "events": [], "sources": []}
                self.store._put(db, "wb_question", q["id"], q)
                self._event(db, m, "research.started", {"question_id": q["id"]})
                self._save(db, m)
                return {"meeting": m, "question": {**q, "discussion_context": self._discussion_context(db, m, q)}}
            return None

    def blocked_reason(self, meeting, question=None):
        if meeting["mode"] == "demo":
            return ""
        caps = self.capabilities
        if not caps.get("model_enabled"):
            return "模型未启用；可继续本地记录"
        permissions = meeting.get("permissions")
        model_allowed = permissions.get("model", False) if permissions is not None else meeting["id"] in caps.get("model_meeting_ids", [])
        search_allowed = permissions.get("search", False) if permissions is not None else meeting["id"] in caps.get("search_meeting_ids", [])
        if meeting["data_scope"] != "synthetic" and not model_allowed:
            return "本会议转写尚未批准发送到模型"
        if question is None or (not question.get("web_search_requested") and
                                (needs_internal_records(question) or is_grounded_explanation(question))):
            return ""
        if not caps.get("search_enabled"):
            return "搜索服务尚未配置或批准"
        if meeting["data_scope"] != "synthetic":
            if not search_allowed:
                return "本会议尚未批准外部搜索"
            if not question.get("public_query"):
                return "未生成公开检索词，请补充后查证" if permissions and permissions.get("auto_search") else "请先确认可公开发送的检索词"
        return ""

    def stage(self, principal, work, stage):
        with self.store.transaction() as db:
            q, m = self._valid_work(db, principal, work)
            q["stage"] = stage
            progress = q.setdefault("progress", {"events": [], "sources": []})
            now = time.time()
            progress.update(updated_at=now, stage_started_at=now)
            progress["events"] = (progress.get("events", []) + [{"kind": stage, "at": now}])[-16:]
            self.store._put(db, "wb_question", q["id"], q)
            self._event(db, m, "research." + stage, {"question_id": q["id"]})
            self._save(db, m)

    def progress(self, principal, work, kind, details):
        """Bounded task-local feedback, fenced exactly like final publication.

        Token deltas update a single record, not the global event log. Raw
        prompts, credentials and model reasoning are never progress payloads.
        """
        with self.store.transaction() as db:
            q, _ = self._valid_work(db, principal, work)
            if kind == 'model_dispatch_check':
                return
            p = q.setdefault("progress", {"events": [], "sources": []})
            now = time.time()
            p["updated_at"] = now
            if kind == "search_query":
                p["query"] = str(details.get("query", ""))[:128]
            elif kind == "sources_ready":
                from live.source_reader import source_url
                sources = []
                for source in details.get("sources", [])[:6]:
                    try:
                        url = source_url(source["url"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    sources.append({"id": str(source.get("id", ""))[:40], "url": url,
                        "title": str(source.get("title", ""))[:300], "excerpt": str(source.get("text", ""))[:240],
                        "retrieved_at": source.get("retrieved_at"), "published_at": source.get("published_at"),
                        "read_status": "read", "read_method": source.get("read_method")})
                p.update(sources=sources, result_count=max(0, int(details.get("result_count", 0))))
            elif kind.startswith("model_"):
                model = p.setdefault("model", {})
                if kind == "model_queued":
                    model.update(phase="queued", queued_at=now)
                elif kind == "model_timeout_retry":
                    model.update(timeout_retry="queued", timeout_request_id=details.get("request_id"),
                                 phase="queued", request_started_at=None, last_response_at=None, draft="")
                elif kind == "model_request":
                    if model.get("timeout_retry") == "queued":
                        model.update(timeout_retry="sent", retry_request_id=details.get("request_id"))
                    model.update(phase="waiting", request_started_at=now, last_response_at=None,
                                 timeout_seconds=details.get("timeout_seconds"), answer_chars=0, reasoning_chars=0, draft="")
                    model["attempt"] = model.get("attempt", 0) + 1
                elif kind == "model_response":
                    model.update(mode=details.get("mode"), last_response_at=now)
                elif kind == "model_delta":
                    for name in ("phase", "mode", "answer_chars", "reasoning_chars", "draft"):
                        if name in details:
                            # Bound the progress snapshot independently of the published answer.
                            model[name] = str(details[name])[:1024] if name == "draft" else details[name]
                    model["last_response_at"] = now
                elif kind == "model_validating":
                    model.update(phase="validating")
                elif kind == "model_correcting":
                    model.update(phase="correcting", format_corrected=True, draft="")
                else:
                    raise ValueError("unknown research progress")
            else:
                raise ValueError("unknown research progress")
            if kind not in {"model_delta", "model_response"}:
                p["events"] = (p.get("events", []) + [{"kind": kind, "at": now,
                    **({"count": len(p["sources"])} if kind == "sources_ready" else {})}])[-16:]
            self.store._put(db, "wb_question", q["id"], q)

    def _valid_work(self, db, principal, work):
        expected = work["question"]
        q = self.store._get(db, "wb_question", expected["id"])
        m = self._meeting(db, principal, q["meeting_id"], True)
        self.store._acl(db, principal, m["project_id"], {"agent"})
        require(m["state"] in RESEARCH_MEETING_STATES and q["state"] == "running"
                and q["generation"] == expected["generation"] and q["version"] == expected["version"]
                and q.get("lease_id") == expected.get("lease_id"), "任务已取消、暂停或更正，忽略晚到结果", 409)
        self._refs(db, m, q["source_refs"])
        return q, m

    def finish(self, principal, work, result):
        from live.source_reader import validate_result
        result = validate_result(result)
        with self.store.transaction() as db:
            q, m = self._valid_work(db, principal, work)
            result = with_original_evidence(q, result)
            q.update(state="completed" if result["evidence_status"] in {"supported", "mixed", "model_explanation"} else "insufficient",
                     stage="publishing", result=result, error=None, error_details=None, updated_at=time.time())
            if q.get("progress"):
                q["progress"].update(finished_at=time.time(), updated_at=time.time())
            did = q["artifact_id"] or uid("document")
            row = db.execute("SELECT data FROM records WHERE kind='wb_document' AND id=?", (did,)).fetchone()
            old = json.loads(row[0]) if row else {}
            doc = {"id": did, "meeting_id": m["id"], "question_id": q["id"], "question_version": q["version"],
                   "path": self.writer_for(m).document_path(m, q), "state": "pending", "basis_stale": False,
                   "question_generation": q["generation"], "meeting_generation": m["generation"],
                   "executor_id": principal["id"],
                   "content": render_document(m, q, result), "hash": old.get("hash"), "created_at": old.get("created_at", time.time()),
                   "history": old.get("history", []) + ([{"version": old["question_version"], "path": old["path"], "content": old["content"], "hash": old.get("hash")}] if old else [])}
            q["artifact_id"] = did
            self.store._put(db, "wb_document", did, doc)
            self.store._put(db, "wb_question", q["id"], q)
            self._event(db, m, "research.completed", {"question_id": q["id"], "evidence_status": result["evidence_status"]})
            self._save(db, m)
        self.publish(did)
        return q

    def fail(self, principal, work, code, details=None):
        with self.store.transaction() as db:
            try:
                q, m = self._valid_work(db, principal, work)
            except LiveError as exc:
                if exc.status in {404, 409}:
                    return
                raise
            q.update(state="outcome_unknown" if code == "outcome_unknown" else "failed", stage=code,
                     error=failure_message(details) if details else None, error_details=details, updated_at=time.time())
            if q.get("progress"):
                q["progress"].update(finished_at=time.time(), updated_at=time.time())
            self.store._put(db, "wb_question", q["id"], q)
            self._event(db, m, "research.failed", {"question_id": q["id"], "code": code, **({"error_details": details} if details else {})})
            self._save(db, m)

    def _carry_completed_publications(self, db, meeting):
        """Carry valid completed output across stop/restart, never across revocation."""
        for doc in self._items(db, "wb_document", meeting["id"]):
            if doc["state"] in {"pending", "save_failed"} and doc.get("meeting_generation") == meeting["generation"]:
                doc["meeting_generation"] = meeting["generation"] + 1
                self.store._put(db, "wb_document", doc["id"], doc)

    def publish(self, did):
        # The short transaction fences cancellation/revision against filesystem publication.
        with self.store.transaction() as db:
            doc = self.store._get(db, "wb_document", did)
            if doc["state"] == "saved":
                return doc
            if doc["state"] not in {"pending", "save_failed"}:
                return doc
            q = self.store._get(db, "wb_question", doc["question_id"])
            m = self.store._get(db, "wb_meeting", doc["meeting_id"])
            executor = self.store._get(db, "member", doc["executor_id"]) if doc.get("executor_id") else {}
            if (m["state"] not in RESEARCH_MEETING_STATES or m["generation"] != doc.get("meeting_generation")
                    or not executor.get("enabled") or executor.get("role") != "agent" or executor.get("project_id") != m["project_id"]
                    or q["state"] not in {"completed", "insufficient"}
                    or q["generation"] != doc.get("question_generation") or q["version"] != doc["question_version"]):
                doc["state"] = "superseded"
                self.store._put(db, "wb_document", did, doc)
                return doc
            try:
                doc["hash"] = self.writer_for(m).write(doc["path"], doc["content"], doc.get("hash"))
                doc.update(state="saved", saved_at=time.time(), error=None)
            except (OSError, LiveError) as exc:
                conflict = isinstance(exc, LiveError) and exc.details.get("file_conflict")
                doc.update(state="conflict" if conflict else "save_failed",
                           error="人工编辑冲突" if conflict else str(exc) if isinstance(exc, LiveError) else "无法写入目录")
            self.store._put(db, "wb_document", did, doc)
            q["stage"] = "已保存" if doc["state"] == "saved" else doc["error"]
            self.store._put(db, "wb_question", q["id"], q)
            self._event(db, m, "document." + doc["state"], {"document_id": did, "question_id": q["id"]})
            if doc["state"] == "saved" and (sync := getattr(self, "feishu_sync", None)) is not None:
                sync.enqueue_saved(db, m, q, doc)
            self._save(db, m)
        self.publish_index(doc["meeting_id"])
        return doc

    def publish_index(self, mid):
        with self.store.transaction() as db:
            m = self.store._get(db, "wb_meeting", mid)
            if not m["directory"] or m["state"] in {"deleting", "delete_failed"}:
                return
            docs = sorted(self._items(db, "wb_document", mid), key=lambda d: d["created_at"])
            lines = [f"# {m['title']}", "", f"会议 ID：{mid}", "", "本目录中的材料用于会议参考，最终判断由人确认。", ""]
            from urllib.parse import quote
            for recording in m.get("recordings", []):
                label = '会议录音 M4A' if recording.get('archive_state') in {'cleanup', 'cleanup_failed', 'saved'} else '原始音频'
                lines += [f"- [{label} · {recording['source']}]({quote(recording['path'].split('/')[-1])}) · {recording['state']}"]
                if recording.get('raw_path') and recording.get('keep_original_caf'):
                    lines += [f"- [原始 CAF · {recording['source']}]({quote(Path(recording['raw_path']).name)})"]
            for kind, label in (("transcript", "全量转录"), ("summary", "会议纪要")):
                base = m.get("base_files", {}).get(kind)
                if base:
                    lines += [f"- [{label}]({quote(base['path'].split('/')[-1])}) · {base['state']}"]
            lines += [""]
            if m.get("audio_import", {}).get("transcript_state") == "saved":
                lines += ["- [会议转写](" + m["audio_import"]["transcript_path"].split("/")[-1] + ")", ""]
            for d in docs:
                q = self.store._get(db, "wb_question", d["question_id"])
                label = q["title"].replace("[", "（").replace("]", "）").replace("\n", " ")
                from urllib.parse import quote
                lines.append(f"- [{label}]({quote(d['path'].split('/')[-1])}) · v{d['question_version']} · {d['state']}" + (" · 依据已更正" if d["basis_stale"] else ""))
            content = "\n".join(lines) + "\n"
            try:
                index_path = m["directory"] + f"/index-{m['seq']:08d}.md"
                m["index_hash"] = self.writer_for(m).write(index_path, content)
                m["index_path"] = index_path
                m["index_paths"] = list(dict.fromkeys(m.get("index_paths", []) + [index_path]))
                m["index_state"] = "saved"
            except (OSError, LiveError):
                m["index_state"] = "save_failed"
            self._save(db, m)

    def artifact(self, principal, did):
        with self.store.transaction() as db:
            doc = self.store._get(db, "wb_document", did)
            m = self._meeting(db, principal, doc["meeting_id"])
            path = self.writer_for(m).path(doc["path"])
            actual = path.read_text(encoding="utf-8") if path.exists() else None
            return {"document": {**doc, "absolute_path": str(path), "on_disk": actual is not None,
                                  "externally_modified": actual is not None and digest(actual) != doc.get("hash")},
                    "content": actual if actual is not None else doc["content"]}

    def recover(self, publish=True):
        # Registered local publications may be recovered; no model/search request
        # is replayed. Cancelled/revised publications still fail their generation fence.
        if publish:
            with self.store.transaction() as db:
                pending = [d["id"] for d in self.store._all(db, "wb_document") if d["state"] in {"pending", "save_failed"}]
            for did in pending:
                self.publish(did)
        interrupted = []
        with self.store.transaction() as db:
            for q in self.store._all(db, "wb_question"):
                if q["state"] == "running":
                    q.update(state="outcome_unknown", stage="上次执行被中断，请确认后重试", generation=q["generation"] + 1)
                    self.store._put(db, "wb_question", q["id"], q)
                elif q["state"] in {"queued", "backlog"}:
                    wait_for_public_query(self.store._get(db, 'wb_meeting', q['meeting_id']), q)
                    if q['state'] != 'needs_query':
                        q.update(state="paused", resume_queued=True, stage="重启后已暂停，恢复研究后继续", generation=q["generation"] + 1)
                    self.store._put(db, "wb_question", q["id"], q)
            for m in self.store._all(db, "wb_meeting"):
                if m["state"] in {"deleting", "delete_failed"}:
                    m.update(state="delete_failed", deletion_error=m.get("deletion_error") or "上次删除被中断，请在会议列表重试删除")
                    self._save(db, m)
                    continue
                m["capture"] = {"status": "disconnected"}
                for recording in m.get("recordings", []):
                    if recording["state"] in {"recording", "copying", "unavailable"}:
                        try:
                            path = self.writer_for(m).path(recording["path"])
                            recording.update(state="partial" if path.is_file() and path.stat().st_size > 0 else "missing")
                            recording.pop("storage_error", None)
                            interrupted.append(m["id"])
                        except (OSError, LiveError):
                            recording.update(state="unavailable", storage_error="原保存目录不可用，录音是否完整尚未核实；请连接原磁盘后重开应用。")
                if m.get("summary", {}).get("state") == "running":
                    from live.meeting_archive import final_segments, render_summary
                    m["summary"].update(state="outcome_unknown", error="outcome_unknown")
                    self._archive_file(db, m, "summary", render_summary(m, final_segments(self, db, m), status="outcome_unknown"), "03-会议纪要-本机摘录")
                if m.get("audio_import", {}).get("state") == "processing":
                    m["audio_import"].update(state="interrupted", failed_at_ms=m["audio_import"]["processed_ms"], error="应用退出中断了导入；已有转写保留，可重新导入为一场新会议。")
                recovery_pending = self.recover_detection_tasks(db, m)
                unfinished = recovery_pending or m["coordinator_state"] in {"running", "pending", "retry_wait"} or any(q["state"] in {"paused", "outcome_unknown"} for q in self._items(db, "wb_question", m["id"]))
                if m["coordinator_state"] == "running":
                    m["coordinator_state"] = "outcome_unknown"
                elif m["coordinator_state"] in {"pending", "retry_wait"}:
                    m["coordinator_state"] = "idle"
                m.pop("coordinator_retry", None)
                if unfinished:
                    self._carry_completed_publications(db, m)
                    m.update(research_paused=True, generation=m["generation"] + 1)
                self._save(db, m)
        for mid in set(interrupted):
            if publish:
                self.archive_meeting(mid)

    def coordinator_work(self, principal, lane=None):
        with self.store.transaction() as db:
            self.store._acl(db, principal, principal["project_id"], {"agent"})
            meetings = self.store._all(db, "wb_meeting")
            # Within each independent lane, rotate meetings after each batch.
            # Legacy/direct callers still prefer realtime when no lane is given.
            meetings.sort(key=lambda m: (coordinator_lane(m) != "realtime" if lane is None else False,
                                         m.get("last_coordinator_claim_at", 0), m["created_at"], m["id"]))
            for m in meetings:
                if (lane is not None and coordinator_lane(m) != lane) or m["project_id"] != principal["project_id"] or m["state"] not in RESEARCH_MEETING_STATES or m["research_paused"] or m["coordinator_state"] not in {"pending", "retry_wait"}:
                    continue
                if self.blocked_reason(m):
                    continue
                if not self.detection_service_ready(m):
                    continue
                retry = m.get("coordinator_retry", {})
                if m["coordinator_state"] == "retry_wait" and time.time() < retry.get("next_at", 0):
                    continue
                segments = sorted((s for s in self._items(db, "wb_segment", m["id"]) if s["type"] == "transcript.final"), key=lambda s: s["event_seq"])
                unread = [s for s in segments if s["event_seq"] > m["coordinator_cursor"]]
                importing = m.get("input_kind") == "audio_file"
                if importing and unread and m.get("audio_import", {}).get("state") == "processing":
                    # Let short fragments coalesce, but do not hold a tail longer
                    # than three seconds. EOF bypasses this wait entirely.
                    if sum(len(s["text"]) for s in unread) < IMPORT_MIN_CHARACTERS and time.time() - unread[0]["received_at"] < 3:
                        continue
                elif not importing and m["state"] == "active" and m["mode"] != "demo" and segments:
                    debounce = self.capabilities.get("debounce_seconds", .8)
                    if time.time() - max(s["received_at"] for s in segments) < debounce and time.time() - m.get("pending_since", 0) < 3:
                        continue
                # Reserve recent context before filling a catch-up batch. Only
                # background may be shortened; unread input is never skipped.
                context, context_size = [], 0
                for s in reversed(segments):
                    if s["event_seq"] > m["coordinator_cursor"]:
                        continue
                    room = COORDINATOR_CONTEXT_CHARACTERS - context_size
                    if len(context) >= COORDINATOR_CONTEXT_SEGMENTS or not room:
                        break
                    if len(s["text"]) > room:
                        if not context:
                            context.append({**s, "text": s["text"][-room:], "truncated": True})
                            context_size += room
                        break
                    context.append(s)
                    context_size += len(s["text"])
                catching_up = len(unread) >= 8 or bool(unread and time.time() - unread[0]["received_at"] >= 15)
                batch_limit = IMPORT_MAX_SEGMENTS if importing else COORDINATOR_CATCHUP_SEGMENTS if catching_up else COORDINATOR_BATCH_SEGMENTS
                # A dense imported batch may exceed the 12-action schema. Revisit
                # it in small batches rather than exhausting continuations on the
                # same large input. Already created questions remain in context.
                if importing and m.get("coordinator_continuations", 0):
                    batch_limit = COORDINATOR_BATCH_SEGMENTS
                batch, size, new_size = [], context_size, 0
                for s in unread:
                    if len(batch) == batch_limit or size + len(s["text"]) > 12000 or (importing and batch and new_size >= IMPORT_BATCH_CHARACTERS):
                        break
                    batch.append(s)
                    size += len(s["text"])
                    new_size += len(s["text"])
                cursor = batch[-1]["event_seq"] if len(batch) < len(unread) else m["semantic_seq"]
                if m["coordinator_state"] == "retry_wait":
                    self._event(db, m, "coordinator.retrying", {"attempt": retry["attempts"], "max_attempts": len(COORDINATOR_RETRY_DELAYS)})
                    retry["state"] = "running"
                m["coordinator_state"] = "running"
                m["last_coordinator_claim_at"] = time.time()
                self._save(db, m)
                return {"meeting": m, "cursor": cursor, "segments": context[::-1] + batch,
                        "lane": coordinator_lane(m),
                        "action_limit": 12 if importing or len(batch) > COORDINATOR_BATCH_SEGMENTS else 4,
                        "questions": sorted(self._items(db, "wb_question", m["id"]), key=lambda q: q["updated_at"])}
        return None

    def validate_coordinator_request(self, principal, work):
        """Recheck authority after waiting for a model slot, including correction calls."""
        with self.store.transaction() as db:
            if work.get('detection_task'):
                self._detection_authority(db, principal, work)
                return
            m = self._meeting(db, principal, work["meeting"]["id"])
            require(m["state"] in RESEARCH_MEETING_STATES and m["generation"] == work["meeting"]["generation"], "协调上下文已失效", 409)
            require(not self.blocked_reason(m), "本场问题识别尚未授权", 403)

    def commit_decisions(self, principal, work, decisions):
        refresh_index = False
        with self.store.transaction() as db:
            m = self._meeting(db, principal, work["meeting"]["id"], True)
            self.store._acl(db, principal, m["project_id"], {"agent"})
            require(m["state"] in RESEARCH_MEETING_STATES
                    and m["generation"] == work["meeting"]["generation"], "协调上下文已失效", 409)
            if m["coordinator_cursor"] >= work["cursor"]:
                return  # This exact input was already committed.
            action_limit = work.get("action_limit", 4)
            progress, refresh_index = self._commit_detection_actions(db, m, work, decisions)
            # A full response may have more actions to emit for the same input.
            # Preserve its cursor and supply the newly created questions next time.
            saturated = sum(a.kind != "no_action" for a in decisions.actions) >= action_limit
            next_cursor = m["coordinator_cursor"] if saturated else work["cursor"]
            continuations = m.get("coordinator_continuations", 0) + 1 if saturated else 0
            blocked = saturated and (progress == 0 or continuations >= 3)
            m.update(coordinator_cursor=next_cursor, coordinator_continuations=continuations,
                     coordinator_state="failed" if blocked else "pending" if m["semantic_seq"] > next_cursor else "idle",
                     coordinator_error="continuation_no_progress" if blocked and not progress else "continuation_limit" if blocked else None,
                     coordinator_error_details=None, coordinator_error_message=None)
            m.pop("coordinator_retry", None)
            self._event(db, m, "coordinator.processed", {"cursor": next_cursor, "action_count": len(decisions.actions), "continuation_required": saturated})
            if blocked:
                self._event(db, m, "coordinator.failed", {"code": m["coordinator_error"]})
            self._save(db, m)
        if refresh_index:
            self.publish_index(m["id"])

    def _commit_detection_actions(self, db, m, work, decisions):
        refresh_index = False
        action_limit = work.get("action_limit", 4)
        require(len(decisions.actions) <= action_limit, "问题识别动作超过本批上限", 400)
        visible = {(s["id"], s["revision"]) for s in work["segments"]}
        new_evidence = {(s["id"], s["revision"]): s.get("event_seq", work["cursor"]) for s in work["segments"]
                        if s.get("event_seq", work["cursor"]) > work["meeting"]["coordinator_cursor"]}
        known_questions = {q["id"] for q in self._items(db, "wb_question", m["id"])}
        progress = 0
        for decision in decisions.actions:
            if decision.kind == "no_action":
                continue
            refs = [r.model_dump() for r in decision.source_refs]
            require(bool(refs) and all((r["segment_id"], r["revision"]) in visible for r in refs), "模型引用不在输入上下文", 400)
            self._refs(db, m, refs)
            if not any((r["segment_id"], r["revision"]) in new_evidence for r in refs):
                continue
            anchor = max(refs, key=lambda r: new_evidence.get((r["segment_id"], r["revision"]), -1))
            target = decision.original_target.model_dump() if decision.original_target else None
            grounding = {k: v for k in ('grounding_quote', 'external_fact') if (v := getattr(decision, k, ''))}
            if decision.kind in {"create_question", "candidate"}:
                query = decision.public_query if m["data_scope"] == "synthetic" or m.get("permissions", {}).get("auto_search") else ""
                created = self._new_question(db, m, decision.title, refs, origin="coordinator", public_query=query,
                    candidate=decision.kind == "candidate", original_target=target, anchor_ref=anchor,
                    evidence_need=decision.evidence_need, grounding=grounding)
                if created["id"] not in known_questions:
                    progress += 1
                    known_questions.add(created["id"])
                elif (created["origin"] == "coordinator" and not created["basis_stale"]
                      and created["state"] in UNSTARTED | {"running", "completed", "insufficient"}
                      and created["public_query"] == query):
                    if decision.kind == "create_question":
                        created["request_kind"] = "explicit"
                    progress += self._mention_question(db, m, created, refs, decision.evidence_need, grounding)
            else:
                q = self.store._get(db, "wb_question", decision.question_id)
                supplied = next((old for old in work["questions"] if old["id"] == q["id"]), None)
                require(supplied is not None and q["meeting_id"] == m["id"] and q["version"] == decision.expected_version
                        and q["version"] == supplied["version"] and q["generation"] == supplied["generation"]
                        and q["state"] not in {"cancelled", "paused", "outcome_unknown"} and not q["basis_stale"], "问题版本或操作状态已失效", 409)
                if decision.kind == "cancel_candidate":
                    require(q["state"] == "candidate", "模型只能直接取消尚未批准的候选", 409)
                    q.update(state="cancelled", generation=q["generation"] + 1)
                else:
                    if not decision.scope_changed:
                        query = decision.public_query.strip() if m["data_scope"] == "synthetic" or m.get("permissions", {}).get("auto_search") else ""
                        require(q["title"] == decision.title.strip() and
                                (not query or not q['public_query'] or q['public_query'] == query),
                                "范围未变的补充不能替换问题或检索词", 400)
                        require(not target or target_key(target_for({**q, "original_target": target})) == target_key(target_for(q)),
                                "范围未变的补充不能替换原文目标", 400)
                        progress += self._mention_question(db, m, q, refs, decision.evidence_need, grounding, query)
                        continue
                    query = decision.public_query if m["data_scope"] == "synthetic" or m.get("permissions", {}).get("auto_search") else ""
                    if (q["title"] == decision.title.strip() and q["source_refs"] == refs and q["public_query"] == query
                            and q.get("original_target") == target and q.get('evidence_need') == decision.evidence_need):
                        continue
                    q["history"].append({"title": q["title"], "version": q["version"], "result": q["result"],
                                         "source_refs": q["source_refs"], "public_query": q["public_query"]})
                    q.update(title=decision.title.strip(), version=q["version"] + 1, generation=q["generation"] + 1,
                             source_refs=refs, anchor_ref=anchor, original_target=target, evidence_need=decision.evidence_need,
                             state="candidate" if q["state"] == "candidate" and not m.get("permissions", {}).get("auto_search") else "queued",
                             basis_stale=False, result=None, error=None, error_details=None,
                             manual_requested=False,
                             updated_at=time.time(),
                             public_query=decision.public_query if m["data_scope"] == "synthetic" or m.get("permissions", {}).get("auto_search") else "")
                    q.pop("progress", None)
                    q.pop("grounding_quote", None)
                    q.pop("external_fact", None)
                    q.pop("web_search_requested", None)
                    q.update(grounding)
                    q["public_query_origin"] = "model" if q["public_query"] else ""
                    wait_for_public_query(m, q)
                    refresh_index = self._mark_document_stale(db, q) or refresh_index
                self.store._put(db, "wb_question", q["id"], q)
                progress += 1
        return progress, refresh_index

    def _mention_question(self, db, meeting, question, refs, evidence_need=None, grounding=None, public_query=''):
        """Link a same-scope mention without invalidating a paid in-flight run.

        Only unstarted questions absorb new basis. Completed/running answers
        retain exactly the basis actually used; later mentions are audit data.
        """
        changed = refs != question.get("last_mention_refs", question["source_refs"])
        if question['state'] in UNSTARTED and evidence_need:
            previous = question.get('evidence_need')
            needs = {previous, evidence_need} - {None, 'explanation'}
            merged = 'mixed' if len(needs) > 1 else next(iter(needs), 'explanation')
            question['evidence_need'] = merged
            if evidence_need in {'public', 'mixed'} and public_query and not question['public_query']:
                question.update(public_query=public_query, public_query_origin='model')
                if question['state'] == 'needs_query':
                    question.update(state='backlog', stage='等待研究')
                changed = True
            wait_for_public_query(meeting, question)
            # Preserve the rationale for the strongest evidence requirement. A
            # later conceptual aside must not replace an existing public fact.
            if grounding and (not question.get('grounding_quote') or merged != previous):
                if len({(r['segment_id'], r['revision']) for r in question['source_refs'] + refs}) <= 12:
                    if merged == evidence_need or (merged == 'mixed' and grounding.get('external_fact')):
                        question.update(grounding)
                        changed = True
            changed = changed or previous != merged
        if changed:
            question["last_mention_refs"] = refs
            if question["state"] in UNSTARTED:
                combined = list(question["source_refs"])
                combined.extend(ref for ref in refs if ref not in combined)
                if len(combined) <= 12:
                    question["source_refs"] = combined
            question["updated_at"] = time.time()
            self._event(db, meeting, "question.mentioned", {"question_id": question["id"], "source_refs": refs,
                                                          "version": question["version"]})
        self.store._put(db, "wb_question", question["id"], question)
        return int(changed)

    def coordinator_failed(self, principal, work, unknown=False, code="request_failed", details=None):
        if work.get('detection_task'):
            return self.detection_recovery_failed(principal, work, details)
        allowed = {"outcome_unknown", "budget_exhausted", "model_contract_invalid", "context_invalid", "request_failed"}
        code = code if code in allowed else "request_failed"
        with self.store.transaction() as db:
            try:
                m = self._meeting(db, principal, work["meeting"]["id"])
            except LiveError as exc:
                if exc.status == 404:
                    return
                raise
            if (m["state"] in RESEARCH_MEETING_STATES and m["generation"] == work["meeting"]["generation"]
                    and m["coordinator_cursor"] < work["cursor"]):
                # A known, locally rejected result can be superseded by newer
                # transcript revisions. Only pure detection has bounded recovery;
                # unknown research/summary calls still require explicit retry.
                attempts = m.get("coordinator_retry", {}).get("attempts", 0)
                # The total semantic cursor can already exceed this batch's cursor
                # when claimed. Backlog is not evidence of a new input revision.
                # Even a real concurrent edit must not bypass bounded recovery.
                retryable = (not unknown and code == "context_invalid") or transient_detection_failure(details)
                changed_input = (not unknown and code == "context_invalid"
                                 and m["semantic_seq"] > work["meeting"]["semantic_seq"])
                recovering = retryable and attempts < len(COORDINATOR_RETRY_DELAYS)
                m["coordinator_state"] = "pending" if recovering and changed_input else "retry_wait" if recovering else "outcome_unknown" if unknown else "failed"
                m["coordinator_error"] = code
                m["coordinator_error_details"] = details
                m["coordinator_error_message"] = failure_message(details) if details else None
                self._event(db, m, "coordinator.failed", {"code": code, **({"error_details": details} if details else {})})
                if recovering:
                    requested = (details or {}).get("retry_after_seconds", 0)
                    delay = 0 if changed_input else max(COORDINATOR_RETRY_DELAYS[attempts], min(300, requested) if type(requested) is int else 0)
                    m["coordinator_retry"] = {"state": "scheduled", "attempts": attempts + 1,
                        "max_attempts": len(COORDINATOR_RETRY_DELAYS), "delay_seconds": delay, "next_at": time.time() + delay}
                    self._event(db, m, "coordinator.retry_scheduled", m["coordinator_retry"])
                elif retryable:
                    m["coordinator_retry"] = {"state": "exhausted", "attempts": attempts,
                        "max_attempts": len(COORDINATOR_RETRY_DELAYS)}
                else:
                    m.pop("coordinator_retry", None)
                self._save(db, m)

    def demo(self, principal, mid):
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid, True)
            require(m["data_scope"] == "synthetic" and m["state"] == "active", "仅限合成测试会议", 409)
            require(not self._items(db, "wb_segment", mid) and not self._items(db, "wb_question", mid), "演示只能加入空的合成会议", 409)
            m["mode"] = "demo"
            self._save(db, m)
        from live.workbench_contracts import Utterance
        self.utterance(principal, mid, Utterance(segment_id="demo-request", text="查一下工作流和智能体的区别，整理佐证即可。"))
        self.utterance(principal, mid, Utterance(segment_id="demo-ordinary", text="我先把下一页的方案展示一下。"))
        return self.snapshot(principal, mid)
