"""Durable detection gaps. Local commits are atomic; model calls remain outside them."""

import hashlib
import copy
import json
import time

from live.detection import DetectionBatch, check_correction_intent
from live.output_validation import OutputValidationError
from live.store import LiveError, require, uid
from live.workbench_contracts import Decisions


def identity(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def refs_of(segments):
    return [{'segment_id': s['id'], 'revision': s['revision']} for s in segments]


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

    def invalidate_detection_tasks(self, db, meeting, segment_id, revision):
        for task in self._detection_tasks(db, meeting['id']):
            if task['state'] in {'completed', 'superseded'}:
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
    def _matches_failed_proposal(action, proposal):
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
        accepted = [a for a in result.actions if not any(a.model_dump() == f.get('proposal') for f in rejected)]
        resolved = {key for key, f in known.items() if any(self._matches_failed_proposal(a, f['proposal']) for a in accepted)}
        task['known_failures'] = [f for key, f in known.items() if key not in resolved]
        task['scan_coverage_complete'] = not outstanding and not result.saturated
        # An unlocatable proposal from the main batch belongs to every possible
        # window until one confirms it. Other windows still owe their own scan.
        shared = {key for key in resolved if known[key].get('batch_scope')}
        if shared:
            for other in self._detection_tasks(db, m['id']):
                if other['id'] == task['id'] or other['family'] != task['family'] or other['state'] in {'completed', 'superseded'}:
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
                if task and task['kind'] == 'scan':
                    same_question = [f['proposal'] for f in task.get('known_failures', [])
                        if (isinstance(f['proposal'].get('title'), str) and action.title.strip()
                            and f['proposal']['title'].strip() == action.title.strip())
                        or (action.question_id and f['proposal'].get('question_id') == action.question_id)]
                    if same_question and not any(self._matches_failed_proposal(action, p) for p in same_question):
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
                    rejected.append({'issues': [{'field': f'actions.{index}', 'code': 'context_stale'}], 'proposal': action.model_dump()})
                else:
                    refresh |= changed
                    self.store._put(db, 'wb_detection_action', key, {'id': key, 'meeting_id': m['id'],
                        'source_refs': [r.model_dump() for r in action.source_refs], 'title': action.title,
                        'kind': action.kind, 'grounding_quote_id': action.grounding_quote_id,
                        'grounding_quote': action.grounding_quote})
                finally:
                    db.execute('RELEASE detection_item')
            # The other lane can report a newer incident while this call is in flight.
            if m.get('detection_service_retry') == work['meeting'].get('detection_service_retry'):
                self.clear_detection_service_error(m)
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
            proposals = [task['proposal']] if task.get('proposal') else [
                failure['proposal'] for failure in task.get('known_failures', [])]
            questions = {q['id']: q for q in self._items(db, 'wb_question', mid)}
            for proposal in proposals:
                target = proposal.get('question_id')
                if proposal.get('kind') not in ('revise_question', 'cancel_candidate') or not isinstance(target, str):
                    continue
                question = questions.get(target)
                require(question and question['version'] == proposal.get('expected_version', 1) and not question['basis_stale']
                        and question['state'] not in {'cancelled', 'paused', 'outcome_unknown'},
                        '关联问题版本或状态已变化。请打开最新问题核对范围，旧操作不能重试。', 409)
            current = self._final_utterances(db, m)
            target_refs = {(ref['segment_id'], ref['revision']) for ref in task['source_refs']}
            positions = [i for i, s in enumerate(current) if (s['id'], s['revision']) in target_refs]
            require(positions, '原发言已更正或撤回，请在转写中查看最新版本', 409)
            context = {j for i in positions for j in range(max(0, i - 6), min(len(current), i + 7))}
            task['context_refs'] = refs_of([current[j] for j in sorted(context)])
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

    def detection_task_snapshot(self, db, m):
        tasks = [t for t in self._detection_tasks(db, m['id']) if t['state'] not in {'completed', 'superseded'}]
        current = {s['id']: s for s in self._items(db, 'wb_segment', m['id'])}
        rows = []
        for task in tasks:
            segments = [current[r['segment_id']] for r in task['source_refs'] if r['segment_id'] in current]
            rows.append({k: task[k] for k in ('id', 'kind', 'state', 'attempts', 'issues', 'source_refs')})
            rows[-1].update(audio_start_ms=min((s.get('audio_start_ms', 0) for s in segments), default=0),
                audio_end_ms=max((s.get('audio_end_ms', 0) for s in segments), default=0))
        return rows
