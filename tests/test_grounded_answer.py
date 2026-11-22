import unittest
from unittest.mock import Mock, patch

from live.provider_settings import ModelService, SearchService, Services
from live.source_reader import Researcher, is_grounded_explanation
from live.workbench_runtime import OutcomeUnknown, TextModel, WorkbenchRuntime


QUOTE = '为什么要把两笔钱折算到同一个时点？'
QUESTION = {'title': '现金流为什么要折现？', 'grounding_quote': QUOTE, 'evidence_need': 'explanation',
            'discussion_context': [{'is_basis': True, 'text': QUOTE + '另外，请提供最新油价的官方出处。'}]}
ANSWER = {'route': 'direct', 'short_answer': '折现使不同时点的现金流可以比较。',
          'answer': '因为资金有时间价值，可用相同的折现率把不同时点的金额折算到同一个时点再比较。'}


class GroundedAnswerTest(unittest.TestCase):
    def test_model_only_configuration_answers_once_with_adjacent_source_request(self):
        config = Services(model=ModelService(base_url='http://localhost:9999'), search=SearchService(enabled=False))
        with patch('live.workbench_runtime.ModelBudget'):
            model, engine = WorkbenchRuntime.prepare_configuration(None, config)
        self.addCleanup(model.close)
        self.assertIsNone(engine.search)
        self.assertTrue(is_grounded_explanation(QUESTION))
        with patch.object(model, 'call', return_value=ANSWER) as call:
            result = engine.run({'data_scope': 'real'}, QUESTION, lambda _: None)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[1]['grounding_quote'], QUOTE)
        self.assertEqual(result['answer'], ANSWER['answer'])
        self.assertEqual(result['evidence_status'], 'model_explanation')
        self.assertEqual((result['sources'], result['claims']), ([], []))

    def test_conflicting_grounded_source_or_status_request_requires_web_permission(self):
        for changed in ({'grounding_quote': '有官方出处吗？'}, {'title': '该政策仍然有效吗？'},
                        {'original_target': {'kind': 'standard'}}, {'evidence_need': 'public'}):
            question = {**QUESTION, **changed}
            with self.subTest(changed=changed):
                self.assertFalse(is_grounded_explanation(question))
                model, search = Mock(), Mock()
                with self.assertRaisesRegex(ValueError, '公开检索词'):
                    Researcher(search, model, choose_search=model.choose_search).run({'data_scope': 'real'}, question, lambda _: None)
                model.explain.assert_not_called()
                model.choose_search.assert_not_called()
                search.search.assert_not_called()

    def test_legacy_explanation_keeps_model_routing(self):
        model = TextModel(None, ModelService())
        self.addCleanup(model.close)
        with patch.object(model, 'call', return_value=ANSWER) as call:
            result = Researcher(None, model, choose_search=model.choose_search).run({}, {
                'title': QUESTION['title'], 'evidence_need': 'explanation'}, lambda _: None)
        self.assertEqual(result['evidence_status'], 'model_explanation')
        self.assertNotIn('grounding_quote', call.call_args.args[1])

    def test_direct_timeout_retries_once_without_switching_to_search(self):
        model = TextModel(None, ModelService())
        self.addCleanup(model.close)
        timeout = OutcomeUnknown(diagnostic={'code': 'response_deadline', 'service': 'model'})
        for second in (ANSWER, timeout):
            stage, search = Mock(), Mock()
            engine = Researcher(search, model, choose_search=model.choose_search)
            with self.subTest(second_timeout=second is timeout), patch.object(model, 'call', side_effect=[timeout, second]) as call:
                if second is timeout:
                    with self.assertRaises(OutcomeUnknown):
                        engine.run({}, QUESTION, stage)
                else:
                    self.assertEqual(engine.run({}, QUESTION, stage)['answer'], ANSWER['answer'])
                self.assertEqual(call.call_count, 2)
                self.assertEqual(sum(c.args[0] == 'model_timeout_retry' for c in stage.progress.call_args_list), 1)
                search.search.assert_not_called()
