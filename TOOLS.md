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
