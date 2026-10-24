#!/usr/bin/env python3
"""Build and verify the ``code.zip`` submission archive.

Usage
-----
    python3 code/package_zip.py [--out PATH] [--check]

The archive contains ``README.md`` and the whole ``code/`` directory, including
the required ``code/evaluation/evaluation_report.md`` and
``code/evaluation/usage_report.md``.  ``dataset/``, git metadata, virtual
environments, caches, nested archives, and credential files are never included.

The build is deterministic: entries are written in sorted order with fixed
1980-01-01 timestamps, forward-slash paths, and ZIP_DEFLATED compression.  After
writing, the archive is re-opened and verified.  ``--check`` verifies an
existing archive without rebuilding it.

Verification fails (non-zero exit) when a forbidden path or extension is found,
when a required file is missing, or when a text entry contains something that
looks like a secret (GitHub/Slack/AWS/OpenAI-style tokens, private keys, hard
coded API keys, bearer tokens).

Standard library only.  Python 3.9+.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
CODE_DIR = SCRIPT_PATH.parent
REPO_ROOT = CODE_DIR.parent
DEFAULT_OUT = REPO_ROOT / "code.zip"

README = "README.md"
REQUIRED_ENTRIES = (
    README,
    "code/evaluation/evaluation_report.md",
    "code/evaluation/usage_report.md",
)

FORBIDDEN_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".idea",
    ".vscode",
    "dataset",
}

FORBIDDEN_NAMES = {
    ".ds_store",
    "output.csv",
    "log.txt",
    "agents.md",
    "code.zip",
    "thumbs.db",
    "desktop.ini",
}

FORBIDDEN_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".7z",
    ".rar",
    ".bz2",
    ".xz",
    ".whl",
    ".egg",
}

SECRET_FILE_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".ppk",
    ".asc",
}

# Each pattern is written so that the source of this file does not match it.
SECRET_PATTERNS = (
    ("GitHub token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("API key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{20,}")),
    ("private key block", re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Slack token", re.compile(r"xoxb-[A-Za-z0-9-]{10,}")),
    ("bearer token", re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}")),
    ("hardcoded api_key", re.compile(r"api_key\s*=\s*['\"][^'\"]{16,}")),
)


def _is_secret_file(name: str) -> bool:
    """Return True for file names that normally hold credentials."""
    lowered = name.lower()
    if lowered in {".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials"}:
        return True
    if lowered.startswith(".env."):
        return True
    if lowered.startswith(("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")):
        return True
    if any(lowered.endswith(suffix) for suffix in SECRET_FILE_SUFFIXES):
        return True
    if "credential" in lowered or "secret" in lowered:
        return True
    if lowered.startswith("token") or lowered.endswith(("_token", ".token")):
        return True
    return False


def _is_forbidden(arcname: str) -> bool:
    """Return a reason string when an archive path must not be shipped."""
    parts = [part for part in arcname.split("/") if part]
    if not parts:
        return "empty path"
    for part in parts:
        if part.lower() in FORBIDDEN_DIRS:
            return "forbidden directory: %s" % part
    base = parts[-1]
    lowered = base.lower()
    if lowered in FORBIDDEN_NAMES:
        return "forbidden file: %s" % base
    if any(lowered.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        return "forbidden extension: %s" % base
    if _is_secret_file(lowered):
        return "credential-like file: %s" % base
    return ""


def _iter_sources(skip_path: Path):
    """Yield (absolute path, archive name) pairs for README.md and code/."""
    candidates = [REPO_ROOT / README]
    candidates.extend(sorted(path for path in CODE_DIR.rglob("*") if path.is_file()))
    for path in candidates:
        resolved = path.resolve()
        if resolved == skip_path:
            continue
        try:
            arcname = resolved.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            continue
        if _is_forbidden(arcname):
            continue
        yield resolved, arcname


def _zip_info(arcname: str) -> zipfile.ZipInfo:
    """Create a deterministic ZipInfo with fixed metadata."""
    info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def build_archive(out_path: Path) -> int:
    """Write the deterministic archive. Return the number of file entries."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    skip = out_path.resolve()
    count = 0
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, arcname in sorted(_iter_sources(skip), key=lambda item: item[1]):
            archive.writestr(_zip_info(arcname), path.read_bytes())
            count += 1
    return count


def _entry_text(data: bytes) -> str:
    """Decode an entry as text, or return an empty string for binary data."""
    if b"\x00" in data[:4096]:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _scan_secrets(names_and_data):
    """Yield human-readable findings without echoing the matched secret."""
    for arcname, data in names_and_data:
        text = _entry_text(data)
        if not text:
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    yield "%s:%d: secret-like %s" % (arcname, line_no, label)


def verify_archive(out_path: Path) -> list:
    """Re-open an archive and return a list of problems (empty means valid)."""
    if not out_path.is_file():
        return ["archive not found: %s" % out_path]

    problems = []
    try:
        archive = zipfile.ZipFile(out_path)
    except (OSError, zipfile.BadZipFile) as exc:
        return ["cannot open %s: %s" % (out_path, exc)]

    with archive:
        infos = archive.infolist()
        names = []
        scanned = []
        for info in infos:
            arcname = info.filename
            names.append(arcname)
            if arcname.startswith("/") or arcname.startswith("../") or "/../" in arcname:
                problems.append("unsafe path: %s" % arcname)
            if "\\" in arcname:
                problems.append("non-forward-slash path: %s" % arcname)
            if arcname == "dataset" or arcname.startswith("dataset/"):
                problems.append("dataset content: %s" % arcname)
            reason = _is_forbidden(arcname)
            if reason:
                problems.append("%s (%s)" % (reason, arcname))
            if info.is_dir():
                continue
            try:
                scanned.append((arcname, archive.read(info)))
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                problems.append("cannot read %s: %s" % (arcname, exc))

        for required in REQUIRED_ENTRIES:
            if required not in names:
                problems.append("missing required entry: %s" % required)

        if names != sorted(names):
            problems.append("entries are not sorted deterministically")

        problems.extend(_scan_secrets(scanned))

    return problems


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _print_summary(path: Path, entry_count: int) -> None:
    size = path.stat().st_size
    print("archive: %s" % path)
    print("entries: %d" % entry_count)
    print("size: %d bytes" % size)
    print("sha256: %s" % _sha256(path))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Build or verify the deterministic code.zip submission archive.",
        epilog="Run from any directory; the default output is <repo>/code.zip.",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="archive path (default: <repo>/code.zip)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify an existing archive instead of rebuilding it",
    )
    args = parser.parse_args(argv)

    out_path = Path(args.out).expanduser()
    if not out_path.is_absolute():
        out_path = (Path.cwd() / out_path).resolve()

    if args.check:
        if not out_path.is_file():
            print("ERROR: archive not found: %s" % out_path, file=sys.stderr)
            return 2
        problems = verify_archive(out_path)
        if problems:
            print("FAILED: %s" % out_path, file=sys.stderr)
            for problem in problems:
                print("  - %s" % problem, file=sys.stderr)
            return 1
        with zipfile.ZipFile(out_path) as archive:
            entry_count = len(archive.infolist())
        _print_summary(out_path, entry_count)
        print("status: OK")
        return 0

    count = build_archive(out_path)
    problems = verify_archive(out_path)
    if problems:
        if out_path.exists():
            out_path.unlink()
        print("FAILED: %s was not created" % out_path, file=sys.stderr)
        for problem in problems:
            print("  - %s" % problem, file=sys.stderr)
        return 1

    _print_summary(out_path, count)
    print("status: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
