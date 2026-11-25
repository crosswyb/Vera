"""Safe diagnostics for rejected model output; never include field values."""

import json

from pydantic import ValidationError


FIELDS = {"actions", "title", "question_id", "expected_version", "source_refs", "segment_id", "revision", "public_query",
          "answer", "claims", "text", "source_id", "quote", "uncertainty", "evidence_status", "overview", "topics",
          "decisions", "open_questions", "sources", "response", "quote_id", "brief", "short_answer", "key_points", "caveat", "kind", "claim_indices",
          "evidence_ids"}
LABELS = {"actions": "问题识别动作", "title": "问题标题", "source_refs": "发言依据", "public_query": "公开检索词",
          "answer": "参考回答", "claims": "佐证", "quote": "原文引用", "uncertainty": "限制说明", "brief": "会中短答"}


class OutputValidationError(ValueError):
    def __init__(self, issues, *, corrections=0, response=None, terminal=False):
        self.issues, self.corrections = issues, corrections
        self.response, self.terminal = response, terminal
        super().__init__("model output validation failed")


def issues_for(exc):
    if isinstance(exc, OutputValidationError):
        return exc.issues
    if isinstance(exc, ValidationError):
        issues = []
        for error in exc.errors(include_input=False, include_context=True, include_url=False)[:8]:
            field = ".".join(str(p) if isinstance(p, int) or p in FIELDS else "unknown_field" for p in error["loc"])
            issue = {"field": field or "response", "code": error["type"]}
            context = error.get("ctx") or {}
            for key, source in (("limit", "max_length"), ("actual", "actual_length")):
                if type(context.get(source)) is int:
                    issue[key] = context[source]
            issues.append(issue)
        return issues
    if isinstance(exc, json.JSONDecodeError):
        return [{"field": "response", "code": "invalid_json"}]
    details = getattr(exc, "details", {})
    summary_fields = {"overview_length": "overview", "overview_coverage": "overview",
                      "source_ref_not_in_transcript": "source_refs", "summary_quote_not_in_transcript": "source_refs"}
    if isinstance(details, dict) and details.get("code") in summary_fields:
        return [{"field": summary_fields[details["code"]], "code": details["code"]}]
    code = "output_incomplete" if str(exc) == "model output incomplete" else "invalid_output"
    return [{"field": "response", "code": code}]


def failure_details(exc, stage):
    details = output_presentation({"stage": stage, "issues": issues_for(exc), "correction_attempts": getattr(exc, "corrections", 0),
            **({"response": exc.response} if isinstance(exc, OutputValidationError) and exc.response else {})})
    if stage == "summarizing":
        details["message"] = failure_message(details)
    return details


def output_presentation(details):
    codes = {issue.get("code") for issue in details.get("issues", [])}
    incomplete = bool(codes & {"output_token_limit", "output_incomplete"})
    return {**details, "label": "模型输出被截断" if incomplete else "模型回答未通过校验",
            "suggestion": "提高单次输出上限，或降低推理强度后重试此问题。" if incomplete else "可重试此问题；若反复失败，请检查模型是否支持要求的 JSON 格式。"}


def failure_message(details):
    stage = {"detecting": "问题识别", "synthesizing": "整理回答", "searching": "搜索资料", "reading": "读取原文", "summarizing": "生成纪要"}.get(details["stage"], "返回内容")
    if details.get("category") == "request":
        return stage + "失败：" + details["message"]
    reasons = []
    for issue in details["issues"][:3]:
        field = issue["field"]
        label = LABELS.get(field.split(".")[0], "返回字段")
        if issue["code"] in {"too_long", "string_too_long"} and "limit" in issue:
            unit = "字" if issue["code"] == "string_too_long" else "条"
            actual = f"，本次 {issue['actual']}{unit}" if "actual" in issue else ""
            reasons.append(f"{label}超过上限（最多 {issue['limit']}{unit}{actual}）")
        elif issue["code"] == "output_token_limit":
            response = details.get("response", {})
            reasons.append(f"模型达到本次 {response.get('max_tokens', '配置的')} token 输出上限，未返回完整回答")
        elif issue["code"] == "output_incomplete" and details.get("provenance") == "historical_ledger" and details.get("response", {}).get("completion_tokens"):
            reasons.append(f"历史模型响应在 {details['response']['completion_tokens']} token 处结束，未返回完整回答")
        elif issue["code"] == "output_incomplete" and details.get("response", {}).get("finish_reason") == "length":
            reasons.append(f"服务返回长度限制（finish_reason=length），回答被截断；本次配置输出上限为 {details['response']['max_tokens']} token")
        elif issue["code"] == "quote_not_in_source":
            reasons.append(f"第 {int(field.split('.')[1]) + 1} 条佐证的引用与原文不一致")
        else:
            reasons.append({"source_not_found": "佐证引用了未返回的来源", "source_not_read": "引用来源没有可核验正文",
                "quote_id_not_found": "引用的原文片段编号不存在", "quote_id_wrong_source": "原文片段与来源编号不一致", "quote_id_conflict": "原文片段与手写引文不一致",
                "claims_required": "回答标记为有证据，但缺少原文佐证", "sources_required": "没有原文却标记为已有证据",
                "claim_reference_not_found": "关键依据引用了不存在的佐证编号",
                "output_incomplete": "模型输出未完成或被输出上限截断", "invalid_json": "模型未返回完整 JSON",
                "quote_not_in_new_segment": "问题依据未匹配所引用的新发言原话",
                "source_not_in_new_segment": "问题引用的发言编号或版本不在本批新发言中",
                "overview_length": "会议概述长度不符合要求",
                "overview_coverage": "会议概述没有覆盖会议目的、讨论、决定、后续行动和未决边界",
                "source_ref_not_in_transcript": "纪要依据的片段编号或版本不在本次输入中",
                "summary_quote_not_in_transcript": "纪要引文与转录不一致",
                "extra_forbidden": f"{label}包含不支持的字段", "missing": f"{label}缺少必要字段"}.get(issue["code"], f"{label}格式不符合要求"))
    corrected = "；已自动修正一次，仍未通过" if details.get("correction_attempts") else ""
    return stage + "未通过校验：" + "；".join(reasons) + corrected + "。"
