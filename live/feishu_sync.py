"""Durable outbound delivery of saved question documents. No CLI runs in a DB transaction.

FeishuSync(store, cli): settings(principal), save(principal, value), recent(principal),
retry(principal, job_id, *, confirm_unknown=False), process_one() -> bool.
save input: {revision,enabled,folder,chat_id}; folder accepts a Feishu URL or token.
settings output: {revision,enabled,folder_token,folder_name,chat_id,chat_name,account_name,account_key}.
recent returns a list of {id,title,state,stage,error,url,...}; state queued/running/sent/failed/unknown/cancelled.
"""
# enqueue_test(principal) -> one queued job, using only a fixed synthetic document and saved target.

import hashlib
import json
import sqlite3
import time
from contextlib import closing

from live.store import LiveError, require, uid


SETTING = 'wb_feishu_settings'
JOB = 'wb_feishu_job'
DEFAULT = dict(revision=0, enabled=False, folder_token='', folder_name='', chat_id='', chat_name='', account_key='', account_name='')
# Keep the body solely in the existing saved document. A correction or deletion
# invalidates the reference, and an old version cannot leak through the queue.
TEST_TITLE = '真实交互 · 飞书同步测试'
TEST_BODY = '# 飞书同步测试\n\n这是一份合成查证结果。\n\n> 这是测试摘录。\n\n[示例来源](https://example.org/)\n'


def _row(db, kind, key):
    row = db.execute('SELECT data FROM records WHERE kind=? AND id=?', (kind, key)).fetchone()
    return json.loads(row[0]) if row else None


def _settings(db, pid):
    return _row(db, SETTING, pid) or DEFAULT.copy()


