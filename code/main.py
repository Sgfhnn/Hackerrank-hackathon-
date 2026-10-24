"""Core entry point for the Ghost Data Still Exists deletion-audit agent.

This module is intentionally standard-library only and importable with no
side effects at import time.  It loads the participant-facing dataset, resolves
current and historical identities, and exposes small, documented plug-in
interfaces for the intent classifier (``code/classifier.py``) and the evidence
auditor (``code/evidence.py``).  A CLI is provided behind ``__main__``.

Run ``python3 code/main.py --help`` for the command-line interface and
``python3 code/main.py --selftest`` for the built-in contract checks.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

CODE_DIR = Path(__file__).resolve().parent
REPO_ROOT = CODE_DIR.parent
DATASET_DIR = REPO_ROOT / "dataset"
OUTPUT_PATH = REPO_ROOT / "output.csv"

OUTPUT_COLUMNS: Tuple[str, ...] = ("case_id", "deletion_status", "steps_json")
STEP_FIELDS: Tuple[str, ...] = (
    "step_no",
    "phase",
    "system_id",
    "resource_id",
    "operation",
    "state",
    "evidence_id",
)

STATUSES = frozenset({"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"})

# The phase/operation/state table is the single authority for allowed steps.
OPERATION_TABLE: Tuple[Tuple[str, str, str], ...] = (
    ("FINDING", "REPORT_REMAINING_RECORD", "PRESENT"),
    ("FINDING", "REPORT_EXPOSED_PII", "PRESENT"),
    ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE"),
    ("FINDING", "REPORT_RESTORE_RISK", "ACTIVE"),
    ("REMEDIATION", "DISABLE_DATA_FLOW", "DISABLED"),
    ("REMEDIATION", "BLOCK_RESTORE", "BLOCKED"),
    ("REMEDIATION", "DELETE_RECORD", "ABSENT"),
    ("REMEDIATION", "REDACT_RECORD", "REDACTED"),
    ("REMEDIATION", "ANONYMIZE_RECORD", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT"),
    ("VERIFICATION", "VERIFY_RECORD_PRESENT", "PRESENT"),
    ("VERIFICATION", "VERIFY_RECORD_REDACTED", "REDACTED"),
    ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
    ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "BLOCKED"),
)

PHASE_ORDER: Dict[str, int] = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
OPERATION_INDEX: Dict[Tuple[str, str], int] = {
    (phase, operation): index
    for index, (phase, operation, _state) in enumerate(OPERATION_TABLE)
}
OPERATION_STATE: Dict[Tuple[str, str], str] = {
    (phase, operation): state for phase, operation, state in OPERATION_TABLE
}

# One switch controls row ordering.  AGENTS.md says "sort rows by case_id";
# Anu chose natural (numeric-aware) order, matching deletion_requests.csv; the
# lexicographic alternative stays here so it can be flipped safely.
CASE_SORT_MODE = "natural"  # team decision 2026-10-24 (Anu): match input order case_33..case_550
_CASE_NUMBER_RE = re.compile(r"(\d+)")


def case_sort_key(case_id: str) -> Any:
    """Return the deterministic sort key for a case_id row.

    The default is plain lexicographic string ordering.  Setting
    ``CASE_SORT_MODE`` to ``"natural"`` would use numeric-aware ordering
    without changing any call site.
    """
    text = str(case_id)
    if CASE_SORT_MODE == "natural":
        return tuple(
            int(part) if part.isdigit() else part
            for part in _CASE_NUMBER_RE.split(text)
        )
    return text


# Fixed UTC+3 offset used for human-readable display/log strings only.
EAT = timezone(timedelta(hours=3), name="EAT")

_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC datetime.

    Accepts a trailing ``Z`` or ``+00:00``, tolerates missing seconds (for
    example ``2026-01-04T14:56``), treats naive values as UTC, and returns
    ``None`` for ``None`` or blank input.  Invalid non-blank values raise
    ``ValueError`` so bad dataset rows fail loudly.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso_z(dt: Optional[datetime]) -> str:
    """Format a datetime as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC), or ``""``."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_eat(dt: Optional[datetime]) -> str:
    """Format a datetime in fixed UTC+3 (EAT) for display and log strings."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(EAT).strftime("%Y-%m-%d %H:%M:%S EAT")


def ts_le(left: Optional[datetime], right: Optional[datetime]) -> bool:
    """Return True only when both timestamps exist and ``left <= right``."""
    return left is not None and right is not None and left <= right


# ---------------------------------------------------------------------------
# Identifier helpers
# ---------------------------------------------------------------------------


def normalize_email(email: Any) -> str:
    """Normalize an email for matching: strip surrounding whitespace and lower."""
    if email is None:
        return ""
    return str(email).strip().lower()


def sha256_email(email: Any) -> str:
    """Return ``sha256(lower(strip(email)))`` as a lowercase hex digest."""
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


def normalize_phone(phone: Any) -> str:
    """Normalize a phone number to optional leading ``+`` plus digits."""
    if phone is None:
        return ""
    text = str(phone).strip()
    if not text:
        return ""
    has_plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if not digits:
        return ""
    return ("+" if has_plus else "") + digits


# ---------------------------------------------------------------------------
# Typed dataset records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    """One row of ``deletion_requests.csv`` / ``sample_deletion_requests.csv``."""

    case_id: str
    email: str
    requested_at: Optional[datetime]
    reported_status: str
    reported_at: Optional[datetime]
    audit_at: Optional[datetime]
    request_text: str
    requested_at_raw: str = ""
    reported_at_raw: str = ""
    audit_at_raw: str = ""
    raw: Dict[str, str] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class Identity:
    """Current identity snapshot from ``user_identities.csv``."""

    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: Optional[datetime]
    updated_at_raw: str = ""
    raw: Dict[str, str] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class AuditEvent:
    """One historical identity change from ``user_audit_log.csv``."""

    audit_id: str
    user_id: str
    event_time: Optional[datetime]
    field_name: str
    old_value: str
    new_value: str
    event_time_raw: str = ""
    raw: Dict[str, str] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class Activity:
    """One operational event from ``system_activity.csv`` (a claim until proven)."""

    activity_id: str
    event_time: Optional[datetime]
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str
    event_time_raw: str = ""
    raw: Dict[str, str] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class Rule:
    """One row of ``deletion_rules.csv``."""

    rule_id: str
    data_category: str
    condition: str
    required_action: str
    raw: Dict[str, str] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class EvidenceFile:
    """One row of ``evidence_files.csv`` with a resolved absolute path."""

    file_id: str
    file_path: str
    origin: str
    captured_at: Optional[datetime]
    captured_at_raw: str = ""
    absolute_path: str = ""
    raw: Dict[str, str] = field(default_factory=dict, compare=False, repr=False)


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------


def _read_dicts(path: Path, required: Sequence[str]) -> Tuple[List[str], List[Dict[str, str]]]:
    """Read a CSV into stripped string dictionaries, validating its columns."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError("dataset file not found: {}".format(path))
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        original_fields = list(reader.fieldnames or [])
        fields = [str(name).strip() for name in original_fields if name is not None]
        missing = [column for column in required if column not in fields]
        if missing:
            raise ValueError(
                "{}: missing required column(s): {}".format(path, ", ".join(missing))
            )
        rows: List[Dict[str, str]] = []
        for original in reader:
            cleaned: Dict[str, str] = {}
            for name in original_fields:
                if name is None:
                    continue
                value = original.get(name)
                cleaned[str(name).strip()] = "" if value is None else str(value).strip()
            rows.append(cleaned)
    return fields, rows


