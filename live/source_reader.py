"""Use the selected provider's search and returned originals, not a crawler.

Only content is evidence; snippets locate pages. Result URLs are data and are
never fetched by this process, including URLs found inside hostile webpages.
"""

import hashlib
import ipaddress
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from live.provider_settings import normalize_base_url
from live.output_validation import OutputValidationError
from live.failure_diagnostics import RequestOutcomeUnknown, describe_failure, request_details, transport_details
from live.model_transport import model_http_error
from live.original_evidence import official_source, target_for
from live.workbench_contracts import ResearchAnswer


def source_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("public HTTP(S) source required")
    host = parsed.hostname.lower()
    if host in {"localhost", "metadata.google.internal"} or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("private source URL rejected")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("private source URL rejected")
    if (parsed.port or 443) not in {80, 443} or len(url) > 3000:
        raise ValueError("unsupported source URL")
    return url


class SearchOutcomeUnknown(RequestOutcomeUnknown):
    pass


class SearchHTTP:
    def request(self, query, payload):
        if not isinstance(query, str) or not query.strip() or len(query) > 128:
            raise ValueError("search query requires 1 to 128 Unicode characters")
        reservation = self.budget.reserve()
        started = time.monotonic()
        with requests.Session() as session:
            session.trust_env = False
            try:
                with session.post(self.endpoint, headers={"Authorization": "Bearer " + self.key},
                                  json=payload, timeout=(5, 25), stream=True, allow_redirects=False) as response:
                    if response.status_code != 200:
                        error = model_http_error(response)
                        error.diagnostic = {**describe_failure(error, "searching", "search"), "request_id": reservation,
                                            "elapsed_ms": round((time.monotonic() - started) * 1000)}
                        self.budget.record(reservation, {"http_status": response.status_code, "diagnostic": error.diagnostic})
                        raise error
                    chunks, size = [], 0
                    for chunk in response.iter_content(32768):
                        size += len(chunk)
                        if size > 1_000_000 or time.monotonic() - started > 25:
                            diagnostic = request_details("response_too_large" if size > 1_000_000 else "response_deadline", "search",
                                timeout=25, elapsed_ms=round((time.monotonic() - started) * 1000), request_id=reservation)
                            self.budget.record(reservation, {"state": "outcome_unknown", "error_kind": "response_limit", "diagnostic": diagnostic})
                            raise SearchOutcomeUnknown(diagnostic=diagnostic)
                        chunks.append(chunk)
            except requests.RequestException as exc:
                diagnostic = transport_details(exc, "search", timeout=25,
                    elapsed_ms=round((time.monotonic() - started) * 1000), request_id=reservation)
                self.budget.record(reservation, {"state": "outcome_unknown", "error_kind": type(exc).__name__, "diagnostic": diagnostic})
                raise SearchOutcomeUnknown(diagnostic=diagnostic) from None
        self.budget.record(reservation, {"state": "received"})
        return json.loads(b"".join(chunks))


class DeepSeekSearch(SearchHTTP):
    def __init__(self, key, budget, endpoint="https://api.deepseek.com"):
        endpoint = normalize_base_url(endpoint)
        self.endpoint = endpoint if endpoint.endswith("/v1/tools/search") else endpoint + ("/tools/search" if endpoint.endswith("/v1") else "/v1/tools/search")
        self.key, self.budget = key, budget

    def search(self, query):
        result = self.request(query, {"query": query, "need_content": True, "count": 4})
        # Actual API: {results}; published example: {code:0,data:{results}}.
        data = result if isinstance(result.get("results"), list) else result.get("data") if result.get("code") == 0 else None
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise RuntimeError("search response contract rejected")
        return data["results"][:4]


