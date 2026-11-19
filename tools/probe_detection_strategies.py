"""Exercise the shipped backend's live/import lanes using loopback-only providers."""

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


def probe(backend):
    blocked, release, realtime_seen = (threading.Event() for _ in range(3))
    detections, searches, answers, errors = [], [], [], []
    texts = [f"导入合成第{i:03d}段：这是仅用于流程验证的合成发言。" for i in range(676)]
    boundaries = {texts[0]: "导入首段合成问题", texts[-1]: "导入末段合成问题",
                  "实时合成：请查证这个当前提出的问题。": "实时合成问题",
                  "会后合成：请查证工作流的定义并给出来源。": "会后新增合成问题"}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/search":
                    searches.append(payload["query"])
                    result = {"results": [{"title": "合成来源", "url": "https://fixture.invalid/detection",
                                           "raw_content": QUOTE * 8}]}
                else:
                    system = payload["messages"][0]["content"]
                    content = json.loads(payload["messages"][1]["content"])
                    if system.startswith("你是会议查证协调器"):
                        new = [s for s in content["recent_segments"] if s["is_new"]]
                        detections.append(new)
                        if any(s["text"].startswith("导入合成") for s in new) and not blocked.is_set():
                            blocked.set()
                            if not release.wait(45):
                                raise TimeoutError("synthetic imported detection gate timed out")
                        if any(s["text"].startswith("实时合成") for s in new):
                            realtime_seen.set()
                        existing = {q["title"] for q in content["existing_questions"]}
                        result = {"actions": [{"kind": "create_question", "title": boundaries[s["text"]],
                            "public_query": "synthetic source evidence", "source_refs": [
                                {"segment_id": s["segment_id"], "revision": s["revision"]}]}
                            for s in new if s["text"] in boundaries and boundaries[s["text"]] not in existing]}
                    elif system.startswith("你是会议纪要整理员"):
                        first = content["segments"][0]
                        result = {"overview": "合成会后总结", "topics": [{"text": "合成议题", "source_refs": [
                            {"segment_id": first["segment_id"], "revision": first["revision"]}]}],
                            "decisions": [], "actions": [], "open_questions": []}
                    else:
                        answers.append(content)
                        result = ANSWER
                    result = {"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)},
                                             "finish_reason": "stop"}], "usage": {"total_tokens": 20}}
                raw = json.dumps(result, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except Exception as exc:
                errors.append(type(exc).__name__)
                self.send_error(500)

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider.daemon_threads = True
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    environment = {k: v for k, v in os.environ.items() if k in {"HOME", "USER", "TMPDIR", "LANG", "LC_ALL"}}
    environment["PATH"] = "/usr/bin:/bin"
    try:
        with tempfile.TemporaryDirectory(prefix="zsjh-detection-strategies-") as folder:
            root = Path(folder)
            options = ["--state-dir", str(root / "state"), "--output-root", str(root / "meetings"),
                       "--env-file", str(root / "missing.env")]
            subprocess.run([str(backend), "init", *options], env=environment, capture_output=True, check=True, timeout=30)
            process = subprocess.Popen([str(backend), "serve", *options, "--desktop", "--audio", "--port", "0"],
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                if not select.select([process.stdout], [], [], 30)[0]:
                    raise TimeoutError("disposable backend handshake timeout")
                handshake = json.loads(process.stdout.readline())
                base = f"http://127.0.0.1:{handshake['port']}"
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

                def request(path, value=None, method=None, agent=False):
                    headers = {"Authorization": "Bearer " + handshake["agent_token" if agent else "owner_token"],
                               "Content-Type": "application/json"}
                    data = json.dumps(value).encode() if value is not None else None
                    with opener.open(urllib.request.Request(base + path, data=data, headers=headers, method=method), timeout=8) as response:
                        return json.loads(response.read())

                def wait_for(mid, predicate, timeout=30):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        snapshot = request(f"/live/v2/meetings/{mid}/snapshot")
                        if predicate(snapshot):
                            return snapshot
                        if errors:
                            raise AssertionError("loopback provider errors: " + ",".join(errors))
                        time.sleep(.1)
                    raise TimeoutError("detection strategy probe did not reach expected state")

                def create_import(title, paused=False):
                    mid = request("/live/v2/meetings", {"title": title, "data_scope": "synthetic", "input_kind": "audio_file"})["id"]
                    if paused:
                        request(f"/live/v2/meetings/{mid}/controls", {"action": "pause_research"})
                    request(f"/internal/live/v2/meetings/{mid}/audio-import", {
                        "import_id": "fixture", "file_name": "synthetic.wav", "duration_ms": 2000000, "bytes": 1000}, agent=True)
                    return mid

                def append(mid, contents):
                    for i, text in enumerate(contents):
                        request(f"/internal/live/v2/meetings/{mid}/audio-events", {
                            "meeting_id": mid, "capture_epoch": "fixture", "stream_id": "fixture-file", "seq": i + 1,
                            "type": "transcript.final", "source": "file", "occurred_at": "2026-11-19T12:00:00Z",
                            "segment_id": f"s{i}", "revision": 1, "text": text, "audio_start_ms": i * 1000, "audio_end_ms": i * 1000 + 900}, agent=True)
                    request(f"/internal/live/v2/meetings/{mid}/audio-import/finish", {
                        "import_id": "fixture", "state": "completed"}, agent=True)

                settings = request("/live/v2/settings")
                model = {k: v for k, v in settings["model"].items() if k != "api_key_configured"}
                search = {k: v for k, v in settings["search"].items() if k not in {"api_key_configured", "effective_key_configured"}}
                endpoint = f"http://127.0.0.1:{provider.server_port}"
                model.update(provider="openai_compatible", base_url=endpoint, name="synthetic-strategies",
                    api_key="fixture-only", enabled=True, reasoning_effort="max", max_tokens=8192, detection_mode="fast")
                search.update(provider="tavily", base_url=endpoint, api_key="fixture-only", enabled=True, use_model_key=False)
                request("/live/v2/settings", {"revision": settings["revision"], "model": model, "search": search}, "PUT")
                imported = create_import("676段导入合成验收")
                append(imported, texts)
                if not blocked.wait(8):
                    raise TimeoutError("imported detection did not start")
                live = request("/live/v2/meetings", {"title": "实时优先合成验收", "data_scope": "synthetic"})["id"]
                started = time.monotonic()
                request(f"/live/v2/meetings/{live}/utterances", {"text": "实时合成：请查证这个当前提出的问题。"})
                if not realtime_seen.wait(5):
                    raise AssertionError("live detection was blocked by imported-audio detection")
                realtime_delay = round(time.monotonic() - started, 3)
                assert not release.is_set()
                release.set()
                done = wait_for(imported, lambda s: s["meeting"]["coordinator_state"] == "idle"
                    and s["meeting"]["coordinator_cursor"] == s["meeting"]["semantic_seq"]
                    and len(s["documents"]) == 2 and all(d["state"] == "saved" for d in s["documents"]))
                wait_for(live, lambda s: len(s["documents"]) == 1 and s["documents"][0]["state"] == "saved")
                imported_batches = [[s["text"] for s in batch if s["text"].startswith("导入合成")] for batch in detections]
                imported_batches = [batch for batch in imported_batches if batch]
                assert [text for batch in imported_batches for text in batch] == texts
                assert len(imported_batches) <= 10, len(imported_batches)
                assert {q["title"] for q in done["questions"]} == {"导入首段合成问题", "导入末段合成问题"}
                ended = create_import("会后续办合成验收", paused=True)
                after_texts = [f"会后合成待处理{i}" for i in range(20)]
                after_texts[-1] = "会后合成：请查证工作流的定义并给出来源。"
                append(ended, after_texts)
                request(f"/live/v2/meetings/{ended}/controls", {"action": "end"})
                assert request(f"/live/v2/meetings/{ended}/snapshot")["meeting"]["coordinator_cursor"] == 0
                request(f"/live/v2/meetings/{ended}/controls", {"action": "resume_research"})
                final = wait_for(ended, lambda s: s["meeting"]["coordinator_state"] == "idle"
                    and s["meeting"]["coordinator_cursor"] == s["meeting"]["semantic_seq"]
                    and len(s["questions"]) == 1 and s["questions"][0]["state"] == "completed"
                    and len(s["documents"]) == 1 and s["documents"][0]["state"] == "saved")
                assert final["meeting"]["state"] == "ended"
                assert final["questions"][0]["title"] == "会后新增合成问题"
                doc_path = root / "meetings" / final["documents"][0]["path"]
                assert doc_path.is_file() and doc_path.suffix == ".md"
                assert "会后新增合成问题" in doc_path.read_text()
                assert [s["text"] for batch in detections for s in batch if s["text"].startswith("会后合成")] == after_texts
                assert len(searches) == len(answers) == 4
                return {"status": "passed", "production_state_used": False, "external_calls": 0,
                    "import_segments": 676, "import_detection_requests": len(imported_batches),
                    "import_no_skipped_or_truncated_segments": True, "first_and_last_question_saved": True,
                    "realtime_detected_while_import_request_blocked": True, "realtime_request_delay_seconds": realtime_delay,
                    "post_meeting_unread_segments_processed": 20, "post_meeting_capture_not_restarted": True,
                    "post_meeting_new_question_researched_and_markdown_saved": True,
                    "loopback_searches": len(searches), "loopback_answers": len(answers), "saved_question_documents": 4}
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
        provider.shutdown()
        provider.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = probe(args.backend.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
