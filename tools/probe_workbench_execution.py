"""Verify the frozen scheduler against synthetic loopback model/search services."""

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


def probe(backend, output, hold=False):
    first_detection, release_detection, release_research = (threading.Event() for _ in range(3))
    detections, searches, answers = [], [], []
    errors = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_json(self, value):
            raw = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            try:
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/search":
                    searches.append(payload["query"])
                    if not release_research.wait(45):
                        raise TimeoutError("synthetic search gate timed out")
                    self.send_json({"results": [{"title": "合成执行验证来源", "url": "https://fixture.invalid/execution",
                                                "raw_content": QUOTE * 6}]})
                    return
                if payload["messages"][0]["content"].startswith("你是会议查证协调器"):
                    content = json.loads(payload["messages"][1]["content"])
                    detections.append({"payload": payload, "content": content})
                    if len(detections) == 1:
                        first_detection.set()
                        if not release_detection.wait(20):
                            raise TimeoutError("synthetic detection gate timed out")
                    existing = {q["title"]: q for q in content["existing_questions"]}
                    actions = []
                    for segment in content["recent_segments"]:
                        if not segment["is_new"]:
                            continue
                        ref = {"segment_id": segment["segment_id"], "revision": segment["revision"]}
                        if segment["text"] == "补充同一问题":
                            q = existing["合成请求0"]
                            actions.append({"kind": "revise_question", "question_id": q["id"],
                                "expected_version": q["version"], "title": q["title"], "public_query": q["public_query"],
                                "source_refs": [ref], "scope_changed": False})
                        elif segment["text"].startswith("合成请求") and segment["text"] not in existing:
                            number = int(segment["text"].removeprefix("合成请求"))
                            actions.append({"kind": "create_question" if number == 40 else "candidate",
                                "title": segment["text"], "public_query": f"synthetic request {number}", "source_refs": [ref]})
                        if len(actions) == content["action_limit"]:
                            break
                    answer = {"actions": actions}
                else:
                    answers.append(payload)
                    answer = ANSWER
                self.send_json({"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)},
                                             "finish_reason": "stop"}], "usage": {"total_tokens": 20}})
            except Exception as exc:
                errors.append(type(exc).__name__)
                self.send_error(500)

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider.daemon_threads = True
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    env = {k: v for k, v in os.environ.items() if k in {"HOME", "USER", "TMPDIR", "LANG", "LC_ALL"}}
    env["PATH"] = "/usr/bin:/bin"
    try:
        with tempfile.TemporaryDirectory(prefix="zsjh-execution-") as folder:
            root = Path(folder)
            options = ["--state-dir", str(root / "state"), "--output-root", str(root / "meetings"),
                       "--env-file", str(root / "missing.env")]
            subprocess.run([str(backend), "init", *options], env=env, capture_output=True, check=True, timeout=30)
            process = subprocess.Popen([str(backend), "serve", *options, "--desktop", "--port", "0"],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                if not select.select([process.stdout], [], [], 30)[0]:
                    raise TimeoutError("disposable backend handshake timeout")
                handshake = json.loads(process.stdout.readline())
                base = f"http://127.0.0.1:{handshake['port']}"
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

                def request(path, value=None, method=None):
                    headers = {"Authorization": "Bearer " + handshake["owner_token"], "Content-Type": "application/json"}
                    data = json.dumps(value).encode() if value is not None else None
                    with opener.open(urllib.request.Request(base + path, data=data, headers=headers, method=method), timeout=5) as response:
                        return json.loads(response.read())

                settings = request("/live/v2/settings")
                model = {k: v for k, v in settings["model"].items() if k != "api_key_configured"}
                search = {k: v for k, v in settings["search"].items() if k not in {"api_key_configured", "effective_key_configured"}}
                endpoint = f"http://127.0.0.1:{provider.server_port}"
                model.update(provider="openai_compatible", base_url=endpoint, name="synthetic-execution",
                    api_key="synthetic-local", enabled=True, reasoning_effort="max", max_tokens=8192, detection_mode="fast")
                search.update(provider="tavily", base_url=endpoint, api_key="synthetic-local", enabled=True, use_model_key=False)
                request("/live/v2/settings", {"revision": settings["revision"], "model": model, "search": search}, "PUT")
                mid = request("/live/v2/meetings", {"title": "执行优化打包验收（合成）", "data_scope": "synthetic"})["id"]

                def say(text):
                    request(f"/live/v2/meetings/{mid}/utterances", {"text": text})

                def wait_for(predicate, timeout=30):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        value = request(f"/live/v2/meetings/{mid}/snapshot")
                        if predicate(value):
                            return value
                        if errors:
                            raise AssertionError("synthetic provider errors: " + ",".join(errors))
                        time.sleep(.1)
                    raise TimeoutError("execution probe did not reach expected state")

                say("合成请求0")
                if not first_detection.wait(8):
                    raise TimeoutError("detection did not start")
                for n in range(1, 41):
                    say(f"合成请求{n}")
                release_detection.set()
                accumulated = wait_for(lambda s: len(s["questions"]) == 41 and s["meeting"]["coordinator_state"] == "idle")
                assert any(q["state"] == "backlog" for q in accumulated["questions"])
                inflight = next(q for q in accumulated["questions"] if q["title"] == "合成请求0")
                assert inflight["state"] == "running" and len(searches) == 2
                say("补充同一问题")
                mentioned = wait_for(lambda s: any(q["id"] == inflight["id"] and q.get("last_mention_refs") for q in s["questions"]))
                unchanged = next(q for q in mentioned["questions"] if q["id"] == inflight["id"])
                assert unchanged["source_refs"] == inflight["source_refs"] and unchanged["generation"] == inflight["generation"]
                request(f"/live/v2/meetings/{mid}/questions", {"title": "手动优先", "public_query": "synthetic manual"})
                release_research.set()
                done = wait_for(lambda s: len(s["questions"]) == 42
                    and all(q["state"] == "completed" for q in s["questions"])
                    and sum(d["state"] == "saved" for d in s["documents"]) == 42, timeout=35)
                assert set(searches[2:4]) == {"synthetic manual", "synthetic request 40"}, searches[:5]
                assert len(searches) == 42 and len(answers) == 42
                assert all(d["payload"]["reasoning_effort"] == "low" for d in detections)
                assert all(d["payload"]["max_tokens"] == 8192 for d in detections)
                assert all(p["reasoning_effort"] == "max" for p in answers)
                assert any(d["content"]["action_limit"] == 12 for d in detections)
                read = {s["text"] for d in detections for s in d["content"]["recent_segments"] if s["is_new"]}
                assert {f"合成请求{n}" for n in range(41)} <= read
                assert len(detections) < 12
                result = {"status": "passed", "production_state_used": False, "external_calls": 0,
                    "detection_requests": len(detections), "catchup_action_limit": 12,
                    "no_unread_input_skipped": True, "fast_detection_payload": True, "max_answer_preserved": True,
                    "manual_and_explicit_ahead_of_full_backlog": True, "inflight_basis_preserved": True,
                    "same_scope_not_researched_twice": True, "completed_questions": len(done["questions"]),
                    "saved_documents": len(done["documents"]), "loopback_searches": len(searches), "loopback_answers": len(answers)}
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if hold:
                    fixture = output.with_name("execution-browser-fixture-private.json")
                    fixture.write_text(json.dumps({"base": base, "owner_token": handshake["owner_token"], "meeting_id": mid}))
                    fixture.chmod(0o600)
                    print(json.dumps({"fixture": str(fixture), "url": base + "/workbench"}), flush=True)
                    while True:
                        time.sleep(1)
            finally:
                release_detection.set()
                release_research.set()
                process.terminate()
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
    finally:
        provider.shutdown()
        provider.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hold", action="store_true")
    args = parser.parse_args()
    try:
        probe(args.backend.resolve(), args.output.resolve(), args.hold)
    except KeyboardInterrupt:
        pass
