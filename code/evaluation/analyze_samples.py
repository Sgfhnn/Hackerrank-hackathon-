#!/usr/bin/env python3
"""Reproducible statistics over the 32 solved samples (dataset/sample_output.csv).

Run:  python3 code/evaluation/analyze_samples.py
It only reads participant-facing files under dataset/ and prints the invariants
that are summarised in code/evaluation/sample_patterns.md.
"""
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DS = os.path.join(ROOT, "dataset")


def rows(name):
    with open(os.path.join(DS, name), newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main():
    gold = {r["case_id"]: r for r in rows("sample_output.csv")}
    req = {r["case_id"]: r for r in rows("sample_deletion_requests.csv")}
    ev = {r["file_id"]: r for r in rows("evidence_files.csv")}
    act = {r["activity_id"]: r for r in rows("system_activity.csv")}
    ident = rows("user_identities.csv")
    current = {i["email"].lower() for i in ident}

    print("## status x reported_status")
    tab = Counter((req[c]["reported_status"], g["deletion_status"]) for c, g in gold.items())
    for k in sorted(tab):
        print(f"  reported={k[0]:<18} true={k[1]:<10} n={tab[k]}")

    print("\n## email form (current vs historical)")
    print(" ", Counter(("current" if req[c]["email"].lower() in current else "historical") for c in gold))

    families = Counter()
    shape_cases = defaultdict(list)
    inv = Counter()
    gaps = []
    for cid, g in sorted(gold.items()):
        steps = json.loads(g["steps_json"])
        by_phase = defaultdict(list)
        for s in steps:
            by_phase[s["phase"]].append(s)
        findings = by_phase["FINDING"]
        rem = by_phase["REMEDIATION"]
        ver = by_phase["VERIFICATION"]
        status = g["deletion_status"]
        sig = "+".join(sorted({s["operation"].replace("REPORT_", "") for s in findings})) or "-"
        families[(status, sig)] += 1
        shape_cases[(status, sig)].append(cid)

        inv["cases"] += 1
        inv["status==INCOMPLETE iff findings"] += (status == "INCOMPLETE") == bool(findings)
        inv["COMPLETE has only VERIFICATION"] += status != "COMPLETE" or (not findings and not rem)
        # 1:1 finding -> remediation on same resource
        rem_keys = {(s["system_id"], s["resource_id"]) for s in rem}
        fin_keys = {(s["system_id"], s["resource_id"]) for s in findings}
        inv["findings map 1:1 to remediation"] += rem_keys == fin_keys and len(rem) == len(findings)
        # every remediated resource is verified
        ver_keys = {(s["system_id"], s["resource_id"]) for s in ver}
        inv["every remediated resource verified"] += rem_keys <= ver_keys
        inv["account_service ABSENT verified"] += any(
            s["system_id"] == "account_service" and s["operation"] == "VERIFY_RECORD_ABSENT" for s in ver
        )
        inv["security_service PRESENT verified"] += any(
            s["system_id"] == "security_service" and s["operation"] == "VERIFY_RECORD_PRESENT" for s in ver
        )
        extra = ver_keys - rem_keys
        kinds = Counter(k[0] for k in extra)
        inv["verify-only resources are account/security only (INCOMPLETE)"] += (
            status != "INCOMPLETE" or set(kinds) <= {"account_service", "security_service"}
        )
        # evidence
        fin_ev = {s["evidence_id"] for s in findings}
        inv["INCOMPLETE: findings share one evidence_id"] += status != "INCOMPLETE" or len(fin_ev) == 1
        inv["INCOMPLETE: remediation evidence_id empty"] += all(s["evidence_id"] == "" for s in rem)
        inv["INCOMPLETE: future verification evidence_id empty"] += status != "INCOMPLETE" or all(
            s["evidence_id"] == "" for s in ver
        )
        inv["COMPLETE: one evidence_id for all steps"] += status != "COMPLETE" or len({s["evidence_id"] for s in ver}) == 1
        # evidence timing
        for e in {s["evidence_id"] for s in steps if s["evidence_id"]}:
            if e in ev:
                gaps.append((ts(req[cid]["audit_at"]) - ts(ev[e]["captured_at"])).total_seconds() / 3600)
            elif e in act:
                gaps.append((ts(req[cid]["audit_at"]) - ts(act[e]["event_time"])).total_seconds() / 3600)
        # step numbering
        inv["step_no consecutive"] += [s["step_no"] for s in steps] == list(range(1, len(steps) + 1))

    print("\n## finding signature -> cases")
    for k, n in sorted(families.items()):
        print(f"  {k[0]:<10} {k[1]:<60} n={n}  {shape_cases[k]}")

    print("\n## invariants (count of the 32 samples where it holds)")
    for k, v in inv.items():
        print(f"  {k:<60} {v}")

    print("\n## evidence lag (audit_at - evidence capture), hours")
    gaps.sort()
    print(f"  n={len(gaps)} min={gaps[0]:.2f} median={gaps[len(gaps)//2]:.2f} max={gaps[-1]:.2f} negative={sum(g < 0 for g in gaps)}")

    print("\n## operation x system")
    ops = defaultdict(Counter)
    for g in gold.values():
        for s in json.loads(g["steps_json"]):
            ops[s["operation"]][s["system_id"]] += 1
    for op, c in ops.items():
        print(f"  {op:<28} {dict(c)}")

    print("\n## step count by status")
    sizes = defaultdict(list)
    for g in gold.values():
        sizes[g["deletion_status"]].append(len(json.loads(g["steps_json"])))
    for k, v in sizes.items():
        print(f"  {k}: min={min(v)} max={max(v)} mean={sum(v)/len(v):.1f} n={len(v)}")


if __name__ == "__main__":
    sys.exit(main())
