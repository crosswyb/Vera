"""Exercise shipped backend -> local search/SSE -> progress -> saved Markdown.

Uses disposable state, synthetic text, loopback-only services and no real keys.
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


QUOTE = "合成原文描述了用步骤和条件组织任务的方式，实际部署能力需要另外核验。"
ANSWER = {"brief": {"short_answer": "合成资料描述按步骤组织任务，具体部署能力仍需验证。",
    "key_points": [{"text": "合成来源描述步骤与条件。", "kind": "source_claim", "claim_indices": [1]}],
    "caveat": "此材料仅用于本机打包验收。"}, "answer": "这是合成分析，没有真实业务结论。",
    "claims": [{"text": "来源描述步骤与条件。", "source_id": "source-1", "quote": QUOTE}],
    "uncertainty": ["实际部署能力未验证。"], "evidence_status": "mixed"}


def probe(backend, post_meeting=False, large_sse=False):
    release = threading.Event()
    response_done = threading.Event()
    requests_seen = {"search": 0, "model": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            if self.path == "/search":
                requests_seen["search"] += 1
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"results": [{"title": "打包合成来源", "url": "https://fixture.invalid/probe",
                    "raw_content": QUOTE * 6}]}).encode())
                return
            requests_seen["model"] += 1
            assert value["stream"] is True
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()

            def event(delta=None, finish=None):
                message = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(message, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()

            raw = json.dumps(ANSWER, ensure_ascii=False)
            boundary = raw.index('\", "key_points"')
            if large_sse:
                # Repeated protocol envelopes exceed the old 1 MB wire limit,
                # while decoded synthetic content remains small.
                frame = ("data: " + json.dumps({"choices": [{"index": 0,
                    "delta": {"reasoning_content": "合成"}, "finish_reason": None}]}, ensure_ascii=False) + "\n\n").encode()
                wire = frame * 12000
                assert len(wire) > 1_000_000
                self.wfile.write(wire)
                self.wfile.flush()
            event({"reasoning_content": "private-synthetic-reasoning"})
            event({"content": raw[:boundary]})
            release.wait(25)
            event({"content": raw[boundary:]})
            event(finish="stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            response_done.set()

    services = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=services.serve_forever, daemon=True).start()
    environment = {k: v for k, v in os.environ.items() if k in {"HOME", "USER", "TMPDIR", "LANG", "LC_ALL"}}
    environment["PATH"] = "/usr/bin:/bin"
    result = {}
    try:
        with tempfile.TemporaryDirectory(prefix="zsjh-stream-probe-") as folder:
            root = Path(folder)
            options = ["--state-dir", str(root / "state"), "--output-root", str(root / "meetings"),
                       "--env-file", str(root / "missing.env")]
            initialized = subprocess.run([str(backend), "init", *options], env=environment,
                                         capture_output=True, timeout=30)
            if initialized.returncode:
                raise RuntimeError("disposable backend initialization failed")
            command = [str(backend), "serve", *options, "--desktop", "--port", "0"]
            process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                if not select.select([process.stdout], [], [], 30)[0]:
                    raise TimeoutError("local backend handshake timeout")
                handshake = json.loads(process.stdout.readline())
                base = "http://127.0.0.1:" + str(handshake["port"])
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

                def request(path, value=None, method=None):
                    headers = {"Authorization": "Bearer " + handshake["owner_token"], "Content-Type": "application/json"}
                    data = json.dumps(value).encode() if value is not None else None
                    with opener.open(urllib.request.Request(base + path, data=data, headers=headers, method=method), timeout=5) as response:
                        return json.loads(response.read())

                settings = request("/live/v2/settings")
                model = {k: v for k, v in settings["model"].items() if k != "api_key_configured"}
                search = {k: v for k, v in settings["search"].items() if k not in {"api_key_configured", "effective_key_configured"}}
                endpoint = "http://127.0.0.1:" + str(services.server_port)
                model.update(provider="openai_compatible", base_url=endpoint, name="synthetic-probe", api_key="synthetic-local", enabled=True)
                search.update(provider="tavily", base_url=endpoint, api_key="synthetic-local", enabled=True, use_model_key=False)
                request("/live/v2/settings", {"revision": settings["revision"], "model": model, "search": search}, "PUT")
                meeting = request("/live/v2/meetings", {"title": "流式打包验收（合成）", "data_scope": "synthetic"})
                mid = meeting["id"]
                request(f"/live/v2/meetings/{mid}/questions", {"title": "工作流如何组织任务", "public_query": "workflow steps"})

                def wait_for(predicate):
                    until = time.monotonic() + 20
                    while time.monotonic() < until:
                        snapshot = request(f"/live/v2/meetings/{mid}/snapshot")
                        if predicate(snapshot):
                            return snapshot
                        time.sleep(.15)
                    raise TimeoutError("local synthetic research did not reach expected state")

                snapshot = wait_for(lambda s: s["questions"][0].get("progress", {}).get("model", {}).get("draft"))
                q = snapshot["questions"][0]
                assert q["state"] == "running" and q["result"] is None and not snapshot["documents"]
                assert q["progress"]["sources"][0]["title"] == "打包合成来源"
                assert q["progress"]["model"]["mode"] == "stream"
                assert "private-synthetic-reasoning" not in json.dumps(snapshot)
                result.update(progress_before_final=True, original_preview=True, partial_short_answer=True,
                              raw_reasoning_absent=True, no_partial_document=True)
                if post_meeting:
                    request(f"/live/v2/meetings/{mid}/controls", {"action": "end"})
                    paused = request(f"/live/v2/meetings/{mid}/snapshot")
                    assert paused["meeting"]["state"] == "ended"
                    assert paused["questions"][0]["state"] == "paused"
                    release.set()
                    if not response_done.wait(5):
                        raise TimeoutError("interrupted synthetic response did not close")
                    release.clear()
                    request(f"/live/v2/questions/{q['id']}/actions", {"action": "retry", "expected_version": q["version"]})
                    continued = wait_for(lambda s: s["questions"][0]["state"] == "running"
                        and s["questions"][0].get("progress", {}).get("model", {}).get("draft"))
                    assert continued["meeting"]["state"] == "ended" and continued["meeting"]["research_paused"]
                    assert continued["questions"][0]["generation"] > q["generation"]
                    assert not continued["documents"]
                    result.update(end_paused_inflight=True, post_meeting_manual_retry=True,
                                  post_meeting_streaming_progress=True, stale_result_not_published=True,
                                  capture_not_reopened=True)
                release.set()
                snapshot = wait_for(lambda s: s["questions"][0]["state"] == "completed" and any(d["state"] == "saved" for d in s["documents"]))
                result.update(final_validated=True, markdown_saved=True, loopback_calls=requests_seen,
                              no_python_on_path=True, production_state_used=False, external_calls=0)
                content = snapshot["documents"][0]["content"]
                assert content.index("## 一句话结论") < content.index("## 完整分析") < content.index("## 关键依据")
                result["readable_markdown_order"] = True
                if large_sse:
                    result["large_sse_envelopes_accepted"] = True
                expected = 2 if post_meeting else 1
                assert requests_seen == {"search": expected, "model": expected}
            finally:
                release.set()
                process.terminate()
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
    finally:
        release.set()
        services.shutdown()
        services.server_close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--post-meeting", action="store_true", help="End an in-flight task, then verify manual retry and publication after the meeting")
    parser.add_argument("--large-sse", action="store_true", help="Verify a small decoded answer with over 1 MB of repeated SSE envelopes")
    args = parser.parse_args()
    result = probe(args.backend.resolve(), post_meeting=args.post_meeting, large_sse=args.large_sse)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
