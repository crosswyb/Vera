"""Own AudioHelper JSONL -> authenticated v2 API; no Cicada process required.

Start is explicit. Parent EOF, API failure, meeting end, or duration expiry stops
the helper. Only metadata is printed. A selected playback PID makes synthetic
tests independent of other sounds on the computer.
"""

import argparse
import json
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from live.workbench_contracts import AudioEvent


def run(endpoint, token, meeting, helper, sources, seconds=0, process_id=None):
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password:
        raise ValueError("audio bridge requires an explicit loopback API")
    events = queue.Queue(maxsize=256)
    stopped, broken = threading.Event(), threading.Event()
    process = subprocess.Popen([str(helper)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, bufsize=1)
    send_lock = threading.Lock()

    def send(command, **value):
        with send_lock:
            process.stdin.write(json.dumps({"command": command, **value}) + "\n")
            process.stdin.flush()

    def read():
        try:
            while True:
                line = process.stdout.readline(65537)
                if not line:
                    break
                if len(line) > 65536:
                    broken.set()
                    break
                value = json.loads(line)
                if not value.get("meeting_id"):
                    continue  # model/control diagnostics are not meeting audio
                AudioEvent.model_validate(value)
                if value["meeting_id"] != meeting:
                    raise ValueError("helper meeting mismatch")
                try:
                    events.put(value, timeout=.1)
                except queue.Full:
                    broken.set()
                    break
        except Exception:
            broken.set()
        finally:
            stopped.set()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    prior = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        prior[sig] = signal.signal(sig, lambda *_: stopped.set())
    count, finals, first, stopping = 0, 0, time.monotonic(), False
    headers = {"Authorization": "Bearer " + token}
    status = "stopped"
    next_health = time.monotonic()
    try:
        with requests.Session() as session:
            session.trust_env = False
            initial = session.get(endpoint + f"/live/v2/meetings/{meeting}/snapshot", headers=headers, timeout=(1, 3))
            initial.raise_for_status()
            if initial.json()["meeting"]["state"] != "active":
                raise ValueError("meeting must be active")
            command = {"meeting_id": meeting, "sources": sources, "locale": "zh-CN"}
            if process_id:
                command["process_id"] = process_id
            send("start", **command)
            stop_deadline = None
            while True:
                if not stopping and (stopped.is_set() or broken.is_set() or (seconds and time.monotonic() - first >= seconds)):
                    send("stop")
                    stopping, stop_deadline = True, time.monotonic() + 6
                if stopping and (process.poll() is not None or time.monotonic() >= stop_deadline):
                    break
                if not stopping and time.monotonic() >= next_health:
                    health = session.get(endpoint + f"/live/v2/meetings/{meeting}/snapshot", headers=headers, timeout=(1, 2))
                    health.raise_for_status()
                    if health.json()["meeting"]["state"] != "active":
                        stopped.set()
                    next_health = time.monotonic() + 1
                try:
                    value = events.get(timeout=.1)
                except queue.Empty:
                    if stopped.is_set() and process.poll() is not None:
                        break
                    continue
                # One stable event may be retried after an ambiguous HTTP ACK.
                for attempt in range(3):
                    try:
                        response = session.post(endpoint + f"/internal/live/v2/meetings/{meeting}/audio-events", json=value, headers=headers, timeout=(1, 2))
                        response.raise_for_status()
                        break
                    except requests.RequestException:
                        if attempt == 2:
                            raise
                        time.sleep(.1)
                count += 1
                finals += value["type"] == "transcript.final"
                if value["type"] in {"audio.gap", "audio.error"}:
                    broken.set()
                    status = "audio_failure"
                if stopping and value["type"] == "audio.stopped":
                    break
        if broken.is_set():
            status = "gap_or_audio_failure"
    except Exception:
        status = "bridge_failed_capture_stopped"
    finally:
        try:
            send("quit")
            process.wait(timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=3)
        reader.join(timeout=1)
        process.stdin.close()
        process.stdout.close()
        for sig, handler in prior.items():
            signal.signal(sig, handler)
    return {"status": status, "forwarded_events": count, "finals": finals,
            "helper_exit": process.returncode, "elapsed_seconds": round(time.monotonic() - first, 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--meeting", required=True)
    parser.add_argument("--helper", type=Path, default=Path(__file__).resolve().parents[1] / "native/build/ZSJHAudioHelper.app/Contents/MacOS/AudioHelper")
    parser.add_argument("--source", choices=["system", "microphone"], default="system")
    parser.add_argument("--seconds", type=int, default=0)
    parser.add_argument("--process-id", type=int)
    args = parser.parse_args()
    value = json.loads(args.credentials.read_text())
    token = next(m["token"] for m in value["members"] if m["role"] == "agent")
    result = run(args.endpoint, token, args.meeting, args.helper, [args.source], args.seconds, args.process_id)
    print(json.dumps(result))
    if result["status"] != "stopped":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
