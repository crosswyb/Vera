#!/usr/bin/env python3
"""Explicit local synthetic-only system capture probe; no cloud or real recording.

Build the app first. --install-model explicitly allows Apple model installation.
The tap selects only this script's afplay process. Logs contain synthetic text.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import queue
import subprocess
import threading
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-model", action="store_true")
    parser.add_argument("--capture-only", action="store_true")
    parser.add_argument("--record", action="store_true", help="Save the synthetic process tap as local CAF and inspect playback metadata.")
    parser.add_argument("--seconds", type=int, default=20)
    args = parser.parse_args()
    if not 5 <= args.seconds <= 30:
        parser.error("--seconds must be between 5 and 30")
    native = Path(__file__).resolve().parents[1]
    helper = native / "build/ZSJHAudioHelper.app/Contents/MacOS/AudioHelper"
    if not helper.is_file():
        parser.error("run native/scripts/build-app.sh first")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = native / "build" / f"synthetic-probe-{stamp}"
    directory.mkdir(parents=True)
    events: list[dict] = []
    inbox: queue.Queue[dict] = queue.Queue()
    errors = (directory / "helper.stderr.log").open("w", encoding="utf-8")
    log = (directory / "events.jsonl").open("w", encoding="utf-8")
    process = subprocess.Popen([str(helper)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=errors, text=True, bufsize=1)
    playback: subprocess.Popen | None = None
    summary: dict = {"synthetic_only": True, "global_system_audio": False,
                     "model_download_explicitly_requested": args.install_model,
                     "recognition_enabled": not args.capture_only,
                     "events": str(directory / "events.jsonl")}

    def consume() -> None:
        assert process.stdout
        for line in process.stdout:
            log.write(line)
            log.flush()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(event)
            inbox.put(event)

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()

    def send(command: str, **kwargs) -> None:
        assert process.stdin
        process.stdin.write(json.dumps({"command": command, **kwargs}, ensure_ascii=False) + "\n")
        process.stdin.flush()

    def wait_for(predicate, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = inbox.get(timeout=min(1, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                if process.poll() is not None:
                    raise RuntimeError(f"AudioHelper exited {process.returncode}")
                continue
            if predicate(event):
                return event
        raise TimeoutError("Timed out waiting for AudioHelper event")

    try:
        if not args.capture_only:
            send("model_status", locale="zh-CN")
            status = wait_for(lambda event: event.get("type") == "audio.model_status", 15)
            summary["model_initial_state"] = status.get("state")
            print(f"Local zh-CN model: {status.get('state')}", flush=True)
            if status.get("state") != "installed":
                if not args.install_model:
                    raise RuntimeError("Local model missing. Explicit --install-model is required for Apple download.")
                print("Explicit Apple local-model installation requested.", flush=True)
                send("install_model", locale="zh-CN", allow_download=True)
                result = wait_for(lambda event: event.get("type") in {"audio.model_status", "audio.error"}, 300)
                summary["model_install_event"] = result
                if result.get("state") != "installed":
                    raise RuntimeError(f"Model installation did not finish: {result}")
                print("Local model installed.", flush=True)
        phrase = "这是一段本地合成的会议测试，不是真实会议。请查一下智能体工作流的定义，给出官方来源和未确定的问题。我们先讨论流程，再比较证据，不直接作出结论。"
        audio = directory / "synthetic.aiff"
        subprocess.run(["/usr/bin/say", "-v", "Tingting", "-r", "170", "-o", str(audio), phrase * 2], check=True)
        playback = subprocess.Popen(["/usr/bin/afplay", "-v", "0.25", str(audio)], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        meeting = f"synthetic-audiocore-{stamp}"
        summary["meeting_id"] = meeting
        summary["synthetic_playback_pid"] = playback.pid
        summary["synthetic_text"] = phrase
        recording = {"recording_directory": str(directory.resolve()), "capture_epoch": "synthetic-recording"} if args.record else {}
        if args.capture_only:
            send("capture_probe", meeting_id=meeting, process_id=playback.pid, seconds=args.seconds, **recording)
        else:
            send("start", meeting_id=meeting, process_id=playback.pid, sources=["system"], locale="zh-CN", **recording)
        event = wait_for(lambda item: item.get("type") in {"audio.ready", "audio.error"}, 30)
        if event.get("type") == "audio.error":
            raise RuntimeError(f"Capture startup failed: {event}")
        print("Synthetic process tap ready; observing samples.", flush=True)
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            time.sleep(0.25)
        send("stop")
        wait_for(lambda item: item.get("type") == "audio.status" and item.get("state") == "stopped", 15)
        levels = [item for item in events if item.get("type") == "audio.level"]
        finals = [item for item in events if item.get("type") == "transcript.final"]
        summary["max_frames_received"] = max((item.get("frames_received", 0) for item in levels), default=0)
        summary["max_non_silent_frames"] = max((item.get("non_silent_frames", 0) for item in levels), default=0)
        summary["transcript_final_count"] = len(finals)
        summary["final_text"] = "\n".join(item.get("text", "") for item in finals)
        summary["has_workflow_term"] = "工作流" in summary["final_text"]
        summary["gaps"] = [item for item in events if item.get("type") in {"audio.gap", "audio.error"}]
        summary["passed"] = bool(summary["max_non_silent_frames"] > 0 and not summary["gaps"] and
                                 (args.capture_only or summary["transcript_final_count"] > 0))
        if args.record:
            recording_file = directory / "01-原始音频-system-synthetic-recording.caf"
            summary["recording_file"] = str(recording_file)
            summary["recording_bytes"] = recording_file.stat().st_size if recording_file.exists() else 0
            summary["recording_events"] = [e for e in events if e.get("type") == "audio.recording"]
            info = subprocess.run(["/usr/bin/afinfo", str(recording_file)], capture_output=True, text=True)
            (directory / "recording-afinfo.txt").write_text(info.stdout + info.stderr, encoding="utf-8")
            summary["recording_readable"] = info.returncode == 0
            summary["passed"] = summary["passed"] and summary["recording_bytes"] > 0 and summary["recording_readable"] and bool(summary["recording_events"])
        return_code = 0 if summary["passed"] else 2
    except Exception as exc:
        summary["passed"] = False
        summary["error"] = str(exc)
        return_code = 2
    finally:
        if playback and playback.poll() is None:
            playback.terminate()
            playback.wait(timeout=5)
        if process.poll() is None:
            try:
                send("quit")
                process.wait(timeout=10)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=5)
        reader.join(timeout=2)
        summary["helper_returncode"] = process.returncode
        (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log.close()
        errors.close()
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
