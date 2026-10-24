# Evaluation report: Ghost Data deletion-audit agent

How we measure the agent, what the scorer checks, what the first reference runs showed,
and which decisions are still open. Everything here is reproducible from this repo with
the commands below; no network access and no model calls are needed.

## 1. Run it

```bash
# score any prediction file against the 32 solved samples (case, row, step, field, sequence level)
python3 code/evaluation/main.py score --pred <your_sample_predictions.csv>

# validate the real 518-row submission (no gold exists for it): format, order, evidence rules
python3 code/evaluation/main.py check --pred output.csv

# custom regression cases (builds code/evaluation/custom/*.csv; add --agent to run an agent on them)
python3 code/evaluation/main.py custom
python3 code/evaluation/main.py custom --agent "python3 code/main.py --requests {requests} --out {out}"

# generality: rename / shuffle / upper-case the 518 requests, rerun, require identical answers
python3 code/evaluation/perturbation_check.py

# token / cost report from code/evaluation/usage_log.jsonl
python3 code/evaluation/main.py usage

# unit tests of the scorer itself (28 tests, < 1 s)
python3 -m unittest discover -s code/evaluation -p "test_*.py"
```

**Evaluation gates (run all of them before submitting; every one must pass):**

| # | Command | Pass condition |
| --- | --- | --- |
| 1 | `python3 code/main.py --selftest` | `SELFTEST PASS` |
| 2 | `python3 code/main.py` then `python3 code/evaluation/main.py check --pred output.csv` | 518 rows, `RESULT: OK` |
| 3 | `python3 code/evaluation/main.py score --pred <samples_pred.csv> --min-status-acc 1.0` | status 32/32 (exact rows 30/32 expected, F1 is known) |
| 4 | `python3 code/evaluation/main.py custom --agent "python3 code/main.py --requests {requests} --out {out}"` | status 8/8 (exact 7/8 expected) |
| 5 | `python3 code/evaluation/perturbation_check.py` | `PERTURBATION PASS` (0 status changes, 0 rows changed) |
| 6 | `python3 -m unittest discover -s code/evaluation -p "test_*.py"` | all tests OK |

Useful flags for `score`: `--failures 20` (print more mismatching cases),
`--json out.json` (everything incl. per-case results), `--min-status-acc 0.95` (exit code 1
below the threshold, for CI), `--update-report code/evaluation/evaluation_report.md`
(rewrites section 8 below), `--subset-ok` (score only cases present in both files).
Note that `dataset/sample_output.csv` only covers `case_01..case_32`, so the agent has to
be run on `dataset/sample_deletion_requests.csv` to produce a comparable file; scoring the
518-row `output.csv` against the sample gold would print a warning and score zero.

## 2. What is measured

| Level | Question | Metrics |
| --- | --- | --- |
| Case | Is the verdict right? | `deletion_status` accuracy, per-class precision / recall / F1 (COMPLETE, INCOMPLETE, NOT_APPLICABLE), macro-F1, confusion matrix |
| Row | Is the whole answer right? | Exact-row accuracy (status and every step incl. `step_no` and `evidence_id`), the same ignoring `evidence_id`, mean per-case step F1 |
| Step | Are the right actions present? | Micro precision / recall / F1 over steps (identity = phase, system, resource, operation, state) with and without `evidence_id`; per-operation TP / FP / FN |
| Field | Which attribute is wrong? | Accuracy of each of the 7 step fields over gold steps after aligning predicted to gold steps (so one wrong resource does not zero the whole step) |
| Sequence | Is the order right? | Exact ordered match, longest-common-subsequence ratio, pairwise order concordance, and compliance of the predicted rows with the ordering rule itself |

Design choices worth knowing:

* **Steps are compared as multisets**, so a missing step lowers recall and an extra step
  lowers precision; duplicates are not free. Cases where both gold and prediction are `[]`
  count as a perfect step F1 (otherwise `NOT_APPLICABLE` cases would be 0/0).
* **Order is scored separately from content.** Swapping two steps leaves step F1 at 1.0 but
  drops the sequence metrics, so a sorting bug cannot hide behind a good set of steps.
