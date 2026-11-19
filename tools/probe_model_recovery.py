"""Frozen backend recovery acceptance, using disposable state and loopback only."""

import argparse
import json
import os
import select
import subprocess
import tempfile
import threading
import time
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def probe(backend, output, hold=False):
    calls, clients = Counter(), {}

    class Provider(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            name = payload["model"]
            calls[name] += 1
            clients.setdefault(name, set()).add(self.client_address)
            status = 401 if name == "fatal" else 503 if name == "exhaust" or name == "recover" and calls[name] == 1 else 200
            data = ({"choices": [{"message": {"content": '{"actions":[]}'}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
                    if status == 200 else {"error": {"code": "api_key_expired" if status == 401 else "temporary_error"}})
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if status == 503:
                self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider.daemon_threads = True
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    env = {k: v for k, v in os.environ.items() if k in {"HOME", "USER", "TMPDIR", "LANG", "LC_ALL"}}
    env["PATH"] = "/usr/bin:/bin"
    try:
        with tempfile.TemporaryDirectory(prefix="zsjh-model-recovery-") as folder:
            root = Path(folder)
            options = ["--state-dir", str(root / "state"), "--output-root", str(root / "meetings"),
                       "--env-file", str(root / "missing.env")]
            subprocess.run([str(backend), "init", *options], env=env, capture_output=True, check=True, timeout=30)
            process = subprocess.Popen([str(backend), "serve", *options, "--desktop", "--port", "0"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                if not select.select([process.stdout], [], [], 30)[0]:
                    raise TimeoutError("disposable backend handshake timed out")
                handshake = json.loads(process.stdout.readline())
                base = f"http://127.0.0.1:{handshake['port']}"
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

                def request(path, value=None, method=None):
                    headers = {"Authorization": "Bearer " + handshake["owner_token"], "Content-Type": "application/json"}
                    data = json.dumps(value).encode() if value is not None else None
                    with opener.open(urllib.request.Request(base + path, data=data, headers=headers, method=method), timeout=5) as response:
                        return json.loads(response.read())

                def configure(name):
                    settings = request("/live/v2/settings")
                    model = {k: v for k, v in settings["model"].items() if k != "api_key_configured"}
                    search = {k: v for k, v in settings["search"].items() if k not in {"api_key_configured", "effective_key_configured"}}
                    model.update(provider="openai_compatible", base_url=f"http://127.0.0.1:{provider.server_port}",
                                 name=name, api_key="local-synthetic", enabled=True)
                    search.update(enabled=False, use_model_key=False, api_key="")
                    request("/live/v2/settings", {"revision": settings["revision"], "model": model, "search": search}, "PUT")

                def new_meeting(name):
                    configure(name)
                    m = request("/live/v2/meetings", {"title": f"模型恢复合成验证 {name}", "data_scope": "synthetic"})
                    request(f"/live/v2/meetings/{m['id']}/utterances", {"text": "合成普通讨论，用于验证连接恢复。"})
                    return m["id"]

                def wait_for(mid, predicate, timeout=30):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        snapshot = request(f"/live/v2/meetings/{mid}/snapshot")
                        if predicate(snapshot):
                            return snapshot
                        time.sleep(.1)
                    raise TimeoutError("synthetic recovery did not reach expected state")

                recovered = new_meeting("recover")
                waiting = wait_for(recovered, lambda s: s["meeting"]["coordinator_state"] == "retry_wait")
                assert waiting["meeting"]["coordinator_retry"]["attempts"] == 1
                assert waiting["meeting"]["coordinator_error_details"]["http_status"] == 503
                assert waiting["meeting"]["coordinator_error_details"]["transport_phase"] == "http_response"
                done = wait_for(recovered, lambda s: s["meeting"]["coordinator_state"] == "idle")
                cursor = done["meeting"]["coordinator_cursor"]
                assert cursor > 0 and "coordinator_retry" not in done["meeting"] and not done["questions"]
                request(f"/live/v2/meetings/{recovered}/utterances", {"text": "第二段合成普通讨论，不需要重复分析第一段。"})
                wait_for(recovered, lambda s: s["meeting"]["coordinator_state"] == "idle" and s["meeting"]["coordinator_cursor"] > cursor)
                assert calls["recover"] == 3 and len(clients["recover"]) == 1

                fatal = new_meeting("fatal")
                rejected = wait_for(fatal, lambda s: s["meeting"]["coordinator_state"] == "failed")
                assert rejected["meeting"]["coordinator_error_details"]["http_status"] == 401
                assert "coordinator_retry" not in rejected["meeting"]

                exhausted = new_meeting("exhaust")
                stopped = wait_for(exhausted, lambda s: s["meeting"].get("coordinator_retry", {}).get("state") == "exhausted", timeout=35)
                assert calls["exhaust"] == 4 and calls["fatal"] == 1
                assert stopped["meeting"]["coordinator_cursor"] == 0
                assert not stopped["questions"] and not stopped["documents"]
                assert "local-synthetic" not in json.dumps(stopped)
                result = {"status": "passed", "automatic_recovery": True, "http_keep_alive_reused": True,
                    "new_input_cursor_advanced": True, "no_duplicate_questions": True, "fatal_401_not_retried": True,
                    "three_retries_then_stopped": True, "request_phase_and_status_visible": True,
                    "loopback_model_calls": dict(calls), "external_calls": 0, "production_state_used": False}
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if hold:
                    fixture = output.with_name("browser-fixture-private.json")
                    fixture.write_text(json.dumps({"base": base, "owner_token": handshake["owner_token"], "meeting_id": exhausted}))
                    fixture.chmod(0o600)
                    print(json.dumps({"browser_fixture": str(fixture), "url": base + "/workbench"}), flush=True)
                    while True:
                        time.sleep(1)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
    finally:
        provider.shutdown()
        provider.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hold", action="store_true", help="Keep disposable fixture running for browser checks")
    args = parser.parse_args()
    try:
        probe(args.backend.resolve(), args.output.resolve(), args.hold)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
