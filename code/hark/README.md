# code/hark - evidence ingestion + state engine (Hark)

Deterministic, stdlib-only (Python 3 + sqlite3). No model calls, so token usage for this component is 0.

```
python3 code/hark/run.py                    # -> code/hark/output_hark.csv (518 rows) + sample_predictions.csv
python3 code/hark/run.py --out output.csv   # also write the submission file
python3 code/hark/score.py                  # sample score vs dataset/sample_output.csv
python3 code/hark/explain_samples.py        # -> sample_explanations.md (per-step evidence trace)
```

| file | role |
|---|---|
| `ingest.py` | loads every SQL/JSON/TXT/log evidence file into flat observations (file_id, observed_at, system_id, resource_id, identifiers, present/pii/enabled/restore flags); ISO-UTC time parsing; log trace_id joins |
| `identity.py` | request email -> user_id via `user_identities.csv` + `user_audit_log.csv`; all current + historical emails/phones/names/hashes |
| `windows.py` | deletion-worker activity windows relative to `reported_at` and the second-level fingerprint that ties follow-up activities to a case's account activity |
| `engine.py` | `Engine.prepare()` global activity->case assignment, `state_at()`, `active_data_flows()`, `restore_risks()`, `plan()` -> (status, ordered steps) |
| `multimodal_facts.py` | adapter: Muse's extracted PNG/PDF facts (`code/multimodal.py`) -> per-resource state, treatment, document owners and retention entries |
| `multimodal_hints.py` | hand-read fallback, used only for a resource the extraction lacks (none today) |
| `test_multimodal_facts.py` | `python3 -m unittest discover -s code/hark -p "test_*.py"` |
| `score.py` | status acc, exact case, sequence acc, step P/R/F1, evidence acc |
| `FINDINGS.md` | reverse-engineered rules from the 32 samples |

Sample score (32 cases): status 1.00, exact case 0.9375 (30/32), sequence 0.9375, step P 1.00 / R 0.99 / F1 0.995, evidence-id 1.00.
The two misses (case_18, case_20) are security audit records that appear in no participant-visible file.
Caveat: the rules were derived from these same 32 samples, so this is an optimistic estimate.
