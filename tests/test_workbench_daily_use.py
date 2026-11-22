"""Regressions from the November 16 meeting; all content stays offline."""

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from live.model_budget import ModelBudget
from live.source_reader import Researcher
from live.store import LiveError
from live.workbench_contracts import AudioEvent, Decisions, EditQuestion, MeetingPermissions, NewMeeting, NewQuestion, QuestionAction, Utterance
from live.workbench_runtime import WorkbenchRuntime
from live_api import create_app


class DailyUseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        app = create_app(self.root / "state.sqlite3", {"TESTING": True,
            "WORKBENCH_ROOT": self.root / "meetings", "WORKBENCH_CAPABILITIES": {
                "model_enabled": True, "search_enabled": True, "debounce_seconds": 0}})
        self.wb = app.extensions["workbench"]
        seed = self.wb.store.seed(fixture=True)
        self.owner, self.member, _, self.agent = [self.wb.store.authenticate(m["token"]) for m in seed["members"]]
        self.mid = self.wb.create(self.owner, NewMeeting(title="日常使用离线验证"))["id"]

    def test_new_meeting_defaults_to_automatic_research_but_opt_out_is_atomic(self):
        meeting = self.wb.snapshot(self.owner, self.mid)["meeting"]
        self.assertEqual({k: meeting.get("permissions", {}).get(k) for k in ("model", "search", "auto_search")},
                         {"model": True, "search": True, "auto_search": True})
        local = self.wb.create(self.owner, NewMeeting(title="仅本机", permissions=MeetingPermissions()))
        self.assertFalse(local["permissions"]["model"])
        self.assertTrue(self.wb.blocked_reason(local))
        self.wb.capabilities.update(model_enabled=False, search_enabled=False)
        offline = self.wb.create(self.owner, NewMeeting(title="无服务"))
        self.assertFalse(offline["permissions"]["model"])
        with self.assertRaises(LiveError):
            self.wb.create(self.owner, NewMeeting(title="不能伪装已启用", permissions=MeetingPermissions(model=True)))

    def test_member_cannot_grant_external_processing_by_creating_a_meeting(self):
        meeting = self.wb.create(self.member, NewMeeting(title="成员新会议"))
        self.assertFalse(meeting["permissions"]["model"])
        with self.assertRaises(LiveError):
            self.wb.create(self.member, NewMeeting(title="成员授权", permissions=MeetingPermissions(model=True)))

    def test_explicit_web_request_requires_query_and_meeting_permission(self):
        q = self.wb.question(self.owner, self.mid, NewQuestion(title='解释采购成本', public_query='采购成本'))
        self.assertFalse(q.get('web_search_requested'))
        with self.assertRaises(LiveError):
            self.wb.question(self.owner, self.mid, NewQuestion(title=q['title'], web_search_requested=True))
        with self.assertRaises(LiveError):
            self.wb.action(self.owner, q['id'], QuestionAction(action='approve', expected_version=1,
                           public_query=' ', web_search_requested=True))
        approved = self.wb.action(self.owner, q['id'], QuestionAction(action='approve', expected_version=1,
                                  public_query='公开行业资料', web_search_requested=True))
        self.assertTrue(approved['web_search_requested'])
        self.wb.permissions(self.owner, self.mid, MeetingPermissions(model=True))
        m = self.wb.snapshot(self.owner, self.mid)['meeting']
        self.assertIn('尚未批准外部搜索', self.wb.blocked_reason(m, {**approved, 'evidence_need':'internal'}))
        self.assertIsNone(self.wb.claim(self.agent))
        self.wb.permissions(self.owner, self.mid, MeetingPermissions(model=True, search=True))
        self.assertTrue(self.wb.claim(self.agent)['question']['web_search_requested'])
        revised = self.wb.edit(self.owner, q['id'], EditQuestion(expected_version=1, title='给定条件计算'))
        self.assertFalse(revised.get('web_search_requested'))

    def test_existing_automatic_answer_cannot_satisfy_a_new_request_for_sources(self):
        title = '解释采购成本'
        old = self.wb.question(self.owner, self.mid, NewQuestion(title=title, public_query='公开资料'))
        request = NewQuestion(title=title, public_query='公开资料', web_search_requested=True)
        new = self.wb.question(self.owner, self.mid, request)
        self.assertNotEqual(old['id'], new['id'])
        self.assertTrue(new['web_search_requested'])
        self.assertEqual(self.wb.question(self.owner, self.mid, request)['id'], new['id'])

    def test_identified_candidate_starts_research_without_question_approval(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="上下文压缩以后为什么会忘记已有约束？"))
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, Decisions.model_validate({"actions": [{
            "kind": "candidate", "title": "上下文压缩后约束遗忘的原因", "public_query": "context compaction instruction retention",
            "source_refs": [{"segment_id": work["segments"][-1]["id"], "revision": 1}]}]}))
        question = self.wb.snapshot(self.owner, self.mid)["questions"][0]
        self.assertEqual(question["state"], "queued")
        self.assertEqual(self.wb.claim(self.agent)["question"]["id"], question["id"])

    def test_existing_candidates_join_queue_when_meeting_allows_automatic_search(self):
        self.wb.permissions(self.owner, self.mid, MeetingPermissions(model=True, search=True))
        self.wb.utterance(self.owner, self.mid, Utterance(text="这个概念还需要了解。"))
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, Decisions.model_validate({"actions": [{
            "kind": "candidate", "title": "已有候选", "source_refs": [
                {"segment_id": work["segments"][-1]["id"], "revision": 1}]}]}))
        question = self.wb.snapshot(self.owner, self.mid)["questions"][0]
        with self.wb.store.transaction() as db:
            question.update(public_query="agent context compaction", public_query_origin="model")
            self.wb.store._put(db, "wb_question", question["id"], question)
        self.wb.permissions(self.owner, self.mid, MeetingPermissions(model=True, search=True, auto_search=True))
        claimed = self.wb.claim(self.agent)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["question"]["id"], question["id"])
        self.assertIsNone(self.wb.claim(self.agent))

    def test_retractions_do_not_occupy_reading_window_or_destroy_audit(self):
        for n in range(125):
            self.wb.utterance(self.owner, self.mid, Utterance(text=f"有效讨论 {n}", segment_id=f"kept-{n}"))
        for n in range(125):
            event = AudioEvent(meeting_id=self.mid, capture_epoch="e", stream_id="mic", seq=n + 1,
                type="transcript.retracted", source="microphone", occurred_at="2026-11-16T02:00:00Z",
                segment_id=f"withdrawn-{n}", revision=1, audio_start_ms=0, audio_end_ms=1000, text="")
            self.wb.audio(self.agent, self.mid, event)
        snapshot = self.wb.snapshot(self.owner, self.mid)
        self.assertEqual(len(snapshot["segments"]), 120)
        self.assertTrue(all(s["type"] == "transcript.final" for s in snapshot["segments"]))
        self.assertEqual(snapshot["segments"][0]["text"], "有效讨论 5")
        self.assertEqual(snapshot["segment_count"], 125)
        with self.wb.store.transaction() as db:
            self.assertEqual(len(self.wb._items(db, "wb_segment", self.mid)), 250)

    def test_more_audio_does_not_retry_a_failed_paid_operation(self):
        self.wb.permissions(self.owner, self.mid, MeetingPermissions(model=True, search=True))
        self.wb.utterance(self.owner, self.mid, Utterance(text="请调研当前工作流的定义"))
        work = self.wb.coordinator_work(self.agent)
        self.wb.coordinator_failed(self.agent, work, code="budget_exhausted")
        self.wb.utterance(self.owner, self.mid, Utterance(text="再看一下有哪些主流方案"))
        meeting = self.wb.snapshot(self.owner, self.mid)["meeting"]
        self.assertEqual(meeting["coordinator_state"], "failed")
        self.assertEqual(meeting["coordinator_error"], "budget_exhausted")
        self.assertEqual(meeting["coordinator_cursor"], 0)
        self.assertIsNone(self.wb.coordinator_work(self.agent))
        self.wb.control(self.owner, self.mid, "retry_coordinator")
        self.assertEqual(len(self.wb.coordinator_work(self.agent)["segments"]), 2)

    def test_retraction_only_update_advances_without_a_model_call(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="普通讨论", segment_id="s"))
        initial = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, initial, Decisions(actions=[]))
        self.wb.audio(self.agent, self.mid, AudioEvent(meeting_id=self.mid, capture_epoch="manual",
            stream_id=self.owner["id"], seq=2, type="transcript.retracted", source="microphone",
            occurred_at="2026-11-16T02:00:00Z", segment_id="s", revision=2,
            audio_start_ms=0, audio_end_ms=0, text=""))
        work = self.wb.coordinator_work(self.agent)
        class NoPaidModel:
            def decisions(_, work):
                raise AssertionError("retractions must not cause paid semantic calls")
        runtime = WorkbenchRuntime(self.wb, NoPaidModel())
        self.addCleanup(runtime.close)
        runtime._coordinate(self.agent, work)
        meeting = self.wb.snapshot(self.owner, self.mid)["meeting"]
        self.assertEqual(meeting["coordinator_cursor"], meeting["semantic_seq"])
        self.assertIsNone(meeting.get("coordinator_error"))

    def test_corrected_evidence_reschedules_new_input_instead_of_replaying_stale_work(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="请调研工作流", segment_id="s"))
        work = self.wb.coordinator_work(self.agent)
        self.wb.utterance(self.owner, self.mid, Utterance(text="请调研工作流与智能体的区别", segment_id="s", revision=2))
        with self.assertRaises(LiveError):
            self.wb.commit_decisions(self.agent, work, Decisions.model_validate({"actions":[{"kind":"create_question",
                "title":"工作流定义", "source_refs":[{"segment_id":work["segments"][0]["id"],"revision":1}]}]}))
        self.wb.coordinator_failed(self.agent, work, code="context_invalid")
        revised = self.wb.coordinator_work(self.agent)
        self.assertIsNotNone(revised)
        self.assertEqual(revised["segments"][0]["revision"], 2)
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["questions"], [])

    def test_disabling_auto_search_fences_inflight_generated_queries(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="请调研工作流定义"))
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, Decisions.model_validate({"actions": [{"kind": "create_question",
            "title": "工作流定义", "public_query": "工作流 定义", "source_refs": [
                {"segment_id": work["segments"][-1]["id"], "revision": 1}]}]}))
        old_work = self.wb.claim(self.agent)
        self.wb.permissions(self.owner, self.mid, MeetingPermissions(model=True, search=True, auto_search=False))
        snapshot = self.wb.snapshot(self.owner, self.mid)
        self.assertEqual(snapshot["questions"][0]["public_query"], "")
        self.assertEqual(snapshot["questions"][0]["state"], "paused")
        with self.assertRaises(LiveError):
            self.wb.stage(self.agent, old_work, "synthesizing")

    def test_automatic_public_query_reaches_markdown_without_per_question_approval(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="请调研一下现在主流的智能体框架，给出来源。"))
        work = self.wb.coordinator_work(self.agent)
        segment = work["segments"][-1]
        decisions = Decisions.model_validate({"actions": [{"kind": "create_question", "title": "主流智能体框架的参考资料",
            "public_query": "开源 智能体 框架 官方文档", "source_refs": [{"segment_id": segment["id"], "revision": 1}]}]})
        self.wb.commit_decisions(self.agent, work, decisions)
        claimed = self.wb.claim(self.agent)
        self.assertIsNotNone(claimed)
        queries = []
        quote = "这是合成的官方文档正文，仅用于验证资料查证与文件保存。"
        class Search:
            def search(_, query):
                queries.append(query)
                return [{"url": "https://fixture.invalid/frameworks", "content": quote * 8}]
        class Model:
            def answer(_, meeting, question, sources):
                return {"answer": "合成参考资料，等待参会者判断。", "claims": [{"text": "合成验证。",
                    "source_id": sources[0]["id"], "quote": quote}], "uncertainty": ["这是离线测试。"], "evidence_status": "supported"}
        result = Researcher(Search(), Model()).run(claimed["meeting"], claimed["question"], lambda value: None)
        self.wb.finish(self.agent, claimed, result)
        snapshot = self.wb.snapshot(self.owner, self.mid)
        self.assertEqual(queries, ["开源 智能体 框架 官方文档"])
        self.assertEqual(snapshot["questions"][0]["state"], "completed")
        document = snapshot["documents"][0]
        self.assertEqual(document["state"], "saved")
        self.assertIn("合成参考资料", self.wb.writer.path(document["path"]).read_text())

    def test_existing_manual_query_permission_is_not_silently_expanded(self):
        self.wb.permissions(self.owner, self.mid, MeetingPermissions(model=True, search=True))
        self.wb.utterance(self.owner, self.mid, Utterance(text="请调研工作流的定义"))
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, Decisions.model_validate({"actions": [{"kind": "create_question",
            "title": "工作流的定义", "public_query": "工作流 定义", "source_refs": [
                {"segment_id": work["segments"][-1]["id"], "revision": 1}]}]}))
        self.assertIsNone(self.wb.claim(self.agent))
        question = self.wb.snapshot(self.owner, self.mid)["questions"][0]
        self.assertEqual(question["public_query"], "")
        self.assertIn("检索词", question["stage"])

    def test_unlimited_budget_preserves_old_usage_and_tracks_new_requests(self):
        path = self.root / "usage.sqlite3"
        limited = ModelBudget(path, limit=2)
        limited.reserve()
        limited.reserve()
        with self.assertRaises(RuntimeError):
            limited.reserve()
        daily = ModelBudget(path, limit="unlimited")
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(lambda _: daily.reserve(), range(30)))
        self.assertEqual(len(set(ids)), 30)
        daily.record(ids[-1], {"total_tokens": 123})
        reopened = ModelBudget(path, limit="unlimited")
        self.assertEqual(reopened.reserve(), 33)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 33)
        with self.assertRaises(RuntimeError):
            ModelBudget(self.root / "zero.sqlite3", limit=0).reserve()

    def test_runtime_accepts_unlimited_workbench_limits_without_expanding_other_services(self):
        config = {"ZSJH_LIVE_MODEL_REQUEST_LIMIT": "6", "ZSJH_WB_MODEL_REQUEST_LIMIT": "unlimited",
            "ZSJH_WB_SEARCH_REQUEST_LIMIT": "unlimited", "ZSJH_LIVE_MODEL_API_KEY": "fixture-key",
            "ZSJH_LIVE_MODEL_USAGE_DB": str(self.root / "model.sqlite3"),
            "ZSJH_WB_SEARCH_USAGE_DB": str(self.root / "search.sqlite3")}
        with patch.dict("os.environ", config):
            runtime = WorkbenchRuntime.configured(self.wb)
            self.addCleanup(runtime.close)
            self.assertIsNone(runtime.model.budget.limit)
            self.assertIsNone(runtime.researcher.search.budget.limit)
            self.assertEqual(ModelBudget(self.root / "other-service.sqlite3").limit, 6)


if __name__ == "__main__":
    unittest.main()
