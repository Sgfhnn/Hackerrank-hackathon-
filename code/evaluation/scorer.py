"""Scoring library for the "Ghost Data Still Exists" deletion-audit agent.

Pure standard library, deterministic, no network access, no model calls.

It compares a predicted ``output.csv`` against a gold file (normally
``dataset/sample_output.csv``) and reports four families of metrics:

* case level   - ``deletion_status`` accuracy, confusion matrix, per-class P/R/F1
* row level    - exact-row match (status + every step incl. step_no/evidence_id)
* step level   - micro/macro P/R/F1 on step identity, with and without evidence_id
* field level  - accuracy of every field of every gold step after alignment
* sequence     - exact order, LCS ratio, pairwise order concordance and
                 compliance with the ordering rule of the problem statement

It can also validate a file without any gold (``validate_submission``), which is
what the team uses for the full 518-row ``output.csv`` where no gold is public.
"""

import csv
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime

# --------------------------------------------------------------------------
# Contract constants (problem_statement.md "Allowed Operations")
# --------------------------------------------------------------------------
STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")

# (phase, operation, state) in the exact order of the problem-statement table.
OPERATION_TABLE = (
    ("FINDING", "REPORT_REMAINING_RECORD", "PRESENT"),
    ("FINDING", "REPORT_EXPOSED_PII", "PRESENT"),
    ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE"),
    ("FINDING", "REPORT_RESTORE_RISK", "ACTIVE"),
    ("REMEDIATION", "DISABLE_DATA_FLOW", "DISABLED"),
    ("REMEDIATION", "BLOCK_RESTORE", "BLOCKED"),
    ("REMEDIATION", "DELETE_RECORD", "ABSENT"),
    ("REMEDIATION", "REDACT_RECORD", "REDACTED"),
    ("REMEDIATION", "ANONYMIZE_RECORD", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT"),
    ("VERIFICATION", "VERIFY_RECORD_PRESENT", "PRESENT"),
    ("VERIFICATION", "VERIFY_RECORD_REDACTED", "REDACTED"),
    ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
    ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "BLOCKED"),
)
OP_RANK = {op: i for i, (_, op, _) in enumerate(OPERATION_TABLE)}
OP_PHASE = {op: ph for ph, op, _ in OPERATION_TABLE}
OP_STATE = {op: st for _, op, st in OPERATION_TABLE}
PHASE_RANK = {p: i for i, p in enumerate(PHASES)}

