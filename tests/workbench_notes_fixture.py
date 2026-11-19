"""Disposable loopback provider and built backend for transcript-note acceptance."""

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

from test_workbench_original_evidence import GB_QUERY, GB_QUESTION, GB_QUOTE, model_answer, search_item


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    searches, lookups, errors = [], [], []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                status = 200
                if self.path == "/search":
                    query = payload["query"]
                    searches.append(query)
                    if query == "合成网络故障":
                        status, value = 503, {"error": {"code": "unavailable"}}
                    else:
                        item = (search_item(url="https://openstd.samr.gov.cn/bzgk/gb/newGbInfo?hcno=fixture", content=None)
                                if query == "GB/T 98765-2024 题录 原文" else search_item())
                        item["raw_content"] = item.pop("content")
                        value = {"results": [item]}
                else:
                    system = payload["messages"][0]["content"]
                    user = json.loads(payload["messages"][1]["content"])
                    if system.startswith("你是会议查证协调器"):
                        actions = []
                        for segment in user["recent_segments"]:
                            if not segment["is_new"]:
                                continue
                            ref = {"segment_id": segment["segment_id"], "revision": segment["revision"]}
                            if segment["text"] == GB_QUESTION:
                                actions.append({"kind": "create_question", "title": GB_QUESTION, "source_refs": [ref],
                                    "public_query": GB_QUERY, "original_target": {"kind": "standard", "identifier": "GB/T 98765-2024", "section": "3.2"}})
                            elif segment["text"] == "合成发言：分别查找题录和故障样例。":
                                for title, query in [("GB/T 98765-2024 能否取得正文？", "GB/T 98765-2024 题录 原文"),
                                                     ("查询合成的故障样例", "合成网络故障")]:
                                    actions.append({"kind": "create_question", "title": title, "source_refs": [ref], "public_query": query})
                        completion = {"actions": actions[:user["action_limit"]]}
                    else:
                        lookups.append(user["original_lookup"])
                        completion = model_answer()
                    value = {"choices": [{"message": {"content": json.dumps(completion, ensure_ascii=False)}, "finish_reason": "stop"}]}
                body = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                errors.append(type(exc).__name__)
                self.send_error(500)

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider.daemon_threads = True
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="zsjh-notes-") as directory:
            root = Path(directory)
            env = {k: v for k, v in os.environ.items() if k in {"HOME", "USER", "TMPDIR", "LANG", "LC_ALL"}}
            opts = ["--state-dir", str(root / "state"), "--output-root", str(root / "out"), "--env-file", str(root / "absent.env")]
            backend = str(args.backend.resolve())
            subprocess.run([backend, "init", *opts], env=env, check=True, capture_output=True, timeout=25)
            process = subprocess.Popen([backend, "serve", *opts, "--desktop", "--audio", "--port", "0"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                assert select.select([process.stdout], [], [], 20)[0], "backend startup timeout"
                ready = json.loads(process.stdout.readline())
                base = f"http://127.0.0.1:{ready['port']}"
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

                def request(path, value=None, method=None, role="owner"):
                    headers = {"Authorization": "Bearer " + ready[role + "_token"], "Content-Type": "application/json"}
                    data = json.dumps(value).encode() if value is not None else None
                    with opener.open(urllib.request.Request(base + path, data=data, headers=headers, method=method), timeout=6) as response:
                        return json.loads(response.read())

                settings = request("/live/v2/settings")
                endpoint = f"http://127.0.0.1:{provider.server_port}"
                model = {k: v for k, v in settings["model"].items() if k != "api_key_configured"}
                search = {k: v for k, v in settings["search"].items() if k not in {"api_key_configured", "effective_key_configured"}}
                model.update(provider="openai_compatible", base_url=endpoint, enabled=True, name="notes-fixture", api_key="local-fixture")
                search.update(provider="tavily", base_url=endpoint, enabled=True, use_model_key=False, api_key="local-fixture")
                request("/live/v2/settings", {"revision": settings["revision"], "model": model, "search": search}, "PUT")
                mid = request("/live/v2/meetings", {"title": "查证便签验收（合成）", "data_scope": "real",
                    "permissions": {"model": True, "search": True, "auto_search": True}})["id"]
                path = f"/live/v2/meetings/{mid}"
                audio_path = f"/internal/live/v2/meetings/{mid}/audio-events"

                def utterance(n, text):
                    request(audio_path, {"meeting_id": mid, "seq": n, "capture_epoch": "fixture", "stream_id": "mic",
                        "type": "transcript.final", "source": "microphone", "occurred_at": "2026-11-18T10:00:00Z",
                        "segment_id": str(n), "revision": 1, "audio_start_ms": n*1000, "audio_end_ms": n*1000 + 500,
                        "text": text}, role="agent")

                utterance(1, GB_QUESTION)
                for n in range(2, 131):
                    utterance(n, f"合成发言 {n}：这是本机界面翻页验证。")
                utterance(131, "合成发言：分别查找题录和故障样例。")
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline and not errors:
                    snapshot = request(path + "/snapshot")
                    if {q["state"] for q in snapshot["questions"]} == {"completed", "insufficient", "failed"}:
                        break
                    time.sleep(.15)
                else:
                    raise AssertionError(f"synthetic pipeline did not finish, fixture errors: {errors}")
                q = next(q for q in snapshot["questions"] if q["title"] == GB_QUESTION)
                doc = request(f"/live/v2/artifacts/{q['artifact_id']}")["content"]
                assert q["anchor_ref"] == {"segment_id": "fixture:mic:1", "revision": 1}
                assert q["result"]["original_evidence"]["quote"] == GB_QUOTE and GB_QUOTE in doc and "找到原文" in doc
                assert lookups[0]["section"] == "3.2" and lookups[0]["candidate_source_ids"] == ["source-1"]
                assert len(lookups) == 1 and len(searches) == 3 and not errors
                page = request(path + "/segments?before=" + str(snapshot["transcript_page"]["next_before"]))
                assert (len(snapshot["segments"]), len(page["segments"])) == (120, 11)
                assert len({s["id"] for s in snapshot["segments"] + page["segments"]}) == 131
                report = {"transcripts": 131, "lookups": len(lookups), "searches": len(searches), "source": "verified",
                          "catalog": "insufficient", "provider_503": "failed", "markdown": "saved"}
                if args.browser:
                    private = {**report, "url": base + "/workbench#meeting/" + mid, "meeting_id": mid,
                               "owner_token": ready["owner_token"], "agent_token": ready["agent_token"]}
                    with os.fdopen(os.open(args.ready_file, os.O_EXCL | os.O_CREAT | os.O_WRONLY, 0o600), "w") as f:
                        json.dump(private, f)
                    print(json.dumps(report), flush=True)
                    input()  # Keep disposable state live for the isolated browser.
                else:
                    args.ready_file.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                    print(json.dumps(report))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    finally:
        provider.shutdown()
        provider.server_close()


if __name__ == "__main__":
    main()