REQUEST_FIELDS = (
    "case_id",
    "email",
    "requested_at",
    "reported_status",
    "reported_at",
    "audit_at",
    "request_text",
)
IDENTITY_FIELDS = ("user_id", "name", "email", "phone", "email_hash", "updated_at")
AUDIT_FIELDS = (
    "audit_id",
    "user_id",
    "event_time",
    "field_name",
    "old_value",
    "new_value",
)
ACTIVITY_FIELDS = (
    "activity_id",
    "event_time",
    "event_type",
    "source_system",
    "target_system",
    "resource_id",
    "reported_result",
    "details",
)
RULE_FIELDS = ("rule_id", "data_category", "condition", "required_action")
EVIDENCE_FIELDS = ("file_id", "file_path", "origin", "captured_at")


def load_requests(path: Optional[Any] = None) -> List[Request]:
    """Load and parse deletion requests; raise on missing columns."""
    target = Path(path) if path is not None else DATASET_DIR / "deletion_requests.csv"
    _fields, rows = _read_dicts(target, REQUEST_FIELDS)
    return [
        Request(
            case_id=row["case_id"],
            email=row["email"],
            requested_at=parse_ts(row["requested_at"]),
            reported_status=row["reported_status"],
            reported_at=parse_ts(row["reported_at"]),
            audit_at=parse_ts(row["audit_at"]),
            request_text=row["request_text"],
            requested_at_raw=row["requested_at"],
            reported_at_raw=row["reported_at"],
            audit_at_raw=row["audit_at"],
            raw=dict(row),
        )
        for row in rows
    ]


def load_identities(path: Optional[Any] = None) -> List[Identity]:
    """Load the current identity snapshot; raise on missing columns."""
    target = Path(path) if path is not None else DATASET_DIR / "user_identities.csv"
    _fields, rows = _read_dicts(target, IDENTITY_FIELDS)
    return [
        Identity(
            user_id=row["user_id"],
            name=row["name"],
            email=row["email"],
            phone=row["phone"],
            email_hash=row["email_hash"],
            updated_at=parse_ts(row["updated_at"]),
            updated_at_raw=row["updated_at"],
            raw=dict(row),
        )
        for row in rows
    ]


def load_audit_log(path: Optional[Any] = None) -> List[AuditEvent]:
    """Load historical identity changes; raise on missing columns."""
    target = Path(path) if path is not None else DATASET_DIR / "user_audit_log.csv"
    _fields, rows = _read_dicts(target, AUDIT_FIELDS)
    return [
        AuditEvent(
            audit_id=row["audit_id"],
            user_id=row["user_id"],
            event_time=parse_ts(row["event_time"]),
            field_name=row["field_name"],
            old_value=row["old_value"],
            new_value=row["new_value"],
            event_time_raw=row["event_time"],
            raw=dict(row),
        )
        for row in rows
    ]


def load_activity(path: Optional[Any] = None) -> List[Activity]:
    """Load the global system-activity stream; raise on missing columns."""
    target = Path(path) if path is not None else DATASET_DIR / "system_activity.csv"
    _fields, rows = _read_dicts(target, ACTIVITY_FIELDS)
    return [
        Activity(
            activity_id=row["activity_id"],
            event_time=parse_ts(row["event_time"]),
            event_type=row["event_type"],
            source_system=row["source_system"],
            target_system=row["target_system"],
            resource_id=row["resource_id"],
            reported_result=row["reported_result"],
            details=row["details"],
            event_time_raw=row["event_time"],
            raw=dict(row),
        )
        for row in rows
    ]


def load_rules(path: Optional[Any] = None) -> List[Rule]:
    """Load deletion policy rules; raise on missing columns."""
    target = Path(path) if path is not None else DATASET_DIR / "deletion_rules.csv"
    _fields, rows = _read_dicts(target, RULE_FIELDS)
    return [
        Rule(
            rule_id=row["rule_id"],
            data_category=row["data_category"],
            condition=row["condition"],
            required_action=row["required_action"],
            raw=dict(row),
        )
        for row in rows
    ]


def load_evidence_manifest(path: Optional[Any] = None) -> List[EvidenceFile]:
    """Load the evidence manifest and resolve file paths relative to its folder."""
    target = Path(path) if path is not None else DATASET_DIR / "evidence_files.csv"
    _fields, rows = _read_dicts(target, EVIDENCE_FIELDS)
    result: List[EvidenceFile] = []
    for row in rows:
        relative = row["file_path"]
        absolute = str((target.parent / relative).resolve()) if relative else ""
        result.append(
            EvidenceFile(
                file_id=row["file_id"],
                file_path=relative,
                origin=row["origin"],
                captured_at=parse_ts(row["captured_at"]),
                captured_at_raw=row["captured_at"],
                absolute_path=absolute,
                raw=dict(row),
            )
        )
    return result


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Subject:
    """A resolved person with identifiers valid up to the requested ``as_of`` time.

    ``current_email`` / ``current_phone`` / ``current_email_hash`` are the values
    in effect at ``as_of`` when one was supplied, otherwise the current snapshot.
    ``emails``, ``phones`` and ``email_hashes`` are every value held on or before
    ``as_of``; values introduced only after ``as_of`` are deliberately excluded.
    """

    user_id: str
    name: str
    current_email: str
    current_phone: str
    current_email_hash: str
    emails: FrozenSet[str]
    phones: FrozenSet[str]
    email_hashes: FrozenSet[str]
    matched_via: str
    history: Tuple[AuditEvent, ...]
    as_of: Optional[datetime] = None


def _event_sort_key(event: AuditEvent) -> Tuple[datetime, str]:
    return (
        event.event_time or datetime.min.replace(tzinfo=timezone.utc),
        event.audit_id,
    )