class TavilySearch(SearchHTTP):
    def __init__(self, key, budget, endpoint="https://api.tavily.com"):
        endpoint = normalize_base_url(endpoint)
        self.endpoint = endpoint if endpoint.endswith("/search") else endpoint + "/search"
        self.key, self.budget = key, budget

    def search(self, query):
        result = self.request(query, {"query": query, "search_depth": "advanced", "max_results": 4,
            "include_answer": False, "include_raw_content": "text", "auto_parameters": False})
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise RuntimeError("search response contract rejected")
        # Tavily's content is a relevance snippet. Only raw_content can serve as
        # a returned original; never silently promote a snippet to evidence.
        return [{"url": item.get("url"), "title": item.get("title"), "date": item.get("published_date"),
                 "content": item.get("raw_content"), "read_method": "tavily_search_returned_raw_content"}
                for item in result["results"][:4] if isinstance(item, dict)]


def read_sources(results, *, include_metadata=False):
    sources, seen, hashes = [], set(), {}
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        url = item["url"].split("#")[0]
        try:
            source_url(url)
        except (ValueError, TypeError):
            continue
        body = item.get("content")
        has_body = isinstance(body, str) and len(body.strip()) >= 80
        if url in seen or not (has_body or include_metadata):
            continue
        seen.add(url)
        text = body.strip()[:14000] if has_body else ""
        content_hash = hashlib.sha256(text.encode()).hexdigest() if has_body else None
        source = {"id": "source-" + str(len(sources) + 1), "title": str(item.get("title") or urlsplit(url).hostname)[:300],
                  "url": url, "text": text, "content_hash": content_hash,
                  "published_at": str(item.get("date"))[:100] if item.get("date") else None,
                  "retrieved_at": datetime.now(timezone.utc).isoformat(), "read_status": "read" if has_body else "metadata_only",
                  "read_method": "tavily_search_returned_raw_content" if item.get("read_method") == "tavily_search_returned_raw_content" else "deepseek_search_returned_content",
                  "date_origin": "provider_reported"}
        if content_hash:
            if content_hash in hashes:
                source["same_content_as"] = hashes[content_hash]
            hashes[content_hash] = source["id"]
        sources.append(source)
    return sources


def quote_catalog(sources):
    """Number bounded, continuous original spans; never reconstruct a quote."""
    options = []
    for source in sources:
        text, start, number = source["text"], 0, 0
        while start < len(text):
            end = min(start + 400, len(text))
            if end < len(text):
                boundary = max(text.rfind(separator, start + 100, end) for separator in "\n。！？.!?；;")
                if boundary >= start + 100:
                    end = boundary + 1
            quote = text[start:end].strip()
            if len(quote) >= 8:
                number += 1
                options.append({"id": f"{source['id']}-quote-{number}", "source_id": source["id"], "quote": quote})
            start = end
    return options


def relevance_terms(text):
    words = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+|[\u4e00-\u9fff]+", text.lower())
    return {term for word in words for term in (
        [word[i:i + 2] for i in range(len(word) - 1)] if '\u4e00' <= word[0] <= '\u9fff' else [word])}


_RESTRICTED_SPEECH = re.compile(r'(?:不要|别|不能|禁止|不宜|不得).{0,10}(?:外传|外发|对外|往外说|发群|发到群|公开)')
_PRIVATE_QUERY = re.compile(
    r'(?:内部|保密|未公开|密钥|令牌|api[_ -]?key|password)|'
    r'(?:人民币|[￥¥$])\s*\d|\d[\d,.]*\s*(?:万|亿)?\s*(?:元|美元)|'
    r'\d[\d,.]*\s*(?:万|亿)(?=\s|$|中标|预算|项目|合同)|'
    r'[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?<!\d)1[3-9]\d{9}(?!\d)', re.I)


def restrict_public_query(query):
    return bool(_PRIVATE_QUERY.search(query))


def restricted_speech(text):
    return bool(_RESTRICTED_SPEECH.search(text))


def restricted_meeting_context(segments, refs):
    """Respect restrictions stated around the transcript basis, including a nearby lead-in."""
    ids = {ref['segment_id'] for ref in refs}
    return any(restricted_speech(segments[j]['text']) for i, s in enumerate(segments) if s['id'] in ids
               for j in range(max(0, i - 4), min(len(segments), i + 2)))


