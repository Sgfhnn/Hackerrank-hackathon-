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

# token / cost report from code/evaluation/usage_log.jsonl
python3 code/evaluation/main.py usage

# unit tests of the scorer itself (28 tests, < 1 s)
python3 -m unittest discover -s code/evaluation -p "test_*.py"
```

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

## 3. Reference results (as of repo state `origin/main` 3af4a4e)

There is no end-to-end `code/main.py` output yet, so these runs use Hark's prototype
(`origin/hark/evidence-engine` 5176af6) purely as a sanity reference for the scorer. They
do not measure the final system, and its rules were derived from the same 32 samples, so
they are an optimistic upper bound.

| Run | Status acc | Exact rows | Step P / R / F1 | Sequence exact | Notes |
| --- | ---: | ---: | --- | ---: | --- |
| Gold vs gold (scorer self-check) | 1.0000 | 32/32 | 1.00 / 1.00 / 1.00 | 1.0000 | proves the scorer has no bias |
| Hark prototype, 32 samples | 1.0000 | 30/32 | 1.00 / 0.99 / 0.995 | 0.9375 | 2 misses, see below |
| Hark prototype, 8 custom cases | 1.0000 | 7/8 | see section 5 | 0.875 | same single miss |
| Hark prototype, 518-row `check` | n/a | n/a | n/a | n/a | structure and evidence rules OK; 356 INCOMPLETE / 152 COMPLETE / 10 NOT_APPLICABLE |

(The briefing quoted status 0.97 / step F1 0.91 for an earlier prototype; the current
branch scores higher, which is consistent with the later commits.)

**The two misses** (`case_18`, `case_20`) are the same kind: the gold row contains a
`VERIFY_RECORD_PRESENT` for a `security_service` `audit_record_*` that appears in no
participant-visible file, so no evidence-based engine can reproduce it, only a heuristic
("every user has one audit record"). The scorer reports each as a *missing step*, nothing
else: status, order and all other fields are right (field accuracy 0.987 over all 7 fields).
This is a useful example of why step-level and row-level numbers are both reported:
status accuracy is 100%, but the answer is not an exact match.

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

**Result on Hark's prototype:** 7/8 exact, status 8/8. The only miss is `custom_06`, the
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
### Latest run: Hark prototype (5176af6) on the 32 samples, reference only

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
