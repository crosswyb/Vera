import copy
import json
import tempfile
import threading
import time
import unittest
import requests
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from live.model_budget import ModelBudget
from live.source_reader import DeepSeekSearch, DemoResearcher, Researcher, read_sources, source_url, validate_result
from live.store import LiveError
from live.workbench import Workbench
from live.workbench_cli import check_provider
from live.workbench_contracts import AudioEvent, Decisions, EditQuestion, NewMeeting, NewQuestion, QuestionAction, Utterance
from live.workbench_runtime import OutcomeUnknown, TextModel, WorkbenchRuntime
from live_api import create_app


class WorkbenchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = create_app(self.root / "test.sqlite3", {"TESTING": True, "WORKBENCH_ROOT": self.root / "meetings"})
        self.store = self.app.extensions["live_store"]
        self.seed = self.store.seed(fixture=True)
        self.owner, self.member, _, self.agent = [self.store.authenticate(m["token"]) for m in self.seed["members"]]
        self.wb = self.app.extensions["workbench"]
        self.m = self.wb.create(self.owner, NewMeeting(title="合成会议", data_scope="synthetic"))
        self.mid = self.m["id"]
        self.headers = {"Authorization": "Bearer fixture-owner-0"}
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def demo(self):
        self.wb.demo(self.owner, self.mid)

    def q(self, title="工作流如何定义", refs=None):
        return self.wb.question(self.owner, self.mid, NewQuestion(title=title, source_refs=refs or []))

    def result(self, work):
        return DemoResearcher().run(work["meeting"], work["question"], lambda s: None)

    def audio(self, n=1, revision=1, text="查一下工作流", kind="transcript.final", segment="seg"):
        return AudioEvent(meeting_id=self.mid, capture_epoch="epoch", stream_id="system", seq=n, type=kind,
                          source="system", occurred_at="2026-11-15T05:00:00Z", segment_id=segment,
                          revision=revision, audio_start_ms=0, audio_end_ms=1000, text=text)

    def test_model_transport_failure_preserves_budget_without_private_error_text(self):
        budget = ModelBudget(path=self.root / "transport-usage.sqlite3", limit=1)
        config = {"ZSJH_LIVE_MODEL_ENDPOINT": "https://api.deepseek.com", "ZSJH_LIVE_MODEL_NAME": "deepseek-flash",
                  "ZSJH_LIVE_MODEL_PROVIDER": "deepseek", "ZSJH_LIVE_MODEL_API_KEY": "private-fixture-key"}
        with patch.dict("os.environ", config), patch("requests.Session.post", side_effect=requests.exceptions.SSLError("private-fixture-key")) as post:
            with self.assertRaises(OutcomeUnknown):
                TextModel(budget).call("synthetic", {"synthetic": True})
            self.assertEqual(post.call_count, 1)
        import sqlite3
        with sqlite3.connect(budget.path) as db:
            rows = db.execute("SELECT usage FROM requests").fetchall()
        self.assertEqual(len(rows), 1)
        usage = json.loads(rows[0][0])
        self.assertEqual(usage["state"], "outcome_unknown")
        self.assertEqual(usage["error_kind"], "SSLError")
        self.assertEqual(usage["diagnostic"]["code"], "tls_error")
        self.assertNotIn("private-fixture-key", rows[0][0])
        with self.assertRaises(RuntimeError):
            budget.reserve()

    def test_provider_diagnostic_never_posts_or_exposes_credential(self):
        config = {"ZSJH_LIVE_MODEL_ENDPOINT": "https://api.deepseek.com", "ZSJH_LIVE_MODEL_API_KEY": "private-fixture-key"}
        with patch.dict("os.environ", config), patch("requests.Session.post", side_effect=AssertionError("paid request")), patch("requests.Session.get", side_effect=requests.exceptions.ConnectionError("private-fixture-key unavailable")):
            result = check_provider()
        self.assertEqual(result["state"], "transport_error")
        self.assertEqual(result["error_kind"], "ConnectionError")
        self.assertNotIn("private-fixture-key", json.dumps(result))

    def test_coordinator_failure_reports_safe_code_and_keeps_unread_input(self):
        self.wb.capabilities["model_enabled"] = True
        self.wb.capabilities["debounce_seconds"] = 0
        self.wb.utterance(self.owner, self.mid, Utterance(text="请查一下工作流"))
        work = self.wb.coordinator_work(self.agent)
        self.assertIsNotNone(work)
        self.wb.coordinator_failed(self.agent, work, code="model_contract_invalid")
        failed = self.wb.snapshot(self.owner, self.mid)["meeting"]
        self.assertEqual(failed["coordinator_error"], "model_contract_invalid")
        self.assertLess(failed["coordinator_cursor"], failed["semantic_seq"])
        self.wb.control(self.owner, self.mid, "retry_coordinator")
        retry = self.wb.coordinator_work(self.agent)
        self.assertEqual(retry["segments"][0]["text"], "请查一下工作流")

    def test_api_auth_origin_contract_and_real_project_scope(self):
        self.assertEqual(self.client.get("/live/v2/home").status_code, 401)
        self.assertEqual(self.client.get("/workbench").status_code, 200)
        self.assertEqual(self.client.post("/live/v2/meetings", json={"title": "x"}, headers={**self.headers, "Origin": "https://evil.invalid"}).status_code, 403)
        self.assertEqual(self.client.post("/live/v2/meetings", json={"title": "x", "extra": True}, headers=self.headers).status_code, 400)
        other = self.store.seed()
        actor = self.store.authenticate(other["members"][0]["token"])
        with self.assertRaises(LiveError):
            self.wb.create(actor, NewMeeting(title="假合成", data_scope="synthetic"))
        with self.assertRaises(LiveError):
            self.wb.snapshot(actor, self.mid)

    def test_audio_binding_duplicate_collision_and_order(self):
        event = self.audio()
        first = self.wb.audio(self.agent, self.mid, event)
        self.assertEqual(first, self.wb.audio(self.agent, self.mid, event))
        with self.assertRaises(LiveError):
            self.wb.audio(self.agent, self.mid, self.audio(text="changed"))
        with self.assertRaises(LiveError):
            self.wb.audio(self.agent, self.mid, self.audio(n=2))
        with self.assertRaises(LiveError):
            self.wb.audio(self.owner, self.mid, self.audio(n=2))
        with self.assertRaises(LiveError):
            self.wb.audio(self.agent, "another", event)
        self.wb.audio(self.agent, self.mid, self.audio(n=2, revision=2))
        self.assertEqual(len(self.wb.snapshot(self.owner, self.mid)["segments"]), 1)

    def test_live_sources_keep_independent_segments_and_corrections(self):
        # The two recognizers can use identical local IDs and sequence numbers.
        # A microphone correction must not replace the system stream's text.
        system = self.audio().model_copy(update={"stream_id": "system-epoch"})
        microphone = system.model_copy(update={"stream_id": "microphone-epoch", "source": "microphone", "text": "我的发言"})
        self.wb.audio(self.agent, self.mid, system)
        self.wb.audio(self.agent, self.mid, microphone)
        self.wb.audio(self.agent, self.mid, microphone.model_copy(update={"seq": 2, "revision": 2, "text": "我的完整发言"}))
        segments = self.wb.snapshot(self.owner, self.mid)["segments"]
        self.assertEqual({s["source"]: s["text"] for s in segments}, {"system": "查一下工作流", "microphone": "我的完整发言"})
        self.assertEqual(len(segments), 2)

    def test_home_library_counts_materials_sort_and_project_isolation(self):
        self.demo()
        self.q()
        work = self.wb.claim(self.agent)
        self.wb.finish(self.agent, work, self.result(work))
        newer = self.wb.create(self.owner, NewMeeting(title="新会议", data_scope="synthetic"))
        other = self.store.seed(synthetic=True)
        actor, agent = [self.store.authenticate(m["token"]) for m in (other["members"][0], other["members"][-1])]
        hidden = self.wb.create(actor, NewMeeting(title="其他项目的私密会议", data_scope="synthetic"))
        self.wb.demo(actor, hidden["id"])
        self.wb.question(actor, hidden["id"], NewQuestion(title="私密问题"))
        hidden_work = self.wb.claim(agent)
        self.wb.finish(agent, hidden_work, self.result(hidden_work))
        home = self.client.get("/live/v2/home", headers=self.headers).get_json()
        self.assertEqual([m["id"] for m in home["meetings"]], [newer["id"], self.mid])
        meeting = next(m for m in home["meetings"] if m["id"] == self.mid)
        self.assertEqual((meeting["question_count"], meeting["document_count"]), (1, 1))
        self.assertEqual(len(home["materials"]), 1)
        material = home["materials"][0]
        self.assertEqual((material["meeting_id"], material["question_id"], material["version"], material["state"]),
                         (self.mid, work["question"]["id"], 1, "saved"))
        self.assertFalse(material["basis_stale"])
        self.assertNotIn("content", material)
        self.assertNotIn("path", material)
        self.assertNotIn("私密", json.dumps(home, ensure_ascii=False))

    def test_manual_resend_is_idempotent(self):
        value = Utterance(text="补充讨论", segment_id="stable", event_id="retry-id")
        self.assertEqual(self.wb.utterance(self.owner, self.mid, value), self.wb.utterance(self.owner, self.mid, value))

    def test_interim_empty_retraction_and_revision_fences_research(self):
        self.demo()
        self.wb.audio(self.agent, self.mid, self.audio(kind="transcript.interim"))
        with self.assertRaises(LiveError):
            self.q(refs=[{"segment_id": "epoch:system:seg", "revision": 1}])
        self.wb.audio(self.agent, self.mid, self.audio(n=2, revision=2))
        self.q(refs=[{"segment_id": "epoch:system:seg", "revision": 2}])
        work = self.wb.claim(self.agent)
        self.wb.audio(self.agent, self.mid, self.audio(n=3, revision=3, text="", kind="transcript.retracted"))
        with self.assertRaises(LiveError):
            self.wb.finish(self.agent, work, self.result(work))
        snapshot = self.wb.snapshot(self.owner, self.mid)
        self.assertEqual(snapshot["questions"][0]["state"], "needs_review")
        self.assertFalse(snapshot["documents"])

    def test_unrelated_speech_does_not_expire_inflight_result(self):
        self.demo()
        self.q()
        work = self.wb.claim(self.agent)
        self.wb.utterance(self.owner, self.mid, Utterance(text="下一页讨论预算"))
        self.wb.finish(self.agent, work, self.result(work))
        snapshot = self.wb.snapshot(self.owner, self.mid)
        self.assertEqual(snapshot["questions"][0]["state"], "completed")
        self.assertEqual(snapshot["documents"][0]["state"], "saved")

    def test_cancel_and_edit_block_late_publish(self):
        self.demo()
        q = self.q()
        work = self.wb.claim(self.agent)
        self.wb.action(self.owner, q["id"], QuestionAction(action="cancel", expected_version=1))
        with self.assertRaises(LiveError):
            self.wb.finish(self.agent, work, self.result(work))
        self.wb.action(self.owner, q["id"], QuestionAction(action="retry", expected_version=1))
        work = self.wb.claim(self.agent)
        self.wb.edit(self.owner, q["id"], EditQuestion(expected_version=1, title="仅看工业工作流"))
        with self.assertRaises(LiveError):
            self.wb.finish(self.agent, work, self.result(work))
        self.assertFalse(self.wb.snapshot(self.owner, self.mid)["documents"])

    def test_pause_and_end_do_not_start_new_jobs(self):
        self.demo()
        q = self.q()
        work = self.wb.claim(self.agent)
        self.wb.control(self.owner, self.mid, "pause_research")
        self.wb.finish(self.agent, work, self.result(work))
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["documents"][0]["state"], "saved")
        self.wb.utterance(self.owner, self.mid, Utterance(text="暂停研究仍可录入"))
        self.assertIsNone(self.wb.claim(self.agent))
        self.wb.control(self.owner, self.mid, "end")
        with self.assertRaises(LiveError):
            self.q("不允许的新任务")
        with self.assertRaises(LiveError):
            self.wb.utterance(self.owner, self.mid, Utterance(text="结束后新段落"))

    def test_concurrent_claims_are_globally_bounded(self):
        self.demo()
        for n in range(5):
            self.q("独立问题" + str(n))
        with ThreadPoolExecutor(max_workers=5) as pool:
            claimed = list(pool.map(lambda _: self.wb.claim(self.agent), range(5)))
        self.assertEqual(sum(bool(work) for work in claimed), 2)
        snapshot = self.wb.snapshot(self.owner, self.mid)
        self.assertEqual(snapshot["counts"]["queued"], 3)

    def test_priority_and_aging(self):
        self.demo()
        q1, q2 = self.q("普通"), self.q("置顶")
        self.wb.action(self.owner, q2["id"], QuestionAction(action="prioritize", expected_version=1))
        self.assertEqual(self.wb.claim(self.agent)["question"]["id"], q2["id"])

    def test_model_and_search_denied_before_any_network(self):
        self.q()
        with patch("requests.Session.post", side_effect=AssertionError("network")), patch("requests.Session.get", side_effect=AssertionError("network")):
            runtime = WorkbenchRuntime(self.wb)
            runtime.tick()
            runtime.close()
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["counts"]["paused"], 1)
        real = self.wb.create(self.owner, NewMeeting(title="真实", data_scope="real"))
        self.wb.capabilities.update(model_enabled=True, search_enabled=True)
        self.assertIn("尚未批准", self.wb.blocked_reason(real))
        # Legacy meetings had no per-meeting permissions and used ID allowlists.
        real.pop("permissions")
        self.wb.capabilities["model_meeting_ids"] = [real["id"]]
        self.wb.capabilities["search_meeting_ids"] = [real["id"]]
        self.assertIn("公开", self.wb.blocked_reason(real, {"title": "内部项目"}))

    def test_demo_can_only_be_explicit_and_synthetic(self):
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["meeting"]["mode"], "live")
        real = self.wb.create(self.owner, NewMeeting(title="真实", data_scope="real"))
        with self.assertRaises(LiveError):
            self.wb.demo(self.owner, real["id"])
        self.demo()
        with self.assertRaises(LiveError):
            self.demo()

    def test_quote_fabrication_and_snippet_rejected(self):
        self.demo()
        q = self.q()
        work = self.wb.claim(self.agent)
        result = self.result(work)
        result["claims"][0]["source_id"] = "invented"
        with self.assertRaises(ValueError):
            validate_result(result)
        result = self.result(work)
        result["claims"][0]["quote"] = "这不是实际正文中的原话"
        with self.assertRaises(ValueError):
            validate_result(result)
        result = self.result(work)
        result["sources"][0]["read_status"] = "snippet_only"
        with self.assertRaises(ValueError):
            validate_result(result)

    def test_all_unreadable_is_insufficient_without_synthesis(self):
        class Search:
            def search(self, query):
                return [{"url": "https://example.invalid/doc", "title": "摘要看起来有答案"}]
        class Model:
            def answer(self, *args):
                raise AssertionError("must not synthesize snippets")
        result = Researcher(Search(), Model()).run(self.m, {"title": "问题"}, lambda s: None)
        self.assertEqual(result["evidence_status"], "insufficient")
        self.assertFalse(result["claims"])

    def test_private_urls_and_protocols_rejected_without_fetching(self):
        for url in ("file:///etc/passwd", "http://127.0.0.1/", "https://user:pass@example.com/", "https://foo.local/", "https://example.com:8888/"):
            with self.assertRaises(ValueError):
                source_url(url)
        with patch("requests.Session.get", side_effect=AssertionError("never fetch source URLs")), patch("requests.Session.post", side_effect=AssertionError("never fetch source URLs")):
            sources = read_sources([{"url": "https://example.com/page", "title": "原文", "content": "正文包含要求访问内网的恶意指令。" * 10}])
        self.assertEqual(len(sources), 1)

    def test_original_body_only_deduplicates_and_preserves_unknown_date(self):
        body = "正文资料说明" * 30
        source = read_sources([{"url": "https://example.com", "content": body, "snippet": "不能替代原文"}])[0]
        self.assertEqual(source["text"], body)
        self.assertIsNone(source["published_at"])
        self.assertEqual(read_sources([{"url": "https://example.com", "snippet": body}]), [])
        copies = read_sources([{"url": "https://example.com/a", "content": body}, {"url": "https://example.com/b", "content": body}])
        self.assertEqual(copies[1]["same_content_as"], copies[0]["id"])

    def test_atomic_document_collision_path_and_index(self):
        self.demo()
        q1, q2 = self.q("../问题/同秒:*?"), self.q("不同问题")
        w1, w2 = self.wb.claim(self.agent), self.wb.claim(self.agent)
        self.wb.finish(self.agent, w1, self.result(w1))
        self.wb.finish(self.agent, w2, self.result(w2))
        docs = self.wb.snapshot(self.owner, self.mid)["documents"]
        self.assertEqual(len({d["path"] for d in docs}), 2)
        for d in docs:
            self.assertTrue(self.wb.writer.path(d["path"]).is_file())
            self.assertIn("合成测试材料", self.wb.artifact(self.owner, d["id"])["content"])
        m = self.wb.snapshot(self.owner, self.mid)["meeting"]
        index = self.wb.writer.path(m["index_path"]).read_text()
        self.assertIn("不同问题", index)
        with self.assertRaises(LiveError):
            self.wb.writer.write("../../outside.md", "no")

    def test_document_human_edits_preserved_across_revision(self):
        self.demo()
        q = self.q()
        first = self.wb.claim(self.agent)
        self.wb.finish(self.agent, first, self.result(first))
        d = self.wb.snapshot(self.owner, self.mid)["documents"][0]
        path = self.wb.writer.path(d["path"])
        path.write_text("人工写的内容", encoding="utf-8")
        self.wb.edit(self.owner, q["id"], EditQuestion(title="更新范围", expected_version=1))
        second = self.wb.claim(self.agent)
        self.wb.finish(self.agent, second, self.result(second))
        self.assertEqual(path.read_text(), "人工写的内容")
        d2 = self.wb.snapshot(self.owner, self.mid)["documents"][0]
        self.assertEqual(d2["state"], "saved")
        self.assertNotEqual(d["path"], d2["path"])
        self.assertEqual(len(d2["history"]), 1)

    def test_index_marks_old_reference_stale_even_when_new_research_fails(self):
        self.demo()
        q = self.q()
        work = self.wb.claim(self.agent)
        self.wb.finish(self.agent, work, self.result(work))
        before = self.wb.snapshot(self.owner, self.mid)
        old_index = self.wb.writer.path(before["meeting"]["index_path"])
        old_index_content = old_index.read_text()
        document_path = self.wb.writer.path(before["documents"][0]["path"])
        old_document = document_path.read_text()
        self.wb.edit(self.owner, q["id"], EditQuestion(title="只看工业场景", expected_version=1))
        revised = self.wb.claim(self.agent)
        self.wb.fail(self.agent, revised, "search_unavailable")
        current = self.wb.snapshot(self.owner, self.mid)
        latest_index = self.wb.writer.path(current["meeting"]["index_path"])
        self.assertNotEqual(latest_index, old_index)
        self.assertIn("依据已更正", latest_index.read_text())
        self.assertEqual(old_index.read_text(), old_index_content)
        self.assertEqual(document_path.read_text(), old_document)
        self.assertEqual(current["questions"][0]["state"], "failed")

    def test_recovery_after_rename_before_database_commit(self):
        self.demo()
        self.q()
        work = self.wb.claim(self.agent)
        with patch.object(self.wb, "publish", return_value=None):
            self.wb.finish(self.agent, work, self.result(work))
        doc = self.wb.snapshot(self.owner, self.mid)["documents"][0]
        self.wb.writer.write(doc["path"], doc["content"])
        restored = Workbench(self.store, self.root / "meetings", self.wb.capabilities)
        restored.recover()
        self.assertEqual(restored.snapshot(self.owner, self.mid)["documents"][0]["state"], "saved")
        snapshot = restored.snapshot(self.owner, self.mid)
        self.assertEqual(len(list((self.root / "meetings").rglob("*.md"))), 1 + len(snapshot["meeting"]["index_paths"]))

    def test_recovery_never_replays_unknown_paid_work(self):
        self.demo()
        self.q()
        self.wb.claim(self.agent)
        self.wb.recover()
        self.assertIsNone(self.wb.claim(self.agent))
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["questions"][0]["state"], "outcome_unknown")

    def test_concurrent_model_budget_reservations_do_not_overspend(self):
        budget = ModelBudget(self.root / "budget.sqlite3", limit=1)
        def spend(_):
            try:
                budget.reserve()
                return True
            except RuntimeError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(spend, range(2))), 1)

    def test_coordinator_preserves_evidence_and_rejects_unknown_ids(self):
        self.demo()
        work = self.wb.coordinator_work(self.agent)
        s = work["segments"][0]
        wrong = Decisions.model_validate({"actions": [{"kind": "create_question", "title": "问题", "source_refs": [{"segment_id": "invented", "revision": 1}]}]})
        with self.assertRaises(LiveError):
            self.wb.commit_decisions(self.agent, work, wrong)
        valid = Decisions.model_validate({"actions": [{"kind": "create_question", "title": "问题", "source_refs": [{"segment_id": s["id"], "revision": s["revision"]}]}]})
        self.wb.commit_decisions(self.agent, work, valid)
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["questions"][0]["source_refs"][0]["segment_id"], s["id"])

    def test_scripted_end_to_end_outputs_real_files(self):
        runtime = WorkbenchRuntime(self.wb).start()
        try:
            self.demo()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                snapshot = self.wb.snapshot(self.owner, self.mid)
                if snapshot["counts"].get("documents") == 1:
                    break
                time.sleep(.02)
            self.assertEqual(snapshot["counts"].get("documents"), 1)
            self.assertEqual(len(snapshot["questions"]), 1)
            self.assertTrue(snapshot["questions"][0]["source_refs"])
            self.assertTrue(self.wb.artifact(self.owner, snapshot["documents"][0]["id"])["document"]["on_disk"])
        finally:
            runtime.close()

    def decision(self, work, kind, q=None, title="工作流如何定义"):
        s = work["segments"][-1]
        return Decisions.model_validate({"actions": [{"kind": kind, "title": title,
            "question_id": q["id"] if q else "", "expected_version": q["version"] if q else 1,
            "source_refs": [{"segment_id": s["id"], "revision": s["revision"]}]}]})

    def test_candidate_revision_still_needs_human_approval(self):
        self.demo()
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, self.decision(work, "candidate"))
        q = self.wb.snapshot(self.owner, self.mid)["questions"][0]
        self.wb.utterance(self.owner, self.mid, Utterance(text="补充范围"))
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, self.decision(work, "revise_question", q, "更准确的候选"))
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["questions"][0]["state"], "candidate")
        self.assertIsNone(self.wb.claim(self.agent))

    def test_cancel_fences_late_coordinator_and_same_basis_recreation(self):
        self.demo()
        q = self.q()
        work = self.wb.coordinator_work(self.agent)
        self.wb.action(self.owner, q["id"], QuestionAction(action="cancel", expected_version=1))
        with self.assertRaises(LiveError):
            self.wb.commit_decisions(self.agent, work, self.decision(work, "revise_question", q))
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["questions"][0]["state"], "cancelled")
        # A coordinator cannot re-create a cancelled request with identical evidence.
        self.wb.commit_decisions(self.agent, work, self.decision(work, "create_question", title="另一个问题"))
        q2 = next(q for q in self.wb.snapshot(self.owner, self.mid)["questions"] if q["title"] == "另一个问题")
        self.wb.action(self.owner, q2["id"], QuestionAction(action="cancel", expected_version=1))
        self.wb.commit_decisions(self.agent, work, self.decision(work, "create_question", title="另一个问题"))
        self.assertEqual(len(self.wb.snapshot(self.owner, self.mid)["questions"]), 2)

    def test_cancel_pending_publication_never_writes_late_file(self):
        self.demo()
        q = self.q()
        work = self.wb.claim(self.agent)
        with patch.object(self.wb, "publish", return_value=None):
            self.wb.finish(self.agent, work, self.result(work))
        doc = self.wb.snapshot(self.owner, self.mid)["documents"][0]
        self.wb.action(self.owner, q["id"], QuestionAction(action="cancel", expected_version=1))
        self.wb.publish(doc["id"])
        self.assertFalse(self.wb.writer.path(doc["path"]).exists())
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["questions"][0]["stage"], "已取消")

    def test_recovery_pauses_pending_coordinator_and_queued_jobs(self):
        self.demo()
        self.q()
        self.wb.recover()
        self.assertIsNone(self.wb.coordinator_work(self.agent))
        self.assertIsNone(self.wb.claim(self.agent))
        self.wb.control(self.owner, self.mid, "resume_research")
        self.assertIsNotNone(self.wb.coordinator_work(self.agent))
        self.assertIsNotNone(self.wb.claim(self.agent))

    def test_agent_demotion_invalidates_existing_work(self):
        self.demo()
        self.q()
        work = self.wb.claim(self.agent)
        with self.store.transaction() as db:
            actor = self.store._get(db, "member", self.agent["id"])
            actor["role"] = "member"
            self.store._put(db, "member", actor["id"], actor)
        with self.assertRaises(LiveError):
            self.wb.finish(self.agent, work, self.result(work))

    def test_runtime_close_blocks_inflight_stage_and_synthesis(self):
        self.demo()
        self.q()
        started, release, done = threading.Event(), threading.Event(), threading.Event()
        class BlockedResearcher:
            def run(inner, meeting, question, stage):
                started.set()
                release.wait(3)
                try:
                    stage("synthesizing")
                    raise AssertionError("must be fenced before further network work")
                finally:
                    done.set()
        runtime = WorkbenchRuntime(self.wb)
        runtime.demo_researcher = BlockedResearcher()
        runtime.tick()
        self.assertTrue(started.wait(2))
        runtime.close()
        release.set()
        self.assertTrue(done.wait(2))
        self.assertFalse(self.wb.snapshot(self.owner, self.mid)["documents"])

    def test_publication_no_clobber_even_when_target_appears_during_save(self):
        import os
        target = self.wb.writer.path("race.md")
        original_link = os.link
        def race(source, destination):
            Path(destination).write_text("人工编辑", encoding="utf-8")
            return original_link(source, destination)
        with patch("live.artifact_writer.os.link", side_effect=race), self.assertRaises(LiveError):
            self.wb.writer.write("race.md", "AI输出")
        self.assertEqual(target.read_text(), "人工编辑")

    def test_real_meetings_and_registered_versions_never_expire_automatically(self):
        from live.workbench_retention import purge
        real = self.wb.create(self.owner, NewMeeting(title="真实测试", data_scope="real"))
        self.wb.utterance(self.owner, real["id"], Utterance(text="受控测试发言"))
        q = self.wb.question(self.owner, real["id"], NewQuestion(title="待查证"))
        external = self.root / "原始录音.wav"
        external.write_bytes(b"original")
        unrelated = self.wb.writer.path(real["directory"] + "/人工资料.md")
        unrelated.write_text("用户文件", encoding="utf-8")
        registered = self.wb.writer.path(real["directory"] + "/output.md")
        registered.write_text("派生输出", encoding="utf-8")
        with self.store.transaction() as db:
            self.store._put(db, "wb_document", "test-doc", {"id": "test-doc", "meeting_id": real["id"], "path": real["directory"] + "/output.md", "state": "saved", "history": []})
        now = real["created_at"] + 7 * 86400 + 1
        self.assertEqual(purge(self.wb, now, apply=False)["expired_meetings"], [])
        self.assertTrue(registered.exists())
        purge(self.wb, now, apply=True)
        snapshot = self.wb.snapshot(self.owner, real["id"])
        self.assertEqual(snapshot["meeting"]["state"], "active")
        self.assertTrue(snapshot["segments"] and snapshot["questions"] and snapshot["documents"])
        self.assertTrue(registered.exists())
        self.assertTrue(external.exists() and unrelated.exists())
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["meeting"]["state"], "active")
        self.wb.utterance(self.owner, real["id"], Utterance(text="长期保留后仍可写入"))

    def test_level_updates_do_not_replace_capture_health_state(self):
        self.wb.audio(self.agent, self.mid, AudioEvent(meeting_id=self.mid, capture_epoch="e", stream_id="s", seq=1,
                      type="audio.ready", source="system", occurred_at="2026-11-15T05:00:00Z"))
        self.wb.audio(self.agent, self.mid, AudioEvent(meeting_id=self.mid, capture_epoch="e", stream_id="s", seq=2,
                      type="audio.level", source="system", occurred_at="2026-11-15T05:00:01Z", rms=.2))
        capture = self.wb.snapshot(self.owner, self.mid)["meeting"]["capture"]
        self.assertEqual(capture["status"], "ready")
        self.assertEqual(capture["level"], .2)

    def test_search_query_limit_is_enforced_before_reservation(self):
        budget = ModelBudget(self.root / "search-budget.sqlite3", limit=1)
        search = DeepSeekSearch("fixture-secret", budget)
        for query in (" ", "字" * 129):
            with self.assertRaises(ValueError):
                search.search(query)
        self.assertEqual(budget.reserve(), 1)

    def test_completed_scope_change_is_not_replayed_when_new_candidate_arrives(self):
        self.demo()
        first = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, first, self.decision(first, "create_question"))
        q = self.wb.snapshot(self.owner, self.mid)["questions"][0]
        work = self.wb.claim(self.agent)
        self.wb.finish(self.agent, work, self.result(work))
        self.wb.utterance(self.owner, self.mid, Utterance(text="另一个待讨论问题，暂时不要查资料"))
        latest = self.wb.coordinator_work(self.agent)
        # Replay of the old question's same title/refs must not consume another search.
        old_action = self.decision(first, "revise_question", q).actions[0]
        new_action = self.decision(latest, "candidate", title="待讨论的问题").actions[0]
        self.wb.commit_decisions(self.agent, latest, Decisions(actions=[old_action, new_action]))
        snapshot = self.wb.snapshot(self.owner, self.mid)
        original = next(item for item in snapshot["questions"] if item["id"] == q["id"])
        self.assertEqual((original["version"], original["state"]), (1, "completed"))
        self.assertEqual(snapshot["counts"].get("candidate"), 1)
        self.assertIsNone(self.wb.claim(self.agent))


    def test_unread_coordinator_input_survives_context_window_and_action_limit(self):
        self.wb.capabilities.update(model_enabled=True, debounce_seconds=0)
        for i in range(121):
            self.wb.audio(self.agent, self.mid, self.audio(i + 1, text=f"独立请求{i}", segment=f"s{i}"))
        handled = set()
        for _ in range(100):
            work = self.wb.coordinator_work(self.agent)
            if work is None:
                break
            actions = []
            for s in work["segments"]:
                if s["event_seq"] > work["meeting"]["coordinator_cursor"] and s["id"] not in handled:
                    if len(actions) >= work["action_limit"]:
                        break
                    actions.append({"kind": "create_question", "title": s["text"], "source_refs": [{"segment_id": s["id"], "revision": s["revision"]}]})
                    handled.add(s["id"])
            self.assertLessEqual(len(actions), work["action_limit"])
            self.wb.commit_decisions(self.agent, work, Decisions.model_validate({"actions": actions}))
        self.assertEqual(len(handled), 121)
        self.assertEqual(len(self.wb.snapshot(self.owner, self.mid)["questions"]), 121)
        self.assertIsNone(self.wb.coordinator_work(self.agent))

    def test_four_actions_keep_same_utterance_available_for_a_fifth_question(self):
        self.wb.capabilities.update(model_enabled=True, debounce_seconds=0)
        self.wb.audio(self.agent, self.mid, self.audio(text="合成发言包含五个独立查证请求"))
        work = self.wb.coordinator_work(self.agent)
        actions = [self.decision(work, "create_question", title=f"独立问题{i}").actions[0] for i in range(4)]
        self.wb.commit_decisions(self.agent, work, Decisions(actions=actions))
        continuation = self.wb.coordinator_work(self.agent)
        self.assertIsNotNone(continuation)
        self.assertEqual(continuation["meeting"]["coordinator_cursor"], 0)
        self.wb.commit_decisions(self.agent, continuation, self.decision(continuation, "create_question", title="独立问题4"))
        self.assertEqual(len(self.wb.snapshot(self.owner, self.mid)["questions"]), 5)
        self.assertIsNone(self.wb.coordinator_work(self.agent))

    def test_save_failure_recovery_republishes_local_result_without_research(self):
        self.demo()
        self.q("保存失败恢复")
        work = self.wb.claim(self.agent)
        with patch.object(self.wb.writer, "write", side_effect=OSError("synthetic disk failure")):
            self.wb.finish(self.agent, work, self.result(work))
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["documents"][0]["state"], "save_failed")
        self.wb.recover()
        doc = self.wb.snapshot(self.owner, self.mid)["documents"][0]
        self.assertEqual(doc["state"], "saved")
        self.assertTrue(self.wb.writer.path(doc["path"]).is_file())
        self.assertIsNone(self.wb.claim(self.agent))

    def test_saturated_coordinator_stops_on_repeated_actions_and_preserves_cursor(self):
        self.wb.capabilities.update(model_enabled=True, debounce_seconds=0)
        self.wb.audio(self.agent, self.mid, self.audio())
        work = self.wb.coordinator_work(self.agent)
        actions = [self.decision(work, "create_question", title=f"独立问题{i}").actions[0] for i in range(4)]
        self.wb.commit_decisions(self.agent, work, Decisions(actions=actions))
        continuation = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, continuation, Decisions(actions=actions))
        meeting = self.wb.snapshot(self.owner, self.mid)["meeting"]
        self.assertEqual(meeting["coordinator_state"], "failed")
        self.assertEqual(meeting["coordinator_error"], "continuation_no_progress")
        self.assertEqual(meeting["coordinator_cursor"], 0)
        self.assertIsNone(self.wb.coordinator_work(self.agent))

    def test_saturated_coordinator_has_a_finite_continuation_limit(self):
        self.wb.capabilities.update(model_enabled=True, debounce_seconds=0)
        self.wb.audio(self.agent, self.mid, self.audio())
        for batch in range(3):
            work = self.wb.coordinator_work(self.agent)
            self.assertIsNotNone(work)
            actions = [self.decision(work, "create_question", title=f"批次{batch}问题{i}").actions[0] for i in range(4)]
            self.wb.commit_decisions(self.agent, work, Decisions(actions=actions))
        meeting = self.wb.snapshot(self.owner, self.mid)["meeting"]
        self.assertEqual(meeting["coordinator_error"], "continuation_limit")
        self.assertEqual(meeting["coordinator_cursor"], 0)
        self.assertIsNone(self.wb.coordinator_work(self.agent))

    def test_retracted_only_coordinator_batch_can_advance_without_final_text(self):
        self.wb.capabilities.update(model_enabled=True, debounce_seconds=0)
        self.wb.audio(self.agent, self.mid, self.audio())
        self.wb.audio(self.agent, self.mid, self.audio(2, revision=2, text="", kind="transcript.retracted"))
        work = self.wb.coordinator_work(self.agent)
        self.assertEqual(work["segments"], [])
        self.wb.commit_decisions(self.agent, work, Decisions(actions=[]))
        self.assertIsNone(self.wb.coordinator_work(self.agent))

    def test_retired_retention_does_not_remove_even_old_temporary_files(self):
        import os
        from live.workbench_retention import purge
        real = self.wb.create(self.owner, NewMeeting(title="崩溃保留期合成复现", data_scope="real"))
        directory = self.wb.writer.path(real["directory"])
        user_file = directory / ".zsjh-user.tmp"
        user_file.write_text("用户保留文件")
        child = os.fork()
        if child == 0:
            with patch("live.artifact_writer.os.link", side_effect=lambda *_: os._exit(91)):
                self.wb.writer.write(real["directory"] + "/crash.md", "合成会议衍生内容")
            os._exit(1)
        _, status = os.waitpid(child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 91)
        self.assertEqual(len(list(directory.glob(".zsjh-*.tmp"))), 2)
        purge(self.wb, now=real["created_at"] + 7 * 86400 + 1, apply=True)
        self.assertEqual(len(list(directory.glob(".zsjh-*.tmp"))), 2)
        self.assertEqual(user_file.read_text(), "用户保留文件")

    def test_temporary_cleanup_preserves_a_replacement_inode(self):
        import os
        import tempfile
        directory = self.wb.writer.path(self.m["directory"])
        fd, name = tempfile.mkstemp(prefix=".zsjh-", suffix=".tmp", dir=directory)
        self.wb.writer._register_temporary(name, fd)
        os.close(fd)
        replacement = directory / "replacement.txt"
        replacement.write_text("人工替换后的文件")
        os.replace(replacement, name)
        self.assertEqual(self.wb.writer.clean_temporaries(self.m["directory"]), 0)
        self.assertEqual(Path(name).read_text(), "人工替换后的文件")
        import sqlite3
        with sqlite3.connect(self.wb.writer.temporary_registry) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM temporary_files").fetchone()[0], 0)

    def test_temporary_registry_uses_private_state_while_markdown_uses_output_root(self):
        self.assertEqual(self.wb.writer.temporary_registry.parent, self.store.path.parent.resolve())
        self.assertNotEqual(self.wb.writer.temporary_registry.parent, self.wb.writer.root)
        self.assertTrue(self.wb.writer.temporary_registry.is_file())
        self.assertFalse((self.wb.writer.root / ".zsjh-temporary.sqlite3").exists())
        self.demo()
        self.q("文档仍保存在用户输出目录")
        work = self.wb.claim(self.agent)
        self.wb.finish(self.agent, work, self.result(work))
        doc = self.wb.snapshot(self.owner, self.mid)["documents"][0]
        output = self.wb.writer.path(doc["path"])
        self.assertTrue(output.is_relative_to(self.wb.writer.root))
        self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