def validate_result(result):
    answer = ResearchAnswer.model_validate({k: v for k, v in result.items() if k != "sources"}).model_dump(exclude_none=True)
    sources = result.get("sources", [])
    if len(sources) > 6 or len({s["id"] for s in sources}) != len(sources):
        raise OutputValidationError([{"field": "sources", "code": "invalid_inventory"}])
    by_id = {s["id"]: s for s in sources}
    issues = []
    for index, claim in enumerate(answer["claims"]):
        source = by_id.get(claim["source_id"])
        code = "source_not_found" if not source else "source_not_read" if source.get("read_status") != "read" else "quote_not_in_source" if claim["quote"] not in source.get("text", "") else ""
        if code:
            issues.append({"field": f"claims.{index}.quote", "code": code})
    if issues:
        raise OutputValidationError(issues)
    for index, point in enumerate((answer.get("brief") or {}).get("key_points", [])):
        if any(ref > len(answer["claims"]) for ref in point["claim_indices"]):
            issues.append({"field": f"brief.key_points.{index}.claim_indices", "code": "claim_reference_not_found"})
    for section_index, section in enumerate(answer.get("analysis", [])):
        for item_index, item in enumerate(section['items']):
            if any(ref > len(answer['claims']) for ref in item['claim_indices']):
                issues.append({"field": f"analysis.{section_index}.items.{item_index}", "code": "claim_reference_not_found"})
    if issues:
        raise OutputValidationError(issues)
    if answer["evidence_status"] in {"supported", "mixed"} and not answer["claims"]:
        raise OutputValidationError([{"field": "claims", "code": "claims_required"}])
    if not sources and answer["evidence_status"] not in {"insufficient", "model_explanation"}:
        if answer["evidence_status"] != "meeting_context" or not answer.get("meeting_evidence"):
            raise OutputValidationError([{"field": "sources", "code": "sources_required"}])
    if answer["evidence_status"] == "meeting_context" and (sources or not answer.get("meeting_evidence")):
        raise OutputValidationError([{"field": "meeting_evidence", "code": "invalid_inventory"}])
    return {**answer, "sources": sources}


# Titles may paraphrase the request. Inspect the cited utterance as well, but
# never send that utterance (or text from neighbouring turns) to web search.
_SOURCE_REQUEST = re.compile(
    r"出处|官方(?:文档|原文|来源|依据|规定|数据|公告)|(?:给出|提供|附上|注明|核实|核对|引用|查找|查一下).{0,10}(?:来源|原文|链接|依据)|"
    r"(?:哪[条个份]|哪一[条份]).{0,10}(?:规定|政策|标准|文件)|\bofficial (?:docs?|documentation|source|guidance)\b|\bcite (?:a |the |your )?sources?\b", re.I)
_CURRENT_STATUS = re.compile(
    r"(?:最新|现行)(?:的)?(?:版本|政策|条款|法规|规定|标准|公告|价格|汇率|报价|油价|行情|市场数据)|实时(?:价格|汇率|报价)|(?:还|仍)(?:然)?(?:在.{0,10}(?:维护|支持)|受.{0,6}支持|有效|适用|能不能用|支持|维护)|"
    r"(?:现在|目前|当前|今天|今日|截至).{0,28}(?:版本|维护|支持|价格|收费|政策|有效|生效|状态|规定|汇率|市价|报价)|"
    r"(?:版本|价格|汇率|政策|标准).{0,15}(?:现在|目前|当前|今天|今日)|(?:规定|政策|标准|版本).{0,16}(?:是否|还|仍).{0,5}(?:有效|废止|生效|维护|支持)|是否(?:已废止|仍有效|已停更)|"
    r"\b(?:latest (?:version|release|price)|still (?:supported|maintained|in force)|current (?:price|version|status))\b", re.I)


_INDUSTRY_EVIDENCE = re.compile(
    r'(?:行业|业界|业内|主流|常见|典型|实际|公开).{0,12}(?:做法|实践|案例|水平|基准|数据|应用效果)|'
    r'(?:行情|市场价差|价格走势|毛利率.{0,12}涨价|涨价.{0,12}毛利率)|'
    r'(?:目前|当前|今年|近期).{0,22}(?:加工利润|价差|供需)|'
    r'(?:模型|产品|方案|系统|信息化).{0,22}(?:能力比较|效果对比|能力对比|应用对比|信息化的?对比)')