* **Missing predicted rows count as wrong** (they are not silently skipped) unless
  `--subset-ok` is set. Unparseable `steps_json` is counted and reported, never a crash.
* **Evidence is tracked but separable.** `evidence_id` is part of the strict row and step
  scores because the statement asks for it, and the "ignoring evidence_id" columns show how
  much of an error is only an evidence-citation error.

### Contract checks (`validate_row`, used by both `score` and `check`)

These need no gold and run on every predicted row. Each check was first run on the 32
sample gold rows (all pass), so they are not stricter than the real data:

1. `deletion_status` is one of the three values; `NOT_APPLICABLE` has `[]`; `COMPLETE`
   has only VERIFICATION steps; `INCOMPLETE` has at least one FINDING.
2. Each step has exactly the 7 keys, `step_no` runs 1..n, `phase` / `state` match the
   operation table, FINDING and VERIFICATION steps cite an `evidence_id`, REMEDIATION
   steps in an `INCOMPLETE` answer leave it empty (as in all 22 samples).
3. Steps are ordered by phase, operation-table rank, `system_id`, `resource_id`
   (numeric or lexical `resource_id` order are both accepted, see section 7).
4. With the dataset present: every cited `evidence_id` exists in `evidence_files.csv` and
   was captured **at or before the case's `audit_at`** (no time travel).
5. For the whole file (`check`): exact header, one row per request in
   `dataset/deletion_requests.csv` (518), no duplicates, no extras, request order.

## 3. Reference results (as of repo state `origin/main` 6435d97)
Reproduced on 2026-10-24 (EAT) from a fresh worktree of `origin/main`. All numbers come from
commands in section 1. No model or network call is involved. The 32 samples have gold; the 518
real rows have none, so they only get structural and consistency checks.

| Run | Status acc | Exact rows | Step P / R / F1 | Sequence exact | Notes |
| --- | ---: | ---: | --- | ---: | --- |
| Gold vs gold (scorer self-check) | 1.0000 | 32/32 | 1.00 / 1.00 / 1.00 | 1.0000 | proves the scorer has no bias |
| `main` (6435d97), 32 samples | 1.0000 (32/32) | 30/32 (0.9375) | 1.0000 / 0.9896 / 0.9948 | 0.9375 | 2 misses, the known F1 (below) |
| `main` (6435d97), 8 custom cases | 1.0000 (8/8) | 7/8 (0.8750) | 1.0000 / 0.9773 / 0.9885 | 0.8750 | 1 miss: custom_06 (F1) |
| `main` (6435d97), 518-row `check` | n/a | n/a | n/a | n/a | OK, ready to submit; 356 INCOMPLETE / 152 COMPLETE / 10 NOT_APPLICABLE |
| `main` (6435d97), perturbation check | n/a | n/a | n/a | n/a | PASS: shuffle, rename, shuffle+rename, e-mail case all give 0 status and 0 row changes |

Movement since the 45c5428 reference (section 8 of the previous revision): samples unchanged
(32/32 status, 30/32 exact); custom cases 5/8 -> **7/8** exact (custom_04 and custom_07 now pass
because Hark's `engine.prepare` is content-ordered and shares one assignment between requests
from the same person for the same reported event, which fixed Cue finding F6); the 518-row
output keeps all 518 statuses and changes the steps of 43 rows, all no-signal guesses (checked
by diffing the 45c5428 and 6435d97 outputs: 0 status changes, 43 `steps_json` changes).

**The two sample misses** (`case_18`, `case_20`) are the same kind as before: the gold row
contains a `VERIFY_RECORD_PRESENT` for a `security_service` `audit_record_*` that appears in no
participant-visible file, so no evidence-based engine can reproduce it, only a heuristic
("every user has one audit record"). The scorer reports each as a *missing step*, nothing else.
The earlier prototype reference (5176af6, 30/32 and 7/8 on the samples and custom cases) is
superseded: main now matches it on both sets without hand-read multimodal hints.

## 4. How to read a failure

`render_failures` prints, for each non-exact case, the status result and the concrete
`missing:` and `extra:` steps (and `evidence:` mismatches when only citations differ). A
typical triage order is:

