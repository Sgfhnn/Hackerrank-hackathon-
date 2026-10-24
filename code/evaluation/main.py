#!/usr/bin/env python3
"""Evaluation CLI for the Ghost Data deletion-audit agent.

Score a prediction file against a gold file (case, row, step, field and sequence level)::

    python3 code/evaluation/main.py score --pred my_sample_output.csv
    python3 code/evaluation/main.py score --pred my.csv --gold dataset/sample_output.csv --failures 20
    python3 code/evaluation/main.py --pred my.csv                     # "score" is the default command

Check the real 518-row submission (no gold needed; format, ordering, evidence rules)::

    python3 code/evaluation/main.py check --pred output.csv

Regenerate the token / cost report from the usage log::

    python3 code/evaluation/main.py usage --out code/evaluation/usage_report.md

Build / run the custom evaluation cases against any agent entry point::

    python3 code/evaluation/main.py custom
    python3 code/evaluation/main.py custom --agent "python3 code/main.py --requests {requests} --out {out}"

Everything is standard library only and deterministic.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import scorer  # noqa: E402
import usage  # noqa: E402

DATASET = os.path.join(ROOT, "dataset")
METRIC_START = "<!-- METRICS:START -->"
METRIC_END = "<!-- METRICS:END -->"


def _splice(path, block, start=METRIC_START, end=METRIC_END):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if start not in text or end not in text:
        raise SystemExit("markers %s / %s not found in %s" % (start, end, path))
    head, rest = text.split(start, 1)
    _, tail = rest.split(end, 1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(head + start + "\n" + block.rstrip() + "\n" + end + tail)


def cmd_score(a):
    gold, _ = scorer.load_output(a.gold)
    pred, _ = scorer.load_output(a.pred)
    ev_t, audit_t = (None, None) if a.no_dataset else scorer.load_dataset_context(a.dataset)
    m = scorer.score(pred, gold, subset_ok=a.subset_ok, evidence_times=ev_t, audit_times=audit_t)
    cov = m["coverage"]
    if gold and not (set(gold) & set(pred)):
        ids = sorted(gold, key=scorer.natural_key)
        sys.stderr.write(
            "WARNING: none of the gold case_ids (%s..%s) appear in %s.\n"
            "         Run your agent on the matching requests file (dataset/sample_deletion_requests.csv\n"
            "         for dataset/sample_output.csv) and score that output; the 518-row output.csv has no public gold.\n"
            % (ids[0], ids[-1], a.pred)
        )
    md = scorer.render_markdown(m, title=a.title)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(m, fh, indent=2, sort_keys=True)
    if a.markdown:
        with open(a.markdown, "w", encoding="utf-8") as fh:
            fh.write(md + "\n")
    if a.update_report:
        _splice(a.update_report, md + "\n```text\n" + scorer.render_failures(m, a.failures) + "\n```")
    if a.quiet_json:
        print(json.dumps({k: v for k, v in m.items() if k != "per_case"}, indent=2, sort_keys=True))
    else:
        print(md)
        print(scorer.render_failures(m, a.failures))
    if a.min_status_acc is not None and m["case_level"]["status_accuracy"] < a.min_status_acc:
        return 1
    return 0


def cmd_check(a):
    pred, info = scorer.load_output(a.pred)
    ev_t, audit_t = scorer.load_dataset_context(a.dataset)
    expected = scorer.read_request_ids(a.dataset, a.requests)
    rep = scorer.validate_submission(pred, info, expected, ev_t, audit_t)
    print("rows: %d (expected %d)   row order: %s   statuses: %s" % (rep["rows"], len(expected), rep["row_order"], rep["status_counts"]))
    for p in rep["file_problems"]:
        print("FILE PROBLEM:", p)
    for cid in sorted(rep["row_issues"], key=scorer.natural_key)[: a.max_issues]:
        for i in rep["row_issues"][cid][:3]:
            print("  %s: %s" % (cid, i))
    if rep["rows_with_issues"] > a.max_issues:
        print("  ... %d more rows with issues" % (rep["rows_with_issues"] - a.max_issues))
    if rep["row_order"] == "natural":
        print("NOTE: rows are in natural order (case_33, case_34 ... case_100), the same order as dataset/deletion_requests.csv; "
              "a plain string sort would put case_100 before case_33. Decide with the team which one the grader expects (see DECISIONS.md).")
    ok = rep["ok"]
    print("RESULT:", "OK - ready to submit" if ok else "NOT OK (%d file problem(s), %d row(s) with issues)" % (len(rep["file_problems"]), rep["rows_with_issues"]))
    return 0 if ok else 1


def cmd_usage(a):
    entries = usage.load_log(a.log)
    n = a.requests if a.requests is not None else len(scorer.read_request_ids(a.dataset))
    text = usage.render_usage_report(entries, n, run_note=a.note)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print("wrote", a.out)
    else:
        print(text)
    return 0


def cmd_custom(a):
    import custom_cases

    return custom_cases.run(a)


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("score", help="score a prediction CSV against a gold CSV")
    s.add_argument("--pred", default=os.path.join(ROOT, "output.csv"))
    s.add_argument("--gold", default=os.path.join(DATASET, "sample_output.csv"))
    s.add_argument("--dataset", default=DATASET)
    s.add_argument("--no-dataset", action="store_true", help="skip evidence-id existence / time-travel checks")
    s.add_argument("--subset-ok", action="store_true", help="score only cases present in both files (default: missing rows count as wrong)")
    s.add_argument("--failures", type=int, default=10, help="how many non-exact cases to print")
    s.add_argument("--json", help="write full metrics (incl. per-case) as JSON")
    s.add_argument("--markdown", help="write the metrics tables as Markdown")
    s.add_argument("--update-report", help="splice the metrics into a Markdown file between METRICS:START/END markers")
    s.add_argument("--title", default="Metrics")
    s.add_argument("--quiet-json", action="store_true", help="print compact JSON instead of Markdown")
    s.add_argument("--min-status-acc", type=float, help="exit 1 if status accuracy is below this value (CI gate)")
    s.set_defaults(func=cmd_score)

    c = sub.add_parser("check", help="validate a full submission (header, row count, order, schema, evidence)")
    c.add_argument("--pred", default=os.path.join(ROOT, "output.csv"))
    c.add_argument("--dataset", default=DATASET)
    c.add_argument("--requests", default="deletion_requests.csv", help="file inside --dataset listing the expected case_ids")
    c.add_argument("--max-issues", type=int, default=15)
    c.set_defaults(func=cmd_check)

    u = sub.add_parser("usage", help="build usage_report.md from usage_log.jsonl")
    u.add_argument("--log", default=None)
    u.add_argument("--out", default=os.path.join(HERE, "usage_report.md"))
    u.add_argument("--requests", type=int, default=None, help="requests in the run (default: rows of deletion_requests.csv)")
    u.add_argument("--dataset", default=DATASET)
    u.add_argument("--note", default="")
    u.set_defaults(func=cmd_usage)

    k = sub.add_parser("custom", help="build / run the custom evaluation cases")
    k.add_argument("--agent", help="shell command template with {requests} and {out}; omit to only (re)build the case files")
    k.add_argument("--dataset", default=DATASET)
    k.add_argument("--workdir", default=os.path.join(HERE, "custom"))
    k.set_defaults(func=cmd_custom)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("score", "check", "usage", "custom", "-h", "--help"):
        argv = ["score"] + argv
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
