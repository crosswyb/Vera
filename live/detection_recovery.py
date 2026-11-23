"""Durable detection gaps. Local commits are atomic; model calls remain outside them."""

import hashlib
import copy
import json
import time

from pydantic import ValidationError

from live.detection import DetectionBatch, check_correction_intent, literal_quote, normalize_question_reference, quote_options
from live.output_validation import OutputValidationError
from live.store import LiveError, require, uid
from live.workbench_contracts import Decisions, QuestionDecision, SourceRef


DETECTION_TERMINAL_STATES = frozenset({'completed', 'superseded', 'dismissed'})


def identity(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def refs_of(segments):
    return [{'segment_id': s['id'], 'revision': s['revision']} for s in segments]


def proposal_refs(proposal):
    cited = proposal.get('source_refs') if isinstance(proposal, dict) else None
    if not isinstance(cited, list) or not cited or not all(isinstance(ref, dict) for ref in cited):
        return None
    try:
        validated = [SourceRef.model_validate({name: ref[name] for name in ('segment_id', 'revision') if name in ref})
            for ref in cited]
    except ValidationError:
        return None
    return {(ref.segment_id, ref.revision) for ref in validated}


def grounding_spans(proposal, source_keys, eligible, segments):
    if not isinstance(proposal, dict) or not source_keys or not source_keys.issubset(segments):
        return set()
    ordered_keys = [(ref['segment_id'], ref['revision']) for ref in proposal['source_refs']]
    cited = [{**segments[key], 'is_new': True} for key in ordered_keys
        if key in source_keys & eligible and key in segments]
    quote_id = proposal.get('grounding_quote_id')
    if isinstance(quote_id, str) and quote_id:
        option = next((option for option in quote_options(cited, -1) if option['id'] == quote_id), None)
        if option:
            return {(option['segment_id'], option['revision'], option['start'], option['end'])}
    quote = proposal.get('grounding_quote')
    if not isinstance(quote, str):
        return set()
    quote = quote.strip()
    matched = literal_quote(cited, quote)
    if not matched:
        return set()
    exact = matched == quote
    fragment = quote if exact else quote.rstrip('。？！?!')
    spans = set()
    for segment in cited:
        start = 0
        while (position := segment['text'].find(fragment, start)) >= 0:
            start = position + 1
            end = position + len(fragment)
            if not exact:
                # An ignored raw quote may refer to any punctuation fallback.
                if end < len(segment['text']) and segment['text'][end] not in '。？！?!':
                    continue
                end = min(end + 1, len(segment['text']))
            spans.add((segment['id'], segment['revision'], position, end))
    return spans


class DetectionRecovery:
    @staticmethod
    def detection_service_ready(meeting):
        retry = meeting.get('detection_service_retry', {})
        return retry.get('state') != 'exhausted' and retry.get('next_at', 0) <= time.time()

    @staticmethod
    def clear_detection_service_error(meeting):
        meeting.pop('detection_service_retry', None)
        meeting.pop('detection_service_error', None)

    def _detection_tasks(self, db, mid):
        return self._items(db, 'wb_detection_task', mid)

    def _dismissal_context(self, db, mid, segments, dismissed):
        if not dismissed:
            return segments
        current = {(s['id'], s['revision']): s for s in self._items(db, 'wb_segment', mid)
            if s['type'] == 'transcript.final'}
        segments = [s for s in segments if (s['id'], s['revision']) in current]
        visible = {(segment['id'], segment['revision']) for segment in segments}
        pinned = set()
        for task in dismissed:
            pinned.update(proposal_refs(task.get('proposal')) or set())
            pinned.update(proposal_refs({'source_refs': task['source_refs']}) or set())
        missing = pinned - visible
        # Matching may need an ignored proposal's clipped context. Supplement
        # only its unchanged revisions, without expanding the scan's new input.
        supplement = [{**current[key], 'is_new': False} for key in missing if key in current]
        return [*segments, *supplement]

    def _scan_batch_scope(self, db, mid, family):
        # Families split one batch's new input into task ranges. Only verified
        # earlier utterances can be context; unknown or later refs stay active.
        tasks = [task for task in self._detection_tasks(db, mid) if task['family'] == family]
        batch_refs = {(ref['segment_id'], ref['revision']) for task in tasks
            for ref in [*task['source_refs'], *task.get('original_source_refs', [])]}
        current = {(segment['id'], segment['revision']): segment['event_seq']
            for segment in self._items(db, 'wb_segment', mid) if segment['type'] == 'transcript.final'}
        batch_refs.intersection_update(current)
        first_event = min((current[key] for key in batch_refs), default=None)
        older_context = {key for key, event in current.items() if first_event is not None and event < first_event}
        dismissed_refs = {(ref['segment_id'], ref['revision']) for task in tasks
            if task['kind'] == 'scan' and task['state'] == 'dismissed' for ref in task['source_refs']}
        return batch_refs, older_context, dismissed_refs

    def invalidate_detection_tasks(self, db, meeting, segment_id, revision):
        for task in self._detection_tasks(db, meeting['id']):
            if task['state'] in DETECTION_TERMINAL_STATES:
                continue
            remaining = [ref for ref in task['source_refs']
                if ref['segment_id'] != segment_id or ref['revision'] == revision]
            if len(remaining) == len(task['source_refs']):
                continue
            task.setdefault('original_source_refs', task['source_refs'])
            task['source_refs'] = remaining
            if not remaining:
                task['state'] = 'superseded'
            elif task['state'] == 'running':
                task['state'] = 'pending'
                task['attempts'] = max(0, task['attempts'] - 1)
            task.update(lease=None, updated_at=time.time())
            self.store._put(db, 'wb_detection_task', task['id'], task)

    def _save_detection_task(self, db, m, work, kind, targets, *, proposal=None, issues=None, suffix='', known_failures=None):
        if not targets:
            return
        refs = refs_of(targets)
        parent = work.get('detection_task', {})
        family = parent.get('family') or identity([m['id'], work['meeting']['coordinator_cursor'], work['cursor']])
        key = 'detection-' + identity([family, kind, refs, proposal, suffix])
        if db.execute("SELECT 1 FROM records WHERE kind='wb_detection_task' AND id=?", (key,)).fetchone():
            return
        visible = work['segments']
        positions = [i for i, s in enumerate(visible) if s['id'] in {r['segment_id'] for r in refs}]
        context = visible[max(0, min(positions) - 6):max(positions) + 7] if positions else visible
        task = {'id': key, 'meeting_id': m['id'], 'kind': kind, 'family': family,
            'source_refs': refs, 'context_refs': refs_of(context), 'proposal': proposal,
            'issues': issues or [], 'state': 'pending', 'attempts': 0,
            'max_attempts': 1 if kind == 'correction' else 2, 'created_at': time.time(),
            'updated_at': time.time(), 'next_at': 0, 'lease': None}
        if known_failures:
            task['known_failures'] = known_failures
        self.store._put(db, 'wb_detection_task', key, task)
        m['detection_protocol'] = 1
        self._event(db, m, 'detection.deferred', {'task_id': key, 'kind': kind, 'segment_count': len(refs)})

    def _queue_located_correction(self, db, m, work, new, failure):
        proposal = failure.get('proposal')
        if not isinstance(proposal, dict) or any(i['code'] == 'overlapping_proposal' for i in failure['issues']):
            return False  # The scan's overlap check must apply on every retry.
        if proposal.get('kind') == 'cancel_candidate':
            return False  # Cancellation is outside the single-question correction contract.
        cited = proposal.get('source_refs')
        allowed = {(s['id'], s['revision']) for s in work['segments']}
        if not isinstance(cited, list) or not cited or not all(
                isinstance(r, dict) and isinstance(r.get('segment_id'), str) and type(r.get('revision')) is int
                and (r['segment_id'], r['revision']) in allowed for r in cited):
            return False
        cited_ids = {r['segment_id'] for r in cited}
        targets = [s for s in new if s['id'] in cited_ids]
        if not targets:
            return False
        self._save_detection_task(db, m, work, 'correction', targets, proposal=proposal, issues=failure['issues'])
        return True

    def _defer_detection(self, db, m, work, result):
        new = [s for s in work['segments'] if s.get('is_new', s.get('event_seq', work['cursor']) > work['meeting']['coordinator_cursor'])]
        remaining = [f for f in result.failures if not self._queue_located_correction(db, m, work, new, f)]
        if remaining or result.saturated:
            known = [{**f, 'batch_scope': len(new) > 4} for f in remaining if isinstance(f.get('proposal'), dict)]
            for i in range(0, len(new), 4):
                self._save_detection_task(db, m, work, 'scan', new[i:i + 4],
                    issues=[{'field': 'actions', 'code': 'scan_incomplete'}], suffix=str(i),
                    known_failures=known)

    @staticmethod
    def _matches_failed_proposal(action, proposal, questions):
        proposal = normalize_question_reference(proposal, questions)
        if action.kind == 'cancel_candidate':
            return (proposal.get('kind') == action.kind and action.question_id == proposal.get('question_id')
                    and action.expected_version == proposal.get('expected_version', 1))
        title = proposal.get('title')
        if not isinstance(title, str) or title.strip() != action.title.strip() or not title.strip():
            return False
        try:
            check_correction_intent(proposal, action)
        except OutputValidationError:
            return False
        return True

    @staticmethod
    def _dismissed_proposal_relation(proposal, task, questions, segments, scan_scope=None):
        if not isinstance(proposal, dict):
            return None
        proposal = normalize_question_reference(proposal, questions)

        refs = proposal_refs(proposal)
        if not refs:
            return None
        dismissed_refs = {(ref['segment_id'], ref['revision']) for ref in task['source_refs']}
        if task['kind'] == 'scan' and scan_scope is not None:
            dismissed_refs = scan_scope[2]
        if not refs.intersection(dismissed_refs):
            return None
        if task['kind'] == 'scan':
            if scan_scope is None:
                return 'same' if refs.issubset(dismissed_refs) else None
            batch_refs, older_context, _ = scan_scope
            new_refs = refs - older_context
            return 'same' if new_refs and new_refs.issubset(batch_refs & dismissed_refs) else None
        original = normalize_question_reference(task.get('proposal'), questions)
        if not isinstance(original, dict):
            return None
        original_refs = proposal_refs(original)
        if not original_refs:
            return None
        targeted = (proposal.get('kind') in ('revise_question', 'cancel_candidate')
            and original.get('kind') in ('revise_question', 'cancel_candidate'))
        fields = ['kind', 'evidence_need']
        if targeted:
            fields.extend(('question_id', 'expected_version'))
            if proposal.get('kind') == original.get('kind') == 'revise_question':
                fields.append('scope_changed')
        public_intent = any(value.get('evidence_need') in ('public', 'mixed') for value in (proposal, original))
        if public_intent:
            fields.extend(('external_fact', 'public_query', 'original_target'))
        question_ids = {question['id'] for question in questions}
        for name in fields:
            previous = original.get(name)
            if name not in original and name in ('expected_version', 'scope_changed') and targeted:
                previous = QuestionDecision.model_fields[name].default
            if previous is None:
                continue
            value = proposal.get(name, QuestionDecision.model_fields[name].default)
            if name == 'question_id' and not (isinstance(previous, str) and previous in question_ids
                    and isinstance(value, str) and value in question_ids):
                continue  # Unknown targets cannot establish a distinct question.
            if name == 'public_query' and not (isinstance(previous, str) and previous.strip()
                    and isinstance(value, str) and value.strip()):
                continue  # An optional query may be omitted without changing intent.
            try:
                # A malformed sibling field must not hide a valid distinction.
                candidate = QuestionDecision.model_validate({'kind': 'candidate', 'evidence_need': 'public', name: value})
                if name == 'kind':
                    prior = QuestionDecision.model_validate({'kind': previous})
                    if {prior.kind, candidate.kind}.issubset({'candidate', 'create_question'}):
                        continue  # Classification does not identify a new question.
                    if prior.kind != candidate.kind:
                        return 'different'
                    continue
                check_correction_intent({name: previous}, candidate)
            except ValidationError:
                continue
            except OutputValidationError:
                return 'different'
        question_id = proposal.get('question_id')
        if (proposal.get('kind') == original.get('kind') == 'cancel_candidate'
                and question_id and question_id == original.get('question_id')
                and any(q['id'] == question_id for q in questions)):
            return 'same'
        current = {(s['id'], s['revision']): s for s in segments}
        new_refs = {key for key, segment in current.items()
            if segment.get('is_new', scan_scope is None or key in scan_scope[0])}
        spans = grounding_spans(proposal, refs, new_refs, current)
        original_spans = grounding_spans(original, original_refs, dismissed_refs, current)
        title, original_title = proposal.get('title'), original.get('title')
        same_title = (isinstance(title, str) and isinstance(original_title, str)
            and bool(title.strip()) and title.strip() == original_title.strip())
        if spans and original_spans:
            if len(spans) == len(original_spans) == 1 and spans == original_spans:
                return 'same' if same_title else 'overlapping'
            if any(span[:2] == previous[:2] and max(span[2], previous[2]) < min(span[3], previous[3])
                    for span in spans for previous in original_spans):
                return 'overlapping'  # Ambiguous or shorter quotes cannot prove a new question.
            return 'different'
        # A broken quote cannot establish span identity. Retain only the narrow
        # fallback used by legacy corrections with the same title and sources.
        return 'fallback' if refs == original_refs and same_title else None

    @classmethod
    def _matches_dismissed_task(cls, proposal, task, questions, segments, scan_scope=None):
        return cls._dismissed_proposal_relation(proposal, task, questions, segments, scan_scope) in {'same', 'fallback'}

    def _supersede_dismissed_correction(self, db, meeting, work, task, dismissed, returned=(), scan_scope=None):
        if task['kind'] != 'correction' or not dismissed:
            return False
        original = normalize_question_reference(task.get('proposal'), work['questions'])
        proposals = []
        for proposal in returned:
            try:
                candidate = QuestionDecision.model_validate(normalize_question_reference(proposal, work['questions']))
                check_correction_intent(original, candidate)
                self._refs(db, meeting, [ref.model_dump() for ref in candidate.source_refs])
            except (ValidationError, OutputValidationError):
                continue
            except LiveError as exc:
                if exc.status not in {400, 404, 409}:
                    raise
                continue
            proposals.append(candidate.model_dump())
        current = {(segment['id'], segment['revision']): segment for segment in work['segments']}
        new_refs = {key for key, segment in current.items()
            if segment.get('is_new', scan_scope is None or key in scan_scope[0])}
        previous = None
        for other in dismissed:
            original_relation = self._dismissed_proposal_relation(original, other, work['questions'], work['segments'], scan_scope)
            returned_relations = []
            for proposal in proposals:
                relation = self._dismissed_proposal_relation(proposal, other, work['questions'], work['segments'], scan_scope)
                if original_relation == 'fallback' and relation is None:
                    refs = proposal_refs(proposal)
                    dismissed_refs = {(ref['segment_id'], ref['revision']) for ref in other['source_refs']}
                    if refs and refs.isdisjoint(dismissed_refs) and grounding_spans(proposal, refs, new_refs, current):
                        relation = 'different'
                returned_relations.append(relation)
            # A retry cannot narrow the stored proposal into an ignored range.
            # A valid correction can refute only the broken-quote fallback.
            if (original_relation == 'different'
                    or other['kind'] == 'scan' and original_relation != 'same'):
                continue
            if (original_relation == 'same' or 'same' in returned_relations
                    or original_relation == 'fallback' and 'different' not in returned_relations):
                previous = other
                break
        if not previous:
            return False
        task.update(state='superseded', superseded_by=previous['id'], lease=None, updated_at=time.time())
        self.store._put(db, 'wb_detection_task', task['id'], task)
        self._event(db, meeting, 'detection.superseded', {'task_id': task['id'],
            'superseded_by': previous['id']})
        self._save(db, meeting)
        return True

    def _scan_failures(self, db, m, work, task, result, rejected):
        new = [s for s in work['segments'] if s['is_new']]
        known = {identity(f['proposal']): f for f in task.get('known_failures', [])}
        overlapping_titles = [f['proposal'].get('title') for f in [*known.values(), *rejected]
                              if any(i['code'] == 'overlapping_proposal' for i in f['issues'])]
        outstanding = []
        for failure in rejected:
            proposal = failure.get('proposal')
            same_overlap = isinstance(proposal, dict) and proposal.get('title') in overlapping_titles
            if not same_overlap and self._queue_located_correction(db, m, work, new, failure):
                continue  # A separate, single-attempt task now owns this proposal.
            if isinstance(failure.get('proposal'), dict):
                known.setdefault(identity(failure['proposal']), failure)
            else:
                outstanding.append(failure)
        # Empty responses and valid but unrelated siblings cannot close a known gap.
        accepted = [a for a in result.actions if not any(f.get('proposal') in
            (a.model_dump(), result.dismissal_proposal(a)) for f in rejected)]
        current = {(segment['id'], segment['revision']): segment for segment in work['segments']}
        new_refs = {(segment['id'], segment['revision']) for segment in new}

        def resolves(action, failure):
            if not self._matches_failed_proposal(action, failure['proposal'], work['questions']):
                return False
            if not any(issue['code'] == 'overlapping_proposal' for issue in failure['issues']):
                return True
            previous = grounding_spans(failure['proposal'], proposal_refs(failure['proposal']), new_refs, current)
            proposal = result.dismissal_proposal(action)
            returned = grounding_spans(proposal, proposal_refs(proposal), new_refs, current)
            return len(returned) == 1 and returned.issubset(previous)

        resolved = {key for key, failure in known.items() if any(resolves(action, failure) for action in accepted)}
        task['known_failures'] = [f for key, f in known.items() if key not in resolved]
        task['scan_coverage_complete'] = not outstanding and not result.saturated
        # An unlocatable proposal from the main batch belongs to every possible
        # window until one confirms it. Other windows still owe their own scan.
        shared = {key for key in resolved if known[key].get('batch_scope')}
        if shared:
            for other in self._detection_tasks(db, m['id']):
                if other['id'] == task['id'] or other['family'] != task['family'] or other['state'] in DETECTION_TERMINAL_STATES:
                    continue
                previous = other.get('known_failures', [])
                remaining = [f for f in previous if not f.get('batch_scope') or identity(f['proposal']) not in shared]
                if remaining == previous:
                    continue
                other['known_failures'] = remaining
                if other.get('scan_coverage_complete') and other['state'] != 'running':
                    other['issues'] = [i for f in remaining for i in f['issues']]
                    if not remaining:
                        other['state'] = 'completed'
                        self._event(db, m, 'detection.completed', {'task_id': other['id']})
                other['updated_at'] = time.time()
                self.store._put(db, 'wb_detection_task', other['id'], other)
        return outstanding + task['known_failures']

    def detection_recovery_work(self, principal):
        with self.store.transaction() as db:
            self.store._acl(db, principal, principal['project_id'], {'agent'})
            tasks = sorted(self.store._all(db, 'wb_detection_task'), key=lambda t: (t.get('last_claim_at', 0), t['created_at']))
            for task in tasks:
                if task['state'] != 'pending' or task.get('next_at', 0) > time.time():
                    continue
                m = self.store._get(db, 'wb_meeting', task['meeting_id'])
                if m['project_id'] != principal['project_id'] or m['state'] not in {'active', 'ended'} or m['research_paused'] or self.blocked_reason(m):
                    continue
                # A service incident must not be amplified by this meeting's gaps.
                if m.get('coordinator_error') or not self.detection_service_ready(m):
                    continue
                current = {(s['id'], s['revision']): s for s in self._items(db, 'wb_segment', m['id']) if s['type'] == 'transcript.final'}
                target_keys = {(r['segment_id'], r['revision']) for r in task['source_refs']}
                if not target_keys.issubset(current):
                    task.setdefault('original_source_refs', task['source_refs'])
                    task['source_refs'] = [r for r in task['source_refs'] if (r['segment_id'], r['revision']) in current]
                    target_keys.intersection_update(current)
                    if not target_keys:
                        task.update(state='superseded', updated_at=time.time(), lease=None)
                        self.store._put(db, 'wb_detection_task', task['id'], task)
                        continue  # Revised input is unread in the main lane.
                segments = [{**current[key], 'is_new': key in target_keys} for r in task['context_refs']
                    if (key := (r['segment_id'], r['revision'])) in current]
                task.update(state='running', attempts=task['attempts'] + 1, lease=uid('lease'), last_claim_at=time.time(), updated_at=time.time())
                self.store._put(db, 'wb_detection_task', task['id'], task)
                handled = [action for action in self._items(db, 'wb_detection_action', m['id'])
                    if any((ref['segment_id'], ref['revision']) in target_keys for ref in action['source_refs'])]
                deferred = [other for other in self._detection_tasks(db, m['id'])
                    if other['id'] != task['id'] and other['kind'] == 'correction'
                    and other['family'] == task['family']
                    and other['state'] not in {'completed', 'superseded'}]
                return {'meeting': m, 'cursor': max(s['event_seq'] for s in segments),
                    'segments': sorted(segments, key=lambda s: s['event_seq']), 'lane': 'recovery',
                    'action_limit': 1 if task['kind'] == 'correction' else 12,
                    'detection_task': task,
                    'handled_detection_actions': [{key: action.get(key) for key in
                        ('kind', 'title', 'source_refs', 'grounding_quote_id')} for action in handled],
                    'deferred_detection_proposals': [{'title': other.get('proposal', {}).get('title'),
                        'source_refs': other['source_refs'], 'state': other['state']} for other in deferred
                        if isinstance(other.get('proposal'), dict)],
                    'questions': sorted(self._items(db, 'wb_question', m['id']), key=lambda q: q['updated_at'])}
        return None

    def _detection_authority(self, db, principal, work):
        m = self._meeting(db, principal, work['meeting']['id'], True)
        self.store._acl(db, principal, m['project_id'], {'agent'})
        require(m['state'] in {'active', 'ended'} and m['generation'] == work['meeting']['generation'], '识别上下文已失效', 409)
        require(not self.blocked_reason(m), '本场问题识别尚未授权', 403)
        if work.get('detection_task'):
            require(not m['research_paused'], '问题补偿已暂停', 409)
            task = self.store._get(db, 'wb_detection_task', work['detection_task']['id'])
            require(task['state'] == 'running' and task['lease'] == work['detection_task']['lease'], '补偿任务已更新', 409)
        return m

    def commit_detection(self, principal, work, result):
        refresh = False
        with self.store.transaction() as db:
            # Completed leases and duplicate main responses are no-ops.
            task = self.store._get(db, 'wb_detection_task', work['detection_task']['id']) if work.get('detection_task') else None
            if task and (task['state'] != 'running' or task['lease'] != work['detection_task']['lease']):
                return
            m = self._detection_authority(db, principal, work)
            if not task and m['coordinator_cursor'] >= work['cursor']:
                return
            # The other lane can report a newer incident while this call is in flight.
            if m.get('detection_service_retry') == work['meeting'].get('detection_service_retry'):
                self.clear_detection_service_error(m)
            if task:
                # Read dismissals in this transaction: a sibling may be ignored
                # after this recovery was claimed or while its model call ran.
                dismissed = [other for other in self._detection_tasks(db, m['id'])
                    if other['family'] == task['family'] and other['state'] == 'dismissed']
                scan_scope = self._scan_batch_scope(db, m['id'], task['family']) if dismissed else None
                dismissal_segments = self._dismissal_context(db, m['id'], work['segments'], dismissed)
                dismissal_work = {**work, 'segments': dismissal_segments}
                returned = [result.dismissal_proposal(action) for action in result.actions]
                if self._supersede_dismissed_correction(db, m, dismissal_work, task, dismissed, returned, scan_scope):
                    return
                def ignored(proposal):
                    return next((other for other in dismissed if self._matches_dismissed_task(
                        proposal, other, work['questions'], dismissal_segments, scan_scope)), None)
                if task['kind'] == 'scan':
                    result = DetectionBatch([action for action in result.actions if not ignored(result.dismissal_proposal(action))],
                        [failure for failure in result.failures if not ignored(failure.get('proposal'))],
                        result.raw_count, result.saturated, result.original_literal_quotes)
                    task['known_failures'] = [failure for failure in task.get('known_failures', [])
                        if not ignored(failure.get('proposal'))]
            target_work = {**work, 'segments': [dict(s) for s in work['segments']]}
            # Reuse the existing action/version checks with a task-local input cursor.
            target_work['meeting'] = {**work['meeting'], 'coordinator_cursor': -1}
            for s in target_work['segments']:
                if not s.get('is_new', s.get('event_seq', work['cursor']) > work['meeting']['coordinator_cursor']):
                    s['event_seq'] = -1
            rejected = list(result.failures)
            if task and task['kind'] == 'correction' and not result.actions and not rejected:
                rejected.append({'issues': [{'field': 'actions', 'code': 'missing_proposal'}], 'proposal': task['proposal']})
            for index, action in enumerate(result.actions):
                proposal = result.dismissal_proposal(action)
                dismissal_relations = [(other, self._dismissed_proposal_relation(proposal, other,
                    work['questions'], dismissal_segments, scan_scope)) for other in dismissed] if task else []
                if task and task['kind'] == 'correction':
                    original_relations = [(other, self._dismissed_proposal_relation(task['proposal'], other,
                        work['questions'], dismissal_segments, scan_scope))
                        for other, relation in dismissal_relations if relation == 'same']
                    if any(original_relation == 'different'
                            or other['kind'] == 'scan' and original_relation != 'same'
                            for other, original_relation in original_relations):
                        rejected.append({'issues': [{'field': f'actions.{index}.grounding_quote', 'code': 'correction_changed_intent'}],
                            'proposal': proposal})
                        continue
                if any(relation == 'overlapping' for _, relation in dismissal_relations):
                    rejected.append({'issues': [{'field': f'actions.{index}', 'code': 'overlapping_proposal'}],
                        'proposal': proposal})
                    continue
                if task and task['kind'] == 'scan':
                    proposals = [normalize_question_reference(f['proposal'], work['questions'])
                        for f in task.get('known_failures', [])]
                    same_question = [proposal for proposal in proposals
                        if (isinstance(proposal.get('title'), str) and action.title.strip()
                            and proposal['title'].strip() == action.title.strip())
                        or (action.question_id and proposal.get('question_id') == action.question_id)]
                    if same_question and not any(self._matches_failed_proposal(action, p, work['questions']) for p in same_question):
                        continue  # Keep the original gap; never dispatch a replacement research target.
                key = 'det-action-' + identity([m['id'], task['id'] if task and task['kind'] == 'correction' else
                    refs_of([s for s in target_work['segments'] if s['event_seq'] >= 0]), action.model_dump()])
                if db.execute("SELECT 1 FROM records WHERE kind='wb_detection_action' AND id=?", (key,)).fetchone():
                    continue
                if task and task['kind'] == 'scan' and action.kind in {'candidate', 'create_question'}:
                    # A rephrased title over the same literal span is ambiguous:
                    # don't silently merge two questions, or run a duplicate.
                    prior = [a for a in self._items(db, 'wb_detection_action', m['id'])
                        if (action.grounding_quote_id and a.get('grounding_quote_id') == action.grounding_quote_id)
                        or (a.get('grounding_quote') == action.grounding_quote and
                            {(r['segment_id'], r['revision']) for r in a.get('source_refs', [])}.intersection(
                                (r.segment_id, r.revision) for r in action.source_refs))]
                    if any(a['title'].strip() == action.title.strip() for a in prior):
                        continue
                    if prior:
                        rejected.append({'issues': [{'field': f'actions.{index}', 'code': 'overlapping_proposal'}],
                            'proposal': action.model_dump()})
                        continue
                db.execute('SAVEPOINT detection_item')
                before = copy.deepcopy(m)
                try:
                    _, changed = self._commit_detection_actions(db, m, target_work, Decisions(actions=[action]))
                except LiveError as exc:
                    db.execute('ROLLBACK TO detection_item')
                    m.clear()
                    m.update(before)
                    if exc.status not in {400, 404, 409}:
                        raise
                    code = exc.details.get('code', 'reference_not_found' if exc.status == 404 else 'context_stale')
                    rejected.append({'issues': [{'field': f'actions.{index}', 'code': code}], 'proposal': action.model_dump()})
                else:
                    refresh |= changed
                    self.store._put(db, 'wb_detection_action', key, {'id': key, 'meeting_id': m['id'],
                        'source_refs': [r.model_dump() for r in action.source_refs], 'title': action.title,
                        'kind': action.kind, 'grounding_quote_id': action.grounding_quote_id,
                        'grounding_quote': action.grounding_quote})
                finally:
                    db.execute('RELEASE detection_item')
            if task:
                if task['kind'] == 'scan':
                    rejected = self._scan_failures(db, m, work, task, result, rejected)
                incomplete = bool(rejected) or (result.saturated and task['kind'] != 'correction')
                state = 'completed'
                if incomplete:
                    state = 'pending' if task['attempts'] < task['max_attempts'] else 'needs_review'
                issues = [issue for failure in rejected for issue in failure['issues']]
                if incomplete and not issues:
                    issues = [{'field': 'actions', 'code': 'scan_incomplete'}]
                task.update(state=state, issues=issues, lease=None, updated_at=time.time())
                self.store._put(db, 'wb_detection_task', task['id'], task)
                self._event(db, m, 'detection.' + task['state'], {'task_id': task['id'], 'action_count': len(result.actions)})
            else:
                self._defer_detection(db, m, work, DetectionBatch(result.actions, rejected, result.raw_count, result.saturated))
                m.update(coordinator_cursor=work['cursor'], coordinator_state='pending' if m['semantic_seq'] > work['cursor'] else 'idle',
                    coordinator_error=None, coordinator_error_details=None, coordinator_error_message=None, coordinator_continuations=0,
                    detection_protocol=1)
                m.pop('coordinator_retry', None)
                self._event(db, m, 'coordinator.processed', {'cursor': work['cursor'], 'action_count': len(result.actions),
                    'raw_action_count': result.raw_count, 'deferred': len(rejected), 'continuation_required': result.saturated})
            self._save(db, m)
        if refresh:
            self.publish_index(m['id'])

    def detection_recovery_failed(self, principal, work, details):
        from live.workbench import COORDINATOR_RETRY_DELAYS, transient_detection_failure
        with self.store.transaction() as db:
            try:
                m = self._meeting(db, principal, work['meeting']['id'])
                task = self.store._get(db, 'wb_detection_task', work['detection_task']['id'])
            except LiveError as exc:
                if exc.status == 404:
                    return
                raise
            if task['state'] != 'running' or task['lease'] != work['detection_task']['lease']:
                return
            dismissed = [other for other in self._detection_tasks(db, m['id'])
                if other['family'] == task['family'] and other['state'] == 'dismissed']
            scan_scope = self._scan_batch_scope(db, m['id'], task['family']) if dismissed else None
            dismissal_work = {**work, 'segments': self._dismissal_context(db, m['id'], work['segments'], dismissed)}
            if self._supersede_dismissed_correction(db, m, dismissal_work, task, dismissed, scan_scope=scan_scope):
                return
            interrupted = m['research_paused'] or m['generation'] != work['meeting']['generation']
            transient = transient_detection_failure(details)
            attempts = m.get('detection_service_retry', {}).get('attempts', 0)
            retry = transient and attempts < len(COORDINATOR_RETRY_DELAYS)
            field = 'saving' if (details or {}).get('stage') == 'saving' else 'response'
            task.update(state='pending' if interrupted or retry else 'needs_review', lease=None, updated_at=time.time(),
                issues=[{'field': field, 'code': (details or {}).get('code', 'request_failed')}])
            if interrupted or retry:
                task['attempts'] = max(0, task['attempts'] - 1)  # Transport is not a content correction.
            if not interrupted and (transient or (details or {}).get('service') == 'model'):
                requested = (details or {}).get('retry_after_seconds', 0)
                delay = 0
                if retry:
                    provider_delay = min(300, requested) if type(requested) is int else 0
                    delay = max(COORDINATOR_RETRY_DELAYS[attempts], provider_delay)
                # All gaps in this meeting share one bounded service incident;
                # a backlog cannot turn a 429 into one retry per deferred item.
                m['detection_service_retry'] = {'revision': uid('incident'), 'state': 'scheduled' if retry else 'exhausted',
                    'attempts': attempts + int(retry), 'max_attempts': len(COORDINATOR_RETRY_DELAYS),
                    'delay_seconds': delay, 'next_at': time.time() + delay}
                m['detection_service_error'] = details
            self.store._put(db, 'wb_detection_task', task['id'], task)
            self._event(db, m, 'detection.' + task['state'], {'task_id': task['id']})
            self._save(db, m)

    def retry_detection_task(self, principal, mid, tid):
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid, True)
            require(m['state'] in {'active', 'ended'}, '会议数据已清理，无法补充识别', 409)
            require(not m['research_paused'] and not self.blocked_reason(m), '请先恢复本场自动查证并检查授权', 409)
            task = self.store._get(db, 'wb_detection_task', tid)
            require(task['meeting_id'] == mid and task['state'] == 'needs_review', '该补偿项无需重试', 409)
            task.update(state='pending', max_attempts=task['attempts'] + 1, next_at=0, updated_at=time.time())
            self.store._put(db, 'wb_detection_task', tid, task)
            self.clear_detection_service_error(m)
            self._event(db, m, 'detection.retry_requested', {'task_id': tid})
            self._save(db, m)
            return {'id': tid, 'state': task['state']}

    def recover_detection_tasks(self, db, m):
        unfinished = False
        for task in self._detection_tasks(db, m['id']):
            if task['state'] in {'pending', 'running'}:
                unfinished = True
                if task['state'] == 'running':
                    task.update(state='needs_review', lease=None, updated_at=time.time(),
                        issues=[{'field': 'response', 'code': 'interrupted'}])
                    self.store._put(db, 'wb_detection_task', task['id'], task)
        return unfinished

    def dismiss_detection_task(self, principal, mid, tid):
        with self.store.transaction() as db:
            m = self._meeting(db, principal, mid, True)
            self.store._acl(db, principal, m['project_id'], {'owner', 'member'})
            require(m['state'] in {'active', 'ended'}, '会议数据已清理，无法处理识别项', 409)
            task = self.store._get(db, 'wb_detection_task', tid)
            require(task['meeting_id'] == mid, '识别项不属于本场会议', 409)
            if task['state'] == 'dismissed':
                return {'id': tid, 'state': task['state']}
            require(task['state'] == 'needs_review', '识别项已更新，请刷新后再处理', 409)
            now = time.time()
            task.update(state='dismissed', lease=None, dismissed_by=principal['id'], dismissed_at=now, updated_at=now)
            self.store._put(db, 'wb_detection_task', tid, task)
            self._event(db, m, 'detection.dismissed', {'task_id': tid, 'actor_id': principal['id']})
            questions = self._items(db, 'wb_question', mid)
            segments = [s for s in self._items(db, 'wb_segment', mid) if s['type'] == 'transcript.final']
            scan_scope = self._scan_batch_scope(db, mid, task['family'])
            for sibling in self._detection_tasks(db, mid):
                if sibling['family'] != task['family'] or sibling['state'] in DETECTION_TERMINAL_STATES:
                    continue
                if sibling['kind'] == 'correction':
                    relation = self._dismissed_proposal_relation(sibling.get('proposal'), task, questions, segments, scan_scope)
                    if relation == 'same' and (task['kind'] == 'scan' or sibling['state'] == 'needs_review'):
                        sibling.update(state='superseded', superseded_by=tid, lease=None, updated_at=now)
                        self.store._put(db, 'wb_detection_task', sibling['id'], sibling)
                        self._event(db, m, 'detection.superseded', {'task_id': sibling['id'], 'superseded_by': tid})
                    continue
                if sibling['kind'] != 'scan':
                    continue
                known = sibling.get('known_failures', [])
                remaining = [failure for failure in known if not self._matches_dismissed_task(
                    failure.get('proposal'), task, questions, segments, scan_scope)]
                if remaining == known:
                    continue
                sibling['known_failures'] = remaining
                if sibling.get('scan_coverage_complete') and sibling['state'] != 'running':
                    sibling['issues'] = [issue for failure in remaining for issue in failure['issues']]
                    if not remaining:
                        sibling.update(state='completed', lease=None)
                        self._event(db, m, 'detection.completed', {'task_id': sibling['id']})
                sibling['updated_at'] = now
                self.store._put(db, 'wb_detection_task', sibling['id'], sibling)
            self._save(db, m)
            return {'id': tid, 'state': task['state']}

    def detection_task_snapshot(self, db, m):
        def titles(proposals):
            return list(dict.fromkeys(proposal['title'].strip() for proposal in proposals
                if isinstance(proposal.get('title'), str) and proposal['title'].strip()))

        tasks = [t for t in self._detection_tasks(db, m['id']) if t['state'] not in DETECTION_TERMINAL_STATES]
        current = {s['id']: s for s in self._items(db, 'wb_segment', m['id'])}
        questions = self._items(db, 'wb_question', m['id'])
        rows = []
        for task in tasks:
            segments = [current[r['segment_id']] for r in task['source_refs'] if r['segment_id'] in current]
            rows.append({k: task[k] for k in ('id', 'kind', 'state', 'attempts', 'issues', 'source_refs')})
            proposals = [task['proposal']] if task.get('proposal') else [f['proposal']
                for f in task.get('known_failures', []) if isinstance(f.get('proposal'), dict)]
            batch_proposals = []
            if task['kind'] == 'scan':
                task_refs = {(ref['segment_id'], ref['revision']) for ref in task['source_refs']}
                local_proposals = []
                for proposal in proposals:
                    target = local_proposals if (proposal_refs(proposal) or set()).intersection(task_refs) else batch_proposals
                    target.append(proposal)
                proposals = local_proposals
            proposal = proposals[0] if proposals else {}
            proposal = normalize_question_reference(proposal, questions)
            question = None
            if proposal.get('kind') in ('revise_question', 'cancel_candidate'):
                question = next((q for q in questions if q['id'] == proposal.get('question_id')), None)
            title = proposal.get('title')
            rows[-1].update(proposal_title=title if isinstance(title, str) else '',
                proposal_titles=titles(proposals), batch_proposal_titles=titles(batch_proposals),
                question_id=question['id'] if question else '', question_title=question['title'] if question else '')
            rows[-1].update(audio_start_ms=min((s.get('audio_start_ms', 0) for s in segments), default=0),
                audio_end_ms=max((s.get('audio_end_ms', 0) for s in segments), default=0))
        return rows
