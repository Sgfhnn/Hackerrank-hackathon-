"""Hark evidence engine - deterministic deletion audit (no LLM calls, stdlib + sqlite3 only).

Usage (from repo root):
  python3 code/hark/run.py                 # writes code/hark/sample_predictions.csv and code/hark/output_hark.csv
  python3 code/hark/run.py --out output.csv  # also write the submission file at that path
  python3 code/hark/score.py               # score sample predictions vs dataset/sample_output.csv
"""
import csv, json, os, sys, collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from engine import Engine  # noqa: E402
from ingest import DATASET  # noqa: E402


def write(reqs, path, eng):
    rows = []
    for r in reqs:
        status, steps, _ = eng.plan(r)
        rows.append((r["case_id"], status, json.dumps(steps, ensure_ascii=False, separators=(",", ":"))))
    rows.sort(key=lambda x: int(x[0].split("_")[1]))
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        w.writerows(rows)
    return rows


def main(argv):
    eng = Engine()
    samples = list(csv.DictReader(open(os.path.join(DATASET, "sample_deletion_requests.csv"))))
    tests = list(csv.DictReader(open(os.path.join(DATASET, "deletion_requests.csv"))))
    eng.prepare(samples + tests)  # global activity->case assignment needs every request
    write(samples, os.path.join(HERE, "sample_predictions.csv"), eng)
    rows = write(tests, os.path.join(HERE, "output_hark.csv"), eng)
    if "--out" in argv:
        out = argv[argv.index("--out") + 1]
        write(tests, out, eng)
    print("rows", len(rows), dict(collections.Counter(r[1] for r in rows)))
    print("activity assignment:", dict(eng.ambiguous))


if __name__ == "__main__":
    main(sys.argv[1:])
