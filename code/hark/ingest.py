"""Evidence ingestion: turns every machine-readable evidence file into a flat list of
observations, one per (file, resource) sighting.

Observation fields:
  file_id, file_path, file_captured_at (manifest), observed_at (record-level time if known,
  else file capture), system_id, resource_id, category (if stated), user_ref, emails, phones,
  names, hashes, present (True/False/None), status (raw status word), enabled, restore_allowed,
  retention_until, source (table/collection/event), lookup_keys (identifiers used to find it),
  pii (True if any direct identifier exposed), raw (small dict)

Only stdlib + sqlite3. PDFs/PNGs are skipped (teammate owns OCR) but listed.
"""
import csv, json, os, re, sqlite3, glob
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
def _find_dataset():
    if os.environ.get("DATASET_DIR"):
        return os.environ["DATASET_DIR"]
    d = HERE
    for _ in range(4):
        if os.path.isfile(os.path.join(d, "dataset", "evidence_files.csv")):
            return os.path.join(d, "dataset")
        d = os.path.dirname(d)
    return os.path.join(HERE, "..", "..", "dataset")


DATASET = _find_dataset()

TS_RE = re.compile(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{4}-\d{2}-\d{2}) (\d{1,2}):(\d{2}) (AM|PM) EAT \(UTC\+03:00\)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
USER_RE = re.compile(r"\buser_\d+\b")
RES_RE = re.compile(r"\b(account|search|support|analytics|export|data_flow|backup|audit_record|tombstone|legal_hold|cache|email_delivery|marketing|object|telemetry|storage|attachment)_\d+\b")

SERVICES = ["account_service", "search_service", "support_platform", "analytics_platform", "security_service",
            "integration_service", "backup_service", "export_service", "identity_service", "legal_service",
            "marketing_platform", "object_storage", "cache_service", "email_delivery_service", "profile_service",
            "notification_service", "support_replica", "telemetry_service"]

PREFIX_SYSTEM = {
    "account": "account_service", "search": "search_service", "support": "support_platform",
    "analytics": "analytics_platform", "export": "export_service", "data_flow": "integration_service",
    "backup": "backup_service", "audit_record": "security_service", "tombstone": "identity_service",
    "legal_hold": "legal_service", "cache": "cache_service", "email_delivery": "email_delivery_service",
}


ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?Z")


def parse_ts(s):
    """All dataset timestamps are ISO-8601 UTC ('2026-01-04T14:56:29Z'). Returns naive UTC datetime.
    (Tool output may display them localized as 'Sun 2026-01-04 5:56 PM EAT' - that form is also accepted.)"""
    if s is None:
        return None
    s = str(s)
    m = ISO_RE.search(s)
    if m:
        return datetime(int(m.group(1)[:4]), int(m.group(1)[5:7]), int(m.group(1)[8:10]), int(m.group(2)),
                        int(m.group(3)), int(m.group(4) or 0))
    m = TS_RE.search(s)
    if not m:
        return None
    d, h, mi, ap = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
    if ap == "AM" and h == 12:
        h = 0
    elif ap == "PM" and h != 12:
        h += 12
    from datetime import timedelta
    return datetime.strptime(d, "%Y-%m-%d").replace(hour=h, minute=mi) - timedelta(hours=3)


def prefix_of(rid):
    m = re.match(r"^(.*)_\d+$", rid or "")
    return m.group(1) if m else None


def system_for(rid):
    return PREFIX_SYSTEM.get(prefix_of(rid))


def norm_email(e):
    return e.strip().lower() if e else None


def norm_phone(p):
    return re.sub(r"\D", "", p) if p else None


PHONE_RE = re.compile(r"\+\d[\d\s().\-]{6,}\d")


def walk_strings(o):
    if isinstance(o, dict):
        for v in o.values():
            yield from walk_strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk_strings(v)
    elif isinstance(o, str):
        yield o


NAME_KEYS = {"full_name", "display_name", "requester_name", "name"}


