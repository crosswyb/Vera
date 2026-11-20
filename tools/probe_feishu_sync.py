"""Run the installed/frozen server against a synthetic CLI in an isolated home on macOS."""

import argparse
import json
import os
import select
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


# Mach-O fixture exercises binary discovery with exactly the PATH of a Finder launch.
# It cannot access the network. It only accepts the commands used in this probe.
CLI = r'''
import Foundation
let args = Array(CommandLine.arguments.dropFirst())
let home = ProcessInfo.processInfo.environment["HOME"]!
func flag(_ name: String) -> String {
    if let i = args.firstIndex(of: name), i+1 < args.count { return args[i+1] }; return ""
}
func emit(_ value: [String:Any]) { let data = try! JSONSerialization.data(withJSONObject:value); print(String(decoding:data, as: UTF8.self)) }
func requireInput(_ ok: Bool) { if !ok { exit(2) } }
let input = String(decoding: FileHandle.standardInput.readDataToEndOfFile(), as: UTF8.self)
let chat = "oc_syntheticmeeting", folder = "fldcnSyntheticMeeting"
let url = "https://example.feishu.cn/docx/SyntheticMeeting"
if args.contains("--version") { print("lark-cli version 1.0.100") }
else if args.starts(with:["auth","status"]) { emit(["appId":"synthetic","brand":"feishu","verified":true,"identities":["user":["available":true,"verified":true,"openId":"ou_synthetic","userName":"合成账号"]]]) }
else if args.starts(with:["drive","metas","batch_query"]) { requireInput(flag("--data").contains(folder)); emit(["ok":true,"data":["metas":[["doc_type":"folder","doc_token":folder,"title":"合成文件夹"]]]]) }
else if args.starts(with:["im","+chat-search"]) { emit(["ok":true,"data":["chats":[["name":"合成群","chat_id":chat]]]]) }
else if args.starts(with:["im","chats","get"]) { requireInput(flag("--chat-id")==chat); emit(["ok":true,"data":["name":"合成群"]]) }
else if args.starts(with:["docs","+create"]) { requireInput(flag("--parent-token")==folder && flag("--as")=="user" && flag("--doc-format")=="markdown" && flag("--content")=="-"); try! input.write(toFile: home+"/delivered.md", atomically:true, encoding:.utf8); emit(["ok":true,"data":["document":["document_id":"SyntheticMeeting","url":url]]]) }
else if args.starts(with:["im","+messages-send"]) { requireInput(flag("--chat-id")==chat && flag("--as")=="user" && !flag("--idempotency-key").isEmpty && input.contains(url)); try! input.write(toFile: home+"/delivered.txt", atomically:true, encoding:.utf8); emit(["ok":true,"data":["message_id":"om_synthetic"]]) }
else { exit(2) }
'''


def run(backend):
    with tempfile.TemporaryDirectory(prefix='feishu-frozen-') as temp:
        home = Path(temp)
        binary = home / '.local/bin/lark-cli'
        binary.parent.mkdir(parents=True)
        swift = home / 'fixture.swift'
        swift.write_text(CLI)
        subprocess.run(['swiftc', str(swift), '-o', str(binary)], check=True, timeout=90)
        args = ['--state-dir', str(home/'state'), '--env-file', str(home/'unused.env'), '--output-root', str(home/'meetings')]
        env = {'HOME': temp, 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG':'en_US.UTF-8'}
        subprocess.run([backend, 'init', *args], env=env, stdout=subprocess.DEVNULL, check=True, timeout=30)
        p = subprocess.Popen([backend, 'serve', *args, '--desktop', '--port','0'], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            assert select.select([p.stdout], [], [], 30)[0], 'server startup timed out'
            ready = json.loads(p.stdout.readline())
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def req(path, data=None, method=None):
                req = urllib.request.Request(f"http://127.0.0.1:{ready['port']}/live/v2/"+path,
                            headers={'Authorization':'Bearer '+ready['owner_token'],'Content-Type':'application/json'},
                            data=None if data is None else json.dumps(data).encode(), method=method)
                with opener.open(req,timeout=10) as response: return json.load(response)
            assert req('feishu/status')['authorized']
            cfg = req('feishu/settings')['settings']
            assert not cfg['enabled']
            folder = 'https://example.feishu.cn/drive/folder/fldcnSyntheticMeeting'
            assert req('feishu/folder', {'folder':folder})['name']=='合成文件夹'
            group = req('feishu/chats', {'query':'合成'})['items'][0]
            req('feishu/settings', {'revision':cfg['revision'], 'enabled':True, 'folder':folder,'chat_id':group['id']}, 'PUT')
            mid = req('meetings', {'title':'飞书会中验收（合成）', 'data_scope':'synthetic'})['id']
            req(f'meetings/{mid}/demo', {})
            deadline = time.monotonic()+25
            while time.monotonic() < deadline:
                jobs = req('feishu/settings')['jobs']
                if jobs and jobs[0]['state'] in {'sent','failed','unknown','cancelled'}: break
                time.sleep(.2)
            assert jobs and jobs[0]['state']=='sent', [(j['state'],j['error']) for j in jobs]
            snapshot = req(f'meetings/{mid}/snapshot')
            assert snapshot['meeting']['state']=='active'
            doc = (home/'delivered.md').read_text()
            message = (home/'delivered.txt').read_text()
            answer = snapshot['questions'][0]['result']['brief']['short_answer']
            assert '## 原文佐证' in doc and answer in doc and answer in message and jobs[0]['url'] in message
            return {'status':'passed', 'cli':'synthetic Mach-O, no network', 'stage':'active meeting',
                    'checked':['saved Markdown','selected folder','selected chat','short answer and link','receipt in settings']}
        finally:
            p.terminate()
            try: p.wait(8)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backend', type=Path)
    print(json.dumps(run(parser.parse_args().backend.resolve()), ensure_ascii=False, indent=2))
