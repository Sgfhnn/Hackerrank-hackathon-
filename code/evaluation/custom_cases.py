"""Custom (team-made) evaluation cases for the deletion-audit agent.

Each case is a *metamorphic variant* of one of the 32 solved samples: the request is
changed in a way that must (or must not) change the answer, and the expected output is
derived from the published ``dataset/sample_output.csv`` - no organizer-only data and no
hand-invented labels. Because every case starts from a different sample, no two cases
share a ``reported_at`` timestamp, which matters for agents that link system activity
to a request through that time.

    python3 code/evaluation/main.py custom                       # (re)build custom/custom_requests.csv + custom_gold.csv
    python3 code/evaluation/main.py custom --agent "python3 code/main.py --requests {requests} --out {out}"

``--agent`` is a shell command template; ``{requests}`` and ``{out}`` are replaced by file paths.
The agent must read the given requests CSV (same columns as dataset/deletion_requests.csv)
and write ``case_id,deletion_status,steps_json`` rows to ``{out}``.
"""

import csv
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import scorer  # noqa: E402

REQUEST_FIELDS = ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"]

ACCESS_REQUEST = (
    "Can you tell me what personal information you hold about me? This is an information request only; "
    "I still use the account, so please do not delete anything."
)
MARKETING_OPT_OUT = "Please remove me from marketing lists only. I still want receipts and service messages for my orders."
CORRECTION = "My surname is misspelled on the account. Can you correct it without removing any of my records?"
KOREAN_DELETE = (
    "이 이메일 주소에 연결된 계정의 영구 삭제를 요청합니다. 개인정보 처리 절차에 따라 현재 및 이전 식별자를 검토해 주세요. "
    "검토가 완료되면 결과를 확인해 주세요."
)

# id, base sample, mutation key, what it tests, expected outcome
CASES = [
    ("custom_01", "case_11", "text_access",
     "Information-only request on a case that would otherwise be a 14-step INCOMPLETE audit. "
     "Non-deletion intent must win over leftover records in the evidence.",
     "NOT_APPLICABLE with []"),
    ("custom_02", "case_15", "text_marketing",
     "Marketing opt-out only (account must stay). An active data flow exists in the evidence, "
     "but no deletion is authorised.",
     "NOT_APPLICABLE with []"),
    ("custom_03", "case_26", "text_correction",
     "Profile-correction request on a fully compliant (COMPLETE-looking) subject: the agent must not emit "
     "verification steps because no deletion was authorised.",
     "NOT_APPLICABLE with []"),
    ("custom_04", "case_29", "email_current",
     "The original request used a HISTORICAL e-mail; here the same person writes with the CURRENT e-mail. "
     "Identity resolution must reach the same records and findings.",
     "identical to sample case_29 gold"),
    ("custom_05", "case_05", "email_upper",
     "Same request with the e-mail in UPPER CASE (15 SQL snapshot files store some e-mails in upper case). "
     "Matching must be case-insensitive.",
     "identical to sample case_05 gold"),
    ("custom_06", "case_18", "status_flip",
     "reported_status flipped COMPLETE -> NOT_STARTED. The workflow claim is not evidence, "
     "so the audit result must not move.",
     "identical to sample case_18 gold"),
    ("custom_07", "case_28", "status_flip2",
     "reported_status flipped NOT_STARTED -> PARTIALLY_COMPLETE on a COMPLETE case; the claim must not turn "
     "a compliant subject into INCOMPLETE.",
     "identical to sample case_28 gold"),
    ("custom_08", "case_16", "text_korean",
     "Deletion request rewritten in Korean (a template that exists in the real request file). "
     "Multilingual intent detection must still authorise deletion.",
     "identical to sample case_16 gold"),
]


def _read(dataset, name):
    with open(os.path.join(dataset, name), newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def build(dataset):
    """Return (request_rows, gold_rows) as lists of dicts."""
    req = {r["case_id"]: r for r in _read(dataset, "sample_deletion_requests.csv")}
    gold = {r["case_id"]: r for r in _read(dataset, "sample_output.csv")}
    ident = {i["user_id"]: i for i in _read(dataset, "user_identities.csv")}
    audit = _read(dataset, "user_audit_log.csv")

    def current_email(old_email):
        for a in audit:
            if a["field_name"] == "email" and a["old_value"].lower() == old_email.lower():
                return ident[a["user_id"]]["email"]
        raise SystemExit("no historical e-mail %s in user_audit_log.csv" % old_email)

    out_req, out_gold = [], []
    for cid, base, mut, _desc, _exp in CASES:
        r = dict(req[base])
        g = dict(gold[base])
        na = False
        if mut == "text_access":
            r["request_text"], na = ACCESS_REQUEST, True
        elif mut == "text_marketing":
            r["request_text"], na = MARKETING_OPT_OUT, True
        elif mut == "text_correction":
            r["request_text"], na = CORRECTION, True
        elif mut == "email_current":
            r["email"] = current_email(r["email"])
        elif mut == "email_upper":
            r["email"] = r["email"].upper()
        elif mut == "status_flip":
            r["reported_status"] = "NOT_STARTED" if r["reported_status"] == "COMPLETE" else "COMPLETE"
        elif mut == "status_flip2":
            r["reported_status"] = "PARTIALLY_COMPLETE"
        elif mut == "text_korean":
            r["request_text"] = KOREAN_DELETE
        r["case_id"] = cid
        g["case_id"] = cid
        if na:
            g["deletion_status"], g["steps_json"] = "NOT_APPLICABLE", "[]"
        out_req.append(r)
        out_gold.append(g)
    return out_req, out_gold


def write_files(dataset, workdir):
    os.makedirs(workdir, exist_ok=True)
    reqs, gold = build(dataset)
    rp, gp = os.path.join(workdir, "custom_requests.csv"), os.path.join(workdir, "custom_gold.csv")
    with open(rp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, REQUEST_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(reqs)
    with open(gp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, ["case_id", "deletion_status", "steps_json"], lineterminator="\n")
        w.writeheader()
        for g in gold:
            w.writerow({k: g[k] for k in ("case_id", "deletion_status", "steps_json")})
    return rp, gp


def run(a):
    rp, gp = write_files(a.dataset, a.workdir)
    print("wrote", rp)
    print("wrote", gp)
    gold, _ = scorer.load_output(gp)
    self_m = scorer.score(gold, gold)  # the gold file must score perfectly against itself
    assert self_m["row_level"]["exact_row_accuracy"] == 1.0
    if not a.agent:
        print("self-check OK (gold vs gold = 1.0). Pass --agent to run an agent on these cases.")
        return 0
    out = os.path.join(a.workdir, "custom_pred.csv")
    cmd = a.agent.format(requests=rp, out=out)
    print("running:", cmd)
    rc = subprocess.call(cmd, shell=True)
    if rc != 0 or not os.path.exists(out):
        print("agent failed (exit %s)" % rc)
        return 1
    pred, _ = scorer.load_output(out)
    m = scorer.score(pred, gold)
    for c in m["per_case"]:
        desc = next(x for x in CASES if x[0] == c["case_id"])
        print("%-10s %-4s  gold=%-14s pred=%-14s  base=%s" % (c["case_id"], "PASS" if c["exact_row"] else "FAIL", c["gold_status"], c["pred_status"], desc[1]))
    print()
    print(scorer.render_markdown(m, "Custom cases"))
    print(scorer.render_failures(m, 20))
    return 0 if m["row_level"]["exact_row_accuracy"] == 1.0 else 1
