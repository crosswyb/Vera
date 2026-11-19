"""Conservative checks on returned text; no fetch or model-supplied verification status."""

import re
import unicodedata
from urllib.parse import urlsplit


GB = re.compile(r"(?<![A-Za-z0-9_])GB\s*(?:/\s*T\s*)?\d+(?:\.\d+)*(?:\s*[-–—−]\s*\d{4})?(?![\d.])", re.I)
POLICY_NUMBER = re.compile(r"[\u4e00-\u9fff]{1,12}[〔\[（(]\s*\d{4}\s*[〕\]）)]\s*\d+\s*号")
ARTICLE = re.compile(r"第\s*(?:[一二三四五六七八九十百千万零〇]+|\d+(?:\.\d+)*)\s*[条款节章项]")
HEADINGS = re.compile(r"(?m)^[^\S\r\n]*(?:#{1,6}[ \t]+|\*{2})?(第\s*[一二三四五六七八九十百千万零〇\d]+\s*[条章节]|"
                      r"\d+(?:\.\d+){0,5})(?=\s|[、：:]|[^\x00-\x7f]|$)")
POLICY_BODY = re.compile(r"(?m)^[^\S\r\n]*(?:#{1,6}[ \t]+|\*{2})?(第[一二三四五六七八九十百千万零〇\d]+条|[一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[）)])")
INTERPRETATION = re.compile(r"政策解读|图解|一图读懂|答记者问|新闻发布会|征求意见稿|修订说明|(?:^|[】])解读[\s：:｜|]|的解读$")
STANDARDS = {"openstd.samr.gov.cn", "std.samr.gov.cn"}
POLICY_PUBLISHERS = {"ndrc.gov.cn": "国家发展和改革委员会", "miit.gov.cn": "工业和信息化部", "samr.gov.cn": "国家市场监督管理总局",
                     "mof.gov.cn": "财政部", "mohrss.gov.cn": "人力资源和社会保障部", "mee.gov.cn": "生态环境部"}
# Publisher, official domain, document path, aliases. Unknown hosts can be
# linked as sources, but cannot acquire an official badge from a model guess.
DEFINITION_DOCS = (
    ("W3C", "w3.org", ("/tr/",), ("w3c", "万维网联盟")),
    ("IETF", "rfc-editor.org", ("/rfc/",), ("ietf", "rfc")),
    ("Kubernetes", "kubernetes.io", ("/docs/", "/zh-cn/docs/"), ("kubernetes", "k8s")),
    ("Microsoft", "learn.microsoft.com", ("/",), ("microsoft", "微软", "azure")),
    ("Apple", "developer.apple.com", ("/documentation/",), ("apple", "苹果")),
    ("AWS", "docs.aws.amazon.com", ("/",), ("aws", "亚马逊云")),
    ("Python", "docs.python.org", ("/",), ("python",)),
    ("DeepSeek", "api-docs.deepseek.com", ("/",), ("deepseek",)),
)
LABELS = {"verified": "找到原文", "catalog_only": "仅找到官方题录", "secondary_only": "仅找到解读或转载",
          "mismatch": "版本、文件或条款不匹配", "unverified": "未核实原文"}


def normal(value):
    return unicodedata.normalize("NFKC", value).strip()


def compact(value):
    return re.sub(r"\s+", "", normal(value)).casefold().translate(str.maketrans("–—−[]()", "---〔〕〔〕"))


def standard_ids(value):
    return {compact(match.group()) for match in GB.finditer(normal(value))}


def contains(value, fragment):
    key = compact(fragment)
    if not key:
        return False
    if key.isascii() and key.isalnum():
        pattern = r"\s+".join(re.escape(word) for word in normal(fragment).casefold().split())
        return bool(re.search(r"(?<![a-z0-9])" + pattern + r"(?![a-z0-9])", normal(value).casefold()))
    return key in compact(value)


