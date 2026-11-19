"""File input follows the existing research pipeline; no external calls here."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from live.source_reader import Researcher
from live.store import LiveError
from live.workbench_contracts import AudioEvent, AudioImport, AudioImportFinish, Decisions, MeetingPermissions, NewMeeting, NewQuestion
from live.workbench_retention import purge
from live.workbench_runtime import WorkbenchRuntime
from live_api import create_app


class AudioImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = create_app(self.root / "test.sqlite3", {"TESTING": True, "WORKBENCH_ROOT": self.root / "meetings",
            "WORKBENCH_CAPABILITIES": {"audio_enabled": True, "model_enabled": True, "search_enabled": True, "debounce_seconds": 0}})
        self.store = self.app.extensions["live_store"]
        seed = self.store.seed(fixture=True)
        self.owner, self.member, _, self.agent = [self.store.authenticate(m["token"]) for m in seed["members"]]
        self.wb = self.app.extensions["workbench"]
        self.meeting = self.wb.create(self.owner, NewMeeting(title="导入测试", input_kind="audio_file"))
        self.mid = self.meeting["id"]
        self.job = AudioImport(import_id="file-test", file_name="会议.m4a", duration_ms=180000, bytes=1024)
        self.client = self.app.test_client()
        self.owner_headers = {"Authorization": "Bearer fixture-owner-0"}
        self.agent_headers = {"Authorization": "Bearer " + seed["members"][-1]["token"]}

    def begin(self):
        return self.wb.begin_import(self.agent, self.mid, self.job)

    def event(self, n=1, text="请查一下工作流的定义，提供原文来源。", **changes):
        value = dict(meeting_id=self.mid, capture_epoch="file-test", stream_id="file-file-test", seq=n,
                     type="transcript.final", source="file", occurred_at="2026-11-15T05:00:00Z",
                     segment_id=f"f{n}", revision=1, audio_start_ms=n * 1000, audio_end_ms=n * 1000 + 500, text=text)
        value.update(changes)
        return AudioEvent(**value)

    def finish(self, state="completed"):
        return self.wb.finish_import(self.agent, self.mid, AudioImportFinish(import_id="file-test", state=state))

    def researcher(self):
        quote = "工作流通过预先设定的步骤组织处理过程。"
        class Search:
            def search(_, query):
                return [{"url": "https://fixture.invalid/workflow", "title": "离线测试资料", "content": ("这是合成测试正文，不能用于业务判断。" + quote) * 8}]
        class Model:
            def answer(_, meeting, question, sources):
                return {"answer": "合成参考回答，仅验证导入到文档的流程。", "claims": [
                    {"text": "合成材料描述预设步骤。", "source_id": sources[0]["id"], "quote": quote}],
                    "uncertainty": ["未执行真实搜索或模型调用。"], "evidence_status": "supported"}
        return Researcher(Search(), Model())

    def test_import_routes_require_agent_origin_and_audio_capability(self):
        url = f"/internal/live/v2/meetings/{self.mid}/audio-import"
        self.assertEqual(self.client.post(url, json=self.job.model_dump()).status_code, 401)
        self.assertEqual(self.client.post(url, json=self.job.model_dump(), headers=self.owner_headers).status_code, 403)
        self.assertEqual(self.client.post(url, json=self.job.model_dump(), headers={**self.agent_headers, "Origin": "https://evil.invalid"}).status_code, 403)
        self.wb.capabilities["audio_enabled"] = False
        self.assertEqual(self.client.post(url, json=self.job.model_dump(), headers=self.agent_headers).status_code, 409)
        self.wb.capabilities["audio_enabled"] = True
        self.assertEqual(self.client.post(url, json=self.job.model_dump(), headers=self.agent_headers).status_code, 200)

    def test_metadata_validation_and_import_mode_fence(self):
        for changes in ({"duration_ms": 0}, {"duration_ms": 21600001}, {"bytes": 2000000001},
                        {"import_id": "../escape"}, {"path": "/private/recording.wav"}):
            response = self.client.post(f"/internal/live/v2/meetings/{self.mid}/audio-import",
                json={**self.job.model_dump(), **changes}, headers=self.agent_headers)
            self.assertEqual(response.status_code, 400, changes)
        for name in ("../会议.m4a", "c:\\会议.wav", "secret\nmeeting.wav", "\tmeeting.wav", "  "):
            with self.assertRaises(LiveError):
                self.wb.begin_import(self.agent, self.mid, self.job.model_copy(update={"file_name": name}))
        live = self.wb.create(self.owner, NewMeeting(title="正在收听"))
        with self.assertRaises(LiveError):
            self.wb.begin_import(self.agent, live["id"], self.job)

    def test_file_binding_time_bounds_deduplication_and_progress(self):
        self.begin()
        event = self.event()
        accepted = self.wb.audio(self.agent, self.mid, event)
        self.assertEqual(accepted, self.wb.audio(self.agent, self.mid, event))
        for changes in ({"capture_epoch": "another"}, {"source": "system"}, {"audio_end_ms": 180001}):
            with self.assertRaises(LiveError):
                self.wb.audio(self.agent, self.mid, self.event(2, **changes))
        self.wb.audio(self.agent, self.mid, self.event(3, type="import.progress", audio_end_ms=90000))
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["meeting"]["audio_import"]["processed_ms"], 90000)
        for end in (89000, 180001):
            with self.assertRaises(LiveError):
                self.wb.audio(self.agent, self.mid, self.event(4, type="import.progress", audio_end_ms=end))

    def test_finish_persists_full_transcript_and_does_not_end_meeting(self):
        original = self.root / "original.wav"
        original.write_bytes(b"original file is not owned by the workbench")
        self.begin()
        self.wb.audio(self.agent, self.mid, self.event())
        meeting = self.finish()
        self.assertEqual(meeting["state"], "active")
        self.assertEqual(meeting["audio_import"]["processed_ms"], 180000)
        content = self.wb.writer.path(meeting["audio_import"]["transcript_path"]).read_text()
        self.assertIn("00:00:01", content)
        self.assertIn("工作流的定义", content)
        self.assertIn("会议转写", self.wb.writer.path(meeting["index_path"]).read_text())
        self.assertEqual(original.read_bytes(), b"original file is not owned by the workbench")
        self.assertNotIn("original.wav", json.dumps(meeting))
        with self.assertRaises(LiveError):
            self.begin()
        with self.assertRaises(LiveError):
            self.wb.audio(self.agent, self.mid, self.event(2))

    def test_cancelled_failed_and_silent_imports_keep_readable_transcript(self):
        for state in ("cancelled", "failed", "completed"):
            with self.subTest(state=state):
                self.mid = self.wb.create(self.owner, NewMeeting(title=state, input_kind="audio_file"))["id"]
                self.begin()
                if state != "completed":
                    self.wb.audio(self.agent, self.mid, self.event())
                m = self.finish(state)
                saved = self.wb.writer.path(m["audio_import"]["transcript_path"]).read_text()
                self.assertIn(state, saved)
                self.assertEqual(m["state"], "active")
                self.assertEqual(self.wb.transcript(self.owner, self.mid)["segment_count"], int(state != "completed"))

    def test_snapshot_limit_does_not_truncate_export(self):
        self.begin()
        for n in range(1, 126):
            self.wb.audio(self.agent, self.mid, self.event(n, text=f"录音段落{n}"))
        snapshot = self.wb.snapshot(self.owner, self.mid)
        self.assertEqual((len(snapshot["segments"]), snapshot["segment_count"]), (120, 125))
        self.finish()
        exported = self.client.get(f"/live/v2/meetings/{self.mid}/transcript", headers=self.owner_headers).get_json()
        self.assertEqual(exported["segment_count"], 125)
        self.assertIn("录音段落1\n", exported["content"])
        self.assertIn("录音段落125", exported["content"])

    def test_transcript_file_failure_keeps_database_export_available(self):
        self.begin()
        self.wb.audio(self.agent, self.mid, self.event())
        with patch.object(self.wb.writer, "write", side_effect=OSError("test permission failure")):
            meeting = self.finish()
        self.assertEqual(meeting["audio_import"]["transcript_state"], "save_failed")
        self.assertEqual(meeting["audio_import"]["state"], "completed")
        self.assertIn("工作流的定义", self.wb.transcript(self.owner, self.mid)["content"])

    def test_project_isolation_including_transcript_export(self):
        other = self.store.seed()
        owner = self.store.authenticate(other["members"][0]["token"])
        agent = self.store.authenticate(other["members"][-1]["token"])
        self.begin()
        for function in (lambda: self.wb.transcript(owner, self.mid), lambda: self.wb.begin_import(agent, self.mid, self.job),
                         lambda: self.wb.finish_import(agent, self.mid, AudioImportFinish(import_id="file-test", state="completed"))):
            with self.assertRaises(LiveError):
                function()

    def test_permissions_are_owner_only_and_require_available_services(self):
        value = MeetingPermissions(model=True, search=True)
        with self.assertRaises(LiveError):
            self.wb.permissions(self.member, self.mid, value)
        self.wb.capabilities["model_enabled"] = False
        with self.assertRaises(LiveError):
            self.wb.permissions(self.owner, self.mid, value)
        self.wb.capabilities["model_enabled"] = True
        with self.assertRaises(LiveError):
            self.wb.permissions(self.owner, self.mid, MeetingPermissions(search=True))
        denied = self.wb.permissions(self.owner, self.mid, MeetingPermissions())
        self.assertIn("尚未批准", self.wb.blocked_reason(denied))
        allowed = self.wb.permissions(self.owner, self.mid, value)
        self.assertEqual(self.wb.blocked_reason(allowed), "")
        self.assertIn("公开", self.wb.blocked_reason(allowed, {"title": "内部问题"}))
        self.assertEqual(self.wb.blocked_reason(allowed, {"public_query": "公开工作流定义"}), "")

    def test_revoking_permissions_fences_coordinator_and_running_results(self):
        self.begin()
        self.wb.audio(self.agent, self.mid, self.event())
        self.finish()  # Release the deliberately coalesced short import tail.
        self.wb.permissions(self.owner, self.mid, MeetingPermissions(model=True, search=True))
        coordinator = self.wb.coordinator_work(self.agent)
        self.wb.question(self.owner, self.mid, NewQuestion(title="定义", public_query="workflow definition"))
        work = self.wb.claim(self.agent)
        self.wb.capabilities["model_meeting_ids"] = [self.mid]
        self.wb.capabilities["search_meeting_ids"] = [self.mid]
        m = self.wb.permissions(self.owner, self.mid, MeetingPermissions())
        self.assertIn("尚未批准", self.wb.blocked_reason(m))
        with self.assertRaises(LiveError):
            self.wb.commit_decisions(self.agent, coordinator, Decisions(actions=[]))
        with self.assertRaises(LiveError):
            self.wb.finish(self.agent, work, self.researcher().run(work["meeting"], work["question"], lambda _: None))
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["questions"][0]["state"], "paused")

    def test_eof_still_runs_question_discovery_research_and_markdown_offline(self):
        # Model and research are deterministic fixtures; this proves plumbing,
        # never the quality/availability of a paid provider.
        self.begin()
        self.wb.audio(self.agent, self.mid, self.event())
        self.finish()
        self.wb.permissions(self.owner, self.mid, MeetingPermissions(model=True, search=True))
        calls = []
        class Model:
            def decisions(_, work):
                calls.append("question-discovery")
                s = work["segments"][0]
                return Decisions.model_validate({"actions": [{"kind": "create_question", "title": "工作流如何定义",
                    "source_refs": [{"segment_id": s["id"], "revision": s["revision"]}]}]})
        runtime = WorkbenchRuntime(self.wb, Model(), self.researcher())
        try:
            with patch("requests.Session.post", side_effect=AssertionError("unexpected network")), patch("requests.Session.get", side_effect=AssertionError("unexpected network")):
                runtime.tick()
                runtime.background_coordinator_job.result(timeout=5)
                q = self.wb.snapshot(self.owner, self.mid)["questions"][0]
                from live.workbench_contracts import QuestionAction
                self.wb.action(self.owner, q["id"], QuestionAction(action="approve", expected_version=1, public_query="workflow definition"))
                runtime.tick()
                for job in runtime.research_jobs:
                    job.result(timeout=5)
            snapshot = self.wb.snapshot(self.owner, self.mid)
            self.assertEqual(calls, ["question-discovery"])
            self.assertEqual(snapshot["meeting"]["state"], "active")
            self.assertEqual(snapshot["documents"][0]["state"], "saved")
            self.assertIn("工作流如何定义", self.wb.writer.path(snapshot["documents"][0]["path"]).read_text())
        finally:
            runtime.close()

    def test_restart_marks_incomplete_import_without_replaying(self):
        self.begin()
        self.wb.audio(self.agent, self.mid, self.event())
        self.wb.recover()
        snapshot = self.wb.snapshot(self.owner, self.mid)
        self.assertEqual(snapshot["meeting"]["audio_import"]["state"], "interrupted")
        self.assertEqual(self.wb.transcript(self.owner, self.mid)["segment_count"], 1)
        with self.assertRaises(LiveError):
            self.wb.audio(self.agent, self.mid, self.event(2))

    def test_import_transcript_and_original_have_no_expiry(self):
        original = self.root / "original.wav"
        original.write_bytes(b"do not delete")
        self.begin()
        self.wb.audio(self.agent, self.mid, self.event())
        meeting = self.finish()
        transcript_path = self.wb.writer.path(meeting["audio_import"]["transcript_path"])
        purge(self.wb, now=time.time() + 8 * 86400, apply=True)
        self.assertTrue(transcript_path.exists())
        self.assertTrue(original.exists())
        self.assertTrue(self.wb.transcript(self.owner, self.mid)["content"])
