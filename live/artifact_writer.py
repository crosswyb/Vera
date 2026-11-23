"""Immutable Markdown versions, published atomically without replacing a file."""

import hashlib
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from live.store import require


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_name(text, limit=60):
    text = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|\[\]`#]', "-", text).strip(" .-")
    return text[:limit] or "未命名问题"


def stamp(created):
    return datetime.fromtimestamp(created).astimezone().strftime("%Y%m%d-%H%M%S")


class ArtifactWriter:
    def __init__(self, root, registry_path=None, *, create_root=True):
        self.root = Path(root).expanduser().resolve() if create_root else Path(root).expanduser().absolute()
        if create_root:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.temporary_registry = Path(registry_path).expanduser().resolve() if registry_path is not None else self.path(".zsjh-temporary.sqlite3")
        self.temporary_registry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(self.temporary_registry) as db:
            db.execute("CREATE TABLE IF NOT EXISTS temporary_files(path TEXT PRIMARY KEY, device INTEGER, inode INTEGER)")
        self.temporary_registry.chmod(0o600)

    def _register_temporary(self, path, fd):
        # Commit ownership before writing any meeting content. This separate
        # registry survives a crash that rolls back the publication transaction.
        stat = os.fstat(fd)
        with sqlite3.connect(self.temporary_registry) as db:
            db.execute("INSERT INTO temporary_files VALUES(?,?,?)", (str(Path(path).relative_to(self.root)), stat.st_dev, stat.st_ino))

    def clean_temporaries(self, directory, only=None):
        removed = 0
        with sqlite3.connect(self.temporary_registry) as db:
            db.execute("PRAGMA secure_delete=ON")
            for relative, device, inode in db.execute("SELECT path,device,inode FROM temporary_files").fetchall():
                if str(Path(relative).parent) != directory:
                    continue
                if only is not None and relative != only:
                    continue
                path = self.path(relative)
                if path.exists():
                    stat = path.stat()
                    if not path.is_file() or (stat.st_dev, stat.st_ino) != (device, inode):
                        # Relinquish ownership without retaining expired meeting
                        # metadata or touching the user's replacement.
                        db.execute("DELETE FROM temporary_files WHERE path=?", (relative,))
                        continue
                    path.unlink()
                    removed += 1
                db.execute("DELETE FROM temporary_files WHERE path=?", (relative,))
        return removed

    def path(self, relative):
        require(self.root.is_dir() and self.root.resolve() == self.root,
                "会议保存目录不可用，请连接原磁盘或恢复原目录后重试", 409)
        candidate = self.root / relative
        # Resolve every component: existing symlinks must never escape the root.
        require(candidate.resolve().is_relative_to(self.root), "文档路径越界", 409)
        require(not candidate.is_symlink(), "文档路径不能是符号链接", 409)
        return candidate

    def meeting_dir(self, meeting):
        return f"{stamp(meeting['created_at'])}-{safe_name(meeting['title'], 40)}-{meeting['id'][-8:]}"

    def document_path(self, meeting, question):
        return f"{meeting['directory']}/{stamp(question['created_at'])}-{safe_name(question['original_title'])}-{question['id'][-8:]}-v{question['version']}-g{question['generation']}.md"

    def write(self, relative, content, previous_hash=None):
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        wanted = digest(content)
        if path.exists():
            current = hashlib.sha256(path.read_bytes()).hexdigest()
            if current == wanted:
                return wanted
            require(False, "已有不同内容，保留原文件；新内容应另存版本", 409, file_conflict=True)
        fd, temporary = tempfile.mkstemp(prefix=".zsjh-", suffix=".tmp", dir=path.parent)
        registered = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                self._register_temporary(temporary, stream.fileno())
                registered = True
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # link() is an atomic no-clobber publication. A human-created file
            # appearing after the initial read also wins; no existing inode is replaced.
            try:
                os.link(temporary, path)
            except FileExistsError:
                require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == wanted,
                        "文件在保存期间发生变化，保留原文件", 409, file_conflict=True)
            return wanted
        finally:
            if registered:
                relative_temporary = str(Path(temporary).relative_to(self.root))
                self.clean_temporaries(str(Path(relative_temporary).parent), only=relative_temporary)
            elif os.path.exists(temporary):
                os.unlink(temporary)


def render_document(meeting, question, result):
    synthetic = "\n> 合成测试材料，来源与回答用于验证程序；不代表真实检索或事实核验。\n" if meeting["mode"] == "demo" else ""
    lines = [f"# {question['title']}", synthetic]
    brief = result.get("brief")
    if brief:
        lines += ["## 结论摘要", "", brief["short_answer"], ""]
        if result.get("analysis"):
            lines += ["## 完整分析", ""]
            for section in result["analysis"]:
                for number, item in enumerate(section["items"], 1):
                    prefix = {"steps": f"{number}. ", "points": "- ", "paragraphs": ""}[section["layout"]]
                    refs = "、".join(str(index) for index in item["claim_indices"])
                    lines.append(prefix + " ".join(item["text"].splitlines()) + (f"（佐证 {refs}）" if refs else ""))
                    if section["layout"] == "paragraphs":
                        lines.append("")
                lines.append("")
        elif result["answer"].strip() != brief["short_answer"].strip():
            lines += ["## 完整分析", "", result["answer"], ""]
        lines += ["## 适用范围", "", brief['caveat'], ""]
        if result.get("meeting_evidence"):
            lines += ["## 会中原话", ""]
            for ref in result["meeting_evidence"]:
                lines += [*(f"> {line}" for line in ref["quote"].splitlines()),
                          "", f"片段 {ref['segment_id']} · 修订 {ref['revision']}", ""]
        lines += ["## 关键依据", ""]
        kinds = {"fact": "资料事实", "source_claim": "来源主张", "inference": "模型推断", "explanation": "参考说明"}
        for point in brief["key_points"]:
            refs = "、".join(str(index) for index in point["claim_indices"])
            lines.append(f"- 【{kinds[point['kind']]}】{point['text']}" + (f"（佐证 {refs}）" if refs else ""))
        lines += [""]
    else:
        lines += ["## 参考回答", "", result["answer"], ""]
    if original := result.get("original_evidence"):
        lines += ["## 原文核验", "", original["label"], ""]
        if locator := original.get("locator"):
            lines += [f"出处：{original['publisher']} · {original['title']} · {locator}", ""]
        if quote := original.get("quote"):
            lines += [*(f"> {line}" for line in quote.splitlines()), ""]
        if url := original.get("url"):
            lines += [f"来源：{url}", ""]
        lines += [original["message"], ""]
    lines += ["## 原文佐证", ""]
    sources = {s["id"]: s for s in result.get("sources", [])}
    for index, claim in enumerate(result.get("claims", []), 1):
        source = sources[claim["source_id"]]
        lines += [f"{index}. {claim['text']} [{source['id']}]", f"   > {claim['quote']}"]
    if not result.get("claims"):
        lines += ["未形成可关联原文的主张。"]
    lines += ["", "## 来源", ""]
    for source in sources.values():
        lines += [f"- [{source['id']}] {source['title']}", f"  {source['url']}",
                  f"  发布/更新时间（服务报告）：{source.get('published_at') or 'unknown'}；读取状态：{source['read_status']}；正文哈希：{source.get('content_hash', 'unknown')}",
                  f"  读取方式：{source.get('read_method', 'unknown')}；检索时间：{source.get('retrieved_at', 'unknown')}"]
    lines += ["", "## 限制与未确定事项", ""] + [f"- {item}" for item in result.get("uncertainty", [])]
    if result.get("evidence_status") != "model_explanation":
        lines.append("- 引用关联已经过程序校验；引用是否在语义上支持主张，仍需人工判断。")
    lines += ["", "## 记录信息", "", "会议参考材料，最终判断由参会者作出。", "",
              f"- 问题 ID：{question['id']}", f"- 问题版本：{question['version']}",
              f"- 生成时间：{datetime.now(timezone.utc).isoformat()}", f"- 证据状态：{result['evidence_status']}",
              f"- 依据发言：{', '.join(r['segment_id'] + '@' + str(r['revision']) for r in question['source_refs']) or '手动建立'}", ""]
    return "\n".join(lines)