STEP_KEYS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")
IDENTITY_FIELDS = ("phase", "system_id", "resource_id", "operation", "state")
FIELD_ORDER = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")
HEADER = ["case_id", "deletion_status", "steps_json"]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def natural_key(text):
    """'case_100' -> ('case_', 100): natural sort for ids such as case_33 < case_100."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(text))]


def parse_ts(value):
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))


def step_identity(step):
    return tuple(step.get(f) for f in IDENTITY_FIELDS)


def step_strict(step):
    return step_identity(step) + (step.get("evidence_id"),)


def f1(p, r):
    return 2 * p * r / (p + r) if (p + r) else 0.0


def safe_div(a, b, empty=0.0):
    return a / b if b else empty


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
class Prediction:
    """One parsed row. ``steps`` is None when the row could not be parsed."""

    def __init__(self, case_id, status, steps, error=None, raw_steps=None):
        self.case_id = case_id
        self.status = status
        self.steps = steps
        self.error = error
        self.raw_steps = raw_steps


def load_output(path):
    """Load a (pred or gold) CSV. Returns (dict case_id -> Prediction, info dict)."""
    info = {"header": None, "order": [], "duplicates": [], "path": path}
    rows = {}
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        try:
            info["header"] = next(reader)
        except StopIteration:
            return rows, info
        for line in reader:
            if not line:
                continue
            case_id = line[0].strip() if line else ""
            status = line[1].strip() if len(line) > 1 else ""
            raw = line[2] if len(line) > 2 else ""
            info["order"].append(case_id)
            if len(line) != 3:
                pred = Prediction(case_id, status, None, "row has %d columns, expected 3" % len(line), raw)
            else:
                try:
                    steps = json.loads(raw)
                    if not isinstance(steps, list):
                        raise ValueError("steps_json is not a JSON array")
                    if not all(isinstance(s, dict) for s in steps):
                        raise ValueError("steps_json contains non-object items")
                    pred = Prediction(case_id, status, steps, None, raw)
                except ValueError as exc:  # json.JSONDecodeError is a ValueError
                    pred = Prediction(case_id, status, None, "invalid steps_json: %s" % exc, raw)
            if case_id in rows:
                info["duplicates"].append(case_id)
            rows[case_id] = pred
    return rows, info


# --------------------------------------------------------------------------
# Row validation (no gold needed)
# --------------------------------------------------------------------------
def order_issues(steps):
    """Check the ordering rule: phase, operation-table order, system_id, resource_id."""
    issues = []
    for a, b in zip(steps, steps[1:]):
        try:
            ka = (PHASE_RANK[a["phase"]], OP_RANK[a["operation"]], a["system_id"])
            kb = (PHASE_RANK[b["phase"]], OP_RANK[b["operation"]], b["system_id"])
        except KeyError:
            return ["ordering cannot be checked (unknown phase/operation)"]
        if ka > kb:
            issues.append("step %s -> %s breaks phase/operation/system order" % (a.get("step_no"), b.get("step_no")))
        elif ka == kb:
            ra, rb = str(a["resource_id"]), str(b["resource_id"])
            # The statement says "ascending resource_id" without saying lexical vs numeric;
            # accept either so we do not raise false alarms (see sample_patterns.md).
            if not (ra <= rb or natural_key(ra) <= natural_key(rb)):
                issues.append("steps %s,%s: resource_id not ascending" % (a.get("step_no"), b.get("step_no")))
    return issues


def validate_row(pred, evidence_times=None, audit_at=None):
    """Return a list of human-readable contract violations for one predicted row."""
    issues = []
    if pred.status not in STATUSES:
        issues.append("deletion_status %r not in %s" % (pred.status, "/".join(STATUSES)))
    if pred.steps is None:
        issues.append(pred.error or "unparseable steps_json")
        return issues
    steps = pred.steps
    if pred.status == "NOT_APPLICABLE" and steps:
        issues.append("NOT_APPLICABLE must have steps_json == []")
    if pred.status in ("COMPLETE", "INCOMPLETE") and not steps:
        issues.append("%s row has no steps" % pred.status)

    seen = set()
    for i, s in enumerate(steps, start=1):
        keys = set(s)
        if keys != set(STEP_KEYS):
            issues.append("step %d keys %s != %s" % (i, sorted(keys), sorted(STEP_KEYS)))
            continue
        if s["step_no"] != i or isinstance(s["step_no"], bool):
            issues.append("step %d has step_no=%r (must be consecutive from 1)" % (i, s["step_no"]))
        op = s["operation"]
        if op not in OP_RANK:
            issues.append("step %d unknown operation %r" % (i, op))
            continue
        if s["phase"] not in PHASES:
            issues.append("step %d unknown phase %r" % (i, s["phase"]))
        elif OP_PHASE[op] != s["phase"]:
            issues.append("step %d operation %s belongs to phase %s, not %s" % (i, op, OP_PHASE[op], s["phase"]))
        if OP_STATE[op] != s["state"]:
            issues.append("step %d operation %s requires state %s, got %r" % (i, op, OP_STATE[op], s["state"]))
        for f in ("system_id", "resource_id", "evidence_id"):
            if not isinstance(s[f], str):
                issues.append("step %d %s must be a string" % (i, f))
        if not s["system_id"] or not s["resource_id"]:
            issues.append("step %d has empty system_id/resource_id" % i)
        ident = step_identity(s)
        if ident in seen:
            issues.append("step %d duplicates an earlier step %s" % (i, ident[1:4]))
        seen.add(ident)

        # evidence rules (problem statement "Rules" + AGENTS.md 6.6)
        ev = s["evidence_id"] if isinstance(s["evidence_id"], str) else ""
        if pred.status == "COMPLETE":
            if s["phase"] != "VERIFICATION":
                issues.append("step %d: COMPLETE rows may only contain VERIFICATION steps" % i)
            if not ev:
                issues.append("step %d: COMPLETE verification must cite evidence_id" % i)
        elif pred.status == "INCOMPLETE":
            if s["phase"] == "FINDING" and not ev:
                issues.append("step %d: finding must cite evidence_id" % i)
            if s["phase"] in ("REMEDIATION", "VERIFICATION") and ev:
                issues.append("step %d: INCOMPLETE %s must use empty evidence_id" % (i, s["phase"].lower()))
        if ev and evidence_times is not None:
            when = evidence_times.get(ev)
            if when is None:
                issues.append("step %d cites unknown evidence_id %r" % (i, ev))
            elif audit_at is not None and when > audit_at:
                issues.append("step %d cites %s captured after audit_at (%s > %s)" % (i, ev, when.isoformat(), audit_at.isoformat()))

    if pred.status == "INCOMPLETE" and steps:
        phases = {s.get("phase") for s in steps}
        for needed in PHASES:
            if needed not in phases:
                issues.append("INCOMPLETE row lacks %s steps" % needed)
    issues.extend(order_issues([s for s in steps if set(s) == set(STEP_KEYS) and s["operation"] in OP_RANK and s["phase"] in PHASE_RANK]))
    return issues


# --------------------------------------------------------------------------
# Alignment for field-level accuracy
# --------------------------------------------------------------------------
def align_steps(gold_steps, pred_steps, min_identity_matches=3):
    """Greedy one-to-one alignment of gold steps to predicted steps.

    Pairs are ranked by number of equal identity fields (phase, system, resource,
    operation, state), then by closeness of position. Pairs with fewer than
    ``min_identity_matches`` equal identity fields are never aligned, so a totally
    unrelated predicted step cannot earn partial field credit.
    """
    pairs = []
    for gi, g in enumerate(gold_steps):
        for pj, p in enumerate(pred_steps):
            score = sum(1 for f in IDENTITY_FIELDS if g.get(f) == p.get(f))
            if score >= min_identity_matches:
                pairs.append((-score, abs(gi - pj), gi, pj))
    pairs.sort()
    used_g, used_p, out = set(), set(), []
    for _, _, gi, pj in pairs:
        if gi in used_g or pj in used_p:
            continue
        used_g.add(gi)
        used_p.add(pj)
        out.append((gi, pj))
    return sorted(out)


def lcs_len(a, b):
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b, start=1):
            cur.append(prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


def order_concordance(gold_keys, pred_keys):
    """Fraction of gold step pairs (both present in pred) kept in the same order.

    Returns (concordant, total) over pairs; repeated keys are matched first-come.
    """
    pos = defaultdict(list)
    for j, k in enumerate(pred_keys):
        pos[k].append(j)
    taken = Counter()
    mapped = []
    for k in gold_keys:
        idx = taken[k]
        if idx < len(pos[k]):
            mapped.append(pos[k][idx])
            taken[k] += 1
    conc = total = 0
    for i in range(len(mapped)):
        for j in range(i + 1, len(mapped)):
            total += 1
            conc += mapped[i] < mapped[j]
    return conc, total


# --------------------------------------------------------------------------
# Main scoring
# --------------------------------------------------------------------------
def score(pred, gold, subset_ok=False, evidence_times=None, audit_times=None):
    """Compare ``pred`` against ``gold`` (dicts of Prediction). Returns a metrics dict.

    ``subset_ok=False`` (default): the gold file defines the universe; a gold case that
    has no predicted row counts as completely wrong. ``subset_ok=True`` scores only the
    cases present in both files (useful for partial runs).
    """
    gold_ids = sorted(gold, key=natural_key)
    pred_ids = set(pred)
    common = [c for c in gold_ids if c in pred_ids]
    universe = common if subset_ok else gold_ids
    missing = [c for c in gold_ids if c not in pred_ids]
    extra = sorted(pred_ids - set(gold), key=natural_key)

    status_ok = 0
    conf = Counter()
    exact_row = exact_row_no_ev = 0
    tp = fp = fn = 0
    tp_ev = 0
    macro_f1_list = []
    macro_f1_ev_list = []
    op_tp, op_fp, op_fn = Counter(), Counter(), Counter()
    phase_tp, phase_fp, phase_fn = Counter(), Counter(), Counter()
    field_ok = Counter()
    field_total = Counter()
    gold_steps_total = aligned_total = 0
    seq_exact = seq_exact_ev = 0
    lcs_ratios = []
    conc_num = conc_den = 0
    conc_cases = 0
    conc_full = 0
    canon_ok = canon_total = 0
    step_no_ok = step_no_total = 0
    invalid_rows = 0
    unparseable = 0
    per_case = []

    for cid in universe:
        g = gold[cid]
        p = pred.get(cid)
        gsteps = g.steps or []
        if p is None or p.steps is None:
            unparseable += 1 if p is not None else 0
            psteps = []
            pstatus = p.status if p is not None else "MISSING"
        else:
            psteps = p.steps
            pstatus = p.status
        psteps_ok = [s for s in psteps if isinstance(s, dict)]
        conf[(g.status, pstatus)] += 1
        s_ok = pstatus == g.status
        status_ok += s_ok

        gk = [step_identity(s) for s in gsteps]
        pk = [step_identity(s) for s in psteps_ok]
        gks = [step_strict(s) for s in gsteps]
        pks = [step_strict(s) for s in psteps_ok]
        gc, pc = Counter(gk), Counter(pk)
        gcs, pcs = Counter(gks), Counter(pks)
        c_tp = sum((gc & pc).values())
        c_fp = sum(pc.values()) - c_tp
        c_fn = sum(gc.values()) - c_tp
        c_tp_ev = sum((gcs & pcs).values())
        tp += c_tp
        fp += c_fp
        fn += c_fn
        tp_ev += c_tp_ev
        if gk or pk:
            cp = safe_div(c_tp, c_tp + c_fp)
            cr = safe_div(c_tp, c_tp + c_fn)
            macro_f1_list.append(f1(cp, cr))
            cpe = safe_div(c_tp_ev, sum(pcs.values()))
            cre = safe_div(c_tp_ev, sum(gcs.values()))
            macro_f1_ev_list.append(f1(cpe, cre))
        else:
            macro_f1_list.append(1.0)
            macro_f1_ev_list.append(1.0)

        for k in (gc & pc):
            op_tp[k[3]] += (gc & pc)[k]
        for k, n in (pc - gc).items():
            op_fp[k[3]] += n
            phase_fp[k[0]] += n
        for k, n in (gc - pc).items():
            op_fn[k[3]] += n
            phase_fn[k[0]] += n
        for k, n in (gc & pc).items():
            phase_tp[k[0]] += n

        # row level
        norm = lambda ss: [{f: s.get(f) for f in STEP_KEYS} for s in ss]
        row_exact = s_ok and norm(gsteps) == norm(psteps_ok) and len(psteps) == len(psteps_ok)
        strip_ev = lambda ss: [{f: s.get(f) for f in STEP_KEYS if f != "evidence_id"} for s in ss]
        row_noev = s_ok and strip_ev(gsteps) == strip_ev(psteps_ok) and len(psteps) == len(psteps_ok)
        exact_row += row_exact
        exact_row_no_ev += row_noev

        # field level
        pairs = align_steps(gsteps, psteps_ok)
        gold_steps_total += len(gsteps)
        aligned_total += len(pairs)
        for f in FIELD_ORDER:
            field_total[f] += len(gsteps)
        for gi, pj in pairs:
            for f in FIELD_ORDER:
                field_ok[f] += gsteps[gi].get(f) == psteps_ok[pj].get(f)

        # sequence level
        seq_e = gk == pk
        seq_exact += seq_e
        seq_exact_ev += gks == pks
        if gk or pk:
            lcs_ratios.append(safe_div(2 * lcs_len(gk, pk), len(gk) + len(pk)))
        else:
            lcs_ratios.append(1.0)
        a, b = order_concordance(gk, pk)
        conc_num += a
        conc_den += b
        if b:
            conc_cases += 1
            conc_full += a == b
        if psteps_ok:
            canon_total += 1
            canon_ok += not order_issues([s for s in psteps_ok if set(s) == set(STEP_KEYS) and s.get("operation") in OP_RANK and s.get("phase") in PHASE_RANK])
            step_no_total += 1
            step_no_ok += [s.get("step_no") for s in psteps_ok] == list(range(1, len(psteps_ok) + 1))

        issues = []
        if p is not None:
            ev_t = evidence_times
            issues = validate_row(p, ev_t, (audit_times or {}).get(cid))
            invalid_rows += bool(issues)
        per_case.append(
            {
                "case_id": cid,
                "gold_status": g.status,
                "pred_status": pstatus,
                "status_ok": s_ok,
                "exact_row": row_exact,
                "sequence_exact": seq_e,
                "tp": c_tp,
                "fp": c_fp,
                "fn": c_fn,
                "extra_steps": [list(k) for k in sorted((pc - gc).elements(), key=str)],
                "missing_steps": [list(k) for k in sorted((gc - pc).elements(), key=str)],
                "evidence_mismatches": _evidence_mismatches(gsteps, psteps_ok),
                "issues": issues,
            }
        )

    n = len(universe)
    prec, rec = safe_div(tp, tp + fp), safe_div(tp, tp + fn)
    prec_ev = safe_div(tp_ev, tp + fp)
    rec_ev = safe_div(tp_ev, tp + fn)

    classes = {}
    for cls in STATUSES:
        c_tp = conf[(cls, cls)]
        c_fp = sum(v for (gs, ps), v in conf.items() if ps == cls and gs != cls)
        c_fn = sum(v for (gs, ps), v in conf.items() if gs == cls and ps != cls)
        cp, cr = safe_div(c_tp, c_tp + c_fp), safe_div(c_tp, c_tp + c_fn)
        classes[cls] = {"support": c_tp + c_fn, "precision": cp, "recall": cr, "f1": f1(cp, cr)}
    present = [c for c in STATUSES if classes[c]["support"] or any(ps == c for (_, ps) in conf)]
    macro_cls = safe_div(sum(classes[c]["f1"] for c in present), len(present))

    ops = {}
    for op in sorted(set(op_tp) | set(op_fp) | set(op_fn), key=lambda o: OP_RANK.get(o, 99)):
        p_, r_ = safe_div(op_tp[op], op_tp[op] + op_fp[op]), safe_div(op_tp[op], op_tp[op] + op_fn[op])
        ops[op] = {"tp": op_tp[op], "fp": op_fp[op], "fn": op_fn[op], "precision": p_, "recall": r_, "f1": f1(p_, r_)}
    phases = {}
    for ph in PHASES:
        p_, r_ = safe_div(phase_tp[ph], phase_tp[ph] + phase_fp[ph]), safe_div(phase_tp[ph], phase_tp[ph] + phase_fn[ph])
        phases[ph] = {"tp": phase_tp[ph], "fp": phase_fp[ph], "fn": phase_fn[ph], "precision": p_, "recall": r_, "f1": f1(p_, r_)}

    return {
        "coverage": {
            "gold_cases": len(gold_ids),
            "pred_rows": len(pred),
            "scored_cases": n,
            "missing_in_pred": missing,
            "extra_in_pred": extra,
            "unparseable_rows": unparseable,
            "rows_with_contract_issues": invalid_rows,
            "subset_ok": subset_ok,
        },
        "case_level": {
            "status_accuracy": safe_div(status_ok, n),
            "correct": status_ok,
            "total": n,
            "macro_f1": macro_cls,
            "per_class": classes,
            "confusion": {"%s->%s" % k: v for k, v in sorted(conf.items())},
        },
        "row_level": {
            "exact_row_accuracy": safe_div(exact_row, n),
            "exact_row_accuracy_ignoring_evidence_id": safe_div(exact_row_no_ev, n),
            "exact_rows": exact_row,
            "mean_case_step_f1": safe_div(sum(macro_f1_list), len(macro_f1_list)),
            "mean_case_step_f1_with_evidence": safe_div(sum(macro_f1_ev_list), len(macro_f1_ev_list)),
        },
        "step_level": {
            "gold_steps": tp + fn,
            "pred_steps": tp + fp,
            "micro_precision": prec,
            "micro_recall": rec,
            "micro_f1": f1(prec, rec),
            "micro_precision_with_evidence": prec_ev,
            "micro_recall_with_evidence": rec_ev,
            "micro_f1_with_evidence": f1(prec_ev, rec_ev),
            "by_operation": ops,
            "by_phase": phases,
        },
        "field_level": {
            "gold_steps": gold_steps_total,
            "aligned_steps": aligned_total,
            "alignment_rate": safe_div(aligned_total, gold_steps_total),
            "accuracy_by_field": {f: safe_div(field_ok[f], field_total[f]) for f in FIELD_ORDER},
            "mean_field_accuracy": safe_div(sum(field_ok.values()), sum(field_total.values())),
            "mean_field_accuracy_identity_only": safe_div(
                sum(field_ok[f] for f in IDENTITY_FIELDS), sum(field_total[f] for f in IDENTITY_FIELDS)
            ),
        },
        "sequence_level": {
            "exact_sequence_accuracy": safe_div(seq_exact, n),
            "exact_sequence_accuracy_with_evidence": safe_div(seq_exact_ev, n),
            "mean_lcs_ratio": safe_div(sum(lcs_ratios), len(lcs_ratios)),
            "pairwise_order_concordance": safe_div(conc_num, conc_den, 1.0),
            "cases_with_at_least_2_common_steps": conc_cases,
            "cases_with_fully_concordant_order": conc_full,
            "canonical_order_compliance": safe_div(canon_ok, canon_total, 1.0),
            "step_no_consecutive_rate": safe_div(step_no_ok, step_no_total, 1.0),
        },
        "per_case": per_case,
    }


def _evidence_mismatches(gsteps, psteps):
    out = []
    pmap = defaultdict(list)
    for s in psteps:
        pmap[step_identity(s)].append(s.get("evidence_id"))
    for s in gsteps:
        k = step_identity(s)
        if k in pmap and s.get("evidence_id") not in pmap[k]:
            out.append({"step": list(k[1:4]), "gold": s.get("evidence_id"), "pred": pmap[k][0]})
    return out


# --------------------------------------------------------------------------
# Dataset-aware helpers (optional; used for evidence time-travel checks)
# --------------------------------------------------------------------------
def load_dataset_context(dataset_dir):
    """Return (evidence_times, audit_times) built from participant-facing files only."""
    evidence_times, audit_times = {}, {}

    def read(name):
        path = os.path.join(dataset_dir, name)
        if not os.path.exists(path):
            return []
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    if not os.path.exists(os.path.join(dataset_dir, "evidence_files.csv")):
        return None, None  # dataset not available (e.g. inside code.zip): skip evidence checks
    for r in read("evidence_files.csv"):
        evidence_times[r["file_id"]] = parse_ts(r["captured_at"])
    for r in read("system_activity.csv"):
        evidence_times[r["activity_id"]] = parse_ts(r["event_time"])
    for r in read("user_audit_log.csv"):
        evidence_times[r["audit_id"]] = parse_ts(r["event_time"])
    for name in ("deletion_requests.csv", "sample_deletion_requests.csv"):
        for r in read(name):
            audit_times[r["case_id"]] = parse_ts(r["audit_at"])
    return evidence_times, audit_times


def read_request_ids(dataset_dir, name="deletion_requests.csv"):
    path = os.path.join(dataset_dir, name)
    with open(path, newline="", encoding="utf-8") as fh:
        return [r["case_id"] for r in csv.DictReader(fh)]


def validate_submission(pred, info, expected_ids=None, evidence_times=None, audit_times=None):
    """Gold-free check of a whole output file. Returns a dict report."""
    problems = []
    if info["header"] != HEADER:
        problems.append("header %r != %r" % (info["header"], HEADER))
    if info["duplicates"]:
        problems.append("duplicate case_ids: %s" % ", ".join(sorted(set(info["duplicates"]))[:10]))
    order = info["order"]
    if order == sorted(order, key=natural_key):
        row_order = "natural" if order != sorted(order) else "natural+lexical"
    elif order == sorted(order):
        row_order = "lexical"
    else:
        row_order = "unsorted"
        problems.append("rows are not sorted by case_id")
    if expected_ids is not None:
        exp = set(expected_ids)
        missing = sorted(exp - set(order), key=natural_key)
        extra = sorted(set(order) - exp, key=natural_key)
        if missing:
            problems.append("%d missing case_ids (first: %s)" % (len(missing), ", ".join(missing[:5])))
        if extra:
            problems.append("%d unexpected case_ids (first: %s)" % (len(extra), ", ".join(extra[:5])))
        if len(order) != len(expected_ids):
            problems.append("%d data rows, expected %d" % (len(order), len(expected_ids)))
    row_issues = {}
    status_counts = Counter()
    for cid, p in pred.items():
        status_counts[p.status] += 1
        iss = validate_row(p, evidence_times, (audit_times or {}).get(cid))
        if iss:
            row_issues[cid] = iss
    return {
        "rows": len(order),
        "row_order": row_order,
        "status_counts": dict(status_counts),
        "file_problems": problems,
        "rows_with_issues": len(row_issues),
        "row_issues": row_issues,
        "ok": not problems and not row_issues,
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def pct(x):
    return "%.4f" % x


def render_markdown(m, title="Metrics"):
    c, rl, st, fl, sq, cov = m["case_level"], m["row_level"], m["step_level"], m["field_level"], m["sequence_level"], m["coverage"]
    L = []
    L.append("### %s" % title)
    L.append("")
    L.append(
        "Scored %d case(s) (gold %d, predicted rows %d, missing %d, extra %d, unparseable %d, rows with contract issues %d)."
        % (cov["scored_cases"], cov["gold_cases"], cov["pred_rows"], len(cov["missing_in_pred"]), len(cov["extra_in_pred"]), cov["unparseable_rows"], cov["rows_with_contract_issues"])
    )
    L.append("")
    L.append("| Level | Metric | Value |")
    L.append("| --- | --- | --- |")
    L.append("| Case | Status accuracy | %s (%d/%d) |" % (pct(c["status_accuracy"]), c["correct"], c["total"]))
    L.append("| Case | Status macro-F1 | %s |" % pct(c["macro_f1"]))
    L.append("| Row | Exact-row accuracy (status + all steps incl. evidence_id) | %s (%d/%d) |" % (pct(rl["exact_row_accuracy"]), rl["exact_rows"], c["total"]))
    L.append("| Row | Exact-row accuracy ignoring evidence_id | %s |" % pct(rl["exact_row_accuracy_ignoring_evidence_id"]))
    L.append("| Row | Mean per-case step F1 | %s |" % pct(rl["mean_case_step_f1"]))
    L.append("| Step | Micro precision / recall / F1 (phase, system, resource, operation, state) | %s / %s / %s |" % (pct(st["micro_precision"]), pct(st["micro_recall"]), pct(st["micro_f1"])))
    L.append("| Step | Micro F1 including evidence_id | %s |" % pct(st["micro_f1_with_evidence"]))
    L.append("| Field | Mean accuracy over all 7 step fields (gold steps, aligned) | %s |" % pct(fl["mean_field_accuracy"]))
    L.append("| Field | Mean accuracy over the 5 identity fields | %s |" % pct(fl["mean_field_accuracy_identity_only"]))
    L.append("| Sequence | Exact ordered sequence (identity keys) | %s |" % pct(sq["exact_sequence_accuracy"]))
    L.append("| Sequence | Exact ordered sequence incl. evidence_id | %s |" % pct(sq["exact_sequence_accuracy_with_evidence"]))
    L.append("| Sequence | Mean LCS ratio | %s |" % pct(sq["mean_lcs_ratio"]))
    L.append("| Sequence | Pairwise order concordance | %s |" % pct(sq["pairwise_order_concordance"]))
    L.append("| Sequence | Canonical-order compliance of predicted rows | %s |" % pct(sq["canonical_order_compliance"]))
    L.append("")
    L.append("**Per-class status metrics**")
    L.append("")
    L.append("| Class | Support | Precision | Recall | F1 |")
    L.append("| --- | --- | --- | --- | --- |")
    for cls, v in c["per_class"].items():
        L.append("| %s | %d | %s | %s | %s |" % (cls, v["support"], pct(v["precision"]), pct(v["recall"]), pct(v["f1"])))
    L.append("")
    L.append("**Confusion (gold->pred)**: " + (", ".join("%s: %d" % kv for kv in c["confusion"].items()) or "none"))
    L.append("")
    L.append("**Field-level accuracy** (alignment rate %s: %d of %d gold steps matched to a predicted step)" % (pct(fl["alignment_rate"]), fl["aligned_steps"], fl["gold_steps"]))
    L.append("")
    L.append("| " + " | ".join(FIELD_ORDER) + " |")
    L.append("| " + " | ".join("---" for _ in FIELD_ORDER) + " |")
    L.append("| " + " | ".join(pct(fl["accuracy_by_field"][f]) for f in FIELD_ORDER) + " |")
    L.append("")
    L.append("**Step precision/recall by operation**")
    L.append("")
    L.append("| Operation | TP | FP | FN | Precision | Recall | F1 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    for op, v in st["by_operation"].items():
        L.append("| %s | %d | %d | %d | %s | %s | %s |" % (op, v["tp"], v["fp"], v["fn"], pct(v["precision"]), pct(v["recall"]), pct(v["f1"])))
    L.append("")
    return "\n".join(L)


def render_failures(m, limit=10):
    L = []
    bad = [c for c in m["per_case"] if not c["exact_row"] or c["issues"]]
    L.append("%d of %d scored cases are not an exact match." % (len([c for c in m["per_case"] if not c["exact_row"]]), len(m["per_case"])))
    for c in bad[:limit]:
        L.append("")
        L.append("- %s: gold=%s pred=%s status_ok=%s sequence_exact=%s" % (c["case_id"], c["gold_status"], c["pred_status"], c["status_ok"], c["sequence_exact"]))
        for k in c["missing_steps"]:
            L.append("    missing: %s" % " ".join(map(str, k)))
        for k in c["extra_steps"]:
            L.append("    extra:   %s" % " ".join(map(str, k)))
        for e in c["evidence_mismatches"]:
            L.append("    evidence: %s gold=%s pred=%s" % ("/".join(e["step"]), e["gold"], e["pred"]))
        for i in c["issues"][:5]:
            L.append("    contract: %s" % i)
    if len(bad) > limit:
        L.append("")
        L.append("... %d more (use --failures N)" % (len(bad) - limit))
    return "\n".join(L)
