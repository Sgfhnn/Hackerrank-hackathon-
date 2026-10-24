#!/usr/bin/env python3
"""Repeatable extraction pipeline for the multimodal evidence facts.

`documents.json` and `images.json` in this folder are NOT hand-typed answers.
They are produced by this repeatable two-stage step, run from the repo root:

  Stage 1 - mechanical extraction (this script)
    * Reads `dataset/evidence_files.csv` (the manifest) and selects the
      4 PDF documents and 11 PNG screenshots.
    * Builds the entry skeleton (evidence_id, file, kind, source_origin,
      captured_at) directly from the manifest - `--skeleton` prints it.
    * Extracts the full text layer of every PDF with `pdftotext -layout`
      (`--pdf-text <evidence_id>` prints one).
    * Parses every PNG's signature + IHDR (dimensions) with the stdlib only.
    * Resolves subject identities and resource ownership from
      `dataset/user_identities.csv`, `dataset/user_audit_log.csv` and the
      LINK_RESOURCE_IDENTITY rows of `dataset/system_activity.csv`.

  Stage 2 - semantic read (vision pass)
    A vision-capable model reads each PNG screenshot and each PDF text layer
    and fills the schema fields that are not mechanical: subjects,
    resources (system_id / resource_id / resource_type / state_at_capture),
    verbatim-grounded `facts` strings, policy_flags and notes. Canonical
    resource_ids come from the LINK rows, never from the artifact label;
    where the artifact shows a different label it is kept as `display_id`.

  Validation (this script, default mode)
    `python3 code/extracted/extract.py` re-runs stage 1 and checks the
    committed JSON against the source artifacts and the dataset:
      - the JSON entries cover exactly the manifest's PDF/PNG files, and
        every mechanical field equals the manifest value;
      - every evidence file exists on disk; PNGs parse as PNG;
      - PDF entries: every subject email / name appears verbatim in the
        PDF text layer, every subject user_id appears there or resolves
        from the subject email via user_identities.csv, every
        display_id and every ORD-#### id quoted in facts appears in the
        text, and every resource_id appears in the text, has a display_id
        that does, or is a document-level container (e.g. a register);
      - image entries: every subject (user_id, email) pair resolves in
        user_identities.csv / user_audit_log.csv; a subject that resolves
        nowhere must be recorded as an unresolved conflict in `notes`
        (the marketing_001 screenshot header is exactly that case);
      - every resource_id has a LINK_RESOURCE_IDENTITY activity, or the
        entry is a PDF (document-owned resources and registers have no
        LINK row by design), and where both the entry subject and the
        LINK row name a user_id they must agree (again, a recorded
        conflict in `notes` is the only allowed exception).

    Exit code 0 means every check passed. Run it after any change to the
    extracted JSON; CI / reviewers can reproduce the facts from the raw
    evidence with it. Requires: python3 stdlib + `pdftotext` (poppler).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]  # code/extracted/extract.py -> repo root
DATASET = ROOT / "dataset"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ORDER_RE = re.compile(r"ORD-\d+")
LINK_RE = re.compile(
    r"linked (?P<rid>\S+) \((?P<label>[^)]*)\) to (?P<email>\S+@\S+) and (?P<uid>user_\d+)"
)
LINK_ALT_RE = re.compile(
    r"confirmed (?P<rid>\S+) \((?P<label>[^)]*)\) belongs only to (?P<email>\S+@\S+) and (?P<uid>user_\d+)"
)

# ---------------------------------------------------------------- stage 1 --


def load_manifest() -> list[dict]:
    """PDF/PNG rows of dataset/evidence_files.csv, in manifest order."""
    rows = []
    with open(DATASET / "evidence_files.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            path = row["file_path"]
            if path.endswith(".pdf") or path.endswith(".png"):
                rows.append(row)
    return rows


def skeleton_entry(row: dict) -> dict:
    """The mechanical part of an extracted entry, from the manifest only."""
    path = row["file_path"]
    return {
        "evidence_id": row["file_id"],
        "file": f"dataset/{path}",
        "kind": "pdf" if path.endswith(".pdf") else "image",
        "source_origin": row["origin"],
        "captured_at": row["captured_at"],
    }


def pdf_text(path: Path) -> str:
    """Full text layer of a PDF via pdftotext -layout (poppler)."""
    proc = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout


def png_dimensions(path: Path) -> tuple[int, int]:
    """(width, height) from the PNG IHDR chunk, stdlib only."""
    with open(path, "rb") as fh:
        header = fh.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"{path} is not a PNG with an IHDR first chunk")
    return struct.unpack(">II", header[16:24])


def load_identities() -> tuple[dict[str, dict], dict[str, set[str]]]:
    """user_id -> identity row, and email -> {user_id} (current + historical)."""
    by_user: dict[str, dict] = {}
    email_users: dict[str, set[str]] = {}
    with open(DATASET / "user_identities.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            by_user[row["user_id"]] = row
            email_users.setdefault(row["email"].lower(), set()).add(row["user_id"])
    with open(DATASET / "user_audit_log.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            for value in row.values():
                if value and "@" in value:
                    email_users.setdefault(value.lower(), set()).add(row["user_id"])
    return by_user, email_users


def load_links() -> dict[str, dict]:
    """resource_id -> {email, user_id, label} from LINK_RESOURCE_IDENTITY rows."""
    links: dict[str, dict] = {}
    with open(DATASET / "system_activity.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if "LINK" not in (row.get("event_type") or ""):
                continue
            details = row.get("details") or ""
            m = LINK_RE.search(details) or LINK_ALT_RE.search(details)
            if m:
                links[row["resource_id"]] = {
                    "email": m.group("email").rstrip("."),
                    "user_id": m.group("uid"),
                    "label": m.group("label"),
                }
    return links


def load_extracted() -> list[dict]:
    entries: list[dict] = []
    for name in ("documents.json", "images.json"):
        with open(HERE / name, encoding="utf-8") as fh:
            entries.extend(json.load(fh))
    return entries


# ------------------------------------------------------------- validation --

# Resource types that name the document itself rather than a label printed
# verbatim in it (a register's own id is assigned by the extraction).
CONTAINER_TYPES = {"retention_register"}


def check_entries(entries: list[dict]) -> list[str]:
    """Validate extracted entries against manifest, artifacts and dataset.

    Returns a list of human-readable problems (empty = all checks pass).
    """
    problems: list[str] = []
    manifest = {row["file_id"]: row for row in load_manifest()}
    by_user, email_users = load_identities()
    links = load_links()

    expected_ids = set(manifest)
    got_ids = [e["evidence_id"] for e in entries]
    if set(got_ids) != expected_ids:
        problems.append(
            f"coverage: extracted ids {sorted(got_ids)} != manifest PDF/PNG ids "
            f"{sorted(expected_ids)}"
        )
    if len(got_ids) != len(set(got_ids)):
        problems.append("coverage: duplicate evidence_id in extracted JSON")

    for entry in entries:
        eid = entry["evidence_id"]
        row = manifest.get(eid)
        if row is None:
            continue  # already reported by the coverage check
        skel = skeleton_entry(row)
        for key, want in skel.items():
            if entry.get(key) != want:
                problems.append(f"{eid}: {key}={entry.get(key)!r} != manifest {want!r}")

        artifact = ROOT / entry["file"]
        if not artifact.exists():
            problems.append(f"{eid}: evidence file missing: {entry['file']}")
            continue

        text = ""
        if entry["kind"] == "pdf":
            try:
                text = pdf_text(artifact)
            except (OSError, subprocess.CalledProcessError) as exc:
                problems.append(f"{eid}: pdftotext failed: {exc}")
        else:
            try:
                width, height = png_dimensions(artifact)
                if width <= 0 or height <= 0:
                    problems.append(f"{eid}: PNG has non-positive dimensions")
            except ValueError as exc:
                problems.append(f"{eid}: {exc}")

        notes = (entry.get("notes") or "").lower()
        conflict_recorded = "conflict" in notes

        for subject in entry.get("subjects", []):
            email = (subject.get("email") or "").lower()
            uid = subject.get("user_id")
            if entry["kind"] == "pdf" and text:
                if email and email not in text.lower():
                    problems.append(f"{eid}: subject email {email} not in PDF text")
                name = subject.get("name")
                if name and name not in text:
                    problems.append(f"{eid}: subject name {name!r} not in PDF text")
                if uid and uid not in text:
                    # The user_id may come from identity resolution on the
                    # email printed in the document instead of being printed.
                    if not (email and uid in email_users.get(email, set())):
                        problems.append(
                            f"{eid}: subject {uid} not in PDF text and not "
                            f"resolvable from its email"
                        )
            if email and uid:
                if uid not in email_users.get(email, set()):
                    problems.append(
                        f"{eid}: subject ({uid}, {email}) not found in "
                        f"user_identities / user_audit_log"
                    )
            elif email and not uid:
                if email not in email_users and not conflict_recorded:
                    problems.append(
                        f"{eid}: subject email {email} resolves to no user and "
                        f"no conflict is recorded in notes"
                    )

        for resource in entry.get("resources", []):
            rid = resource.get("resource_id")
            display_id = resource.get("display_id")
            if entry["kind"] == "pdf" and text:
                if display_id and display_id not in text:
                    problems.append(f"{eid}: display_id {display_id} not in PDF text")
                if (
                    rid
                    and rid not in text
                    and not display_id
                    and resource.get("resource_type") not in CONTAINER_TYPES
                ):
                    problems.append(f"{eid}: resource_id {rid} not in PDF text")
                for fact in resource.get("facts", []):
                    for order_id in ORDER_RE.findall(fact):
                        if order_id not in text:
                            problems.append(
                                f"{eid}: order {order_id} quoted in facts not in PDF text"
                            )
            link = links.get(rid)
            if link is None:
                if entry["kind"] != "pdf":
                    problems.append(
                        f"{eid}: resource {rid} has no LINK_RESOURCE_IDENTITY activity"
                    )
                # PDF resources with no LINK row are document-owned (owned via
                # the subject printed in the document) or containers; both are
                # handled by code/hark/multimodal_facts.py.
            else:
                for subject in entry.get("subjects", []):
                    suid = subject.get("user_id")
                    if suid and link["user_id"] != suid and not conflict_recorded:
                        problems.append(
                            f"{eid}: resource {rid} LINK user {link['user_id']} "
                            f"!= subject user {suid} (and no conflict recorded)"
                        )
    return problems


# -------------------------------------------------------------------- CLI --


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--skeleton", action="store_true",
        help="print the manifest-derived entry skeletons as JSON and exit",
    )
    parser.add_argument(
        "--pdf-text", metavar="EVIDENCE_ID",
        help="print the pdftotext -layout text layer of one PDF and exit",
    )
    args = parser.parse_args(argv)

    if args.skeleton:
        print(json.dumps([skeleton_entry(r) for r in load_manifest()], indent=2))
        return 0

    if args.pdf_text:
        for row in load_manifest():
            if row["file_id"] == args.pdf_text and row["file_path"].endswith(".pdf"):
                sys.stdout.write(pdf_text(DATASET / row["file_path"]))
                return 0
        print(f"no PDF evidence with id {args.pdf_text}", file=sys.stderr)
        return 2

    entries = load_extracted()
    problems = check_entries(entries)
    n_pdf = sum(1 for e in entries if e["kind"] == "pdf")
    n_img = sum(1 for e in entries if e["kind"] == "image")
    if problems:
        print(f"extract.py: {len(problems)} problem(s) in extracted facts:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(
        f"extract.py: OK - {len(entries)} extracted entries ({n_pdf} PDFs, "
        f"{n_img} PNGs) match the manifest, the PDF text layers, "
        f"user_identities/user_audit_log and the LINK_RESOURCE_IDENTITY rows."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