_LOCAL_ASSET = (r'(?:内部(?:系统|项目|平台|流程|台账|预算|工单)|本公司|本企业|我司|我方|本项目|本部门|本厂|'
                r'我们(?:自己|公司)?的?(?:系统|项目|平台|审批|台账|预算|进度|工单))')
_LOCAL_STATUS = re.compile(_LOCAL_ASSET + r'的?(?:(?:最新|目前|当前|现在)的?)?(?:是否|已经|能否)?'
                           r'(?:支持|具备|进展|进度|状态|上线|部署|开通|完成|实际(?:成本|售价)|预算余额)')
_QUERY_YEAR = re.compile(r'(?P<edition>[0-9][:：]\s*(?:19|20)\d{2}(?!\d))|'
                         r'(?<![A-Za-z0-9/:：.\-])(?:19|20)\d{2}(?![A-Za-z0-9/:：.\-])年?')


def needs_internal_records(question):
    """Local operational status cannot be established by a public search."""
    intent = question.get('evidence_need')
    if intent == 'internal':
        return True
    if intent in {'public', 'mixed'} or target_for(question):
        return False
    scope = '\n'.join((question.get('title', ''), question.get('grounding_quote') or ''))
    return bool(_LOCAL_STATUS.search(scope) and not (_SOURCE_REQUEST.search(scope) or _INDUSTRY_EVIDENCE.search(scope)))


def requires_web_evidence(question):
    if question.get('web_search_requested'):
        return True
    if needs_internal_records(question):
        return False
    if question.get('evidence_need') in {'public', 'mixed'} or target_for(question):
        return True
    inputs = [question['title'], question.get('public_query', '')]
    quote = (question.get('grounding_quote') or '').strip()
    if quote:
        inputs.append(quote)
    else:
        inputs.extend(s['text'] for s in question.get('discussion_context', []) if s.get('is_basis'))
    if any(_SOURCE_REQUEST.search(text) or _CURRENT_STATUS.search(text) for text in inputs):
        return True
    # Legacy cards predate coordinator intent. Match the requested scope, not
    # incidental industry vocabulary elsewhere in a cited conversational turn.
    return not question.get('evidence_need') and any(_INDUSTRY_EVIDENCE.search(text) for text in inputs[:2])


def is_grounded_explanation(question):
    """A checked explanation can execute with model permission alone."""
    return (question.get('evidence_need') == 'explanation'
            and bool((question.get('grounding_quote') or '').strip())
            and not requires_web_evidence(question))


def _explanation_result(answer):
    note = '模型直接回答，未经网页核验。'
    point = answer.short_answer if len(answer.short_answer) <= 140 else '具体理由见完整分析。'
    return validate_result({'answer': answer.answer, 'claims': [], 'sources': [],
        'brief': {'short_answer': answer.short_answer, 'key_points': [{'text': point,
            'kind': 'explanation', 'claim_indices': []}], 'caveat': note},
        'uncertainty': [note], 'evidence_status': 'model_explanation'})


def _public_search_query(meeting, question, target):
    query = question.get('public_query', '').strip()
    if question.get('web_search_requested'):
        return query  # Send only the text explicitly approved by the user.
    if meeting.get('data_scope') == 'real' and question.get('public_query_origin') == 'model':
        if question.get('restricted_context') or restrict_public_query(query):
            return ''
    if not query and meeting.get('data_scope') in {None, 'synthetic'}:
        return question['title']
    if target or question.get('public_query_origin') != 'model':
        return query
    basis = ' '.join(s['text'] for s in question.get('discussion_context', []) if s.get('is_basis'))
    # Imported audio may be older than the import date. Do not silently use a
    # guessed calendar year from the model's query as the reporting period.
    if re.search(r'今年|去年|本年度', basis) and not re.search(r'(?:19|20)\d{2}', basis):
        # Remove only standalone calendar years. A colon-delimited edition is
        # part of an identifier (ISO 9001:2015), even with a space after ':'.
        query = _QUERY_YEAR.sub(lambda match: match.group('edition') or ' ', query)
    return ' '.join(query.split())


