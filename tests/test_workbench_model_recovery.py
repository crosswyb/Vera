"""Offline transport and coordinator recovery through the actual request path."""

import json
import socket
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests
import urllib3

from live.failure_diagnostics import RequestOutcomeUnknown, transport_details
from live.model_budget import ModelBudget
from live.model_transport import retry_after_seconds
from live.provider_settings import ModelService
from live.workbench_contracts import Decisions, NewMeeting, Utterance
from live.workbench_runtime import TextModel, WorkbenchRuntime
from live_api import create_app


class Response:
    status_code = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, _size):
        yield b'{"choices":[{"message":{"content":"{\\"actions\\":[]}"},"finish_reason":"stop"}]}'


class TransportRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def model(self, base_url="https://fixture.invalid/v1"):
        return TextModel(ModelBudget(self.root / "usage.sqlite3", limit="unlimited"),
                         ModelService(provider="openai_compatible", base_url=base_url,
                                      name="synthetic", api_key="fixture-secret"))

    def test_connect_wait_is_longer_without_changing_model_wait_or_payload(self):
        with patch("requests.Session.post", return_value=Response()) as post:
            self.assertEqual(self.model().call("synthetic", {}), {"actions": []})
        self.assertEqual(post.call_args.kwargs["timeout"], (20, 180))
        self.assertNotIn("connect_timeout", post.call_args.kwargs["json"])

    def test_successful_calls_reuse_the_real_http_connection(self):
        clients = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                clients.append(self.client_address)
                data = next(Response().iter_content(0))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            model = self.model(f"http://127.0.0.1:{server.server_port}/v1")
            model.call("synthetic", {})
            model.call("synthetic", {})
            self.assertEqual(len(clients), 2)
            self.assertEqual(clients[0], clients[1], "each call opened a new TCP connection")
            model.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_sessions_are_reused_per_thread_without_sharing_mutable_sessions(self):
        model = self.model()
        sessions = {}
        barrier = threading.Barrier(2)

        def post(session, *_args, **_kwargs):
            sessions.setdefault(threading.get_ident(), []).append(session)
            return Response()

        def run():
            barrier.wait(timeout=2)
            model.call("synthetic", {})
            model.call("synthetic", {})

        with patch("requests.Session.post", autospec=True, side_effect=post), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            for future in futures:
                future.result(timeout=5)
        pairs = list(sessions.values())
        self.assertEqual(len(pairs), 2)
        self.assertIs(pairs[0][0], pairs[0][1])
        self.assertIs(pairs[1][0], pairs[1][1])
        self.assertIsNot(pairs[0][0], pairs[1][0])
        model.close()

    def test_tls_handshake_timeout_is_not_reported_as_180_second_model_wait(self):
        class Connection:
            is_closed = True
            proxy = None
            has_connected_to_proxy = False

            def __init__(self, *_args, **kwargs):
                self.timeout = kwargs.get("timeout")

            def close(self):
                pass

        class Pool(urllib3.HTTPSConnectionPool):
            ConnectionCls = Connection

            def _validate_conn(self, _conn):
                raise socket.timeout("fixture-secret synthetic handshake")

        class Adapter(requests.adapters.HTTPAdapter):
            def get_connection_with_tls_context(self, *_args, **_kwargs):
                return Pool("offline.invalid", retries=False)

            def cert_verify(self, *_args, **_kwargs):
                pass

        with requests.Session() as session:
            session.trust_env = False
            session.mount("https://", Adapter(max_retries=0))
            try:
                session.post("https://offline.invalid", timeout=(20, 180))
            except requests.ReadTimeout as exc:
                details = transport_details(exc, "model", timeout=180, connect_timeout=20,
                                            elapsed_ms=20025, request_id=7, response_received=False)
            else:
                self.fail("offline handshake probe did not exercise the requests timeout chain")
        self.assertEqual(details["code"], "tls_handshake_timeout")
        self.assertEqual(details["transport_phase"], "tls_handshake")
        self.assertEqual(details["timeout_seconds"], 20)
        self.assertIn("20", details["message"])
        self.assertNotIn("fixture-secret", json.dumps(details))

    def test_text_model_does_not_implicitly_replay_unknown_paid_calls(self):
        with patch("requests.Session.post", side_effect=requests.ReadTimeout("private")) as post:
            with self.assertRaises(RequestOutcomeUnknown):
                self.model().call("synthetic", {})
        self.assertEqual(post.call_count, 1)

    def test_retry_after_is_bounded_and_accepts_http_date(self):
        response = Response()
        for raw, expected in [("30", 30), ("99999", 300), ("invalid", None),
                              ("Thu, 01 Jan 1970 00:01:00 GMT", 10)]:
            with self.subTest(raw=raw), patch("live.model_transport.time.time", return_value=50):
                response.headers = {"Retry-After": raw}
                self.assertEqual(retry_after_seconds(response), expected)

    def test_close_retires_idle_sessions_but_does_not_break_a_pinned_task(self):
        model = self.model()
        entered, release = threading.Event(), threading.Event()

        def post(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return Response()

        with patch("requests.Session.post", side_effect=post), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(model.call, "synthetic", {})
            self.assertTrue(entered.wait(2))
            model.close()
            release.set()
            self.assertEqual(future.result(timeout=2), {"actions": []})
            self.assertFalse(model.sessions.sessions)
            self.assertEqual(pool.submit(model.call, "synthetic", {}).result(timeout=2), {"actions": []})
        self.assertFalse(model.sessions.sessions)


class CoordinatorRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        app = create_app(root / "state.sqlite3", {"TESTING": True, "WORKBENCH_ROOT": root / "meetings",
            "WORKBENCH_CAPABILITIES": {"model_enabled": True, "search_enabled": True, "debounce_seconds": 0}})
        self.wb = app.extensions["workbench"]
        members = self.wb.store.seed(fixture=True)["members"]
        self.owner, self.agent = [self.wb.store.authenticate(m["token"]) for m in (members[0], members[-1])]
        self.mid = self.wb.create(self.owner, NewMeeting(title="合成恢复测试", data_scope="synthetic"))["id"]
        model = TextModel(ModelBudget(root / "usage.sqlite3", limit="unlimited"), ModelService(
            provider="openai_compatible", base_url="https://fixture.invalid/v1", name="synthetic", api_key="fixture-secret"))
        self.runtime = WorkbenchRuntime(self.wb, model=model)
        self.addCleanup(self.runtime.close)

    def meeting(self):
        return self.wb.snapshot(self.owner, self.mid)["meeting"]

    def say(self, number):
        self.wb.utterance(self.owner, self.mid, Utterance(text=f"合成发言 {number}", segment_id=str(number)))

    def fail_detection(self, work, code="read_timeout", **details):
        self.wb.coordinator_failed(self.agent, work, unknown=True, code="outcome_unknown", details={
            "category": "request", "service": "model", "stage": "detecting", "code": code,
            "message": "合成临时故障", **details})

    def test_runtime_timeout_schedules_retry_and_keeps_unread_cursor(self):
        self.say(1)
        work = self.wb.coordinator_work(self.agent)
        with patch("requests.Session.post", side_effect=requests.ReadTimeout("fixture-secret")) as post:
            self.runtime._coordinate(self.agent, work)
        meeting = self.meeting()
        self.assertEqual(meeting["coordinator_state"], "retry_wait")
        self.assertEqual(meeting["coordinator_retry"]["attempts"], 1)
        self.assertEqual(meeting["coordinator_retry"]["delay_seconds"], 2)
        self.assertEqual(meeting["coordinator_cursor"], work["meeting"]["coordinator_cursor"])
        self.assertIsNone(self.wb.coordinator_work(self.agent))
        self.assertEqual(post.call_count, 1, "retry must be scheduled, never sleep inside a worker")

    def test_retry_recovers_without_reprocessing_committed_input_or_duplicate_questions(self):
        self.say(1)
        first = self.wb.coordinator_work(self.agent)
        self.wb.commit_decisions(self.agent, first, Decisions(actions=[]))
        self.say(2)
        work = self.wb.coordinator_work(self.agent)
        self.fail_detection(work)
        self.say(3)
        meeting = self.meeting()
        self.assertEqual(meeting["coordinator_state"], "retry_wait", "new transcript must not bypass backoff")
        with patch("live.workbench.time.time", return_value=meeting["coordinator_retry"]["next_at"] + .1):
            retry = self.wb.coordinator_work(self.agent)
        new = [s["text"] for s in retry["segments"] if s["event_seq"] > retry["meeting"]["coordinator_cursor"]]
        self.assertEqual(new, ["合成发言 2", "合成发言 3"])
        segment = retry["segments"][-1]
        decision = Decisions.model_validate({"actions": [{"kind": "create_question", "title": "合成恢复后的问题",
            "public_query": "synthetic", "source_refs": [{"segment_id": segment["id"], "revision": segment["revision"]}]}]})
        self.wb.commit_decisions(self.agent, retry, decision)
        self.wb.commit_decisions(self.agent, retry, decision)
        self.assertEqual(len(self.wb.snapshot(self.owner, self.mid)["questions"]), 1)
        self.assertNotIn("coordinator_retry", self.meeting())
        self.assertIsNone(self.wb.coordinator_work(self.agent))
        self.fail_detection(work)
        self.assertIsNone(self.meeting()["coordinator_error"], "a late failure cannot undo a committed retry")

    def test_retry_stops_after_three_retries_even_if_new_audio_keeps_arriving(self):
        self.say(1)
        work = self.wb.coordinator_work(self.agent)
        for attempt, delay in enumerate([2, 5, 15], start=1):
            self.fail_detection(work)
            meeting = self.meeting()
            self.assertEqual(meeting["coordinator_retry"]["attempts"], attempt)
            self.assertEqual(meeting["coordinator_retry"]["delay_seconds"], delay)
            self.say(attempt + 1)
            with patch("live.workbench.time.time", return_value=meeting["coordinator_retry"]["next_at"] + .1):
                work = self.wb.coordinator_work(self.agent)
            self.assertIsNotNone(work)
        self.fail_detection(work)
        self.assertEqual(self.meeting()["coordinator_state"], "outcome_unknown")
        self.assertEqual(self.meeting()["coordinator_retry"]["state"], "exhausted")
        self.say(5)
        self.assertIsNone(self.wb.coordinator_work(self.agent))

    def test_fatal_errors_and_long_reasoning_deadlines_are_not_retried(self):
        self.say(1)
        for code, extra in [("http_error", {"http_status": 401}), ("http_error", {"http_status": 403}),
                            ("http_error", {"http_status": 429, "provider_code": "insufficient_quota"}),
                            ("http_error", {"http_status": 400}), ("tls_error", {}),
                            ("response_deadline", {}), ("response_too_large", {}),
                            ("unknown_outcome", {})]:
            with self.subTest(code=code, extra=extra):
                work = self.wb.coordinator_work(self.agent)
                self.fail_detection(work, code, **extra)
                self.assertNotEqual(self.meeting()["coordinator_state"], "retry_wait")
                self.assertIsNone(self.wb.coordinator_work(self.agent))
                self.wb.control(self.owner, self.mid, "retry_coordinator")

    def test_http_backpressure_honors_retry_after(self):
        self.say(1)
        self.fail_detection(self.wb.coordinator_work(self.agent), "http_error", http_status=429, retry_after_seconds=30)
        self.assertEqual(self.meeting()["coordinator_retry"]["delay_seconds"], 30)

    def test_waiting_retry_is_cleared_and_paused_after_restart(self):
        self.say(1)
        self.fail_detection(self.wb.coordinator_work(self.agent))
        self.wb.recover(publish=False)
        meeting = self.meeting()
        self.assertTrue(meeting["research_paused"])
        self.assertNotIn("coordinator_retry", meeting)
        self.assertIsNone(self.wb.coordinator_work(self.agent))

    def test_manual_retry_cannot_duplicate_an_automatic_request_in_flight(self):
        from live.store import LiveError
        self.say(1)
        self.fail_detection(self.wb.coordinator_work(self.agent))
        due = self.meeting()["coordinator_retry"]["next_at"] + 1
        with patch("live.workbench.time.time", return_value=due):
            self.assertIsNotNone(self.wb.coordinator_work(self.agent))
        with self.assertRaises(LiveError):
            self.wb.control(self.owner, self.mid, "retry_coordinator")

    def test_pause_end_and_restart_do_not_start_a_waiting_retry(self):
        self.say(1)
        self.fail_detection(self.wb.coordinator_work(self.agent))
        due = self.meeting()["coordinator_retry"]["next_at"] + 1
        self.wb.control(self.owner, self.mid, "pause_research")
        with patch("live.workbench.time.time", return_value=due):
            self.assertIsNone(self.wb.coordinator_work(self.agent))
            self.wb.control(self.owner, self.mid, "resume_research")
            self.assertIsNotNone(self.wb.coordinator_work(self.agent))
        self.wb.recover(publish=False)
        self.assertTrue(self.meeting()["research_paused"])
        self.assertIsNone(self.wb.coordinator_work(self.agent))
        self.wb.control(self.owner, self.mid, "end")
        self.assertNotIn("coordinator_retry", self.meeting())
        with patch("live.workbench.time.time", return_value=due + 100):
            self.assertIsNone(self.wb.coordinator_work(self.agent))


if __name__ == "__main__":
    unittest.main()
