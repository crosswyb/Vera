"""Validate a frozen synthetic semantic set; live evaluation requires an opt-in.

By default this only validates fixture shape and reports its hash. It never
changes the persisted request ceiling, retries a failed paid request, or reads
real meeting text. Use the existing workbench state to share its usage budget.
"""

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live.model_budget import ModelBudget
from live.workbench_runtime import TextModel


def work_for(case):
    old = {"id": "old-context", "revision": 1, "event_seq": 1, "text": case.get("previous_text", "我们讨论项目方案。")}
    new = {"id": "new-utterance", "revision": 1, "event_seq": 2, "text": case["text"]}
    qs = []
    if case.get("existing_title"):
        qs = [{"id": "existing-question", "title": case["existing_title"], "version": 1,
               "state": case.get("existing_state", "queued"), "generation": 1}]
    return {"meeting": {"coordinator_cursor": 1}, "cursor": 2, "segments": [old, new], "questions": qs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "tests/fixtures/workbench-semantic-acceptance.jsonl")
    parser.add_argument("--allow-synthetic-network", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Explicit number of live cases; zero validates fixtures only")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.live")
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".zsjh/workbench-pilot")
    parser.add_argument("--output", type=Path, default=ROOT / ".zsjh/workbench-semantic-evaluation.json")
    args = parser.parse_args()
    raw = args.input.read_bytes()
    cases = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    if len({c["id"] for c in cases}) != len(cases):
        raise SystemExit("duplicate case IDs")
    for case in cases:
        if case["category"] not in {"explicit", "nonrequest", "correction", "cancellation"} or not case["text"].strip():
            raise SystemExit("invalid synthetic case")
        if case["expected_kind"] not in {"create_question", "revise_question", "cancel_candidate", "no_action"}:
            raise SystemExit("invalid expected action")
    report = {"dataset": str(args.input), "sha256": hashlib.sha256(raw).hexdigest(),
              "cases": len(cases), "categories": dict(Counter(c["category"] for c in cases)),
              "fixture_status": "passed", "semantic_status": "not_run", "results": [],
              "scoring_scope": "action kind, object/version, new evidence reference and supplied title substrings; human semantic review is still required"}
    if args.limit:
        if not args.allow_synthetic_network or not 1 <= args.limit <= len(cases):
            raise SystemExit("live cases require --allow-synthetic-network and a valid --limit")
        load_dotenv(args.env_file, override=False)
        if os.getenv("ZSJH_LIVE_MODEL_ENDPOINT", "").rstrip("/") != "https://api.deepseek.com":
            raise SystemExit("this probe is scoped to the selected DeepSeek endpoint")
        model = TextModel(ModelBudget(args.state_dir / "model-usage.sqlite3"))
        for case in cases[:args.limit]:
            try:
                decisions = model.decisions(work_for(case))
                actions = [a for a in decisions.actions if a.kind != "no_action"]
                if case["expected_kind"] == "no_action":
                    correct = not actions
                else:
                    expected = case["expected_kind"]
                    matching = [a for a in actions if a.kind == expected]
                    correct = len(actions) == len(matching) == 1
                    if correct:
                        a = matching[0]
                        correct = all(word in a.title for word in case.get("title_terms", []))
                        correct &= any(r.segment_id == "new-utterance" and r.revision == 1 for r in a.source_refs)
                        if expected in {"revise_question", "cancel_candidate"}:
                            correct &= a.question_id == "existing-question" and a.expected_version == 1
                report["results"].append({"case_id": case["id"], "status": "passed" if correct else "failed", "actions": decisions.model_dump()["actions"]})
            except Exception as exc:
                report["results"].append({"case_id": case["id"], "status": "blocked", "error_type": type(exc).__name__})
                break  # No automatic paid replay or budget reset.
        done = report["results"]
        statuses = {r["status"] for r in done}
        report["semantic_status"] = ("failed" if "failed" in statuses else "blocked" if "blocked" in statuses
                                     else "requires_human_review" if done else "not_run")
        report["automatic_status"] = ("failed" if "failed" in statuses else "blocked" if "blocked" in statuses
                                      else "passed" if len(done) == len(cases) else "partial")
        report["complete_dataset"] = len(done) == len(cases)
        report["executed"] = len(done)
        report["passed_cases"] = sum(r["status"] == "passed" for r in done)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
