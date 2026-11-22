import unittest

from tests import test_detection_recovery as recovery


class DetectionGapRepairsTest(unittest.TestCase):
    setUp = recovery.DetectionRecoveryTest.setUp
    say = recovery.DetectionRecoveryTest.say
    work = recovery.DetectionRecoveryTest.work
    action = recovery.DetectionRecoveryTest.action
    run_work = recovery.DetectionRecoveryTest.run_work
    snapshot = recovery.DetectionRecoveryTest.snapshot

    def next_task(self):
        return self.wb.detection_recovery_work(self.agent)

    def start_scan(self):
        self.say('问题一？问题二？问题三？问题四？问题五？')
        main = self.work()
        self.run_work(main, [self.action(main, str(n + 1), n) for n in range(4)])
        return self.next_task()

    def test_malformed_refs_do_not_rollback_valid_question_or_block_new_input(self):
        self.say('成本？利润？')
        work = self.work()
        bad = self.action(work, '利润', 1)
        ref, = bad['source_refs']
        actions = [{**bad, 'source_refs': [{**ref, field: value}]}
                   for field, value in [('segment_id', []), ('revision', {})]]
        self.run_work(work, [self.action(work, '成本'), *actions])
        snap = self.snapshot()
        self.assertEqual([q['title'] for q in snap['questions']], ['成本'])
        self.assertEqual(snap['meeting']['coordinator_cursor'], work['cursor'])
        self.assertEqual([t['kind'] for t in snap['detection_tasks']], ['scan'])
        self.say('现金流？')
        fresh = self.work()
        self.run_work(fresh, [self.action(fresh, '现金流')])
        for _ in range(2):
            self.run_work(self.next_task(), [])
        review, = self.snapshot()['detection_tasks']
        self.assertEqual(review['state'], 'needs_review')
        self.wb.retry_detection_task(self.owner, self.mid, review['id'])
        retry = self.next_task()
        self.run_work(retry, [self.action(retry, '利润', 1)])
        self.assertTrue(self.snapshot()['automation_progress']['complete'])
        self.assertEqual([q['title'] for q in self.snapshot()['questions']], ['成本', '现金流', '利润'])

    def test_located_scan_failure_gets_one_correction_and_manual_retry(self):
        scan = self.start_scan()
        bad = {**self.action(scan, '5', 4), 'grounding_quote_id': 'missing'}
        self.run_work(scan, [bad])
        repair = self.next_task()
        self.assertEqual(repair['detection_task']['kind'], 'correction')
        self.assertEqual(repair['detection_task']['proposal'], bad)
        self.run_work(repair, [])
        review, = self.snapshot()['detection_tasks']
        self.assertEqual((review['kind'], review['state'], review['attempts']), ('correction', 'needs_review', 1))
        self.assertIsNone(self.next_task())
        self.wb.retry_detection_task(self.owner, self.mid, review['id'])
        repair = self.next_task()
        self.run_work(repair, [self.action(repair, '5', 4)])
        self.assertTrue(self.snapshot()['automation_progress']['complete'])
        self.assertEqual(len(self.snapshot()['questions']), 5)

    def test_only_one_window_must_confirm_an_unlocated_main_proposal(self):
        for n in range(8):
            self.say(f'第{n}个问题？', f's{n}')
        work = self.work()
        bad = {**self.action(work, '最后的问题', 7), 'source_refs': [{'segment_id': {}, 'revision': 1}]}
        self.run_work(work, [bad])
        first = self.next_task()
        self.assertEqual(len(first['detection_task']['source_refs']), 4)
        self.run_work(first, [])
        self.assertFalse(self.snapshot()['automation_progress']['complete'])
        second = self.next_task()
        self.assertNotEqual(first['detection_task']['id'], second['detection_task']['id'])
        self.run_work(second, [self.action(second, '最后的问题', 3)])
        self.assertTrue(self.snapshot()['automation_progress']['complete'])
        self.assertIsNone(self.next_task())

    def test_changed_public_target_cannot_close_an_unlocated_proposal(self):
        scan = self.start_scan()
        question = {**self.action(scan, '标准原文', 4), 'evidence_need': 'public',
                    'external_fact': '标准内容', 'public_query': '标准章节',
                    'original_target': {'kind': 'standard', 'identifier': '标准一'}}
        self.run_work(scan, [{**question, 'source_refs': [{'segment_id': {}, 'revision': 1}]}])
        retry = self.next_task()
        self.run_work(retry, [{**question, 'original_target': {'kind': 'standard', 'identifier': '标准二'}}])
        review, = self.snapshot()['detection_tasks']
        self.assertEqual(review['state'], 'needs_review')
        self.assertFalse(self.snapshot()['automation_progress']['complete'])
        self.assertNotIn('标准原文', [q['title'] for q in self.snapshot()['questions']])
        self.wb.retry_detection_task(self.owner, self.mid, review['id'])
        self.run_work(self.next_task(), [question])
        fixed, = [q for q in self.snapshot()['questions'] if q['title'] == '标准原文']
        self.assertEqual(fixed['original_target']['identifier'], '标准一')
        self.assertTrue(self.snapshot()['automation_progress']['complete'])

    def test_malformed_intent_fields_with_valid_refs_still_get_targeted_repair(self):
        scan = self.start_scan()
        question = self.action(scan, '5', 4)
        self.run_work(scan, [{**question, 'kind': [], 'evidence_need': {}}])
        repair = self.next_task()
        self.assertEqual(repair['detection_task']['kind'], 'correction')
        self.run_work(repair, [question])
        self.assertTrue(self.snapshot()['automation_progress']['complete'])

    def test_malformed_repeat_cannot_bypass_the_scan_overlap_guard(self):
        scan = self.start_scan()
        question = self.action(scan, '改写第一问', 0)
        self.run_work(scan, [question])
        self.run_work(self.next_task(), [{**question, 'grounding_quote_id': 'missing'}])
        review, = self.snapshot()['detection_tasks']
        self.assertEqual((review['kind'], review['state']), ('scan', 'needs_review'))
        self.assertIsNone(self.next_task())
        self.assertEqual(len(self.snapshot()['questions']), 4)

    def test_unlocated_scan_failure_and_overlap_survive_empty_retry(self):
        scan = self.start_scan()
        unlocated = {**self.action(scan, '5', 4), 'source_refs': [{'segment_id': [], 'revision': 1}]}
        self.run_work(scan, [unlocated, self.action(scan, '改写第一问', 0)])
        scan = self.next_task()
        self.assertEqual(scan['detection_task']['kind'], 'scan')
        self.run_work(scan, [])
        review, = self.snapshot()['detection_tasks']
        self.assertFalse(self.snapshot()['automation_progress']['complete'])
        self.assertEqual(review['state'], 'needs_review')
        self.assertIn('overlapping_proposal', [i['code'] for i in review['issues']])
        self.assertIsNone(self.next_task())
        self.wb.retry_detection_task(self.owner, self.mid, review['id'])
        retry = self.next_task()
        self.run_work(retry, [self.action(retry, '改写第一问', 0), self.action(retry, '5', 4)])
        review, = self.snapshot()['detection_tasks']
        self.assertEqual([i['code'] for i in review['issues']], ['overlapping_proposal'])
        self.assertEqual(len(self.snapshot()['questions']), 5)
        self.assertIsNone(self.next_task())
