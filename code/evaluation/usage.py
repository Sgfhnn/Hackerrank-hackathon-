"""Token / cost accounting for the final full-dataset run.

The deletion-audit pipeline is mostly deterministic, but any step that calls a
model (OCR / vision extraction, intent classification, LLM planning ...) must report
its usage here so that ``code/evaluation/usage_report.md`` can be generated from
facts instead of guesses.

Recording a call (one line of JSON appended to ``code/evaluation/usage_log.jsonl``)::

    from evaluation.usage import record_usage          # or: from usage import record_usage
    resp = client.chat.completions.create(...)
    record_usage("openai", resp.model, resp.usage.prompt_tokens,
                 resp.usage.completion_tokens, case_id="case_42", purpose="intent")

Only counts and labels are stored - never prompts, responses, keys or other secrets.

Report generation::

    python3 code/evaluation/main.py usage --out code/evaluation/usage_report.md

Prices live in ``PRICING`` (USD per 1M tokens). They are intentionally empty: fill them
in from your provider's price page (or export ``USAGE_PRICING_JSON``) before the final
run. Models without a price are listed as "unpriced" and contribute $0 to the totals,
and the report says so explicitly.
"""

import json
import os
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.join(HERE, "usage_log.jsonl")

# model name -> {"input": usd_per_1M, "output": usd_per_1M, "cached_input": usd_per_1M (optional)}
PRICING = {
    # "example-model": {"input": 0.0, "output": 0.0},
}


def _pricing():
    table = dict(PRICING)
    raw = os.environ.get("USAGE_PRICING_JSON")
    if raw:
        try:
            table.update(json.loads(raw))
        except ValueError:
            pass
    return table


def log_path():
    return os.environ.get("USAGE_LOG_PATH", DEFAULT_LOG)


def record_usage(provider, model, input_tokens, output_tokens, case_id=None, purpose="", cached_input_tokens=0, path=None):
    """Append one model call to the usage log (single-line appends)."""
    entry = OrderedDict(
        ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        provider=str(provider),
        model=str(model),
        input_tokens=int(input_tokens or 0),
        output_tokens=int(output_tokens or 0),
        cached_input_tokens=int(cached_input_tokens or 0),
        case_id=case_id,
        purpose=purpose,
    )
    with open(path or log_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def load_log(path=None):
    path = path or log_path()
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def cost_of(model, inp, out, cached=0, table=None):
    """USD cost for one aggregate, or None if the model has no configured price."""
    price = (table if table is not None else _pricing()).get(model)
    if price is None:
        return None
    cached_price = price.get("cached_input", price["input"])
    return ((inp - cached) * price["input"] + cached * cached_price + out * price["output"]) / 1e6


def summarize(entries, n_requests, table=None):
    table = table if table is not None else _pricing()
    per_model = OrderedDict()
    for e in entries:
        key = (e["provider"], e["model"])
        m = per_model.setdefault(key, {"calls": 0, "input": 0, "output": 0, "cached": 0, "cases": set()})
        m["calls"] += 1
        m["input"] += e["input_tokens"]
        m["output"] += e["output_tokens"]
        m["cached"] += e.get("cached_input_tokens", 0)
        if e.get("case_id"):
            m["cases"].add(e["case_id"])
    rows = []
    total = {"calls": 0, "input": 0, "output": 0, "cost": 0.0, "unpriced": []}
    for (prov, model), m in sorted(per_model.items()):
        c = cost_of(model, m["input"], m["output"], m["cached"], table)
        rows.append({"provider": prov, "model": model, **{k: m[k] for k in ("calls", "input", "output", "cached")}, "cost": c, "cases": len(m["cases"])})
        total["calls"] += m["calls"]
        total["input"] += m["input"]
        total["output"] += m["output"]
        if c is None:
            total["unpriced"].append(model)
        else:
            total["cost"] += c
    total["tokens"] = total["input"] + total["output"]
    total["tokens_per_request"] = total["tokens"] / n_requests if n_requests else 0.0
    total["cost_per_request"] = total["cost"] / n_requests if n_requests else 0.0
    by_purpose = defaultdict(lambda: {"calls": 0, "input": 0, "output": 0})
    for e in entries:
        p = by_purpose[e.get("purpose") or "unspecified"]
        p["calls"] += 1
        p["input"] += e["input_tokens"]
        p["output"] += e["output_tokens"]
    return rows, total, dict(by_purpose)


def render_usage_report(entries, n_requests, run_note="", table=None):
    rows, total, by_purpose = summarize(entries, n_requests, table)
    fmt_cost = lambda c: "unpriced" if c is None else "$%.4f" % c
    L = ["# Usage report: tokens and cost of the final full-dataset run", ""]
    if run_note:
        L += [run_note, ""]
    L += [
        "Requests processed: **%d** (rows of `dataset/deletion_requests.csv`).  " % n_requests,
        "Generated by `python3 code/evaluation/main.py usage` from `code/evaluation/usage_log.jsonl` "
        "(counts only; no prompts, responses or credentials are stored).",
        "",
    ]
    if not entries:
        L += [
            "## Result",
            "",
            "No model calls were recorded for this run: the pipeline that produced `output.csv` was fully "
            "deterministic (rules + SQLite/JSON/log parsing), so input tokens, output tokens and estimated "
            "cost are all **0**. Any component that starts calling a model must use "
            "`evaluation/usage.py::record_usage`, and this report must be regenerated after the final run.",
            "",
        ]
    L += ["## Per model", "", "| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Est. cost (USD) |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for r in rows:
        L.append("| %s | %s | %d | %d | %d | %d | %s |" % (r["provider"], r["model"], r["calls"], r["input"], r["output"], r["input"] + r["output"], fmt_cost(r["cost"])))
    if not rows:
        L.append("| (none) | (none) | 0 | 0 | 0 | 0 | $0.0000 |")
    L += [
        "",
        "## Overall",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        "| Model calls | %d |" % total["calls"],
        "| Input tokens | %d |" % total["input"],
        "| Output tokens | %d |" % total["output"],
        "| Total tokens | %d |" % total["tokens"],
        "| Average tokens per request | %.2f |" % total["tokens_per_request"],
        "| Estimated total cost (USD) | $%.4f |" % total["cost"],
        "| Estimated cost per request (USD) | $%.6f |" % total["cost_per_request"],
        "",
    ]
    if total["unpriced"]:
        L += ["> Unpriced models (counted as $0, set `PRICING` in `evaluation/usage.py`): %s." % ", ".join(sorted(set(total["unpriced"]))), ""]
    if by_purpose:
        L += ["## By purpose", "", "| Purpose | Calls | Input tokens | Output tokens |", "| --- | ---: | ---: | ---: |"]
        for k, v in sorted(by_purpose.items()):
            L.append("| %s | %d | %d | %d |" % (k, v["calls"], v["input"], v["output"]))
        L.append("")
    L += [
        "## How the cost is estimated",
        "",
        "Cost = (non-cached input tokens x input price + cached input tokens x cached price + output tokens x output price) / 1,000,000, "
        "using the per-model USD prices configured in `PRICING`. Average tokens per request divides total tokens by the number of requests above, "
        "not by the number of model calls.",
        "",
    ]
    return "\n".join(L)
