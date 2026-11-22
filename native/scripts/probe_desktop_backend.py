#!/usr/bin/env python3
"""Verify the frozen local backend without Python on PATH or external requests."""

import argparse
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import tempfile
import time
import urllib.request


def probe(backend):
    result = {}
    environment = {k: v for k, v in os.environ.items() if k in {"HOME", "USER", "TMPDIR", "LANG", "LC_ALL"}}
    environment["PATH"] = "/usr/bin:/bin"
    with tempfile.TemporaryDirectory(prefix="zsjh-frozen-probe-") as directory:
        root = Path(directory)
        args = ["--state-dir", str(root / "state"), "--env-file", str(root / "absent.env"),
                "--output-root", str(root / "meetings")]
        for command in ("init", "doctor"):
            completed = subprocess.run([str(backend), command, *args], env=environment,
                                       capture_output=True, text=True, timeout=30)
            if completed.returncode:
                raise RuntimeError(command + " failed; private child output intentionally omitted")
            result[command + "_exit"] = completed.returncode
            if command == "doctor":
                doctor = json.loads(completed.stdout)
                assert doctor["tls_context"] == "ready"
                result["tls_context"] = doctor["tls_context"]
                caps = doctor["capabilities"]
                assert not caps["model_enabled"] and not caps["search_enabled"] and not caps["audio_enabled"]
        result["credential_mode"] = oct(stat.S_IMODE((root / "state/credentials.json").stat().st_mode))
        assert result["credential_mode"] == "0o600"
        process = subprocess.Popen([str(backend), "serve", *args, "--port", "0", "--desktop", "--audio"],
                                   env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            if not select.select([process.stdout], [], [], 30)[0]:
                raise TimeoutError("private backend handshake timeout")
            handshake = json.loads(process.stdout.readline())
            assert handshake["type"] == "ready"
            result["private_handshake"] = "received; tokens not logged"
            base = "http://127.0.0.1:" + str(handshake["port"])
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def request(path, body=None, authenticated=True, method=None):
                headers = {"Authorization": "Bearer " + handshake["owner_token"]} if authenticated else {}
                payload = None
                if body is not None:
                    payload = json.dumps(body).encode()
                    headers["Content-Type"] = "application/json"
                with opener.open(urllib.request.Request(base + path, data=payload, headers=headers, method=method), timeout=4) as response:
                    return response.status, response.read()

            for path, kind in [("/workbench", "html"), ("/static/workbench.css", "css"), ("/static/workbench-layout.css", "layout_css"), ("/static/workbench.js", "js")]:
                status, data = request(path, authenticated=False)
                assert status == 200 and len(data) > 500
                result[kind] = {"status": status, "bytes": len(data)}
            status, data = request("/live/v2/home")
            caps = json.loads(data)["capabilities"]
            assert not caps["model_enabled"] and not caps["search_enabled"] and caps["audio_enabled"]
            result["authenticated_home"] = status
            result["model_enabled"] = caps["model_enabled"]
            result["search_enabled"] = caps["search_enabled"]
            result["audio_api_enabled_without_capture"] = caps["audio_enabled"]
            status, data = request("/live/v2/settings")
            settings = json.loads(data)
            assert status == 200 and settings["editable"] and "api_key" not in settings["model"]
            result["settings_get"] = status
            speech = settings["speech"]
            assert speech["live_provider"] == speech["file_provider"] == "apple_local"
            assert set(speech["providers"]["deepseek"]["destinations"]) == {"file"}
            assert all("api_key" not in provider for provider in speech["providers"].values())
            assert all(receipt["status"] == "not_tested" for kinds in speech["verification"].values()
                       for receipt in kinds.values())
            assert handshake.get("speech_token") and handshake["speech_token"] != handshake["owner_token"]
            status, data = request("/static/workbench-speech.js", authenticated=False)
            assert status == 200 and b"createSpeechSettings" in data
            result["speech_settings"] = "Apple defaults, cloud services awaiting tests, bundled speech UI, private native token"
            status, data = request("/live/v2/feishu/settings")
            sync = json.loads(data)
            assert status == 200 and not sync["settings"]["enabled"] and sync["jobs"] == []
            status, data = request("/static/workbench-feishu.js", authenticated=False)
            assert status == 200 and b'createFeishuSettings' in data
            result["feishu"] = "new workspace starts disabled, settings API and bundled frontend available"
            draft = {"revision": settings["revision"],
                     "model": {k: v for k, v in settings["model"].items() if k != "api_key_configured"},
                     "search": {k: v for k, v in settings["search"].items() if k not in {"api_key_configured", "effective_key_configured"}}}
            draft["model"].update(provider="openai_compatible", name="frozen-provider-fixture", base_url="https://model.fixture.invalid/v1",
                                  enabled=False, api_key="frozen-fixture-only")
            draft["search"].update(provider="tavily", base_url="https://api.tavily.com", enabled=False,
                                   use_model_key=False, api_key="search-fixture-only")
            status, data = request("/live/v2/settings", draft, method="PUT")
            assert status == 200 and b"fixture-only" not in data
            result["settings_save"] = status
            assert stat.S_IMODE((root / "state/providers.json").stat().st_mode) == 0o600
            reopened = subprocess.run([str(backend), "doctor", *args], env=environment, capture_output=True, text=True, timeout=30)
            assert reopened.returncode == 0
            saved_caps = json.loads(reopened.stdout)["capabilities"]
            assert saved_caps["model_name"] == "frozen-provider-fixture" and saved_caps["search_provider"] == "tavily"
            assert not saved_caps["model_enabled"] and not saved_caps["search_enabled"]
            result["settings_restart_persistence"] = True
            saved = json.loads(data)
            compatible = next(p for p in saved["model_profiles"] if p["name"] == "frozen-provider-fixture")
            assert compatible["api_key_configured"] and "api_key" not in compatible
            draft["revision"] = saved["revision"]
            draft["model"].update(provider="deepseek", name="deepseek-v4-pro", base_url="https://api.deepseek.com", api_key=None)
            _, raw = request("/live/v2/settings", draft, method="PUT")
            draft["revision"] = json.loads(raw)["revision"]
            draft["model"] = {k: v for k, v in compatible.items() if k != "api_key_configured"}
            _, raw = request("/live/v2/settings", draft, method="PUT")
            assert json.loads(raw)["model"]["api_key_configured"] and b"fixture-only" not in raw
            result["saved_model_round_trip"] = True
            status, data = request("/live/v2/storage")
            storage = json.loads(data)
            assert status == 200 and storage["output_root"] == str((root / "meetings").resolve())
            first_root, second_root = root / "chosen meetings", root / "next meetings"
            status, data = request("/live/v2/storage", {"output_root": str(first_root)}, method="PUT")
            assert status == 200 and json.loads(data)["output_root"] == str(first_root.resolve())
            status, data = request("/live/v2/meetings", {"title": "Frozen local smoke", "data_scope": "synthetic"})
            meeting = json.loads(data)
            assert meeting["output_root"] == str(first_root.resolve())
            status, _ = request("/live/v2/meetings/" + meeting["id"] + "/demo", {})
            result["offline_demo"] = status
            deadline = time.monotonic() + 8
            answer = None
            while time.monotonic() < deadline:
                status, data = request("/live/v2/meetings/" + meeting["id"] + "/snapshot")
                questions = json.loads(data)["questions"]
                answer = questions[0].get("result") if questions else None
                if answer:
                    break
                time.sleep(0.1)
            assert answer, "offline reference answer did not finish"
            result["created_markdown_files"] = len(list(first_root.rglob("*.md")))
            assert result["created_markdown_files"] > 0
            assert answer["brief"]["short_answer"] and answer["brief"]["caveat"]
            assert 1 <= len(answer["brief"]["key_points"]) <= 3
            document = next(path.read_text() for path in first_root.rglob("*.md")
                            if "## 结论摘要" in path.read_text())
            assert document.index("## 结论摘要") < document.index("## 完整分析") < document.index("## 关键依据")
            assert answer["brief"]["caveat"] in document
            result["structured_brief_and_markdown"] = "short answer, full analysis, caveat, original evidence"
            status, data = request("/live/v2/storage", {"output_root": str(second_root)}, method="PUT")
            assert status == 200 and json.loads(data)["output_root"] == str(second_root.resolve())
            status, _ = request("/live/v2/meetings/" + meeting["id"] + "/controls", {"action": "end"})
            assert status == 200
            result["base_files"] = {}
            for kind in ("transcript", "summary"):
                status, data = request("/live/v2/meetings/" + meeting["id"] + "/files/" + kind)
                saved = json.loads(data)
                assert status == 200 and Path(saved["path"]).is_file() and saved["content"].strip()
                assert Path(saved["path"]).is_relative_to(first_root.resolve())
                result["base_files"][kind] = saved["filename"]
            assert result["base_files"]["transcript"] == "02-全量转录.md"
            # This offline probe has no model. Do not label raw excerpts as an
            # AI summary or claim there is an audio recording without capture.
            assert result["base_files"]["summary"] == "03-会议纪要-本机摘录.md"
            meeting_url = "/live/v2/meetings/" + meeting["id"]
            status, data = request(meeting_url, {"title": "Renamed frozen smoke"}, method="PATCH")
            renamed = json.loads(data)
            assert status == 200 and renamed["title"] == "Renamed frozen smoke"
            assert renamed["directory"] == meeting["directory"]
            status, data = request(meeting_url + "/deletion")
            impact = json.loads(data)
            assert status == 200 and impact["file_count"] > 0 and impact["question_count"] > 0
            directory = first_root / meeting["directory"]
            assert directory.is_dir(), "deletion preview must not remove files"
            status, data = request("/live/v2/meetings", {"title": "Preserved frozen smoke", "data_scope": "synthetic"})
            survivor = json.loads(data)
            assert survivor["output_root"] == str(second_root.resolve())
            status, data = request(meeting_url, {"confirmation": meeting["id"]}, method="DELETE")
            assert status == 200 and json.loads(data)["deleted"] and not directory.exists()
            _, data = request("/live/v2/home")
            home = json.loads(data)
            assert {m["id"] for m in home["meetings"]} == {survivor["id"]}
            assert not home["materials"] and (second_root / survivor["directory"]).is_dir()
            result["meeting_management"] = "rename, read-only deletion preview, owned files and records removed, other meeting preserved"
            result["storage_switch_isolation"] = "old meeting saves/reads/deletes in its bound root; new meeting uses selected root"
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        result["serve_exit"] = process.returncode
        assert process.returncode == 0
        reopened = subprocess.run([str(backend), "doctor", *args], env=environment, capture_output=True, text=True, timeout=30)
        assert reopened.returncode == 0
        restarted_storage = json.loads(reopened.stdout)
        assert restarted_storage["output_root"] == str(second_root.resolve())
        assert restarted_storage["default_output_root"] == str((root / "meetings").resolve())
        assert (second_root / survivor["directory"]).is_dir()
        result["storage_restart_persistence"] = True
        result["minimal_path"] = environment["PATH"]
        result["passed"] = True
    return result


def main():
    native = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, default=native / "build/ZSJHWorkbench.app/Contents/Resources/backend/zsjh-workbench-backend")
    parser.add_argument("--output", type=Path, default=native / "build/desktop-frozen-check.json")
    args = parser.parse_args()
    result = probe(args.backend.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
