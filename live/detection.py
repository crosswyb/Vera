"""Detection-only parsing: keep valid proposals and account for every rejected item."""

import hashlib
import json
import re
from dataclasses import dataclass, field

from pydantic import ValidationError

from live.output_validation import OutputValidationError, issues_for
from live.workbench_contracts import QuestionDecision


@dataclass
class DetectionBatch:
    actions: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    raw_count: int = 0
    saturated: bool = False
    original_literal_quotes: list = field(default_factory=list)

    def dismissal_proposal(self, action):
        proposal = action.model_dump()
        for parsed_action, original_quote in self.original_literal_quotes:
            if parsed_action is action:
                proposal['grounding_quote'] = original_quote
                break
        return proposal


def utterance_spans(text, limit):
    """Yield literal spans in transcript order, keeping short utterances intact."""
    for match in re.finditer(r'[^。！？?!\n]+[。！？?!\n]*|[。！？?!]+', text):
        for start in range(match.start(), match.end(), limit):
            end = min(start + limit, match.end())
            if any(c.isalnum() for c in text[start:end]):
                yield start, end


def quote_options(segments, cursor):
    """Stable IDs for literal new utterance spans, including very short questions."""
    options = []
    for segment in segments:
        if not segment.get('is_new', segment.get('event_seq', cursor + 1) > cursor):
            continue
        text = segment['text']
        for start, end in utterance_spans(text, 400):
            identity = json.dumps([segment['id'], segment['revision'], start, end], ensure_ascii=False)
            options.append({'id': 'u-' + hashlib.sha256(identity.encode()).hexdigest()[:16],
                'segment_id': segment['id'], 'revision': segment['revision'], 'start': start,
                'end': end, 'quote': text[start:end]})
    return options


def literal_quote(cited, quote):
    if not quote or not cited or not all(cited) or not any(c.isalnum() for c in quote):
        return None
    texts = [s['text'] for s in cited if s['is_new']]
    if any(quote in text for text in texts):
        return quote
    stem = quote.rstrip('。？！?!')
    if stem and stem != quote:
        for text in texts:
            start = 0
            while (pos := text.find(stem, start)) >= 0:
                end = pos + len(stem)
                if end == len(text) or text[end] in '。？！?!':
                    return text[pos:end + 1]
                start = pos + 1
    return None


def normalize_question_reference(proposal, questions):
    """Repair only a missing fixed prefix; versions and intent stay unchanged."""
    if not isinstance(proposal, dict) or proposal.get('kind') not in ('revise_question', 'cancel_candidate'):
        return proposal
    question_id = proposal.get('question_id')
    if not isinstance(question_id, str) or any(q['id'] == question_id for q in questions):
        return proposal
    if not re.fullmatch(r'[0-9a-f]{16}', question_id):
        return proposal
    matches = [q['id'] for q in questions if q['id'] == 'question-' + question_id]
    if len(matches) == 1:
        return {**proposal, 'question_id': matches[0]}
    return proposal


def check_correction_intent(previous, action):
    proposal_kinds = ('candidate', 'create_question', 'revise_question')
    if action.kind not in proposal_kinds or not isinstance(previous, dict):
        raise OutputValidationError([{'field': 'actions', 'code': 'missing_proposal'}])
    fields = ['kind', 'title', 'question_id', 'expected_version', 'scope_changed', 'evidence_need', 'recheck_meeting', 'defer_research']
    if action.evidence_need in ('public', 'mixed'):
        fields.extend(('external_fact', 'public_query', 'original_target'))
    for name in fields:
        value = previous.get(name)
        if name == 'expected_version' and name not in previous and previous.get('kind') == 'revise_question':
            # Omission used the contract default in the original decision; a
            # retry cannot opt into a newer edit just by supplying its version.
            value = QuestionDecision.model_fields[name].default
        if value is None:
            continue
        # An invalid sibling must not prevent us from protecting this field.
        try:
            original = QuestionDecision.model_validate({'kind': action.kind, name: value})
        except ValidationError:
            continue
        prior = getattr(original, name)
        current = getattr(action, name)
        if name == 'kind' and prior not in proposal_kinds:
            continue
        if name in ('title', 'external_fact', 'public_query'):
            prior, current = prior.strip(), current.strip()
            if not prior:
                continue
        if current != prior:
            raise OutputValidationError([{'field': name, 'code': 'correction_changed_intent'}])


