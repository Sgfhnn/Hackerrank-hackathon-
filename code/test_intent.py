"""Validation for code/intent.py (Muse, task 4).

Expectations come from a plain reading of the participant-facing request
texts, plus the public sample answers (sample_output.csv is provided as
development data):

  * all 32 sample requests authorize deletion (their sample statuses are
    COMPLETE or INCOMPLETE, never NOT_APPLICABLE);
  * the non-deletion requests in deletion_requests.csv (correction,
    export, access, opt-out, login help, email change, invoice copy -
  identified by reading request_text) must classify NOT_DELETION;
  * every other request in deletion_requests.csv uses one of the deletion
    templates and must classify DELETION_REQUESTED.

Run: python3 code/test_intent.py   (exit code 0 = all pass)
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from intent import DELETION, NOT_DELETION, classify_intent_rules  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "dataset"


def read_csv(name: str) -> list[dict]:
    with open(DATASET / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    failures: list[str] = []

    # 1. Sample requests: all authorize deletion.
    sample_out = {r["case_id"]: r["deletion_status"] for r in read_csv("sample_output.csv")}
    for row in read_csv("sample_deletion_requests.csv"):
        assert sample_out[row["case_id"]] != "NOT_APPLICABLE"
        result = classify_intent_rules(row["request_text"])
        if result.intent != DELETION:
            failures.append(f"sample {row['case_id']}: expected DELETION, got {result}")

    # 2 + 3. Full dataset: a request is non-deletion only when its text asks
    # for one of the known non-deletion actions (identified by reading the
    # text, see module docstring). Everything else is a deletion template.
    non_deletion_markers = (
        "replace it with the new one",
        "stop the promotional emails",
        "download my account information",
        "resetting the login",
        "correct it without removing",
        "remove me from marketing lists only",
        "what personal information you hold about me",
        "copy of last year's invoices",
        "change the email address used for this account",
        "copy of the information held on my account",
    )
    counts = {DELETION: 0, NOT_DELETION: 0}
    for row in read_csv("deletion_requests.csv"):
        expected = (
            NOT_DELETION
            if any(m in row["request_text"] for m in non_deletion_markers)
            else DELETION
        )
        result = classify_intent_rules(row["request_text"])
        counts[result.intent] = counts.get(result.intent, 0) + 1
        if result.intent != expected:
            failures.append(
                f"{row['case_id']}: expected {expected}, got {result.intent} "
                f"({result.category}, matched={result.matched}) :: {row['request_text'][:90]}"
            )
        if result.needs_llm:
            failures.append(f"{row['case_id']}: unexpectedly needs LLM fallback")

    print(f"classified: {counts}")
    if failures:
        print(f"{len(failures)} FAILURES")
        for failure in failures:
            print(" -", failure)
        return 1
    print("ALL INTENT TESTS PASS (32 samples + 518 evaluation requests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
