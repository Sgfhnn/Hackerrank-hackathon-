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

## Loaders

Use `code/multimodal.py`:

- `load_extracted_facts()` — all 15 entries.
- `facts_for_user(user_id=..., email=...)` — entries whose subjects match.
- `facts_for_resource(resource_id)` — entries mentioning a resource.
- `retention_entries()` — entries flagged as retention/security/legal
  (evidence_27 register, evidence_55 + evidence_37 audit log).
