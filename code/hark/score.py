"""Scorer: compares predictions to sample_output.csv.
Usage: python3 code/hark/score.py [pred.csv] [gold.csv]
Metrics: case status accuracy, exact steps match, step-level P/R/F1 on (phase,system,resource,op,state),
evidence accuracy on matched steps, sequence (exact order) accuracy."""
import csv, json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
from ingest import DATASET as DS


def load(p):
    return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(open(p))}


def key(s):
    return (s["phase"], s["system_id"], s["resource_id"], s["operation"], s["state"])


def score(pred, gold, verbose=False):
    n = st_ok = exact = seq = 0
    tp = fp = fn = 0
    ev_ok = ev_n = 0
    rows = []
    for cid, (gs, gsteps) in sorted(gold.items(), key=lambda x: int(x[0].split("_")[1])):
        n += 1
        ps, psteps = pred.get(cid, ("MISSING", []))
        st_ok += ps == gs
        gk = [key(s) for s in gsteps]
        pk = [key(s) for s in psteps]
        gset, pset = set(gk), set(pk)
        tp += len(gset & pset)
        fp += len(pset - gset)
        fn += len(gset - pset)
        gev = {key(s): s["evidence_id"] for s in gsteps}
        for s in psteps:
            if key(s) in gev:
                ev_n += 1
                ev_ok += s["evidence_id"] == gev[key(s)]
        is_seq = gk == pk
        seq += is_seq
        ex = is_seq and all(a["evidence_id"] == b["evidence_id"] for a, b in zip(gsteps, psteps)) and ps == gs
        exact += ex
        rows.append((cid, gs, ps, ex, sorted(pset - gset), sorted(gset - pset)))
    P = tp / (tp + fp) if tp + fp else 0
    R = tp / (tp + fn) if tp + fn else 0
    F = 2 * P * R / (P + R) if P + R else 0
    summary = dict(cases=n, status_acc=st_ok / n, exact_case=exact / n, sequence_acc=seq / n, step_P=P, step_R=R,
                   step_F1=F, evidence_acc=(ev_ok / ev_n if ev_n else 0))
    if verbose:
        for cid, gs, ps, ex, extra, missing in rows:
            if not ex:
                print(f"{cid}: gold={gs} pred={ps}")
                for e in extra:
                    print("   + extra  ", e)
                for m in missing:
                    print("   - missing", m)
    return summary


if __name__ == "__main__":
    pred = load(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "sample_predictions.csv"))
    gold = load(sys.argv[2] if len(sys.argv) > 2 else os.path.join(DS, "sample_output.csv"))
    s = score(pred, gold, verbose=True)
    print(json.dumps(s, indent=2))
