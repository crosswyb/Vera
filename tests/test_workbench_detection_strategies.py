"""Independent synthetic acceptance of realtime/import detection isolation."""

import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from live.model_budget import ModelBudget
from live.provider_settings import ModelService
from live.source_reader import Researcher
from live.store import LiveError
from live.workbench_contracts import AudioEvent, AudioImport, AudioImportFinish, Decisions, MeetingPermissions, NewMeeting, Utterance
from live.workbench_runtime import TextModel, WorkbenchRuntime
from live_api import create_app


class DetectionStrategiesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.wb = create_app(self.root / "state.sqlite3", {
            "TESTING": True, "WORKBENCH_ROOT": self.root / "meetings",
            "WORKBENCH_SETTINGS_PATH": self.root / "providers.json",
            "WORKBENCH_CAPABILITIES": {"audio_enabled": True, "model_enabled": True,
                "search_enabled": True, "debounce_seconds": 0}}).extensions["workbench"]
        members = self.wb.store.seed(fixture=True)["members"]
        self.owner, self.agent = [self.wb.store.authenticate(m["token"]) for m in (members[0], members[-1])]

    def meeting(self, imported=False, title="合成策略验证", scope="synthetic"):
        m = self.wb.create(self.owner, NewMeeting(title=title, data_scope=scope,
            input_kind="audio_file" if imported else "live"))
        if imported:
            self.wb.begin_import(self.agent, m["id"], AudioImport(import_id="fixture", file_name="synthetic.wav",
                duration_ms=2000000, bytes=1000))
        return m["id"]

    def append(self, mid, texts, imported=False):
        for index, text in enumerate(texts):
            if imported:
                self.wb.audio(self.agent, mid, AudioEvent(meeting_id=mid, capture_epoch="fixture", stream_id="fixture-file",
                    seq=index + 1, type="transcript.final", source="file", occurred_at="2026-11-19T12:00:00Z",
                    segment_id=f"s{index}", revision=1, text=text, audio_start_ms=index * 1000, audio_end_ms=index * 1000 + 900))
            else:
                self.wb.utterance(self.owner, mid, Utterance(text=text, segment_id=f"s{index}"))

    def eof(self, mid):
        self.wb.finish_import(self.agent, mid, AudioImportFinish(import_id="fixture", state="completed"))

    def new_segments(self, work):
        return [s for s in work["segments"] if s["event_seq"] > work["meeting"]["coordinator_cursor"]]

    def drain(self, lane, *, make_questions=False):
        seen, batches = [], []
        for _ in range(250):
            work = self.wb.coordinator_work(self.agent, lane=lane)
            if work is None:
                return seen, batches
            new = self.new_segments(work)
            seen.extend(s["text"] for s in new)
            self.assertTrue(new)
            self.assertLessEqual(sum(len(s["text"]) for s in work["segments"]), 12000)
            self.assertTrue(all(not s.get("truncated") for s in new))
            batches.append(work)
            existing = {q["title"] for q in work["questions"]}
            actions = [{"kind": "create_question", "title": s["text"], "public_query": "synthetic evidence",
                "source_refs": [{"segment_id": s["id"], "revision": s["revision"]}]} for s in new if s["text"] not in existing]
            self.wb.commit_decisions(self.agent, work, Decisions.model_validate({
                "actions": actions[:work["action_limit"]] if make_questions else []}))
        self.fail("detection did not finish after 250 synthetic batches")

    def test_676_short_import_segments_are_read_exactly_once_in_few_requests(self):
        mid = self.meeting(True)
        texts = [f"合成音频第{i:03d}段，原话保留不截断。" for i in range(676)]
        self.append(mid, texts, True)
        self.eof(mid)
        seen, batches = self.drain("background")
        self.assertEqual(seen, texts)
        self.assertLessEqual(len(batches), 8, "import still incurs one request per small realtime batch")
        self.assertGreater(len(self.new_segments(batches[0])), 16)
        m = self.wb.snapshot(self.owner, mid)["meeting"]
        self.assertEqual(m["coordinator_cursor"], m["semantic_seq"])
        self.assertEqual(m["coordinator_state"], "idle")

    def test_import_keeps_full_long_segments_and_final_tail(self):
        mid = self.meeting(True)
        texts = ["首段" + "甲" * 4998, "中段" + "乙" * 4998, "末段" + "丙" * 4998, "最后只有一句，也不能遗漏。"]
        self.append(mid, texts, True)
        self.eof(mid)
        seen, _ = self.drain("background")
        self.assertEqual(seen, texts)

    def test_saturated_import_actions_do_not_skip_middle_or_last_questions(self):
        mid = self.meeting(True)
        texts = [f"合成待查问题{i:03d}" for i in range(96)]
        self.append(mid, texts, True)
        self.eof(mid)
        _, batches = self.drain("background", make_questions=True)
        snapshot = self.wb.snapshot(self.owner, mid)
        self.assertEqual({q["title"] for q in snapshot["questions"]}, set(texts))
        self.assertEqual(snapshot["meeting"]["coordinator_cursor"], snapshot["meeting"]["semantic_seq"])
        self.assertEqual(snapshot["meeting"]["coordinator_state"], "idle")
        self.assertFalse(snapshot["meeting"].get("coordinator_error"))
        self.assertLess(len(batches), len(texts))

    def test_processing_import_wait_is_bounded_and_eof_tail_is_immediate(self):
        now = time.time()
        with patch("live.workbench.time.time", return_value=now):
            mid = self.meeting(True)
            self.append(mid, ["合成短片段，等待后续输入。"], True)
        with patch("live.workbench.time.time", return_value=now + 1):
            self.assertIsNone(self.wb.coordinator_work(self.agent, lane="background"))
        with patch("live.workbench.time.time", return_value=now + 3.1):
            work = self.wb.coordinator_work(self.agent, lane="background")
            self.assertIsNotNone(work)
            self.wb.commit_decisions(self.agent, work, Decisions(actions=[]))
        with patch("live.workbench.time.time", return_value=now + 4):
            tail = self.meeting(True, "尾批合成测试")
            self.append(tail, ["短尾批立即查证。"], True)
            self.eof(tail)
            self.assertEqual(self.wb.coordinator_work(self.agent, lane="background")["meeting"]["id"], tail)

    def test_realtime_keeps_small_batches_and_excludes_imports(self):
        background = self.meeting(True)
        self.append(background, ["合成导入" * 900], True)
        live = self.meeting()
        self.append(live, [f"实时合成{i}" for i in range(5)])
        work = self.wb.coordinator_work(self.agent, lane="realtime")
        self.assertEqual(work["meeting"]["id"], live)
        self.assertEqual(len(self.new_segments(work)), 4)
        self.assertEqual(work["action_limit"], 4)
        self.assertEqual(self.wb.coordinator_work(self.agent, lane="background")["meeting"]["id"], background)

    def test_realtime_debounce_still_avoids_request_for_each_immediate_fragment(self):
        self.wb.capabilities["debounce_seconds"] = .8
        now = time.time()
        with patch("live.workbench.time.time", return_value=now):
            mid = self.meeting()
            self.append(mid, ["刚结束的一句合成发言。"])
        with patch("live.workbench.time.time", return_value=now + .2):
            self.assertIsNone(self.wb.coordinator_work(self.agent, lane="realtime"))
        with patch("live.workbench.time.time", return_value=now + .9):
            self.assertIsNotNone(self.wb.coordinator_work(self.agent, lane="realtime"))

    def test_background_meetings_rotate_instead_of_draining_one_first(self):
        mids = [self.meeting(True, f"轮转{i}") for i in range(3)]
        for mid in mids:
            self.append(mid, [f"合成讨论{i:03d}" + "甲" * 90 for i in range(130)], True)
            self.eof(mid)
        claimed = []
        for _ in range(6):
            work = self.wb.coordinator_work(self.agent, lane="background")
            self.assertIsNotNone(work)
            claimed.append(work["meeting"]["id"])
            self.wb.commit_decisions(self.agent, work, Decisions(actions=[]))
        self.assertEqual(set(claimed[:3]), set(mids))
        self.assertEqual(set(claimed[3:6]), set(mids))

    def test_ended_meeting_resume_and_restart_continue_unread_input(self):
        mid = self.meeting(True)
        texts = [f"会后待处理{i:03d}" + "甲" * 80 for i in range(100)]
        self.append(mid, texts, True)
        self.eof(mid)
        stale = self.wb.coordinator_work(self.agent, lane="background")
        self.wb.control(self.owner, mid, "end")
        with self.assertRaises(LiveError):
            self.wb.commit_decisions(self.agent, stale, Decisions(actions=[]))
        self.assertIsNone(self.wb.coordinator_work(self.agent, lane="background"))
        self.wb.recover(publish=False)
        self.wb.control(self.owner, mid, "resume_research")
        self.assertIsNone(self.wb.coordinator_work(self.agent, lane="realtime"))
        seen, _ = self.drain("background")
        self.assertEqual(seen, texts)

    def test_ended_detection_failure_can_be_explicitly_retried(self):
        mid = self.meeting(True)
        self.append(mid, ["会后查证合成问题"], True)
        self.eof(mid)
        self.wb.control(self.owner, mid, "end")
        self.wb.control(self.owner, mid, "resume_research")
        work = self.wb.coordinator_work(self.agent, lane="background")
        self.wb.coordinator_failed(self.agent, work, unknown=True, code="outcome_unknown")
        self.assertIsNone(self.wb.coordinator_work(self.agent, lane="background"))
        self.wb.control(self.owner, mid, "retry_coordinator")
        retried = self.wb.coordinator_work(self.agent, lane="background")
        self.assertIsNotNone(retried)
        self.assertEqual(retried["cursor"], work["cursor"])

    def test_ended_unread_question_is_detected_researched_and_saved_as_markdown(self):
        mid = self.meeting(True)
        self.append(mid, ["合成会后请求：请查证工作流的定义并给出来源。"], True)
        self.eof(mid)
        ended = self.wb.control(self.owner, mid, "end")
        self.wb.control(self.owner, mid, "resume_research")
        detector = self.wb.coordinator_work(self.agent, lane="background")
        segment = self.new_segments(detector)[0]
        self.wb.commit_decisions(self.agent, detector, Decisions.model_validate({"actions": [{
            "kind": "create_question", "title": "会后新增的工作流定义查证", "public_query": "synthetic workflow definition",
            "source_refs": [{"segment_id": segment["id"], "revision": segment["revision"]}]}]}))
        work = self.wb.claim(self.agent)
        self.assertIsNotNone(work)
        self.assertEqual(work["meeting"]["state"], "ended")
        quote = "工作流通过预先设定的步骤组织处理过程。"

        class Search:
            def search(_, query):
                return [{"url": "https://fixture.invalid/workflow", "title": "离线合成测试资料",
                         "content": ("这是合成测试正文，不能用于业务判断。" + quote) * 8}]

        class Model:
            def answer(_, meeting, question, sources):
                return {"answer": "合成参考回答：工作流通过预设步骤组织处理。",
                    "claims": [{"text": "合成材料描述了工作流的预设步骤。", "source_id": sources[0]["id"], "quote": quote}],
                    "uncertainty": ["未执行真实搜索或模型调用。"], "evidence_status": "supported"}

        result = Researcher(Search(), Model()).run(work["meeting"], work["question"],
            lambda stage: self.wb.stage(self.agent, work, stage))
        self.wb.finish(self.agent, work, result)
        snapshot = self.wb.snapshot(self.owner, mid)
        self.assertEqual(snapshot["meeting"]["state"], "ended")
        self.assertEqual(snapshot["meeting"]["ended_at"], ended["ended_at"])
        self.assertEqual(snapshot["meeting"]["coordinator_cursor"], snapshot["meeting"]["semantic_seq"])
        self.assertEqual(snapshot["questions"][0]["state"], "completed")
        self.assertEqual(snapshot["questions"][0]["source_refs"][0]["segment_id"], segment["id"])
        self.assertEqual(len(snapshot["documents"]), 1)
        doc = snapshot["documents"][0]
        self.assertEqual(doc["state"], "saved")
        path = self.wb.writer.path(doc["path"])
        self.assertEqual(path.suffix, ".md")
        self.assertIn(quote, path.read_text())
        self.assertIn("会后新增的工作流定义查证", path.read_text())
        self.assertIsNone(self.wb.coordinator_work(self.agent, lane="background"))
        self.assertIsNone(self.wb.claim(self.agent))

    def test_permissions_revoke_inflight_background_result_and_further_claims(self):
        # The real-scope permission boundary is exercised with synthetic data.
        mid = self.meeting(True, scope="real")
        self.append(mid, ["合成问题"], True)
        self.eof(mid)
        work = self.wb.coordinator_work(self.agent, lane="background")
        self.wb.permissions(self.owner, mid, MeetingPermissions(model=False, search=False, auto_search=False))
        with self.assertRaises(LiveError):
            self.wb.commit_decisions(self.agent, work, Decisions(actions=[]))
        self.assertIsNone(self.wb.coordinator_work(self.agent, lane="background"))

    def automatic_questions(self, mid, *, imported, count):
        self.append(mid, [f"自动查证合成问题{i}" for i in range(count)], imported)
        if imported:
            self.eof(mid)
        work = self.wb.coordinator_work(self.agent, lane="background" if imported else "realtime")
        actions = [{"kind": "create_question", "title": s["text"], "public_query": "synthetic automatic question",
            "source_refs": [{"segment_id": s["id"], "revision": s["revision"]}]} for s in self.new_segments(work)]
        self.wb.commit_decisions(self.agent, work, Decisions.model_validate({"actions": actions}))

    def test_import_research_cannot_fill_both_slots_while_live_meeting_is_open(self):
        background = self.meeting(True)
        self.automatic_questions(background, imported=True, count=3)
        live = self.meeting()
        first = self.wb.claim(self.agent)
        self.assertEqual(first["meeting"]["id"], background)
        self.assertIsNone(self.wb.claim(self.agent), "imported automatic research consumed the reserved live slot")
        self.automatic_questions(live, imported=False, count=1)
        second = self.wb.claim(self.agent)
        self.assertEqual(second["meeting"]["id"], live)
        self.assertIsNone(self.wb.claim(self.agent))

    def test_import_research_still_uses_two_slots_without_an_open_live_meeting(self):
        background = self.meeting(True)
        self.automatic_questions(background, imported=True, count=3)
        first, second = self.wb.claim(self.agent), self.wb.claim(self.agent)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first["meeting"]["id"], background)
        self.assertEqual(second["meeting"]["id"], background)
        self.assertNotEqual(first["question"]["id"], second["question"]["id"])
        self.assertIsNone(self.wb.claim(self.agent))

    def test_blocked_import_worker_does_not_block_realtime_detection(self):
        background = self.meeting(True)
        self.append(background, ["合成导入" * 900], True)
        entered, release, heard = (threading.Event() for _ in range(3))
        class Model:
            def decisions(_, work):
                if work["meeting"]["id"] == background:
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("synthetic gate timed out")
                else:
                    heard.set()
                return Decisions(actions=[])
        runtime = WorkbenchRuntime(self.wb, model=Model())
        try:
            runtime.tick()
            self.assertTrue(entered.wait(2))
            live = self.meeting()
            self.append(live, ["实时老板刚提出的合成问题。"])
            runtime.tick()
            self.assertTrue(heard.wait(2), "realtime waited behind a blocked imported-audio detection")
            self.assertFalse(release.is_set())
        finally:
            release.set()
            runtime.close()
            runtime.coordinator_pool.shutdown(wait=True)
            runtime.background_coordinator_pool.shutdown(wait=True)


