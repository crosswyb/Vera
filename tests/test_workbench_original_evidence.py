import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from live.output_validation import OutputValidationError
from live.source_reader import Researcher
from live.workbench_contracts import Decisions, NewMeeting, NewQuestion, Utterance
from live_api import create_app


GB_TITLE = "GB/T 98765-2024 合成归档规范"
GB_URL = "https://openstd.samr.gov.cn/bzgk/gb/showGb?hcno=fixture"
GB_QUERY = "GB/T 98765-2024 3.2 条款 原文"
GB_QUESTION = "请查 GB/T 98765-2024 的 3.2 条款原文"
GB_QUOTE = "工作记录应保留形成时间、取证范围和关联对象，并允许复核此前的版本。"
GB_BODY = f"{GB_TITLE}\n3.1 适用范围\n本合成文件用于验证工作台，不是真实国家标准。\n3.2 记录要求\n{GB_QUOTE}\n3.3 截止时间\n项目组另行约定归档时间。"
POLICY_TITLE = "国务院办公厅关于印发《合成归档试点方案》的通知"
POLICY_ID = "国办发〔2024〕999号"
POLICY_QUOTE = "试点资料仅用于流程测试，组织方应在每次复核前确认文件的来源和适用范围。"
DEFINITION_QUOTE = "Web Annotation is a structured association between a body of information and a resource that it describes in this synthetic example."


def search_item(title=GB_TITLE, url=GB_URL, content=GB_BODY):
    return {"title": title, "url": url, "content": content, "date": "2024-03-01"}


def model_answer(quote=GB_QUOTE):
    return {"answer": "合成分析：这是验证公开查证流程的测试数据。", "evidence_status": "supported",
            "claims": [{"source_id": "source-1", "text": "合成材料说明记录要求。", "quote": quote}],
            "brief": {"short_answer": "合成规定要求保留时间、取证范围和关联对象。",
                      "key_points": [{"text": "保留形成时间。", "kind": "fact", "claim_indices": [1]}],
                      "caveat": "只适用于这个合成样例。"}}


class OriginalEvidenceTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        app = create_app(root / "state.db", {"TESTING": True, "WORKBENCH_ROOT": root / "out",
            "WORKBENCH_CAPABILITIES": {"model_enabled": True, "search_enabled": True, "debounce_seconds": 0}})
        self.wb = app.extensions["workbench"]
        users = self.wb.store.seed(fixture=True)["members"]
        self.owner, self.agent = [self.wb.store.authenticate(m["token"]) for m in (users[0], users[-1])]

    def research(self, items, title=GB_QUESTION, query=GB_QUERY, quote=GB_QUOTE, *, candidate=False):
        mid = self.wb.create(self.owner, NewMeeting(title="原文合成验收", data_scope="synthetic"))["id"]
        if candidate:
            self.wb.utterance(self.owner, mid, Utterance(text="上文提到了归档规范。", segment_id="context"))
            self.wb.utterance(self.owner, mid, Utterance(text=title, segment_id="request"))
            work = self.wb.coordinator_work(self.agent)
            refs = [{"segment_id": s["id"], "revision": s["revision"]} for s in work["segments"]]
            self.wb.commit_decisions(self.agent, work, Decisions.model_validate({"actions": [{
                "kind": "create_question", "title": title, "public_query": query, "source_refs": refs,
                "original_target": {"kind": "standard", "identifier": "GB/T 98765-2024", "section": "3.2"}}]}))
        else:
            self.wb.question(self.owner, mid, NewQuestion(title=title, public_query=query))
        work = self.wb.claim(self.agent)
        search = Mock(search=Mock(return_value=items))
        model = Mock(answer=Mock(return_value=model_answer(quote)))
        result = Researcher(search, model).run(work["meeting"], work["question"], lambda value: self.wb.stage(self.agent, work, value))
        search.search.assert_called_once_with(query)
        saved = self.wb.finish(self.agent, work, result)
        document = self.wb.artifact(self.owner, saved["artifact_id"])["content"]
        return saved, document, model

    def test_final_utterance_to_verified_standard_and_document(self):
        saved, document, model = self.research([search_item()], candidate=True)
        model.answer.assert_called_once()
        self.assertEqual(saved["state"], "completed")
        self.assertTrue(saved["anchor_ref"]["segment_id"].endswith(":request"))
        proof = saved["result"]["original_evidence"]
        self.assertEqual((proof["state"], proof["locator"], proof["quote"], proof["url"]), ("verified", "3.2", GB_QUOTE, GB_URL))
        self.assertIn("## 原文核验\n\n找到原文", document)
        self.assertIn(GB_QUOTE, document)

    def test_only_metadata_wrong_edition_reprint_and_empty(self):
        edition = GB_TITLE.replace("2024", "2023")
        for items, expected in [
            ([search_item(url="https://openstd.samr.gov.cn/bzgk/gb/newGbInfo?hcno=fixture", content=None)], "catalog_only"),
            ([search_item(title=edition, content=GB_BODY.replace(GB_TITLE, edition))], "mismatch"),
            ([search_item(url="https://fixture.invalid/a/reprint")], "secondary_only"),
            ([], "unverified"),
        ]:
            with self.subTest(expected=expected):
                saved, doc, model = self.research(items)
                model.answer.assert_not_called()
                proof = saved["result"]["original_evidence"]
                self.assertEqual(proof["state"], expected)
                self.assertEqual(saved["state"], "insufficient")
                self.assertNotIn(GB_QUOTE, doc)
                self.assertIn(proof["label"], doc)

    def test_exact_quote_from_another_section_cannot_be_used(self):
        saved, doc, _ = self.research([search_item()], quote="项目组另行约定归档时间。")
        self.assertEqual(saved["result"]["original_evidence"]["state"], "unverified")
        self.assertEqual(saved["result"]["claims"], [])
        self.assertNotIn("合成规定要求保留", doc)

    def test_invented_quote_fails_existing_exact_quote_gate(self):
        with self.assertRaises(OutputValidationError):
            self.research([search_item()], quote="这句话绝不可能出现在返回的正文里。")

    def test_policy_original_is_separate_from_interpretation(self):
        url = "https://app.www.gov.cn/govdata/gov/202412/01/99999/article.html"
        title = f"请定位国务院办公厅《合成归档试点方案》第三条原文，文号：{POLICY_ID}"
        query = f"{POLICY_ID} 第三条 原文"
        body = f"发文机关：国务院办公厅\n{POLICY_ID}\n第一条 本文件是为了验证流程提供的合成样张。\n　　第三条 {POLICY_QUOTE}"
        saved, _, _ = self.research([search_item(POLICY_TITLE, url, body)], title, query, POLICY_QUOTE)
        proof = saved["result"]["original_evidence"]
        self.assertEqual((proof["state"], proof["locator"], proof["publisher"]), ("verified", "第三条", "国务院办公厅"))
        for item in (search_item(POLICY_TITLE + "政策解读", url, body), search_item(POLICY_TITLE, url, body.replace("办公厅", ""))):
            saved, _, model = self.research([item], title, query, POLICY_QUOTE)
            model.answer.assert_not_called()
            self.assertNotEqual(saved["result"]["original_evidence"]["state"], "verified")

    def test_formal_definition_checks_publisher_and_term(self):
        saved, _, _ = self.research([search_item("Web Annotation Data Model", "https://www.w3.org/TR/annotation-model/",
            "Synthetic definition for local test.\n" + DEFINITION_QUOTE)], "W3C 对 Web Annotation 的官方定义是什么？",
            "W3C Web Annotation 定义", DEFINITION_QUOTE)
        self.assertEqual(saved["result"]["original_evidence"]["state"], "verified")

    def test_real_meeting_without_search_consent_never_claims_work(self):
        mid = self.wb.create(self.owner, NewMeeting(title="合成授权边界", data_scope="real", permissions={"model": True}))["id"]
        self.wb.question(self.owner, mid, NewQuestion(title=GB_QUESTION, public_query=GB_QUERY))
        self.assertIsNone(self.wb.claim(self.agent))
        q = self.wb.snapshot(self.owner, mid)["questions"][0]
        self.assertIn("尚未批准外部搜索", q["stage"])
        self.assertIsNone(q["result"])

    def test_generic_research_has_no_special_source_status(self):
        saved, document, model = self.research([search_item()], "讨论记录怎么保存？", "讨论记录 保存方法")
        self.assertNotIn("original_evidence", saved["result"])
        self.assertNotIn("## 原文核验", document)
        model.answer.assert_called_once()
