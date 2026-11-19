"""Meeting capture can end while existing research remains resumable; offline only."""

import tempfile
import unittest
from pathlib import Path

from live.source_reader import DemoResearcher
from live.store import LiveError
from live.workbench_contracts import Decisions, EditQuestion, MeetingPermissions, NewMeeting, NewQuestion, QuestionAction, Utterance
from live_api import create_app


class PostMeetingResearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {"TESTING": True, "WORKBENCH_ROOT": self.root / "meetings",
            "WORKBENCH_CAPABILITIES": {"model_enabled": True, "search_enabled": True, "debounce_seconds": 0}}
        self.app = create_app(self.root / "state.sqlite3", self.config)
        self.wb = self.app.extensions["workbench"]
        seed = self.wb.store.seed(fixture=True)
        self.owner, self.member, self.other_member, self.agent = [self.wb.store.authenticate(m["token"]) for m in seed["members"]]
        self.headers = {"Authorization": "Bearer fixture-owner-0"}
        self.mid = self.create()

    def create(self, kind="live"):
        return self.wb.create(self.owner, NewMeeting(title="会后继续查证（合成）", data_scope="synthetic", input_kind=kind))["id"]

    def question(self, title="合成查证问题"):
        return self.wb.question(self.owner, self.mid, NewQuestion(title=title, public_query="synthetic public reference"))

    def snapshot(self):
        return self.wb.snapshot(self.owner, self.mid)

    def result(self, work):
        return DemoResearcher().run({**work["meeting"], "mode": "demo", "data_scope": "synthetic"}, work["question"], lambda _: None)

    def finish(self, work):
        return self.wb.finish(self.agent, work, self.result(work))

    def test_retry_after_end_executes_and_publishes_without_reopening_meeting(self):
        for kind in ("live", "audio_file"):
            with self.subTest(input_kind=kind):
                self.mid = self.create(kind)
                question = self.question()
                old = self.wb.claim(self.agent)
                self.wb.progress(self.agent, old, "search_query", {"query": "synthetic old request"})
                ended = self.wb.control(self.owner, self.mid, "end")
                self.assertIsNone(self.wb.claim(self.agent))
                path = f"/live/v2/questions/{question['id']}/actions"
                response = self.app.test_client().post(path, headers=self.headers,
                    json={"action": "retry", "expected_version": 1})
                self.assertEqual(response.status_code, 200, response.json)
                retry = self.wb.claim(self.agent)
                self.assertEqual(retry["question"]["id"], question["id"])
                self.assertNotEqual(retry["question"]["generation"], old["question"]["generation"])
                for late in (lambda: self.wb.stage(self.agent, old, "synthesizing"),
                             lambda: self.wb.progress(self.agent, old, "search_query", {"query": "late"}),
                             lambda: self.finish(old)):
                    with self.assertRaises(LiveError):
                        late()
                self.wb.fail(self.agent, old, "synthetic_late_error")
                self.wb.progress(self.agent, retry, "search_query", {"query": "synthetic new request"})
                self.finish(retry)
                snapshot = self.snapshot()
                self.assertEqual(snapshot["meeting"]["state"], "ended")
                self.assertEqual(snapshot["meeting"]["ended_at"], ended["ended_at"])
                self.assertTrue(snapshot["meeting"]["research_paused"], "单个重试不应恢复整场查证")
                self.assertEqual(snapshot["meeting"]["summary"], ended["summary"])
                self.assertEqual(snapshot["questions"][0]["state"], "completed")
                doc = snapshot["documents"][0]
                self.assertEqual(doc["state"], "saved")
                self.assertTrue(self.wb.writer.path(doc["path"]).is_file())
                self.assertIsNone(self.wb.claim(self.agent))

    def test_end_pauses_backlog_and_resume_only_requeues_unfinished_questions(self):
        done = self.question("已经完成的参考资料")
        self.finish(self.wb.claim(self.agent))
        completed = self.snapshot()["questions"][0]
        for n in range(23):
            self.question(f"等待继续的查证 {n}")
        interrupted = self.wb.claim(self.agent)
        before = self.snapshot()
        self.assertIn("backlog", [q["state"] for q in before["questions"]])
        self.wb.control(self.owner, self.mid, "end")
        paused = self.snapshot()
        self.assertTrue(all(q["state"] == "paused" for q in paused["questions"] if q["id"] != done["id"]))
        self.assertTrue(next(q for q in paused["questions"] if q["id"] == interrupted["question"]["id"])["progress"].get("finished_at"))
        # Old installed versions marked interrupted calls resume_queued=False.
        with self.wb.store.transaction() as db:
            legacy = self.wb.store._get(db, "wb_question", interrupted["question"]["id"])
            legacy["resume_queued"] = False
            self.wb.store._put(db, "wb_question", legacy["id"], legacy)
        resumed = self.wb.control(self.owner, self.mid, "resume_research")
        self.assertEqual(resumed["state"], "ended")
        self.assertEqual(resumed["coordinator_state"], "idle")
        self.assertIsNone(self.wb.coordinator_work(self.agent))
        processed = []
        while work := self.wb.claim(self.agent):
            processed.append(work["question"]["id"])
            self.finish(work)
        self.assertEqual(len(processed), 23)
        self.assertEqual(len(set(processed)), 23)
        after = self.snapshot()
        self.assertEqual(next(q for q in after["questions"] if q["id"] == done["id"]), completed)
        self.assertEqual(len(after["documents"]), 24)
        self.assertTrue(all(d["state"] == "saved" for d in after["documents"]))

    def test_resume_never_restarts_detector_or_reanalyses_old_transcript(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="合成：请调研工作流", segment_id="s1"))
        detector = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, detector, Decisions(actions=[]))
        self.wb.utterance(self.owner, self.mid, Utterance(text="合成：仍未分析的尾部发言", segment_id="s2"))
        late_detector = self.wb.coordinator_work(self.agent)
        self.question()
        ended = self.wb.control(self.owner, self.mid, "end")
        for _ in range(3):
            self.wb.control(self.owner, self.mid, "resume_research")
            self.assertIsNone(self.wb.coordinator_work(self.agent))
            self.assertEqual(self.snapshot()["meeting"]["coordinator_cursor"], ended["coordinator_cursor"])
            self.wb.control(self.owner, self.mid, "pause_research")
        with self.assertRaises(LiveError):
            self.wb.commit_decisions(self.agent, late_detector, Decisions(actions=[]))
        with self.assertRaises(LiveError):
            self.wb.control(self.owner, self.mid, "retry_coordinator")
        with self.assertRaises(LiveError):
            self.wb.utterance(self.owner, self.mid, Utterance(text="结束后不能收新发言"))

    def test_pausing_resumed_batch_still_allows_single_manual_retry(self):
        first = self.question("恢复后先运行的问题")
        second = self.question("恢复后仍等待的问题")
        self.wb.control(self.owner, self.mid, "end")
        self.wb.control(self.owner, self.mid, "resume_research")
        running = self.wb.claim(self.agent)
        self.assertEqual(running["question"]["id"], first["id"])
        self.wb.control(self.owner, self.mid, "pause_research")
        self.assertIsNone(self.wb.claim(self.agent))
        self.finish(running)
        self.wb.action(self.owner, second["id"], QuestionAction(action="retry", expected_version=1))
        manual = self.wb.claim(self.agent)
        self.assertEqual(manual["question"]["id"], second["id"])
        self.finish(manual)
        self.assertTrue(self.snapshot()["meeting"]["research_paused"])

    def test_failed_cancelled_and_stale_questions_require_individual_action(self):
        failed = self.question("失败问题")
        self.wb.fail(self.agent, self.wb.claim(self.agent), "synthetic_failure")
        cancelled = self.question("主动取消的问题")
        self.wb.action(self.owner, cancelled["id"], QuestionAction(action="cancel", expected_version=1))
        stale = self.question("需要确认依据的问题")
        with self.wb.store.transaction() as db:
            q = self.wb.store._get(db, "wb_question", stale["id"])
            q.update(basis_stale=True, state="needs_review")
            self.wb.store._put(db, "wb_question", q["id"], q)
        self.wb.control(self.owner, self.mid, "end")
        self.wb.control(self.owner, self.mid, "resume_research")
        self.assertIsNone(self.wb.claim(self.agent))
        states = {q["id"]: q["state"] for q in self.snapshot()["questions"]}
        self.assertEqual(states[failed["id"]], "failed")
        self.assertEqual(states[cancelled["id"]], "cancelled")
        with self.assertRaises(LiveError):
            self.wb.action(self.owner, stale["id"], QuestionAction(action="retry", expected_version=1))
        updated = self.wb.edit(self.owner, stale["id"], EditQuestion(expected_version=1, title="已确认的范围"))
        self.assertEqual(updated["version"], 2)
        with self.assertRaises(LiveError):
            self.wb.action(self.owner, stale["id"], QuestionAction(action="retry", expected_version=1))
        self.finish(self.wb.claim(self.agent))

    def test_ended_permissions_remain_enforced_and_can_be_changed_by_owner(self):
        self.question()
        self.wb.control(self.owner, self.mid, "end")
        self.wb.permissions(self.owner, self.mid, MeetingPermissions())
        self.wb.control(self.owner, self.mid, "resume_research")
        # Use real scope here so that per-meeting permission is mandatory.
        with self.wb.store.transaction() as db:
            m = self.wb.store._get(db, "wb_meeting", self.mid)
            m["data_scope"] = "real"
            self.wb._save(db, m)
        self.assertIsNone(self.wb.claim(self.agent))
        self.wb.permissions(self.owner, self.mid, MeetingPermissions(model=True, search=True, auto_search=True))
        self.assertIsNone(self.wb.coordinator_work(self.agent))
        work = self.wb.claim(self.agent)
        self.assertIsNotNone(work)
        self.wb.permissions(self.owner, self.mid, MeetingPermissions())
        with self.assertRaises(LiveError):
            self.finish(work)

    def test_ended_controls_keep_auth_and_reject_expired_or_deleting_meetings(self):
        question = self.question()
        self.wb.control(self.owner, self.mid, "end")
        client = self.app.test_client()
        path = f"/live/v2/questions/{question['id']}/actions"
        body = {"action": "retry", "expected_version": 1}
        self.assertEqual(client.post(path, json=body).status_code, 401)
        self.assertEqual(client.post(path, json=body, headers={**self.headers, "Origin": "https://evil.invalid"}).status_code, 403)
        with self.assertRaises(LiveError):
            self.wb.permissions(self.member, self.mid, MeetingPermissions())
        with self.wb.store.transaction() as db:
            disabled = self.wb.store._get(db, "member", self.other_member["id"])
            disabled["enabled"] = False
            self.wb.store._put(db, "member", disabled["id"], disabled)
        with self.assertRaises(LiveError):
            self.wb.action(self.other_member, question["id"], QuestionAction(**body))
        for state in ("expired", "deleting", "delete_failed"):
            with self.subTest(state=state):
                with self.wb.store.transaction() as db:
                    m = self.wb.store._get(db, "wb_meeting", self.mid)
                    m["state"] = state
                    self.wb._save(db, m)
                for change in (lambda: self.wb.action(self.owner, question["id"], QuestionAction(**body)),
                               lambda: self.wb.control(self.owner, self.mid, "resume_research"),
                               lambda: self.wb.edit(self.owner, question["id"], EditQuestion(expected_version=1, title="不可修改")),
                               lambda: self.wb.permissions(self.owner, self.mid, MeetingPermissions())):
                    with self.assertRaises(LiveError):
                        change()
                self.assertIsNone(self.wb.claim(self.agent))

    def test_ended_retry_survives_restart_without_automatic_paid_replay(self):
        question = self.question()
        self.wb.control(self.owner, self.mid, "end")
        self.wb.action(self.owner, question["id"], QuestionAction(action="retry", expected_version=1))
        restarted = create_app(self.root / "state.sqlite3", self.config).extensions["workbench"]
        restarted.recover()
        self.assertIsNone(restarted.claim(self.agent))
        restarted.control(self.owner, self.mid, "resume_research")
        work = restarted.claim(self.agent)
        self.assertEqual(work["question"]["id"], question["id"])
        restarted.finish(self.agent, work, self.result(work))
        self.assertEqual(restarted.snapshot(self.owner, self.mid)["documents"][0]["state"], "saved")


if __name__ == "__main__":
    unittest.main()