def extract_ids(d):
    """Pull identifiers out of an arbitrary dict (values may be nested)."""
    emails, phones, hashes, users, names = set(), set(), set(), set(), set()
    for s in walk_strings(d):
        if s in ("null", "None"):
            continue
        for e in EMAIL_RE.findall(s):
            emails.add(e.lower())
        for h in HASH_RE.findall(s):
            hashes.add(h)
        for u in USER_RE.findall(s):
            users.add(u)
        for p in PHONE_RE.findall(s):
            phones.add(norm_phone(p))

    def names_in(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in NAME_KEYS and isinstance(v, str) and v and v != "null":
                    names.add(v)
                else:
                    names_in(v)
        elif isinstance(o, list):
            for v in o:
                names_in(v)
    names_in(d)
    return emails, phones, hashes, users, names


class Obs(dict):
    __getattr__ = dict.get


def make_obs(file_id, fmeta, rid, system=None, observed_at=None, present=None, status=None, data=None,
             source=None, lookup_keys=None, category=None, **kw):
    data = data or {}
    emails, phones, hashes, users, names = extract_ids(data)
    lk = set()
    for k in (lookup_keys or []):
        if k and k != "null":
            lk.add(k.lower() if "@" in k else k)
    o = Obs(
        file_id=file_id, file_path=fmeta["file_path"], file_captured_at=fmeta["captured_at"],
        observed_at=observed_at or fmeta["captured_at"], system_id=system or system_for(rid), resource_id=rid,
        category=category, present=present, status=status, emails=emails, phones=phones, hashes=hashes,
        users=users, names=names, lookup_keys=lk, source=source, raw=data,
    )
    # direct identifiers exposed (user_id reference alone is not PII for tombstones)
    o["pii"] = bool(emails or phones or hashes or names)
    o.update(kw)
    return o


# ---------------------------------------------------------------- loaders

def load_manifest():
    files = {}
    for r in csv.DictReader(open(os.path.join(DATASET, "evidence_files.csv"))):
        files[r["file_id"]] = dict(file_path=r["file_path"], origin=r["origin"], captured_at=parse_ts(r["captured_at"]),
                                   captured_at_raw=r["captured_at"])
    return files


def _collection_category(system, coll):
    return {
        ("support_platform", "ticket_attachments"): "SUPPORT_ATTACHMENT",
        ("support_platform", "support_conversations"): "SUPPORT_MESSAGE",
        ("legal_service", "retained_order_records"): "LEGAL_HOLD_RECORD",
        ("identity_service", "deleted_customer_references"): "ACCOUNT_TOMBSTONE",
        ("security_service", "security_audit_events"): "SECURITY_AUDIT_LOG",
        ("account_service", "customer_profiles"): "USER_PROFILE",
        ("search_service", "customer_search_documents"): "SEARCH_INDEX_ENTRY",
        ("analytics_platform", "customer_analytics_reports"): "ANALYTICS_PROFILE",
        ("export_service", "customer_export_jobs"): "USER_EXPORT",
        ("integration_service", "connector_configurations"): "DATA_FLOW",
        ("backup_service", "backup_catalog"): "BACKUP_ARCHIVE",
    }.get((system, coll))


def _flags(d):
    kw = {}
    if "enabled" in d:
        kw["enabled"] = bool(d["enabled"]) if not isinstance(d["enabled"], str) else d["enabled"] in ("1", "true", "True")
    if "restore_allowed" in d:
        v = d["restore_allowed"]
        kw["restore_allowed"] = v in (1, True, "1", "true", "True")
    for k in ("retention_until", "retained_until"):
        if k in d:
            kw["retention_until"] = parse_ts(d[k])
    if "account_status" in d:
        kw["account_status"] = d["account_status"]
    return kw


TABLE_SYSTEM = {
    "customer_profiles": ("account_service", "USER_PROFILE"),
    "customer_search_documents": ("search_service", "SEARCH_INDEX_ENTRY"),
    "customer_analytics_profiles": ("analytics_platform", "ANALYTICS_PROFILE"),
    "support_records": ("support_platform", None),
    "customer_exports": ("export_service", "USER_EXPORT"),
    "connector_subscriptions": ("integration_service", "DATA_FLOW"),
    "backup_restore_entries": ("backup_service", "BACKUP_ARCHIVE"),
    "security_audit_records": ("security_service", "SECURITY_AUDIT_LOG"),
    "customer_profiles_primary": ("account_service", "USER_PROFILE"),
    "customer_profiles_failover_replica": ("account_service", "USER_PROFILE"),
    "search_documents_primary": ("search_service", "SEARCH_INDEX_ENTRY"),
}


def load_sql(file_id, fmeta):
    path = os.path.join(DATASET, fmeta["file_path"])
    con = sqlite3.connect(":memory:")
    con.executescript(open(path).read())
    con.row_factory = sqlite3.Row
    out = []
    tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    for t in tabs:
        rows = [dict(r) for r in con.execute(f'select * from "{t}"')]
        if t == "snapshot_scope":
            continue
        for r in rows:
            if "record_json" in r:
                d = json.loads(r["record_json"])
                sysid = r["origin_system"]
                cat = _collection_category(sysid, r["collection_name"])
                out.append(make_obs(file_id, fmeta, d.get("resource_id") or r["resource_id"], sysid,
                                    parse_ts(r.get("updated_at")) or fmeta["captured_at"], True, "ROW", d,
                                    source=f"{t}/{r['collection_name']}", category=cat, table=t, **_flags(d)))
            else:
                sysid, cat = TABLE_SYSTEM.get(t, (None, None))
                d = {k: v for k, v in r.items() if k not in ("updated_at",)}
                out.append(make_obs(file_id, fmeta, r["resource_id"], sysid,
                                    parse_ts(r.get("updated_at")) or fmeta["captured_at"], True, "ROW", d,
                                    source=t, category=cat, table=t, **_flags(d)))
    return out


def load_json(file_id, fmeta):
    path = os.path.join(DATASET, fmeta["file_path"])
    o = json.load(open(path))
    out = []
    cap = parse_ts(o.get("exported_at") or o.get("captured_at")) or fmeta["captured_at"]
    if "snapshots" in o:  # records assurance bundles
        for s in o["snapshots"]:
            sysid = s["data_store"]
            cat = _collection_category(sysid, s["collection"])
            st = parse_ts(s["captured_at"])
            if not s["records"]:
                out.append(make_obs(file_id, fmeta, None, sysid, st, False, "EMPTY_COLLECTION", {},
                                    source=f"bundle/{s['collection']}", category=cat))
            for d in s["records"]:
                out.append(make_obs(file_id, fmeta, d["resource_id"], sysid, st, True, "ROW", d,
                                    source=f"bundle/{s['collection']}", category=cat, **_flags(d)))
    elif "service_exports" in o:
        for sysid, groups in o["service_exports"].items():
            for gname, items in groups.items():
                for d in items:
                    st = parse_ts(d.get("observed_at")) or cap
                    rid = d.get("resource_id")
                    present, status, data = True, "ROW", dict(d)
                    if gname == "profile_lookups":
                        present = d.get("status_code") == 200
                        status = f"HTTP_{d.get('status_code')}"
                        data = d.get("profile") or {}
                    elif gname == "index_queries":
                        docs = (d.get("hits") or {}).get("documents") or []
                        present = bool(docs)
                        status = "HIT" if docs else "MISS"
                        data = {"docs": docs}
                        for doc in docs:
                            if doc.get("resource_id") and doc["resource_id"] != rid:
                                pass
                    lk = [d.get("query")] if d.get("query") else []
                    data.pop("observed_at", None)
                    out.append(make_obs(file_id, fmeta, rid, sysid, st, present, status, data,
                                        source=f"service_export/{gname}", lookup_keys=lk, **_flags(d)))
    elif "records" in o and o["records"] and "record_type" in o["records"][0]:
        # manifest/ledger/connector config style: system, resource_id, record_type, result, subject_reference
        for d in o["records"]:
            res = d["result"]
            out.append(make_obs(file_id, fmeta, d["resource_id"], d["system"], parse_ts(d.get("recorded_at")) or cap,
                                res not in ("NOT_FOUND", "ABSENT", "DELETED"), res, {"subject_reference": d["subject_reference"]},
                                source="ledger", category=d["record_type"]))
    elif "records" in o:  # collection dumps (analytics report, export jobs)
        coll = o.get("collection", "")
        src = o.get("source", "")
        sysid = {"customer_analytics_reports": "analytics_platform", "customer_export_jobs": "export_service"}.get(coll) or src
        cat = _collection_category(sysid, coll)
        for d in o["records"]:
            out.append(make_obs(file_id, fmeta, d["resource_id"], sysid, cap, True, "ROW", d,
                                source=f"collection/{coll}", category=cat, **_flags(d)))
    return out


def load_txt(file_id, fmeta):
    path = os.path.join(DATASET, fmeta["file_path"])
    out = []
    for line in open(path):
        parts = [p.strip() for p in line.strip().lstrip("- ").split("|")]
        if len(parts) == 6 and RES_RE.search(parts[2]):
            ts, sysid, rid, cat, res, subj = parts
            out.append(make_obs(file_id, fmeta, rid, sysid, parse_ts(ts), res not in ("NOT_FOUND", "ABSENT"), res,
                                {"subject_reference": subj} if subj != "no direct identifier" else {},
                                source="handover", category=cat))
    return out


KV_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=(?:"((?:[^"\\]|\\.)*)"|([^\s"]+))')