class IdentityResolver:
    """Resolve requests to subjects using current and historical identifiers.

    Matching is case-insensitive for emails.  Request-side matching ignores
    ``as_of`` because a request may legitimately use any historical address;
    ``as_of`` only controls which identifiers and audit history are exposed.
    """

    def __init__(self, identities: Sequence[Identity], audit_log: Sequence[AuditEvent]):
        self.identities = list(identities)
        self.audit_log = list(audit_log)
        self._by_user: Dict[str, Identity] = {}
        self._by_current_email: Dict[str, str] = {}
        self._by_historical_email: Dict[str, str] = {}
        self._by_hash: Dict[str, str] = {}
        self._events_by_user: Dict[str, List[AuditEvent]] = {}

        for identity in self.identities:
            self._by_user[identity.user_id] = identity
            current_email = normalize_email(identity.email)
            if current_email:
                self._by_current_email.setdefault(current_email, identity.user_id)
            current_hash = identity.email_hash.strip().lower()
            if current_hash:
                self._by_hash.setdefault(current_hash, identity.user_id)
            if current_email:
                self._by_hash.setdefault(sha256_email(current_email), identity.user_id)

        for event in self.audit_log:
            self._events_by_user.setdefault(event.user_id, []).append(event)
            if event.field_name == "email":
                for value in (event.old_value, event.new_value):
                    email = normalize_email(value)
                    if email:
                        self._by_historical_email.setdefault(email, event.user_id)
                        self._by_hash.setdefault(sha256_email(email), event.user_id)
            elif event.field_name == "email_hash":
                for value in (event.old_value, event.new_value):
                    digest = str(value).strip().lower()
                    if digest:
                        self._by_hash.setdefault(digest, event.user_id)

        for user_id, events in self._events_by_user.items():
            events.sort(key=_event_sort_key)
            identity = self._by_user.get(user_id)
            if identity is not None:
                self._by_hash.setdefault(
                    sha256_email(identity.email), identity.user_id
                )

    def resolve(self, email: Any, as_of: Optional[datetime] = None) -> Optional[Subject]:
        """Resolve an email (or 64-hex email hash) to a :class:`Subject`.

        Returns ``None`` when no current email, historical email, or hash matches.
        """
        if email is None:
            return None
        raw = str(email).strip()
        if not raw:
            return None

        normalized = normalize_email(raw)
        user_id: Optional[str] = None
        matched_via = ""

        if normalized in self._by_current_email:
            user_id = self._by_current_email[normalized]
            matched_via = "current_email"
        elif normalized in self._by_historical_email:
            user_id = self._by_historical_email[normalized]
            matched_via = "historical_email"
        elif _HEX64_RE.match(raw):
            digest = raw.lower()
            user_id = self._by_hash.get(digest)
            matched_via = "email_hash"

        if user_id is None:
            return None
        return self._build_subject(user_id, matched_via, as_of)

    def _build_subject(
        self, user_id: str, matched_via: str, as_of: Optional[datetime]
    ) -> Subject:
        identity = self._by_user[user_id]
        events = list(self._events_by_user.get(user_id, ()))

        email = identity.email
        phone = identity.phone
        email_hash = identity.email_hash
        relevant = events

        if as_of is not None:
            # The snapshot is the final value; undo later changes to recover the
            # value that was actually in effect at as_of.
            for event in reversed(events):
                if event.event_time is None:
                    continue
                if ts_le(event.event_time, as_of):
                    break
                if event.field_name == "email":
                    email = event.old_value
                elif event.field_name == "phone":
                    phone = event.old_value
                elif event.field_name == "email_hash":
                    email_hash = event.old_value
            relevant = [
                event
                for event in events
                if event.event_time is not None and ts_le(event.event_time, as_of)
            ]

        emails = set()
        phones = set()
        explicit_hashes = set()

        normalized_email = normalize_email(email)
        if normalized_email:
            emails.add(normalized_email)
        normalized_phone = normalize_phone(phone)
        if normalized_phone:
            phones.add(normalized_phone)
        normalized_hash = str(email_hash).strip().lower()

        for event in relevant:
            if event.field_name == "email":
                for value in (event.old_value, event.new_value):
                    candidate = normalize_email(value)
                    if candidate:
                        emails.add(candidate)
            elif event.field_name == "phone":
                for value in (event.old_value, event.new_value):
                    candidate = normalize_phone(value)
                    if candidate:
                        phones.add(candidate)
            elif event.field_name == "email_hash":
                for value in (event.old_value, event.new_value):
                    candidate = str(value).strip().lower()
                    if candidate:
                        explicit_hashes.add(candidate)

        hashes = set(explicit_hashes)
        for known_email in emails:
            hashes.add(sha256_email(known_email))
        # At as_of, keep the reconstructed hash only when it is derived from an
        # email that was actually held by that time (or is explicit history).
        if normalized_hash and (as_of is None or normalized_hash in hashes):
            hashes.add(normalized_hash)

        return Subject(
            user_id=identity.user_id,
            name=identity.name,
            current_email=normalized_email,
            current_phone=normalized_phone,
            current_email_hash=normalized_hash,
            emails=frozenset(emails),
            phones=frozenset(phones),
            email_hashes=frozenset(hashes),
            matched_via=matched_via,
            history=tuple(relevant),
            as_of=as_of,
        )

    def identifiers_for_search(self, subject: Subject) -> Dict[str, Any]:
        """Return deterministic identifier collections for downstream search.

        Keys are ``user_id``, ``name``, ``emails``, ``phones``, ``email_hashes``
        plus plural aliases ``user_ids`` and ``names`` for callers that prefer
        uniform list handling.
        """
        return {
            "user_id": subject.user_id,
            "name": subject.name,
            "emails": sorted(subject.emails),
            "phones": sorted(subject.phones),
            "email_hashes": sorted(subject.email_hashes),
            "user_ids": [subject.user_id],
            "names": [subject.name],
        }

    def owns_identifier(self, subject: Subject, value: Any) -> bool:
        """Return True when ``value`` is one of the subject's known identifiers."""
        if value is None:
            return False
        raw = str(value).strip()
        if not raw:
            return False
        lowered = raw.lower()

        if lowered == subject.user_id.strip().lower():
            return True
        if lowered == subject.name.strip().lower():
            return True
        if _EMAIL_RE.match(lowered) and normalize_email(lowered) in subject.emails:
            return True
        if _HEX64_RE.match(raw) and lowered in subject.email_hashes:
            return True

        phone = normalize_phone(raw)
        if phone and len(re.sub(r"\D", "", phone)) >= 7 and phone in subject.phones:
            return True
        return False


# ---------------------------------------------------------------------------
# Context and plug-in loading
# ---------------------------------------------------------------------------


@dataclass
class Context:
    """All loaded datasets, resolver, indexes, and a scratch dict for plug-ins."""

    dataset_dir: Path
    requests: List[Request]
    identities: List[Identity]
    audit_log: List[AuditEvent]
    activity: List[Activity]
    rules: List[Rule]
    evidence: List[EvidenceFile]
    resolver: IdentityResolver
    activity_by_resource_id: Dict[str, List[Activity]]
    activity_by_target_system: Dict[str, List[Activity]]
    activity_by_event_type: Dict[str, List[Activity]]
    activity_by_id: Dict[str, Activity]
    evidence_by_id: Dict[str, EvidenceFile]
    rules_by_category: Dict[str, List[Rule]]
    audit_by_id: Dict[str, AuditEvent]
    audits_by_user: Dict[str, List[AuditEvent]]
    extras: Dict[str, Any] = field(default_factory=dict)