def _result_signature(question):
    result = question['result']
    fields = ('answer', 'brief', 'claims', 'uncertainty', 'evidence_status', 'original_evidence')
    material = {field: result.get(field) for field in fields}
    material['title'] = question['title']
    source_fields = ('id', 'title', 'url', 'published_at', 'read_status', 'content_hash')
    material['sources'] = [{field: source.get(field) for field in source_fields} for source in result.get('sources', [])]
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class FeishuSync:
    def __init__(self, store, cli):
        self.store, self.cli = store, cli
        with self.store.transaction() as db:
            db.execute("CREATE INDEX IF NOT EXISTS wb_feishu_dispatch ON records(json_extract(data,'$.state'),json_extract(data,'$.updated_at')) WHERE kind='wb_feishu_job'")
            db.execute("CREATE INDEX IF NOT EXISTS wb_feishu_versions ON records(json_extract(data,'$.question_id'),json_extract(data,'$.revision'),json_extract(data,'$.created_at')) WHERE kind='wb_feishu_job'")

    def settings(self, principal):
        with self.store.transaction() as db:
            self.store._acl(db, principal, principal['project_id'], {'owner'})
            return _settings(db, principal['project_id'])

    def save(self, principal, value):
        original = self.settings(principal)
        require(isinstance(value, dict) and set(value) <= {'revision', 'enabled', 'folder', 'chat_id'} and
                type(value.get('enabled')) is bool and type(value.get('revision')) is int, '飞书同步设置不完整', 400)
        require(value['revision'] == original['revision'], '设置已更新，请刷新后重试', 409)
        settings = dict(original, enabled=value['enabled'])
        if value['enabled']:
            account = self.cli.status(verify=True)
            require(account.get('connected') and account.get('account_key'), '请先授权飞书用户账号', 409)
            folder_input = value.get('folder', original['folder_token'])
            chat_input = value.get('chat_id', original['chat_id'])
            require(isinstance(folder_input, str) and 0 < len(folder_input) <= 2048 and
                    isinstance(chat_input, str) and len(chat_input) <= 128, '请检查飞书目录和通知群', 400)
            folder = self.cli.validate_folder(folder_input)
            chat = self.cli.validate_chat(chat_input) if chat_input else {'name': '', 'id': ''}
            settings.update(account_key=account['account_key'], account_name=account.get('account_name') or '',
                            folder_token=folder['token'], folder_name=folder['name'], chat_id=chat['id'], chat_name=chat['name'])
        pid = principal['project_id']
        with self.store.transaction() as db:
            self.store._acl(db, principal, pid, {'owner'})
            require(_settings(db, pid)['revision'] == original['revision'], '设置已更新，请刷新后重试', 409)
            if settings != original:
                settings['revision'] += 1
                self.store._put(db, SETTING, pid, settings)
                db.execute("""UPDATE records SET data=json_set(data,'$.state','cancelled','$.error',?, '$.updated_at',?)
                              WHERE kind=? AND json_extract(data,'$.project_id')=? AND json_extract(data,'$.state') IN ('queued','failed','unknown')""",
                           ('飞书同步已关闭或目标已变更', time.time(), JOB, pid))
            return settings

    def _insert(self, db, settings, pid, job_id, title, **refs):
        now = time.time()
        job = dict(id=job_id, project_id=pid, title=title, state='queued', stage='document', error='', url='',
                   revision=settings['revision'], created_at=now, updated_at=now, **refs)
        db.execute('INSERT OR IGNORE INTO records VALUES(?,?,?)', (JOB, job_id, json.dumps(job, ensure_ascii=False)))
        return job

    def enqueue_saved(self, db, meeting, question, document):
        pid = meeting['project_id']
        settings = _settings(db, pid)
        if not settings['enabled'] or document['state'] != 'saved' or document.get('basis_stale') or question.get('basis_stale'):
            return
        signature = _result_signature(question)
        refs = dict(doc_id=document['id'], doc_hash=document['hash'], version=document['question_version'],
                    generation=document['question_generation'])
        row = db.execute("""SELECT data FROM records WHERE kind='wb_feishu_job' AND json_extract(data,'$.question_id')=?
                            AND json_extract(data,'$.revision')=? ORDER BY json_extract(data,'$.created_at') DESC LIMIT 1""",
                         (question['id'], settings['revision'])).fetchone()
        last = json.loads(row[0]) if row else {}
        if last.get('signature') == signature:
            last.update(refs)
            if last['state'] == 'cancelled':
                last.update(state='queued', error='', updated_at=time.time())
            self.store._put(db, JOB, last['id'], last)
            return
        self._insert(db, settings, pid, uid('delivery'), question['title'], meeting_id=meeting['id'],
                     question_id=question['id'], signature=signature, **refs)

    def enqueue_test(self, principal):
        pid = principal['project_id']
        with self.store.transaction() as db:
            self.store._acl(db, principal, pid, {'owner'})
            settings = _settings(db, pid)
            require(settings['enabled'], '请先启用并保存飞书同步设置', 409)
            return self._insert(db, settings, pid, uid('test'), TEST_TITLE, test=True)

    def recent(self, principal):
        with self.store.transaction() as db:
            self.store._acl(db, principal, principal['project_id'], {'owner'})
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM records WHERE kind=? AND json_extract(data,'$.project_id')=? ORDER BY json_extract(data,'$.created_at') DESC LIMIT 50", (JOB, principal['project_id']))]

    def _payload(self, db, job):
        settings = _settings(db, job['project_id'])
        if not settings['enabled'] or settings['revision'] != job['revision']:
            return None
        if job.get('test'):
            return settings, TEST_TITLE, TEST_BODY, '合成查证结果已生成。'
        doc = _row(db, 'wb_document', job['doc_id'])
        question = _row(db, 'wb_question', job['question_id'])
        meeting = _row(db, 'wb_meeting', job['meeting_id'])
        if (not doc or not meeting or not question or meeting['state'] not in {'active', 'ended'}
                or question['state'] not in {'completed', 'insufficient'} or doc['state'] != 'saved'
                or question['version'] != job['version'] or question['generation'] != job['generation']
                or question.get('basis_stale') or doc.get('basis_stale') or doc['hash'] != job['doc_hash']):
            return None
        answer = ((question.get('result') or {}).get('brief') or {}).get('short_answer', '')
        if question['state'] == 'insufficient':
            answer = '依据不足。' + answer
        title, body = f"{meeting['title']} · {job['title']}", doc['content']
        previous = db.execute("""SELECT json_extract(data,'$.url') FROM records WHERE kind='wb_feishu_job'
                    AND json_extract(data,'$.question_id')=? AND json_extract(data,'$.revision')=?
                    AND json_extract(data,'$.created_at')<? AND json_extract(data,'$.url')!=''
                    ORDER BY json_extract(data,'$.created_at') DESC LIMIT 1""",
                    (job['question_id'], job['revision'], job['created_at'])).fetchone()
        if previous:
            title = '修订版 · ' + title
            body += f"\n\n[查看上一版飞书文档]({previous[0]})\n"
        return settings, title, body, answer[:300]

    def retry(self, principal, job_id, *, confirm_unknown=False):
        with self.store.transaction() as db:
            self.store._acl(db, principal, principal['project_id'], {'owner'})
            job = self.store._get(db, JOB, job_id)
            require(job['project_id'] == principal['project_id'], '找不到此任务', 404)
            require(job['state'] in {'failed', 'unknown'}, '该任务无需重试', 409)
            require(job['state'] != 'unknown' or confirm_unknown is True, '上次发送结果未知，请先到飞书核对并确认重试', 409)
            require(self._payload(db, job), '设置或文档已变更，此任务不能重新发送', 409)
            job.update(state='queued', error='', updated_at=time.time())
            self.store._put(db, JOB, job['id'], job)
            return job

    def _claim(self):
        # The idle poll uses a read connection and cannot wait behind a recording writer.
        query = """SELECT id FROM records WHERE kind='wb_feishu_job' AND
                   (json_extract(data,'$.state')='queued' OR
                    (json_extract(data,'$.state')='running' AND json_extract(data,'$.updated_at')<?))
                   ORDER BY json_extract(data,'$.updated_at'),id LIMIT 1"""
        cutoff = time.time() - 600  # Longer than the bounded CLI call and auth preflight combined.
        with closing(sqlite3.connect(self.store.path)) as db:
            if not db.execute(query, (cutoff,)).fetchone():
                return None
        with self.store.transaction() as db:
            row = db.execute(query, (cutoff,)).fetchone()
            if not row:
                return None
            job = self.store._get(db, JOB, row[0])
            if job['state'] == 'running':
                job.update(state='unknown', error='上次发送中断，请在飞书核对后重试')
            elif not self._payload(db, job):
                job.update(state='cancelled', error='同步已关闭、目标变更或文档版本已失效')
            else:
                job.update(state='running', lease=uid('send'))
            job['updated_at'] = time.time()
            self.store._put(db, JOB, job['id'], job)
            return job

    def process_one(self):
        job = self._claim()
        if not job:
            return False
        if job['state'] != 'running':
            return True
        dispatched = False
        update = {}
        try:
            account = self.cli.status(verify=True)
            with self.store.transaction() as db:
                current = _row(db, JOB, job['id'])
                payload = self._payload(db, current) if current and current.get('lease') == job['lease'] and current['state'] == 'running' else None
            if not payload:
                update = dict(state='cancelled', error='发送前文档或同步设置已变更')
            else:
                settings, title, body, answer = payload
                require(account.get('connected') and account.get('account_key') == settings['account_key'],
                        '飞书账号未授权或账号已变更，请重新连接原账号', 409)
                if job['stage'] == 'document':
                    dispatched = True
                    receipt = self.cli.create_document(settings['folder_token'], title, body)
                    update = dict(state='queued' if settings['chat_id'] else 'sent', stage='message' if settings['chat_id'] else 'document',
                                  url=receipt['url'], remote_id=receipt['document_id'])
                else:
                    dispatched = True
                    receipt = self.cli.send_message(settings['chat_id'], f"{title}\n{answer}\n{job['url']}", job['id'])
                    update = dict(state='sent', message_id=receipt['message_id'])
        except LiveError as exc:
            unknown = dispatched and getattr(exc, 'uncertain', True)
            update = dict(state='unknown' if unknown else 'failed', error=str(exc)[:500])
        except Exception:
            update = dict(state='unknown' if dispatched else 'failed', error='未收到可核验的飞书回执，请检查连接和目标')
        with self.store.transaction() as db:
            current = _row(db, JOB, job['id'])
            if current and current['state'] == 'running' and current.get('lease') == job['lease']:
                current.update(**update, updated_at=time.time())
                if current['state'] == 'queued' and not self._payload(db, current):
                    current.update(state='cancelled', error='文档已发送，通知前设置或文档已变更')
                current.pop('lease', None)
                self.store._put(db, JOB, job['id'], current)
        return True
