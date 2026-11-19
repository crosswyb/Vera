"""Automatic scheduling and manual work remain independent; no paid requests."""

import tempfile
import unittest
from pathlib import Path

from live.source_reader import DemoResearcher
from live.store import LiveError
from live.workbench_contracts import Decisions, MeetingPermissions, NewMeeting, NewQuestion, QuestionAction, Utterance
from live_api import create_app


class AutomationControlsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        self.config = {"TESTING": True,
            "WORKBENCH_ROOT": root / "meetings", "WORKBENCH_CAPABILITIES": {
                "model_enabled": True, "search_enabled": True, "debounce_seconds": 0}}
        self.app = create_app(root / "state.sqlite3", self.config)
        self.wb = self.app.extensions["workbench"]
        members = self.wb.store.seed(fixture=True)["members"]
        self.owner, self.agent = [self.wb.store.authenticate(m["token"]) for m in (members[0], members[-1])]
        self.mid = self.wb.create(self.owner, NewMeeting(title="自动查证控制合成验证", data_scope="synthetic"))["id"]

    def say(self, number):
        self.wb.utterance(self.owner, self.mid, Utterance(text=f"合成讨论 {number}", segment_id=str(number)))

    def result(self, work):
        return DemoResearcher().run({**work["meeting"], "mode": "demo"}, work["question"], lambda _: None)

    def decisions(self, work, titles):
        segment = work["segments"][-1]
        return Decisions.model_validate({"actions": [{"kind": "create_question", "title": title,
            "public_query": "synthetic public reference", "source_refs": [
                {"segment_id": segment["id"], "revision": segment["revision"]}]} for title in titles]})

    def discover(self, number, *titles):
        self.say(number)
        work = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, work, self.decisions(work, titles))

    def test_manual_retry_executes_while_automation_is_paused(self):
        question = self.wb.question(self.owner, self.mid, NewQuestion(title="合成手动查证"))
        work = self.wb.claim(self.agent)
        self.wb.fail(self.agent, work, "synthetic_failure")
        self.wb.control(self.owner, self.mid, "pause_research")
        self.wb.action(self.owner, question["id"], QuestionAction(action="retry", expected_version=1))
        retry = self.wb.claim(self.agent)
        self.assertIsNotNone(retry, "暂停自动查证不应阻止用户明确点击的重试")
        self.assertEqual(retry["question"]["id"], question["id"])
        self.wb.finish(self.agent, retry, self.result(retry))
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["documents"][0]["state"], "saved")

    def test_pause_does_not_discard_started_research(self):
        self.wb.question(self.owner, self.mid, NewQuestion(title="合成已开始的查证"))
        work = self.wb.claim(self.agent)
        self.wb.control(self.owner, self.mid, "pause_research")
        self.wb.finish(self.agent, work, self.result(work))
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["documents"][0]["state"], "saved")

    def test_toggle_preserves_inflight_detection_and_commits_cursor_once(self):
        self.say(1)
        work = self.wb.coordinator_work(self.agent)
        self.wb.control(self.owner, self.mid, "pause_research")
        self.wb.control(self.owner, self.mid, "resume_research")
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["meeting"]["coordinator_state"], "running")
        self.wb.commit_decisions(self.agent, work, Decisions(actions=[]))
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["meeting"]["coordinator_cursor"], work["cursor"])
        self.assertIsNone(self.wb.coordinator_work(self.agent))

    def test_new_input_receives_bounded_context_instead_of_all_old_transcript(self):
        for number in range(40):
            self.say(number)
            work = self.wb.coordinator_work(self.agent)
            self.wb.commit_decisions(self.agent, work, Decisions(actions=[]))
        self.wb.control(self.owner, self.mid, "pause_research")
        self.say(40)
        self.wb.control(self.owner, self.mid, "resume_research")
        work = self.wb.coordinator_work(self.agent)
        old = [s for s in work["segments"] if s["event_seq"] <= work["meeting"]["coordinator_cursor"]]
        new = [s for s in work["segments"] if s["event_seq"] > work["meeting"]["coordinator_cursor"]]
        self.assertLessEqual(len(old), 6, "历史转写只能作为少量上下文，不能每次全部发送")
        self.assertEqual([s["text"] for s in new], ["合成讨论 40"])

    def test_paused_detection_finishes_but_only_manual_takeover_is_scheduled(self):
        self.say(1)
        work = self.wb.coordinator_work(self.agent)
        self.wb.control(self.owner, self.mid, "pause_research")
        self.wb.commit_decisions(self.agent, work, self.decisions(work, ["自动问题一", "自动问题二"]))
        snapshot = self.wb.snapshot(self.owner, self.mid)
        self.assertEqual(snapshot["automation_progress"], {"processed": 1, "pending": 0, "held_questions": 2,
                                                        "pending_characters": 0, "strategy": "live"})
        self.assertIsNone(self.wb.claim(self.agent))
        question = snapshot["questions"][1]
        self.wb.action(self.owner, question["id"], QuestionAction(action="retry", expected_version=1))
        manual = self.wb.claim(self.agent)
        self.assertEqual(manual["question"]["id"], question["id"])
        self.wb.finish(self.agent, manual, self.result(manual))
        self.assertIsNone(self.wb.claim(self.agent))
        self.assertTrue(self.wb.snapshot(self.owner, self.mid)["meeting"]["research_paused"])
        self.wb.control(self.owner, self.mid, "resume_research")
        self.assertIsNone(self.wb.coordinator_work(self.agent), "已提交的转写不应因恢复而重新分析")
        self.assertEqual(self.wb.claim(self.agent)["question"]["id"], snapshot["questions"][0]["id"])

    def test_manual_question_is_not_starved_by_twenty_held_automatic_jobs(self):
        for n in range(7):
            self.discover(n, *[f"自动排队 {n * 3 + i}" for i in range(3)])
        self.wb.control(self.owner, self.mid, "pause_research")
        question = self.wb.question(self.owner, self.mid, NewQuestion(title="暂停后手动添加"))
        self.assertEqual(question["state"], "backlog")
        manual = self.wb.claim(self.agent)
        self.assertIsNotNone(manual)
        self.assertEqual(manual["question"]["id"], question["id"])
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["automation_progress"]["held_questions"], 21)
        self.assertIsNone(self.wb.claim(self.agent))

    def test_new_transcript_does_not_duplicate_inflight_detection_or_lose_next_batch(self):
        self.say(1)
        first = self.wb.coordinator_work(self.agent)
        self.say(2)
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["meeting"]["coordinator_state"], "running")
        self.assertIsNone(self.wb.coordinator_work(self.agent))
        self.wb.control(self.owner, self.mid, "pause_research")
        self.wb.commit_decisions(self.agent, first, Decisions(actions=[]))
        self.assertIsNone(self.wb.coordinator_work(self.agent))
        self.wb.control(self.owner, self.mid, "resume_research")
        second = self.wb.coordinator_work(self.agent)
        new = [s["text"] for s in second["segments"] if s["event_seq"] > second["meeting"]["coordinator_cursor"]]
        self.assertEqual(new, ["合成讨论 2"])
        self.wb.commit_decisions(self.agent, second, Decisions(actions=[]))
        for _ in range(3):
            self.wb.control(self.owner, self.mid, "pause_research")
            self.wb.control(self.owner, self.mid, "resume_research")
            self.assertIsNone(self.wb.coordinator_work(self.agent))

    def test_resume_preserves_failures_until_explicit_detector_retry(self):
        self.discover(1)
        self.say(2)
        failed = self.wb.coordinator_work(self.agent)
        self.wb.coordinator_failed(self.agent, failed, unknown=True, code="outcome_unknown")
        self.wb.control(self.owner, self.mid, "pause_research")
        self.say(3)
        self.wb.control(self.owner, self.mid, "resume_research")
        meeting = self.wb.snapshot(self.owner, self.mid)["meeting"]
        self.assertEqual(meeting["coordinator_state"], "outcome_unknown")
        self.assertEqual(meeting["coordinator_error"], "outcome_unknown")
        self.assertEqual(meeting["coordinator_cursor"], failed["meeting"]["coordinator_cursor"])
        self.assertIsNone(self.wb.coordinator_work(self.agent))
        self.wb.control(self.owner, self.mid, "retry_coordinator")
        retry = self.wb.coordinator_work(self.agent)
        self.assertEqual([s["text"] for s in retry["segments"] if s["event_seq"] > retry["meeting"]["coordinator_cursor"]], ["合成讨论 2", "合成讨论 3"])

    def test_committed_cursor_survives_restart(self):
        for n in range(8):
            self.discover(n)
        self.wb.control(self.owner, self.mid, "pause_research")
        self.say(8)
        restarted = create_app(self.root / "state.sqlite3", self.config).extensions["workbench"]
        restarted.recover()
        restarted.control(self.owner, self.mid, "resume_research")
        work = restarted.coordinator_work(self.agent)
        self.assertEqual([s["text"] for s in work["segments"] if s["event_seq"] > work["meeting"]["coordinator_cursor"]], ["合成讨论 8"])
        self.assertLessEqual(len(work["segments"]), 7)

    def test_context_limit_does_not_skip_unread_long_transcript(self):
        for n in range(5):
            self.wb.utterance(self.owner, self.mid, Utterance(text=str(n) * 2000, segment_id=f"long-{n}"))
            work = self.wb.coordinator_work(self.agent)
            self.wb.commit_decisions(self.agent, work, Decisions(actions=[]))
        self.wb.control(self.owner, self.mid, "pause_research")
        for n in range(3):
            self.wb.utterance(self.owner, self.mid, Utterance(text=str(n) * 5000, segment_id=f"new-long-{n}"))
        self.wb.control(self.owner, self.mid, "resume_research")
        work = self.wb.coordinator_work(self.agent)
        old = [s for s in work["segments"] if s["event_seq"] <= work["meeting"]["coordinator_cursor"]]
        new = [s for s in work["segments"] if s["event_seq"] > work["meeting"]["coordinator_cursor"]]
        self.assertLessEqual(sum(len(s["text"]) for s in old), 3000)
        self.assertLessEqual(sum(len(s["text"]) for s in work["segments"]), 12000)
        self.assertEqual([s["segment_id"].rsplit(":", 1)[-1] for s in new], ["new-long-0", "new-long-1"])
        self.wb.commit_decisions(self.agent, work, Decisions(actions=[]))
        remaining = self.wb.coordinator_work(self.agent)
        self.assertEqual([s["segment_id"].rsplit(":", 1)[-1] for s in remaining["segments"] if s["event_seq"] > remaining["meeting"]["coordinator_cursor"]], ["new-long-2"])

    def test_end_cancel_and_revocation_still_reject_late_results(self):
        for action in ("end", "cancel", "revoke"):
            with self.subTest(action=action):
                question = self.wb.question(self.owner, self.mid, NewQuestion(title="失效结果 " + action))
                work = self.wb.claim(self.agent)
                self.wb.control(self.owner, self.mid, "pause_research")
                if action == "end":
                    self.wb.control(self.owner, self.mid, "end")
                elif action == "cancel":
                    self.wb.action(self.owner, question["id"], QuestionAction(action="cancel", expected_version=1))
                else:
                    self.wb.permissions(self.owner, self.mid, MeetingPermissions())
                with self.assertRaises(LiveError):
                    self.wb.finish(self.agent, work, self.result(work))
                self.assertFalse(self.wb.snapshot(self.owner, self.mid)["documents"])
                self.mid = self.wb.create(self.owner, NewMeeting(title="下一独立合成场次", data_scope="synthetic"))["id"]

    def test_explicit_detector_retry_has_its_own_authenticated_http_action(self):
        self.say(1)
        work = self.wb.coordinator_work(self.agent)
        self.wb.coordinator_failed(self.agent, work, code="model_contract_invalid")
        client = self.app.test_client()
        path = f"/live/v2/meetings/{self.mid}/controls"
        body = {"action": "retry_coordinator"}
        headers = {"Authorization": "Bearer fixture-owner-0"}
        self.assertEqual(client.post(path, json=body).status_code, 401)
        self.assertEqual(client.post(path, json=body, headers={**headers, "Origin": "https://evil.invalid"}).status_code, 403)
        self.assertEqual(client.post(path, json={"action": "resume_research"}, headers=headers).status_code, 200)
        self.assertIsNone(self.wb.coordinator_work(self.agent))
        self.assertEqual(client.post(path, json=body, headers=headers).status_code, 200)
        self.assertIsNotNone(self.wb.coordinator_work(self.agent))
        self.assertEqual(client.post(path, json=body, headers=headers).status_code, 409)


if __name__ == "__main__":
    unittest.main()