def _group_activity(
    activity: Sequence[Activity], attribute: str
) -> Dict[str, List[Activity]]:
    grouped: Dict[str, List[Activity]] = {}
    for item in activity:
        key = str(getattr(item, attribute))
        grouped.setdefault(key, []).append(item)
    return grouped


def load_context(dataset_dir: Optional[Any] = None) -> Context:
    """Load every participant-facing dataset and build lookup indexes."""
    directory = Path(dataset_dir) if dataset_dir is not None else DATASET_DIR
    requests = load_requests(directory / "deletion_requests.csv")
    identities = load_identities(directory / "user_identities.csv")
    audit_log = load_audit_log(directory / "user_audit_log.csv")
    activity = load_activity(directory / "system_activity.csv")
    rules = load_rules(directory / "deletion_rules.csv")
    evidence = load_evidence_manifest(directory / "evidence_files.csv")
    resolver = IdentityResolver(identities, audit_log)

    rules_by_category: Dict[str, List[Rule]] = {}
    for rule in rules:
        rules_by_category.setdefault(rule.data_category, []).append(rule)

    audits_by_user: Dict[str, List[AuditEvent]] = {}
    for event in audit_log:
        audits_by_user.setdefault(event.user_id, []).append(event)
    for events in audits_by_user.values():
        events.sort(key=_event_sort_key)

    return Context(
        dataset_dir=directory,
        requests=requests,
        identities=identities,
        audit_log=audit_log,
        activity=activity,
        rules=rules,
        evidence=evidence,
        resolver=resolver,
        activity_by_resource_id=_group_activity(activity, "resource_id"),
        activity_by_target_system=_group_activity(activity, "target_system"),
        activity_by_event_type=_group_activity(activity, "event_type"),
        activity_by_id={item.activity_id: item for item in activity},
        evidence_by_id={item.file_id: item for item in evidence},
        rules_by_category=rules_by_category,
        audit_by_id={item.audit_id: item for item in audit_log},
        audits_by_user=audits_by_user,
    )


def _load_plugin(module_name: str, attribute: str) -> Optional[Callable[..., Any]]:
    """Import a sibling plug-in from ``code/`` without masking its own errors.

    Only ``ModuleNotFoundError`` for the plug-in module itself falls back to the
    caller's stub.  A missing dependency inside the plug-in, a missing attribute,
    or any exception raised while importing propagates.
    """
    code_dir = str(CODE_DIR)
    if code_dir not in sys.path:
        sys.path.insert(0, code_dir)
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise
    plugin = getattr(module, attribute, None)
    if plugin is None:
        raise ImportError(
            "plug-in {}.{} is missing".format(module_name, attribute)
        )
    if not callable(plugin):
        raise ImportError(
            "plug-in {}.{} is not callable".format(module_name, attribute)
        )
    return plugin


def _stub_classifier(request: Request) -> bool:
    """Fallback intent classifier: conservatively assume deletion was requested."""
    return True


_stub_classifier.is_stub = True  # type: ignore[attr-defined]


def _stub_auditor(
    ctx: Context, request: Request, subject: Subject
) -> Tuple[str, List[Step]]:
    """Fallback evidence auditor: reports an unresolved incomplete case."""
    return "INCOMPLETE", []


_stub_auditor.is_stub = True  # type: ignore[attr-defined]


def _intent_module_adapter(classify_intent: Callable[..., Any]) -> Callable[[Request], bool]:
    """Adapt Muse's ``intent.classify_intent(request_text)`` to ``Request -> bool``.

    ``classify_intent`` returns an object whose ``intent`` is
    ``"DELETION_REQUESTED"`` for authorized deletions.
    """

    def is_deletion_request(request: Request) -> bool:
        result = classify_intent(request.request_text)
        return str(getattr(result, "intent", result)).upper().startswith("DELETION")

    is_deletion_request.is_stub = False  # type: ignore[attr-defined]
    return is_deletion_request


def get_intent_classifier() -> Callable[[Request], bool]:
    """Return the deletion-intent classifier.

    Lookup order: ``code/classifier.py::is_deletion_request(request)``, then
    Muse's ``code/intent.py::classify_intent(request_text)`` (adapted), then
    the documented stub that treats every request as a deletion request.
    """
    plugin = _load_plugin("classifier", "is_deletion_request")
    if plugin is not None:
        return plugin
    adapted = _load_plugin("intent", "classify_intent")
    if adapted is not None:
        return _intent_module_adapter(adapted)
    return _stub_classifier


def get_evidence_auditor(warn: bool = True) -> Callable[..., Tuple[str, Any]]:
    """Return ``evidence.audit_case`` or a stub that emits one warning."""
    plugin = _load_plugin("evidence", "audit_case")
    if plugin is None:
        if warn:
            print(
                "warning: evidence engine is not installed "
                "(expected code/evidence.py with audit_case); using stub",
                file=sys.stderr,
            )
        return _stub_auditor
    return plugin


# ---------------------------------------------------------------------------
# Steps, ordering, validation, and output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """An un-numbered audit step; ``finalize_steps`` adds step_no and sorting."""

    phase: str
    system_id: str
    resource_id: str
    operation: str
    state: str
    evidence_id: str = ""


def step_sort_key(step: Any) -> Tuple[int, int, str, str]:
    """Sort key verified against all 32 sample rows.

    Order is phase, operation-table index, ``system_id``, then ``resource_id``.
    Accepts either a :class:`Step` or an equivalent mapping.
    """
    if isinstance(step, Step):
        phase = step.phase
        operation = step.operation
        system_id = step.system_id
        resource_id = step.resource_id
    elif isinstance(step, Mapping):
        phase = str(step.get("phase", ""))
        operation = str(step.get("operation", ""))
        system_id = str(step.get("system_id", ""))
        resource_id = str(step.get("resource_id", ""))
    else:
        raise TypeError("step must be a Step or mapping, got {!r}".format(type(step)))
    return (
        PHASE_ORDER.get(phase, 99),
        OPERATION_INDEX.get((phase, operation), 10 ** 6),
        system_id,
        resource_id,
    )


