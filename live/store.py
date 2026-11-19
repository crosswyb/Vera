"""Transactional pilot store. Only the API process writes this database.

LLM/retrieval/media calls happen outside transactions. Every late result has to
re-enter through a lease, meeting generation and field-version check.
"""

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock

from live.contracts import AgentPlan, Operation


FIELDS = ("name", "constraint", "evidence", "pending", "decision", "rationale")


def uid(prefix):
    return prefix + "-" + uuid.uuid4().hex[:16]


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class LiveError(Exception):
    def __init__(self, message, status=400, **details):
        super().__init__(message)
        self.status, self.details = status, details


def require(condition, message="没有此操作的权限", status=403, **details):
    if not condition:
        raise LiveError(message, status, **details)


class LiveStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path) + ".schema.lock", timeout=10):
            with self.transaction() as db:
                db.execute("CREATE TABLE IF NOT EXISTS records(kind TEXT,id TEXT,data TEXT NOT NULL,PRIMARY KEY(kind,id))")
                db.execute("CREATE TABLE IF NOT EXISTS events(meeting TEXT,seq INTEGER,source TEXT,data TEXT NOT NULL,PRIMARY KEY(meeting,seq),UNIQUE(meeting,source))")
                db.execute("CREATE TABLE IF NOT EXISTS ops(artifact TEXT,seq INTEGER,op_id TEXT,digest TEXT,data TEXT NOT NULL,PRIMARY KEY(artifact,seq),UNIQUE(artifact,op_id))")
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _get(db, kind, key):
        r = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        require(r is not None, "对象不存在", 404)
        return json.loads(r[0])

    @staticmethod
    def _put(db, kind, key, data):
        db.execute("INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data", (kind, key, encode(data)))

    @staticmethod
    def _all(db, kind):
        return [json.loads(r[0]) for r in db.execute("SELECT data FROM records WHERE kind=?", (kind,))]

    def authenticate(self, token):
        require(isinstance(token, str) and 8 <= len(token) <= 512, "请先使用成员访问码登录", 401)
        with self.transaction() as db:
            key = hashlib.sha256(token.encode()).hexdigest()
            row = db.execute("SELECT data FROM records WHERE kind='credential' AND id=?", (key,)).fetchone()
            require(row, "访问码无效或已撤销", 401)
            member = self._get(db, "member", json.loads(row[0])["member_id"])
            require(member["enabled"], "成员身份已停用", 401)
            return member

    def _acl(self, db, principal, project_id, roles=None):
        # Re-read even for a worker that authenticated before a long model call.
        current = self._get(db, "member", principal["id"])
        require(current["enabled"] and current["project_id"] == project_id)
        require(roles is None or current["role"] in roles)
        return current

    def seed(self, title="ZSJH 接入路线评审", fixture=False, synthetic=False):
        with self.transaction() as db:
            project_id, artifact_id = uid("project"), uid("table")
            data_scope = "synthetic" if fixture or synthetic else "real"
            self._put(db, "project", project_id, {"id": project_id, "title": title, "artifact_id": artifact_id, "data_scope": data_scope})
            credentials = []
            for name, role in (("主持人", "owner"), ("参会者乙", "member"), ("参会者丙", "member"), ("ZSJH", "agent")):
                member_id = uid("member")
                token = ("fixture-" + role + "-" + str(len(credentials))) if fixture else secrets.token_urlsafe(32)
                member = {"id": member_id, "name": name, "role": role, "project_id": project_id, "enabled": True}
                self._put(db, "member", member_id, member)
                self._put(db, "credential", hashlib.sha256(token.encode()).hexdigest(), {"member_id": member_id})
                credentials.append({**member, "token": token})
            rows = []
            for row_id, name in (("option-a", "方案 A · 可控会议房"), ("option-b", "方案 B · 日常会议平台")):
                rows.append({"id": row_id, "fields": {f: {"value": name if f == "name" else "", "version": 0, "confirmed": False, "evidence_refs": []} for f in FIELDS}})
            self._put(db, "artifact", artifact_id, {"id": artifact_id, "project_id": project_id, "data_scope": data_scope, "cursor": 0, "rows": rows})
            return {"project_id": project_id, "artifact_id": artifact_id, "members": credentials}

    def home(self, principal):
        with self.transaction() as db:
            self._acl(db, principal, principal["project_id"])
            project = self._get(db, "project", principal["project_id"])
            meetings = [m for m in self._all(db, "meeting") if m["project_id"] == project["id"]]
            members = [m for m in self._all(db, "member") if m["project_id"] == project["id"]]
            return {"member": principal, "project": project, "meetings": meetings, "members": members}

    def create_meeting(self, principal, value):
        with self.transaction() as db:
            self._acl(db, principal, value.project_id, {"owner"})
            artifact = self._get(db, "artifact", value.artifact_id)
            require(artifact["project_id"] == value.project_id)
            require(value.data_scope != "synthetic" or artifact.get("data_scope") == "synthetic", "合成模型测试请使用独立合成项目，避免发送原项目内容", 409)
            mid = uid("meeting")
            meeting = {"id": mid, "project_id": value.project_id, "artifact_id": value.artifact_id,
                       "title": value.title, "responsibility": value.responsibility, "data_scope": value.data_scope, "state": "active", "muted": False,
                       "generation": 1, "playback_generation": 1, "seq": 0, "semantic_seq": 0,
                       "cursor": 0, "members": {}, "agent_lease": None, "media": {"status": "disconnected"}}
            self._put(db, "meeting", mid, meeting)
            return meeting

    def meeting(self, principal, mid):
        with self.transaction() as db:
            m = self._get(db, "meeting", mid)
            self._acl(db, principal, m["project_id"])
            return m

    def join(self, principal, mid, consent):
        with self.transaction() as db:
            m = self._get(db, "meeting", mid)
            actor = self._acl(db, principal, m["project_id"], {"owner", "member"})
            require(m["state"] != "ended", "会议已结束", 409)
            m["members"][actor["id"]] = {"consent": consent, "present": True, "name": actor["name"]}
            if not consent:
                self._pause(db, m)
            self._append(db, m, uid("join"), "participant.consent", actor["id"], {"consent": consent})
            self._put(db, "meeting", mid, m)
            return m

    @staticmethod
    def ready(m):
        present = [v for v in m["members"].values() if v["present"]]
        return m["state"] == "active" and bool(present) and all(v["consent"] for v in present)

    def _append(self, db, m, source, kind, actor, payload, semantic=False):
        m["seq"] += 1
        event = {"id": m["id"] + ":" + str(m["seq"]), "seq": m["seq"], "meeting_id": m["id"],
                 "source_event_id": source, "type": kind, "actor_ref": actor,
                 "received_at": time.time(), "payload": payload}
        if semantic:
            m["semantic_seq"] = m["seq"]
        db.execute("INSERT INTO events VALUES(?,?,?,?)", (m["id"], m["seq"], source, encode(event)))
        return event

    def events(self, principal, mid, after=0):
        with self.transaction() as db:
            m = self._get(db, "meeting", mid)
            self._acl(db, principal, m["project_id"])
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM events WHERE meeting=? AND seq>? ORDER BY seq LIMIT 200", (mid, after))]

    def event(self, principal, mid, seq):
        with self.transaction() as db:
            m = self._get(db, "meeting", mid)
            self._acl(db, principal, m["project_id"])
            row = db.execute("SELECT data FROM events WHERE meeting=? AND seq=?", (mid, seq)).fetchone()
            require(row is not None, "来源不存在或已删除", 404)
            return json.loads(row[0])

    def append_event(self, principal, mid, value, internal=False):
        with self.transaction() as db:
            m = self._get(db, "meeting", mid)
            actor = self._acl(db, principal, m["project_id"], {"agent"} if internal else {"owner", "member"})
            require(internal or value.type in {"transcript.final"}, "此事件只能由媒体服务提交")
            speaker = value.speaker_id if internal else actor["id"]
            canonical = value.model_dump()
            canonical["speaker_id"] = speaker
            source = actor["id"] + ":" + value.source_event_id
            previous = db.execute("SELECT data FROM events WHERE meeting=? AND source=?", (mid, source)).fetchone()
            if previous:
                e = json.loads(previous[0])
                require(e["payload"] == canonical, "同一事件 ID 的内容不同", 409)
                return e
            require(m["state"] != "ended", "会议已结束", 409)
            if value.type.startswith("transcript."):
                require(self.ready(m), "收听尚未获准或已暂停", 409)
                require(not internal or value.generation == m["generation"], "音频识别结果来自旧的参与状态", 409)
                require(speaker in m["members"] and m["members"][speaker]["consent"] and m["members"][speaker]["present"], "说话人尚未签入并同意", 403)
                require(value.segment_id and value.text.strip(), "转写需包含段落 ID 与文本", 400)
                older = [json.loads(r[0]) for r in db.execute("SELECT data FROM events WHERE meeting=? ORDER BY seq DESC", (mid,))]
                revisions = [e for e in older if e["type"] == "transcript.final" and e["payload"].get("segment_id") == value.segment_id and e["actor_ref"] == speaker]
                require(not revisions or value.revision > max(e["payload"]["revision"] for e in revisions), "转写修订版本过期", 409)
            elif value.type == "participant.left":
                if speaker in m["members"]:
                    m["members"][speaker]["present"] = False
            elif value.type == "participant.joined":
                if speaker not in m["members"] or not m["members"][speaker]["consent"]:
                    self._pause(db, m)
                else:
                    m["members"][speaker]["present"] = True
            elif value.type == "media.status":
                m["media"] = {"status": value.text, "updated_at": time.time()}
            e = self._append(db, m, source, value.type, speaker or actor["id"], canonical, value.type == "transcript.final")
            self._put(db, "meeting", mid, m)
            return e

    def _pause(self, db, m, ended=False):
        m["state"] = "ended" if ended else "paused"
        m["generation"] += 1
        m["playback_generation"] += 1
        m["cursor"] = m["semantic_seq"]
        m["agent_lease"] = None
        for task in self._all(db, "task"):
            if task["meeting_id"] == m["id"] and task["state"] in {"queued", "running"}:
                task.update(state="paused", generation=task["generation"] + 1, lease=None)
                self._put(db, "task", task["id"], task)

    def control(self, principal, mid, action):
        with self.transaction() as db:
            m = self._get(db, "meeting", mid)
            actor = self._acl(db, principal, m["project_id"], {"owner", "member"} if action == "interrupt" else {"owner"})
            require(m["state"] != "ended", "会议已结束", 409)
            if action in {"pause", "end"}:
                self._pause(db, m, action == "end")
            elif action == "resume":
                require(all(v["consent"] for v in m["members"].values() if v["present"]), "仍有参会者未同意", 409)
                m.update(state="active", cursor=m["semantic_seq"], agent_lease=None)
                m["generation"] += 1
            elif action in {"mute", "unmute"}:
                m["muted"] = action == "mute"
                m["playback_generation"] += 1
            elif action == "interrupt":
                m["playback_generation"] += 1
            self._append(db, m, uid("control"), "meeting.control", actor["id"], {"action": action})
            self._put(db, "meeting", mid, m)
            return m

    def artifact(self, principal, project_id, aid):
        with self.transaction() as db:
            self._acl(db, principal, project_id)
            a = self._get(db, "artifact", aid)
            require(a["project_id"] == project_id)
            return a

    def media_interrupt(self, principal, mid):
        with self.transaction() as db:
            m = self._get(db, "meeting", mid)
            self._acl(db, principal, m["project_id"], {"agent"})
            m["playback_generation"] += 1
            self._append(db, m, uid("barge"), "media.interrupt", principal["id"], {})
            self._put(db, "meeting", mid, m)
            return m

    def responsibility(self, principal, mid, text, expected_generation):
        with self.transaction() as db:
            m = self._get(db, "meeting", mid)
            self._acl(db, principal, m["project_id"], {"owner"})
            require(m["state"] != "ended" and m["generation"] == expected_generation, "会议状态已变化，请重新读取", 409)
            state = m["state"]
            self._pause(db, m)
            m.update(state=state, responsibility=text)
            self._append(db, m, uid("role"), "responsibility.changed", principal["id"], {"text": text})
            self._put(db, "meeting", mid, m)
            return m

    def operations(self, principal, project_id, aid, after=0):
        self.artifact(principal, project_id, aid)
        with self.transaction() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM ops WHERE artifact=? AND seq>? ORDER BY seq LIMIT 200", (aid, after))]

    def _evidence(self, db, m, refs):
        for ref in refs:
            row = db.execute("SELECT data FROM events WHERE meeting=? AND json_extract(data,'$.id')=?", (m["id"], ref)).fetchone()
            require(row, "引用的证据不属于此会议", 400)
            event = json.loads(row[0])
            require(event["type"] in {"transcript.final", "research.result"}, "引用不是最终发言或研究结果", 400)
            require(not event["payload"].get("redacted"), "引用的来源已按保存期限删除", 409)
            if event["type"] == "transcript.final":
                newer = db.execute("SELECT 1 FROM events WHERE meeting=? AND seq>? AND json_extract(data,'$.type')='transcript.final' AND json_extract(data,'$.actor_ref')=? AND json_extract(data,'$.payload.segment_id')=? LIMIT 1", (m["id"], event["seq"], event["actor_ref"], event["payload"]["segment_id"])).fetchone()
                require(not newer, "引用的发言已被更正", 409)
            else:
                require(event["payload"].get("state") == "completed", "引用的研究结果已过期", 409)

    def _apply(self, db, actor, a, op, m=None):
        payload = {**op.model_dump(), "actor": actor["id"]}
        dg = hashlib.sha256(encode(payload).encode()).hexdigest()
        old = db.execute("SELECT digest,data FROM ops WHERE artifact=? AND op_id=?", (a["id"], op.op_id)).fetchone()
        if old:
            require(old["digest"] == dg, "同一操作 ID 的内容或发起人不同", 409)
            return json.loads(old["data"])
        row = next((r for r in a["rows"] if r["id"] == op.row_id), None)
        require(row, "目标行不存在", 404)
        current = row["fields"][op.field]
        require(current["version"] == op.expected_field_version, "字段已被其他人修改，请重新读取", 409, current=current)
        if actor["role"] == "agent":
            require(m is not None and self.ready(m), "AI 当前不能修改", 409)
            require(op.kind == "set_draft_field", "AI 不能确认决定或撤销他人修改")
            require(op.evidence_refs, "AI 修改必须附来源", 400)
            require(not current["confirmed"], "已确认的决定须由负责人更正", 409)
        if op.evidence_refs:
            require(m, "证据操作必须关联来源会议", 400)
            self._evidence(db, m, op.evidence_refs)
        if op.kind == "confirm_decision":
            require(actor["role"] == "owner" and op.field == "decision", "只有负责人可以确认决定")
        if current["confirmed"]:
            require(actor["role"] == "owner", "只有负责人可以更正已确认决定")
        value = op.value
        confirmed = op.kind == "confirm_decision"
        refs = op.evidence_refs
        if op.kind == "undo":
            undo = db.execute("SELECT data FROM ops WHERE artifact=? AND op_id=?", (a["id"], op.undo_op_id)).fetchone()
            require(undo, "要撤销的操作不存在", 404)
            undo = json.loads(undo[0])
            require(actor["role"] == "owner" or undo["actor"] == actor["id"], "只能撤销自己的修改")
            require(undo["row_id"] == op.row_id and undo["field"] == op.field and undo["after"]["version"] == current["version"], "目标字段已有后续改动，不能直接撤销", 409)
            value = undo["before"]["value"]
            confirmed = undo["before"]["confirmed"]
            refs = undo["before"]["evidence_refs"]
            require(not confirmed or actor["role"] == "owner", "恢复已确认决定需要负责人权限")
        after = {"value": value, "version": current["version"] + 1, "confirmed": confirmed, "evidence_refs": refs}
        a["cursor"] += 1
        receipt = {**op.model_dump(), "actor": actor["id"], "actor_name": actor["name"], "seq": a["cursor"], "before": current, "after": after, "committed_at": time.time()}
        row["fields"][op.field] = after
        db.execute("INSERT INTO ops VALUES(?,?,?,?,?)", (a["id"], a["cursor"], op.op_id, dg, encode(receipt)))
        self._put(db, "artifact", a["id"], a)
        return receipt

    def apply(self, principal, project_id, aid, op):
        with self.transaction() as db:
            actor = self._acl(db, principal, project_id, {"owner", "member"})
            a = self._get(db, "artifact", aid)
            require(a["project_id"] == project_id)
            m = self._get(db, "meeting", op.meeting_id) if op.meeting_id else None
            require(m is None or m["project_id"] == project_id)
            existed = db.execute("SELECT 1 FROM ops WHERE artifact=? AND op_id=?", (aid, op.op_id)).fetchone()
            result = self._apply(db, actor, a, op, m)
            if m and not existed:
                self._append(db, m, uid("op"), "artifact.changed", actor["id"], {"artifact_id": aid, "op_id": op.op_id})
                self._put(db, "meeting", m["id"], m)
            return result

    def claim_agent(self, principal, mid, lease_seconds=120):
        with self.transaction() as db:
            m = self._get(db, "meeting", mid)
            self._acl(db, principal, m["project_id"], {"agent"})
            if not self.ready(m) or m["semantic_seq"] <= m["cursor"]:
                return None
            lease = m["agent_lease"]
            if lease and lease["expires"] > time.time():
                return None
            a = self._get(db, "artifact", m["artifact_id"])
            lease = {"id": uid("lease"), "expires": time.time() + lease_seconds, "generation": m["generation"], "basis_seq": m["semantic_seq"], "artifact_cursor": a["cursor"], "playback_generation": m["playback_generation"]}
            m["agent_lease"] = lease
            self._put(db, "meeting", mid, m)
            ev = [json.loads(r[0]) for r in db.execute("SELECT data FROM events WHERE meeting=? AND seq<=? ORDER BY seq DESC LIMIT 120", (mid, lease["basis_seq"]))][::-1]
            # A final revision replaces its earlier version only in model context.
            latest = {}
            for e in ev:
                if e["type"] == "transcript.final":
                    latest[(e["actor_ref"], e["payload"]["segment_id"])] = e
            ev = [e for e in ev if e["type"] != "transcript.final" or latest[(e["actor_ref"], e["payload"]["segment_id"])]["id"] == e["id"]]
            ev = [e for e in ev if not e["payload"].get("redacted")]
            tasks = [t for t in self._all(db, "task") if t["project_id"] == m["project_id"]][-20:]
            return {"lease": lease, "meeting": m, "artifact": a, "events": ev, "tasks": tasks,
                    "context_window": {"first_seq": ev[0]["seq"] if ev else None, "last_seq": lease["basis_seq"], "bounded": True}}

    def commit_agent(self, principal, mid, lease_id, plan):
        plan = AgentPlan.model_validate(plan)
        with self.transaction() as db:
            m = self._get(db, "meeting", mid)
            actor = self._acl(db, principal, m["project_id"], {"agent"})
            lease = m["agent_lease"]
            require(self.ready(m) and lease and lease["id"] == lease_id and lease["expires"] >= time.time() and lease["generation"] == m["generation"] and lease["basis_seq"] == m["semantic_seq"], "这次推理已过期", 409)
            a = self._get(db, "artifact", m["artifact_id"])
            require(a["cursor"] == lease["artifact_cursor"], "表格已变化，请重新读取", 409)
            output = []
            for i, action in enumerate(plan.actions):
                if action.kind == "set_draft_field":
                    row = next((r for r in a["rows"] if r["id"] == action.row_id), None)
                    require(row, "目标行不存在", 404)
                    op = Operation(op_id=lease_id + "-" + str(i), row_id=action.row_id, field=action.field, value=action.value, evidence_refs=action.evidence_refs, meeting_id=mid, expected_field_version=row["fields"][action.field]["version"])
                    output.append(self._apply(db, actor, a, op, m))
                elif action.kind == "research":
                    output.append(self._queue_task(db, m, a, action))
                elif not m["muted"] and m["playback_generation"] == lease["playback_generation"]:
                    self._append(db, m, uid("say"), "agent.speech", actor["id"], {"text": action.value, "playback_id": uid("speech"), "generation": m["playback_generation"], "expires": time.time() + 20})
            m["cursor"] = lease["basis_seq"]
            m["agent_lease"] = None
            self._append(db, m, uid("run"), "agent.completed", actor["id"], {"actions": len(output), "basis_seq": lease["basis_seq"]})
            self._put(db, "meeting", mid, m)
            return output

    def fail_agent(self, principal, mid, lease_id, reason="model_unavailable"):
        with self.transaction() as db:
            m = self._get(db, "meeting", mid)
            self._acl(db, principal, m["project_id"], {"agent"})
            if m["agent_lease"] and m["agent_lease"]["id"] == lease_id:
                if reason != "stale_context":
                    m["cursor"] = m["agent_lease"]["basis_seq"]
                m["agent_lease"] = None
                self._append(db, m, uid("failure"), "agent.failed", principal["id"], {"reason": reason})
                self._put(db, "meeting", mid, m)

    def _queue_task(self, db, m, a, action):
        require(any(r["id"] == action.row_id for r in a["rows"]), "研究目标不存在", 400)
        self._evidence(db, m, action.evidence_refs)
        require(len(action.value) <= 600 and action.value.strip(), "查证关键词应简短", 400)
        row = next(r for r in a["rows"] if r["id"] == action.row_id)
        for t in self._all(db, "task"):
            if t["project_id"] == m["project_id"] and t["row_id"] == action.row_id and t["query"] == action.value:
                same_basis = all(row["fields"][f]["version"] == v for f, v in t.get("basis_fields", {}).items() if f != "evidence")
                if t["state"] in {"queued", "running"} or (t["state"] == "completed" and same_basis):
                    return t
        t = {"id": uid("task"), "project_id": m["project_id"], "meeting_id": m["id"], "artifact_id": a["id"], "row_id": action.row_id, "query": action.value, "evidence_refs": action.evidence_refs, "basis_version": row["fields"]["evidence"]["version"], "basis_fields": {f: cell["version"] for f, cell in row["fields"].items()}, "meeting_generation": m["generation"], "generation": 1, "state": "queued", "lease": None, "result": None}
        self._put(db, "task", t["id"], t)
        return t

    def tasks(self, principal):
        with self.transaction() as db:
            self._acl(db, principal, principal["project_id"])
            return [t for t in self._all(db, "task") if t["project_id"] == principal["project_id"]]

    def cancel_task(self, principal, tid):
        with self.transaction() as db:
            t = self._get(db, "task", tid)
            self._acl(db, principal, t["project_id"], {"owner", "member"})
            t.update(state="cancelled", generation=t["generation"] + 1, lease=None)
            self._put(db, "task", tid, t)
            return t

    def resume_task(self, principal, tid, mid):
        with self.transaction() as db:
            t = self._get(db, "task", tid)
            self._acl(db, principal, t["project_id"], {"owner"})
            m = self._get(db, "meeting", mid)
            require(m["project_id"] == t["project_id"] and m["artifact_id"] == t["artifact_id"] and self.ready(m), "请先选择同一项目并签入可参与的会议", 409)
            require(t["state"] == "paused", "只有已暂停的任务可以续办", 409)
            source = self._get(db, "meeting", t.get("source_meeting_id", t["meeting_id"]))
            self._evidence(db, source, t["evidence_refs"])
            a = self._get(db, "artifact", t["artifact_id"])
            row = next(r for r in a["rows"] if r["id"] == t["row_id"])
            require(all(row["fields"][f]["version"] == version for f, version in t.get("basis_fields", {"evidence": t["basis_version"]}).items()), "目标行已有修改，请基于新内容重新建任务", 409)
            # Preserve original provenance; the current meeting governs this execution.
            t.setdefault("source_meeting_id", t["meeting_id"])
            t.update(meeting_id=mid, meeting_generation=m["generation"], generation=t["generation"] + 1, state="queued", lease=None)
            self._put(db, "task", tid, t)
            return t

    def claim_task(self, principal):
        with self.transaction() as db:
            self._acl(db, principal, principal["project_id"], {"agent"})
            for t in self._all(db, "task"):
                if t["project_id"] != principal["project_id"]:
                    continue
                m = self._get(db, "meeting", t["meeting_id"])
                if not self.ready(m) or t["meeting_generation"] != m["generation"]:
                    continue
                if t["state"] == "queued" or (t["state"] == "running" and t["lease"]["expires"] < time.time()):
                    t.update(state="running", lease={"id": uid("research-lease"), "expires": time.time() + 45})
                    self._put(db, "task", t["id"], t)
                    return t
            return None

    def finish_task(self, principal, tid, lease_id, result):
        require(isinstance(result, dict) and result.get("state") in {"verified", "unverified", "failed"}, "无效研究结果", 400)
        require(len(encode(result)) <= 20000, "研究结果过大", 400)
        with self.transaction() as db:
            t = self._get(db, "task", tid)
            self._acl(db, principal, t["project_id"], {"agent"})
            m = self._get(db, "meeting", t["meeting_id"])
            require(t["state"] == "running" and t["lease"]["id"] == lease_id and t["lease"]["expires"] >= time.time() and self.ready(m) and t["meeting_generation"] == m["generation"], "任务已取消、暂停或过期", 409)
            a = self._get(db, "artifact", t["artifact_id"])
            row = next(r for r in a["rows"] if r["id"] == t["row_id"])
            t.update(state="completed", result=result, lease=None)
            basis = t.get("basis_fields", {"evidence": t["basis_version"]})
            if any(row["fields"][f]["version"] != v for f, v in basis.items()):
                t["state"] = "superseded"
            try:
                source = self._get(db, "meeting", t.get("source_meeting_id", t["meeting_id"]))
                self._evidence(db, source, t["evidence_refs"])
            except LiveError:
                t["state"] = "superseded"
            e = self._append(db, m, uid("result"), "research.result", principal["id"], {"task_id": tid, "row_id": t["row_id"], "state": t["state"], "result": result}, semantic=t["state"] == "completed")
            # Result stays attached to the originating row/task, even after topic changes.
            if t["state"] == "completed":
                op = Operation(op_id="result-" + tid, row_id=t["row_id"], field="evidence", expected_field_version=t["basis_version"], value=str(result.get("summary", "未核实"))[:8000], evidence_refs=[e["id"]], meeting_id=m["id"])
                self._apply(db, principal, a, op, m)
            self._put(db, "task", tid, t)
            self._put(db, "meeting", m["id"], m)
            return t

    def playback(self, principal, mid, event_id, status):
        """At most one media process claims an output. Unknown playout is never replayed."""
        require(status in {"claimed", "sent", "interrupted", "failed"}, "无效播放状态", 400)
        with self.transaction() as db:
            m = self._get(db, "meeting", mid)
            self._acl(db, principal, m["project_id"], {"agent"})
            row = db.execute("SELECT data FROM events WHERE meeting=? AND json_extract(data,'$.id')=?", (mid, event_id)).fetchone()
            require(row is not None, "播报不存在", 404)
            e = json.loads(row[0])
            require(e["type"] == "agent.speech", "此事件不是播报", 400)
            key = e["payload"]["playback_id"]
            old = db.execute("SELECT data FROM records WHERE kind='playback' AND id=?", (key,)).fetchone()
            if status == "claimed":
                require(not old, "播报已领取；结果未知时不自动重播", 409)
                require(self.ready(m) and not m["muted"] and e["payload"]["generation"] == m["playback_generation"] and e["payload"]["expires"] > time.time(), "播报已过期", 409)
            else:
                require(old and json.loads(old[0])["status"] == "claimed", "播报没有有效领取状态", 409)
            receipt = {"playback_id": key, "event_id": event_id, "status": status, "updated_at": time.time()}
            self._put(db, "playback", key, receipt)
            self._append(db, m, uid("playback"), "playback.status", principal["id"], receipt)
            self._put(db, "meeting", mid, m)
            return receipt