1. status wrong -> intent or finding detection (the entire answer is at risk);
2. missing / extra steps with the right status -> evidence interpretation for that resource;
3. only `evidence_id` differs -> the wrong snapshot file was cited (check `captured_at`
   and `audit_at`);
4. only order differs -> sorting rule (section 7).

## 5. Custom test cases (`code/evaluation/custom_cases.py`)

Eight metamorphic variants of solved samples, so the expected answer comes from the
published gold and nothing is hand-labelled. Files: `custom/custom_requests.csv` (same
columns as the dataset) and `custom/custom_gold.csv`.

| Case | Base | Change | Expected | Why it matters |
| --- | --- | --- | --- | --- |
| custom_01 | case_11 | text becomes an information-only request | `NOT_APPLICABLE`, `[]` | 14-step INCOMPLETE evidence must not override intent |
| custom_02 | case_15 | text becomes "remove me from marketing lists only" | `NOT_APPLICABLE`, `[]` | deletion words inside a limited scope |
| custom_03 | case_26 | text becomes a profile-correction request | `NOT_APPLICABLE`, `[]` | no verification steps for a non-deletion request, even on a clean subject |
| custom_04 | case_29 | historical email replaced by the user's current email | same as case_29 | identity resolution must reach the same records |
| custom_05 | case_05 | email upper-cased | same as case_05 | 15 SQL snapshots store emails in upper case |
| custom_06 | case_18 | `reported_status` COMPLETE -> NOT_STARTED | same as case_18 | the workflow claim must not move the audit |
| custom_07 | case_28 | `reported_status` NOT_STARTED -> PARTIALLY_COMPLETE | same as case_28 | a claim must not turn COMPLETE into INCOMPLETE |
| custom_08 | case_16 | request text rewritten in Korean | same as case_16 | multilingual intent detection |

The first three are the `NOT_APPLICABLE` regressions that the 32 samples cannot cover (the
samples contain none, while the real file has 10: `case_533..542`). Because each case comes
from a different sample, no two share a `reported_at` timestamp, which matters for engines
that link system activity to a request through that time.

**Result on Hark's prototype (5176af6; main at 6435d97 is in section 8.2: 7/8 exact, status 8/8):** 7/8 exact, status 8/8. The only miss is `custom_06`, the
same unrecoverable audit record as in `case_18`. The cases passed only when the engine
planned the custom requests together with the real 518; mixing them with the 32 samples
in one batch made `custom_04`, `custom_06` and `custom_07` fail with a wrong account id,
because the base samples share `reported_at` with their variants and the engine's
activity-to-case assignment is global. This is a robustness note for whoever owns the
linking logic: results must not depend on which other requests are in the same run.

## 6. Failures found and improvements made

Found while building and exercising the evaluation (owner in brackets):

| # | Failure found | Evidence | Status / improvement |
| --- | --- | --- | --- |
| F1 | Two sample rows (`case_18`, `case_20`) lack the `security_service` audit-record verification | `VERIFY_RECORD_PRESENT` on `audit_record_6645` / `audit_record_5569` exists in no participant-visible file | Open [Hark / OpenCode]: 24 of 32 samples have this step and these 2 are the only ones the prototype cannot reproduce from evidence; decide whether a rule-based fallback is justified. The scorer shows it as a single missing step, so the effect is visible and small |
| F2 | The prototype's result for a request depends on which other requests are in the same batch | `custom_04`, `custom_06`, `custom_07` returned wrong account ids when planned together with the 32 samples (they share `reported_at` with their base case) and were correct (apart from F1) when planned with the 518 only | Open [Hark]: make the activity-to-case assignment a function of the request alone, or handle `reported_at` collisions explicitly; custom cases now guard this |
| F3 | Non-deletion intent is easy to miss | 10 real requests (`case_533..542`) are NOT_APPLICABLE, 0 samples are; 6 of them contain deletion words inside a negation or limited scope | Custom cases `custom_01..03` added; the prototype handles them (3/3) [Muse owns the classifier] |
| F4 | Ordering rule has two readings | numeric vs string `resource_id`, natural vs string case order | Scorer accepts both for validation and prints a note; decision requested (section 7) |
| F5 | Hand-made starter cases (`case_01..08, 10`) behave differently from generated ones | no `security_service` step, older evidence, 3-digit ids | Documented in `sample_patterns.md`; a rule applied blindly to both families would break one of them |

