"""Observable research before completion, with real local SSE and task fences."""

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from live.model_budget import ModelBudget
from live.model_stream import read_completion, short_answer_preview
from live.provider_settings import ModelService
from live.source_reader import Researcher, read_sources
from live.store import LiveError
from live.workbench_contracts import EditQuestion, NewMeeting, NewQuestion, QuestionAction
from live.workbench_runtime import OutcomeUnknown, TextModel, WorkbenchRuntime
from live_api import create_app


QUOTE = "这段合成原文说明工作流按步骤与条件组织任务，部署条件需要另行核对。"
RESULTS = [{"url": "https://fixture.invalid/workflow", "title": "合成原文", "content": QUOTE * 6}]
ANSWER = {"brief": {"short_answer": "资料描述按步骤和条件组织任务，部署能力仍待验证。",
    "key_points": [{"text": "合成来源描述步骤与条件。", "kind": "source_claim", "claim_indices": [1]}],
    "caveat": "合成资料不能作为业务依据。"}, "answer": "这是一段合成参考分析。",
    "claims": [{"text": "来源描述按步骤组织任务。", "source_id": "source-1", "quote": QUOTE}],
    "uncertainty": ["部署能力尚未核对。"], "evidence_status": "supported"}


def sse(delta=None, *, finish=None, usage=None):
    value = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        value["usage"] = usage
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


