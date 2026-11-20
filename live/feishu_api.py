"""Owner-facing Feishu settings and the local delivery thread."""

import logging
import threading

from flask import Blueprint, g, jsonify, request

from live.feishu_cli import FeishuCLI
from live.feishu_sync import FeishuSync
from live.store import require


class FeishuWorker:
    def __init__(self, sync):
        self.sync = sync
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="feishu-delivery", daemon=True)

    def _run(self):
        while not self.stopped.is_set():
            try:
                if self.sync.process_one():
                    continue
            except Exception as exc:
                # Subprocess details may contain private identifiers or text.
                logging.getLogger(__name__).warning("Feishu delivery loop: %s", type(exc).__name__)
            self.stopped.wait(2)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=2)


def register_feishu(app, wb):
    cli = app.config.get('WORKBENCH_FEISHU_CLIENT') or FeishuCLI()
    sync = FeishuSync(wb.store, cli)
    wb.feishu_sync = sync
    app.extensions['feishu_worker'] = FeishuWorker(sync)

    api = Blueprint('feishu', __name__, url_prefix='/live/v2/feishu')

    @api.before_request
    def owner_only():
        require(g.member['role'] == 'owner', '仅工作区主持人可使用飞书同步设置', 403)

    def fields(*allowed, required=()):
        value = request.get_json(silent=True)
        require(isinstance(value, dict) and not (value.keys() - set(allowed)) and set(required) <= value.keys(),
                '飞书设置参数无效，请刷新后重试', 400)
        return value

    def snapshot():
        settings = sync.settings(g.member)
        labels = {'queued': '等待同步', 'running': '正在同步', 'sent': '已同步', 'failed': '同步失败',
                  'unknown': '结果待核对', 'cancelled': '已取消'}
        jobs = []
        for item in sync.recent(g.member):
            public = {k: item.get(k) for k in ('id', 'title', 'state', 'stage', 'error', 'url')}
            public['state_label'] = labels.get(item['state'], item['state'])
            public['retryable'] = item['state'] in {'failed', 'unknown'}
            jobs.append(public)
        return jsonify(settings={k: v for k, v in settings.items() if k != 'account_key'}, jobs=jobs)

    @api.get('/settings')
    def get_settings():
        return snapshot()

    @api.put('/settings')
    def put_settings():
        value = fields('revision', 'enabled', 'folder', 'chat_id', 'chat_name', required=('revision', 'enabled'))
        value.pop('chat_name', None)  # resolve the authoritative name in the CLI adapter
        sync.save(g.member, value)
        return snapshot()

    @api.get('/status')
    def get_status():
        status = cli.status()
        return jsonify(installed=status['available'], authorized=status['connected'], user_name=status['account_name'],
                       configured=status['configured'], message=status['message'], version=status['version'])

    @api.post('/auth/start')
    def start_auth():
        fields()
        result = cli.auth_start()
        return jsonify(verification_url=result['verification_url'], expires_in=result.get('expires_in'))

    @api.post('/auth/poll')
    def poll_auth():
        fields()
        result = cli.auth_poll()
        return jsonify(state=result['state'], authorized=result['state'] in {'authorized','connected'}, message=result['message'])

    @api.post('/folder')
    def folder():
        value = fields('folder', required=('folder',))
        return jsonify(cli.validate_folder(value['folder']))

    @api.post('/chats')
    def chats():
        value = fields('query', required=('query',))
        return jsonify(items=cli.search_chats(value['query']))

    @api.post('/jobs/<job_id>/retry')
    def retry(job_id):
        value = fields('confirm_unknown')
        sync.retry(g.member, job_id, confirm_unknown=value.get('confirm_unknown') is True)
        return snapshot()

    @api.post('/test')
    def test_delivery():
        fields()
        job = sync.enqueue_test(g.member)
        return jsonify(id=job['id'], message='合成测试已加入队列，请在最近同步中查看文档和通知结果。')

    app.register_blueprint(api)
