# code/evaluation: scorer, validator, custom cases, usage accounting

Standard library only (Python 3.8+), deterministic, no network. Task 5 of the team plan.

| File | Purpose |
| --- | --- |
| `main.py` | CLI: `score`, `check`, `usage`, `custom` |
| `scorer.py` | metrics (case / row / step / field / sequence), row and file validators |
| `custom_cases.py` | 8 metamorphic evaluation cases; builds `custom/custom_requests.csv` + `custom/custom_gold.csv` |
| `usage.py` | `record_usage(...)` for every model call; builds `usage_report.md` |
| `batch_check.py` | diagnostic: plans custom_04/06/07 in 3 batch compositions to show the engine's dependence on which requests are planned together |
| `analyze_samples.py` | prints the pattern statistics behind `sample_patterns.md` |
| `test_scorer.py` | 28 unit tests |
| `evaluation_report.md` | required report (metrics, custom cases, failures, improvements) |
| `usage_report.md` | required token / cost report (regenerate after the final run) |
| `sample_patterns.md` | team note on the 32 solved samples |

## Commands (run from the repo root)

```bash
# 1. score an output against the 32 solved samples
#    (first run your agent on dataset/sample_deletion_requests.csv)
python3 code/evaluation/main.py score --pred <sample_predictions.csv> --failures 20

# 2. check the real submission: 518 rows, header, order, schema, evidence <= audit_at
python3 code/evaluation/main.py check --pred output.csv

# 3. custom cases (add --agent to run an agent on them; {requests} and {out} are filled in)
python3 code/evaluation/main.py custom --agent "python3 code/main.py --requests {requests} --out {out}"

# 4. refresh the numbers inside evaluation_report.md (section 8) and the usage report
python3 code/evaluation/main.py score --pred <sample_predictions.csv> --update-report code/evaluation/evaluation_report.md
python3 code/evaluation/main.py usage

# 5. tests
python3 -m unittest discover -s code/evaluation -p "test_*.py"
```

`score` exits 1 with `--min-status-acc X` if status accuracy is below X, and `check` exits 1
when the submission is not valid, so both can gate a CI step or a pre-submit script.

## Reporting model usage

Every component that calls a model must record its usage (counts only, no prompts):

```python
from evaluation.usage import record_usage   # or: from usage import record_usage when run from this folder
record_usage("provider", "model-name", input_tokens, output_tokens, case_id="case_42", purpose="vision")
```

Fill `PRICING` in `usage.py` (USD per 1M tokens) or export `USAGE_PRICING_JSON`, delete
`usage_log.jsonl` before the final full run, run the pipeline once, then `main.py usage`.
A model without a price shows up as "unpriced" in the report instead of silently costing 0.

## Decisions and tool actions (for DECISIONS.md / TOOLS.md once they are on main)

DECISIONS.md and TOOLS.md exist only on `origin/muse/multimodal-intent`, not on `main`, so
this PR does not create them (it would conflict). Entries to paste after that PR merges:

* Decision: the scorer treats `resource_id` order as valid if it is numeric-natural **or**
  lexical until the team picks one; `check` also accepts natural or string case order.
* Decision: evidence citations must exist in `evidence_files.csv`, `system_activity.csv`
  or `user_audit_log.csv` and be timestamped at or before the case's `audit_at`.
* Decision: custom cases are derived from the published gold by metamorphic changes, not
  hand-labelled, and no organizer-only data is used anywhere in this folder.
* Tool actions: standard-library Python only; read-only use of `dataset/`; Hark's branch
  was read (not modified) to produce reference runs.