def parse_detection(raw, work, options):
    result = DetectionBatch()
    segments = {(s['id'], s['revision']): {**s,
        'is_new': s.get('is_new', s.get('event_seq', work['cursor']) > work['meeting']['coordinator_cursor'])}
        for s in work['segments']}
    by_id = {q['id']: q for q in options}

    def reject(issues, proposal=None):
        try:
            json.dumps(proposal, allow_nan=False)
        except (TypeError, ValueError):
            # Python's decoder accepts NaN/Infinity, whereas SQLite's JSON
            # functions reject them. Preserve the range instead of bad JSON.
            proposal = None
        result.failures.append({'issues': issues, 'proposal': proposal})

    if not isinstance(raw, dict) or set(raw) != {'actions'} or not isinstance(raw['actions'], list):
        reject([{'field': 'actions', 'code': 'invalid_envelope'}])
        return result
    result.raw_count = len(raw['actions'])
    action_limit = min(work.get('action_limit', 4), 12)
    result.saturated = result.raw_count >= action_limit
    correction = work.get('detection_task', {}).get('kind') == 'correction'
    questions = work.get('questions', [])
    previous = normalize_question_reference(work.get('detection_task', {}).get('proposal'), questions)
    if correction and result.raw_count != 1:
        reject([{'field': 'actions', 'code': 'correction_count_mismatch'}], work['detection_task'].get('proposal'))
        return result
    if result.raw_count > action_limit:
        reject([{'field': 'actions', 'code': 'too_many_actions'}])
    for index, proposal in enumerate(raw['actions'][:action_limit]):
        try:
            original_literal = None
            proposal = normalize_question_reference(proposal, questions)
            action = QuestionDecision.model_validate(proposal)
            if correction:
                check_correction_intent(previous, action)
            if action.kind == 'no_action':
                continue
            if action.kind in {'revise_question', 'cancel_candidate'}:
                question = next((q for q in questions if q['id'] == action.question_id), None)
                if question is None:
                    raise OutputValidationError([{'field': 'question_id', 'code': 'question_not_found'}])
                if question['version'] != action.expected_version:
                    raise OutputValidationError([{'field': 'expected_version', 'code': 'question_version_stale'}])
            cited = [segments.get((r.segment_id, r.revision)) for r in action.source_refs]
            if not cited or not all(cited) or not any(s['is_new'] for s in cited):
                raise OutputValidationError([{'field': 'source_refs', 'code': 'source_not_in_new_segment'}])
            if action.kind in {'candidate', 'create_question', 'revise_question'}:
                if action.grounding_quote_id:
                    option = by_id.get(action.grounding_quote_id)
                    if not option or (option['segment_id'], option['revision']) not in {(r.segment_id, r.revision) for r in action.source_refs}:
                        raise OutputValidationError([{'field': 'grounding_quote', 'code': 'quote_id_not_found'}])
                    quote = option['quote']
                else:
                    original_literal = action.grounding_quote.strip()
                    quote = literal_quote(cited, original_literal)
                if not quote or not any(c.isalnum() for c in quote):
                    raise OutputValidationError([{'field': 'grounding_quote', 'code': 'quote_not_in_new_segment'}])
                if not action.title.strip() or not action.evidence_need:
                    raise OutputValidationError([{'field': 'actions', 'code': 'missing_intent'}])
                action = action.model_copy(update={'grounding_quote': quote})
                if action.evidence_need in {'public', 'mixed'}:
                    if not action.external_fact.strip():
                        raise OutputValidationError([{'field': 'external_fact', 'code': 'missing_external_fact'}])
                elif action.original_target:
                    raise OutputValidationError([{'field': 'actions', 'code': 'conflicting_target'}])
                else:
                    action = action.model_copy(update={'external_fact': '', 'public_query': ''})
            result.actions.append(action)
            if original_literal is not None and original_literal != action.grounding_quote:
                result.original_literal_quotes.append((action, original_literal))
        except (ValidationError, OutputValidationError) as exc:
            issues = [{**issue, 'field': f"actions.{index}.{issue['field']}"} for issue in issues_for(exc)]
            reject(issues, proposal if isinstance(proposal, dict) else None)
    return result
