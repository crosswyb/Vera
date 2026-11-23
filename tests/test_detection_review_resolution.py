"""Recovery of malformed question references and explicit dismissal of gaps."""

import json
import unittest

from live.detection import parse_detection, quote_options
from live.store import LiveError
from live.workbench_contracts import EditQuestion, MeetingPermissions, NewMeeting, QuestionAction
from live_api import create_app
from tests import test_detection_recovery as recovery


class DetectionReviewResolutionTest(unittest.TestCase):
    setUp = recovery.DetectionRecoveryTest.setUp
    say = recovery.DetectionRecoveryTest.say
    work = recovery.DetectionRecoveryTest.work
    action = recovery.DetectionRecoveryTest.action
    run_work = recovery.DetectionRecoveryTest.run_work
    snapshot = recovery.DetectionRecoveryTest.snapshot
    bad_batch = recovery.DetectionRecoveryTest.bad_batch

    def followup(self):
        self.say('合成材料的范围是什么？', 'original')
        work = self.work()
        self.run_work(work, [self.action(work, '合成材料范围')])
        question, = self.snapshot()['questions']
        self.say('继续补充合成材料的范围。', 'followup')
        work = self.work()
        action = {**self.action(work, question['title']), 'kind': 'revise_question',
            'question_id': question['id'], 'expected_version': question['version'], 'scope_changed': False}
        return question, work, action

    def legacy_review(self, work, proposal):
        # A persisted pre-upgrade task must retain its ID and original proposal.
        self.run_work(work, [])
        with self.wb.store.transaction() as db:
            meeting = self.wb._meeting(db, self.owner, self.mid, True)
            self.wb._save_detection_task(db, meeting, work, 'correction', [work['segments'][-1]],
                proposal=proposal, issues=[{'field': 'actions.0', 'code': 'context_stale'}])
            task = self.wb._detection_tasks(db, self.mid)[0]
            task.update(state='needs_review', attempts=1)
            self.wb.store._put(db, 'wb_detection_task', task['id'], task)
            self.wb._save(db, meeting)
        return task

    def review(self):
        self.bad_batch()
        work = self.wb.detection_recovery_work(self.agent)
        self.run_work(work, [])
        return self.snapshot()['detection_tasks'][0]

    def test_main_detection_repairs_full_suffix_without_recreating_question(self):
        question, work, action = self.followup()
        self.run_work(work, [{**action, 'question_id': question['id'].removeprefix('question-')}])
        snap = self.snapshot()
        self.assertEqual(len(snap['questions']), 1)
        self.assertEqual(snap['questions'][0]['id'], question['id'])
        self.assertEqual(snap['questions'][0]['version'], question['version'])
        self.assertEqual(snap['questions'][0]['last_mention_refs'], action['source_refs'])
        self.assertFalse(snap['detection_tasks'])

    def test_old_review_accepts_canonical_or_bare_response_and_keeps_task_identity(self):
        for canonical_response in (True, False):
            with self.subTest(canonical_response=canonical_response):
                self.mid = self.wb.create(self.owner, NewMeeting(title='旧告警恢复', data_scope='synthetic'))['id']
                question, work, action = self.followup()
                original = {**action, 'question_id': question['id'].removeprefix('question-')}
                task = self.legacy_review(work, original)
                snap_task, = self.snapshot()['detection_tasks']
                self.assertEqual(snap_task['proposal_title'], question['title'])
                self.assertEqual(snap_task['question_id'], question['id'])
                self.wb.retry_detection_task(self.owner, self.mid, task['id'])
                retry = self.wb.detection_recovery_work(self.agent)
                self.run_work(retry, [action if canonical_response else original])
                content = self.model.call.call_args.args[1]
                self.assertEqual(content['previous_proposal']['question_id'], question['id'])
                snap = self.snapshot()
                self.assertTrue(snap['automation_progress']['complete'])
                self.assertFalse(snap['detection_tasks'])
                self.assertEqual(len(snap['questions']), 1)
                with self.wb.store.transaction() as db:
                    stored, = self.wb._detection_tasks(db, self.mid)
                    self.assertEqual((stored['id'], stored['state']), (task['id'], 'completed'))
                    self.assertEqual(stored['proposal'], original)
                self.wb.commit_detection(self.agent, retry, parse_detection({'actions': [action]}, retry,
                    quote_options(retry['segments'], retry['meeting']['coordinator_cursor'])))
                self.assertEqual(len(self.snapshot()['questions']), 1)

    def test_alias_cannot_refresh_version_guess_partial_id_or_cross_meetings(self):
        question, work, action = self.followup()
        for changes, code in [
            ({'question_id': question['id'].removeprefix('question-'), 'expected_version': question['version'] + 1}, 'question_version_stale'),
            ({'question_id': question['id'][-8:]}, 'question_not_found'),
        ]:
            with self.subTest(changes=changes):
                batch = parse_detection({'actions': [{**action, **changes}]}, work, quote_options(work['segments'], 0))
                self.assertFalse(batch.actions)
                self.assertEqual(batch.failures[0]['issues'][0]['code'], code)
        foreign_work = {**work, 'questions': []}
        batch = parse_detection({'actions': [action]}, foreign_work, quote_options(work['segments'], 0))
        self.assertFalse(batch.actions)
        self.assertEqual(batch.failures[0]['issues'][0]['code'], 'question_not_found')

    def test_reference_repair_does_not_allow_changing_the_question_or_scope(self):
        question, work, action = self.followup()
        previous = {**action, 'question_id': question['id'].removeprefix('question-')}
        work = {**work, 'detection_task': {'kind': 'correction', 'proposal': previous}}
        for changes in ({'title': '另一个问题'}, {'scope_changed': True}, {'expected_version': question['version'] + 1}):
            with self.subTest(changes=changes):
                batch = parse_detection({'actions': [{**action, **changes}]}, work, quote_options(work['segments'], 0))
                self.assertFalse(batch.actions)
                self.assertEqual(batch.failures[0]['issues'][0]['code'], 'correction_changed_intent')

    def test_legacy_omitted_version_keeps_its_effective_original_version(self):
        for edited in (False, True):
            with self.subTest(edited=edited):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='旧提案缺省版本', data_scope='synthetic'))['id']
                question, work, action = self.followup()
                original = {**action, 'question_id': question['id'].removeprefix('question-'),
                    'title': '合成材料新范围', 'scope_changed': True}
                original.pop('expected_version')
                task = self.legacy_review(work, original)
                current = question
                if edited:
                    current = self.wb.edit(self.owner, question['id'],
                        EditQuestion(expected_version=question['version'], title='人工更正后的合成范围'))
                self.wb.retry_detection_task(self.owner, self.mid, task['id'])
                retry = self.wb.detection_recovery_work(self.agent)
                self.run_work(retry, [{**original, 'question_id': question['id'], 'expected_version': current['version']}])
                snap = self.snapshot()
                if edited:
                    self.assertEqual(snap['questions'], [current])
                    remaining, = snap['detection_tasks']
                    self.assertEqual(remaining['state'], 'needs_review')
                    self.assertEqual(remaining['issues'][0]['code'], 'correction_changed_intent')
                    self.assertTrue(remaining['issues'][0]['field'].endswith('expected_version'))
                else:
                    revised, = snap['questions']
                    self.assertEqual((revised['id'], revised['title'], revised['version']),
                        (question['id'], original['title'], question['version'] + 1))
                    self.assertFalse(snap['detection_tasks'])
                with self.wb.store.transaction() as db:
                    self.assertEqual(self.wb.store._get(db, 'wb_detection_task', task['id'])['proposal'], original)

    def test_concurrent_question_version_change_is_not_overwritten(self):
        question, work, action = self.followup()
        batch = parse_detection({'actions': [action]}, work, quote_options(work['segments'], 0))
        with self.wb.store.transaction() as db:
            current = self.wb.store._get(db, 'wb_question', question['id'])
            current.update(version=current['version'] + 1, generation=current['generation'] + 1)
            self.wb.store._put(db, 'wb_question', current['id'], current)
        self.wb.commit_detection(self.agent, work, batch)
        snap = self.snapshot()
        self.assertEqual(snap['questions'], [current])
        task, = snap['detection_tasks']
        self.assertEqual(task['issues'][0]['code'], 'question_version_stale')

    def test_approval_during_detection_reports_changed_state_without_a_new_version(self):
        question, work, action = self.followup()
        batch = parse_detection({'actions': [action]}, work, quote_options(work['segments'], 0))
        approved = self.wb.action(self.owner, question['id'],
            QuestionAction(expected_version=question['version'], action='approve'))
        self.assertEqual(approved['version'], question['version'])
        self.assertEqual(approved['generation'], question['generation'] + 1)
        self.wb.commit_detection(self.agent, work, batch)
        snap = self.snapshot()
        self.assertEqual(snap['questions'], [approved])
        task, = snap['detection_tasks']
        self.assertEqual(task['issues'][0]['code'], 'question_state_stale')

    def test_old_scan_reference_repair_cannot_replace_question_or_scope(self):
        question, work, action = self.followup()
        original = {**action, 'question_id': question['id'].removeprefix('question-')}
        self.run_work(work, [])
        with self.wb.store.transaction() as db:
            meeting = self.wb._meeting(db, self.owner, self.mid, True)
            self.wb._save_detection_task(db, meeting, work, 'scan', [work['segments'][-1]],
                known_failures=[{'proposal': original, 'issues': [{'field': 'actions.0', 'code': 'context_stale'}]}])
            task, = self.wb._detection_tasks(db, self.mid)
            self.wb._save(db, meeting)
        retry = self.wb.detection_recovery_work(self.agent)
        replacement = {**action, 'title': '另一个合成问题范围', 'scope_changed': True}
        self.run_work(retry, [replacement])
        current, = self.snapshot()['questions']
        self.assertEqual(current, question)
        with self.wb.store.transaction() as db:
            stored = self.wb.store._get(db, 'wb_detection_task', task['id'])
            self.assertEqual(stored['state'], 'pending')
            self.assertEqual(stored['known_failures'][0]['proposal'], original)
        retry = self.wb.detection_recovery_work(self.agent)
        self.run_work(retry, [action])
        self.assertFalse(self.snapshot()['detection_tasks'])
        self.assertEqual(len(self.snapshot()['questions']), 1)
        with self.wb.store.transaction() as db:
            stored = self.wb.store._get(db, 'wb_detection_task', task['id'])
            self.assertEqual(stored['state'], 'completed')

    def test_repaired_mention_preserves_finished_research_and_its_basis(self):
        question, work, action = self.followup()
        with self.wb.store.transaction() as db:
            finished = self.wb.store._get(db, 'wb_question', question['id'])
            finished.update(state='insufficient', result={'answer': '合成资料不足'})
            self.wb.store._put(db, 'wb_question', finished['id'], finished)
        work['questions'] = [finished]
        self.run_work(work, [{**action, 'question_id': question['id'].removeprefix('question-')}])
        current, = self.snapshot()['questions']
        for field in ('id', 'state', 'result', 'source_refs', 'generation', 'version'):
            self.assertEqual(current[field], finished[field])
        self.assertEqual(current['last_mention_refs'], action['source_refs'])
        self.assertFalse(self.snapshot()['detection_tasks'])

    def test_dismissed_review_is_audited_survives_restart_and_is_not_success(self):
        task = self.review()
        self.wb.control(self.owner, self.mid, 'pause_research')
        self.wb.permissions(self.owner, self.mid, MeetingPermissions())
        before = self.snapshot()
        result = self.wb.dismiss_detection_task(self.owner, self.mid, task['id'])
        self.assertEqual(result['state'], 'dismissed')
        self.assertEqual(self.wb.dismiss_detection_task(self.owner, self.mid, task['id']), result)
        snap = self.snapshot()
        self.assertFalse(snap['detection_tasks'])
        self.assertEqual(snap['questions'], before['questions'])
        self.assertEqual(snap['segments'], before['segments'])
        self.assertEqual(snap['automation_progress']['needs_review'], 0)
        self.assertEqual(snap['automation_progress']['dismissed'], 1)
        self.assertFalse(snap['automation_progress']['complete'])
        with self.wb.store.transaction() as db:
            stored = self.wb.store._get(db, 'wb_detection_task', task['id'])
            self.assertEqual(stored['dismissed_by'], self.owner['id'])
            self.assertGreater(stored['dismissed_at'], 0)
            events = [json.loads(r[0]) for r in db.execute("SELECT data FROM events WHERE meeting=?", (self.mid,))]
            self.assertEqual(sum(e['type'] == 'detection.dismissed' for e in events), 1)
        reopened = create_app(self.wb.store.path, self.config).extensions['workbench']
        reopened.recover(publish=False)
        self.assertFalse(reopened.snapshot(self.owner, self.mid)['detection_tasks'])
        self.assertIsNone(reopened.detection_recovery_work(self.agent))
        with self.assertRaises(LiveError):
            reopened.retry_detection_task(self.owner, self.mid, task['id'])
        from tools.prepare_detection_rollback import prepare_rollback
        report = prepare_rollback(self.wb.store.path, self.root / 'rollback.sqlite3')
        self.assertFalse(report['paused_meetings'])

    def test_sibling_scan_does_not_recreate_dismissed_correction(self):
        for dismiss_before_claim in (True, False):
            for invalid_response in (True, False):
                with self.subTest(dismiss_before_claim=dismiss_before_claim, invalid_response=invalid_response):
                    self.wb.control(self.owner, self.mid, 'pause_research')
                    self.mid = self.wb.create(self.owner, NewMeeting(title='忽略后补扫', data_scope='synthetic'))['id']
                    self.say('合成第一问？合成第二问？合成第三问？合成第四问？合成第五问？')
                    work = self.work()
                    actions = [self.action(work, f'合成第{n}问', n - 1) for n in range(1, 5)]
                    actions[-1]['grounding_quote_id'] = 'absent'
                    self.run_work(work, actions)
                    correction = self.wb.detection_recovery_work(self.agent)
                    self.assertEqual(correction['detection_task']['kind'], 'correction')
                    self.run_work(correction, [])
                    tid = correction['detection_task']['id']
                    scan = None if dismiss_before_claim else self.wb.detection_recovery_work(self.agent)
                    self.wb.dismiss_detection_task(self.owner, self.mid, tid)
                    if scan is None:
                        scan = self.wb.detection_recovery_work(self.agent)
                    self.assertEqual(scan['detection_task']['kind'], 'scan')
                    ignored = self.action(scan, '合成第4问', 3)
                    if invalid_response:
                        ignored['grounding_quote_id'] = 'absent-again'
                    self.run_work(scan, [ignored, self.action(scan, '合成第5问', 4)])
                    snap = self.snapshot()
                    self.assertEqual({q['title'] for q in snap['questions']}, {f'合成第{n}问' for n in (1, 2, 3, 5)})
                    self.assertFalse(snap['detection_tasks'])
                    self.assertEqual(snap['automation_progress']['dismissed'], 1)
                    if dismiss_before_claim:
                        deferred, = scan['deferred_detection_proposals']
                        self.assertEqual(deferred['state'], 'dismissed')
                    with self.wb.store.transaction() as db:
                        self.assertEqual(self.wb.store._get(db, 'wb_detection_task', tid)['state'], 'dismissed')
                    self.say('合成第四问在新发言中再次提出？')
                    later = self.work()
                    self.run_work(later, [self.action(later, '合成第4问')])
                    self.assertEqual(len(self.snapshot()['questions']), 5)

    def test_dismissed_scan_suppresses_its_range_but_keeps_other_questions(self):
        for n in range(3):
            self.say(f'合成范围问题{n}？', str(n))
        work = self.work()
        failures = [{'proposal': self.action(work, f'合成范围问题{n}', n),
            'issues': [{'field': 'actions', 'code': 'context_stale'}]} for n in range(2)]
        self.run_work(work, [])
        with self.wb.store.transaction() as db:
            meeting = self.wb._meeting(db, self.owner, self.mid, True)
            self.wb._save_detection_task(db, meeting, work, 'scan', work['segments'][:2], known_failures=failures)
            ignored, = self.wb._detection_tasks(db, self.mid)
            ignored.update(state='needs_review', attempts=2)
            self.wb.store._put(db, 'wb_detection_task', ignored['id'], ignored)
            self.wb._save_detection_task(db, meeting, work, 'scan', work['segments'],
                known_failures=failures, suffix='overlapping-range')
            self.wb._save(db, meeting)
        scan = self.wb.detection_recovery_work(self.agent)
        self.wb.dismiss_detection_task(self.owner, self.mid, ignored['id'])
        self.run_work(scan, [self.action(scan, f'合成范围问题{n}', n) for n in range(3)])
        snap = self.snapshot()
        self.assertEqual([q['title'] for q in snap['questions']], ['合成范围问题2'])
        self.assertFalse(snap['detection_tasks'])
        self.assertEqual(snap['automation_progress']['dismissed'], 1)

    def test_dismissed_correction_supersedes_an_already_claimed_duplicate(self):
        for response in ('corrected', 'empty', 'failed'):
            with self.subTest(response=response):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='重复纠正忽略', data_scope='synthetic'))['id']
                self.say('合成第一问？合成第二问？合成第三问？合成交付日期？合成第五问？')
                work = self.work()
                actions = [self.action(work, f'合成第{n}问', n - 1) for n in range(1, 4)]
                actions.append({**self.action(work, '合成交付日期', 3), 'kind': 'create_question',
                    'grounding_quote_id': 'absent'})
                self.run_work(work, actions)
                correction = self.wb.detection_recovery_work(self.agent)
                self.run_work(correction, [])
                scan = self.wb.detection_recovery_work(self.agent)
                self.run_work(scan, [{**self.action(scan, '合成交付日期', 3), 'kind': 'create_question',
                    'grounding_quote_id': 'different-invalid'}, self.action(scan, '合成第五问', 4)])
                duplicate = self.wb.detection_recovery_work(self.agent)
                tid = correction['detection_task']['id']
                self.assertNotEqual(duplicate['detection_task']['id'], tid)
                self.wb.dismiss_detection_task(self.owner, self.mid, tid)
                if response == 'failed':
                    self.wb.detection_recovery_failed(self.agent, duplicate,
                        {'stage': 'saving', 'code': 'local_save_failed'})
                else:
                    actions = [{**self.action(duplicate, '合成交付日期', 3), 'kind': 'create_question'}]
                    self.run_work(duplicate, actions if response == 'corrected' else [])
                snap = self.snapshot()
                self.assertNotIn('合成交付日期', [q['title'] for q in snap['questions']])
                self.assertFalse(snap['detection_tasks'])
                with self.wb.store.transaction() as db:
                    stored = self.wb.store._get(db, 'wb_detection_task', duplicate['detection_task']['id'])
                    self.assertEqual(stored['state'], 'superseded')
                    self.assertEqual(stored['superseded_by'], tid)
                    self.assertIsNone(stored['lease'])

    def test_confirmed_duplicate_already_in_review_closes_on_dismissal(self):
        self.say('合成交付日期？')
        work = self.work()
        action = self.action(work, '合成交付日期')
        self.run_work(work, [{**action, 'extra': True}, *[{'kind': 'no_action'}] * (work['action_limit'] - 1)])
        original = self.wb.detection_recovery_work(self.agent)
        self.run_work(original, [])
        scan = self.wb.detection_recovery_work(self.agent)
        self.run_work(scan, [{**action, 'other_extra': True}])
        duplicate = self.wb.detection_recovery_work(self.agent)
        self.run_work(duplicate, [])
        self.assertEqual(len(self.snapshot()['detection_tasks']), 2)
        self.wb.dismiss_detection_task(self.owner, self.mid, original['detection_task']['id'])
        self.assertFalse(self.snapshot()['detection_tasks'])

    def test_returned_punctuation_fallback_remains_ambiguous_after_parsing(self):
        self.say('合成交付日期。', 'east')
        self.say('合成交付日期！', 'west')
        work = self.work()
        east, west = [self.action(work, '合成交付日期', index) for index in range(2)]
        self.run_work(work, [{**east, 'extra': True}, *[{'kind': 'no_action'}] * (work['action_limit'] - 1)])
        original = self.wb.detection_recovery_work(self.agent)
        self.run_work(original, [])
        self.wb.dismiss_detection_task(self.owner, self.mid, original['detection_task']['id'])
        refs = east['source_refs'] + west['source_refs']
        for citations in (refs, list(reversed(refs))):
            scan = self.wb.detection_recovery_work(self.agent)
            self.run_work(scan, [{**east, 'source_refs': citations,
                'grounding_quote_id': '', 'grounding_quote': '合成交付日期？'}])
            snap = self.snapshot()
            self.assertFalse(snap['questions'])
            task, = snap['detection_tasks']
            self.assertEqual(task['issues'][0]['code'], 'overlapping_proposal')
        self.assertEqual(task['state'], 'needs_review')

    def test_stale_context_citation_cannot_close_an_unresolved_correction(self):
        self.say('合成旧发言。', 'context')
        work = self.work()
        context_refs = self.action(work, '合成旧发言')['source_refs']
        self.run_work(work, [])
        self.say('合成东区交付日期？', 'east')
        self.say('合成西区交付日期？', 'west')
        work = self.work()
        east, west = [self.action(work, '合成交付日期', index) for index in range(2)]
        broken = {**west, 'source_refs': east['source_refs'] + west['source_refs'], 'grounding_quote_id': 'invalid'}
        self.run_work(work, [{**east, 'extra': True}, broken])
        original = self.wb.detection_recovery_work(self.agent)
        self.run_work(original, [])
        repair = self.wb.detection_recovery_work(self.agent)
        self.wb.dismiss_detection_task(self.owner, self.mid, original['detection_task']['id'])
        self.say('合成旧发言已更正。', 'context', revision=2)
        self.run_work(repair, [{**east, 'source_refs': east['source_refs'] + context_refs}])
        snap = self.snapshot()
        self.assertFalse(snap['questions'])
        unresolved, = snap['detection_tasks']
        self.assertEqual(unresolved['id'], repair['detection_task']['id'])
        self.assertEqual(unresolved['state'], 'needs_review')
        self.assertEqual(unresolved['issues'][0]['code'], 'source_revision_stale')

    def test_dismissed_quote_cannot_be_revived_with_a_rephrased_title(self):
        self.say('合成第一问？合成第二问？合成第三问？合成交付日期？合成第五问？')
        work = self.work()
        actions = [self.action(work, f'合成第{n}问', n - 1) for n in range(1, 4)]
        actions.append({**self.action(work, '合成交付日期', 3), 'kind': 'create_question',
            'evidence_need': 'public'})  # Valid quote, but the external fact is missing.
        self.run_work(work, actions)
        correction = self.wb.detection_recovery_work(self.agent)
        self.run_work(correction, [])
        scan = self.wb.detection_recovery_work(self.agent)
        self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
        self.run_work(scan, [{**self.action(scan, '合成交付时间', 3), 'kind': 'create_question',
            'evidence_need': 'public', 'external_fact': '合成交付日期'}, self.action(scan, '合成第五问', 4)])
        retry = self.wb.detection_recovery_work(self.agent)
        self.run_work(retry, [{**self.action(retry, '合成交付时间', 3), 'kind': 'create_question',
            'evidence_need': 'public', 'external_fact': '合成交付日期'}])
        snap = self.snapshot()
        self.assertEqual({q['title'] for q in snap['questions']},
            {'合成第1问', '合成第2问', '合成第3问', '合成第五问'})
        review, = snap['detection_tasks']
        self.assertEqual((review['kind'], review['state']), ('scan', 'needs_review'))
        self.assertEqual(review['issues'][0]['code'], 'overlapping_proposal')
        self.assertEqual(snap['automation_progress']['dismissed'], 1)
        with self.wb.store.transaction() as db:
            self.assertEqual(self.wb.store._get(db, 'wb_detection_task', correction['detection_task']['id'])['state'], 'dismissed')

    def test_dismissed_quote_preserves_questions_with_different_valid_intent(self):
        for discriminator in ('public_query', 'external_fact', 'original_target'):
            with self.subTest(discriminator=discriminator):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='同引文独立问题', data_scope='synthetic'))['id']
                self.say('合成第一问？合成第二问？合成第三问？合成预算是多少、交付日期是什么？')
                work = self.work()
                original = {**self.action(work, '合成预算', 3), 'kind': 'create_question', 'evidence_need': 'public'}
                changed = {'public_query': '合成交付日期', 'external_fact': '合成交付日期'}
                if discriminator == 'public_query':
                    original['public_query'] = '合成预算'
                elif discriminator == 'external_fact':
                    original.update(external_fact='合成预算', public_query=7)
                else:
                    original['original_target'] = {'kind': 'standard', 'identifier': 'SYNTH-A'}
                    changed['original_target'] = {'kind': 'standard', 'identifier': 'SYNTH-B'}
                actions = [self.action(work, f'合成第{n}问', n - 1) for n in range(1, 4)]
                self.run_work(work, [*actions, original])
                correction = self.wb.detection_recovery_work(self.agent)
                self.run_work(correction, [])
                scan = self.wb.detection_recovery_work(self.agent)
                self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
                self.run_work(scan, [{**self.action(scan, '合成交付日期', 3), 'kind': 'create_question',
                    'evidence_need': 'public', **changed}])
                snap = self.snapshot()
                question = next(q for q in snap['questions'] if q['title'] == '合成交付日期')
                self.assertEqual((question['state'], question['external_fact']), ('queued', '合成交付日期'))
                self.assertEqual(question['grounding_quote'], '合成预算是多少、交付日期是什么？')
                self.assertNotIn('合成预算', [q['title'] for q in snap['questions']])
                self.assertFalse(snap['detection_tasks'])

    def test_omitted_or_empty_query_cannot_revive_a_dismissed_public_question(self):
        for returned_query in (None, '', '  '):
            with self.subTest(returned_query=returned_query):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='缺省查询不复活已忽略问题', data_scope='synthetic'))['id']
                self.say('合成第一问？合成第二问？合成第三问？合成交付日期？')
                work = self.work()
                original = {**self.action(work, '合成交付日期', 3), 'kind': 'create_question',
                    'evidence_need': 'public', 'external_fact': '合成交付日期',
                    'public_query': '合成交付日期', 'extra': True}
                self.run_work(work, [*[self.action(work, f'合成第{index+1}问', index) for index in range(3)], original])
                correction = self.wb.detection_recovery_work(self.agent)
                self.run_work(correction, [])
                self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
                scan = self.wb.detection_recovery_work(self.agent)
                returned = {key: value for key, value in original.items() if key not in {'extra', 'public_query'}}
                if returned_query is not None:
                    returned['public_query'] = returned_query
                self.run_work(scan, [returned])
                snap = self.snapshot()
                self.assertEqual({question['title'] for question in snap['questions']},
                    {'合成第1问', '合成第2问', '合成第3问'})
                self.assertFalse(snap['detection_tasks'])
                self.assertEqual(snap['automation_progress']['dismissed'], 1)

    def test_dismissal_keeps_valid_intent_fields_when_another_field_is_invalid(self):
        self.say('合成第一问？合成第二问？合成第三问？甲乙两类的合成审批限额各是什么？')
        work = self.work()
        original = {**self.action(work, '合成审批限额', 3), 'kind': 'create_question',
            'evidence_need': 'public', 'external_fact': '甲类的合成限额', 'public_query': 7}
        actions = [self.action(work, f'合成第{n}问', n - 1) for n in range(1, 4)]
        self.run_work(work, [*actions, original])
        correction = self.wb.detection_recovery_work(self.agent)
        self.run_work(correction, [])
        self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
        scan = self.wb.detection_recovery_work(self.agent)
        distinct = {**original, 'external_fact': '乙类的合成限额'}
        self.run_work(scan, [distinct])
        repair = self.wb.detection_recovery_work(self.agent)
        self.assertEqual(repair['detection_task']['proposal'], distinct)
        self.run_work(repair, [])
        remaining, = self.snapshot()['detection_tasks']
        self.assertEqual((remaining['id'], remaining['state']), (repair['detection_task']['id'], 'needs_review'))
        self.assertEqual(self.snapshot()['automation_progress']['dismissed'], 1)
        self.assertNotIn('合成审批限额', [q['title'] for q in self.snapshot()['questions']])

    def test_candidate_classification_changes_cannot_revive_dismissed_questions(self):
        for initial, returned in (('candidate', 'create_question'), ('create_question', 'candidate')):
            with self.subTest(initial=initial, returned=returned):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='忽略后分类改变', data_scope='synthetic'))['id']
                self.say('合成第一问？合成第二问？合成第三问？合成交付日期？')
                work = self.work()
                original = {**self.action(work, '合成交付日期', 3), 'kind': initial,
                    'evidence_need': 'public', 'external_fact': '合成交付日期', 'public_query': 7}
                actions = [self.action(work, f'合成第{n}问', n - 1) for n in range(1, 4)]
                self.run_work(work, [*actions, original])
                correction = self.wb.detection_recovery_work(self.agent)
                self.run_work(correction, [])
                self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
                scan = self.wb.detection_recovery_work(self.agent)
                self.run_work(scan, [{**original, 'kind': returned, 'public_query': ''}])
                snap = self.snapshot()
                self.assertEqual({q['title'] for q in snap['questions']}, {'合成第1问', '合成第2问', '合成第3问'})
                self.assertFalse(snap['detection_tasks'])
                self.assertEqual(snap['automation_progress']['dismissed'], 1)

    def test_unique_subquotes_stay_for_review_while_distinct_quotes_continue(self):
        for overlapping in (True, False):
            with self.subTest(overlapping=overlapping):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='原话片段重叠', data_scope='synthetic'))['id']
                self.say('合成第一问？合成第二问？合成第三问？合成东区交付日期什么时候确定？合成西区交付日期什么时候确定？')
                work = self.work()
                original = {**self.action(work, '合成交付日期', 3), 'kind': 'create_question', 'evidence_need': 'public'}
                actions = [self.action(work, f'合成第{n}问', n - 1) for n in range(1, 4)]
                self.run_work(work, [*actions, original])
                correction = self.wb.detection_recovery_work(self.agent)
                self.run_work(correction, [])
                self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
                scan = self.wb.detection_recovery_work(self.agent)
                quote = '东区交付日期什么时候确定' if overlapping else '西区交付日期什么时候确定'
                response = {**original, 'grounding_quote_id': '', 'grounding_quote': quote,
                    'external_fact': '合成区域交付日期'}
                self.run_work(scan, [response])
                if overlapping:
                    self.run_work(self.wb.detection_recovery_work(self.agent), [response])
                    review, = self.snapshot()['detection_tasks']
                    self.assertEqual((review['kind'], review['state']), ('scan', 'needs_review'))
                    self.assertEqual(review['issues'][0]['code'], 'overlapping_proposal')
                    self.assertNotIn('合成交付日期', [q['title'] for q in self.snapshot()['questions']])
                else:
                    question = next(q for q in self.snapshot()['questions'] if q['title'] == '合成交付日期')
                    self.assertEqual((question['state'], question['grounding_quote']), ('queued', quote))
                    self.assertFalse(self.snapshot()['detection_tasks'])
                self.assertEqual(self.snapshot()['automation_progress']['dismissed'], 1)

    def test_dismissed_broken_quote_matches_reordered_original_citation_set(self):
        for reordered in (False, True):
            with self.subTest(reordered=reordered):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='引用顺序忽略', data_scope='synthetic'))['id']
                self.say('旧的合成上下文？', 'old')
                self.run_work(self.work(), [])
                self.say('合成第一问？合成第二问？合成第三问？合成交付日期？', 'new')
                work = self.work()
                proposal = {**self.action(work, '合成交付日期', 3), 'kind': 'create_question'}
                proposal['source_refs'].append({'segment_id': work['segments'][0]['id'],
                    'revision': work['segments'][0]['revision']})
                original = {**proposal, 'grounding_quote_id': 'broken'}
                actions = [self.action(work, f'合成第{n}问', n - 1) for n in range(1, 4)]
                self.run_work(work, [*actions, original])
                correction = self.wb.detection_recovery_work(self.agent)
                self.assertEqual(len(correction['detection_task']['source_refs']), 1)
                self.assertEqual(len(original['source_refs']), 2)
                self.run_work(correction, [])
                scan = self.wb.detection_recovery_work(self.agent)
                self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
                response = {**proposal, 'source_refs': list(reversed(proposal['source_refs'])) if reordered else proposal['source_refs']}
                self.run_work(scan, [response])
                snap = self.snapshot()
                self.assertEqual({q['title'] for q in snap['questions']}, {'合成第1问', '合成第2问', '合成第3问'})
                self.assertFalse(snap['detection_tasks'])
                with self.wb.store.transaction() as db:
                    stored = self.wb.store._get(db, 'wb_detection_task', correction['detection_task']['id'])
                    self.assertEqual((stored['state'], stored['proposal']), ('dismissed', original))

    def test_dismissed_title_does_not_hide_another_literal_question(self):
        self.say('合成第一问？合成第二问？合成第三问？合成东区交付日期？合成西区交付日期？')
        work = self.work()
        actions = [self.action(work, f'合成第{n}问', n - 1) for n in range(1, 4)]
        actions.append({**self.action(work, '合成交付日期', 3), 'evidence_need': 'invalid'})
        self.run_work(work, actions)
        correction = self.wb.detection_recovery_work(self.agent)
        self.run_work(correction, [])
        self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
        scan = self.wb.detection_recovery_work(self.agent)
        self.run_work(scan, [{**self.action(scan, '合成交付日期', 4), 'kind': 'create_question',
            'evidence_need': 'public', 'external_fact': '合成西区交付日期'}])
        question = next(q for q in self.snapshot()['questions'] if q['title'] == '合成交付日期')
        self.assertEqual(question['grounding_quote'], '合成西区交付日期？')
        self.assertEqual(question['state'], 'queued')
        self.assertFalse(self.snapshot()['detection_tasks'])

    def test_dismissing_scan_preserves_shared_failures_outside_its_range(self):
        for source_known in (True, False):
            with self.subTest(source_known=source_known):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='不同补扫范围', data_scope='synthetic'))['id']
                self.say('合成预算确定了吗？', 'candidate')
                work = self.work()
                self.run_work(work, [self.action(work, '合成预算')])
                question, = self.snapshot()['questions']
                for n in range(8):
                    self.say('撤回合成预算问题？' if n == 7 else f'合成其他问题{n}？', f'window-{n}')
                work = self.work()
                proposal = {**self.action(work, '合成预算', 7), 'kind': 'cancel_candidate',
                    'question_id': question['id'], 'expected_version': question['version'] + 1}
                if not source_known:
                    proposal['source_refs'] = [{'segment_id': {}, 'revision': 1}]
                self.run_work(work, [proposal])
                first = self.wb.detection_recovery_work(self.agent)
                self.run_work(first, [])
                second = self.wb.detection_recovery_work(self.agent)
                self.run_work(second, [])
                retry_first = self.wb.detection_recovery_work(self.agent)
                self.assertEqual(retry_first['detection_task']['id'], first['detection_task']['id'])
                self.run_work(retry_first, [])
                self.wb.dismiss_detection_task(self.owner, self.mid, first['detection_task']['id'])
                retry_second = self.wb.detection_recovery_work(self.agent)
                self.assertEqual(retry_second['detection_task']['id'], second['detection_task']['id'])
                self.run_work(retry_second, [])
                with self.wb.store.transaction() as db:
                    stored = self.wb.store._get(db, 'wb_detection_task', second['detection_task']['id'])
                    self.assertEqual(stored['state'], 'needs_review')
                    self.assertEqual(stored['known_failures'][0]['proposal'], proposal)

    def test_scan_dismissal_excludes_only_verified_older_context_from_its_range(self):
        for additional in ('old_context', 'other_window', 'unknown', 'later'):
            with self.subTest(additional=additional):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='补扫旧上下文归属', data_scope='synthetic'))['id']
                self.say('合成预算由哪个部门管理？', 'old')
                first = self.work()
                old_ref = self.action(first, '合成预算')['source_refs'][0]
                self.run_work(first, [])
                for n in range(8):
                    self.say('撤回合成预算问题？' if n == 0 else f'合成其他问题{n}？', f'window-{n}')
                work = self.work()
                proposal = {**self.action(work, '合成预算'), 'kind': 'cancel_candidate', 'question_id': 'absent'}
                proposal['source_refs'].append(old_ref)
                if additional == 'other_window':
                    proposal['source_refs'] += self.action(work, '合成其他问题7', 7)['source_refs']
                elif additional == 'unknown':
                    proposal['source_refs'].append({'segment_id': 'absent', 'revision': 1})
                elif additional == 'later':
                    self.say('批次之后的新发言？', 'later')
                    later = next(segment for segment in self.snapshot()['segments'] if segment['text'] == '批次之后的新发言？')
                    proposal['source_refs'].append({'segment_id': later['id'], 'revision': later['revision']})
                self.run_work(work, [proposal])
                for _ in range(4):
                    self.run_work(self.wb.detection_recovery_work(self.agent), [])
                tasks = self.snapshot()['detection_tasks']
                ignored = next(task for task in tasks if proposal['source_refs'][0] in task['source_refs'])
                self.wb.dismiss_detection_task(self.owner, self.mid, ignored['id'])
                if additional == 'old_context':
                    self.assertFalse(self.snapshot()['detection_tasks'])
                else:
                    remaining, = self.snapshot()['detection_tasks']
                    self.assertEqual((remaining['kind'], remaining['state']), ('scan', 'needs_review'))
                    with self.wb.store.transaction() as db:
                        stored = self.wb.store._get(db, 'wb_detection_task', remaining['id'])
                        self.assertEqual(stored['known_failures'][0]['proposal'], proposal)

    def test_dismissed_revision_does_not_hide_another_question_on_the_same_quote(self):
        self.say('合成预算？合成交付日期？', 'original-questions')
        work = self.work()
        self.run_work(work, [self.action(work, '合成预算'), self.action(work, '合成交付日期', 1)])
        original = {q['title']: q for q in self.snapshot()['questions']}
        self.say('合成第一问？合成第二问？合成第三问？两项都需要更新？', 'revisions')
        work = self.work()

        def revision(target, title, current):
            question = original[target]
            return {**self.action(current, title, 3), 'kind': 'revise_question',
                'question_id': question['id'], 'expected_version': question['version'], 'scope_changed': True}

        actions = [self.action(work, f'合成第{n}问', n - 1) for n in range(1, 4)]
        actions.append({**revision('合成预算', '合成共同范围更新', work), 'evidence_need': 'public',
            'external_fact': '合成共同范围', 'extra': True})
        self.run_work(work, actions)
        correction = self.wb.detection_recovery_work(self.agent)
        self.run_work(correction, [])
        scan = self.wb.detection_recovery_work(self.agent)
        self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
        self.run_work(scan, [{**revision('合成交付日期', '合成共同范围更新', scan), 'evidence_need': 'public',
            'external_fact': '合成共同范围'}])
        questions = {q['id']: q for q in self.snapshot()['questions']}
        self.assertEqual(questions[original['合成预算']['id']], original['合成预算'])
        revised = questions[original['合成交付日期']['id']]
        self.assertEqual(revised['title'], '合成共同范围更新')
        self.assertEqual(revised['version'], original['合成交付日期']['version'] + 1)
        self.assertFalse(self.snapshot()['detection_tasks'])

    def test_corrected_response_cannot_revive_a_rephrased_dismissed_proposal(self):
        for valid_response in (True, False):
            with self.subTest(valid_response=valid_response):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='改写纠正忽略', data_scope='synthetic'))['id']
                self.say('合成第一问？合成第二问？合成第三问？合成交付日期？')
                work = self.work()
                actions = [self.action(work, f'合成第{n}问', n - 1) for n in range(1, 4)]
                actions.append({**self.action(work, '合成交付日期', 3), 'kind': 'create_question',
                    'evidence_need': 'public'})
                self.run_work(work, actions)
                correction = self.wb.detection_recovery_work(self.agent)
                self.run_work(correction, [])
                scan = self.wb.detection_recovery_work(self.agent)
                self.run_work(scan, [{**self.action(scan, '合成交付时间', 3), 'kind': 'create_question',
                    'evidence_need': 'public', 'grounding_quote_id': 'invalid'}])
                duplicate = self.wb.detection_recovery_work(self.agent)
                tid = correction['detection_task']['id']
                self.wb.dismiss_detection_task(self.owner, self.mid, tid)
                response = {**self.action(duplicate, '合成交付时间', 3), 'kind': 'create_question',
                    'evidence_need': 'public', 'external_fact': '合成交付日期'}
                if not valid_response:
                    response['evidence_need'] = 'invalid'
                self.run_work(duplicate, [response])
                snap = self.snapshot()
                self.assertEqual({q['title'] for q in snap['questions']}, {'合成第1问', '合成第2问', '合成第3问'})
                review, = snap['detection_tasks']
                self.assertEqual((review['id'], review['state']), (duplicate['detection_task']['id'], 'needs_review'))
                self.assertEqual(review['issues'][0]['code'], 'overlapping_proposal' if valid_response else 'literal_error')
                self.assertEqual(snap['automation_progress']['dismissed'], 1)
                with self.wb.store.transaction() as db:
                    stored = self.wb.store._get(db, 'wb_detection_task', duplicate['detection_task']['id'])
                    self.assertEqual(stored['state'], 'needs_review')
                    self.assertNotIn('superseded_by', stored)
                    self.assertEqual(self.wb.store._get(db, 'wb_detection_task', tid)['state'], 'dismissed')

    def test_rejected_retarget_cannot_close_an_unrelated_correction(self):
        self.say('合成交付日期是什么？合成预算是多少？')
        work = self.work()
        delivery = {**self.action(work, '合成交付日期'), 'evidence_need': 'public'}
        budget = {**self.action(work, '合成预算', 1), 'evidence_need': 'public'}
        self.run_work(work, [delivery, budget])
        ignored = self.wb.detection_recovery_work(self.agent)
        self.run_work(ignored, [])
        budget_work = self.wb.detection_recovery_work(self.agent)
        self.assertEqual(budget_work['detection_task']['proposal']['title'], '合成预算')
        self.wb.dismiss_detection_task(self.owner, self.mid, ignored['detection_task']['id'])
        self.run_work(budget_work, [{**delivery, 'external_fact': '合成交付日期'}])
        snap = self.snapshot()
        self.assertFalse(snap['questions'])
        remaining, = snap['detection_tasks']
        self.assertEqual((remaining['id'], remaining['state']), (budget_work['detection_task']['id'], 'needs_review'))
        self.assertEqual(remaining['issues'][0]['code'], 'correction_changed_intent')
        with self.wb.store.transaction() as db:
            stored = self.wb.store._get(db, 'wb_detection_task', remaining['id'])
            self.assertEqual(stored['proposal'], budget)
            self.assertNotIn('superseded_by', stored)

    def test_valid_corrected_quote_refutes_only_the_broken_quote_fallback(self):
        for original_grounding in ('broken', 'quote_id', 'literal_with_bad_id'):
            with self.subTest(original_grounding=original_grounding):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='纠正后的独立原话', data_scope='synthetic'))['id']
                self.say('合成第一问？合成第二问？合成第三问？合成东区交付日期？合成西区交付日期？')
                work = self.work()
                east = {**self.action(work, '合成交付日期', 3), 'kind': 'create_question', 'evidence_need': 'public'}
                actions = [self.action(work, f'合成第{n}问', n - 1) for n in range(1, 4)]
                self.run_work(work, [*actions, east])
                ignored = self.wb.detection_recovery_work(self.agent)
                self.run_work(ignored, [])
                scan = self.wb.detection_recovery_work(self.agent)
                west = {**self.action(scan, '合成交付日期', 4), 'kind': 'create_question',
                    'evidence_need': 'public', 'external_fact': '合成西区交付日期'}
                original = {**west, 'grounding_quote_id': east['grounding_quote_id'] if original_grounding == 'quote_id' else 'broken'}
                if original_grounding != 'broken':
                    original['public_query'] = 7
                if original_grounding == 'literal_with_bad_id':
                    original['grounding_quote'] = '合成东区交付日期？'
                self.run_work(scan, [original])
                repair = self.wb.detection_recovery_work(self.agent)
                self.wb.dismiss_detection_task(self.owner, self.mid, ignored['detection_task']['id'])
                self.run_work(repair, [west])
                snap = self.snapshot()
                self.assertFalse(snap['detection_tasks'])
                with self.wb.store.transaction() as db:
                    stored = self.wb.store._get(db, 'wb_detection_task', repair['detection_task']['id'])
                    self.assertEqual(stored['proposal'], original)
                if original_grounding != 'broken':
                    self.assertNotIn('合成交付日期', [q['title'] for q in snap['questions']])
                    self.assertEqual((stored['state'], stored['superseded_by']), ('superseded', ignored['detection_task']['id']))
                else:
                    question = next(q for q in snap['questions'] if q['title'] == '合成交付日期')
                    self.assertEqual((question['state'], question['grounding_quote']), ('queued', '合成西区交付日期？'))
                    self.assertEqual(stored['state'], 'completed')
                    self.assertNotIn('superseded_by', stored)

    def test_repeated_literal_text_does_not_hide_a_different_source(self):
        for ambiguous_sources in (False, True):
            with self.subTest(ambiguous_sources=ambiguous_sources):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='同文不同来源', data_scope='synthetic'))['id']
                self.say('合成第一问？合成第二问？合成第三问？交付日期确定了吗？', 'east')
                self.say('现在讨论西区！交付日期确定了吗？', 'west')
                work = self.work()
                actions = [self.action(work, f'合成第{n}问', n - 1) for n in range(1, 4)]
                ignored = {**self.action(work, '合成东区交付日期', 3), 'evidence_need': 'public',
                    'grounding_quote_id': '', 'grounding_quote': '交付日期确定了吗？'}
                if ambiguous_sources:
                    ignored['source_refs'] += self.action(work, '合成西区交付日期', 5)['source_refs']
                self.run_work(work, [*actions, ignored])
                correction = self.wb.detection_recovery_work(self.agent)
                self.run_work(correction, [])
                scan = self.wb.detection_recovery_work(self.agent)
                self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
                western = self.action(scan, '合成西区交付日期', 5)
                western['source_refs'] += self.action(scan, '合成东区交付日期', 3)['source_refs']
                self.run_work(scan, [western])
                question = next(q for q in self.snapshot()['questions'] if q['title'] == '合成西区交付日期')
                self.assertEqual(question['grounding_quote'], '交付日期确定了吗？')
                self.assertEqual(question['source_refs'], western['source_refs'])
                self.assertFalse(self.snapshot()['detection_tasks'])

    def test_repeated_literal_quotes_need_review_only_when_possible_spans_overlap(self):
        for returned_grounding in ('same_literal', 'subquote', 'different_literal', 'different_anchor'):
            with self.subTest(returned_grounding=returned_grounding):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='重复原话按可能位置判断重叠', data_scope='synthetic'))['id']
                self.say('合成第一问？合成第二问？合成第三问？合成东区交付日期？合成东区交付日期？合成西区交付日期？合成西区交付日期？')
                work = self.work()
                original = {**self.action(work, '合成区域交付日期', 3), 'kind': 'create_question',
                    'evidence_need': 'public'}
                if returned_grounding != 'different_anchor':
                    original.update(grounding_quote_id='', grounding_quote='合成东区交付日期？')
                self.run_work(work, [*[self.action(work, f'合成第{index+1}问', index) for index in range(3)], original])
                correction = self.wb.detection_recovery_work(self.agent)
                self.run_work(correction, [])
                self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
                scan = self.wb.detection_recovery_work(self.agent)
                returned = {**original, 'external_fact': '合成区域交付日期'}
                if returned_grounding == 'subquote':
                    returned['grounding_quote'] = '东区交付日期'
                elif returned_grounding == 'different_literal':
                    returned['grounding_quote'] = '合成西区交付日期？'
                elif returned_grounding == 'different_anchor':
                    returned['grounding_quote_id'] = self.action(scan, original['title'], 4)['grounding_quote_id']
                self.run_work(scan, [returned])
                if returned_grounding in ('same_literal', 'subquote'):
                    self.run_work(self.wb.detection_recovery_work(self.agent), [returned])
                    snap = self.snapshot()
                    self.assertNotIn(original['title'], [question['title'] for question in snap['questions']])
                    review, = snap['detection_tasks']
                    self.assertEqual((review['kind'], review['state']), ('scan', 'needs_review'))
                    self.assertEqual(review['issues'][0]['code'], 'overlapping_proposal')
                else:
                    snap = self.snapshot()
                    question = next(item for item in snap['questions'] if item['title'] == original['title'])
                    self.assertEqual(question['grounding_quote'],
                        '合成西区交付日期？' if returned_grounding == 'different_literal' else '合成东区交付日期？')
                    self.assertFalse(snap['detection_tasks'])

    def test_dismissed_reference_extra_fields_do_not_change_its_verified_identity(self):
        self.say('合成第一问？合成第二问？合成第三问？合成交付日期？')
        work = self.work()
        original = self.action(work, '合成交付日期', 3)
        original['source_refs'][0]['extra'] = '合成备注'
        parsed = parse_detection({'actions': [original]}, work,
            quote_options(work['segments'], work['meeting']['coordinator_cursor']))
        self.assertFalse(parsed.actions)
        self.assertEqual(parsed.failures[0]['issues'][0]['code'], 'extra_forbidden')
        self.run_work(work, [*[self.action(work, f'合成第{index+1}问', index) for index in range(3)], original])
        correction = self.wb.detection_recovery_work(self.agent)
        self.run_work(correction, [])
        self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
        scan = self.wb.detection_recovery_work(self.agent)
        self.run_work(scan, [self.action(scan, original['title'], 3)])
        snap = self.snapshot()
        self.assertEqual({question['title'] for question in snap['questions']},
            {'合成第1问', '合成第2问', '合成第3问'})
        self.assertFalse(snap['detection_tasks'])
        self.assertEqual(snap['automation_progress']['dismissed'], 1)
        with self.wb.store.transaction() as db:
            stored = self.wb.store._get(db, 'wb_detection_task', correction['detection_task']['id'])
            self.assertEqual(stored['proposal'], original)

    def test_literal_dismissal_uses_its_pinned_sources_outside_a_scan_window(self):
        for remote_source in ('distinct', 'repeated', 'revised'):
            with self.subTest(remote_source=remote_source):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='裁剪上下文保留原忽略依据', data_scope='synthetic'))['id']
                literal = '合成第0段交付问题？'
                for index in range(12):
                    self.say(literal if remote_source == 'repeated' and index == 11
                        else f'合成第{index}段交付问题？', f's-{index}')
                work = self.work()
                original = {**self.action(work, '合成第0段交付问题', 0), 'grounding_quote_id': '',
                    'grounding_quote': literal, 'evidence_need': 'public'}
                original['source_refs'] += self.action(work, '合成远处上下文', 11)['source_refs']
                self.run_work(work, [original, None])
                correction = self.wb.detection_recovery_work(self.agent)
                self.run_work(correction, [])
                self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
                if remote_source == 'revised':
                    self.say('合成远处上下文已更正？', 's-11', revision=2)
                scan = self.wb.detection_recovery_work(self.agent)
                self.assertEqual(scan['detection_task']['source_refs'][0], original['source_refs'][0])
                self.assertNotIn(original['source_refs'][1]['segment_id'], {segment['id'] for segment in scan['segments']})
                returned = {**self.action(scan, original['title'], 0), 'evidence_need': 'public',
                    'external_fact': '合成第0段交付问题'}
                self.run_work(scan, [returned])
                snap = self.snapshot()
                if remote_source == 'revised':
                    # A changed revision cannot be substituted into the old proposal.
                    self.assertEqual([question['title'] for question in snap['questions']], [original['title']])
                else:
                    self.assertFalse(snap['questions'])
                with self.wb.store.transaction() as db:
                    stored = self.wb.store._get(db, 'wb_detection_task', scan['detection_task']['id'])
                    if remote_source == 'repeated':
                        self.assertEqual(stored['state'], 'pending')
                        self.assertEqual(stored['issues'][0]['code'], 'overlapping_proposal')
                    else:
                        self.assertEqual(stored['state'], 'completed')

    def test_a_specific_quote_can_resolve_an_ambiguous_overlap_review(self):
        self.say('合成第一问？合成第二问？合成第三问？东区。合成交付日期？西区。合成交付日期？')
        work = self.work()
        original = {**self.action(work, '合成东区交付日期', 4), 'extra': True}
        self.run_work(work, [*[self.action(work, f'合成第{index+1}问', index) for index in range(3)], original])
        correction = self.wb.detection_recovery_work(self.agent)
        self.run_work(correction, [])
        self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
        scan = self.wb.detection_recovery_work(self.agent)
        ambiguous = {**self.action(scan, '合成区域交付日期', 4), 'grounding_quote_id': '',
            'grounding_quote': '合成交付日期？'}
        self.run_work(scan, [ambiguous])
        first, = self.snapshot()['detection_tasks']
        self.assertEqual(first['issues'][0]['code'], 'overlapping_proposal')
        retry = self.wb.detection_recovery_work(self.agent)
        western = self.action(retry, ambiguous['title'], 6)
        self.run_work(retry, [western])
        snap = self.snapshot()
        question = next(item for item in snap['questions'] if item['title'] == ambiguous['title'])
        self.assertEqual(question['grounding_quote'], '合成交付日期？')
        self.assertFalse(snap['detection_tasks'])
        self.assertEqual(snap['automation_progress']['dismissed'], 1)
        with self.wb.store.transaction() as db:
            accepted = next(action for action in self.wb._items(db, 'wb_detection_action', self.mid)
                if action['title'] == ambiguous['title'])
            self.assertEqual(accepted['grounding_quote_id'], western['grounding_quote_id'])
            stored = self.wb.store._get(db, 'wb_detection_task', scan['detection_task']['id'])
            self.assertEqual((stored['state'], stored['known_failures']), ('completed', []))

    def test_a_distinct_quote_cannot_close_an_existing_overlap_review(self):
        self.say('合成第一问？合成第二问？合成第三问？合成东区交付日期？合成西区交付日期？')
        work = self.work()
        original = {**self.action(work, '合成东区交付日期', 3), 'extra': True}
        self.run_work(work, [*[self.action(work, f'合成第{index+1}问', index) for index in range(3)], original])
        correction = self.wb.detection_recovery_work(self.agent)
        self.run_work(correction, [])
        self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
        scan = self.wb.detection_recovery_work(self.agent)
        overlapping = self.action(scan, '合成区域交付日期', 3)
        self.run_work(scan, [overlapping])
        first, = self.snapshot()['detection_tasks']
        self.assertEqual(first['issues'][0]['code'], 'overlapping_proposal')
        with self.wb.store.transaction() as db:
            original_failure = self.wb.store._get(db, 'wb_detection_task', first['id'])['known_failures'][0]
        retry = self.wb.detection_recovery_work(self.agent)
        self.run_work(retry, [self.action(retry, overlapping['title'], 4)])
        snap = self.snapshot()
        question = next(item for item in snap['questions'] if item['title'] == overlapping['title'])
        self.assertEqual(question['grounding_quote'], '合成西区交付日期？')
        remaining, = snap['detection_tasks']
        self.assertEqual((remaining['id'], remaining['state']), (scan['detection_task']['id'], 'needs_review'))
        self.assertEqual(remaining['issues'][0]['code'], 'overlapping_proposal')
        with self.wb.store.transaction() as db:
            stored = self.wb.store._get(db, 'wb_detection_task', remaining['id'])
            self.assertEqual(stored['known_failures'][0], original_failure)

    def test_literal_matching_prefers_exact_text_across_all_eligible_sources(self):
        self.say('合成第一问？合成第二问？合成第三问？', 'other')
        self.say('关于东区。合成交付日期？', 'east')
        self.say('关于西区。合成交付日期。', 'west')
        work = self.work()
        original = {**self.action(work, '合成东区交付日期', 4), 'grounding_quote_id': '',
            'grounding_quote': '合成交付日期？', 'extra': True}
        original['source_refs'] += self.action(work, '合成西区交付日期', 6)['source_refs']
        self.run_work(work, [*[self.action(work, f'合成第{index+1}问', index) for index in range(3)], original])
        correction = self.wb.detection_recovery_work(self.agent)
        self.run_work(correction, [])
        self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
        scan = self.wb.detection_recovery_work(self.agent)
        self.run_work(scan, [self.action(scan, '合成西区交付日期', 6)])
        snap = self.snapshot()
        question = next(item for item in snap['questions'] if item['title'] == '合成西区交付日期')
        self.assertEqual(question['grounding_quote'], '合成交付日期。')
        self.assertFalse(snap['detection_tasks'])

    def test_valid_disjoint_quote_refutes_a_broken_fallback_across_scan_windows(self):
        for index in range(8):
            self.say('合成东区交付日期？' if index == 0 else '合成西区交付日期？' if index == 4
                else f'合成第{index}段上下文。', f'window-{index}')
        work = self.work()
        self.run_work(work, [{'kind': 'no_action'}] * work['action_limit'])
        east, west = [self.action(work, '合成交付日期', index) for index in (0, 4)]
        broken = {**east, 'grounding_quote_id': 'invalid',
            'source_refs': east['source_refs'] + west['source_refs']}
        for _ in range(2):
            scan = self.wb.detection_recovery_work(self.agent)
            self.assertEqual(scan['detection_task']['kind'], 'scan')
            self.run_work(scan, [broken])
        ignored = self.wb.detection_recovery_work(self.agent)
        self.run_work(ignored, [])
        repair = self.wb.detection_recovery_work(self.agent)
        self.assertEqual(ignored['detection_task']['source_refs'], east['source_refs'])
        self.assertEqual(repair['detection_task']['source_refs'], west['source_refs'])
        self.wb.dismiss_detection_task(self.owner, self.mid, ignored['detection_task']['id'])
        self.run_work(repair, [west])
        snap = self.snapshot()
        self.assertEqual(len(snap['questions']), 1)
        question, = snap['questions']
        self.assertEqual((question['title'], question['grounding_quote']), ('合成交付日期', '合成西区交付日期？'))
        self.assertFalse(snap['detection_tasks'])
        with self.wb.store.transaction() as db:
            stored = self.wb.store._get(db, 'wb_detection_task', repair['detection_task']['id'])
            self.assertEqual(stored['state'], 'completed')
            self.assertEqual(stored['proposal'], broken)

    def test_punctuation_fallback_keeps_all_possible_original_sources(self):
        for reversed_refs in (False, True):
            with self.subTest(reversed_refs=reversed_refs):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='标点回退位置歧义', data_scope='synthetic'))['id']
                self.say('合成交付日期。', 'east')
                self.say('合成交付日期！', 'west')
                work = self.work()
                east, west = [self.action(work, '合成交付日期', index) for index in range(2)]
                refs = east['source_refs'] + west['source_refs']
                original = {**east, 'source_refs': list(reversed(refs)) if reversed_refs else refs,
                    'grounding_quote_id': '', 'grounding_quote': '合成交付日期？', 'extra': True}
                self.run_work(work, [original, *[{'kind': 'no_action'}] * (work['action_limit'] - 1)])
                correction = self.wb.detection_recovery_work(self.agent)
                self.run_work(correction, [])
                self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
                for _ in range(2):
                    scan = self.wb.detection_recovery_work(self.agent)
                    self.assertIsNotNone(scan)
                    self.run_work(scan, [west])
                snap = self.snapshot()
                self.assertFalse(snap['questions'])
                remaining, = snap['detection_tasks']
                self.assertEqual((remaining['kind'], remaining['state']), ('scan', 'needs_review'))
                self.assertEqual(remaining['issues'][0]['code'], 'overlapping_proposal')

    def test_omitted_revision_scope_uses_its_contract_default_for_dismissal(self):
        for explicit_scope in (False, True):
            with self.subTest(explicit_scope=explicit_scope):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='忽略范围修改保留单纯提及', data_scope='synthetic'))['id']
                question, work, mention = self.followup()
                original = {name: value for name, value in mention.items() if name != 'scope_changed'}
                if explicit_scope:
                    original['scope_changed'] = True
                original['extra'] = True
                self.run_work(work, [original, *[{'kind': 'no_action'}] * (work['action_limit'] - 1)])
                correction = self.wb.detection_recovery_work(self.agent)
                self.run_work(correction, [])
                self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
                scan = self.wb.detection_recovery_work(self.agent)
                self.run_work(scan, [mention])
                snap = self.snapshot()
                updated, = snap['questions']
                self.assertEqual(updated['id'], question['id'])
                self.assertEqual(updated.get('last_mention_refs'), mention['source_refs'])
                self.assertFalse(snap['detection_tasks'])

    def test_new_question_proposals_do_not_link_an_incidental_existing_question(self):
        for kind in ('candidate', 'create_question'):
            with self.subTest(kind=kind):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='新问题不跳转到附带编号', data_scope='synthetic'))['id']
                self.say('合成预算是多少？', 'budget')
                work = self.work()
                self.run_work(work, [self.action(work, '合成预算')])
                question, = self.snapshot()['questions']
                self.say('合成交付日期？', 'delivery')
                work = self.work()
                proposal = {**self.action(work, '合成交付日期'), 'kind': kind,
                    'question_id': question['id'], 'grounding_quote_id': 'invalid'}
                self.run_work(work, [proposal])
                task, = self.snapshot()['detection_tasks']
                self.assertEqual(task['proposal_title'], proposal['title'])
                self.assertEqual((task['question_id'], task['question_title']), ('', ''))

    def test_dismissing_scan_closes_a_covered_sibling_without_another_model_call(self):
        self.say('合成预算确定了吗？', 'candidate')
        work = self.work()
        self.run_work(work, [self.action(work, '合成预算')])
        question, = self.snapshot()['questions']
        for n in range(8):
            self.say('撤回合成预算问题？' if n == 0 else f'合成其他问题{n}？', f'window-{n}')
        work = self.work()
        proposal = {**self.action(work, '合成预算'), 'kind': 'cancel_candidate',
            'question_id': question['id'], 'expected_version': question['version'] + 1}
        self.run_work(work, [proposal])
        for _ in range(4):
            self.run_work(self.wb.detection_recovery_work(self.agent), [])
        rows = self.snapshot()['detection_tasks']
        ignored = next(task for task in rows if proposal['source_refs'][0] in task['source_refs'])
        sibling = next(task for task in rows if task['id'] != ignored['id'])
        calls_before = self.model.call.call_count
        self.wb.dismiss_detection_task(self.owner, self.mid, ignored['id'])
        self.assertFalse(self.snapshot()['detection_tasks'])
        self.assertIsNone(self.wb.detection_recovery_work(self.agent))
        self.assertEqual(self.model.call.call_count, calls_before)
        with self.wb.store.transaction() as db:
            stored = self.wb.store._get(db, 'wb_detection_task', sibling['id'])
            self.assertTrue(stored['scan_coverage_complete'])
            self.assertEqual((stored['state'], stored['known_failures'], stored['issues']), ('completed', [], []))

    def test_dismissing_scan_keeps_an_unscanned_sibling_pending(self):
        self.say('合成预算确定了吗？', 'candidate')
        work = self.work()
        self.run_work(work, [self.action(work, '合成预算')])
        question, = self.snapshot()['questions']
        for n in range(8):
            self.say('撤回合成预算问题？' if n == 0 else f'合成其他问题{n}？', f'window-{n}')
        work = self.work()
        proposal = {**self.action(work, '合成预算'), 'kind': 'cancel_candidate',
            'question_id': question['id'], 'expected_version': question['version'] + 1}
        self.run_work(work, [proposal])
        first = self.wb.detection_recovery_work(self.agent)
        self.run_work(first, [])
        with self.wb.store.transaction() as db:
            sibling = next(t for t in self.wb._detection_tasks(db, self.mid) if t['id'] != first['detection_task']['id'])
            sibling['next_at'] = 10 ** 12  # Keep the second window unclaimed while the first exhausts.
            self.wb.store._put(db, 'wb_detection_task', sibling['id'], sibling)
        self.run_work(self.wb.detection_recovery_work(self.agent), [])
        self.wb.dismiss_detection_task(self.owner, self.mid, first['detection_task']['id'])
        pending, = self.snapshot()['detection_tasks']
        self.assertEqual((pending['id'], pending['state'], pending['attempts']), (sibling['id'], 'pending', 0))
        with self.wb.store.transaction() as db:
            stored = self.wb.store._get(db, 'wb_detection_task', sibling['id'])
            self.assertFalse(stored.get('scan_coverage_complete'))
            self.assertFalse(stored['known_failures'])
            stored['next_at'] = 0
            self.wb.store._put(db, 'wb_detection_task', stored['id'], stored)
        self.run_work(self.wb.detection_recovery_work(self.agent), [])
        self.assertFalse(self.snapshot()['detection_tasks'])

    def test_rejected_response_cannot_close_a_distinct_same_title_correction(self):
        for rejection in ('missing_external_fact', 'source_not_in_new_segment'):
            with self.subTest(rejection=rejection):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='无效纠正不能关闭独立问题', data_scope='synthetic'))['id']
                if rejection == 'source_not_in_new_segment':
                    self.say('合成东区交付日期？', 'east')
                    self.say('合成西区交付日期？', 'west')
                else:
                    self.say('合成东区交付日期？合成西区交付日期？', 'both')
                work = self.work()
                east, west = [{**self.action(work, '合成交付日期', index),
                    'kind': 'create_question', 'evidence_need': 'public'} for index in range(2)]
                self.run_work(work, [east, west])
                ignored = self.wb.detection_recovery_work(self.agent)
                self.run_work(ignored, [])
                correction = self.wb.detection_recovery_work(self.agent)
                self.assertEqual(correction['detection_task']['proposal'], west)
                self.wb.dismiss_detection_task(self.owner, self.mid, ignored['detection_task']['id'])
                response = dict(east)
                if rejection == 'source_not_in_new_segment':
                    response['external_fact'] = '合成交付日期'
                parsed = parse_detection({'actions': [response]}, correction,
                    quote_options(correction['segments'], correction['meeting']['coordinator_cursor']))
                self.assertFalse(parsed.actions)
                self.assertEqual(parsed.failures[0]['issues'][0]['code'], rejection)
                self.run_work(correction, [response])
                remaining, = self.snapshot()['detection_tasks']
                self.assertEqual((remaining['id'], remaining['state']), (correction['detection_task']['id'], 'needs_review'))
                self.assertFalse(self.snapshot()['questions'])
                with self.wb.store.transaction() as db:
                    stored = self.wb.store._get(db, 'wb_detection_task', remaining['id'])
                    self.assertEqual(stored['proposal'], west)
                    self.assertNotIn('superseded_by', stored)

    def test_valid_wrong_quote_keeps_a_distinct_correction_for_review(self):
        self.say('合成东区交付日期？合成西区交付日期？')
        work = self.work()
        east, west = [{**self.action(work, '合成交付日期', index),
            'kind': 'create_question', 'evidence_need': 'public'} for index in range(2)]
        self.run_work(work, [east, west])
        ignored = self.wb.detection_recovery_work(self.agent)
        self.run_work(ignored, [])
        correction = self.wb.detection_recovery_work(self.agent)
        self.assertEqual(correction['detection_task']['proposal'], west)
        self.wb.dismiss_detection_task(self.owner, self.mid, ignored['detection_task']['id'])
        wrong = {**east, 'external_fact': '合成交付日期'}
        parsed = parse_detection({'actions': [wrong]}, correction,
            quote_options(correction['segments'], correction['meeting']['coordinator_cursor']))
        self.assertEqual(len(parsed.actions), 1)
        self.run_work(correction, [wrong])
        remaining, = self.snapshot()['detection_tasks']
        self.assertEqual((remaining['id'], remaining['state']), (correction['detection_task']['id'], 'needs_review'))
        self.assertEqual(remaining['issues'][0]['code'], 'correction_changed_intent')
        self.assertTrue(remaining['issues'][0]['field'].endswith('grounding_quote'))
        self.assertFalse(self.snapshot()['questions'])
        with self.wb.store.transaction() as db:
            stored = self.wb.store._get(db, 'wb_detection_task', remaining['id'])
            self.assertEqual(stored['proposal'], west)
            self.assertNotIn('superseded_by', stored)
        self.wb.retry_detection_task(self.owner, self.mid, remaining['id'])
        retry = self.wb.detection_recovery_work(self.agent)
        self.run_work(retry, [{**west, 'external_fact': '合成交付日期'}])
        question, = self.snapshot()['questions']
        self.assertEqual(question['grounding_quote'], '合成西区交付日期？')
        self.assertFalse(self.snapshot()['detection_tasks'])

    def test_unused_revision_fields_cannot_revive_a_dismissed_new_question(self):
        for kind in ('candidate', 'create_question'):
            for field, original_value in (('expected_version', 2), ('scope_changed', False), ('question_id', 'placeholder')):
                with self.subTest(kind=kind, field=field):
                    self.wb.control(self.owner, self.mid, 'pause_research')
                    self.mid = self.wb.create(self.owner, NewMeeting(title='无关字段不能复活新问题', data_scope='synthetic'))['id']
                    self.say('合成第一问？合成第二问？合成第三问？合成交付日期？')
                    work = self.work()
                    original = {**self.action(work, '合成交付日期', 3), 'kind': kind,
                        'evidence_need': 'public', field: original_value}
                    self.run_work(work, [*[self.action(work, f'合成第{index+1}问', index) for index in range(3)], original])
                    ignored = self.wb.detection_recovery_work(self.agent)
                    self.run_work(ignored, [])
                    scan = self.wb.detection_recovery_work(self.agent)
                    self.wb.dismiss_detection_task(self.owner, self.mid, ignored['detection_task']['id'])
                    response = {**original, 'external_fact': '合成交付日期'}
                    response.pop(field)
                    self.run_work(scan, [response])
                    snap = self.snapshot()
                    self.assertEqual({question['title'] for question in snap['questions']},
                        {'合成第1问', '合成第2问', '合成第3问'})
                    self.assertFalse(snap['detection_tasks'])
                    self.assertEqual(snap['automation_progress']['dismissed'], 1)

    def test_malformed_action_kinds_do_not_block_a_valid_scan_sibling(self):
        for invalid_kind in ([], {'unexpected': 'candidate'}):
            with self.subTest(invalid_kind=invalid_kind):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='坏类型不阻塞独立问题', data_scope='synthetic'))['id']
                self.say('合成第一问？合成第二问？合成第三问？待忽略问题？合成独立问题？')
                work = self.work()
                original = {**self.action(work, '待忽略问题', 3), 'grounding_quote_id': 'absent'}
                self.run_work(work, [*[self.action(work, f'合成第{index+1}问', index) for index in range(3)], original])
                correction = self.wb.detection_recovery_work(self.agent)
                self.run_work(correction, [])
                self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
                scan = self.wb.detection_recovery_work(self.agent)
                self.run_work(scan, [{**self.action(scan, original['title'], 3), 'kind': invalid_kind},
                    self.action(scan, '合成独立问题', 4)])
                snap = self.snapshot()
                self.assertEqual({question['title'] for question in snap['questions']},
                    {'合成第1问', '合成第2问', '合成第3问', '合成独立问题'})
                self.assertFalse(snap['detection_tasks'])
                self.assertEqual(snap['automation_progress']['dismissed'], 1)

    def test_malformed_original_kind_does_not_leave_a_duplicate_correction_running(self):
        for callback in ('commit', 'failure'):
            with self.subTest(callback=callback):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='坏类型重复纠正可收尾', data_scope='synthetic'))['id']
                self.say('合成第一问？合成第二问？合成第三问？待忽略问题？')
                work = self.work()
                original = {**self.action(work, '待忽略问题', 3), 'kind': {'unexpected': 'candidate'}}
                self.run_work(work, [*[self.action(work, f'合成第{index+1}问', index) for index in range(3)], original])
                correction = self.wb.detection_recovery_work(self.agent)
                self.run_work(correction, [])
                scan = self.wb.detection_recovery_work(self.agent)
                duplicate_proposal = {**original, 'grounding_quote_id': 'absent'}
                self.run_work(scan, [duplicate_proposal])
                duplicate = self.wb.detection_recovery_work(self.agent)
                self.assertEqual(duplicate['detection_task']['kind'], 'correction')
                self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
                if callback == 'commit':
                    self.run_work(duplicate, [])
                else:
                    self.wb.detection_recovery_failed(self.agent, duplicate,
                        {'stage': 'saving', 'code': 'local_save_failed'})
                self.assertFalse(self.snapshot()['detection_tasks'])
                with self.wb.store.transaction() as db:
                    stored = self.wb.store._get(db, 'wb_detection_task', duplicate['detection_task']['id'])
                    self.assertEqual((stored['state'], stored['superseded_by'], stored['lease']),
                        ('superseded', correction['detection_task']['id'], None))
                    self.assertEqual(stored['proposal'], duplicate_proposal)

    def test_malformed_question_id_cannot_revive_a_dismissed_revision(self):
        for invalid_id in (['candidate'], {'target': 'candidate'}, 7,
                'question-0000000000000000', '0123456789abcdef'):
            with self.subTest(invalid_id=invalid_id):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='坏编号不复活已忽略修订', data_scope='synthetic'))['id']
                self.say('合成材料范围？', 'initial')
                initial = self.work()
                self.run_work(initial, [self.action(initial, '合成材料范围')])
                question, = self.snapshot()['questions']
                self.say('合成第一问？合成第二问？合成第三问？合成材料新范围？', 'followup')
                work = self.work()
                original = {**self.action(work, '合成材料新范围', 3), 'kind': 'revise_question',
                    'question_id': invalid_id, 'expected_version': question['version']}
                self.run_work(work, [*[self.action(work, f'合成第{index+1}问', index) for index in range(3)], original])
                correction = self.wb.detection_recovery_work(self.agent)
                self.run_work(correction, [])
                self.wb.dismiss_detection_task(self.owner, self.mid, correction['detection_task']['id'])
                scan = self.wb.detection_recovery_work(self.agent)
                self.run_work(scan, [{**original, 'question_id': question['id']}])
                snap = self.snapshot()
                current = next(item for item in snap['questions'] if item['id'] == question['id'])
                self.assertEqual(current, question)
                self.assertFalse(snap['detection_tasks'])
                self.assertEqual(snap['automation_progress']['dismissed'], 1)

    def test_literal_dismissal_excludes_repeated_older_context(self):
        for literal_original in (False, True):
            with self.subTest(literal_original=literal_original):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='旧上下文不造成原话歧义', data_scope='synthetic'))['id']
                self.say('合成交付日期？', 'old')
                old_work = self.work()
                old_ref = self.action(old_work, '旧上下文')['source_refs'][0]
                self.run_work(old_work, [])
                self.say('合成第一问？合成第二问？合成第三问？合成交付日期？', 'new')
                work = self.work()
                original = {**self.action(work, '合成交付日期', 3), 'kind': 'create_question', 'evidence_need': 'public'}
                original['source_refs'].append(old_ref)
                if literal_original:
                    original.update(grounding_quote_id='', grounding_quote='合成交付日期？')
                self.run_work(work, [*[self.action(work, f'合成第{index+1}问', index) for index in range(3)], original])
                ignored = self.wb.detection_recovery_work(self.agent)
                self.run_work(ignored, [])
                self.wb.dismiss_detection_task(self.owner, self.mid, ignored['detection_task']['id'])
                scan = self.wb.detection_recovery_work(self.agent)
                response = {**original, 'grounding_quote_id': '', 'grounding_quote': '合成交付日期？',
                    'external_fact': '合成交付日期'}
                parsed = parse_detection({'actions': [response]}, scan,
                    quote_options(scan['segments'], scan['meeting']['coordinator_cursor']))
                self.assertEqual(len(parsed.actions), 1)
                self.run_work(scan, [response])
                snap = self.snapshot()
                self.assertEqual({question['title'] for question in snap['questions']},
                    {'合成第1问', '合成第2问', '合成第3问'})
                self.assertFalse(snap['detection_tasks'])
                self.assertEqual(snap['automation_progress']['dismissed'], 1)

    def test_same_question_distinct_revision_uses_its_own_quote(self):
        self.say('合成交付日期？', 'initial')
        work = self.work()
        self.run_work(work, [self.action(work, '合成交付日期')])
        question, = self.snapshot()['questions']
        self.say('合成第一问？合成第二问？合成第三问？东区交付日期是什么？西区交付日期是什么？', 'updates')
        work = self.work()
        revision = {'kind': 'revise_question', 'question_id': question['id'],
            'expected_version': question['version'], 'scope_changed': True, 'evidence_need': 'public'}
        east = {**self.action(work, '合成东区交付日期', 3), **revision}
        self.run_work(work, [*[self.action(work, f'合成第{index+1}问', index) for index in range(3)], east])
        ignored = self.wb.detection_recovery_work(self.agent)
        self.run_work(ignored, [])
        self.wb.dismiss_detection_task(self.owner, self.mid, ignored['detection_task']['id'])
        scan = self.wb.detection_recovery_work(self.agent)
        west = {**self.action(scan, '合成西区交付日期', 4), **revision, 'external_fact': '合成西区交付日期'}
        parsed = parse_detection({'actions': [west]}, scan,
            quote_options(scan['segments'], scan['meeting']['coordinator_cursor']))
        self.assertEqual(len(parsed.actions), 1)
        self.run_work(scan, [west])
        snap = self.snapshot()
        updated = next(item for item in snap['questions'] if item['id'] == question['id'])
        self.assertEqual((updated['title'], updated['version']), (west['title'], question['version'] + 1))
        self.assertEqual(updated['grounding_quote'], '西区交付日期是什么？')
        self.assertFalse(snap['detection_tasks'])
        self.assertEqual(snap['automation_progress']['dismissed'], 1)

    def test_multiple_dismissed_ranges_clear_only_fully_covered_shared_failures(self):
        for remainder in ('none', 'other_range', 'unknown', 'unscanned'):
            with self.subTest(remainder=remainder):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='多个忽略范围的共享失败', data_scope='synthetic'))['id']
                for index in range(12):
                    self.say(f'合成范围问题{index}？', str(index))
                work = self.work()
                self.run_work(work, [])
                shared = self.action(work, '合成跨范围问题')
                shared['source_refs'] += self.action(work, shared['title'], 5)['source_refs']
                failures = [{'proposal': shared, 'issues': [{'field': 'actions', 'code': 'context_stale'}], 'batch_scope': True}]
                independent = None
                if remainder in ('other_range', 'unknown'):
                    independent = self.action(work, '合成独立待处理问题', 8)
                    if remainder == 'unknown':
                        independent['source_refs'] = [{'segment_id': 'unknown', 'revision': 1}]
                    failures.append({'proposal': independent,
                        'issues': [{'field': 'actions', 'code': 'context_stale'}], 'batch_scope': True})
                with self.wb.store.transaction() as db:
                    meeting = self.wb._meeting(db, self.owner, self.mid, True)
                    for start in range(0, 12, 4):
                        self.wb._save_detection_task(db, meeting, work, 'scan', work['segments'][start:start+4],
                            known_failures=failures, suffix=str(start))
                    tasks = self.wb._detection_tasks(db, self.mid)
                    third_ref = self.action(work, '第三范围', 8)['source_refs'][0]
                    for task in tasks:
                        unscanned = remainder == 'unscanned' and third_ref in task['source_refs']
                        task.update(state='pending' if unscanned else 'needs_review',
                            attempts=0 if unscanned else 2, scan_coverage_complete=not unscanned)
                        self.wb.store._put(db, 'wb_detection_task', task['id'], task)
                    self.wb._save(db, meeting)
                rows = self.snapshot()['detection_tasks']
                third = next(task for task in rows if third_ref in task['source_refs'])
                self.assertEqual(third['proposal_titles'], [independent['title']] if remainder == 'other_range' else [])
                self.assertIn(shared['title'], third['batch_proposal_titles'])
                for row in rows:
                    if row['id'] != third['id']:
                        self.wb.dismiss_detection_task(self.owner, self.mid, row['id'])
                with self.wb.store.transaction() as db:
                    stored = self.wb.store._get(db, 'wb_detection_task', third['id'])
                    self.assertEqual(stored['known_failures'], failures[1:])
                    self.assertEqual(stored['state'], 'pending' if remainder == 'unscanned'
                        else 'needs_review' if independent else 'completed')
                if remainder == 'unscanned':
                    self.assertFalse(stored['scan_coverage_complete'])
                    scan = self.wb.detection_recovery_work(self.agent)
                    self.assertEqual(scan['detection_task']['id'], third['id'])
                    self.run_work(scan, [])
                self.assertEqual(len(self.snapshot()['detection_tasks']), int(independent is not None))

    def test_scan_dismissal_closes_covered_corrections_and_fences_running_callbacks(self):
        for correction_state in ('needs_review', 'pending', 'running_commit', 'running_failure'):
            with self.subTest(correction_state=correction_state):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='范围忽略同步收尾纠正', data_scope='synthetic'))['id']
                self.say('合成第一问？合成第二问？合成第三问？合成交付日期？')
                work = self.work()
                original = {**self.action(work, '合成交付日期', 3), 'evidence_need': 'public'}
                self.run_work(work, [*[self.action(work, f'合成第{index+1}问', index) for index in range(3)], original])
                correction = None
                if correction_state == 'pending':
                    with self.wb.store.transaction() as db:
                        pending = next(task for task in self.wb._detection_tasks(db, self.mid) if task['kind'] == 'correction')
                        pending['next_at'] = 10 ** 12
                        self.wb.store._put(db, 'wb_detection_task', pending['id'], pending)
                    correction_id = pending['id']
                else:
                    correction = self.wb.detection_recovery_work(self.agent)
                    correction_id = correction['detection_task']['id']
                    if correction_state == 'needs_review':
                        self.run_work(correction, [])
                for _ in range(2):
                    scan = self.wb.detection_recovery_work(self.agent)
                    self.assertEqual(scan['detection_task']['kind'], 'scan')
                    self.run_work(scan, [None])
                before = self.snapshot()
                self.wb.dismiss_detection_task(self.owner, self.mid, scan['detection_task']['id'])
                self.assertFalse(self.snapshot()['detection_tasks'])
                if correction_state == 'running_commit':
                    self.run_work(correction, [{**original, 'external_fact': '合成交付日期'}])
                elif correction_state == 'running_failure':
                    self.wb.detection_recovery_failed(self.agent, correction, {'stage': 'saving', 'code': 'local_save_failed'})
                snap = self.snapshot()
                self.assertEqual((snap['questions'], snap['segments']), (before['questions'], before['segments']))
                self.assertFalse(snap['detection_tasks'])
                self.assertIsNone(self.wb.detection_recovery_work(self.agent))
                with self.wb.store.transaction() as db:
                    stored = self.wb.store._get(db, 'wb_detection_task', correction_id)
                    self.assertEqual((stored['state'], stored['superseded_by'], stored['lease']),
                        ('superseded', scan['detection_task']['id'], None))
                    self.assertEqual(stored['proposal'], original)
                    events = [json.loads(row[0]) for row in db.execute("SELECT data FROM events WHERE meeting=?", (self.mid,))]
                    self.assertEqual(sum(event['type'] == 'detection.superseded'
                        and event['payload']['task_id'] == correction_id for event in events), 1)

    def test_scan_dismissal_keeps_corrections_with_uncovered_sources(self):
        for additional_source in ('other_range', 'unknown', 'later'):
            with self.subTest(additional_source=additional_source):
                self.wb.control(self.owner, self.mid, 'pause_research')
                self.mid = self.wb.create(self.owner, NewMeeting(title='范围外纠正仍需核对', data_scope='synthetic'))['id']
                self.say('合成当前范围？', 'current')
                self.say('合成后续范围？', 'next')
                work = self.work()
                original = self.action(work, '合成跨范围问题')
                extra = {'segment_id': 'unknown', 'revision': 1} if additional_source == 'unknown' else self.action(work, '后续范围', 1)['source_refs'][0]
                original['source_refs'].append(extra)
                self.run_work(work, [])
                with self.wb.store.transaction() as db:
                    meeting = self.wb._meeting(db, self.owner, self.mid, True)
                    targets = work['segments'] if additional_source == 'other_range' else work['segments'][:1]
                    self.wb._save_detection_task(db, meeting, work, 'correction', targets,
                        proposal=original, issues=[{'field': 'actions', 'code': 'context_stale'}])
                    self.wb._save_detection_task(db, meeting, work, 'scan', work['segments'][:1])
                    tasks = self.wb._detection_tasks(db, self.mid)
                    for task in tasks:
                        task.update(state='needs_review', attempts=2)
                        self.wb.store._put(db, 'wb_detection_task', task['id'], task)
                    scan = next(task for task in tasks if task['kind'] == 'scan')
                    correction = next(task for task in tasks if task['kind'] == 'correction')
                    self.wb._save(db, meeting)
                self.wb.dismiss_detection_task(self.owner, self.mid, scan['id'])
                remaining, = self.snapshot()['detection_tasks']
                self.assertEqual((remaining['id'], remaining['state']), (correction['id'], 'needs_review'))
                with self.wb.store.transaction() as db:
                    stored = self.wb.store._get(db, 'wb_detection_task', correction['id'])
                    self.assertEqual(stored['proposal'], original)
                    self.assertNotIn('superseded_by', stored)

    def test_running_cross_range_correction_cannot_narrow_into_an_ignored_scan(self):
        for index in range(8):
            self.say(f'合成范围问题{index}？', f's-{index}')
        work = self.work()
        ignored_proposal = self.action(work, '合成跨范围问题', 0)
        original = {**self.action(work, ignored_proposal['title'], 7), 'evidence_need': 'public'}
        original['source_refs'] += ignored_proposal['source_refs']
        self.run_work(work, [original, {'kind': 'no_action', 'extra': 1}])
        correction = self.wb.detection_recovery_work(self.agent)
        self.assertEqual(correction['detection_task']['proposal'], original)
        for _ in range(4):
            self.run_work(self.wb.detection_recovery_work(self.agent), [None])
        scan = next(task for task in self.snapshot()['detection_tasks']
            if task['kind'] == 'scan' and ignored_proposal['source_refs'][0] in task['source_refs'])
        self.wb.dismiss_detection_task(self.owner, self.mid, scan['id'])
        wrong = {**ignored_proposal, 'evidence_need': 'public', 'external_fact': '合成范围事实'}
        parsed = parse_detection({'actions': [wrong]}, correction,
            quote_options(correction['segments'], correction['meeting']['coordinator_cursor']))
        self.assertEqual(len(parsed.actions), 1)
        self.run_work(correction, [wrong])
        remaining = next(task for task in self.snapshot()['detection_tasks']
            if task['id'] == correction['detection_task']['id'])
        self.assertEqual(remaining['state'], 'needs_review')
        self.assertEqual(remaining['issues'][0]['code'], 'correction_changed_intent')
        self.assertFalse(self.snapshot()['questions'])
        with self.wb.store.transaction() as db:
            stored = self.wb.store._get(db, 'wb_detection_task', remaining['id'])
            self.assertEqual(stored['proposal'], original)
            self.assertNotIn('superseded_by', stored)
        self.wb.retry_detection_task(self.owner, self.mid, remaining['id'])
        retry = self.wb.detection_recovery_work(self.agent)
        self.assertEqual(retry['detection_task']['id'], remaining['id'])
        self.run_work(retry, [{**original, 'external_fact': '合成范围事实'}])
        question, = self.snapshot()['questions']
        self.assertEqual(question['grounding_quote'], '合成范围问题7？')
        with self.wb.store.transaction() as db:
            stored = self.wb.store._get(db, 'wb_detection_task', remaining['id'])
            self.assertEqual(stored['state'], 'completed')

    def test_scan_review_separates_unknown_batch_questions_from_its_source_range(self):
        self.say('合成预算？合成工期？', 'range')
        work = self.work()
        proposals = [{**self.action(work, title, index),
            'source_refs': [{'segment_id': 'absent', 'revision': 1}]}
            for index, title in enumerate(('合成预算', '合成工期'))]
        self.run_work(work, proposals)
        for _ in range(2):
            self.run_work(self.wb.detection_recovery_work(self.agent), [])
        task, = self.snapshot()['detection_tasks']
        self.assertEqual((task['kind'], task['state']), ('scan', 'needs_review'))
        self.assertEqual(task['proposal_titles'], [])
        self.assertEqual(task['batch_proposal_titles'], ['合成预算', '合成工期'])
        self.assertEqual((task['proposal_title'], task['question_id']), ('', ''))
        self.assertEqual(task['source_refs'], [{'segment_id': work['segments'][0]['id'], 'revision': 1}])

    def test_dismiss_api_checks_origin_identity_meeting_and_current_state(self):
        task = self.review()
        client = self.app.test_client()
        headers = {'Authorization': 'Bearer fixture-owner-0'}
        path = f'/live/v2/meetings/{self.mid}/detection-tasks/{task["id"]}/dismiss'
        self.assertEqual(client.post(path, json={}).status_code, 401)
        self.assertEqual(client.post(path, json={}, headers={**headers, 'Origin': 'https://evil.invalid'}).status_code, 403)
        self.assertEqual(client.post(path, json={}, headers={'Authorization': 'Bearer fixture-agent-3'}).status_code, 403)
        other = self.wb.create(self.owner, NewMeeting(title='另一场会议', data_scope='synthetic'))['id']
        wrong_path = path.replace(self.mid, other)
        self.assertEqual(client.post(wrong_path, json={}, headers=headers).status_code, 409)
        self.assertEqual(client.post(path, json={'state': 'completed'}, headers=headers).status_code, 400)
        self.wb.retry_detection_task(self.owner, self.mid, task['id'])
        self.assertEqual(client.post(path, json={}, headers=headers).status_code, 409)
        retry = self.wb.detection_recovery_work(self.agent)
        self.assertEqual(client.post(path, json={}, headers=headers).status_code, 409)
        self.run_work(retry, [])
        self.assertEqual(client.post(path, json={}, headers=headers).status_code, 200)


if __name__ == '__main__':
    unittest.main()