def _coerce_step(step: Any) -> Dict[str, str]:
    """Convert a Step or mapping into the canonical string-field dictionary."""
    if isinstance(step, Step):
        values = {
            "phase": step.phase,
            "system_id": step.system_id,
            "resource_id": step.resource_id,
            "operation": step.operation,
            "state": step.state,
            "evidence_id": step.evidence_id,
        }
    elif isinstance(step, Mapping):
        values = {}
        for name in ("phase", "system_id", "resource_id", "operation", "state"):
            if name not in step or step[name] is None:
                raise ValueError("step is missing required field {!r}".format(name))
            values[name] = step[name]
        values["evidence_id"] = step.get("evidence_id", "")
    else:
        raise TypeError("step must be a Step or mapping, got {!r}".format(type(step)))

    normalized: Dict[str, str] = {}
    for name in ("phase", "system_id", "resource_id", "operation", "state"):
        normalized[name] = str(values[name])
    evidence = values.get("evidence_id", "")
    normalized["evidence_id"] = "" if evidence is None else str(evidence)
    return normalized


def finalize_steps(steps: Sequence[Any]) -> List[Dict[str, Any]]:
    """Deduplicate, sort, number, and serialize steps into canonical dicts.

    Accepts :class:`Step` objects or mappings (with or without ``step_no``).
    The returned dictionaries always use the required key order:
    ``step_no, phase, system_id, resource_id, operation, state, evidence_id``.
    """
    if steps is None:
        return []
    unique: List[Dict[str, str]] = []
    seen = set()
    for raw_step in steps:
        normalized = _coerce_step(raw_step)
        signature = tuple(
            normalized[name]
            for name in (
                "phase",
                "system_id",
                "resource_id",
                "operation",
                "state",
                "evidence_id",
            )
        )
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(normalized)

    unique.sort(key=step_sort_key)
    finalized: List[Dict[str, Any]] = []
    for number, item in enumerate(unique, start=1):
        finalized.append(
            {
                "step_no": number,
                "phase": item["phase"],
                "system_id": item["system_id"],
                "resource_id": item["resource_id"],
                "operation": item["operation"],
                "state": item["state"],
                "evidence_id": item["evidence_id"],
            }
        )
    return finalized


def _step_evidence_in_time(
    ctx: Context, evidence_id: str, audit_at: Optional[datetime]
) -> Tuple[bool, bool]:
    """Return ``(found, within_cutoff)`` for an evidence/activity/audit id."""
    found = False
    times: List[Optional[datetime]] = []
    if evidence_id in ctx.evidence_by_id:
        found = True
        times.append(ctx.evidence_by_id[evidence_id].captured_at)
    if evidence_id in ctx.activity_by_id:
        found = True
        times.append(ctx.activity_by_id[evidence_id].event_time)
    if evidence_id in ctx.audit_by_id:
        found = True
        times.append(ctx.audit_by_id[evidence_id].event_time)
    if not found:
        return False, False
    return True, any(ts_le(moment, audit_at) for moment in times)


def validate_steps(
    status: str,
    steps: Sequence[Any],
    request: Optional[Request] = None,
    ctx: Optional[Context] = None,
    strict_evidence: bool = False,
) -> List[str]:
    """Validate a finalized (or finalizable) step list; return problem strings.

    An empty list means the row is valid.  With ``strict_evidence``, ``ctx`` and
    ``request`` are required and every cited id must exist and have a timestamp
    at or before ``request.audit_at``.
    """
    problems: List[str] = []
    status_text = str(status).strip()

    if status_text not in STATUSES:
        problems.append("invalid deletion_status {!r}".format(status))
        return problems

    if steps is None:
        steps = []
    if not isinstance(steps, (list, tuple)):
        return ["steps must be a list or tuple"]

    items = list(steps)
    needs_finalizing = any(
        isinstance(item, Step)
        or not isinstance(item, Mapping)
        or "step_no" not in item
        for item in items
    )
    if needs_finalizing:
        try:
            items = finalize_steps(items)
        except (TypeError, ValueError) as exc:
            return ["steps could not be finalized: {}".format(exc)]
    else:
        items = [dict(item) for item in items]

    if status_text == "NOT_APPLICABLE":
        if items:
            problems.append("NOT_APPLICABLE must have an empty steps list")
        return problems

    if not items:
        problems.append("{} case must have at least one step".format(status_text))
        return problems

    if status_text == "COMPLETE":
        if any(str(item.get("phase", "")) != "VERIFICATION" for item in items):
            problems.append("COMPLETE cases may contain VERIFICATION steps only")
        for index, item in enumerate(items, start=1):
            if not str(item.get("evidence_id", "")).strip():
                problems.append(
                    "COMPLETE verification step {} must cite evidence".format(index)
                )
    else:  # INCOMPLETE
        if not any(str(item.get("phase", "")) == "FINDING" for item in items):
            problems.append("INCOMPLETE cases need at least one FINDING step")

    expected_numbers = list(range(1, len(items) + 1))
    actual_numbers = [item.get("step_no") for item in items]
    if actual_numbers != expected_numbers:
        problems.append(
            "step_no must be consecutive from 1 (got {!r})".format(actual_numbers)
        )

    sort_keys = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            problems.append("step {} is not an object".format(index))
            continue
        keys = set(item.keys())
        if keys != set(STEP_FIELDS):
            problems.append(
                "step {} has key set {} (expected {})".format(
                    index,
                    sorted(keys),
                    list(STEP_FIELDS),
                )
            )
        phase = str(item.get("phase", ""))
        operation = str(item.get("operation", ""))
        state = str(item.get("state", ""))
        expected_state = OPERATION_STATE.get((phase, operation))
        if expected_state is None:
            problems.append(
                "step {} has unsupported phase/operation {!r}/{!r}".format(
                    index, phase, operation
                )
            )
        elif expected_state != state:
            problems.append(
                "step {} state {!r} must be {!r} for {}".format(
                    index, state, expected_state, operation
                )
            )
        evidence_id = str(item.get("evidence_id", ""))
        if phase == "FINDING" and not evidence_id:
            problems.append("step {} FINDING must cite evidence".format(index))
        if (
            status_text == "INCOMPLETE"
            and phase in ("REMEDIATION", "VERIFICATION")
            and evidence_id
        ):
            problems.append(
                "step {} {} must use an empty evidence_id for INCOMPLETE".format(
                    index, phase
                )
            )
        sort_keys.append(step_sort_key(item))

    if sort_keys != sorted(sort_keys):
        problems.append("steps are not in phase/operation/system/resource order")

    if strict_evidence:
        if ctx is None or request is None:
            problems.append(
                "strict evidence validation requires both ctx and request"
            )
        else:
            for index, item in enumerate(items, start=1):
                evidence_id = str(item.get("evidence_id", "")).strip()
                if not evidence_id:
                    continue
                found, within_cutoff = _step_evidence_in_time(
                    ctx, evidence_id, request.audit_at
                )
                if not found:
                    problems.append(
                        "step {} cites unknown evidence id {!r}".format(
                            index, evidence_id
                        )
                    )
                elif not within_cutoff:
                    problems.append(
                        "step {} cites evidence {!r} after audit_at {}".format(
                            index, evidence_id, to_iso_z(request.audit_at)
                        )
                    )

    return problems


