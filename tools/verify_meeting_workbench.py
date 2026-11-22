"""Reproducible offline engineering checks, without capture or paid services.

This does not label the 54-case product acceptance matrix as complete.
Optional --native runs Swift smoke checks without a physical device or a cloud account.
"""

import argparse
import io
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / ".zsjh/workbench-verification")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    stream = io.StringIO()
    # The repository now contains only workbench tests. Discovery includes new
    # regressions automatically, rather than silently omitting new test files.
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    started = time.monotonic()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    (output / "workbench-unittest.log").write_text(stream.getvalue(), encoding="utf-8")
    checks.append({"name": "workbench_contracts_and_lifecycle", "status": "passed" if result.wasSuccessful() else "failed",
                   "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                   "skipped": len(result.skipped),
                   "seconds": round(time.monotonic() - started, 3), "evidence": "workbench-unittest.log"})
    commands = [("javascript_syntax", ["node", "--check", "static/workbench.js"]),
                ("speech_javascript_syntax", ["node", "--check", "static/workbench-speech.js"]),
                ("tracked_diff_whitespace", ["git", "diff", "--check"])]
    if args.native:
        commands.extend([("native_framework_free_smoke", ["/bin/zsh", "native/scripts/smoke.sh"]),
                         ("native_cloud_asr_stub", ["/bin/zsh", "native/scripts/test-cloud-asr.sh"])])
    for name, command in commands:
        try:
            value = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
            (output / (name + ".log")).write_text(value.stdout + value.stderr, encoding="utf-8")
            checks.append({"name": name, "status": "passed" if value.returncode == 0 else "failed", "evidence": name + ".log"})
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks.append({"name": name, "status": "blocked", "reason": type(exc).__name__})
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "python": sys.version.split()[0],
              "engineering_status": "passed" if all(c["status"] == "passed" for c in checks) else "failed",
              "product_acceptance_status": "not_run", "checks": checks,
              "not_covered": ["100-case frozen semantic evaluation", "20-question / 40-claim evidence evaluation",
                              "60-minute audio and sleep/device recovery", "human meeting value acceptance",
                              "clean second-machine installation and distribution", "Tencent bidirectional voice"]}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["engineering_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
