"""Meeting context, evidence-linked briefs, legacy answers and saved output."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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


LONG_CONCLUSION = "长结论需要保留必要条件及其适用范围。" * 12


class AnswerBriefTest(unittest.TestCase):
    def test_live_short_answer_is_delivered_before_full_answer_and_invalid_quote_is_omitted(self):
        class ReplyModel:
            supports_research_progress = True
            def quick_answer(self, *_args, **_kwargs):
                return short
            def answer(self, *_args, **_kwargs):
                self.progress_at_full_answer = [call.args[0] for call in stage.progress.call_args_list]
                return ANSWER

        for valid in (True, False):
            with self.subTest(valid=valid):
                search, stage = Mock(), Mock()
                search.search.return_value = [{'url': SOURCES[0]['url'], 'content': QUOTE * 5}]
                short = copy.deepcopy(ANSWER)
                if not valid:
                    short['claims'][0]['quote'] = '这段话从未出现在来源中。'
                model = ReplyModel()
                result = Researcher(search, model).run(
                    {'data_scope': 'real', 'state': 'active'},
                    {'title': '步骤怎么组织？', 'public_query': '实施步骤', 'evidence_need': 'public'}, stage)
                self.assertEqual('verified_brief' in model.progress_at_full_answer, valid)
                self.assertEqual(result['answer'], ANSWER['answer'])
                search.search.assert_called_once()

    def test_answer_deadline_reuses_read_sources_and_retries_only_once(self):
        from unittest.mock import Mock
        from live.workbench_runtime import OutcomeUnknown
        timeout = OutcomeUnknown(diagnostic={'code': 'response_deadline', 'service': 'model'})
        for second_timeout in (False, True):
            with self.subTest(second_timeout=second_timeout):
                model, search, stage = Mock(), Mock(), Mock()
                search.search.return_value = [{'url': SOURCES[0]['url'], 'content': QUOTE * 5}]
                model.answer.side_effect = [timeout, timeout if second_timeout else ANSWER]
                run = lambda: Researcher(search, model).run({}, {'title': '行情', 'public_query': '行情'}, stage)
                if second_timeout:
                    with self.assertRaises(OutcomeUnknown):
                        run()
                else:
                    self.assertEqual(run()['evidence_status'], 'mixed')
                self.assertEqual(model.answer.call_count, 2)
                self.assertIs(model.answer.call_args_list[0].args[2], model.answer.call_args_list[1].args[2])
                search.search.assert_called_once_with('行情')
                self.assertEqual(sum(c.args[0] == 'model_timeout_retry' for c in stage.progress.call_args_list), 1)

    def test_industry_questions_from_meeting_require_search_even_for_legacy_cards(self):
        from unittest.mock import Mock
        for title in ('当前炼油加工利润是否受原油与产品两端挤压', '大宗商品企业用时间错配与套保管理价格波动的常见做法',
                      '服务器硬件毛利率与内存涨价影响', '企业利润管理采用量化模型与传统信息化的对比',
                      '流程工业设备状态推演与故障诊断的行业做法'):
            with self.subTest(title=title):
                search = Mock()
                search.search.return_value = []
                engine = Researcher(search, None, choose_search=Mock(side_effect=AssertionError('unexpected direct route')))
                result = engine.run({'data_scope': 'real'}, {'title': title, 'public_query': '允许外发的检索词'}, lambda _: None)
                search.search.assert_called_once_with('允许外发的检索词')
                self.assertEqual(result['evidence_status'], 'insufficient')

    def test_relative_year_in_import_does_not_anchor_search_to_invented_year(self):
        from unittest.mock import Mock
        search = Mock()
        search.search.return_value = []
        q = {'title': '行情', 'evidence_need': 'public', 'public_query': '内存 2025年 价格走势',
             'public_query_origin': 'model', 'discussion_context': [{'is_basis': True, 'text': '今年涨了吗？'}]}
        engine = Researcher(search, None)
        engine.run({'data_scope': 'real'}, q, lambda _: None)
        search.search.assert_called_once_with('内存 价格走势')
        engine.run({'data_scope': 'real'}, {**q, 'public_query_origin': 'owner'}, lambda _: None)
        self.assertEqual(search.search.call_args.args[0], '内存 2025年 价格走势')

    def test_mixed_intent_bounds_supported_result_and_internal_intent_uses_no_services(self):
        from unittest.mock import Mock
        search, model = Mock(), Mock()
        search.search.return_value = [{'url': SOURCES[0]['url'], 'content': QUOTE * 5}]
        model.answer.return_value = {**ANSWER, 'evidence_status': 'supported'}
        engine = Researcher(search, model, choose_search=Mock(side_effect=AssertionError('unexpected routing call')))
        result = engine.run({}, {'title': '这个判断是否成立', 'evidence_need': 'mixed', 'public_query': '公开行情'}, lambda _: None)
        self.assertEqual(result['evidence_status'], 'mixed')
        self.assertIn('内部', result['uncertainty'][0])
        result = engine.run({}, {'title': '本公司实际采购成本是多少', 'evidence_need': 'internal'}, lambda _: None)
        self.assertEqual(result['evidence_status'], 'insufficient')
        search.search.assert_called_once_with('公开行情')

    def test_mandatory_search_bypasses_direct_model_and_only_sends_public_query(self):
        from unittest.mock import Mock
        for title, basis in [("Python 3.9 现在还在官方安全维护期内吗？", ""), ("请给出原文链接", ""),
                             ("这项标准是否有效？", ""), ("维护周期", "请注明出处")]:
            with self.subTest(title=title):
                search = Mock()
                search.search.return_value = []
                route = Mock(side_effect=AssertionError("mandatory search bypasses model routing"))
                q = {"title": title, "public_query": "approved query", "discussion_context": [{"is_basis": True, "text": basis}]}
                result = Researcher(search, None, choose_search=route).run({}, q, lambda _: None)
                search.search.assert_called_once_with("approved query")
                self.assertEqual(result["evidence_status"], "insufficient")

    def test_neighbouring_source_request_does_not_force_an_unrelated_calculation(self):
        from live.source_reader import requires_web_evidence
        self.assertFalse(requires_web_evidence({"title": "请现在计算120乘2.5", "discussion_context": [
            {"is_basis": False, "text": "另一个问题请提供官方出处"}]}))

    def test_required_evidence_does_not_publish_an_uncited_model_explanation(self):
        from unittest.mock import Mock
        search, model = Mock(), Mock()
        search.search.return_value = [{"url": "https://fixture.invalid/status", "content": QUOTE * 5}]
        model.answer.return_value = {"answer": "没有佐证的结论", "claims": [], "evidence_status": "model_explanation"}
        result = Researcher(search, model).run({}, {"title": "现在还支持吗？"}, lambda _: None)
        self.assertEqual(result["evidence_status"], "insufficient")
        self.assertNotIn("没有佐证的结论", str(result))

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
        for changes in ({"short_answer": ""}, {"key_points": BRIEF["key_points"] * 2}, {"caveat": ""}):
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
        self.assertNotIn("maxLength", AnswerBrief.model_json_schema()["properties"]["short_answer"])

    def test_sourced_long_conclusion_is_published_without_correction(self):
        model = TextModel(None, ModelService())
        output = {**ANSWER, "brief": {**BRIEF, "short_answer": LONG_CONCLUSION}}
        with patch.object(model, "call", return_value=output) as call:
            result = model.answer({}, {"title": "合成问题"}, SOURCES)
        call.assert_called_once()
        self.assertEqual(result["brief"]["short_answer"], LONG_CONCLUSION)

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
        self.assertIn("## 结论摘要", content)
        self.assertIn("（佐证 1）", content)
        self.assertIn(QUOTE, content)
        self.assertIn(SOURCES[0]["url"], content)


class AnswerContextTest(unittest.TestCase):
    def test_model_direct_decision_publishes_without_search_or_citations(self):
        self.question([])
        work = self.wb.claim(self.agent)
        model = TextModel(None, ModelService())
        engine = Researcher(None, model, choose_search=model.choose_search)
        with patch.object(model, "call", return_value={"route": "direct", "short_answer": LONG_CONCLUSION, "answer": "120 × 2.5 = 300件。"}) as call:
            result = engine.run(work["meeting"], work["question"], lambda _: None)
        self.wb.finish(self.agent, work, result)
        saved = self.wb.snapshot(self.owner, self.mid)["questions"][0]
        self.assertEqual(call.call_count, 1)
        self.assertEqual(saved["state"], "completed")
        self.assertEqual(saved["result"]["evidence_status"], "model_explanation")
        self.assertEqual(saved["result"]["sources"], [])
        self.assertEqual(saved["result"]["brief"]["short_answer"], LONG_CONCLUSION)

    def test_model_search_decision_uses_only_approved_query(self):
        from unittest.mock import Mock
        search = Mock()
        search.search.return_value = []
        model = TextModel(None, ModelService())
        engine = Researcher(search, model, choose_search=model.choose_search)
        with patch.object(model, "call", return_value={"route": "web_search"}):
            result = engine.run({}, {"title": "内部讨论的问题", "public_query": "公开检索词"}, lambda _: None)
        search.search.assert_called_once_with("公开检索词")
        self.assertEqual(result["evidence_status"], "insufficient")

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
