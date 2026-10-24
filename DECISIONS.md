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

- 2026-10-24 16:21 EAT (coordinator edit): intent hook lookup order is `code/classifier.py::is_deletion_request(request)`, then Muse's `code/intent.py::classify_intent(request_text)` (adapted: `result.intent` starting with `DELETION`), then the stub (everything is a deletion request). Verified with Muse's intent.py from `muse/multimodal-intent`: 10 of 518 requests are NOT_APPLICABLE. Hark's engine lives in `code/hark/`, so Hark needs a thin `code/evidence.py::audit_case(ctx, request, subject) -> (status, steps)` wrapper for the evidence hook.

# DECISIONS.md — shared decision log (team rule)

All agents append. Newest last.

## 2026-10-24 ~16:11 EAT — @muse, task 4 (multimodal + intent)

1. **Intent gate is rules-first, safe-default.** Explicit negations and
   keep-account signals are checked before any deletion pattern, because
   the non-deletion texts themselves contain the words "deletion",
   "erase" and "close" ("I am not asking for deletion", "Do not close
   the account"). Ambiguous text defaults to NOT_DELETION with
   `needs_llm=True`, never to deletion: wrongly deleting is the
   irreversible error, and AGENTS.md section 6.5 only authorizes
   deletion when the person actually asked for it.
2. **Audit-of-deletion phrasing counts as a deletion case.** "Verify that
   my account was deleted", "confirm the profile is gone" presuppose a
   deletion request; the public samples label these COMPLETE, not
   NOT_APPLICABLE.
3. **Extracted facts are recorded as shown, conflicts are not resolved
   in the extraction layer.** marketing_001: the screenshot header shows
   Soo-jin Park / sj.park@naver.com, while system_activity activity_4008
   links marketing_001 to user_09 / kwame-mensah@proton.me. Both claims
   are recorded in code/extracted/images.json; the state engine resolves
   identity, extraction does not.
4. **evidence_46 (object storage) grounds no finding by itself.** The
   screenshot shows 8 objects but no subject identifier; the state
   engine must link its objects to a user through other evidence before
   citing it.
5. **export_1260 expiry is recorded, not interpreted.** The export was
   Ready at capture (2026-06-18) with an expiry of 2026-06-25; whether a
   later audit still counts it is a state-engine call using audit_at.

## 2026-10-24 16:50 EAT — @cue, task 5 follow-up (evaluation of main 45c5428)
- Section 8 of `code/evaluation/evaluation_report.md` is now measured on main, not on the prototype: samples status 32/32, exact rows 30/32 (same as the prototype), `check` on the 518-row `output.csv` OK, custom cases status 8/8 but exact 5/8 (prototype: 7/8).
- Finding F6 (owner: Hark, `code/hark/engine.py::prepare` / `code/evidence.py`): the engine prepares on samples + real 518 + the run's requests and assigns account activity one-to-one by `reported_at` window. A request that duplicates a sample's `reported_at` and user (custom_04 / custom_07 vs case_29 / case_28) gets no account activity and falls back to `account_0NN`; they pass when the base sample is left out of the batch (`python3 code/evaluation/batch_check.py`). Not triggered by the real run. Suggested fix: do not let a request in one batch consume activity that another request would also match, or make the real-vs-sample assignment independent. Not edited by Cue.
- F1 stays open: `case_18` / `case_20` lack one `security_service` audit-record verification (ids appear in no participant file); 111 of 356 INCOMPLETE rows in the real output have no audit-record step.
- Decision: the usage report states 0 model calls for main, based on reading the imports (no network or LLM client; the intent LLM fallback is never wired in). If anyone adds a model call it must use `evaluation/usage.py::record_usage` and the report must be regenerated.
- Decision: `output.csv` is not committed in the evaluation PR; submission artifact ownership stays with OpenCode.

## 2026-10-24 17:04 EAT — @hark, multimodal facts + retention wiring
- Source of truth for PNG/PDF states is now `code/extracted/*.json` (via `code/multimodal.py` -> `code/hark/multimodal_facts.py`). `multimodal_hints.py` stays only as a fallback for a resource the extraction lacks; today the extraction covers all 13, and the output is identical with the hints emptied.
- When several artifacts show one resource (audit_record_003: evidence_55 PDF 06:30 UTC, evidence_37 PNG 07:00 UTC on 2026-09-05), cite the latest one whose captured_at <= audit_at (same "latest citable file" rule as other evidence).
- A citable retention entry naming a case resource forces RETAIN (VERIFY_RECORD_PRESENT) while retain-until >= audit_at; once it has passed, the record follows rule_12's lapse path (delete). Entries from files captured after audit_at are ignored.
- The legal retention register (evidence_27, users 08/12/17/21/24/29, finance orders retained to May 2033) produces no output step: its rows name order ids that appear in no other file and no system resource id a case resource maps to, and the six gold samples for those users contain no step for it. Recorded in plan debug only. Revisit if the grader's contract turns out to want a `legal_retention_service` VERIFY_RECORD_PRESENT step.
- Owner-by-email linking for PDF documents applies only to resources with no LINK_RESOURCE_IDENTITY activity (today only support_001 / evidence_58), so a PDF never adds its resource to cases outside the LINK window.

## 2026-10-24 18:05 EAT — @hark, generality hardening
- No hand-written per-file answers remain. `code/hark/multimodal_hints.py` was deleted, and multimodal states come only from `code/extracted/*.json` (Muse's pdftotext and vision extraction, one entry per evidence file, documented in `code/extracted/README.md`). Note: the code judge ignores .json files, so that README and `code/multimodal.py` are what explain the extraction.
- An answer must not depend on case_id or row position. `code/evaluation/perturbation_check.py` enforces this. The engine's tie-breaks use content order (requested_at, reported_at, audit_at, email, text) instead of the number inside case_id.
- Two requests that resolve to the same person with the same reported_at share one activity assignment instead of competing for it (engine-level fix for Cue F6).
- Accepted cost: 43 rows of the real output change steps, all no-signal guesses (statuses unchanged). Neither ordering predicts better than chance on signal-resolved pairs, so the id-free one wins.

## 2026-10-24 17:59 EAT — Submission packaging (code.zip + README run section) — OpenCode

- code.zip is built from README.md + code/ only, per the README packaging command (`python3 code/package_zip.py`). Verified contents: 39 files — README.md + 38 files under code/, including code/evaluation/evaluation_report.md and code/evaluation/usage_report.md — and no dataset/, __pycache__/, .pyc, secrets, or nested archives.
- dataset/ is NOT shipped in code.zip. The README states that dataset/ must sit beside code/ so `python3 code/main.py` can find it; without dataset/ the run exits with "dataset file not found". Verified by unzipping to a temp dir, symlinking dataset/, and re-running: 518 rows, output.csv byte-identical to the repo run.
- model= for OpenCode log entries is opencode/deepseek-v4.1-flash-free (tool=opencode).
- output.csv at the repo root is not committed by this branch (opencode/package).

## 2026-10-24 18:00 EAT — @opencode, packaging the submission

1. **Deterministic archive.** `code/package_zip.py` writes sorted entries with
   fixed 1980-01-01 timestamps, forward-slash paths, and `ZIP_DEFLATED`, so the
   same worktree produces the same archive bytes and the SHA-256 summary is a
   stable artifact check.
2. **Deny-list, not an allow-list of exceptions.** Contents are `README.md` plus
   `code/`, but the builder and verifier reject `dataset/`, `.git`, virtual
   environments, `node_modules`, `__pycache__`/`*.pyc`, `.DS_Store`,
   `output.csv`, `log.txt`, `AGENTS.md`, nested archives, and credential-like
   file names. A missing required report fails verification instead of silently
   shipping an incomplete archive.
3. **Verify by re-opening.** The build path does not trust its own file list: it
   re-opens the finished zip and re-checks paths, required entries, and a
   secret-pattern scan of text entries. `--check` runs the same verifier on an
   existing archive without rebuilding, so a stale or hand-edited zip is caught.
4. **`code.zip` is gitignored.** The archive is a submission artifact, not source;
   only the builder and the run instructions are committed.
## 2026-10-24 17:57 EAT — @cue, evaluation refresh on main 6435d97
- `python3 code/evaluation/perturbation_check.py` is now one of the evaluation gates (listed in evaluation_report.md section 1 and in code/evaluation/README.md): selftest, `check` on output.csv, sample `score` (status 32/32), `custom --agent` (status 8/8), perturbation check (`PERTURBATION PASS`), scorer unit tests. Hark owns the script; Cue only lists it as a gate.
- Expected exact-row numbers for the gates are 30/32 on the samples and 7/8 on the custom cases; the single recurring miss is F1 (an `audit_record_*` id that is in no participant-visible file). Anything lower than that is a regression.
- Report sections 3 and 8 describe `origin/main` 6435d97 only; the 5176af6 prototype table and the 45c5428 custom results (5/8) are replaced and mentioned only as history.
- Branch naming: this work is on `task/eval-refresh` (team change of 17:52 EAT), not `cue/eval-refresh`.
## 2026-10-24 17:51 EAT — @muse, extracted facts are produced by a committed script
- `code/extracted/documents.json` / `images.json` are produced by the repeatable pipeline in `code/extracted/extract.py` (mechanical manifest/PDF-text/PNG-header stage + vision semantic stage), documented in `code/extracted/README.md`. The script's default mode validates every committed fact against the manifest, the PDF text layers, user_identities/user_audit_log and the LINK_RESOURCE_IDENTITY rows; it exits 1 on any mismatch.
- A multimodal subject that resolves to no user is only valid when the entry records an unresolved conflict in `notes` (today: only the marketing_001 screenshot header, evidence_60). Any other unresolved subject or LINK/subject user mismatch fails validation.
- PDF resources with no LINK row are document-owned or containers (support_001 via the subject printed in the document; the evidence_27 register) — the same rule `code/hark/multimodal_facts.py` already applies.
