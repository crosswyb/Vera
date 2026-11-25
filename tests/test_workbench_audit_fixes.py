"""Offline regressions for audit findings; no production state or external calls."""

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from live.model_budget import ModelBudget
from live.meeting_archive import SummaryValidationError
from live.original_evidence import article_number, original_result, standard_ids, target_for
from live.output_validation import OutputValidationError
from live.provider_settings import ModelService
from live.source_reader import DemoResearcher
from live.store import LiveError
from live.workbench_contracts import Decisions, MeetingPermissions, NewMeeting, NewQuestion, Utterance
from live.workbench_retention import purge
from live.workbench_runtime import TextModel, WorkbenchRuntime
from live_api import create_app


class AuditFixTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.app = create_app(self.root / "state.sqlite3", {
            "TESTING": True, "WORKBENCH_ROOT": self.root / "meetings",
            "WORKBENCH_SETTINGS_PATH": self.root / "providers.json",
            "WORKBENCH_CAPABILITIES": {"model_enabled": True, "search_enabled": True, "debounce_seconds": 0}})
        self.wb = self.app.extensions["workbench"]
        members = self.wb.store.seed(fixture=True)["members"]
        self.owner, self.agent = [self.wb.store.authenticate(m["token"]) for m in (members[0], members[-1])]
        self.mid = self.wb.create(self.owner, NewMeeting(title="Synthetic audit", data_scope="synthetic"))["id"]

    def runtime(self):
        runtime = WorkbenchRuntime(self.wb)
        self.wb.runtime = runtime
        self.addCleanup(runtime.close)
        return runtime

    def meeting(self):
        return self.wb.snapshot(self.owner, self.mid)["meeting"]

    def test_fixed_backlog_invalid_reference_has_bounded_retries(self):
        for n in range(17):
            self.wb.utterance(self.owner, self.mid, Utterance(text=f"Synthetic discussion {n}", segment_id=f"s{n}"))
        runtime = self.runtime()
        model = SimpleNamespace(decisions=lambda _: Decisions.model_validate({"actions": [{
            "kind": "create_question", "title": "Synthetic question",
            "source_refs": [{"segment_id": "not-an-input-id", "revision": 1}]}]}))
        now, cursors = time.time(), []
        for attempt in range(4):
            with patch("live.workbench.time.time", return_value=now):
                work = self.wb.coordinator_work(self.agent)
                self.assertIsNotNone(work)
                self.assertGreater(work["meeting"]["semantic_seq"], work["cursor"])
                cursors.append(work["cursor"])
                runtime._coordinate(self.agent, work, model)
            m = self.meeting()
            if attempt < 3:
                self.assertEqual(m["coordinator_state"], "retry_wait")
                self.assertIsNone(self.wb.coordinator_work(self.agent))
                now = m["coordinator_retry"]["next_at"] + 1
        self.assertEqual(len(set(cursors)), 1)
        self.assertEqual(m["coordinator_state"], "failed")
        self.assertEqual(m["coordinator_error"], "context_invalid")
        self.assertEqual(m["coordinator_retry"]["state"], "exhausted")
        self.assertIsNone(self.wb.coordinator_work(self.agent))

    def test_database_lock_does_not_kill_scheduler_and_health_recovers(self):
        runtime = self.runtime()
        failing, succeeded = threading.Event(), threading.Event()
        connect = sqlite3.connect
        lock = connect(self.wb.store.path)
        lock.execute("BEGIN IMMEDIATE")
        self.addCleanup(lock.close)
        def tick():
            try:
                with self.wb.store.transaction():
                    pass
            except sqlite3.OperationalError:
                failing.set()
                raise
            succeeded.set()
        def short_lock_wait(*args, **kwargs):
            return connect(*args, **{**kwargs, "timeout": .05})
        patched = patch("live.store.sqlite3.connect", side_effect=short_lock_wait)
        patched.start()
        self.addCleanup(patched.stop)
        runtime.tick = tick
        runtime.thread = threading.Thread(target=runtime._run, daemon=True)
        runtime.thread.start()
        self.assertTrue(failing.wait(2))
        deadline = time.monotonic() + 2
        while not runtime.last_error and time.monotonic() < deadline:
            time.sleep(.01)
        response = self.app.test_client().get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json["scheduler"]["thread_alive"])
        self.assertEqual(response.json["scheduler"]["last_error_kind"], "OperationalError")
        lock.rollback()
        self.assertTrue(succeeded.wait(3))
        deadline = time.monotonic() + 2
        while runtime.last_error and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.app.test_client().get("/health").status_code, 200)

    def test_concurrent_revisions_cannot_bypass_retry_limit(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="合成原文", segment_id="s"))
        for revision in range(2, 6):
            work = self.wb.coordinator_work(self.agent)
            self.assertIsNotNone(work)
            self.wb.utterance(self.owner, self.mid, Utterance(text="合成修订" + str(revision), segment_id="s", revision=revision))
            self.wb.coordinator_failed(self.agent, work, code="context_invalid")
        self.assertEqual(self.meeting()["coordinator_state"], "failed")
        self.assertEqual(self.meeting()["coordinator_retry"]["state"], "exhausted")
        self.assertIsNone(self.wb.coordinator_work(self.agent))

    def test_health_reports_dead_or_stalled_scheduler(self):
        runtime = self.runtime()
        self.assertEqual(self.app.test_client().get("/health").status_code, 503)
        runtime.thread = SimpleNamespace(is_alive=lambda: True, join=lambda **_: None)
        runtime.last_success_at = time.monotonic() - 60
        self.assertEqual(self.app.test_client().get("/health").status_code, 503)

    def _worker_lock(self, kind):
        runtime = self.runtime()
        calls, locks = [], []
        connect = sqlite3.connect
        def hold_database():
            calls.append(kind)
            lock = connect(self.wb.store.path, check_same_thread=False)
            lock.execute("BEGIN IMMEDIATE")
            locks.append(lock)
        if kind == "coordinator":
            self.wb.utterance(self.owner, self.mid, Utterance(text="合成讨论", segment_id="s"))
            work = self.wb.coordinator_work(self.agent)
            def decide(_):
                hold_database()
                return Decisions(actions=[])
            service = SimpleNamespace(decisions=decide)
            pool, function = runtime.coordinator_pool, runtime._coordinate
        elif kind == "summary":
            self.wb.utterance(self.owner, self.mid, Utterance(text="合成讨论", segment_id="s"))
            self.wb.control(self.owner, self.mid, "end")
            work = self.wb.summary_work(self.agent)
            def summarize(_):
                hold_database()
                first = work["segments"][0]
                return {"overview": "合成摘要", "topics": [{"text": "合成讨论", "source_refs": [
                    {"segment_id": first["id"], "revision": first["revision"]}]}], "decisions": [], "actions": [], "open_questions": []}
            service = SimpleNamespace(summarize=summarize)
            pool, function = runtime.summary_pool, runtime._summarize
        else:
            self.wb.demo(self.owner, self.mid)
            self.wb.question(self.owner, self.mid, NewQuestion(title="合成待查证问题"))
            work = self.wb.claim(self.agent)
            def research(meeting, question, stage):
                result = DemoResearcher().run(meeting, question, stage)
                hold_database()
                return result
            service = SimpleNamespace(run=research)
            runtime.demo_researcher = service
            pool, function = runtime.research_pool, runtime._research
        def short_wait(*args, **kwargs):
            return connect(*args, **{**kwargs, "timeout": .03})
        try:
            with patch("live.store.sqlite3.connect", side_effect=short_wait):
                future = runtime._submit_worker(pool, kind, function, self.agent, work, service)
                self.assertIsInstance(future.exception(timeout=3), sqlite3.OperationalError)
                self.assertEqual(runtime.health()["pending_local_outcomes"], 1)
                self.assertEqual(runtime.health()["status"], "degraded")
                with self.assertRaises(sqlite3.OperationalError):
                    runtime._reap_workers()
                self.assertIn(future, runtime.worker_tasks)
                locks[0].rollback()
                runtime._reap_workers()
        finally:
            for lock in locks:
                lock.close()
        self.assertEqual(calls, [kind], "only local state persistence may be retried")
        self.assertFalse(runtime.worker_tasks)
        snapshot = self.wb.snapshot(self.owner, self.mid)
        if kind == "summary":
            self.assertEqual(snapshot["meeting"]["summary"]["state"], "outcome_unknown")
            self.wb.retry_summary(self.owner, self.mid)
        elif kind == "coordinator":
            self.assertEqual(snapshot["meeting"]["coordinator_state"], "outcome_unknown")
            self.wb.control(self.owner, self.mid, "retry_coordinator")
        else:
            self.assertEqual(snapshot["questions"][0]["state"], "outcome_unknown")

    def test_summary_worker_database_failure_becomes_retryable_without_paid_replay(self):
        self._worker_lock("summary")

    def test_coordinator_worker_database_failure_becomes_retryable_without_paid_replay(self):
        self._worker_lock("coordinator")

    def test_research_worker_database_failure_becomes_retryable_without_paid_replay(self):
        self._worker_lock("research")

    def test_worker_recovery_preserves_an_already_committed_answer(self):
        runtime = self.runtime()
        self.wb.demo(self.owner, self.mid)
        self.wb.question(self.owner, self.mid, NewQuestion(title="合成问题"))
        work = self.wb.claim(self.agent)
        result = DemoResearcher().run(work["meeting"], work["question"], lambda _: None)
        def committed_then_failed(actor, task, _):
            self.wb.finish(actor, task, result)
            raise sqlite3.OperationalError("synthetic subsequent local write failed")
        future = runtime._submit_worker(runtime.research_pool, "research", committed_then_failed, self.agent, work, None)
        self.assertIsInstance(future.exception(timeout=3), sqlite3.OperationalError)
        runtime._reap_workers()
        snapshot = self.wb.snapshot(self.owner, self.mid)
        self.assertEqual(snapshot["questions"][0]["state"], "completed")
        self.assertEqual(snapshot["documents"][0]["state"], "saved")

    def test_no_automatic_retention_in_scheduler_or_retired_api(self):
        runtime = self.runtime()
        runtime.next_retention = 0  # A legacy field cannot reactivate deletion.
        runtime.principals = Mock(return_value=[])
        with patch("live.workbench_retention.purge", side_effect=AssertionError("must never run")):
            runtime.tick()
        runtime.principals.assert_called_once()
        self.assertFalse(purge(self.wb, now=time.time() + 100 * 365 * 86400, apply=True)["applied"])
        self.assertEqual(self.meeting()["state"], "active")
        self.assertIsNone(self.wb.capabilities["retention_days"])

    def test_completed_answer_can_be_saved_after_meeting_ends(self):
        self.wb.demo(self.owner, self.mid)
        self.wb.question(self.owner, self.mid, NewQuestion(title="Synthetic question"))
        work = self.wb.claim(self.agent)
        result = DemoResearcher().run(work["meeting"], work["question"], lambda _: None)
        with patch.object(self.wb.writer, "write", side_effect=OSError("synthetic disk unavailable")):
            self.wb.finish(self.agent, work, result)
        doc = self.wb.snapshot(self.owner, self.mid)["documents"][0]
        self.assertEqual(doc["state"], "save_failed")
        self.wb.control(self.owner, self.mid, "end")
        published = self.wb.publish(doc["id"])
        self.assertEqual(published["state"], "saved")
        self.assertTrue(self.wb.writer.path(published["path"]).is_file())

    def test_end_does_not_revive_publication_after_permissions_changed(self):
        self.wb.demo(self.owner, self.mid)
        self.wb.question(self.owner, self.mid, NewQuestion(title="Synthetic question"))
        work = self.wb.claim(self.agent)
        result = DemoResearcher().run(work["meeting"], work["question"], lambda _: None)
        with patch.object(self.wb.writer, "write", side_effect=OSError("synthetic disk unavailable")):
            self.wb.finish(self.agent, work, result)
        doc = self.wb.snapshot(self.owner, self.mid)["documents"][0]
        self.wb.permissions(self.owner, self.mid, MeetingPermissions(model=False, search=False, auto_search=False))
        self.wb.control(self.owner, self.mid, "end")
        self.assertEqual(self.wb.publish(doc["id"])["state"], "superseded")

    def test_end_and_restart_with_pending_sibling_preserve_completed_publication(self):
        self.wb.demo(self.owner, self.mid)
        self.wb.question(self.owner, self.mid, NewQuestion(title="已完成回答"))
        work = self.wb.claim(self.agent)
        result = DemoResearcher().run(work["meeting"], work["question"], lambda _: None)
        with patch.object(self.wb.writer, "write", side_effect=OSError("synthetic disk unavailable")):
            self.wb.finish(self.agent, work, result)
        self.wb.question(self.owner, self.mid, NewQuestion(title="尚未执行的问题"))
        doc = self.wb.snapshot(self.owner, self.mid)["documents"][0]
        self.wb.control(self.owner, self.mid, "end")
        runtime = WorkbenchRuntime(self.wb)
        runtime.close()
        self.assertEqual(self.wb.publish(doc["id"])["state"], "saved")
        self.assertTrue(self.wb.writer.path(doc["path"]).is_file())

    def test_summary_revocation_stops_next_batch_and_rejects_late_result(self):
        for n in range(8):
            self.wb.utterance(self.owner, self.mid, Utterance(text="合成讨论。" * 800, segment_id=f"s{n}"))
        self.wb.control(self.owner, self.mid, "end")
        work = self.wb.summary_work(self.agent)
        calls = []
        def response(system, content, **kwargs):
            calls.append(content)
            self.wb.permissions(self.owner, self.mid, MeetingPermissions(model=False, search=False, auto_search=False))
            first = content["segments"][0]
            return {"overview": "合成摘要", "topics": [{"text": "合成议题", "source_refs": [
                {"segment_id": first["segment_id"], "revision": first["revision"]}]}],
                "decisions": [], "actions": [], "open_questions": []}
        model = TextModel(None)
        with patch.object(model, "call", side_effect=response):
            self.runtime()._summarize(self.agent, work, model)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.meeting()["summary"]["state"], "permission_revoked")
        self.assertIn("已停止后续总结请求", self.wb.meeting_file(self.owner, self.mid, "summary")["content"])

    def test_structured_correction_rechecks_authorization(self):
        model = TextModel(None)
        allowed = [True]
        def guard():
            if not allowed[0]:
                raise LiveError("revoked", 409)
        def response(*args, **kwargs):
            allowed[0] = False
            return {"actions": "invalid"}
        with patch.object(model, "call", side_effect=response) as call, self.assertRaises(LiveError):
            model._structured("synthetic", {}, Decisions, before_request=guard)
        self.assertEqual(call.call_count, 1)

    def test_summary_corrects_only_the_invalid_reference_and_restores_exact_revisions(self):
        model = TextModel(None)
        work = {"meeting": {"id": "meeting", "title": "合成会议"}, "segments": [
            {"id": "long-id-for-a-revised-segment", "revision": 3, "text": "讨论采购范围。"},
            {"id": "long-id-for-an-action", "revision": 2, "text": "周五前核对清单。"}],
            "follow_ups": [
                {"text": "周五前核对清单。", "source_refs": [{"segment_id": "long-id-for-an-action", "revision": 2}]},
                {"text": "旧行动。", "source_refs": [{"segment_id": "long-id-for-an-action", "revision": 1}]}]}
        valid = {"overview": "合成概述", "topics": [{"text": "采购范围", "evidence_ids": [1]}],
                 "decisions": [], "actions": [{"text": "周五前核对清单。", "evidence_ids": [2]}], "open_questions": []}
        invalid = {**valid, "actions": [{"text": "周五前核对清单。", "evidence_ids": [3]}]}
        with patch.object(model, "call", side_effect=[invalid, valid, valid]) as call:
            result = model.summarize(work)
        self.assertEqual(result["topics"], [{"text": "采购范围",
            "source_refs": [{"segment_id": "long-id-for-a-revised-segment", "revision": 3, "quote": "讨论采购范围。"}]}])
        self.assertEqual(result["actions"], [{"text": "周五前核对清单。",
            "source_refs": [{"segment_id": "long-id-for-an-action", "revision": 2, "quote": "周五前核对清单。"}]}])
        self.assertEqual(call.call_count, 3)
        prompt = call.call_args_list[0].args[1]
        self.assertEqual(prompt["segments"], [{"evidence_id": 1, "text": "讨论采购范围。"},
                                              {"evidence_id": 2, "text": "周五前核对清单。"}])
        self.assertEqual(prompt["follow_up_candidates"], [{"text": "周五前核对清单。", "evidence_ranges": [[2, 2]]}])
        self.assertEqual(call.call_args_list[1].args[1]["validation_feedback"],
                         [{"field": "actions.0.evidence_ids", "code": "source_ref_not_in_transcript"}])

    def test_summary_does_not_publish_when_no_evidence_number_matches(self):
        model = TextModel(None)
        work = {"meeting": {"title": "合成会议"}, "segments": [
            {"id": "s1", "revision": 1, "text": "讨论采购范围。"}]}
        invalid = {"overview": "合成概述", "topics": [{"text": "采购范围", "evidence_ids": [999]}]}
        with patch.object(model, "call", return_value=invalid) as call, self.assertRaises(OutputValidationError) as error:
            model.summarize(work)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(error.exception.issues,
                         [{"field": "topics.0.evidence_ids", "code": "source_ref_not_in_transcript"}])

    def test_summary_reviews_decisions_and_actions_against_literal_context(self):
        work = {"meeting": {"title": "方案验证"}, "segments": [
            {"id": "example", "revision": 3, "text": "先看一下屏幕。假设某地拿不到数据，可以请当地协助验证。这里只是举例。"},
            {"id": "scope", "revision": 1, "text": "可以拿一份报告做对标。这只是其中一类。"},
            {"id": "assigned", "revision": 2, "text": "先跑通一个试点。周五前由实施组核对数据。"}]}
        draft = {"overview": "仅作候选", "topics": [{"text": "讨论验证方法", "evidence_ids": [2]}],
                 "decisions": [{"text": "全量方案按报告重构", "evidence_ids": [4]}],
                 "actions": [{"text": "联系当地团队", "evidence_ids": [2]},
                             {"text": "实施组核对数据", "evidence_ids": [7]}]}
        reviewed = {"overview": "会上讨论验证方法，先跑通一个试点。",
                    "topics": [{"text": "以一份报告作为对标样例", "evidence_ids": [4, 5]}],
                    "decisions": [{"text": "先跑通一个试点", "evidence_ids": [6]}],
                    "actions": [{"text": "实施组周五前核对数据", "evidence_ids": [7]}]}
        def respond(system, content, **kwargs):
            if "source_quotes" not in content:
                return draft
            quotes = {item["evidence_id"]: item["text"] for item in content["source_quotes"]}
            self.assertEqual(quotes[2], "假设某地拿不到数据，可以请当地协助验证。")
            self.assertEqual(quotes[3], "这里只是举例。")
            self.assertEqual(quotes[5], "这只是其中一类。")
            return reviewed

        with patch.object(TextModel, "call", side_effect=respond) as call:
            result = TextModel(None).summarize(work)
        self.assertEqual(call.call_count, 2)
        self.assertEqual([item["text"] for item in result["actions"]], ["实施组周五前核对数据"])
        self.assertEqual(result["decisions"][0]["source_refs"],
                         [{"segment_id": "assigned", "revision": 2, "quote": "先跑通一个试点。"}])
        self.assertEqual(result["topics"][0]["source_refs"][1]["quote"], "这只是其中一类。")

    def test_summary_returns_grounded_body_when_overview_is_brief(self):
        model = TextModel(None)
        quote = "这是一段用于合成测试的会议讨论内容。"
        segments = [{"id": f"s{n}", "revision": 1, "text": quote} for n in range(3)]
        work = {"meeting": {"id": "meeting", "title": "合成会议"}, "segments": segments, "follow_ups": []}
        first = {"overview": "短概述", "topics": [{"text": "采购范围", "evidence_ids": [1]}],
                 "decisions": [{"text": "保留原决定", "evidence_ids": [2]}],
                 "actions": [{"text": "保留原行动", "evidence_ids": [3]}],
                 "open_questions": [{"text": "保留未决问题", "evidence_ids": [2, 2]}]}
        with patch.object(model, "call", return_value=first) as call:
            result = model.summarize(work)
        self.assertEqual(result["overview"], first["overview"])
        for (section, segment_id) in (("topics", "s0"), ("decisions", "s1"), ("actions", "s2"), ("open_questions", "s1")):
            self.assertEqual(result[section], [{"text": first[section][0]["text"],
                                               "source_refs": [{"segment_id": segment_id, "revision": 1, "quote": quote}]}])
        self.assertEqual(call.call_count, 2)

    def test_summary_failure_persists_structured_reason_for_the_ui(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="合成讨论内容。"))
        self.wb.control(self.owner, self.mid, "end")
        work = self.wb.summary_work(self.agent)
        runtime = self.runtime()
        error = SummaryValidationError("概述长度错误", 409, code="overview_length")
        model = SimpleNamespace(summarize=Mock(side_effect=error))
        runtime._summarize(self.agent, work, model)
        summary = self.meeting()["summary"]
        self.assertEqual(summary["state"], "failed")
        self.assertEqual(summary["error"], "model_contract_invalid")
        self.assertEqual(summary["error_details"]["issues"][0]["code"], "overview_length")

    def test_summary_result_returning_after_revocation_is_not_published(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="合成讨论。", segment_id="s"))
        self.wb.control(self.owner, self.mid, "end")
        work = self.wb.summary_work(self.agent)
        self.wb.permissions(self.owner, self.mid, MeetingPermissions(model=False, search=False, auto_search=False))
        first = work["segments"][0]
        result = {"overview": "不得保存为已完成的合成总结", "topics": [{"text": "合成讨论", "source_refs": [
            {"segment_id": first["id"], "revision": first["revision"]}]}], "decisions": [], "actions": [], "open_questions": []}
        self.wb.finish_summary(self.agent, work, result=result)
        summary = self.meeting()["summary"]
        self.assertEqual(summary["state"], "permission_revoked")
        self.assertIsNone(summary["result"])
        self.assertNotIn(result["overview"], self.wb.meeting_file(self.owner, self.mid, "summary")["content"])

    def test_authorization_is_rechecked_after_model_slot_wait(self):
        allowed = [True]
        class Slot:
            def __enter__(self):
                allowed[0] = False  # Represents revocation while queued.
            def __exit__(self, *_):
                pass
        budget = ModelBudget(self.root / "usage.sqlite3", limit="unlimited")
        model = TextModel(budget, ModelService(api_key="synthetic"))
        def guard():
            if not allowed[0]:
                raise LiveError("revoked", 409)
        with patch("live.workbench_runtime.MODEL_SLOTS", Slot()), patch.object(budget, "reserve") as reserve, self.assertRaises(LiveError):
            model.call("synthetic", {}, before_request=guard)
        reserve.assert_not_called()


