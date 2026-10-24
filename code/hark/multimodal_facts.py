"""Adapter: Muse's extracted multimodal facts (code/multimodal.py) -> Hark engine inputs.

Source of truth is code/extracted/{documents,images}.json via code/multimodal.py.
There is no hand-written fallback: a resource the extraction does not cover is simply not a
multimodal fact (the hand-read multimodal_hints.py placeholders were removed for generality).

Exports
  FACTS[rid]     -> list of (file_id, system_id, treatment, flags) one per artifact showing rid
                    (audit_record_003 has two: evidence_55 PDF and evidence_37 PNG). The engine picks the
                    latest one whose manifest captured_at <= audit_at.
                    treatment: DELETE | REDACT | ANONYMIZE | RETAIN | FLOW | BACKUP
                    flags: present / pii / enabled / restore_allowed / retention_until (naive UTC datetime)
  DOC_OWNERS[rid]-> (file_id, {emails})  PDF documents with no LINK_RESOURCE_IDENTITY activity, owned via
                    the subject email printed in the document (support_001 / evidence_58).
  RETENTION      -> list of dicts {evidence_id, system_id, resource_id, user_id, order_id, retain_until}
                    from multimodal.retention_entries() (evidence_27 register, evidence_55/37 audit log).
  SOURCE[rid]    -> "extracted"
"""
import os
import re
import sys
from datetime import datetime

_CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CODE not in sys.path:
    sys.path.insert(0, _CODE)

import multimodal  # noqa: E402  (code/multimodal.py, Muse)

# resource_type (as extracted) -> deletion_rules.csv action family used by Engine.plan
TYPE_TREATMENT = {
    "user_profile": "DELETE",                 # rule_01
    "search_index_entry": "DELETE",           # rule_03
    "user_export": "DELETE",                  # rule_09
    "customer_statement": "DELETE",           # copy of personal data, treated as user export (rule_09)
    "marketing_profile": "DELETE",            # rule_10
    "object_storage_listing": "DELETE",       # support-attachment objects owned by the subject (rule_06)
    "support_attachment": "DELETE",           # rule_06
    "support_message": "REDACT",              # rule_05
    "analytics_profile": "ANONYMIZE",         # rule_07
    "account_tombstone_reference": "REDACT",  # rule_15
    "security_audit_log": "RETAIN",           # rule_12
    "retention_register": "RETAIN",           # rule_12/13 family
    "data_flow": "FLOW",
    "backup_archive": "BACKUP",               # rule_14
}
_NON_PII = {"user_id", "order_id"}
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_RETAIN_FACT = re.compile(r"Retain until (\d{1,2}) (\w{3}) (\d{4})(?: (\d{2}):(\d{2}) UTC)?")


def _parse_retain(text):
    m = _RETAIN_FACT.search(text)
    if not m or m.group(2) not in _MONTHS:
        return None
    return datetime(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)),
                    int(m.group(4) or 0), int(m.group(5) or 0))


def _retain_until(entry, res):
    for fact in res.get("facts", []):
        t = _parse_retain(fact)
        if t:
            return t
    for flag in entry.get("policy_flags", []):
        m = re.match(r"retain_until_(\d{4})-(\d{2})-(\d{2})$", flag)
        if m:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _flags(entry, res):
    state = (res.get("state_at_capture") or "").upper()
    pflags = set(entry.get("policy_flags", []))
    exposed = set()
    for s in entry.get("subjects", []):
        exposed |= set(s.get("identifiers_exposed", []))
    flags = dict(present=not state.startswith("ABSENT"),
                 pii=bool(exposed - _NON_PII))
    rtype = res.get("resource_type")
    if rtype == "data_flow":
        flags["enabled"] = state == "ACTIVE" or "active_data_flow" in pflags
    if rtype == "backup_archive":
        flags["restore_allowed"] = "restore_risk" in pflags
    ru = _retain_until(entry, res)
    if ru is not None and TYPE_TREATMENT.get(rtype) == "RETAIN":
        flags["retention_until"] = ru
    return flags


def build(link_rids=()):
    """link_rids: resource ids that have a LINK_RESOURCE_IDENTITY activity (they are linked by activity,
    so their documents need no owner-by-email rule)."""
    facts, owners, retention, source = {}, {}, [], {}
    entries = multimodal.load_extracted_facts()
    for entry in entries:
        fid = entry["evidence_id"]
        for res in entry.get("resources", []):
            rid, rtype = res["resource_id"], res.get("resource_type")
            tr = TYPE_TREATMENT.get(rtype)
            if tr is None:
                continue
            if rtype == "retention_register":
                continue  # not a per-case resource; used through RETENTION only
            facts.setdefault(rid, []).append((fid, res["system_id"], tr, _flags(entry, res)))
            source[rid] = "extracted"
            emails = {s["email"].lower() for s in entry.get("subjects", []) if s.get("email")}
            if entry.get("kind") == "pdf" and rid not in link_rids and emails and tr != "RETAIN":
                owners[rid] = (fid, emails)
    for entry in multimodal.retention_entries():
        for res in entry.get("resources", []):
            until = _retain_until(entry, res)
            if res.get("resource_type") == "retention_register":
                # one register row per subject: "ORD-... | user_08 | ... | Retain until 04 May 2033"
                for fact in res.get("facts", []):
                    m = re.match(r"(ORD-\d+) \| (user_\d+) \|", fact)
                    if m:
                        retention.append(dict(evidence_id=entry["evidence_id"], system_id=res["system_id"],
                                              resource_id=res["resource_id"], user_id=m.group(2),
                                              order_id=m.group(1), retain_until=_parse_retain(fact) or until))
            else:
                for s in entry.get("subjects", []) or [{}]:
                    retention.append(dict(evidence_id=entry["evidence_id"], system_id=res["system_id"],
                                          resource_id=res["resource_id"], user_id=s.get("user_id"),
                                          order_id=None, retain_until=until))
    return facts, owners, retention, source


if __name__ == "__main__":
    from ingest import load_activity
    FACTS, DOC_OWNERS, RETENTION, SOURCE = build(
        {a["resource_id"] for a in load_activity() if a["event_type"] == "LINK_RESOURCE_IDENTITY"})
    for rid, lst in sorted(FACTS.items()):
        print(rid, SOURCE[rid], lst)
    print("owners", DOC_OWNERS)
    for r in RETENTION:
        print("retain", r)
