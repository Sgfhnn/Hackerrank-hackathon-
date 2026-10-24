"""Evidence plug-in for the OpenCode core (`code/main.py`): adapter onto the Hark engine.

Core contract:  audit_case(ctx, request, subject) -> (status, steps)
  * status is COMPLETE / INCOMPLETE / NOT_APPLICABLE
  * steps are dicts in the output schema (step_no, phase, system_id, resource_id,
    operation, state, evidence_id); core re-sorts/numbers them via finalize_steps.

The Hark engine (code/hark/engine.py) is built once per Context and cached in
ctx.extras["hark_engine"]. Its activity->case assignment is global, so it is
prepared on the current run's requests plus every other request the dataset contains
(sample + test files); a dataset request with the same email and reported_at as a run request is
skipped so it cannot steal that request's activity.
Stdlib only, no model calls. Works from repo root (`python3 code/main.py`) or any cwd.
"""
import csv
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_HARK_DIR = os.path.join(_HERE, "hark")
_EXTRA_KEY = "hark_engine"
_REQUEST_FILES = ("sample_deletion_requests.csv", "deletion_requests.csv")
_FIELDS = ("case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text")


def _dataset_dir(ctx):
    d = getattr(ctx, "dataset_dir", None)
    return os.path.abspath(str(d)) if d else None


def _import_engine(dataset_dir):
    """Import code/hark/engine.py with ingest.DATASET pointed at the core's dataset."""
    if dataset_dir and "ingest" not in sys.modules:
        # ingest resolves DATASET at import time; DATASET_DIR wins over its own search.
        os.environ.setdefault("DATASET_DIR", dataset_dir)
    if _HARK_DIR not in sys.path:
        sys.path.insert(0, _HARK_DIR)
    import engine as hark_engine  # noqa: E402  (code/hark/engine.py)
    return hark_engine


def _case_dict(request):
    """Request dataclass -> the plain CSV-row dict the Hark engine reads."""
    raw = dict(getattr(request, "raw", None) or {})
    for name in _FIELDS:
        if raw.get(name) in (None, ""):
            val = getattr(request, name + "_raw", None) or getattr(request, name, "")
            raw[name] = "" if val is None else str(val)
    return raw


def _all_cases(ctx, dataset_dir):
    cases, seen = [], set()

    def add(row):
        cid = row.get("case_id")
        if cid and cid not in seen:
            seen.add(cid)
            cases.append(row)

    # The current run's requests go first and win. A dataset request that describes the same
    # event (same email and reported_at) as one of them is left out, so it cannot claim the
    # shared account activity away from the request actually being audited (Cue finding F6).
    run = [_case_dict(req) for req in getattr(ctx, "requests", None) or []]
    for row in run:
        add(row)
    taken = {_event_key(r) for r in run}
    if dataset_dir:
        for name in _REQUEST_FILES:
            path = os.path.join(dataset_dir, name)
            if os.path.exists(path):
                with open(path, newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        if _event_key(row) not in taken:
                            add(row)
    return cases


def _event_key(row):
    return ((row.get("email") or "").strip().lower(), (row.get("reported_at") or "").strip())


def get_engine(ctx):
    extras = getattr(ctx, "extras", None)
    if isinstance(extras, dict) and extras.get(_EXTRA_KEY) is not None:
        return extras[_EXTRA_KEY]
    dataset_dir = _dataset_dir(ctx)
    mod = _import_engine(dataset_dir)
    eng = mod.Engine()
    eng.prepare(_all_cases(ctx, dataset_dir))
    if isinstance(extras, dict):
        extras[_EXTRA_KEY] = eng
    return eng


def audit_case(ctx, request, subject):
    eng = get_engine(ctx)
    status, steps, _debug = eng.plan(_case_dict(request))
    if status == "NOT_APPLICABLE":
        return "NOT_APPLICABLE", []
    return status, [dict(s) for s in steps]
