"""Loader for the multimodal facts extracted from PDF/PNG evidence.

Facts live in code/extracted/documents.json (4 PDFs) and
code/extracted/images.json (11 PNGs). Schema is documented in
code/extracted/README.md.

The state engine should:
  * match facts to a case through subjects (user_id / email, current or
    historical) or through resources (system_id + resource_id);
  * only cite an entry when its captured_at <= the case's audit_at;
  * treat policy_flags as hints, and deletion_rules.csv as the authority.
"""

from __future__ import annotations

import json
from pathlib import Path

EXTRACTED_DIR = Path(__file__).resolve().parent / "extracted"

_RETENTION_FLAGS = {
    "retention_register",
    "legal_or_tax_retention",
    "security_audit_log",
}


def load_extracted_facts() -> list[dict]:
    """Return all extracted entries (documents first, then images)."""
    facts: list[dict] = []
    for name in ("documents.json", "images.json"):
        with open(EXTRACTED_DIR / name, encoding="utf-8") as fh:
            facts.extend(json.load(fh))
    return facts


def facts_for_user(user_id: str | None = None, email: str | None = None) -> list[dict]:
    """Entries whose recorded subjects match a user_id or an email."""
    out = []
    for entry in load_extracted_facts():
        for subject in entry.get("subjects", []):
            if user_id and subject.get("user_id") == user_id:
                out.append(entry)
                break
            if email and subject.get("email", "").lower() == email.lower():
                out.append(entry)
                break
    return out


def facts_for_resource(resource_id: str) -> list[dict]:
    """Entries that mention a given resource_id."""
    return [
        entry
        for entry in load_extracted_facts()
        if any(r.get("resource_id") == resource_id for r in entry.get("resources", []))
    ]


def retention_entries() -> list[dict]:
    """Entries that ground a RETAIN decision (registers, audit logs)."""
    return [
        entry
        for entry in load_extracted_facts()
        if _RETENTION_FLAGS.intersection(entry.get("policy_flags", []))
    ]


if __name__ == "__main__":
    all_facts = load_extracted_facts()
    print(f"{len(all_facts)} extracted entries")
    for entry in all_facts:
        resources = ", ".join(
            f"{r['system_id']}/{r['resource_id']}:{r['state_at_capture']}"
            for r in entry["resources"]
        )
        print(f"{entry['evidence_id']} ({entry['kind']}, {entry['captured_at']}) {resources}")