class Response:
    status_code = 200

    def __init__(self, frames, streaming=True):
        self.frames = frames
        self.headers = {"Content-Type": "text/event-stream" if streaming else "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_lines(self, **_):
        for frame in self.frames:
            yield from frame.splitlines()

    def iter_content(self, _=None, **kwargs):
        yield from self.frames


class ResearchProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        app = create_app(root / "state.sqlite3", {"TESTING": True, "WORKBENCH_ROOT": root / "meetings",
            "WORKBENCH_CAPABILITIES": {"model_enabled": True, "search_enabled": True}})
        self.wb = app.extensions["workbench"]
        members = self.wb.store.seed(fixture=True)["members"]
        self.owner, self.agent = [self.wb.store.authenticate(m["token"]) for m in (members[0], members[-1])]
        self.mid = self.wb.create(self.owner, NewMeeting(title="实时进度验收（合成）", data_scope="synthetic"))["id"]
        self.wb.question(self.owner, self.mid, NewQuestion(title="工作流如何组织任务", public_query="workflow steps"))
        self.budget = ModelBudget(root / "usage.sqlite3", limit="unlimited")
        self.model = TextModel(self.budget, ModelService(provider="openai_compatible", base_url="https://fixture.invalid/v1",
            api_key="fixture-secret", name="fixture-model", max_tokens=8192, reasoning_effort="max"))
        self.search = Mock()
        self.search.search.return_value = RESULTS
        self.runtime = WorkbenchRuntime(self.wb, model=self.model, researcher=Researcher(self.search, self.model))
        self.addCleanup(self.runtime.close)

    def question(self):
        return self.wb.snapshot(self.owner, self.mid)["questions"][0]

    def progress(self, work, kind, **details):
        self.wb.progress(self.agent, work, kind, details)

    def test_local_sse_exposes_sources_and_draft_before_final_validation(self):
        """A real HTTP response remains open while the snapshot is inspected."""
        release, visible = threading.Event(), threading.Event()
        payloads, errors = [], []
        answer_text = json.dumps(ANSWER, ensure_ascii=False)
        boundary = answer_text.index('", "key_points"')
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                payloads.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(sse({"reasoning_content": "private-reasoning-fixture"}))
                self.wfile.write(sse({"content": answer_text[:boundary]}))
                self.wfile.flush()
                release.wait(5)
                self.wfile.write(sse({"content": answer_text[boundary:]}))
                self.wfile.write(sse(finish="stop", usage={"completion_tokens": 90}))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(release.set)
        self.model.config = self.model.config.model_copy(update={"base_url": f"http://127.0.0.1:{server.server_port}"})
        original = self.wb.progress

        def observe(actor, work, kind, details):
            original(actor, work, kind, details)
            if details.get("draft"):
                visible.set()

        self.wb.progress = observe
        work = self.wb.claim(self.agent)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.runtime._research, self.agent, work)
            try:
                self.assertTrue(visible.wait(4), "SSE bytes were buffered until the complete answer")
                q = test.question()
                self.assertEqual(q["state"], "running")
                self.assertIsNone(q["result"])
                self.assertFalse(self.wb.snapshot(self.owner, self.mid)["documents"])
                self.assertEqual(q["progress"]["query"], "workflow steps")
                self.assertEqual(q["progress"]["sources"][0]["title"], "合成原文")
                self.assertIn("按步骤", q["progress"]["model"]["draft"])
                self.assertGreater(q["progress"]["model"]["reasoning_chars"], 0)
                self.assertNotIn("private-reasoning-fixture", json.dumps(q))
                self.assertFalse(future.done())
            finally:
                release.set()
            future.result(timeout=5)
        q = self.question()
        self.assertEqual(q["state"], "completed")
        self.assertEqual(q["result"]["brief"], ANSWER["brief"])
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["documents"][0]["state"], "saved")
        self.assertTrue(payloads[0]["stream"])
        self.assertEqual((payloads[0]["max_tokens"], payloads[0]["reasoning_effort"]), (8192, "max"))

    def test_provider_returning_json_is_accepted_without_second_request(self):
        body = json.dumps({"choices": [{"message": {"content": json.dumps(ANSWER)}, "finish_reason": "stop"}]}).encode()
        events = []
        with patch("requests.Session.post", return_value=Response([body], streaming=False)) as post:
            answer = self.model.answer({}, {"title": "合成问题"}, read_sources(RESULTS), progress=lambda k, d: events.append((k, d)))
        self.assertEqual(answer["brief"], ANSWER["brief"])
        self.assertEqual(post.call_count, 1)
        self.assertTrue(any(d.get("mode") == "buffered" for _, d in events))

    def test_interrupted_stream_keeps_sources_and_specific_failure_without_publishing_draft(self):
        frames = [sse({"content": '{"brief":{"short_answer":"尚未核验'})]
        work = self.wb.claim(self.agent)
        with patch("requests.Session.post", return_value=Response(frames)) as post:
            self.runtime._research(self.agent, work)
        q = self.question()
        self.assertEqual(q["state"], "outcome_unknown")
        self.assertEqual(q["error_details"]["code"], "connection_interrupted")
        self.assertEqual(q["error_details"]["stage"], "synthesizing")
        self.assertTrue(q["progress"]["sources"])
        self.assertIsNone(q["result"])
        self.assertFalse(self.wb.snapshot(self.owner, self.mid)["documents"])
        self.assertEqual(post.call_count, 1)

    def test_cancellation_rejects_late_progress_and_retry_starts_clean(self):
        old = self.wb.claim(self.agent)
        self.progress(old, "search_query", query="old query")
        self.wb.action(self.owner, old["question"]["id"], QuestionAction(action="cancel", expected_version=1))
        with self.assertRaises(LiveError):
            self.progress(old, "model_delta", draft="晚到的内容")
        self.wb.action(self.owner, old["question"]["id"], QuestionAction(action="retry", expected_version=1))
        self.assertNotIn("progress", self.question())
        fresh = self.wb.claim(self.agent)
        self.assertNotIn("query", self.question()["progress"])
        with self.assertRaises(LiveError):
            self.progress(old, "search_query", query="旧请求覆盖新进度")
        self.progress(fresh, "search_query", query="new query")
        self.assertEqual(self.question()["progress"]["query"], "new query")

    def test_revision_and_meeting_end_reject_late_updates(self):
        old = self.wb.claim(self.agent)
        self.wb.edit(self.owner, old["question"]["id"], EditQuestion(title="更正的问题范围", expected_version=1))
        with self.assertRaises(LiveError):
            self.progress(old, "model_delta", draft="旧范围")
        self.assertNotIn("progress", self.question())
        fresh = self.wb.claim(self.agent)
        self.wb.control(self.owner, self.mid, "end")
        with self.assertRaises(LiveError):
            self.progress(fresh, "model_response", mode="stream")

    def test_pausing_automation_does_not_stop_active_progress(self):
        work = self.wb.claim(self.agent)
        self.wb.control(self.owner, self.mid, "pause_research")
        self.progress(work, "model_delta", phase="thinking", reasoning_chars=30)
        self.assertEqual(self.question()["progress"]["model"]["phase"], "thinking")

    def test_progress_is_bounded_and_deltas_do_not_fill_global_activity(self):
        work = self.wb.claim(self.agent)
        self.wb.stage(self.agent, work, "synthesizing")
        before = len(self.wb.snapshot(self.owner, self.mid)["activity"])
        for n in range(100):
            self.progress(work, "model_delta", phase="answering", answer_chars=n, draft="字" * 500,
                          reasoning_content="private-fixture", api_key="fixture-secret")
        q = self.question()
        self.assertEqual(len(self.wb.snapshot(self.owner, self.mid)["activity"]), before)
        self.assertLessEqual(len(q["progress"]["events"]), 16)
        self.assertEqual(len(q["progress"]["model"]["draft"]), 80)
        self.assertNotIn("fixture-secret", json.dumps(q))
        self.assertNotIn("private-fixture", json.dumps(q))

    def test_cancelled_model_queue_does_not_reserve_or_send_another_call(self):
        work = self.wb.claim(self.agent)
        model = self.model
        test = self
        class WaitingSlot:
            def __enter__(self):
                test.wb.action(test.owner, work["question"]["id"], QuestionAction(action="cancel", expected_version=1))
            def __exit__(self, *_):
                pass
        def progress(kind, details):
            self.progress(work, kind, **details)
        with patch('live.workbench_runtime.MODEL_SLOTS', WaitingSlot()), patch.object(model.budget, 'reserve') as reserve, patch('requests.Session.post') as post:
            with self.assertRaises(LiveError):
                model.call('synthetic', {}, progress=progress)
        reserve.assert_not_called()
        post.assert_not_called()

    def test_source_previews_reject_private_urls_and_clip_originals(self):
        work = self.wb.claim(self.agent)
        self.progress(work, "sources_ready", result_count=3, sources=read_sources(RESULTS) + [{"url": "http://127.0.0.1/private"}])
        sources = self.question()["progress"]["sources"]
        self.assertEqual(len(sources), 1)
        self.assertLessEqual(len(sources[0]["excerpt"]), 240)
        self.assertNotIn("text", sources[0])

    def test_invalid_quote_stream_does_not_become_final_answer(self):
        bad = {**ANSWER, "claims": [{"text": "编造的主张", "source_id": "source-1", "quote": "原文中不存在的引文"}]}
        frames = [sse({"content": json.dumps(bad)}), sse(finish="stop"), b"data: [DONE]\n\n"]
        work = self.wb.claim(self.agent)
        with patch("requests.Session.post", return_value=Response(frames)) as post:
            self.runtime._research(self.agent, work)
        q = self.question()
        self.assertEqual(q["state"], "failed")
        self.assertIsNone(q["result"])
        self.assertFalse(self.wb.snapshot(self.owner, self.mid)["documents"])
        self.assertEqual(post.call_count, 2, "only one correction of received invalid output is allowed")
        self.assertEqual(q["progress"]["model"]["attempt"], 2)

    def test_output_limit_in_stream_keeps_diagnostic_and_does_not_retry(self):
        frames = [sse({"content": '{"brief":{"short_answer":"未完成'}),
                  sse(finish="length", usage={"completion_tokens": 8192}), b"data: [DONE]\n\n"]
        work = self.wb.claim(self.agent)
        with patch("requests.Session.post", return_value=Response(frames)) as post:
            self.runtime._research(self.agent, work)
        self.assertEqual(self.question()["state"], "failed")
        self.assertIn("8192", self.question()["error"])
        self.assertEqual(post.call_count, 1)

    def test_partial_json_preview_handles_escapes_and_never_returns_raw_json(self):
        for content, expected in [('{"brief":{"short_answer":"第一行\\n第二行', "第一行\n第二行"),
            ('```json\n{"brief":{"short_answer":"中文', "中文"),
            ('{"answer":"\\\"short_answer\\\": injected"', ""), ('not json', "")]:
            with self.subTest(content=content):
                self.assertEqual(short_answer_preview(content), expected)

    def test_multiline_sse_comments_usage_and_other_choice_are_handled(self):
        lines = [b": keepalive\n\n", b'data: {"choices":\ndata: [{"index":0,"delta":{"content":"{\\"ok\\":true}"}}]}\n\n',
            b'data: {"choices":[{"index":1,"delta":{"content":"ignore"}}]}\n\n',
            sse(finish="stop"), b'data: {"choices":[],"usage":{"total_tokens":30}}\n\n', b"data: [DONE]\n\n"]
        result = read_completion(Response(lines), lambda _: None)
        self.assertEqual(result["choices"][0]["message"]["content"], '{"ok":true}')
        self.assertEqual(result["usage"], {"total_tokens": 30})

    def test_newline_free_stream_is_bounded_before_a_frame_completes(self):
        frames = [b"data: " + b"x" * 1_000_001]
        work = self.wb.claim(self.agent)
        with patch("requests.Session.post", return_value=Response(frames)) as post:
            self.runtime._research(self.agent, work)
        q = self.question()
        self.assertEqual(q["error_details"]["code"], "response_too_large")
        self.assertFalse(self.wb.snapshot(self.owner, self.mid)["documents"])
        self.assertEqual(post.call_count, 1)

    def test_sse_envelopes_over_one_megabyte_do_not_reject_a_small_answer(self):
        # Many tiny deltas have much larger wire size than decoded content.
        frames = [sse({"reasoning_content": "合成"})] * 10000
        frames += [sse({"content": json.dumps(ANSWER, ensure_ascii=False)}), sse(finish="stop"), b"data: [DONE]\n\n"]
        self.assertGreater(sum(map(len, frames)), 1_000_000)
        work = self.wb.claim(self.agent)
        with patch("requests.Session.post", return_value=Response(frames)) as post:
            self.runtime._research(self.agent, work)
        self.assertEqual(self.question()["state"], "completed")
        self.assertEqual(post.call_count, 1)
        self.assertNotIn("reasoning_content", json.dumps(self.question()))

    def test_decoded_stream_and_wire_limits_remain_bounded(self):
        for frames in ([sse({"reasoning_content": "字" * 200000})] * 2,
                       [b": heartbeat\n\n"] * 1250000):
            with self.subTest(kind="decoded" if frames[0].startswith(b"data") else "wire"):
                with patch("requests.Session.post", return_value=Response(frames)) as post:
                    with self.assertRaises(OutcomeUnknown) as caught:
                        self.model.call("synthetic", {}, progress=lambda *_: None)
                self.assertEqual(caught.exception.diagnostic["code"], "response_too_large")
                self.assertIn("limit_kind", caught.exception.diagnostic)
                self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