Improvements made to the evaluation tooling itself: strict and relaxed (ignoring
`evidence_id`) variants of every score so evidence-citation errors are separable from
planning errors; field accuracy is computed after step alignment so one wrong resource
does not zero a whole step; a gold-free `check` for the 518-row file (the only
out-of-sample test available); metamorphic custom cases instead of hand labels; and 28
unit tests, including a test that every one of the 32 gold rows passes the contract
checks (so a validator bug cannot silently fail good output). Improvements to the agent
itself (F1-F3) are listed in the table as open items; update the Status column when
they land and re-run section 8.

## 7. Open decisions and limits

* **Resource ordering is ambiguous.** "Ascending `resource_id`" could mean string order
  (`search_1007` < `search_898`) or numeric order. No sample has two steps with the same
  operation and system, so the gold cannot settle it. The scorer accepts both when
  validating; `check` prints a note. The team should choose one and record it in
  DECISIONS.md. Case order has the same ambiguity: `deletion_requests.csv` is in natural
  order (`case_33` before `case_100`); `check` accepts it and says so.
* **The hidden grader is unknown.** Our metrics are our own; the official score may weight
  things differently (for example exact-row only). That is why the report keeps both
  strict and relaxed variants.
* **No gold for the 518 rows.** Only structural and evidence-consistency checks (section
  2) can be run there, plus agreement between independently built engines
  (`score` accepts any file as gold, for example `--gold other_engine.csv`).
* **Overfitting risk.** Every rule derived from the 32 samples is validated on the same 32.
  Use the custom cases and the 518-row `check` as the out-of-sample guard.
* **Model call accounting** lives in `usage.py`; see `usage_report.md`.

## 8. Latest scored run

The block between the markers is rewritten by
`python3 code/evaluation/main.py score --pred <file> --update-report code/evaluation/evaluation_report.md`.
Re-run it on the final sample predictions before submitting.

<!-- METRICS:START -->
### Latest run: main @ 6435d97 (python3 code/main.py) on the 32 samples

Scored 32 case(s) (gold 32, predicted rows 32, missing 0, extra 0, unparseable 0, rows with contract issues 0).

| Level | Metric | Value |
| --- | --- | --- |
| Case | Status accuracy | 1.0000 (32/32) |
| Case | Status macro-F1 | 1.0000 |
| Row | Exact-row accuracy (status + all steps incl. evidence_id) | 0.9375 (30/32) |
| Row | Exact-row accuracy ignoring evidence_id | 0.9375 |
| Row | Mean per-case step F1 | 0.9968 |
| Step | Micro precision / recall / F1 (phase, system, resource, operation, state) | 1.0000 / 0.9896 / 0.9948 |
| Step | Micro F1 including evidence_id | 0.9948 |
| Field | Mean accuracy over all 7 step fields (gold steps, aligned) | 0.9867 |
| Field | Mean accuracy over the 5 identity fields | 0.9896 |
| Sequence | Exact ordered sequence (identity keys) | 0.9375 |
| Sequence | Exact ordered sequence incl. evidence_id | 0.9375 |
| Sequence | Mean LCS ratio | 0.9968 |
| Sequence | Pairwise order concordance | 1.0000 |
| Sequence | Canonical-order compliance of predicted rows | 1.0000 |

**Per-class status metrics**

| Class | Support | Precision | Recall | F1 |
| --- | --- | --- | --- | --- |
| COMPLETE | 10 | 1.0000 | 1.0000 | 1.0000 |
| INCOMPLETE | 22 | 1.0000 | 1.0000 | 1.0000 |
| NOT_APPLICABLE | 0 | 0.0000 | 0.0000 | 0.0000 |

**Confusion (gold->pred)**: COMPLETE->COMPLETE: 10, INCOMPLETE->INCOMPLETE: 22

