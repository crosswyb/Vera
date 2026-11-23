import unittest
from unittest.mock import Mock

from live.source_reader import Researcher, requires_web_evidence


class QueryRoutingRepairsTest(unittest.TestCase):
    def setUp(self):
        self.search, self.model = Mock(), Mock()
        self.search.search.return_value = []
        self.engine = Researcher(self.search, self.model, choose_search=self.model.choose_search)
        self.meeting = {'data_scope': 'real'}

    def test_internal_status_stays_local_with_missing_or_incorrect_intent(self):
        for title in ['我们内部系统目前支持哪些审批？', '我们项目最新进展是什么？']:
            for need in [None, 'explanation', 'internal']:
                with self.subTest(title=title, need=need):
                    q = {'title': title, 'grounding_quote': title, 'evidence_need': need, 'public_query': '审批'}
                    result = self.engine.run(self.meeting, q, lambda _: None)
                    self.assertEqual(result['evidence_status'], 'insufficient')
        self.search.search.assert_not_called()
        self.model.choose_search.assert_not_called()
        self.model.explain.assert_not_called()

    def test_current_facts_and_explicit_public_sources_still_require_search(self):
        for title in ['今年原油价格走势', 'Python 3.9 目前支持哪些版本？', '本项目用的 Python 目前还在维护吗？',
                      '本项目请核对官方文档', '最新油价是多少？']:
            with self.subTest(title=title):
                self.assertTrue(requires_web_evidence({'title': title}))
        self.assertFalse(requires_web_evidence({'title': '最新的一条讨论说了什么？'}))

    def test_explicit_search_overrides_internal_or_explanation_and_sends_only_approved_query(self):
        for need in ('internal', 'explanation', None):
            q = {'web_search_requested': True, 'evidence_need': need, 'title': '我们项目最新进展是什么？',
                 'grounding_quote': '私有客户名', 'public_query': '行业公开资料'}
            with self.subTest(need=need):
                self.engine.run(self.meeting, q, lambda _: None)
                self.search.search.assert_called_once_with('行业公开资料')
                self.search.search.reset_mock()
                with self.assertRaisesRegex(ValueError, '公开检索词'):
                    self.engine.run({'data_scope': 'synthetic'}, {**q, 'public_query': ''}, lambda _: None)
                self.search.search.assert_not_called()
        self.model.choose_search.assert_not_called()
        self.model.explain.assert_not_called()

    def test_public_evidence_cannot_verify_internal_status(self):
        quote = '公开文档只说明软件的通用能力，具体项目是否已经完成上线需要另行核对。'
        self.search.search.return_value = [{'url': 'https://fixture.invalid/doc', 'content': quote * 5}]
        self.model.answer.return_value = {'answer': quote, 'claims': [{'text': quote, 'source_id': 'source-1',
                                                                      'quote': quote}], 'uncertainty': [],
                                          'evidence_status': 'supported'}
        question = {'title': '我们内部系统目前支持哪些审批？', 'public_query': '公开审批软件功能',
                    'evidence_need': 'internal', 'web_search_requested': True}
        result = self.engine.run(self.meeting, question, lambda _: None)
        self.assertEqual(result['evidence_status'], 'insufficient')
        self.assertIn('内部原始记录', result['uncertainty'][0])
        self.assertEqual(len(result['sources']), 1)
        self.assertEqual(self.model.answer.call_args.args[1]['evidence_need'], 'mixed')

    def test_relative_period_normalization_preserves_compound_identifiers(self):
        for code in ['ISO 9001:2015', 'ISO 9001：2015', 'ISO 9001: 2015', 'ISO 9001: 2015认证',
                     'ISO/IEC 27001:2022']:
            q = {'title': '今年行业认证趋势', 'public_query': code + ' 2025年 认证', 'public_query_origin': 'model',
                 'evidence_need': 'public', 'discussion_context': [{'is_basis': True, 'text': '今年认证趋势如何？'}]}
            with self.subTest(code=code):
                self.engine.run(self.meeting, q, lambda _: None)
                self.search.search.assert_called_once_with(code + ' 认证')
                self.search.search.reset_mock()

    def test_generated_search_terms_with_transaction_amount_or_restricted_context_are_held(self):
        base = {'title': '公开技术资料', 'evidence_need': 'public', 'public_query_origin': 'model'}
        for question in ({**base, 'public_query': '设备项目 1200万元交付'},
                         {**base, 'public_query': '设备项目 公开资料', 'restricted_context': True}):
            with self.subTest(question=question), self.assertRaisesRegex(ValueError, '公开检索词'):
                self.engine.run(self.meeting, question, lambda _: None)
        self.search.search.assert_not_called()