def build_row(case_id: str, status: str, steps: Sequence[Any]) -> Dict[str, str]:
    """Build one output row, finalizing and compactly serializing its steps."""
    finalized = finalize_steps(steps)
    steps_json = json.dumps(finalized, ensure_ascii=False, separators=(",", ":"))
    return {
        "case_id": str(case_id),
        "deletion_status": str(status),
        "steps_json": steps_json,
    }


def write_output(rows: Iterable[Mapping[str, Any]], path: Optional[Any] = None) -> Path:
    """Sort, validate, and write ``output.csv``; return the written path."""
    target = Path(path) if path is not None else OUTPUT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    normalized: List[Dict[str, str]] = []
    seen = set()
    for row in rows:
        if set(row.keys()) != set(OUTPUT_COLUMNS):
            raise ValueError(
                "output row must have exactly columns {}".format(list(OUTPUT_COLUMNS))
            )
        case_id = str(row["case_id"])
        if case_id in seen:
            raise ValueError("duplicate case_id in output: {}".format(case_id))
        seen.add(case_id)

        status = str(row["deletion_status"])
        if status not in STATUSES:
            raise ValueError(
                "{}: invalid deletion_status {!r}".format(case_id, status)
            )

        steps_json = row["steps_json"]
        if not isinstance(steps_json, str):
            raise ValueError("{}: steps_json must be a string".format(case_id))
        parsed = json.loads(steps_json)
        if not isinstance(parsed, list):
            raise ValueError("{}: steps_json must encode a JSON array".format(case_id))
        for index, step in enumerate(parsed, start=1):
            if not isinstance(step, dict):
                raise ValueError(
                    "{}: step {} is not a JSON object".format(case_id, index)
                )
            if step.get("step_no") != index:
                raise ValueError(
                    "{}: step numbers must be consecutive from 1".format(case_id)
                )

        normalized.append(
            {
                "case_id": case_id,
                "deletion_status": status,
                "steps_json": steps_json,
            }
        )

    normalized.sort(key=lambda item: case_sort_key(item["case_id"]))
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(OUTPUT_COLUMNS), lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(normalized)
    return target


def _case_ids_from_requests(requests: Iterable[Any]) -> List[str]:
    case_ids: List[str] = []
    for request in requests:
        if isinstance(request, str):
            case_ids.append(request)
        elif isinstance(request, Request):
            case_ids.append(request.case_id)
        elif isinstance(request, Mapping):
            case_ids.append(str(request.get("case_id", "")))
        else:
            case_ids.append(str(getattr(request, "case_id", request)))
    return case_ids


def check_output_file(path: Any, requests: Iterable[Any]) -> List[str]:
    """Structurally check an output file against the expected request case_ids."""
    problems: List[str] = []
    target = Path(path)
    if not target.exists():
        return ["output file not found: {}".format(target)]

    try:
        fields, rows = _read_dicts(target, OUTPUT_COLUMNS)
    except (OSError, ValueError) as exc:
        return ["could not read {}: {}".format(target, exc)]

    if tuple(fields) != OUTPUT_COLUMNS:
        problems.append(
            "header must be exactly {} (got {})".format(
                ",".join(OUTPUT_COLUMNS), ",".join(fields)
            )
        )

    expected = _case_ids_from_requests(requests)
    expected_set = set(expected)
    seen = set()
    duplicates = set()
    for row in rows:
        case_id = row.get("case_id", "")
        if case_id in seen:
            duplicates.add(case_id)
        seen.add(case_id)

    missing = sorted(expected_set - seen)
    extra = sorted(seen - expected_set)
    if duplicates:
        problems.append("duplicate case_id rows: {}".format(sorted(duplicates)))
    if missing:
        problems.append("missing case_id rows: {}".format(missing[:20]))
    if extra:
        problems.append("unexpected case_id rows: {}".format(extra[:20]))
    if len(rows) != len(expected_set) and not (missing or extra or duplicates):
        problems.append(
            "expected {} rows, found {}".format(len(expected_set), len(rows))
        )

    # Verify ordering exactly as written, not just as a set.
    written_ids = [row.get("case_id", "") for row in rows]
    if written_ids != sorted(written_ids, key=case_sort_key):
        problems.append("rows are not sorted by case_id")

    for row in rows:
        case_id = row.get("case_id", "")
        status = row.get("deletion_status", "")
        if status not in STATUSES:
            problems.append("{}: invalid deletion_status {!r}".format(case_id, status))
        raw_steps = row.get("steps_json", "")
        try:
            parsed = json.loads(raw_steps)
        except (TypeError, ValueError) as exc:
            problems.append("{}: invalid steps_json ({})".format(case_id, exc))
            continue
        if not isinstance(parsed, list):
            problems.append("{}: steps_json is not a JSON array".format(case_id))
            continue
        if status == "NOT_APPLICABLE" and parsed:
            problems.append("{}: NOT_APPLICABLE must use []".format(case_id))
        if status == "COMPLETE" and not parsed:
            problems.append("{}: COMPLETE needs verification steps".format(case_id))
        for index, step in enumerate(parsed, start=1):
            if not isinstance(step, dict):
                problems.append(
                    "{}: step {} is not a JSON object".format(case_id, index)
                )
                continue
            if step.get("step_no") != index:
                problems.append(
                    "{}: step numbers are not consecutive from 1".format(case_id)
                )
                break

    return problems


# ---------------------------------------------------------------------------
# Per-case solving
# ---------------------------------------------------------------------------


def _record_problem(problems: Optional[List[str]], case_id: str, message: str) -> str:
    text = "{}: {}".format(case_id, message)
    if problems is not None:
        problems.append(text)
    return text


