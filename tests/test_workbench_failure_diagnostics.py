"""Real request failure -> runtime -> persisted user-visible diagnostics."""

import errno
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from live.model_budget import ModelBudget
from live.provider_settings import ModelService
from live.source_reader import Researcher, TavilySearch
from live.output_validation import failure_details
from live.workbench_contracts import NewMeeting, NewQuestion, QuestionAction, Utterance
from live.workbench_runtime import TextModel, WorkbenchRuntime
from live_api import create_app


class Response:
    def __init__(self, data, status=200):
        self.status_code = status
        self.data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, _size):
        yield self.data


class FailureDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        app = create_app(self.root / "workbench.sqlite3", {"TESTING": True,
            "WORKBENCH_ROOT": self.root / "meetings",
            "WORKBENCH_CAPABILITIES": {"model_enabled": True, "search_enabled": True}})
        self.wb = app.extensions["workbench"]
        members = self.wb.store.seed(fixture=True)["members"]
        self.owner, self.agent = [self.wb.store.authenticate(m["token"]) for m in (members[0], members[-1])]
        self.mid = self.wb.create(self.owner, NewMeeting(title="合成错误诊断", data_scope="synthetic"))["id"]
        self.wb.question(self.owner, self.mid, NewQuestion(title="合成问题", public_query="synthetic query"))
        self.budget = ModelBudget(self.root / "model-usage.sqlite3", limit="unlimited")
        self.model = TextModel(self.budget, ModelService(provider="openai_compatible", base_url="https://fixture.invalid/v1",
            name="fixture-model", api_key="fixture-secret", max_tokens=8192, reasoning_effort="max"))
        self.search = Mock()
        self.search.search.return_value = [{"url": "https://fixture.invalid/source", "content": "这是可核验的合成原文，供错误诊断使用。" * 8}]
        self.runtime = WorkbenchRuntime(self.wb, model=self.model, researcher=Researcher(self.search, self.model))
        self.addCleanup(self.runtime.close)

    def run_question(self, **post_args):
        work = self.wb.claim(self.agent)
        with patch("requests.Session.post", **post_args) as post:
            self.runtime._research(self.agent, work)
        return self.wb.snapshot(self.owner, self.mid)["questions"][0], post

    def test_read_timeout_keeps_actual_stage_and_actionable_reason_without_secret(self):
        question, post = self.run_question(side_effect=requests.ReadTimeout("fixture-secret private request body"))
        self.assertEqual(question["state"], "outcome_unknown")
        self.assertIsNotNone(question.get("error_details"), "request exception class was discarded before the UI")
        self.assertEqual(question["error_details"]["stage"], "synthesizing")
        self.assertEqual(question["error_details"]["code"], "read_timeout")
        self.assertIn("超时", question["error"])
        self.assertNotIn("180", question["error"], "the configured timeout is not the actual elapsed time")
        self.assertNotIn("fixture-secret", json.dumps(question))
        self.assertEqual(post.call_count, 1)

    def test_http_rejection_reaches_question_instead_of_research_unavailable(self):
        question, post = self.run_question(return_value=Response({"error": {"code": "api_key_expired", "message": "fixture-secret"}}, 401))
        self.assertIn("401", question.get("error") or "")
        self.assertIn("过期", question["error"])
        self.assertEqual(question["error_details"]["stage"], "synthesizing")
        self.assertNotIn("fixture-secret", json.dumps(question))
        self.assertEqual(post.call_count, 1)

    def test_output_limit_discloses_actual_limit_without_repeating_the_same_paid_request(self):
        response = Response({"choices": [{"message": {"content": "{unfinished"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 8192, "total_tokens": 8292}})
        question, post = self.run_question(return_value=response)
        self.assertEqual(question["state"], "failed")
        self.assertIn("8192", question.get("error") or "")
        self.assertIn("上限", question["error"])
        self.assertEqual(post.call_count, 1, "the same capped output cannot be repaired by repeating it")

    def test_network_failures_are_distinct_and_never_echo_private_exception_text(self):
        dns = requests.ConnectionError("fixture-secret")
        dns.__cause__ = socket.gaierror("fixture-secret")
        unreachable = requests.ConnectionError("fixture-secret")
        unreachable.__cause__ = OSError(errno.ENETUNREACH, "fixture-secret")
        cases = [(dns, "dns_error"), (requests.exceptions.SSLError("fixture-secret"), "tls_error"),
                 (unreachable, "network_unreachable"),
                 (requests.ConnectTimeout("fixture-secret"), "connect_timeout"),
                 (requests.exceptions.ChunkedEncodingError("fixture-secret"), "connection_interrupted")]
        for exc, code in cases:
            with self.subTest(code=code):
                if self.wb.snapshot(self.owner, self.mid)["questions"][0]["state"] != "queued":
                    q = self.wb.snapshot(self.owner, self.mid)["questions"][0]
                    self.wb.action(self.owner, q["id"], QuestionAction(action="retry", expected_version=q["version"]))
                q, post = self.run_question(side_effect=exc)
                self.assertEqual(q["error_details"]["code"], code)
                self.assertEqual(q["error_details"]["stage"], "synthesizing")
                self.assertNotIn("fixture-secret", json.dumps(q))
                self.assertEqual(post.call_count, 1)

    def test_search_timeout_is_identified_as_search_instead_of_model_failure(self):
        self.runtime.researcher = Researcher(TavilySearch("fixture-secret", self.budget, "https://fixture.invalid"), self.model)
        q, post = self.run_question(side_effect=requests.ReadTimeout("fixture-secret"))
        details = q["error_details"]
        self.assertEqual((details["stage"], details["service"], details["timeout_seconds"]), ("searching", "search", 25))
        self.assertIn("搜索", q["error"])
        self.assertEqual(post.call_count, 1)

    def test_question_identifies_rate_limit_and_retains_http_status(self):
        q, post = self.run_question(return_value=Response({"error": {"code": "rate_limit_exceeded", "message": "fixture-secret"}}, 429))
        self.assertEqual(q["state"], "failed")
        self.assertEqual(q["error_details"]["http_status"], 429)
        self.assertEqual(q["error_details"]["provider_code"], "rate_limit_exceeded")
        self.assertIn("429", q["error"])
        self.assertNotIn("fixture-secret", json.dumps(q))
        self.assertEqual(post.call_count, 1)

    def test_default_wait_accepts_slow_complete_response_and_keeps_user_model_parameters(self):
        clock = [0]
        def respond(*_args, **_kwargs):
            clock[0] = 55
            return Response({"choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}]})
        with patch("requests.Session.post", side_effect=respond) as post, patch("live.workbench_runtime.time.monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(self.model.call("synthetic", {}), {"ok": True})
        self.assertEqual(post.call_args.kwargs["timeout"], (20, 180))
        self.assertEqual(post.call_args.kwargs["json"]["reasoning_effort"], "max")
        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 8192)
        self.assertNotIn("timeout_seconds", post.call_args.kwargs["json"])

    def test_length_finish_without_reaching_configured_cap_is_not_misreported(self):
        q, post = self.run_question(return_value=Response({"choices": [{"message": {"content": "{"}, "finish_reason": "length"}],
            "usage": {"completion_tokens": 10}}))
        self.assertIn("length", q["error"])
        self.assertNotIn("达到本次", q["error"])
        self.assertEqual(post.call_count, 1)

    def legacy_failure(self, *, capped=False, mixed=False):
        work = self.wb.claim(self.agent)
        with patch("live.workbench.time.time", return_value=1000):
            self.wb.stage(self.agent, work, "synthesizing")
            request_id = self.budget.reserve()
            self.budget.record(request_id, {"completion_tokens": 2048} if capped else {"state": "outcome_unknown", "error_kind": "ReadTimeout"})
            if mixed:
                request_id = self.budget.reserve()
                self.budget.record(request_id, {"state": "outcome_unknown", "error_kind": "SSLError"})
        with patch("live.workbench.time.time", return_value=1045.4):
            self.wb.fail(self.agent, work, "invalid_evidence_or_contract" if capped else "outcome_unknown",
                details=failure_details(ValueError("model output incomplete"), "synthesizing") if capped else None)
        return work["question"]["id"]

    def test_legacy_read_timeout_is_explained_without_mutation_or_new_calls(self):
        qid = self.legacy_failure()
        with self.wb.store.transaction() as db:
            before = self.wb.store._get(db, "wb_question", qid)
        with patch("requests.Session.post", side_effect=AssertionError("must not call provider")):
            q = self.wb.snapshot(self.owner, self.mid)["questions"][0]
        self.assertEqual(q["error_details"]["code"], "read_timeout")
        self.assertEqual(q["error_details"]["timeout_seconds"], 45)
        self.assertEqual(q["error_details"]["provenance"], "historical_ledger")
        with self.wb.store.transaction() as db:
            self.assertEqual(self.wb.store._get(db, "wb_question", qid), before)

    def test_mixed_historical_evidence_does_not_invent_a_timeout(self):
        self.legacy_failure(mixed=True)
        q = self.wb.snapshot(self.owner, self.mid)["questions"][0]
        self.assertEqual(q["error_details"]["provenance"], "legacy_incomplete")
        self.assertIn("未保留具体错误", q["error"])

    def test_historical_incomplete_output_reports_observed_token_usage(self):
        self.legacy_failure(capped=True)
        q = self.wb.snapshot(self.owner, self.mid)["questions"][0]
        self.assertIn("2048", q["error"])
        self.assertEqual(q["error_details"]["provenance"], "historical_ledger")

    def test_coordinator_timeout_keeps_reason_while_preserving_unread_input(self):
        self.wb.capabilities["debounce_seconds"] = 0
        self.wb.utterance(self.owner, self.mid, Utterance(text="请调研合成方案"))
        work = self.wb.coordinator_work(self.agent)
        with patch("requests.Session.post", side_effect=requests.ReadTimeout("fixture-secret")):
            self.runtime._coordinate(self.agent, work)
        m = self.wb.snapshot(self.owner, self.mid)["meeting"]
        self.assertEqual(m["coordinator_error_details"]["code"], "read_timeout")
        self.assertIn("超时", m["coordinator_error_message"])
        self.assertLess(m["coordinator_cursor"], m["semantic_seq"])


if __name__ == "__main__":
    unittest.main()