class Researcher:
    def __init__(self, search, model, *, choose_search=None):
        self.search, self.model = search, model
        self.choose_search = choose_search

    def run(self, meeting, question, stage):
        need = question.get('evidence_need')
        internal = needs_internal_records(question)
        progress = getattr(stage, "progress", None)
        if internal and not question.get('web_search_requested'):
            if question.get('discussion_context') and callable(getattr(type(self.model), 'answer_from_meeting', None)):
                stage("synthesizing")
                return self.model.answer_from_meeting(meeting, question, progress=progress)
            note = '本场转写暂无可用于回答的上下文。请核对问题所指的内部记录。'
            return validate_result({'answer': note, 'claims': [], 'sources': [], 'uncertainty': [note],
                                    'evidence_status': 'insufficient', 'meeting_resolution': 'missing'})
        target = target_for(question)
        query = _public_search_query(meeting, question, target)
        timeout_retried = False

        def model_step(call, *args, **kwargs):
            nonlocal timeout_retried
            try:
                return call(*args, **kwargs)
            except RequestOutcomeUnknown as exc:
                diagnostic = exc.diagnostic or {}
                if (timeout_retried or diagnostic.get('service') != 'model'
                        or diagnostic.get('code') not in {'response_deadline', 'read_timeout', 'request_timeout'}):
                    raise
                timeout_retried = True
                if progress:
                    progress('model_timeout_retry', {'request_id': diagnostic.get('request_id')})
            return call(*args, **kwargs)

        model_progress = {'progress': progress} if progress and getattr(self.model, 'supports_research_progress', False) is True else {}
        if is_grounded_explanation(question):
            return _explanation_result(model_step(self.model.explain, meeting, question, **model_progress))
        must_search = requires_web_evidence(question)
        if self.choose_search and not must_search:
            route_progress = model_progress if getattr(self.choose_search, '__self__', None) is self.model else {}
            decision = model_step(self.choose_search, meeting, question, **route_progress)
            if decision.route == "direct":
                return _explanation_result(decision)
        if self.choose_search:
            stage("searching")
        if self.search is None:
            raise ValueError('搜索服务尚未配置')
        if not query.strip():
            raise ValueError('缺少获准外发的公开检索词')
        if progress:
            progress("search_query", {"query": query})
        results = self.search.search(query)
        stage("reading")
        sources = read_sources(results, include_metadata=bool(target))
        if progress:
            progress("sources_ready", {"result_count": len(results), "sources": sources})
        reading = [s for s in sources if official_source(target, s)[1] == "body"] if target and target["identifier"] else ([] if target else sources)
        if not reading:
            return {"answer": "本次搜索未返回可核验的网页正文，暂不提供事实性回答。", "claims": [],
                    "brief": {"short_answer": "本次未获得可核验的网页正文，暂时无法回答这个问题。",
                              "key_points": [{"text": "需要补充包含相关内容的原始资料，或调整检索范围后再查。", "kind": "explanation", "claim_indices": []}],
                              "caveat": "搜索摘要没有作为原文证据使用。"},
                    "uncertainty": ["搜索结果摘要没有作为原文使用。", "未静默调用其他搜索或抓取服务。"],
                    "evidence_status": "insufficient", "sources": sources}
        stage("synthesizing")
        synthesis = {**question, 'public_query': query}
        if internal:
            synthesis['evidence_need'] = 'mixed'  # Web evidence concerns only the public part of the question.
        quick = None
        if (progress and not internal and meeting.get('data_scope') == 'real' and meeting.get('state') == 'active'
                and meeting.get('input_kind') != 'audio_file'
                and not target and callable(getattr(type(self.model), 'quick_answer', None))):
            synthesis['hide_unverified_draft'] = True
            try:
                candidate = validate_result({**model_step(self.model.quick_answer, meeting, synthesis, reading, **model_progress),
                                             'sources': reading})
            except (ValueError, RequestOutcomeUnknown):
                candidate = None  # The full answer has access to all of the source passages.
            if candidate and candidate['evidence_status'] in {'supported', 'mixed'}:
                if need == 'mixed':
                    candidate['evidence_status'] = 'mixed'
                    boundary = '公开来源仅核验公开部分，企业实际情况仍需核对内部记录。'
                    candidate['brief']['caveat'] = boundary
                    candidate['uncertainty'] = [boundary]
                quick = candidate
            if quick:
                progress('verified_brief', quick)
                synthesis['verified_brief'] = quick['brief']['short_answer']
        try:
            answer = model_step(self.model.answer, meeting, synthesis, reading, **model_progress)
            checked = validate_result({**answer, "sources": sources})
        except (ValueError, RuntimeError):
            if not quick:
                raise
            note = '完整分析未能生成，已保留会中短答及其出处。'
            return {**quick, 'uncertainty': [*quick['uncertainty'][:11], note],
                    'brief': {**quick['brief'], 'caveat': (quick['brief']['caveat'][:140] + ' ' + note).strip()}}
        if (must_search or self.choose_search) and checked['evidence_status'] not in {'supported', 'mixed'}:
            if target:
                return checked  # Formal originals get their separate lookup status at publication.
            if checked['evidence_status'] == 'insufficient':
                return checked
            note = '公开正文未能核实所问事实。请核对具体对象和原始出处。'
            return {**checked, 'answer': note, 'brief': None, 'claims': [], 'uncertainty': [note],
                    'evidence_status': 'insufficient', 'analysis': None}
        if internal:
            note = '公开来源仅核验行业部分，本企业情况需另核内部原始记录。'
            checked['evidence_status'] = 'insufficient'
            checked['uncertainty'] = [note, *checked['uncertainty'][:11]]
        elif need == 'mixed':
            note = '公开来源仅核验公开部分，内部情况需另核本场或企业原始记录。'
            checked['evidence_status'] = 'mixed'
            checked['uncertainty'] = [note, *checked['uncertainty'][:11]]
        return checked


