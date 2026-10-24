# Tools

Append-only log of tools used on this worktree. Times are EAT.

| time (EAT) | tool | what it did |
|---|---|---|
| 2026-10-24 16:13 | read | Read AGENTS.md, README.md, problem_statement.md, and dataset samples. |
| 2026-10-24 16:13 | write | Created `log.txt` with the required SESSION START entry. |
| 2026-10-24 16:14 | bash (python3) | Inspected dataset headers, audit chains, sample answers, and timestamp/identifier consistency. |
| 2026-10-24 16:14 | write | Created `code/main.py` (stdlib-only core, loaders, resolver, steps, validation, CLI, selftest). |
| 2026-10-24 16:15 | bash (python3) | Ran `python3 code/main.py --selftest` until it passed 32/32 sample round-trips. |
| 2026-10-24 16:15 | bash (python3) | Ran `--limit 20` smoke test and full 518-row run to `/tmp/out_full.csv`; ran `--check`. |
| 2026-10-24 16:16 | bash (python3) | Exercised the classifier/evidence plug-in hooks with temporary modules in `/tmp`. |
| 2026-10-24 16:16 | edit | Hardened `as_of` hash reconstruction, sorted per-user audit history, and removed a redundant check. |
| 2026-10-24 16:16 | write | Created `DECISIONS.md`, `TOOLS.md`, and `code/README_core.md`. |
| 2026-10-24 16:18 | coordinator-review | reviewed main.py, ran selftest/full run/32-sample strict-evidence test |
| 2026-10-24 16:18 | edit | solve_case keeps plug-in steps on validation problems; stub auditor skips validation |
| 2026-10-24 16:21 | coordinator edit | Added the `intent.py` (Muse) adapter to `get_intent_classifier`; tested it against Muse's branch copy of intent.py (not committed here). |

# TOOLS.md — shared tool-action log (team rule: time EAT, tool, what it did)

All agents append. One line per action.

- 2026-10-24 16:04 EAT | Muse (Muse) | git clone of the public starter repo + GitHub git-database API | Copied the starter into Sgfhnn/Hackerrank-hackathon- main (commit 3af4a4e, 110 files, verified blob-by-blob, 0 mismatches) at @anu's order.
- 2026-10-24 16:08 EAT | Muse (Muse) | file reads | Read AGENTS.md, problem_statement.md, deletion_rules.csv, evidence_files.csv to pin the output contract for task 4.
- 2026-10-24 16:09 EAT | Muse (Muse) | pdftotext -layout | Extracted text from the 4 evidence PDFs (evidence_18, evidence_27, evidence_55, evidence_58).
- 2026-10-24 16:09 EAT | Muse (Muse) | screenshot viewing (vision) | Read all 11 evidence PNGs and transcribed their console states.
- 2026-10-24 16:10 EAT | Muse (Muse) | grep on dataset CSVs | Cross-checked extracted identifiers against user_identities.csv and system_activity.csv LINK_RESOURCE_IDENTITY rows; found the marketing_001 identity conflict (see DECISIONS.md).
- 2026-10-24 16:11 EAT | Muse (Muse) | python3 code/test_intent.py | Intent classifier validation: 32/32 samples deletion, 518/518 evaluation requests classified (508 deletion / 10 not-deletion), 0 needing LLM fallback.
- 2026-10-24 16:42 EAT | Cue (Cue) | git fetch / git worktree add | Fresh worktree `/workspace/wt-cue2` on branch `cue/eval-main-run` from origin/main (45c5428).
- 2026-10-24 16:43 EAT | Cue (Cue) | python3 code/main.py (+ --requests/--output on the 32 samples, --selftest, --check) | End-to-end run: 518 rows (COMPLETE 152 / INCOMPLETE 356 / NOT_APPLICABLE 10), deterministic, 3.4 s; sample predictions to /tmp.
- 2026-10-24 16:43 EAT | Cue (Cue) | python3 code/evaluation/main.py score / check / custom --agent / usage | Scored samples (status 32/32, exact 30/32), checked output.csv (OK), custom cases (status 8/8, exact 5/8), usage (0 model calls).
- 2026-10-24 16:46 EAT | Cue (Cue) | python3 code/evaluation/batch_check.py (new) | Showed custom_04 / custom_07 depend on the batch (pass without their base sample); custom_06 is the known F1.
- 2026-10-24 16:47 EAT | Cue (Cue) | file edit | Updated evaluation_report.md (section 8 and pointers), usage_report.md, evaluation README.md.