**Field-level accuracy** (alignment rate 0.9896: 191 of 193 gold steps matched to a predicted step)

| step_no | phase | system_id | resource_id | operation | state | evidence_id |
| --- | --- | --- | --- | --- | --- | --- |
| 0.9689 | 0.9896 | 0.9896 | 0.9896 | 0.9896 | 0.9896 | 0.9896 |

**Step precision/recall by operation**

| Operation | TP | FP | FN | Precision | Recall | F1 |
| --- | --- | --- | --- | --- | --- | --- |
| REPORT_REMAINING_RECORD | 21 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| REPORT_EXPOSED_PII | 11 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| REPORT_ACTIVE_DATA_FLOW | 6 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| REPORT_RESTORE_RISK | 3 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| DISABLE_DATA_FLOW | 6 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| BLOCK_RESTORE | 3 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| DELETE_RECORD | 21 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| REDACT_RECORD | 6 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| ANONYMIZE_RECORD | 5 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| VERIFY_RECORD_ABSENT | 50 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| VERIFY_RECORD_PRESENT | 22 | 0 | 2 | 1.0000 | 0.9167 | 0.9565 |
| VERIFY_RECORD_REDACTED | 10 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| VERIFY_RECORD_ANONYMIZED | 12 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| VERIFY_DATA_FLOW_DISABLED | 9 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| VERIFY_RESTORE_BLOCKED | 6 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |

```text
2 of 32 scored cases are not an exact match.

- case_18: gold=INCOMPLETE pred=INCOMPLETE status_ok=True sequence_exact=False
    missing: VERIFICATION security_service audit_record_6645 VERIFY_RECORD_PRESENT PRESENT

- case_20: gold=INCOMPLETE pred=INCOMPLETE status_ok=True sequence_exact=False
    missing: VERIFICATION security_service audit_record_5569 VERIFY_RECORD_PRESENT PRESENT
```
<!-- METRICS:END -->

### 8.1 Real run on main (`python3 code/main.py`, commit 6435d97)
Run on 2026-10-24 (EAT) in a fresh worktree of `origin/main` (branch `task/eval-refresh`); no
model or network calls, 3.4 s wall time, `output.csv` byte-identical across two runs
(md5 ac01f7a4be3f9d2ff703ec1aee28c835).

| Check | Result |
| --- | --- |
| `python3 code/main.py` | 518 rows written to `output.csv`; plug-ins `classifier=installed evidence=installed`, 0 validation/plug-in problems |
| `python3 code/main.py --selftest` | PASS |
| `python3 code/main.py --check output.csv` | 0 problems |
| `python3 code/evaluation/main.py check --pred output.csv` | **OK, ready to submit**: 518 rows, natural case order, all evidence ids exist and predate `audit_at` |
| `python3 code/evaluation/perturbation_check.py` | **PERTURBATION PASS**: shuffle / rename / shuffle+rename / e-mail case: 0 status changes, 0 rows changed each |
| Status counts (518) | COMPLETE 152, INCOMPLETE 356, NOT_APPLICABLE 10 (the same 10 the intent classifier flags: 508 deletion / 10 not) |
| Steps (518) | 3,247 in total; COMPLETE rows average 3.4 steps, INCOMPLETE rows 7.7 (max 14); `[]` only on the 10 NOT_APPLICABLE rows |
| Unit tests | 28 scorer tests OK; 4 `code/hark` tests OK; intent tests PASS (32 samples + 518 requests) |
| `python3 code/evaluation/batch_check.py` | custom_04 and custom_07 PASS in all three batch compositions; custom_06 differs only by the F1 step |

Only the 32 samples have gold. The 518-row result has no accuracy number; `check` proves the
file is well-formed, ordered and time-consistent, and the perturbation check proves it does not
depend on case ids, row order or e-mail case. Nothing more.

**Samples (section above):** status accuracy 32/32, exact rows 30/32 (0.9375), step F1 0.9948.
The two misses are the known F1 rows, each lacking one `security_service` verification
(`case_18` `audit_record_6645`, `case_20` `audit_record_5569`). Both ids appear in no
participant-visible file, so the engine cannot derive them. The merges of PRs #5 to #10
(canonical resource ids, adapter fix, retention wiring, generality hardening) changed nothing on
the samples.