def target_for(question):
    """Reuse explicit scope; infer only recognizable targets for manual/older jobs."""
    saved = question.get("original_target") or {}
    title, query = question.get("title", ""), question.get("public_query", "")
    scope = title + "\n" + query
    ids = standard_ids(title) or standard_ids(query)
    numbers = POLICY_NUMBER.findall(normal(title)) or POLICY_NUMBER.findall(normal(query))
    names = re.findall(r"《([^》]{4,150})》", title)
    kind = saved.get("kind")
    if not kind:
        if ids or re.search(r"国标.{0,16}(?:原文|条款|规定)|(?:原文|条款).{0,8}国标", title):
            kind = "standard"
        elif numbers or (names and re.search(r"原文|文号|政策|条款|规定|第.+条", title)):
            kind = "policy"
        elif re.search(r"(?:正式|官方|原文|规范).{0,15}定义|定义.{0,10}(?:原文|官方)", title):
            kind = "definition"
        else:
            return None
    identifier, section, issuer = (saved.get(key, "").strip() for key in ("identifier", "section", "issuer"))
    if identifier and not contains(scope, identifier):
        identifier = ""
    if section and not contains(scope, section):
        section = ""
    if issuer and not contains(scope, issuer):
        issuer = ""
    if kind == "standard":
        # A partial identifier can identify a catalog entry, never an edition.
        identifier = next(iter(ids)) if len(ids) == 1 else ""
    elif kind == "policy":
        identifier = numbers[0] if len(numbers) == 1 else names[0] if len(names) == 1 and not numbers else identifier if not numbers and not names else ""
        if not issuer:
            match = re.search(r"国务院办公厅|国务院|国家[^，。、《》]{2,16}(?:委员会|管理局|总局)", title)
            issuer = match.group() if match else ""
    elif not identifier:
        match = re.search(r"(?:对|关于)\s*([^，。？?]{2,60}?)\s*的(?:正式|官方|规范)定义", title)
        identifier = match.group(1).strip("《》“” ") if match else ""
    sections = set(ARTICLE.findall(normal(title))) or set(ARTICLE.findall(normal(query)))
    if kind == "standard" and not sections:
        sections = set(re.findall(r"(?<![\d.])\d+(?:\.\d+)+(?=\s*(?:条款|条|款|节))", normal(scope)))
    # Multiple requested clauses must not be stamped as verified by one excerpt.
    if len(sections) > 1:
        identifier = ""
    elif sections:
        section = next(iter(sections))
    return {"kind": kind, "identifier": identifier, "section": section, "issuer": issuer,
            "document_title": names[0] if kind == "policy" and len(names) == 1 else "",
            "scope": scope}


def target_key(target):
    return tuple(compact((target or {}).get(key, "")) for key in ("kind", "identifier", "section", "issuer", "document_title"))


def article_number(value):
    """Normalize Chinese clause numerals without changing document identifiers."""
    digits = {c: n for n, c in enumerate("零一二三四五六七八九")}
    digits["〇"] = 0
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    if not value or any(c not in digits and c not in units for c in value):
        return value
    if not any(c in units for c in value):
        return str(int("".join(str(digits[c]) for c in value)))
    total = section = number = 0
    for c in value:
        if c in digits:
            number = digits[c]
        elif c == "万":
            total += (section + number or 1) * 10000
            section = number = 0
        else:
            section += (number or 1) * units[c]
            number = 0
    return str(total + section + number)


def official_source(target, source):
    parsed = urlsplit(source["url"])
    host, path = (parsed.hostname or "").lower(), parsed.path.lower()
    if parsed.scheme != "https" or parsed.port not in {None, 443}:
        return "", "secondary_only"
    title, body = source["title"].strip("# "), source["text"]
    if INTERPRETATION.search(title) or re.search(r"/(?:jd|jiedu|zcjd|news|blog)(?:/|$)", path):
        return "", "secondary_only"
    identifier = target["identifier"]
    header = title + "\n" + body[:900]
    if target["kind"] == "standard":
        if host not in STANDARDS:
            return "", "secondary_only"
        publisher = "国家标准公开平台"
        head_ids = standard_ids(title) or standard_ids(body[:650])
        if not identifier or (identifier not in head_ids and not ("-" not in identifier and any(i.startswith(identifier + "-") for i in head_ids))):
            return publisher, "mismatch"
        if re.search(r"/(?:newgbinfo|std_list|search|query)(?:/|$)", path) or "-" not in identifier or not HEADINGS.search(body):
            return publisher, "catalog_only"
    elif target["kind"] == "policy":
        if not (host.endswith(".gov.cn") or host == "gov.cn"):
            return "", "secondary_only"
        if re.search(r"(?:来源|转载自)\s*[:：]\s*(?:新华社|新华网|人民日报|人民网|央视|媒体)", body[:1000]):
            return "", "secondary_only"
        central = (host in {"www.gov.cn", "gov.cn"} and path.startswith(("/zhengce/", "/gongbao/"))
                   and bool(re.search(r"/content_\d+\.s?html?$", path))) or (
                       host == "app.www.gov.cn" and bool(re.fullmatch(r"/govdata/gov/\d{6}/\d{2}/\d+/article\.html", path)))
        issuer = target["issuer"]
        marker = re.search(r"(?:发文机关|发布机构|发文单位)\s*[:：]\s*([^\n|]{2,80})", body[:1500])
        department = POLICY_PUBLISHERS.get(host.removeprefix("www."))
        if not central and not (department and marker and compact(department) == compact(marker.group(1))):
            return "", "secondary_only"
        publisher = marker.group(1).strip() if marker else issuer or "中国政府网"
        if not identifier or not contains(header, identifier) or (target["document_title"] and not contains(header, target["document_title"])):
            return publisher, "mismatch"
        if issuer and ((marker and compact(issuer) != compact(publisher)) or (not marker and not contains(header, issuer))):
            return publisher, "mismatch"
        if not POLICY_BODY.search(body):
            return publisher, "catalog_only"
    else:
        publisher = next((name for name, domain, paths, aliases in DEFINITION_DOCS
            if (host == domain or host.endswith("." + domain)) and path.startswith(paths)
            and any(contains(target["issuer"] or target["scope"], alias) for alias in aliases)), "")
        if not publisher:
            return "", "secondary_only"
        if not identifier or not contains(title + "\n" + body, identifier):
            return publisher, "mismatch"
    return publisher, "body" if source.get("read_status") == "read" else "unverified"


