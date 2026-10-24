"""Generality check: the agent's answer for a request must not depend on its case_id,
its row position, or the letter case of the email.

The hidden grader uses cases we cannot see, so a prediction that changed when a case
is renamed or moved would mean a case-specific shortcut. This script rewrites
dataset/deletion_requests.csv under several perturbations, reruns the full agent
(code/main.py --requests ...) on each copy, maps rows back to the original case_id,
and requires deletion_status and steps_json to be identical to the baseline run.

    python3 code/evaluation/perturbation_check.py            # all perturbations
    python3 code/evaluation/perturbation_check.py --seed 7   # another shuffle

Exit code 0 = every perturbation reproduced the baseline exactly. Stdlib only.
"""
from __future__ import annotations

import argparse
import csv
import random
import subprocess
import sys
import tempfile
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_DIR.parent
MAIN = CODE_DIR / "main.py"


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return reader.fieldnames, list(reader)


def write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run_agent(requests_path, out_path):
    proc = subprocess.run(
        [sys.executable, str(MAIN), "--requests", str(requests_path), "--output", str(out_path)],
        cwd=str(REPO_ROOT), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("agent failed:\n" + proc.stdout + proc.stderr)
    _, rows = read_csv(out_path)
    return {r["case_id"]: (r["deletion_status"], r["steps_json"]) for r in rows}


def perturb(rows, kind, rng):
    """Return (new_rows, new_id -> original_id)."""
    rows = [dict(r) for r in rows]
    mapping = {r["case_id"]: r["case_id"] for r in rows}
    if kind in ("shuffle", "shuffle+rename"):
        rng.shuffle(rows)
    if kind in ("rename", "shuffle+rename"):
        ids = rng.sample(range(100000, 999999), len(rows))
        mapping = {}
        for r, n in zip(rows, ids):
            new_id = "req_{}".format(n)
            mapping[new_id] = r["case_id"]
            r["case_id"] = new_id
    if kind == "email_case":
        for r in rows:
            r["email"] = r["email"].upper()
    return rows, mapping


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", default=str(REPO_ROOT / "dataset"))
    ap.add_argument("--seed", type=int, default=20261025)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    fields, rows = read_csv(Path(args.dataset) / "deletion_requests.csv")

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        write_csv(tmp / "base.csv", fields, rows)
        base = run_agent(tmp / "base.csv", tmp / "base_out.csv")
        print("baseline: {} rows".format(len(base)))
        for kind in ("shuffle", "rename", "shuffle+rename", "email_case"):
            prows, mapping = perturb(rows, kind, rng)
            write_csv(tmp / (kind + ".csv"), fields, prows)
            got = run_agent(tmp / (kind + ".csv"), tmp / (kind + "_out.csv"))
            got = {mapping[cid]: v for cid, v in got.items()}
            status_diff = [c for c in base if got.get(c, (None,))[0] != base[c][0]]
            steps_diff = [c for c in base if got.get(c) != base[c]]
            ok = not steps_diff and len(got) == len(base)
            failures += not ok
            print("{:<15} {}  status changed: {}  rows changed: {}{}".format(
                kind, "OK  " if ok else "FAIL", len(status_diff), len(steps_diff),
                "  e.g. " + ", ".join(steps_diff[:5]) if steps_diff else ""))
    print("PERTURBATION PASS" if not failures else "PERTURBATION FAIL ({} perturbation(s))".format(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
