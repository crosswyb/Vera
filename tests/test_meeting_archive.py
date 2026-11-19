"""Required deliverables survive absent research, model failures, and restarts."""

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from live.meeting_archive import final_segments
from live.store import LiveError
from live.workbench_contracts import AudioEvent, AudioImport, AudioImportFinish, MeetingPermissions, NewMeeting, RecordingStart, Utterance
from live.workbench_retention import purge
from live.workbench_runtime import OutcomeUnknown, TextModel, WorkbenchRuntime
from live_api import create_app


class MeetingArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = create_app(self.root / "state.sqlite3", {"TESTING": True, "WORKBENCH_ROOT": self.root / "meetings",
            "WORKBENCH_CAPABILITIES": {"audio_enabled": True, "model_enabled": True, "search_enabled": True}})
        self.wb = self.app.extensions["workbench"]
        seed = self.wb.store.seed(fixture=True)
        self.owner, self.member, _, self.agent = [self.wb.store.authenticate(m["token"]) for m in seed["members"]]
        self.mid = self.wb.create(self.owner, NewMeeting(title="三份基础产物验证"))["id"]
        self.client = self.app.test_client()
        self.owner_headers = {"Authorization": "Bearer fixture-owner-0"}
        self.agent_headers = {"Authorization": "Bearer " + seed["members"][-1]["token"]}

    def event(self, kind="transcript.final", seq=1, **changes):
        values = dict(meeting_id=self.mid, capture_epoch="e1", stream_id="microphone-e1", seq=seq,
                      type=kind, source="microphone", occurred_at="2026-11-16T05:00:00Z")
        if kind.startswith("transcript."):
            values.update(segment_id="s1", revision=1, audio_start_ms=0, audio_end_ms=1000, text="我们决定先完成本机验证，其他安排待定。")
        values.update(changes)
        return AudioEvent(**values)

    def record(self, epoch="e1", source="microphone"):
        result = self.wb.start_recording(self.agent, self.mid, RecordingStart(recording_id=epoch, sources=[source]))
        path = Path(result["recording_directory"]) / f"01-原始音频-{source}-{epoch}.caf"
        path.write_bytes(b"synthetic audio container for metadata test")
        return path

    @staticmethod
    def result(work):
        segment = work["segments"][0]
        point = {"text": "先完成本机验证。", "source_refs": [{"segment_id": segment["id"], "revision": segment["revision"]}]}
        return {"overview": "会议讨论本机验证安排。", "topics": [point], "decisions": [point], "actions": [], "open_questions": []}

    def test_end_without_research_writes_every_final_segment_not_only_ui_window(self):
        for n in range(130):
            self.wb.utterance(self.owner, self.mid, Utterance(text=f"第{n}段有效讨论", segment_id=f"s{n}"))
        self.wb.audio(self.agent, self.mid, self.event("transcript.retracted", text=""))
        m = self.wb.control(self.owner, self.mid, "end")
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["questions"], [])
        transcript = self.wb.meeting_file(self.owner, self.mid, "transcript")["content"]
        self.assertIn("有效最终片段：130", transcript)
        self.assertIn("第0段有效讨论", transcript); self.assertIn("第129段有效讨论", transcript)
        self.assertEqual(m["summary"]["state"], "pending")
        self.assertIn("AI 总结正在等待", self.wb.meeting_file(self.owner, self.mid, "summary")["content"])

    def test_registered_recording_and_summary_complete_independently_of_search(self):
        path = self.record()
        self.wb.audio(self.agent, self.mid, self.event())
        self.wb.audio(self.agent, self.mid, self.event("audio.recording", seq=2, state="saved", audio_end_ms=1000))
        self.wb.audio(self.agent, self.mid, self.event("audio.status", seq=3, state="stopped"))
        self.wb.control(self.owner, self.mid, "end")
        work = self.wb.summary_work(self.agent)
        self.wb.finish_summary(self.agent, work, self.result(work))
        m = self.wb.snapshot(self.owner, self.mid)["meeting"]
        self.assertTrue(path.is_file()); self.assertEqual(m["recordings"][0]["state"], "saved")
        self.assertEqual(m["summary"]["state"], "completed")
        content = self.wb.meeting_file(self.owner, self.mid, "summary")["content"]
        self.assertIn("明确决定", content); self.assertIn("依据：S0001", content)
        for kind in ("transcript", "summary"):
            self.assertTrue(self.wb.writer.path(m["base_files"][kind]["path"]).is_file())
        listed = next(row for row in self.wb.home(self.owner)["meetings"] if row["id"] == self.mid)
        self.assertEqual(listed["document_count"], 3)

    def test_model_failure_keeps_readable_draft_and_requires_explicit_retry(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="本机验证完成后再确定日期。"))
        self.wb.control(self.owner, self.mid, "end")
        work = self.wb.summary_work(self.agent)
        self.wb.finish_summary(self.agent, work, error="outcome_unknown")
        self.assertIn("结果未知", self.wb.meeting_file(self.owner, self.mid, "summary")["content"])
        self.assertIsNone(self.wb.summary_work(self.agent))
        self.wb.retry_summary(self.owner, self.mid)
        self.assertIsNotNone(self.wb.summary_work(self.agent))

    def test_no_permission_means_local_draft_and_no_paid_summary(self):
        self.wb.permissions(self.owner, self.mid, MeetingPermissions())
        self.wb.utterance(self.owner, self.mid, Utterance(text="仅本机讨论，先保留原话。"))
        m = self.wb.control(self.owner, self.mid, "end")
        self.assertEqual(m["summary"]["state"], "local_only")
        self.assertIsNone(self.wb.summary_work(self.agent))
        self.assertIn("仅本机讨论", self.wb.meeting_file(self.owner, self.mid, "summary")["content"])

    def test_stopped_capture_exports_draft_then_ending_queues_summary(self):
        self.wb.audio(self.agent, self.mid, self.event())
        self.wb.audio(self.agent, self.mid, self.event("audio.status", seq=2, state="stopped"))
        self.assertIn("会议尚未结束", self.wb.meeting_file(self.owner, self.mid, "summary")["content"])
        self.assertIsNone(self.wb.summary_work(self.agent))
        self.wb.control(self.owner, self.mid, "end")
        self.assertIsNotNone(self.wb.summary_work(self.agent))

    def test_repeated_summary_retry_cannot_replace_pending_or_running_work(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="验证重试只生成一次。"))
        self.wb.control(self.owner, self.mid, "end")
        work = self.wb.summary_work(self.agent)
        self.wb.finish_summary(self.agent, work, error="failed")
        self.wb.retry_summary(self.owner, self.mid)
        with self.assertRaises(LiveError): self.wb.retry_summary(self.owner, self.mid)
        retried = self.wb.summary_work(self.agent)
        with self.assertRaises(LiveError): self.wb.retry_summary(self.owner, self.mid)
        self.assertIsNone(self.wb.summary_work(self.agent))
        self.wb.finish_summary(self.agent, retried, self.result(retried))
        saved = self.wb.meeting_file(self.owner, self.mid, "summary")
        self.assertEqual(saved["filename"], "03-会议纪要.md")
        self.assertIn("会议概述", saved["content"])

    def test_pause_resume_preserves_separate_raw_files_and_empty_capture_is_visible(self):
        first = self.record()
        self.wb.audio(self.agent, self.mid, self.event("audio.recording", state="saved", audio_end_ms=1000))
        self.wb.start_recording(self.agent, self.mid, RecordingStart(recording_id="e2", sources=["system"]))
        self.wb.audio(self.agent, self.mid, self.event("audio.recording", seq=2, capture_epoch="e2", source="system", state="saved"))
        m = self.wb.control(self.owner, self.mid, "end")
        self.assertEqual([r["state"] for r in m["recordings"]], ["saved", "missing"])
        self.assertTrue(first.is_file())
        self.assertIn("没有已确认", self.wb.meeting_file(self.owner, self.mid, "transcript")["content"])

    def test_recording_registration_requires_agent_and_rejects_arbitrary_paths(self):
        url = f"/internal/live/v2/meetings/{self.mid}/recordings"
        body = {"recording_id": "safe", "sources": ["microphone"]}
        self.assertEqual(self.client.post(url, json=body, headers=self.owner_headers).status_code, 403)
        self.assertEqual(self.client.post(url, json={**body, "directory": "/tmp"}, headers=self.agent_headers).status_code, 400)
        self.assertEqual(self.client.post(url, json={**body, "recording_id": "../x"}, headers=self.agent_headers).status_code, 400)
        self.assertEqual(self.client.post(url, json=body, headers={**self.agent_headers, "Origin": "https://other.invalid"}).status_code, 403)
        self.assertEqual(self.client.post(url, json=body, headers=self.agent_headers).status_code, 200)
        with self.assertRaises(LiveError):
            self.wb.start_recording(self.agent, self.mid, RecordingStart(recording_id="another", sources=["system"]))

    def test_restart_keeps_partial_recording_and_exports_available_text(self):
        path = self.record()
        self.wb.audio(self.agent, self.mid, self.event())
        self.wb.recover()
        m = self.wb.snapshot(self.owner, self.mid)["meeting"]
        self.assertEqual(m["recordings"][0]["state"], "partial")
        self.assertTrue(path.is_file())
        self.assertIn("我们决定", self.wb.meeting_file(self.owner, self.mid, "transcript")["content"])

    def test_restart_does_not_replay_running_summary(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="继续验证纪要恢复。"))
        self.wb.control(self.owner, self.mid, "end")
        self.wb.summary_work(self.agent); self.wb.recover()
        self.assertIsNone(self.wb.summary_work(self.agent))
        self.assertIn("未自动重复调用", self.wb.meeting_file(self.owner, self.mid, "summary")["content"])

    def test_late_correction_regenerates_transcript_and_fences_old_summary(self):
        self.wb.audio(self.agent, self.mid, self.event())
        self.wb.control(self.owner, self.mid, "end")
        work = self.wb.summary_work(self.agent)
        self.wb.audio(self.agent, self.mid, self.event(seq=2, revision=2, text="更正：先讨论方案，没有作出决定。"))
        with self.assertRaises(LiveError):
            self.wb.finish_summary(self.agent, work, self.result(work))
        self.assertIn("没有作出决定", self.wb.meeting_file(self.owner, self.mid, "transcript")["content"])

    def test_invalid_summary_evidence_never_publishes(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="验证依据引用。"))
        self.wb.control(self.owner, self.mid, "end")
        work = self.wb.summary_work(self.agent); result = self.result(work)
        result["topics"][0]["source_refs"][0]["segment_id"] = "invented"
        with self.assertRaises(LiveError): self.wb.finish_summary(self.agent, work, result)

    def test_user_edited_markdown_is_preserved(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="保留用户编辑。"))
        m = self.wb.control(self.owner, self.mid, "end")
        path = self.wb.writer.path(m["base_files"]["summary"]["path"]); path.write_text("用户自己的纪要内容", encoding="utf-8")
        work = self.wb.summary_work(self.agent); self.wb.finish_summary(self.agent, work, self.result(work))
        self.assertEqual(path.read_text(), "用户自己的纪要内容")
        self.assertNotEqual(self.wb.snapshot(self.owner, self.mid)["meeting"]["base_files"]["summary"]["path"], m["base_files"]["summary"]["path"])

    def test_audio_and_markdown_are_retained_without_expiry(self):
        audio = self.record()
        self.wb.audio(self.agent, self.mid, self.event("audio.recording", state="saved"))
        self.wb.utterance(self.owner, self.mid, Utterance(text="七天保留验证。"))
        m = self.wb.control(self.owner, self.mid, "end")
        foreign = audio.parent / "用户文件.txt"; foreign.write_text("保留")
        purge(self.wb, now=m["created_at"] + 8 * 86400, apply=True)
        self.assertTrue(audio.exists()); self.assertTrue(foreign.exists())
        for relative in m["base_file_paths"]: self.assertTrue(self.wb.writer.path(relative).exists())

    def test_recording_stop_preserves_zero_frames_for_missing_source_diagnostics(self):
        self.wb.start_recording(self.agent, self.mid, RecordingStart(recording_id="e1", sources=["microphone"]))
        self.wb.audio(self.agent, self.mid, self.event("audio.recording", state="missing", frames_received=0))
        recording = self.wb.snapshot(self.owner, self.mid)["meeting"]["recordings"][0]
        self.assertEqual(recording["state"], "missing")
        self.assertEqual(recording["frames_received"], 0)

    def test_model_gets_all_long_meeting_segments_in_bounded_map_reduce(self):
        segments = [{"id": f"s{i}", "revision": 1, "text": f"第{i}段" + "讨论内容" * 1000} for i in range(15)]
        seen = []
        def response(system, content):
            if "segments" in content:
                batch = content["segments"]; seen.extend(s["segment_id"] for s in batch)
                first = batch[0]
                point = {"text": "合成讨论要点。", "source_refs": [{"segment_id": first["segment_id"], "revision": 1}]}
                return {"overview": "合成会议", "topics": [point], "decisions": [], "actions": [], "open_questions": []}
            return content["chunk_notes"][0]
        with patch.object(TextModel, "call", side_effect=response):
            result = TextModel(None).summarize({"meeting": {"title": "长会议"}, "segments": segments})
        self.assertEqual(seen, [s["id"] for s in segments]); self.assertTrue(result["topics"])

    def test_save_failure_keeps_database_export_and_can_recover(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="磁盘失败也能导出正文。"))
        with patch.object(self.wb.writer, "write", side_effect=OSError("disk full")):
            m = self.wb.control(self.owner, self.mid, "end")
        self.assertEqual(m["base_files"]["transcript"]["state"], "save_failed")
        self.assertIn("磁盘失败", self.wb.transcript(self.owner, self.mid)["content"])
        self.wb.archive_meeting(self.mid)
        self.assertIn("磁盘失败", self.wb.meeting_file(self.owner, self.mid, "transcript")["content"])
