"""Meeting context, evidence-linked briefs, legacy answers and saved output."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from live.artifact_writer import render_document
from live.output_validation import OutputValidationError
from live.provider_settings import ModelService
from live.source_reader import Researcher, read_sources, validate_result
from live.workbench_contracts import AnswerBrief, NewMeeting, NewQuestion, ResearchDraft, SourceRef, Utterance
from live.workbench_runtime import TextModel
from live_api import create_app


QUOTE = "合成原文说明步骤和条件可以组织任务执行，具体部署能力需要另行验证。"
SOURCES = read_sources([{"url": "https://fixture.invalid/original", "content": QUOTE * 5}])
BRIEF = {"short_answer": "合成来源描述了按步骤和条件组织任务的方式，尚不能确认部署能力。",
         "key_points": [{"text": "来源描述步骤与条件，不代表产品经过独立验证。", "kind": "source_claim", "claim_indices": [1]},
                        {"text": "如需判断本机部署，应继续核对具体版本。", "kind": "inference", "claim_indices": [1]}],
         "caveat": "这是合成资料；是否支持本机部署尚未确认。"}
ANSWER = {"answer": "合成补充分析。\n\n具体部署条件仍需核对。", "brief": BRIEF,
          "claims": [{"text": "来源描述按步骤和条件组织任务。", "source_id": "source-1", "quote": QUOTE}],
          "uncertainty": ["本机部署能力尚未确认。"], "evidence_status": "mixed"}


class AnswerBriefTest(unittest.TestCase):
    def test_brief_is_saved_with_valid_evidence_links(self):
        result = validate_result({**ANSWER, "sources": SOURCES})
        self.assertEqual(result["brief"], BRIEF)
        self.assertEqual(result["claims"][0]["quote"], QUOTE)

    def test_missing_evidence_reference_is_rejected(self):
        value = copy.deepcopy(ANSWER)
        value["brief"]["key_points"][0]["claim_indices"] = [2]
        with self.assertRaises(OutputValidationError) as caught:
            validate_result({**value, "sources": SOURCES})
        self.assertEqual(caught.exception.issues[0]["code"], "claim_reference_not_found")

    def test_factual_point_requires_citation_and_inference_remains_labelled(self):
        value = copy.deepcopy(BRIEF)
        value["key_points"][0]["claim_indices"] = []
        with self.assertRaises(ValidationError):
            AnswerBrief.model_validate(value)
        value["key_points"][0]["kind"] = "inference"
        self.assertEqual(AnswerBrief.model_validate(value).key_points[0].kind, "inference")

    def test_brief_limits_are_enforced_by_schema(self):
        for changes in ({"short_answer": "字" * 81}, {"key_points": BRIEF["key_points"] * 2}, {"caveat": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                AnswerBrief.model_validate({**BRIEF, **changes})

    def test_claim_indices_are_strict_and_one_based(self):
        for references in ([0], [13], [True], ["1"], [1, 1]):
            value = copy.deepcopy(BRIEF)
            value["key_points"][0]["claim_indices"] = references
            with self.subTest(references=references), self.assertRaises(ValidationError):
                AnswerBrief.model_validate(value)

    def test_legacy_result_remains_readable_but_new_model_draft_requires_brief(self):
        legacy = {k: v for k, v in ANSWER.items() if k != "brief"}
        result = validate_result({**legacy, "sources": SOURCES})
        self.assertNotIn("brief", result)
        self.assertEqual(result["answer"], ANSWER["answer"])
        with self.assertRaises(ValidationError):
            ResearchDraft.model_validate(legacy)

    def test_answer_request_receives_only_pinned_context_and_current_date(self):
        context = [{"segment_id": "basis", "revision": 2, "text": "只考虑本机部署。", "is_basis": True, "truncated": False}]
        model = TextModel(None, ModelService())
        with patch.object(model, "call", return_value=ANSWER) as call:
            model.answer({"title": "合成路线评审", "unrelated": "不应发给模型"},
                         {"title": "部署能力", "discussion_context": context, "public_query": "workflow local deployment"}, SOURCES)
        system, payload = call.call_args.args
        self.assertEqual(payload["meeting_context"], {"meeting_title": "合成路线评审", "related_segments": context})
        self.assertIn("as_of", payload)
        self.assertEqual(payload["public_query"], "workflow local deployment")
        self.assertNotIn("不应发给模型", str(payload))
        self.assertIn("官方声称支持的功能未经独立验证时用source_claim", system)
        self.assertIn('"maxLength": 80', system)

    def test_bad_brief_reference_gets_one_correction_using_the_same_sources(self):
        model = TextModel(None, ModelService())
        bad = copy.deepcopy(ANSWER)
        bad["brief"]["key_points"][0]["claim_indices"] = [2]
        with patch.object(model, "call", side_effect=[bad, ANSWER]) as call:
            result = model.answer({}, {"title": "合成问题"}, SOURCES)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result["brief"], BRIEF)
        self.assertEqual(call.call_args.args[1]["sources"], call.call_args_list[0].args[1]["sources"])
        self.assertEqual(call.call_args.args[1]["quote_options"], call.call_args_list[0].args[1]["quote_options"])
        self.assertEqual(call.call_args.args[1]["validation_feedback"][0]["code"], "claim_reference_not_found")

    def test_source_text_is_sent_once_without_losing_original_quotes_or_metadata(self):
        model = TextModel(None, ModelService())
        with patch.object(model, "call", return_value=ANSWER) as call:
            result = model.answer({}, {"title": "合成问题"}, SOURCES)
        payload = call.call_args.args[1]
        self.assertNotIn("text", payload["sources"][0])
        self.assertEqual(payload["sources"][0]["content_hash"], SOURCES[0]["content_hash"])
        self.assertEqual(payload["sources"][0]["url"], SOURCES[0]["url"])
        self.assertEqual("".join(item["quote"] for item in payload["quote_options"]), SOURCES[0]["text"])
        self.assertEqual(result["claims"][0]["quote"], QUOTE)

    def test_no_original_text_returns_clear_insufficiency_without_model_call(self):
        class Search:
            def search(self, query):
                return [{"url": "https://fixture.invalid/snippet", "snippet": "不能作为正文"}]
        class Model:
            def answer(self, *args):
                raise AssertionError("no original text must not call answer model")
        result = Researcher(Search(), Model()).run({}, {"title": "合成问题"}, lambda _: None)
        checked = validate_result(result)
        self.assertEqual(checked["evidence_status"], "insufficient")
        self.assertFalse(checked["claims"])
        self.assertIn("无法回答", checked["brief"]["short_answer"])

    def test_markdown_starts_with_brief_and_retains_full_evidence(self):
        content = render_document({"mode": "live"}, {"id": "question", "title": "合成问题", "version": 1, "source_refs": []}, {**ANSWER, "sources": SOURCES})
        self.assertLess(content.index(BRIEF["short_answer"]), content.index("## 完整分析"))
        self.assertLess(content.index("## 完整分析"), content.index("## 关键依据"))
        self.assertLess(content.index("## 完整分析"), content.index(BRIEF['caveat']))
        self.assertIn("## 一句话结论", content)
        self.assertIn("（佐证 1）", content)
        self.assertIn(QUOTE, content)
        self.assertIn(SOURCES[0]["url"], content)


class AnswerContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        app = create_app(root / "state.sqlite3", {"TESTING": True, "WORKBENCH_ROOT": root / "meetings",
                         "WORKBENCH_CAPABILITIES": {"model_enabled": True, "search_enabled": True}})
        self.wb = app.extensions["workbench"]
        members = self.wb.store.seed(fixture=True)["members"]
        self.owner, self.agent = [self.wb.store.authenticate(m["token"]) for m in (members[0], members[-1])]
        self.mid = self.wb.create(self.owner, NewMeeting(title="合成讨论", data_scope="synthetic"))["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def say(self, text, segment, revision=1):
        self.wb.utterance(self.owner, self.mid, Utterance(text=text, segment_id=segment, revision=revision))

    def question(self, segments):
        refs = [SourceRef(segment_id=s["id"], revision=s["revision"]) for s in self.wb.snapshot(self.owner, self.mid)["segments"] if s["text"] in segments]
        return self.wb.question(self.owner, self.mid, NewQuestion(title="合成部署问题", source_refs=refs, public_query="local deployment"))

    def test_context_includes_basis_and_neighbours_without_unrelated_meeting_or_future_turns(self):
        self.say("更早的无关内容", "early")
        self.say("当前比较两条路线", "background-1")
        self.say("只考虑本机部署", "background-2")
        self.say("请查一下这个条件", "basis")
        self.say("需要保留版本范围", "after")
        self.say("接下来另一个议题", "other")
        q = self.question(["请查一下这个条件"])
        work = self.wb.claim(self.agent)
        context = work["question"]["discussion_context"]
        self.assertEqual([s["text"] for s in context], ["当前比较两条路线", "只考虑本机部署", "请查一下这个条件", "需要保留版本范围"])
        self.assertEqual([s["text"] for s in context if s["is_basis"]], ["请查一下这个条件"])
        stored = next(item for item in self.wb.snapshot(self.owner, self.mid)["questions"] if item["id"] == q["id"])
        self.assertNotIn("discussion_context", stored)

    def test_context_uses_latest_final_revision(self):
        self.say("云端部署", "basis")
        self.say("只验证本机部署", "basis", revision=2)
        self.question(["只验证本机部署"])
        context = self.wb.claim(self.agent)["question"]["discussion_context"]
        self.assertEqual(len(context), 1)
        self.assertEqual(context[0]["revision"], 2)
        self.assertEqual(context[0]["text"], "只验证本机部署")

    def test_large_context_keeps_all_basis_ids_and_marks_truncation(self):
        statements = []
        for i in range(12):
            value = str(i) + "字" * 4490
            statements.append(value)
            self.say(value, str(i))
        self.question(statements)
        context = self.wb.claim(self.agent)["question"]["discussion_context"]
        self.assertEqual(len(context), 12)
        self.assertTrue(all(s["is_basis"] and s["truncated"] for s in context))
        self.assertLessEqual(sum(len(s["text"]) for s in context), 18000)

    def test_manual_question_does_not_send_an_arbitrary_meeting_window(self):
        self.say("不相关的讨论", "other")
        self.question([])
        self.assertEqual(self.wb.claim(self.agent)["question"]["discussion_context"], [])


if __name__ == "__main__":
    unittest.main()
