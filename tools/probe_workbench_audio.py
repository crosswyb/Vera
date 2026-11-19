"""Explicit synthetic playback -> own audio -> approved workbench network probe.

Uses the running workbench's persisted request ceilings. Never loads real audio.
Run only against the selected loopback server with --allow-synthetic-network.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live.audio_bridge import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-synthetic-network", action="store_true", required=True)
    parser.add_argument("--port", type=int, default=56633)
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".zsjh/workbench-pilot")
    args = parser.parse_args()
    state = args.state_dir.resolve()
    evidence = state / ("audio-network-probe-" + time.strftime("%Y%m%d-%H%M%S"))
    evidence.mkdir(mode=0o700)
    credentials = json.loads((state / "credentials.json").read_text())
    owner = next(m["token"] for m in credentials["members"] if m["role"] == "owner")
    agent = next(m["token"] for m in credentials["members"] if m["role"] == "agent")
    endpoint = f"http://127.0.0.1:{args.port}"
    session = requests.Session()
    session.trust_env = False
    session.headers["Authorization"] = "Bearer " + owner
    def api(path, value=None):
        response = session.get(endpoint + path, timeout=4) if value is None else session.post(endpoint + path, json=value, timeout=4)
        response.raise_for_status()
        return response.json()
    meeting = api("/live/v2/meetings", {"title": "合成语音至查证文档实测", "data_scope": "synthetic"})
    mid = meeting["id"]
    api(f"/live/v2/meetings/{mid}/controls", {"action": "pause_research"})
    phrase = "这是一段合成的会议测试。请查一下智能体和工作流有什么区别，给出公开文档的参考证据，说明哪些问题还不能确定。"
    audio = evidence / "synthetic.aiff"
    subprocess.run(["/usr/bin/say", "-v", "Tingting", "-r", "165", "-o", str(audio), phrase * 2], check=True)
    playback = subprocess.Popen(["/usr/bin/afplay", "-v", "0.2", str(audio)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = time.monotonic()
    try:
        bridge = run(endpoint, agent, mid, ROOT / "native/build/ZSJHAudioHelper.app/Contents/MacOS/AudioHelper", ["system"], seconds=18, process_id=playback.pid)
    finally:
        if playback.poll() is None:
            playback.terminate()
        playback.wait(timeout=3)
    snapshot = api(f"/live/v2/meetings/{mid}/snapshot")
    finals = [s for s in snapshot["segments"] if s["type"] == "transcript.final"]
    summary = {"meeting_id": mid, "synthetic_only": True, "capture": bridge,
               "recognized_final_text": [s["text"] for s in finals], "transcription_seconds": round(time.monotonic() - started, 3)}
    if bridge["status"] != "stopped" or not finals:
        summary["passed"] = False
    else:
        api(f"/live/v2/meetings/{mid}/controls", {"action": "resume_research"})
        deadline = time.monotonic() + 110
        while time.monotonic() < deadline:
            snapshot = api(f"/live/v2/meetings/{mid}/snapshot")
            if snapshot["documents"] or any(q["state"] in {"failed", "outcome_unknown", "needs_review"} for q in snapshot["questions"]):
                break
            time.sleep(.5)
        summary.update(elapsed_seconds=round(time.monotonic() - started, 3),
                       counts=snapshot["counts"], coordinator=snapshot["meeting"]["coordinator_state"],
                       questions=[{k: q[k] for k in ("id", "title", "state", "stage", "source_refs")} for q in snapshot["questions"]],
                       documents=[{k: d[k] for k in ("id", "path", "state")} for d in snapshot["documents"]],
                       output_directory=snapshot["output_directory"])
        summary["passed"] = bool(snapshot["documents"] and all(d["state"] == "saved" for d in snapshot["documents"]))
    (evidence / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    (evidence / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({**summary, "evidence": str(evidence)}, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
