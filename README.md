# Ghost Data Still Exists: a deletion audit agent

Built in 24 hours for **HackerRank Orchestrate, October 2026**, a solo AI agent hackathon.

A company says a user's data was deleted. Was it really? This agent audits privacy deletion requests across databases, logs, support attachments, exports, search indexes, backups and sync jobs. For each request it:

1. Decides whether a deletion was actually requested.
2. Returns `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE`.
3. Lists any data left behind or at risk of coming back, plus the ordered remediation and verification steps to finish the deletion safely.

It processes all 518 cases in the dataset and writes `output.csv` (`case_id, deletion_status, steps_json`).

## Results

- **Output accuracy: 28.8 / 30** against HackerRank's hidden test cases.
- Matches 30 of the 32 public sample answers exactly.
- Final rank: 1,449 of 4,094 participants. The code score pulled the rank down (see Lessons).

## How it works

- **Identity resolution** (`code/main.py`): follows email changes, phone changes and hashed emails over time, so data stored under an old identifier is still found.
- **Intent classifier** (`code/intent.py`): separates real deletion requests from look-alikes such as export or correction requests.
- **Evidence engine** (`code/hark/`): ties activity logs, audit events and evidence files to each case, then runs a state machine per system to work out what still exists, what is still syncing, and what a backup restore could bring back.
- **Multimodal facts** (`code/multimodal.py`, `code/extracted/`): facts read from PDF reports and screenshots, produced by `code/extracted/extract.py`.
- **Validation** (`code/main.py`, `code/evaluation/`): every step is checked against the allowed phase, operation and state table, evidence must predate the audit, and a perturbation check makes sure answers don't depend on row order or ids.

## Lessons

I'm keeping this section honest, since that's the useful part.

- **No runtime LLM.** The pipeline is fully deterministic. It's accurate and reproducible, but the judges expected an LLM agent, so the code score was low. Next time: the model reads and interprets, and the code validates its output.
- **Sample-fit heuristics.** `code/hark/windows.py` has a timing fallback tuned on the 32 public samples. It improved sample accuracy, but it isn't guaranteed to generalize, and it's labeled as such in the file.
- **Precomputed extraction.** The multimodal facts are checked in as JSON instead of being extracted at runtime.
- **Read the rubric first.** Confirm what the judges reward before locking the architecture.

## How to run (`python3 code/main.py`)

Requirements:

- **Python 3.9+, standard library only.** Every module imported under `code/` (`csv`, `json`, `os`, `re`, `sys`, `sqlite3`, `hashlib`, `pathlib`, `dataclasses`, `datetime`, `argparse`, `tempfile`, `unittest`, plus local modules) is part of the Python 3 standard library or ships inside this repository. No `pip install` or virtual environment is needed.
- The `dataset/` directory must sit **next to `code/` at the repository root** (i.e. `dataset/` and `code/` are siblings). The dataset is **not** shipped inside `code.zip`; place the challenge-provided `dataset/` beside the extracted `code/` before running.

Run from the repository root:

```bash
python3 code/main.py
```

This processes all 518 requests in `dataset/deletion_requests.csv` and writes `output.csv` at the repository root with exactly 518 data rows (plus the header `case_id,deletion_status,steps_json`), sorted by `case_id`. Typical runtime is a few seconds on a laptop CPU. No network access, API keys, or model calls are required: the pipeline is deterministic.

Optional checks:

- Run the built-in contract self-tests:

  ```bash
  python3 code/main.py --selftest
  ```

- Validate an existing output file (exit non-zero on problems):

  ```bash
  python3 code/main.py --check output.csv
  ```

- Re-run the full agent under shuffled, renamed, and re-cased requests to prove the answers do not depend on `case_id` or row order (requires `dataset/`):

  ```bash
  python3 code/evaluation/perturbation_check.py
  ```

Evaluation and reports:

- `code/evaluation/evaluation_report.md` — case-level, step-level, field-level, and sequence accuracy, custom evaluation cases, failures found, and improvements made.
- `code/evaluation/usage_report.md` — model providers/names, model-call counts, token usage, and cost breakdown for the final full-dataset run.
- Score a prediction file against the public sample gold (requires a run matched to `dataset/sample_deletion_requests.csv`):

  ```bash
  python3 code/evaluation/main.py score --pred <predictions.csv> --gold dataset/sample_output.csv
  ```

  `python3 code/evaluation/main.py score` (default command) scores the default prediction file against `dataset/sample_output.csv`; the 518-case `output.csv` has no public gold, so score sample-sized outputs for a meaningful comparison.
- Validate the full submission format without gold answers:

  ```bash
  python3 code/evaluation/main.py check --pred output.csv
  ```

### Package and verify `code.zip`

`code.zip` contains `README.md` and the `code/` directory, including `code/evaluation/evaluation_report.md` and `code/evaluation/usage_report.md`. The builder excludes `dataset/`, git metadata, virtual environments, `__pycache__`/`*.pyc`, `output.csv`, `log.txt`, nested archives, and credential files. Entries are sorted with fixed timestamps, so the archive is reproducible.

```bash
python3 code/package_zip.py
python3 code/package_zip.py --check
```

`--check` re-opens an existing archive and exits non-zero if a forbidden path or extension is present, if `README.md` or either evaluation report is missing, or if a text entry contains something that looks like a secret.

### Code layout

- `code/main.py` — core contract, dataset loaders, identity resolution, and output rendering.
- `code/evidence.py` — adapter that runs the Hark evidence/state engine.
- `code/hark/` — evidence ingestion, identity windows, and the state engine.
- `code/intent.py` — rules-first deletion-intent classifier.
- `code/multimodal.py` + `code/extracted/` — facts extracted from PDFs and screenshots.
- `code/evaluation/` — scorer, validators, custom cases, perturbation check, and the required reports.
- `code/package_zip.py` — deterministic `code.zip` builder and verifier.
