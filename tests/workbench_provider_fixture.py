"""Runnable UI fixture: real loopback HTTP, synthetic model and search only.

Run with .venv/bin/python tests/workbench_provider_fixture.py --state-dir <new-dir>.
No provider credentials or meeting content from the installed App are loaded.
"""

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from werkzeug.serving import WSGIRequestHandler, make_server

from live.store import LiveStore
from live.workbench_runtime import WorkbenchRuntime
from live_api import create_app


QUOTE = "工作流通过明确的步骤和条件组织任务，具体适用范围仍需要结合会议讨论核对。"
BODY = "本机合成测试原文，用于验证可配置搜索服务和引用保存。" + QUOTE * 5


class QuietRequests(WSGIRequestHandler):
    def log(self, *_args, **_kwargs):
        pass


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        content = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        status = 200
        if content.get("model") == "expired-key-fixture":
            status = 401
            value = {"error": {"type": "authentication_error", "code": "api_key_expired", "message": "local-model-fixture"}}
        elif content.get("model") == "rejected-parameter-fixture":
            status = 400
            value = {"error": {"type": "invalid_request_error", "param": "reasoning_effort", "message": "local-model-fixture"}}
        elif self.path.endswith("/search"):
            tavily = self.path == "/search"
            value = {"results": [{"title": "合成工作流资料", "url": "https://fixture.invalid/workflow",
                "content": "这是搜索摘要，不能作为正文。" if tavily else BODY,
                **({"raw_content": BODY} if tavily else {})}]}
        else:
            system = content["messages"][0]["content"]
            prompt = json.loads(content["messages"][1]["content"])
            if "connection test" in system:
                answer = {"ok": True}
            elif "recent_segments" in prompt:
                segment = next((s for s in prompt["recent_segments"] if s["is_new"]), None)
                answer = {"actions": [] if not segment or prompt["existing_questions"] else [{
                    "kind": "candidate", "title": "工作流定义与适用范围（合成查证）", "public_query": "workflow definition official documentation",
                    "source_refs": [{"segment_id": segment["segment_id"], "revision": segment["revision"]}]}]}
            elif "sources" in prompt:
                answer = {"answer": "合成参考回答：材料描述了工作流的步骤组织方式，供参会者核对，尚不构成会议结论。",
                    "brief": {"short_answer": "合成资料将工作流描述为按步骤和条件组织任务；具体适用范围仍需核对。",
                        "key_points": [{"text": "资料描述了明确的步骤与条件，不能据此证明某个产品的实际能力。", "kind": "source_claim", "claim_indices": [1]},
                                       {"text": "当前讨论需要进一步明确流程是否固定、哪些节点需要人工判断。", "kind": "explanation", "claim_indices": []}],
                        "caveat": "本页使用合成模型与搜索服务验证流程，尚未进行真实资料查证。"},
                    "claims": [{"text": "合成来源说明工作流可按步骤与条件组织任务。", "source_id": prompt["sources"][0]["id"], "quote": QUOTE}],
                    "uncertainty": ["这是本机协议模拟，不是真实联网查证。"], "evidence_status": "supported"}
                if prompt.get("question") == "校验失败示例":
                    answer["claims"] *= 14
            else:
                segment = prompt["segments"][0]
                answer = {"overview": "合成会议验证了服务配置与查证资料生成。", "topics": [{"text": "讨论了工作流定义的查证请求。",
                    "source_refs": [{"segment_id": segment["segment_id"], "revision": segment["revision"]}]}],
                    "decisions": [], "actions": [], "open_questions": []}
            value = {"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.state_dir.resolve()
    root.mkdir(parents=True, mode=0o700)
    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    endpoint = "http://127.0.0.1:" + str(provider.server_port)
    os.environ.update(ZSJH_LIVE_MODEL_PROVIDER="deepseek", ZSJH_LIVE_MODEL_ENDPOINT=endpoint,
        ZSJH_LIVE_MODEL_NAME="ui-fixture-model", ZSJH_LIVE_MODEL_API_KEY="local-model-fixture",
        ZSJH_LIVE_MODEL_THINKING="disabled", ZSJH_LIVE_MODEL_REASONING_EFFORT="default",
        ZSJH_WB_SEARCH_PROVIDER="deepseek", ZSJH_WB_SEARCH_ENDPOINT=endpoint, ZSJH_WB_SEARCH_API_KEY="",
        ZSJH_WB_MODEL_APPROVED="true", ZSJH_WB_SEARCH_APPROVED="true", ZSJH_WB_AUDIO_APPROVED="false",
        ZSJH_WB_MODEL_REQUEST_LIMIT="unlimited", ZSJH_WB_SEARCH_REQUEST_LIMIT="unlimited",
        ZSJH_LIVE_MODEL_USAGE_DB=str(root / "model-usage.sqlite3"), ZSJH_WB_SEARCH_USAGE_DB=str(root / "search-usage.sqlite3"))
    database = root / "workbench.sqlite3"
    if database.exists():
        raise SystemExit("Choose a new fixture directory; existing fixture data is preserved.")
    LiveStore(database).seed(fixture=True)
    app = create_app(database, {"WORKBENCH_ROOT": root / "meetings", "WORKBENCH_SETTINGS_PATH": root / "providers.json"})
    runtime = WorkbenchRuntime.configured(app.extensions["workbench"]).start()
    server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietRequests)
    print(json.dumps({"url": "http://127.0.0.1:" + str(server.server_port) + "/workbench", "provider": endpoint}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.close(); server.server_close(); provider.shutdown()


if __name__ == "__main__":
    main()
