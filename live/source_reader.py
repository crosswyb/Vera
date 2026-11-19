"""Use the selected provider's search and returned originals, not a crawler.

Only content is evidence; snippets locate pages. Result URLs are data and are
never fetched by this process, including URLs found inside hostile webpages.
"""

import hashlib
import ipaddress
import json
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
    if issues:
        raise OutputValidationError(issues)
    if answer["evidence_status"] in {"supported", "mixed"} and not answer["claims"]:
        raise OutputValidationError([{"field": "claims", "code": "claims_required"}])
    if not sources and answer["evidence_status"] != "insufficient":
        raise OutputValidationError([{"field": "sources", "code": "sources_required"}])
    return {**answer, "sources": sources}


class Researcher:
    def __init__(self, search, model):
        self.search, self.model = search, model

    def run(self, meeting, question, stage):
        query = question.get("public_query") or question["title"]
        target = target_for(question)
        progress = getattr(stage, "progress", None)
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
        answer = self.model.answer(meeting, question, reading,
            **({"progress": progress} if progress and getattr(self.model, "supports_research_progress", False) is True else {}))
        return validate_result({**answer, "sources": sources})


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
