"""Required deliverables survive absent research, model failures, and restarts."""

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from live.meeting_archive import distinct_utterances, final_segments, render_summary, render_transcript, transcript_anchor
from live.store import LiveError
from live.workbench_contracts import AudioEvent, AudioImport, AudioImportFinish, MeetingPermissions, NewMeeting, RecordingStart, Utterance
from live.workbench_retention import purge
from live.workbench_runtime import OutcomeUnknown, TextModel, WorkbenchRuntime
from live_api import create_app


class MeetingArchiveTest(unittest.TestCase):
    def test_overlapping_track_copy_is_filtered_without_dropping_a_correction(self):
        base = dict(capture_epoch='fixture', audio_start_ms=2000, audio_end_ms=5000)
        system = dict(base, id='system', event_seq=1, source='system',
                      text='当前测试环境可以使用合成的访问日志来核对请求。')
        duplicate = dict(base, id='echo', event_seq=2, source='microphone',
                         text='当前测试环境可以使用合成的访问日志来核对请求')
        correction = dict(duplicate, id='correction', event_seq=3,
                          text='当前测试环境不可以使用合成的访问日志来核对请求。')
        later = dict(duplicate, id='later', event_seq=4, audio_start_ms=5400, audio_end_ms=8000)
        self.assertEqual([s['id'] for s in distinct_utterances([system, duplicate, correction, later])],
                         ['system', 'correction', 'later'])

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
        self.assertIn("明确决定", content)
        self.assertIn("- 先完成本机验证。\n", content)
        self.assertEqual(m["summary"]["result"]["decisions"], self.result(work)["decisions"])
        self.assertNotIn("我们决定先完成本机验证", content)
        self.assertNotIn("依据：", content)
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
        summary = self.wb.meeting_file(self.owner, self.mid, "summary")
        self.assertIn("本场未启用 AI 总结", summary["content"])
        self.assertNotIn("仅本机讨论", summary["content"])
        self.assertIn("仅本机讨论", self.wb.meeting_file(self.owner, self.mid, "transcript")["content"])

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

    def test_summary_omits_references_in_every_section_while_transcript_retains_anchors(self):
        segments = [{"id": "seg:one", "revision": 2, "audio_start_ms": 223000,
                     "capture_epoch": "e1", "source": "microphone", "text": "先完成接口联调，再确认上线窗口。"}]
        meeting = {"id": "m", "title": "引用定位", "created_at": time.time(),
                   "base_files": {"transcript": {"path": "m/02-全量转录.md"}}}
        result = {"overview": "会议围绕接口联调、上线窗口和验收安排进行讨论。",
                  "topics": [{"text": "确认接口联调先行。", "source_refs": [{"segment_id": "seg:one", "revision": 2}]}],
                  "decisions": [{"text": "暂不确定上线窗口。", "source_refs": [{"segment_id": "seg:one", "revision": 2}]}],
                  "actions": [{"text": "继续完成联调。", "source_refs": [{"segment_id": "seg:one", "revision": 2}]}],
                  "open_questions": [{"text": "上线窗口待确认。", "source_refs": ["legacy-ref"]}]}
        transcript = render_transcript(meeting, segments)
        summary = render_summary(meeting, segments, result)
        anchor = transcript_anchor("seg:one", 2)
        self.assertIn(f'<a id="{anchor}"></a>', transcript)
        self.assertIn("S0001 · 00:03:43", transcript)
        self.assertIn(segments[0]["text"], transcript)
        for item in ("确认接口联调先行。", "暂不确定上线窗口。", "继续完成联调。", "上线窗口待确认。"):
            self.assertIn(f"- {item}\n", summary)
        for hidden in (segments[0]["text"], "依据：", "定位原文", "查看全量转录", "legacy-ref", anchor):
            self.assertNotIn(hidden, summary)

    def test_historical_summary_file_api_hides_generated_references_without_rewriting(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="我们决定先完成本机验证，其他安排待定。"))
        self.wb.control(self.owner, self.mid, "end")
        work = self.wb.summary_work(self.agent)
        self.wb.finish_summary(self.agent, work, self.result(work))
        old_content = (
            "# 三份基础产物验证 · 会议纪要\n\n- 转录片段：1\n\n"
            "[查看全量转录](02-全量转录.md)\n\n## 明确决定\n\n"
            "- 先完成本机验证。（依据：S0001 · 00:00:00 · “我们决定先完成本机验证，其他安排待定。”）\n"
        )
        with self.wb.store.transaction() as db:
            m = self.wb._meeting(db, self.owner, self.mid, True)
            self.wb._archive_file(db, m, "summary", old_content, "03-会议纪要")
            self.wb._save(db, m)
        url = f"/live/v2/meetings/{self.mid}/files/summary"
        value = self.client.get(url, headers=self.owner_headers).get_json()
        self.assertIn("- 先完成本机验证。\n", value["content"])
        self.assertNotIn("我们决定先完成本机验证", value["content"])
        self.assertNotIn("依据：", value["content"])
        self.assertNotIn("查看全量转录", value["content"])
        path = Path(value["path"])
        self.assertEqual(path.read_text(encoding="utf-8"), old_content)
        edited = old_content + "\n参会者补录的决议。\n"
        path.write_text(edited, encoding="utf-8")
        self.assertEqual(self.client.get(url, headers=self.owner_headers).get_json()["content"], edited)

    def test_historical_local_draft_api_omits_raw_excerpt(self):
        self.wb.permissions(self.owner, self.mid, MeetingPermissions())
        self.wb.utterance(self.owner, self.mid, Utterance(text="仅本机讨论，先保留原话。"))
        self.wb.control(self.owner, self.mid, "end")
        old_content = ("# 三份基础产物验证 · 会议纪要\n\n- 转录片段：1\n\n"
                       "## 讨论摘录\n\n- 仅本机讨论，先保留原话。（原话：S0001）\n")
        with self.wb.store.transaction() as db:
            m = self.wb._meeting(db, self.owner, self.mid, True)
            self.wb._archive_file(db, m, "summary", old_content, "03-会议纪要-本机摘录")
            self.wb._save(db, m)
        value = self.wb.meeting_file(self.owner, self.mid, "summary")
        self.assertEqual(value["filename"], "03-会议纪要-状态.md")
        self.assertIn("本场未启用 AI 总结", value["content"])
        self.assertNotIn("仅本机讨论", value["content"])
        self.assertEqual(Path(value["path"]).read_text(encoding="utf-8"), old_content)

    def test_summary_overview_retry_keeps_saved_body_on_failure_or_restart(self):
        for index in range(3):
            self.wb.utterance(self.owner, self.mid, Utterance(text=f"第{index}段补充本机验证安排。" * 10))
        self.wb.control(self.owner, self.mid, "end")
        work = self.wb.summary_work(self.agent)
        result = self.result(work)
        result.pop("overview")
        self.wb.finish_summary(self.agent, work, result)
        draft = self.wb.meeting_file(self.owner, self.mid, "summary")
        self.assertIn("概述待完善", draft["content"])
        self.assertIn("先完成本机验证。", draft["content"])
        self.assertNotIn("定位原文", draft["content"])

        self.wb.retry_summary(self.owner, self.mid)
        retried = self.wb.summary_work(self.agent)
        self.assertEqual(retried["overview_draft"]["decisions"], result["decisions"])
        self.wb.finish_summary(self.agent, retried, error="model_contract_invalid")
        self.assertEqual(self.wb.meeting_file(self.owner, self.mid, "summary"), draft)

        self.wb.retry_summary(self.owner, self.mid)
        self.wb.summary_work(self.agent)
        self.wb.recover()
        self.assertIsNone(self.wb.summary_work(self.agent))
        self.assertEqual(self.wb.meeting_file(self.owner, self.mid, "summary"), draft)

        self.wb.retry_summary(self.owner, self.mid)
        retried = self.wb.summary_work(self.agent)
        overview = ("本次会议围绕新系统的试运行与验收展开，旨在明确首批上线的业务和参与人员。参会人员逐一核对了数据迁移、权限配置与异常回退的安排，"
                    "比较了先试点和全量切换两种做法。会议同意先在一个车间完成试点，验收通过后再推广。下一步由实施组在周五前提交回退清单，"
                    "由业务组复核测试记录并反馈差异，双方在下次例会核对验收条件。接口稳定性与第二批上线日期仍待确认，供应商反馈后再安排复核。")
        with patch.object(TextModel, "call", return_value={"overview": overview}) as call:
            updated = TextModel(None).summarize(retried)
        self.assertEqual(call.call_count, 1)
        self.assertNotIn("segments", call.call_args.args[1])
        self.wb.finish_summary(self.agent, retried, updated)
        saved = self.wb.meeting_file(self.owner, self.mid, "summary")
        self.assertIn(overview, saved["content"])
        self.assertNotIn("概述待完善", saved["content"])
        self.assertIn("先完成本机验证。", saved["content"])

    def test_user_edited_markdown_is_preserved(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="保留用户编辑。"))
        m = self.wb.control(self.owner, self.mid, "end")
        path = self.wb.writer.path(m["base_files"]["summary"]["path"]); path.write_text("用户自己的纪要内容", encoding="utf-8")
        work = self.wb.summary_work(self.agent); self.wb.finish_summary(self.agent, work, self.result(work))
        self.assertEqual(path.read_text(), "用户自己的纪要内容")
        self.assertNotEqual(self.wb.snapshot(self.owner, self.mid)["meeting"]["base_files"]["summary"]["path"], m["base_files"]["summary"]["path"])

    def test_regenerate_reviews_all_content_and_preserves_existing_archive_on_failure(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="先打开文档。实施组周五核对清单。"))
        self.wb.control(self.owner, self.mid, "end")
        work = self.wb.summary_work(self.agent)
        previous = self.result(work)
        previous.pop("overview")
        self.wb.finish_summary(self.agent, work, result=previous)
        saved = self.wb.meeting_file(self.owner, self.mid, "summary")

        response = self.client.post(f"/live/v2/meetings/{self.mid}/summary", json={"regenerate": True},
                                    headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        work = self.wb.summary_work(self.agent)
        self.assertNotIn("overview_draft", work)
        self.wb.finish_summary(self.agent, work, error="model_contract_invalid")
        self.assertEqual(self.wb.meeting_file(self.owner, self.mid, "summary"), saved)

        self.wb.retry_summary(self.owner, self.mid, regenerate=True)
        work = self.wb.summary_work(self.agent)
        segment = work["segments"][0]
        point = {"text": "实施组周五核对清单", "source_refs": [
            {"segment_id": segment["id"], "revision": segment["revision"], "quote": "实施组周五核对清单。"}]}
        invalid = {"text": point["text"], "source_refs": [{**point["source_refs"][0], "quote": "周一提交"}]}
        with self.assertRaises(LiveError):
            self.wb.finish_summary(self.agent, work, result={"topics": [point], "actions": [invalid]})
        self.wb.finish_summary(self.agent, work, result={"topics": [point], "actions": [point]})
        content = self.wb.meeting_file(self.owner, self.mid, "summary")["content"]
        self.assertIn("- 实施组周五核对清单\n", content)
        self.assertNotIn("“实施组周五核对清单。”", content)
        saved_result = self.wb.snapshot(self.owner, self.mid)["meeting"]["summary"]["result"]
        self.assertEqual(saved_result["actions"], [point])
        self.assertNotIn("先打开文档", content)

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
                batch = content["segments"]
                if content["batch_index"] == 2 and "validation_feedback" not in content:
                    # The first batch's ID exists in the meeting but is out of scope here.
                    return {"overview": "合成概述", "topics": [{"text": "跨批次引用", "evidence_ids": [1]}]}
                seen.extend(batch)
                point = {"text": "合成讨论要点。", "evidence_ids": [batch[0]["evidence_id"]]}
                return {"overview": ("本次会议围绕新系统的试运行与验收展开，旨在明确首批上线的业务和参与人员。参会人员逐一核对了数据迁移、权限配置与异常回退的安排，"
                    "比较了先试点和全量切换两种做法。会议同意先在一个车间完成试点，验收通过后再推广。下一步由实施组在周五前提交回退清单，"
                    "由业务组复核测试记录并反馈差异，双方在下次例会核对验收条件。接口稳定性与第二批上线日期仍待确认，供应商反馈后再安排复核。"),
                    "topics": [point], "decisions": [], "actions": [], "open_questions": []}
            first = content["chunk_notes"][0]
            if "validation_feedback" not in content:
                self.assertEqual(content["source_quotes"][0]["evidence_id"], 1)
                self.assertEqual(content["source_quotes"][1]["evidence_id"], 2)
                # ID 3 exists in the transcript, but is outside the review window.
                return {**first, "topics": [{"text": "未进入复核范围的引文", "evidence_ids": [3]}]}
            return {**first, "topics": [note["topics"][0] for note in content["chunk_notes"]]}
        with patch.object(TextModel, "call", side_effect=response) as call:
            result = TextModel(None).summarize({"meeting": {"title": "长会议"}, "segments": segments})
        self.assertEqual(call.call_count, 7)
        self.assertEqual([row["evidence_id"] for row in seen], list(range(1, len(seen) + 1)))
        self.assertEqual("".join(row["text"] for row in seen), "".join(s["text"] for s in segments))
        self.assertEqual([item["source_refs"] for item in result["topics"]],
                         [[{"segment_id": f"s{index}", "revision": 1, "quote": segments[index]["text"][:160]}]
                          for index in (0, 4, 8, 12)])

    def test_save_failure_keeps_database_export_and_can_recover(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="磁盘失败也能导出正文。"))
        with patch.object(self.wb.writer, "write", side_effect=OSError("disk full")):
            m = self.wb.control(self.owner, self.mid, "end")
        self.assertEqual(m["base_files"]["transcript"]["state"], "save_failed")
        self.assertIn("磁盘失败", self.wb.transcript(self.owner, self.mid)["content"])
        self.wb.archive_meeting(self.mid)
        self.assertIn("磁盘失败", self.wb.meeting_file(self.owner, self.mid, "transcript")["content"])