def parse_log_line(line):
    ts = parse_ts(line)
    body = line.replace('\\"', '"')
    kv = {}
    j = body.find("{")
    if j >= 0 and body.rstrip().endswith("}"):
        try:
            kv.update(json.loads(body[j:]))
        except Exception:
            pass
    for m in KV_RE.finditer(body):
        k = m.group(1)
        v = m.group(2) if m.group(2) is not None else m.group(3)
        if k not in kv:
            kv[k] = v
    # service + trace + event from structural formats
    svc = kv.get("service") or kv.get("function") or kv.get("logger")
    if not svc or svc not in SERVICES:
        for s in SERVICES:
            if re.search(r"[\[\| ]%s[\]\| ]" % s, body):
                svc = s
                break
    m = re.search(r"\b((?:tr|rb)-[A-Za-z0-9\-]+)\b", body)
    trace = kv.get("trace_id") or kv.get("requestId") or (m.group(1) if m else None)
    ev = kv.get("event")
    if not ev:
        parts = [p.strip() for p in body.split(" | ")]
        if len(parts) >= 6:
            ev = parts[4]
            if parts[2] in SERVICES:
                svc = parts[2]
        else:
            m2 = re.search(r"\] ([a-z_]+): ", body)
            if m2:
                ev = m2.group(1)
    return ts, svc, trace, ev, kv