def citation_location(target, source, quote):
    body, requested = source["text"], target["section"]
    at = body.find(quote)
    if at < 0:
        return None
    if target["kind"] == "definition":
        return ("定义段落", quote) if not requested and contains(quote, target["identifier"]) and re.search(
            r"定义为|是指|指的是|是一种|\b(?:is|are|means|refers to|defined as)\b", quote, re.I) else None
    pattern = HEADINGS if target["kind"] == "standard" else POLICY_BODY
    headings = list(pattern.finditer(body))
    def section_key(value):
        value = compact(value)
        # Preserve the kind of policy locator: chapter three is not article three.
        suffix = value[-1:] if target["kind"] == "policy" and value[-1:] in "条款节章项" else ""
        number = re.sub(r"^[第〔]|[条款节章项、〕]$", "", value)
        return article_number(number), suffix

    for index, heading in enumerate(headings):
        locator = heading.group(1)
        if requested and section_key(locator) != section_key(requested):
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        section_body = body[heading.end():end].strip()
        if target["kind"] == "standard" and "\n" not in section_body and not re.search(r"[。.!?；;]", section_body):
            continue  # Heading-only entries do not establish a clause body.
        left, right = max(at, heading.end()), min(end, at + len(quote))
        excerpt = body[left:right].strip() if left < right else ""
        if len(excerpt) >= 8 and at - heading.end() <= 1800 and not re.search(r"\.{3,}|…{2,}", excerpt):
            return locator, excerpt
    return None


def original_result(target, sources, claims):
    if not target:
        return None
    result = {"kind": target["kind"], "identifier": target["identifier"], "section": target["section"]}
    if not target["identifier"]:
        return {**result, "state": "unverified", "label": LABELS["unverified"],
                "message": "未能唯一确定所问的文件、版本、条款或正式定义。请补充编号、完整标题或制定机构。"}
    best, rank = {}, -1
    for source in sources:
        publisher, classification = official_source(target, source)
        locator, quote = None, None
        if classification == "body":
            for claim in claims:
                if claim["source_id"] == source["id"] and (located := citation_location(target, source, claim["quote"])):
                    locator, quote = located
                    break
            classification = "verified" if locator else "unverified"
        score = {"secondary_only": 0, "mismatch": 1, "catalog_only": 2, "unverified": 3, "verified": 4}[classification]
        if score > rank:
            best = {"state": classification, "source_id": source["id"], "title": source["title"], "url": source["url"]}
            if publisher:
                best["publisher"] = publisher
            if locator:
                best.update(locator=locator, quote=quote)
            rank = score
    state = best.get("state", "unverified")
    message = {
        "verified": "已核对该出处中的正文摘引。是否仍为现行版本及是否适用，需另行确认。",
        "catalog_only": "官方页面可确认题录或基本信息，本次未取得可核验的条文正文。",
        "secondary_only": "现有结果是解读、转载或其他未能核验发布方的页面，不能作为正式条文。",
        "mismatch": "官方来源中的文件、版本或条款与问题不一致。请核对编号、年份或条款后再查。",
        "unverified": "未取得与问题匹配、可定位的正式正文摘引。",
    }[state]
    return {**result, **best, "state": state, "label": LABELS[state], "message": message}


def with_original_evidence(question, result):
    lookup = original_result(target_for(question), result["sources"], result["claims"])
    if not lookup:
        return result
    if lookup["state"] != "verified":
        # Never surface an unverified clause from the model in a conclusion,
        # analysis, or claim. The links remain available for human follow-up.
        result = {**result, "evidence_status": "insufficient", "claims": [], "answer": lookup["message"], "uncertainty": [], "brief": {
            "short_answer": "尚不能确认所问原文。" if lookup["state"] == "unverified" else lookup["label"] + "，暂不能确认所问条文。",
            "key_points": [{"text": lookup["message"], "kind": "explanation", "claim_indices": []}],
            "caveat": "不能凭这次检索确认条文。"}}
    return {**result, "original_evidence": lookup}
