# Core module (`code/main.py`)

Standard-library-only core for the Ghost Data Still Exists audit. It loads the
participant-facing dataset, resolves current/historical identities, provides the
step/output contract, and hosts the two plug-in seams. Import has no side
effects; the CLI is behind `if __name__ == "__main__"`.

## Run

```bash
python3 code/main.py --selftest
python3 code/main.py --limit 20 --output /tmp/out_test.csv
python3 code/main.py --output /tmp/out_full.csv
python3 code/main.py --check /tmp/out_full.csv
```

Default output is `<repo>/output.csv` (resolved from `__file__`). `--dataset DIR`
loads another dataset directory; `--requests PATH` overrides the request file;
`--strict-evidence` requires every cited evidence id to exist and predate
`audit_at`.

## Public interfaces

Paths: `REPO_ROOT`, `DATASET_DIR`, `OUTPUT_PATH`.

Time: `parse_ts(value) -> datetime | None`, `to_iso_z(dt) -> str`,
`format_eat(dt) -> str` (display only), `ts_le(a, b) -> bool`.

Identifiers: `normalize_email`, `normalize_phone`, `sha256_email`.

Records (frozen dataclasses, parsed datetimes plus raw strings):
`Request`, `Identity`, `AuditEvent`, `Activity`, `Rule`, `EvidenceFile`,
`Subject`, `Step`.

Loaders (return lists, validate headers): `load_requests(path=None)`,
`load_identities(path=None)`, `load_audit_log(path=None)`,
`load_activity(path=None)`, `load_rules(path=None)`,
`load_evidence_manifest(path=None)`.

Identity:

```python
IdentityResolver(identities, audit_log)
    .resolve(email, as_of=None) -> Subject | None
    .identifiers_for_search(subject) -> dict
    .owns_identifier(subject, value) -> bool
```

`Subject` exposes `user_id`, `name`, `current_email`, `current_phone`,
`current_email_hash`, `emails`, `phones`, `email_hashes`, `matched_via`
(`current_email` / `historical_email` / `email_hash`), `history`, and `as_of`.
When `as_of` is set, the `current_*` values are the ones in effect at that time
and all identifier sets exclude values introduced later.

Context and steps:

```python
load_context(dataset_dir=None) -> Context
Step(phase, system_id, resource_id, operation, state, evidence_id="")
finalize_steps(steps) -> list[dict]          # dedupe, sort, number
validate_steps(status, steps, request=None, ctx=None, strict_evidence=False) -> list[str]
build_row(case_id, status, steps) -> dict
write_output(rows, path=OUTPUT_PATH) -> Path
check_output_file(path, requests) -> list[str]
solve_case(ctx, request, classifier, auditor, strict_evidence=False, problems=None)
```

`Context` holds `dataset_dir`, `requests`, `identities`, `audit_log`,
`activity`, `rules`, `evidence`, `resolver`, `activity_by_resource_id`,
`activity_by_target_system`, `activity_by_event_type`, `activity_by_id`,
`evidence_by_id`, `rules_by_category`, `audit_by_id`, `audits_by_user`, and an
`extras` dict for co-operating modules.

## Plug-ins

- `code/classifier.py`: `is_deletion_request(request: Request) -> bool`.
- `code/evidence.py`: `audit_case(ctx: Context, request: Request, subject: Subject)
  -> tuple[str, list[Step | dict]]`.

Core inserts `code/` into `sys.path`, imports the module, and stubs only when the
module itself is absent. The classifier stub returns `True`; the evidence stub
returns `("INCOMPLETE", [])` and warns once on stderr. Plug-in and validation
failures are listed in the run report; the affected case falls back to
`("INCOMPLETE", [])` so the run still produces all rows.

`--selftest` checks: 32/32 sample round-trips and ordering, 518 identity
resolutions (418 current / 100 historical / 0 unresolved), timestamp parsing
across every dataset CSV, `as_of` reverse-replay on a real audit chain, and a
temporary output/check cycle.