class DetectionCapacityTest(unittest.TestCase):
    def test_two_background_requests_leave_one_realtime_slot_with_total_at_most_three(self):
        counters = {"active": 0, "background": 0, "max_active": 0, "max_background": 0}
        entered, released = {key: threading.Event() for key in ("bg1", "bg2", "bg3", "live")}, threading.Event()
        mutex = threading.Lock()
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                name = json.loads(body["messages"][1]["content"])["name"]
                with mutex:
                    counters["active"] += 1
                    counters["background"] += int(name != "live")
                    counters["max_active"] = max(counters["max_active"], counters["active"])
                    counters["max_background"] = max(counters["max_background"], counters["background"])
                entered[name].set()
                released.wait(5)
                raw = b'{"choices":[{"message":{"content":"{\\"actions\\":[]}"},"finish_reason":"stop"}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                with mutex:
                    counters["active"] -= 1
                    counters["background"] -= int(name != "live")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                model = TextModel(ModelBudget(Path(folder) / "budget.sqlite3", limit="unlimited"),
                    ModelService(provider="openai_compatible", base_url=f"http://127.0.0.1:{server.server_port}",
                        name="synthetic", api_key="fixture-only"))
                pool = ThreadPoolExecutor(max_workers=4)
                try:
                    futures = [pool.submit(model.call, "synthetic", {"name": key}) for key in ("bg1", "bg2")]
                    self.assertTrue(entered["bg1"].wait(2) and entered["bg2"].wait(2))
                    futures.append(pool.submit(model.call, "synthetic", {"name": "bg3"}))
                    self.assertFalse(entered["bg3"].wait(.1))
                    futures.append(pool.submit(model.call, "synthetic", {"name": "live"}, realtime=True))
                    self.assertTrue(entered["live"].wait(2), "background queue consumed reserved realtime capacity")
                    self.assertFalse(entered["bg3"].is_set())
                    released.set()
                    for future in futures:
                        self.assertEqual(future.result(timeout=3), {"actions": []})
                finally:
                    released.set()
                    pool.shutdown(wait=True)
                    model.close()
        finally:
            released.set()
            server.shutdown()
            server.server_close()
        self.assertLessEqual(counters["max_active"], 3)
        self.assertLessEqual(counters["max_background"], 2)
        self.assertEqual(counters["max_active"], 3)


if __name__ == "__main__":
    unittest.main()