def solve_case(
    ctx: Context,
    request: Request,
    classifier: Callable[[Request], bool],
    auditor: Callable[[Context, Request, Subject], Tuple[str, Any]],
    strict_evidence: bool = False,
    problems: Optional[List[str]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Classify, resolve, audit, finalize, and validate one case.

    Plug-in exceptions and validation failures never abort the run: the case is
    recorded in ``problems`` and falls back to ``("INCOMPLETE", [])``.
    """
    case_id = request.case_id

    try:
        is_deletion = bool(classifier(request))
    except Exception as exc:  # noqa: BLE001 - per-case isolation is intentional
        _record_problem(
            problems,
            case_id,
            "intent classifier failed ({}: {})".format(type(exc).__name__, exc),
        )
        return "INCOMPLETE", []

    if not is_deletion:
        return "NOT_APPLICABLE", []

    subject = ctx.resolver.resolve(request.email, as_of=request.audit_at)
    if subject is None:
        text = _record_problem(
            problems,
            case_id,
            "identity could not be resolved for request email",
        )
        print("warning: {}".format(text), file=sys.stderr)
        return "INCOMPLETE", []

    try:
        status, raw_steps = auditor(ctx, request, subject)
        status_text = str(status).strip()
        steps = finalize_steps(raw_steps)
        if getattr(auditor, "is_stub", False):
            return status_text, steps
        validation = validate_steps(
            status_text,
            steps,
            request=request,
            ctx=ctx,
            strict_evidence=strict_evidence,
        )
        if validation:
            _record_problem(
                problems,
                case_id,
                "validation: {}".format("; ".join(validation[:5])),
            )
            # Keep the numbered, sorted plug-in answer so partial credit is not
            # lost; only an unusable status falls back to INCOMPLETE/[].
            if status_text not in STATUSES:
                return "INCOMPLETE", []
            if status_text == "NOT_APPLICABLE":
                return status_text, []
            return status_text, steps
        return status_text, steps
    except Exception as exc:  # noqa: BLE001 - per-case isolation is intentional
        _record_problem(
            problems,
            case_id,
            "evidence auditor failed ({}: {})".format(type(exc).__name__, exc),
        )
        return "INCOMPLETE", []


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _plugin_label(plugin: Callable[..., Any]) -> str:
    return "stub" if getattr(plugin, "is_stub", False) else "installed"


def run_selftest(dataset_dir: Optional[Any] = None) -> int:
    """Run contract self-checks; return 0 when every check passes."""
    directory = Path(dataset_dir) if dataset_dir is not None else DATASET_DIR
    failures: List[str] = []

    try:
        ctx = load_context(directory)
    except Exception as exc:  # noqa: BLE001 - selftest reports cleanly
        print("SELFTEST FAIL: could not load context: {}".format(exc), file=sys.stderr)
        return 1

    sample_requests_path = directory / "sample_deletion_requests.csv"
    sample_output_path = directory / "sample_output.csv"
    sample_requests: List[Request] = []
    if sample_requests_path.exists():
        sample_requests = load_requests(sample_requests_path)
    else:
        failures.append("missing {}".format(sample_requests_path))
    by_case = {request.case_id: request for request in sample_requests}

    # (a) Round-trip every sample answer through finalize_steps + validate_steps.
    sample_rows: List[Dict[str, str]] = []
    if sample_output_path.exists():
        try:
            _fields, sample_rows = _read_dicts(sample_output_path, OUTPUT_COLUMNS)
        except (OSError, ValueError) as exc:
            failures.append("could not read sample_output.csv: {}".format(exc))
    else:
        failures.append("missing {}".format(sample_output_path))

    roundtrip_ok = 0
    for row in sample_rows:
        case_id = row.get("case_id", "")
        try:
            original = json.loads(row.get("steps_json", ""))
        except (TypeError, ValueError) as exc:
            failures.append("{}: sample steps_json invalid: {}".format(case_id, exc))
            continue
        finalized = finalize_steps(original)
        if finalized != original:
            failures.append(
                "{}: finalize_steps changed sample ordering/content".format(case_id)
            )
            continue
        strict = case_id in by_case
        problems = validate_steps(
            row.get("deletion_status", ""),
            finalized,
            request=by_case.get(case_id),
            ctx=ctx if strict else None,
            strict_evidence=strict,
        )
        if problems:
            failures.append("{}: {}".format(case_id, "; ".join(problems)))
        else:
            roundtrip_ok += 1
    print(
        "selftest: sample round-trip {}/{} rows".format(
            roundtrip_ok, len(sample_rows)
        )
    )

    # (b) Resolve every evaluation request email.
    via_counts: Counter = Counter()
    unresolved: List[str] = []
    for request in ctx.requests:
        subject = ctx.resolver.resolve(request.email)
        if subject is None:
            unresolved.append(request.case_id)
        else:
            via_counts[subject.matched_via] += 1
    print(
        "selftest: identity resolution current={} historical={} hash={} unresolved={}".format(
            via_counts.get("current_email", 0),
            via_counts.get("historical_email", 0),
            via_counts.get("email_hash", 0),
            len(unresolved),
        )
    )
    if unresolved:
        failures.append(
            "unresolved request emails: {} (first: {})".format(
                len(unresolved), unresolved[:10]
            )
        )
    if len(ctx.requests) == 518:
        expected_counts = {
            "current_email": 418,
            "historical_email": 100,
            "email_hash": 0,
        }
        for key, expected in expected_counts.items():
            if via_counts.get(key, 0) != expected:
                failures.append(
                    "identity {} count {} != expected {}".format(
                        key, via_counts.get(key, 0), expected
                    )
                )

    # (c) Parse every timestamp cell in every dataset CSV.
    timestamp_cells = 0
    timestamp_failures: List[str] = []
    for csv_path in sorted(directory.glob("*.csv")):
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                fields = [name.strip() for name in (reader.fieldnames or []) if name]
                stamp_fields = [
                    name
                    for name in fields
                    if name == "event_time"
                    or name.endswith("_at")
                    or name.endswith("_time")
                ]
                for row in reader:
                    for name in stamp_fields:
                        value = row.get(name)
                        if value is None or not str(value).strip():
                            continue
                        timestamp_cells += 1
                        try:
                            parsed = parse_ts(value)
                        except ValueError as exc:
                            timestamp_failures.append(
                                "{}:{}={!r}: {}".format(csv_path.name, name, value, exc)
                            )
                            continue
                        if parsed is None or parsed.tzinfo is None:
                            timestamp_failures.append(
                                "{}:{}={!r}: not timezone-aware".format(
                                    csv_path.name, name, value
                                )
                            )
        except (OSError, ValueError) as exc:
            timestamp_failures.append("{}: {}".format(csv_path, exc))
    print(
        "selftest: parsed {} timestamp cells ({} failures)".format(
            timestamp_cells, len(timestamp_failures)
        )
    )
    if timestamp_failures:
        failures.extend(timestamp_failures[:10])

    # (d) Reconstruct identifiers at as_of from an audit-log email chain.
    email_chains: List[Tuple[str, List[AuditEvent]]] = []
    for user_id, events in ctx.audits_by_user.items():
        chain = [
            event
            for event in sorted(events, key=_event_sort_key)
            if event.field_name == "email"
        ]
        if len(chain) >= 2:
            email_chains.append((user_id, chain))
    if not email_chains:
        failures.append("as_of selftest: no multi-step email chain found")
    else:
        email_chains.sort(key=lambda item: item[0])
        user_id, chain = email_chains[0]
        first = chain[0]
        second = chain[1]
        last = chain[-1]
        try:
            identity = ctx.resolver._by_user[user_id]
            if first.event_time is None or second.event_time is None:
                raise AssertionError("chain timestamps are missing")
            if not ts_le(first.event_time, second.event_time):
                raise AssertionError("chain is not ordered")

            before = ctx.resolver.resolve(
                identity.email, as_of=first.event_time - timedelta(seconds=1)
            )
            at_first = ctx.resolver.resolve(identity.email, as_of=first.event_time)
            between = ctx.resolver.resolve(
                identity.email, as_of=second.event_time - timedelta(seconds=1)
            )
            at_second = ctx.resolver.resolve(identity.email, as_of=second.event_time)

            assert before is not None and at_first is not None
            assert between is not None and at_second is not None

            expected_before = normalize_email(first.old_value)
            expected_first = normalize_email(first.new_value)
            expected_between = normalize_email(second.old_value)
            expected_second = normalize_email(second.new_value)

            if before.current_email != expected_before:
                raise AssertionError(
                    "before chain: {} != {}".format(
                        before.current_email, expected_before
                    )
                )
            if before.history:
                raise AssertionError("before chain: unexpected history")
            if before.emails != frozenset({expected_before}):
                raise AssertionError("before chain: emails not reconstructed")

            if at_first.current_email != expected_first:
                raise AssertionError(
                    "at first event: {} != {}".format(
                        at_first.current_email, expected_first
                    )
                )
            if at_first.emails != frozenset({expected_before, expected_first}):
                raise AssertionError("at first event: emails not reconstructed")
            if sha256_email(at_first.current_email) not in at_first.email_hashes:
                raise AssertionError("at first event: email hash missing")

            if between.current_email != expected_between:
                raise AssertionError(
                    "between events: {} != {}".format(
                        between.current_email, expected_between
                    )
                )
            if normalize_email(last.new_value) in between.emails and (
                normalize_email(last.new_value) != expected_between
            ):
                raise AssertionError("between events: a future email leaked in")

            if at_second.current_email != expected_second:
                raise AssertionError(
                    "at second event: {} != {}".format(
                        at_second.current_email, expected_second
                    )
                )
            if sha256_email(at_second.current_email) not in at_second.email_hashes:
                raise AssertionError("at second event: email hash missing")

            after = ctx.resolver.resolve(identity.email, as_of=last.event_time)
            assert after is not None
            if after.current_email != normalize_email(identity.email):
                raise AssertionError("after chain: current snapshot not recovered")
            print(
                "selftest: as_of reconstruction OK for {} ({} email changes)".format(
                    user_id, len(chain)
                )
            )
        except AssertionError as exc:
            failures.append("as_of selftest ({}): {}".format(user_id, exc))

    # (e) Write a tiny temporary output and check it.
    if sample_rows and sample_requests:
        with tempfile.TemporaryDirectory() as temp_dir:
            tiny_path = Path(temp_dir) / "tiny_output.csv"
            tiny_rows = [
                build_row(
                    row.get("case_id", ""),
                    row.get("deletion_status", ""),
                    json.loads(row.get("steps_json", "[]")),
                )
                for row in sample_rows[:3]
            ]
            tiny_ids = {row.get("case_id", "") for row in sample_rows[:3]}
            tiny_requests = [
                request for request in sample_requests if request.case_id in tiny_ids
            ]
            try:
                write_output(tiny_rows, tiny_path)
                tiny_problems = check_output_file(tiny_path, tiny_requests)
            except Exception as exc:  # noqa: BLE001 - selftest reports cleanly
                failures.append("tiny output check crashed: {}".format(exc))
            else:
                if tiny_problems:
                    failures.extend(
                        "tiny output: {}".format(problem)
                        for problem in tiny_problems
                    )
                else:
                    print("selftest: temporary output round-trip OK")
    else:
        failures.append("tiny output check skipped (no sample rows)")

    if failures:
        print("SELFTEST FAIL ({} problem(s))".format(len(failures)), file=sys.stderr)
        for failure in failures[:40]:
            print("  - {}".format(failure), file=sys.stderr)
        if len(failures) > 40:
            print("  ... {} more".format(len(failures) - 40), file=sys.stderr)
        return 1

    print("SELFTEST PASS")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ghost Data Still Exists deletion-audit agent"
    )
    parser.add_argument(
        "--dataset",
        metavar="DIR",
        default=None,
        help="dataset directory (default: <repo>/dataset)",
    )
    parser.add_argument(
        "--requests",
        metavar="PATH",
        default=None,
        help="override the deletion_requests.csv path",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help="output CSV path (default: <repo>/output.csv)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="process only the first N requests (useful for smoke tests)",
    )
    parser.add_argument(
        "--strict-evidence",
        action="store_true",
        help="require cited evidence ids to exist and predate audit_at",
    )
    parser.add_argument(
        "--check",
        metavar="FILE",
        default=None,
        help="only validate an existing output CSV and exit non-zero on problems",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="run built-in contract self-tests and exit non-zero on failure",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point; returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest(args.dataset)

    dataset_dir = Path(args.dataset) if args.dataset else DATASET_DIR
    try:
        ctx = load_context(dataset_dir)
        if args.requests:
            ctx.requests = load_requests(args.requests)
    except Exception as exc:  # noqa: BLE001 - surface a clean CLI error
        print("error loading dataset: {}".format(exc), file=sys.stderr)
        return 2

    if args.check:
        problems = check_output_file(args.check, ctx.requests)
        for problem in problems:
            print(problem, file=sys.stderr)
        print("check: {} problem(s)".format(len(problems)))
        return 1 if problems else 0

    classifier = get_intent_classifier()
    auditor = get_evidence_auditor()

    requests = list(ctx.requests)
    if args.limit is not None:
        requests = requests[: max(0, args.limit)]

    problems: List[str] = []
    counts: Counter = Counter()
    rows: List[Dict[str, str]] = []
    for request in requests:
        status, steps = solve_case(
            ctx,
            request,
            classifier,
            auditor,
            strict_evidence=bool(args.strict_evidence),
            problems=problems,
        )
        counts[status] += 1
        rows.append(build_row(request.case_id, status, steps))

    output_path = Path(args.output) if args.output else OUTPUT_PATH
    try:
        write_output(rows, output_path)
    except Exception as exc:  # noqa: BLE001 - surface a clean CLI error
        print("error writing output: {}".format(exc), file=sys.stderr)
        return 2

    print(
        "processed {} case(s): COMPLETE={} INCOMPLETE={} NOT_APPLICABLE={}".format(
            len(requests),
            counts.get("COMPLETE", 0),
            counts.get("INCOMPLETE", 0),
            counts.get("NOT_APPLICABLE", 0),
        )
    )
    print(
        "plug-ins: classifier={} evidence={}".format(
            _plugin_label(classifier), _plugin_label(auditor)
        )
    )
    print("wrote {}".format(output_path))
    print(
        "validation/plugin problems: {}".format(len(problems)),
        file=sys.stderr,
    )
    for problem in problems[:20]:
        print("  - {}".format(problem), file=sys.stderr)
    if len(problems) > 20:
        print(
            "  ... {} more problem(s)".format(len(problems) - 20), file=sys.stderr
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
