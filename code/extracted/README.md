# Extracted multimodal facts (Muse, task 4)

Facts extracted from the 4 PDF documents and 11 PNG screenshots in
`dataset/evidence/`. These are participant-facing evidence files; the state
engine should treat them like any other evidence source:

- Cite the `evidence_id` (from `dataset/evidence_files.csv`), never this folder.
- A fact only counts for cases whose `audit_at` is at or after the file's
  `captured_at` (manifest rule in AGENTS.md section 6.6).
- Facts are recorded as shown in the artifact. Where a screenshot conflicts
  with another source (see the marketing_001 identity conflict in
  `images.json`), both claims are recorded and resolution is left to the
  state engine.

## Files

- `documents.json` — one entry per PDF (evidence_18, evidence_27,
  evidence_55, evidence_58).
- `images.json` — one entry per PNG (evidence_04, evidence_09, evidence_14,
  evidence_19, evidence_23, evidence_28, evidence_32, evidence_37,
  evidence_42, evidence_46, evidence_60).

## Schema (per entry)

```json
{
  "evidence_id": "evidence_04",
  "file": "dataset/evidence/images/account_admin_console_20260926.png",
  "kind": "image | pdf",
  "source_origin": "<manifest source_origin>",
  "captured_at": "<manifest captured_at, ISO UTC>",
  "subjects": [{"user_id": "...", "name": "...", "email": "...",
                "identifiers_exposed": ["name", "email"]}],
  "resources": [{"system_id": "...", "resource_id": "...",
                 "resource_type": "...", "state_at_capture": "PRESENT",
                 "facts": ["verbatim-grounded fact strings"]}],
  "policy_flags": ["retention / active flow / shared context hints"],
  "notes": "interpretation hints for the state engine",
  "extraction_method": "how the facts were obtained"
}
```

## How these files are produced (repeatable)

These JSON files are the output of a repeatable extraction step, not
hand-typed answers. The pipeline is `code/extracted/extract.py`:

1. **Mechanical stage (script).** The script reads
   `dataset/evidence_files.csv` and selects the 4 PDFs + 11 PNGs. The
   skeleton of every entry (`evidence_id`, `file`, `kind`, `source_origin`,
   `captured_at`) is copied from the manifest — print it with
   `python3 code/extracted/extract.py --skeleton`. PDF text layers are
   extracted with `pdftotext -layout` (`--pdf-text <evidence_id>` prints
   one); PNG headers are parsed with the stdlib. Subject identities and
   resource ownership are resolved from `user_identities.csv`,
   `user_audit_log.csv` and the `LINK_RESOURCE_IDENTITY` rows of
   `system_activity.csv`.
2. **Semantic stage (vision pass).** A vision-capable model reads each
   screenshot and PDF text layer and fills the non-mechanical fields
   (subjects, resources and their state at capture, verbatim-grounded
   `facts`, policy flags, notes). Canonical `resource_id`s come from the
   LINK rows, never from the label printed in the artifact; a differing
   artifact label is kept as `display_id`.
3. **Validation (script).** `python3 code/extracted/extract.py` (no
   arguments) re-runs stage 1 and fails (exit 1) if any committed fact
   disagrees with the manifest, a PDF text layer, the identity files or
   the LINK rows. A subject that resolves nowhere is only accepted when
   the entry's `notes` record it as an unresolved conflict — the
   marketing_001 screenshot header is that case. Run it after any change
   to these JSON files.

## Loaders

Use `code/multimodal.py`:

- `load_extracted_facts()` — all 15 entries.
- `facts_for_user(user_id=..., email=...)` — entries whose subjects match.
- `facts_for_resource(resource_id)` — entries mentioning a resource.
- `retention_entries()` — entries flagged as retention/security/legal
  (evidence_27 register, evidence_55 + evidence_37 audit log).

## Canonical resource IDs

`resource_id` is always the system resource ID the output cites (taken from the
file's `LINK_RESOURCE_IDENTITY` activity). Where the artifact itself shows a
different label, that label is kept as `display_id`:
evidence_18 `NST-2026-0718-1842` -> `export_service/export_1842` (activity_4009; artifact system kept as display_system_id),
evidence_46 bucket prefix -> `object_19` (activity_4007, delivery_confirmation_19.pdf),
evidence_42 `user_17` -> `tombstone_17` (activity_4011).