**Gold-free signal on the real file (audit-record step).** In the gold samples 17 of 22
INCOMPLETE rows (77%) carry a `VERIFY_RECORD_PRESENT` on an `audit_record_*`; on the real file
245 of 356 (69%) do, and 151 of 152 COMPLETE rows do (gold: 7 of 10). Unchanged from 45c5428.
The 111 INCOMPLETE rows without it are where a missing step of the same type as `case_18` /
`case_20` would sit if the record is not discoverable from the supplied files. This is not proof
of an error (the gold samples also have 5 such INCOMPLETE rows), just where one would be.

### 8.2 Custom cases on main (`custom --agent "python3 code/main.py --requests {requests} --out {out}"`)
Status 8/8 correct. Exact rows **7/8** (0.8750), step micro P / R / F1 1.0000 / 0.9773 / 0.9885,
mean per-case step F1 0.9954. Previous run (45c5428): 5/8 exact, F1 0.9176.

| Case | Base | Result | Detail |
| --- | --- | --- | --- |
| custom_01, 02, 03 | case_11, 15, 26 | PASS | `NOT_APPLICABLE` with `[]` (information request, marketing-only, profile correction) |
| custom_04 | case_29 | PASS (was FAIL) | account step is now `account_7012` |
| custom_05 | case_05 | PASS | upper-cased email |
| custom_07 | case_28 | PASS (was FAIL) | `account_5482` and the `analytics_6077` `VERIFY_RECORD_ANONYMIZED` step present |
| custom_08 | case_16 | PASS | Korean request text |
| custom_06 | case_18 | **FAIL** | status right; the `audit_record_6645` step is missing (the known F1). The `account_5521` verification, missing at 45c5428, is now present |

Reproduce the batch dependence with `python3 code/evaluation/batch_check.py`:

| Batch | custom_04 | custom_06 | custom_07 |
| --- | --- | --- | --- |
| A. samples + real 518 + custom (what main does) | PASS | DIFF (F1 only) | PASS |
| B. samples minus the 3 base cases + real 518 + custom | PASS | DIFF (F1 only) | PASS |
| C. real 518 + custom | PASS | DIFF (F1 only) | PASS |

At 45c5428 batch A failed all three; F6 (the answer depends on which requests are planned
together) is fixed for this case family. Two requests that resolve to the same person and the
same reported event now share one assignment. Requests of different people that collide on
`reported_at` (for example `case_18`, `case_20`, `case_27`, `case_28`, `case_29`, `case_51`,
`case_539`) are not covered by these 8 cases; the perturbation check covers ordering and naming
but not new colliding people.

### 8.3 Usage
No model calls anywhere in the main pipeline (no network or LLM client imported, re-checked at
6435d97; the intent classifier's optional `llm_call` is not supplied; `code/multimodal.py` and
`code/hark/multimodal_facts.py` read pre-extracted `code/extracted/*.json`): 0 input tokens, 0
output tokens, estimated cost $0. `usage_log.jsonl` does not exist. See `usage_report.md`
(current for 6435d97).

### 8.4 Failures and open items from this run
1. **F1 (open, Hark/OpenCode):** `case_18` and `case_20` miss one `security_service` audit-record
   verification each (custom_06 inherits it); 111 of 356 INCOMPLETE rows in `output.csv` have no
   such step.
2. **F6 (fixed in 6435d97):** batch dependence in the custom case family. No new failure.
3. **Observation (not a failure):** 43 of 518 rows changed steps with the id-free ordering; they
   are no-signal guesses, so the change cannot be scored without gold. It is the accepted cost
   recorded in DECISIONS.md (Hark, 18:05 EAT).
4. Nothing else failed: end-to-end run, selftest, `check`, perturbation check, intent tests,
   hark tests and scorer tests are green.

## 9. Generality check (`code/evaluation/perturbation_check.py`)