class ArticleNumberTest(unittest.TestCase):
    def test_chinese_and_arabic_article_numbers_select_same_exact_excerpt(self):
        quote = "这是用于验证定位逻辑的合成条文，不代表任何实际政策。"
        source = {"id": "s1", "url": "https://www.gov.cn/zhengce/content_9999999.htm",
            "title": "合成管理条例", "text": "合成管理条例\n第三条 " + quote + "\n第四条 另一条合成内容。", "read_status": "read"}
        for section, state in (("第3条", "verified"), ("第三条", "verified"), ("第4条", "unverified"), ("第三章", "unverified")):
            question = {"title": f"查《合成管理条例》{section}原文"}
            self.assertEqual(original_result(target_for(question), [source], [{"source_id": "s1", "quote": quote}])["state"], state)

    def test_compound_numerals_and_adjacent_standard_identifier(self):
        for value, expected in (("十", "10"), ("十二", "12"), ("二十一", "21"), ("一百零三", "103"), ("一千零二十", "1020"), ("二〇二六", "2026"), ("3.2", "3.2")):
            self.assertEqual(article_number(value), expected)
        self.assertEqual(standard_ids("查一下GB/T 98765-2024的原文"), {"gb/t98765-2024"})
        self.assertEqual(standard_ids("NOTGB/T 98765-2024"), set())


if __name__ == "__main__":
    unittest.main()