ID_KEYS = ("lookup_key", "match_key", "subject_key", "scan_key", "correlation_key", "subject_ref", "matched_value",
           "subject_reference")


def load_log(file_id, fmeta):
    path = os.path.join(DATASET, fmeta["file_path"])
    out = []
    traces = {}
    lines = [l.rstrip("\n") for l in open(path) if l.strip()]
    parsed = [parse_log_line(l) for l in lines]
    for ts, svc, trace, ev, kv in parsed:
        if trace:
            t = traces.setdefault(trace, {"keys": set()})
            for k in ID_KEYS:
                if kv.get(k) and kv[k] != "null":
                    t["keys"].add(kv[k])
    for (ts, svc, trace, ev, kv), line in zip(parsed, lines):
        rid = kv.get("resource_id")
        if not rid or not RES_RE.fullmatch(rid):
            continue
        sysid = kv.get("system") or system_for(rid)
        if svc == "support_replica":
            sysid = "support_platform"
        keys = set(traces.get(trace, {}).get("keys", set())) if trace else set()
        for k in ID_KEYS:
            if kv.get(k) and kv[k] != "null":
                keys.add(kv[k])
        present, status = None, ev
        data = {}
        if ev == "record_lookup":
            res = kv.get("result")
            present = res not in ("NOT_FOUND", "ABSENT")
            status = res
            data = {"subject_reference": kv.get("subject_reference")}
            out.append(make_obs(file_id, fmeta, rid, sysid, ts, present, status, data, source=f"log/{ev}",
                                category=kv.get("record_type")))
            continue
        rows = kv.get("rows")
        http = kv.get("http_status")
        if http:
            present = http == "200"
        elif rows is not None:
            present = rows not in ("0",)
        if kv.get("result") in ("hit",):
            present = True
        elif kv.get("result") in ("miss", "no_hit", "none"):
            present = False
        if "total_hits" in kv:
            present = kv["total_hits"] not in ("0",)
        for k in ("email", "phone", "email_hash", "customer_email", "requester_email", "requester_phone",
                  "requester_name", "full_name", "display_name", "owner_email", "customer_reference", "uploaded_by",
                  "matched_value", "subject_reference", "message_text", "subject_ref"):
            if k in kv and kv[k] not in (None, "null", ""):
                data[k] = kv[k]
        if ev in ("message_pii_scan", "attachment_pii_scan", "analytics_identity_scan", "message_read",
                  "analytics_profile_read", "contact_lookup_completed", "connector_heartbeat", "connector_config_loaded",
                  "restore_candidate_evaluated", "restore_authorization_completed", "export_download_check_completed"):
            present = True
        if "direct_identifiers" in kv:
            present = True
        if ev and ev.endswith("_completed") and present is None:
            present = True
        if kv.get("archive_present") == "false" or kv.get("available") == "false":
            present = False
        if svc == "profile_service":
            sysid = "account_service"
        flags = {}
        if "enabled" in kv:
            flags["enabled"] = kv["enabled"] == "true"
        if "restore_allowed" in kv:
            flags["restore_allowed"] = kv["restore_allowed"] == "true"
        if "retention_until" in kv:
            flags["retention_until"] = parse_ts(kv["retention_until"].replace('"', ''))
        if "eligible" in kv:
            flags["restore_allowed"] = kv["eligible"] == "true"
        if kv.get("decision") == "deny":
            flags["restore_allowed"] = False
        elif kv.get("decision") in ("allow", "permit", "approved"):
            flags["restore_allowed"] = True
        for extra in ("archive_present", "marker_found", "download_available", "direct_identifiers", "file",
                      "connector_name", "last_delivery_status", "integrity_check", "event_code", "index", "decision",
                      "restore_path", "status", "state", "deletion_block", "eligible", "policy", "access", "reason",
                      "email_opt_in", "available", "scheduler_registered"):
            if extra in kv:
                flags["x_" + extra] = kv[extra]
        o = make_obs(file_id, fmeta, rid, sysid, ts, present, status, data, source=f"log/{ev}", lookup_keys=keys,
                     trace=trace, service=svc, line=line[:400], **flags)
        out.append(o)
    return out


def load_all():
    files = load_manifest()
    obs = []
    skipped = []
    for fid, fm in files.items():
        p = fm["file_path"]
        try:
            if p.endswith(".sql"):
                obs += load_sql(fid, fm)
            elif p.endswith(".json"):
                obs += load_json(fid, fm)
            elif p.endswith(".txt"):
                obs += load_txt(fid, fm)
            elif p.endswith(".log"):
                obs += load_log(fid, fm)
            else:
                skipped.append((fid, p))
        except Exception as e:  # keep going; report
            skipped.append((fid, p + " ERROR " + repr(e)))
    return files, obs, skipped


def load_activity():
    acts = []
    for r in csv.DictReader(open(os.path.join(DATASET, "system_activity.csv"))):
        r = dict(r)
        r["ts"] = parse_ts(r["event_time"])
        acts.append(r)
    return acts


if __name__ == "__main__":
    import collections
    files, obs, skipped = load_all()
    print("observations", len(obs))
    print("skipped", skipped)
    c = collections.Counter((o.file_id, o.system_id) for o in obs)
    per = collections.Counter(o.file_id for o in obs)
    for f in files:
        print(f, files[f]["file_path"], per.get(f, 0))
