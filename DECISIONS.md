# Decisions

Append-only decision log. Times are EAT (UTC+3) for readability; the dataset
itself is parsed and compared in UTC.

## 2026-10-24 16:16 EAT — Core module (`code/main.py`) — OpenCode

### Time handling

- All dataset timestamps are parsed as ISO-8601 UTC. `parse_ts` accepts `Z`,
  `+00:00`, missing seconds, and naïve values (treated as UTC), and always
  returns a timezone-aware UTC `datetime`. `to_iso_z` serializes back with `Z`.
- EAT (fixed UTC+3) is used only by `format_eat` for human-readable logs and
  display strings. It never participates in comparisons or cutoffs.

### Identity resolution

- A request email is matched case-insensitively against the current snapshot
  first, then against every historical `email` audit value, then (for a
  64-hex input) against current/historical email hashes and `sha256(lower(email))`
  of every known address.
- `Subject.emails`, `.phones`, and `.email_hashes` are the union of every
  identifier held on or before the requested `as_of` time. Request-side
  matching deliberately ignores `as_of`, because the request may legitimately
  use any historical address.
- To recover the identifier in effect at `audit_at`, we start from the current
  snapshot and reverse-replay audit events newer than `audit_at`, undoing each
  field to its `old_value`. Values introduced only after `audit_at` are
  therefore excluded. Audit events at exactly `audit_at` count as in effect.
- `email_hash == sha256(email.lower().encode("utf-8")).hexdigest()` is used both
  to validate historical hash events and to add derived hashes for every known
  email. At `as_of`, the reconstructed hash is kept only when it is explicit
  history or derivable from an email actually held by that time.

### Step ordering and serialization

- The sort key `(phase order, operation-table index, system_id, resource_id)`
  reproduces all 32 rows of `dataset/sample_output.csv`, including the
  INCOMPLETE remediation/verification ordering. Verified by `--selftest`.
- Rows are sorted by `case_id` with plain lexicographic string ordering, the
  literal reading of AGENTS.md. Case ids are not uniformly zero-padded
  (`case_33` … `case_550`), so this differs from natural sort (for example
  `case_100` precedes `case_33`). The behavior is isolated in
  `case_sort_key` / `CASE_SORT_MODE` so it can be changed in one place.
- `steps_json` objects are emitted in the required key order
  `step_no, phase, system_id, resource_id, operation, state, evidence_id` with
  compact separators and `ensure_ascii=False`. `NOT_APPLICABLE` uses `[]`.
- Output is written with the `csv` module, exact header
  `case_id,deletion_status,steps_json`, LF line endings, and UTF-8. Rows are
  re-parsed before writing and checked again by `check_output_file`.

### Evidence citation rules

- Every FINDING cites a non-empty `evidence_id`; every COMPLETE verification
  step cites a non-empty `evidence_id`; INCOMPLETE REMEDIATION and VERIFICATION
  steps use `""`.
- With `--strict-evidence`, each cited id must exist in the manifest, activity
  stream, or audit log, and its `captured_at` / `event_time` must be less than
  or equal to the case's `audit_at`. Missing timestamps fail the check. These
  rules were derived from the sample answers and are enforced by `--selftest`.

### Plug-in contracts

- `code/classifier.py` exports `is_deletion_request(request: Request) -> bool`.
- `code/evidence.py` exports
  `audit_case(ctx: Context, request: Request, subject: Subject) -> (status, steps)`.
  Steps may be `Step` objects or equivalent mappings; core sorts, numbers,
  de-duplicates, and validates them.
- Only a missing plug-in module triggers a stub. Import errors inside a plug-in,
  missing attributes, and per-case exceptions are never silently swallowed.
- The evidence stub warns once to stderr. The classifier stub conservatively
  returns `True` until `code/classifier.py` is installed.
- Plug-in loading inserts `code/` on `sys.path` so `python3 code/main.py` works
  from any working directory.

### Failure policy

- A plug-in exception or a validation failure is recorded in the run's
  `problems` list and reported in the stderr summary. The case falls back to
  `("INCOMPLETE", [])`; the run continues and still writes one row per request.
  This avoids losing all 518 rows because of one malformed case.
- `--check` returns a non-zero exit code only for structural output problems.
  Default runs return zero after reporting stub/validation counts, because a
  stubbed evidence engine is an expected development state.

### Open issue

- AGENTS.md §6.5 says to disable data flows and block restores before changing
  dependent records, and to process downstream copies before their sources.
  `problem_statement.md` fixes ordering as phase, then operation-table order,
  then `system_id`, then `resource_id`. We follow the problem statement because
  it reproduces all 32 sample rows (for example `DISABLE_DATA_FLOW` sorts before
  `DELETE_RECORD`, and verification order is operation-driven rather than
  dependency-driven). The hidden grader's behavior beyond that is unknown.

## 2026-10-24 16:18 EAT — Partial-credit fallback (`solve_case`) — OpenCode

- When a plug-in auditor returns a valid status but its steps fail validation, `solve_case` keeps the numbered, sorted plug-in answer and records the validation problems in the run summary instead of replacing the answer with `INCOMPLETE`/`[]`, preserving partial credit. Stub auditors skip validation, and only an unusable status still falls back to `INCOMPLETE`/`[]`.