class DemoResearcher:
    """A deliberately synthetic pipeline fixture, never a network fallback."""
    def run(self, meeting, question, stage):
        if meeting["data_scope"] != "synthetic" or meeting["mode"] != "demo":
            raise ValueError("demo researcher requires a synthetic demo meeting")
        stage("reading")
        body = "合成测试正文：工作流通过预先设定的步骤组织处理过程，智能体根据当前目标和反馈选择后续行动。两种方式可以组合使用。选择时需要考虑流程确定性、工具边界和人工判断。此段只用于验证引用和文档生成，不是外部查证。"
        source = read_sources([{"title": "合成资料：工作流与智能体", "url": "https://fixture.invalid/workflow", "content": body}])[0]
        source["read_method"] = "synthetic_fixture"
        stage("synthesizing")
        return {"answer": "这是一份合成参考回答，用于展示从问题到原文佐证再到 Markdown 的完整路径。真实会议中应读取实际来源后，结合讨论范围给出参考材料。",
                "brief": {"short_answer": "合成材料描述了预设步骤和按反馈行动两种方式，可以组合使用。",
                          "key_points": [{"text": "材料以预设步骤描述工作流，以根据目标和反馈选择行动描述智能体。", "kind": "source_claim", "claim_indices": [1]}],
                          "caveat": "这是合成演示，没有进行真实检索，不能作为业务依据。"},
                "claims": [{"text": "合成材料分别描述了预设步骤和按反馈选择行动两种处理方式。", "source_id": source["id"], "quote": "工作流通过预先设定的步骤组织处理过程，智能体根据当前目标和反馈选择后续行动。"}],
                "uncertainty": ["未进行真实网络搜索，也未调用模型。", "本材料不能用于会议业务判断。"],
                "evidence_status": "supported", "sources": [source]}
