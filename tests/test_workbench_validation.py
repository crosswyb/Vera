"""Bounded model correction and useful, private-data-free failure details."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live.provider_settings import ModelService
from live.source_reader import Researcher, read_sources
from live.workbench_contracts import NewMeeting, NewQuestion, ResearchAnswer
from live.workbench_runtime import OutcomeUnknown, TextModel, WorkbenchRuntime
from live_api import create_app


QUOTE = "A `SessionStart` hook reloads the saved instructions after compaction."
SOURCES = read_sources([{"url": "https://fixture.invalid/context", "content": (QUOTE + "\n") * 3}])
ANSWER = {"answer": "合成参考回答。", "claims": [{"text": "合成资料描述约束重载。", "source_id": "source-1", "quote": QUOTE}],
          "uncertainty": ["仅用于离线验证。"], "evidence_status": "supported",
          "brief": {"short_answer": "合成资料描述了压缩后重新加载约束。",
                    "key_points": [{"text": "来源描述使用钩子重载约束。", "kind": "source_claim", "claim_indices": [1]}],
                    "caveat": "这是离线合成资料，未验证实际效果。"}}


class OutputValidationTest(unittest.TestCase):
    def model(self):
        return TextModel(None, ModelService())

    def test_oversized_claims_are_corrected_using_the_actual_schema(self):
        invalid = {**ANSWER, "claims": ANSWER["claims"] * 14}
        model = self.model()
        with patch.object(model, "call", side_effect=[invalid, ANSWER]) as call:
            self.assertEqual(model.answer({}, {"title": "压缩后的约束"}, SOURCES), ANSWER)
        self.assertEqual(call.call_count, 2)
        self.assertIn('"maxItems": 12', call.call_args_list[0].args[0])
        correction = call.call_args_list[1].args[1]
        self.assertEqual(correction["sources"], call.call_args_list[0].args[1]["sources"])
        self.assertEqual(correction["quote_options"], call.call_args_list[0].args[1]["quote_options"])
        self.assertIn("claims", json.dumps(correction["validation_feedback"]))

    def test_paraphrased_quote_is_corrected_without_repeating_search(self):
        invalid = copy.deepcopy(ANSWER)
        invalid["claims"][0]["quote"] = QUOTE.replace("`", "")
        model = self.model()
        with patch.object(model, "call", side_effect=[invalid, ANSWER]) as call, \
                patch("live.source_reader.DeepSeekSearch.search", return_value=[{
                    "url": "https://fixture.invalid/context", "content": SOURCES[0]["text"]}]) as search:
            from live.source_reader import DeepSeekSearch
            result = Researcher(DeepSeekSearch("fixture", None), model).run({}, {"title": "约束重载"}, lambda _: None)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result["claims"][0]["quote"], QUOTE)
        self.assertEqual(call.call_args_list[1].args[1]["validation_feedback"][0]["code"], "quote_not_in_source")

    def test_repeated_invalid_output_stops_after_one_correction(self):
        invalid = {**ANSWER, "claims": ANSWER["claims"] * 14}
        model = self.model()
        with patch.object(model, "call", return_value=invalid) as call:
            with self.assertRaises(ValueError) as caught:
                model.answer({}, {"title": "约束"}, SOURCES)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(caught.exception.corrections, 1)

    def test_failure_diagnostics_never_include_unknown_field_names_or_values(self):
        from live.output_validation import failure_details
        private = "private-fixture-credential"
        with self.assertRaises(ValueError) as caught:
            ResearchAnswer.model_validate({**ANSWER, private: private})
        details = failure_details(caught.exception, "synthesizing")
        self.assertNotIn(private, json.dumps(details))
        self.assertEqual(details["issues"][0], {"field": "unknown_field", "code": "extra_forbidden"})

    def test_unknown_request_outcome_is_never_retried(self):
        model = self.model()
        with patch.object(model, "call", side_effect=OutcomeUnknown("unknown")) as call:
            with self.assertRaises(OutcomeUnknown):
                model.answer({}, {"title": "约束"}, SOURCES)
        self.assertEqual(call.call_count, 1)

    def test_model_selects_original_quote_by_id_without_retyping_its_format(self):
        model = self.model()
        selected = []
        def choose(system, content):
            option = content["quote_options"][0]
            selected.append(option)
            return {**ANSWER, "claims": [{"text": "合成资料描述约束重载。", "source_id": option["source_id"], "quote_id": option["id"]}]}
        with patch.object(model, "call", side_effect=choose) as call:
            result = model.answer({}, {"title": "约束重载"}, SOURCES)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result["claims"][0]["quote"], selected[0]["quote"])
        self.assertIn(result["claims"][0]["quote"], SOURCES[0]["text"])
        self.assertNotIn("quote_id", result["claims"][0])

    def test_unknown_quote_id_cannot_become_valid_evidence(self):
        model = self.model()
        invalid = {**ANSWER, "claims": [{"text": "合成佐证。", "source_id": "source-1", "quote_id": "invented"}]}
        with patch.object(model, "call", return_value=invalid) as call:
            with self.assertRaises(ValueError) as caught:
                model.answer({}, {"title": "约束"}, SOURCES)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(caught.exception.issues[0]["code"], "quote_id_not_found")

    def test_quote_selection_rejects_wrong_source_and_conflicting_literal(self):
        from live.source_reader import quote_catalog
        option = quote_catalog(SOURCES)[0]
        for source_id, literal, expected in (("source-2", "", "quote_id_wrong_source"),
                                              ("source-1", "This was not the selected quotation.", "quote_id_conflict")):
            with self.subTest(expected=expected):
                invalid = {**ANSWER, "claims": [{"text": "合成佐证。", "source_id": source_id,
                    "quote_id": option["id"], "quote": literal}]}
                model = self.model()
                with patch.object(model, "call", return_value=invalid), self.assertRaises(ValueError) as caught:
                    model.answer({}, {"title": "约束"}, SOURCES)
                self.assertEqual(caught.exception.issues[0]["code"], expected)

    def test_catalog_contains_only_bounded_continuous_original_spans(self):
        from live.source_reader import quote_catalog
        sources = read_sources([{"url": "https://fixture.invalid/long", "content": (QUOTE + "\n\n含格式的原文。\n") * 120}])
        options = quote_catalog(sources)
        self.assertGreater(len(options), 1)
        self.assertEqual(len({o["id"] for o in options}), len(options))
        for option in options:
            self.assertTrue(8 <= len(option["quote"]) <= 500)
            self.assertIn(option["quote"], sources[0]["text"])

    def test_all_bad_quotations_are_reported_in_one_correction(self):
        from live.source_reader import validate_result
        invalid = copy.deepcopy(ANSWER)
        invalid["claims"] = [{"text": "合成错误引用。", "source_id": "source-1", "quote": "This quote never appeared in the source."}] * 2
        with self.assertRaises(ValueError) as caught:
            validate_result({**invalid, "sources": SOURCES})
        self.assertEqual(len(caught.exception.issues), 2)

    def test_runtime_failure_keeps_validation_field_counts_and_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(root / "state.sqlite3", {"TESTING": True, "WORKBENCH_ROOT": root / "meetings",
                "WORKBENCH_CAPABILITIES": {"model_enabled": True, "search_enabled": True}})
            wb = app.extensions["workbench"]
            seed = wb.store.seed(fixture=True)
            owner, agent = [wb.store.authenticate(m["token"]) for m in (seed["members"][0], seed["members"][-1])]
            mid = wb.create(owner, NewMeeting(title="校验错误离线验证"))["id"]
            wb.question(owner, mid, NewQuestion(title="约束压缩", public_query="context compaction"))
            work = wb.claim(agent)
            class InvalidResearcher:
                def run(_, meeting, question, stage):
                    stage("reading")
                    stage("synthesizing")
                    ResearchAnswer.model_validate({**ANSWER, "claims": ANSWER["claims"] * 14})
            runtime = WorkbenchRuntime(wb, researcher=InvalidResearcher())
            try:
                runtime._research(agent, work)
            finally:
                runtime.close()
            snapshot = wb.snapshot(owner, mid)
            question = snapshot["questions"][0]
            self.assertEqual(question["state"], "failed")
            details = question.get("error_details", {})
            self.assertEqual(details.get("stage"), "synthesizing")
            self.assertEqual(details["issues"][0], {"field": "claims", "code": "too_long", "limit": 12, "actual": 14})
            self.assertIn("12", question["error"])
            self.assertIn("14", question["error"])
            failed = next(e for e in snapshot["activity"] if e["type"] == "research.failed")
            self.assertEqual(failed["payload"]["error_details"], details)


if __name__ == "__main__":
    unittest.main()
