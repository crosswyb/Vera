"""Exercise packaged per-item recovery through HTTP and loopback providers.

An optional private fixture lets browser QA retry the failed item itself.
All meetings, access codes, model responses and searches are disposable.
"""

import argparse
import json
import os
import select
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from probe_research_progress import ANSWER, QUOTE


def probe(backend, output, hold_file=None):
    calls = {'detecting': 0, 'correcting': 0, 'searching': 0, 'answering': 0}
    errors = []
    source_text = '请查证合成问题A？请查证合成问题B？'
    next_text = '请查证后续的合成问题C？'
    malformed_text = '请查证合成问题D？请查证合成问题E？'
    after_malformed = '请查证坏引用之后的合成问题F？'

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path == '/search':
                    calls['searching'] += 1
                    result = {'results': [{'title': '合成来源', 'url': 'https://fixture.invalid/recovery',
                        'raw_content': QUOTE * 8}]}
                else:
                    system = payload['messages'][0]['content']
                    content = json.loads(payload['messages'][1]['content'])
                    if system.startswith('你是会议查证协调器'):
                        calls['detecting'] += 1
                        options = content['grounding_options']
                        existing = {q['title'] for q in content['existing_questions']}

                        def action(title, quote, invalid=False):
                            return {'kind': 'candidate', 'title': title, 'evidence_need': 'public',
                                'external_fact': '合成资料描述的组织任务方式', 'public_query': 'synthetic ' + title[-1],
                                'grounding_quote_id': 'invalid-option' if invalid else quote['id'],
                                'source_refs': [{'segment_id': quote['segment_id'], 'revision': quote['revision']}]}

                        if 'previous_proposal' in content:
                            calls['correcting'] += 1
                            # The first targeted correction fails visibly. A user
                            # retry succeeds without re-running A or C.
                            result = {'actions': [] if calls['correcting'] == 1 else [action('合成问题B', options[1])]}
                        else:
                            actions = []
                            for segment in content['recent_segments']:
                                if not segment['is_new']:
                                    continue
                                quotes = [q for q in options if q['segment_id'] == segment['segment_id']]
                                if segment['text'] == source_text and '合成问题A' not in existing:
                                    actions.extend([action('合成问题A', quotes[0]), action('合成问题B', quotes[1], True)])
                                if segment['text'] == next_text and '合成问题C' not in existing:
                                    actions.append(action('合成问题C', quotes[0]))
                                if segment['text'] == after_malformed and '合成问题F' not in existing:
                                    actions.append(action('合成问题F', quotes[0]))
                                if segment['text'] == malformed_text:
                                    if content.get('recovery_context', {}).get('kind') == 'scan':
                                        hints = content['recovery_context'].get('unverified_proposals', [])
                                        assert any(h.get('title') == '合成问题E' and h.get('public_query') == 'synthetic E' for h in hints)
                                        actions.append(action('合成问题E', quotes[1]))
                                    else:
                                        invalid = action('合成问题E', quotes[1])
                                        invalid['source_refs'][0]['segment_id'] = ['错误字段类型']
                                        actions.extend([action('合成问题D', quotes[0]), invalid])
                            result = {'actions': actions}
                    elif system.startswith('判断回答会议问题'):
                        result = {'route': 'web_search'}
                    else:
                        calls['answering'] += 1
                        result = ANSWER
                    result = {'choices': [{'message': {'content': json.dumps(result, ensure_ascii=False)},
                        'finish_reason': 'stop'}], 'usage': {'total_tokens': 20}}
                raw = json.dumps(result, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except Exception as exc:
                errors.append(type(exc).__name__)
                self.send_error(500)

    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    provider.daemon_threads = True
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    environment = {k: v for k, v in os.environ.items() if k in {'HOME', 'USER', 'TMPDIR', 'LANG', 'LC_ALL'}}
    environment['PATH'] = '/usr/bin:/bin'
    try:
        with tempfile.TemporaryDirectory(prefix='zsjh-recovery-frozen-') as directory:
            root = Path(directory)
            options = ['--state-dir', str(root / 'state'), '--output-root', str(root / 'meetings'),
                '--env-file', str(root / 'absent.env')]
            subprocess.run([str(backend), 'init', *options], env=environment, capture_output=True, check=True, timeout=30)
            process = subprocess.Popen([str(backend), 'serve', *options, '--desktop', '--port', '0'],
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                if not select.select([process.stdout], [], [], 30)[0]:
                    raise TimeoutError('isolated backend handshake timeout')
                handshake = json.loads(process.stdout.readline())
                base = f"http://127.0.0.1:{handshake['port']}"
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

                def request(path, body=None, method=None):
                    headers = {'Authorization': 'Bearer ' + handshake['owner_token'], 'Content-Type': 'application/json'}
                    payload = json.dumps(body).encode() if body is not None else None
                    with opener.open(urllib.request.Request(base + path, data=payload, headers=headers, method=method), timeout=5) as response:
                        return json.loads(response.read())

                settings = request('/live/v2/settings')
                model = {k: v for k, v in settings['model'].items() if k != 'api_key_configured'}
                search = {k: v for k, v in settings['search'].items() if k not in {'api_key_configured', 'effective_key_configured'}}
                endpoint = f'http://127.0.0.1:{provider.server_port}'
                model.update(provider='openai_compatible', base_url=endpoint, name='synthetic-recovery', api_key='synthetic-local', enabled=True)
                search.update(provider='tavily', base_url=endpoint, api_key='synthetic-local', enabled=True, use_model_key=False)
                request('/live/v2/settings', {'revision': settings['revision'], 'model': model, 'search': search}, 'PUT')
                mid = request('/live/v2/meetings', {'title': '识别故障隔离验收（合成）', 'data_scope': 'synthetic'})['id']

                def wait_for(predicate, timeout=20):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        snapshot = request(f'/live/v2/meetings/{mid}/snapshot')
                        if predicate(snapshot):
                            return snapshot
                        if snapshot['meeting']['coordinator_state'] in {'failed', 'outcome_unknown'}:
                            raise AssertionError(f"main detection stopped: {snapshot['meeting'].get('coordinator_error')}")
                        if errors:
                            raise AssertionError('synthetic provider errors: ' + ','.join(errors))
                        time.sleep(.1)
                    raise TimeoutError('packaged detection recovery did not reach expected state')

                request(f'/live/v2/meetings/{mid}/utterances', {'text': source_text})
                first = wait_for(lambda s: s['automation_progress']['needs_review'] == 1)
                tid = first['detection_tasks'][0]['id']
                # Put the failed item behind the latest transcript page so the
                # browser must load history before locating its original basis.
                for n in range(130):
                    request(f'/live/v2/meetings/{mid}/utterances', {'text': f'历史分页合成发言{n}'})
                request(f'/live/v2/meetings/{mid}/utterances', {'text': next_text})
                ready = wait_for(lambda s: len(s['questions']) == 2 and all(q['state'] == 'completed' for q in s['questions'])
                    and s['automation_progress']['pending'] == 0)
                assert {q['title'] for q in ready['questions']} == {'合成问题A', '合成问题C'}
                assert calls['searching'] == 2 and calls['correcting'] == 1
                result = {'status': 'waiting_for_browser' if hold_file else 'running', 'production_state_used': False,
                    'external_calls': 0, 'new_input_continued_without_manual_retry': True,
                    'unresolved_item_visible': True, 'no_search_for_rejected_item': True,
                    'before_retry': dict(calls)}
                if hold_file:
                    hold_file.write_text(json.dumps({'base': base, 'owner_token': handshake['owner_token'],
                        'meeting_id': mid, 'task_id': tid, 'pid': os.getpid()}), encoding='utf-8')
                    hold_file.chmod(0o600)
                    print(json.dumps({'state': 'browser_ready', 'fixture': str(hold_file)}), flush=True)
                    deadline = time.monotonic() + 600
                    while not hold_file.with_suffix('.stop').exists() and time.monotonic() < deadline:
                        time.sleep(.2)
                else:
                    request(f'/live/v2/meetings/{mid}/detection-tasks/{tid}/retry', {})
                final = wait_for(lambda s: len(s['questions']) == 3 and all(q['state'] == 'completed' for q in s['questions'])
                    and s['automation_progress']['complete'])
                assert len(final['documents']) == 3
                assert calls['searching'] == 3 and calls['correcting'] == 2
                result.update(status='passed', after_retry=dict(calls), questions=3, documents=3,
                    targeted_retry_only=True, original_questions_not_researched_twice=True)
                if not hold_file:
                    request(f'/live/v2/meetings/{mid}/utterances', {'text': malformed_text})
                    recovered = wait_for(lambda s: len(s['questions']) == 5 and s['automation_progress']['complete']
                        and all(q['state'] == 'completed' for q in s['questions']))
                    assert {q['title'] for q in recovered['questions']} == {'合成问题' + c for c in 'ABCDE'}
                    assert calls['searching'] == 5 and len(recovered['documents']) == 5
                    request(f'/live/v2/meetings/{mid}/utterances', {'text': after_malformed})
                    later = wait_for(lambda s: len(s['questions']) == 6 and s['automation_progress']['complete']
                        and all(q['state'] == 'completed' for q in s['questions']))
                    assert {q['title'] for q in later['questions']} == {'合成问题' + c for c in 'ABCDEF'}
                    assert calls['searching'] == 6 and len(later['documents']) == 6
                    result.update(malformed_reference_good_sibling_saved=True, malformed_range_recovered=True,
                        later_input_processed=True, final_questions=6, final_documents=6, final_searches=calls['searching'])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return result
            finally:
                process.terminate()
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                if hold_file:
                    hold_file.unlink(missing_ok=True)
                    hold_file.with_suffix('.stop').unlink(missing_ok=True)
    finally:
        provider.shutdown()
        provider.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--hold-file', type=Path)
    args = parser.parse_args()
    probe(args.backend.resolve(), args.output.resolve(), args.hold_file)