The hidden grader scores cases we cannot see, so an answer must come from a request's content and the
evidence, never from its `case_id` or its row position. The check rewrites `dataset/deletion_requests.csv`
four ways, reruns the full agent (`code/main.py --requests ...`) on each copy, maps rows back to the
original id, and requires `deletion_status` and `steps_json` to be byte-identical to the baseline:

| Perturbation | What changes | First run (before fix) | After fix |
| --- | --- | --- | --- |
| shuffle | row order | 2 rows changed (steps only) | 0 |
| rename | every case_id -> random `req_NNNNNN` | 22 rows changed | 0 |
| shuffle+rename | both | 25 rows changed | 0 |
| email_case | e-mail upper-cased | 0 | 0 |

Status never changed in any run. All diffs were in the engine's last-resort assignment (account
`DELETE` / un-owned follow-up activity with no ownership or fingerprint signal). It broke ties by the number
inside `case_id` and by input order. Fix (Hark, `code/hark/engine.py::prepare`):

- Cases are processed in content order: requested_at, reported_at, audit_at, email, then text. The
  `guess` and `cluster-guess` tie-breaks use the same key.
- Two requests from the same person for the same reported event (for example re-sent from another of
  their e-mails) share one assignment instead of competing for the same activities. This also fixes
  Cue finding F6 at the engine level: custom_04 and custom_07 pass with the base sample in the batch.

Effect on the 518-row output: 43 rows change their steps, every one of them a no-signal guess, and
0 statuses change. Measured on the cases resolved with real signal (owner / fingerprint, 385 pairs that
share a window), neither tie-break predicts the account order better than chance: case-number order
agrees with activity time 184/385, request-time order 201/385. So the new order costs nothing and removes
the dependence on ids. Gates after the fix: selftest PASS, check 0 problems, samples status 32/32 and exact
30/32, custom 7/8 (custom_06 = the known missing audit_record_6645), perturbation PASS.


## 10. Case-to-activity matching: method, coverage and limits

Most `system_activity.csv` rows written by the deletion worker (for example "Deletion worker submitted
primary profile account_X", "Policy action completed for analytics_Y") carry **no name, email, phone or
user_id**. To know which request an account or follow-up activity belongs to, the engine
(`code/hark/engine.py::Engine.prepare`, `code/hark/windows.py`) uses this order, strongest first:

1. **Evidence (owner).** A SQL row, JSON record, handover line or log line that names one of the subject's
   identifiers (current or historical email, phone, email hash, user_id) links the resource directly.
   This always wins.
2. **Time-proximity fingerprint (fallback).** Only when no evidence links an account activity, it is matched
   to the request whose `reported_at` window contains it, with a seconds-offset band between the account
   DELETE and its follow-up activities used to separate requests that share a window.
3. **Elimination.** Only one unclaimed candidate is left.
4. **Deterministic guess.** No signal at all. The tie-break is content-ordered (section 9), so it does not
   depend on case_id or row order.

Coverage on the 518-row run (main, rerun 2026-10-24 22:25 EAT):

| How the account activity was linked | Requests |
| --- | ---: |
| Evidence (owner) | 238 |
| Time-proximity fingerprint (with evidence-owned follow-ups) | 179 |
| Time-proximity fingerprint (un-owned follow-ups only) | 11 |
| Elimination | 30 |
| Guess with 1 free candidate | 18 |
| Guess among 2 to 3 candidates | 20 |
| No account activity to link (non-deletion and other cases) | 22 |

**Limits, stated plainly:**
* The time windows and the seconds-offset bands were **observed on the 32 solved samples**. They are not a
  documented property of the system, so the hidden-set accuracy of the 190 fingerprint matches and the 38
  guesses is likely lower than the 30/32 sample score suggests.
* Ablation: removing only the seconds-offset check (keeping the windows) drops the samples from 30/32 to
  20/32 exact (step F1 0.995 -> 0.953) and changes 166 of 518 rows. The check carries real signal, and no
  evidence-based replacement exists in the data, so it is kept as a labelled fallback rather than removed.
* For low-numbered users (user_01..30) with no linked account, the account id `account_0NN` is inferred
  from the naming convention seen in all 10 such samples (`engine.py`, naming-convention fallback).
