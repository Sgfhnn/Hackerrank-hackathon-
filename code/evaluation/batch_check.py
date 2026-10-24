"""Diagnostic: do the custom cases pass when the Hark engine plans them in different batches?

Why this exists: code/evidence.py always prepares the Hark engine on
sample_deletion_requests.csv + deletion_requests.csv + the requests of the current run, and the
engine assigns system activity to requests globally (by reported_at window and account
fingerprint). A custom case that is a variant of a solved sample (same reported_at, same user)
therefore competes with its own base sample for the same activity.

This script (read-only on the engine; stdlib only) plans the custom cases with
  A. samples + real 518 + custom   (exactly what `python3 code/main.py --requests custom.csv` does)
  B. samples minus the base cases of custom_04/06/07 + real 518 + custom
  C. real 518 + custom
and prints PASS/DIFF per case against custom/custom_gold.csv (identity keys: phase, system,
resource, operation, state). Run from anywhere:  python3 code/evaluation/batch_check.py
"""
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DATASET = os.path.join(ROOT, "dataset")
sys.path.insert(0, os.path.join(ROOT, "code", "hark"))
from engine import Engine  # noqa: E402

CHECKED = {"custom_04": "case_29", "custom_06": "case_18", "custom_07": "case_28"}


def rd(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def key(steps):
    return [(s["phase"], s["system_id"], s["resource_id"], s["operation"], s["state"]) for s in steps]


def run_batch(label, batch, cust, gold):
    eng = Engine()
    eng.prepare(batch)
    res = []
    for c in cust:
        if c["case_id"] not in CHECKED:
            continue
        status, steps, _ = eng.plan(c)
        ok = status == gold[c["case_id"]]["deletion_status"] and key(steps) == key(
            json.loads(gold[c["case_id"]]["steps_json"]))
        res.append("%s:%s" % (c["case_id"], "PASS" if ok else "DIFF"))
    print("%-58s %s" % (label, "  ".join(res)))


def main():
    samples = rd(os.path.join(DATASET, "sample_deletion_requests.csv"))
    real = rd(os.path.join(DATASET, "deletion_requests.csv"))
    cust = rd(os.path.join(HERE, "custom", "custom_requests.csv"))
    gold = {r["case_id"]: r for r in rd(os.path.join(HERE, "custom", "custom_gold.csv"))}
    bases = set(CHECKED.values())
    run_batch("A. samples + real 518 + custom (what main.py does)", samples + real + cust, cust, gold)
    run_batch("B. samples without the 3 base cases + real 518 + custom",
              [s for s in samples if s["case_id"] not in bases] + real + cust, cust, gold)
    run_batch("C. real 518 + custom", real + cust, cust, gold)
    shared = sorted({r["case_id"] for r in samples + real for c in cust
                     if c["case_id"] in CHECKED and r["reported_at"] == c["reported_at"]})
    print("requests sharing reported_at with those custom cases:", ", ".join(shared))


if __name__ == "__main__":
    main()
