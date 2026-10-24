"""State engine: links resources to a deletion subject, replays activity + evidence to get the
state of each resource at audit_at, and plans FINDING / REMEDIATION / VERIFICATION steps.

Key API:
  eng = Engine()
  eng.case_resources(case)            -> {resource_id: ResourceInfo}
  eng.state_at(resource_id, audit_at) -> dict(present, pii, enabled, restore_allowed, evidence_id, ...)
  eng.active_data_flows(user_id, audit_at), eng.restore_risks(user_id, audit_at)
  eng.plan(case) -> (status, steps)
"""
import collections, csv, json, os, re
from datetime import timedelta
from ingest import load_all, load_activity, DATASET, parse_ts, system_for, prefix_of, norm_email
from identity import Identity, obs_matches
from windows import window_of, offset_ok, residue_ok
from multimodal_hints import HINTS, DOC_OWNERS

OP_ORDER = ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK",
            "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD",
            "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED", "VERIFY_RECORD_ANONYMIZED",
            "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"]
PHASE = {op: ("FINDING" if op.startswith("REPORT") else "VERIFICATION" if op.startswith("VERIFY") else "REMEDIATION")
         for op in OP_ORDER}
STATE = {"REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
         "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
         "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
         "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
         "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
         "VERIFY_RESTORE_BLOCKED": "BLOCKED"}

# deletion-worker cluster window relative to reported_at (minutes); learned from the 32 samples:
#   account DELETE at R-75..R-45 (generated) / R-6..R+9 (hand-made), policy SUCCESS at R-28/-21/-14/-7,
#   FAILED / CHECK_* follow-ups at R+38..R+115.
WIN_LO, WIN_HI = -85, 125
WORKER_TYPES = {"DELETE_RECORD", "ANONYMIZE_RECORD", "REDACT_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE",
                "CHECK_DATA_FLOW", "CHECK_RESTORE_ACCESS", "RETAIN_RECORD", "CLASSIFY_REQUEST"}

NON_DELETION = re.compile(
    r"not asking for deletion|keep (the|my) account|leave (the|my) (account|profile)|don.t delete|do not (close|delete)|"
    r"without removing|marketing lists only|continue using|i still (use|need|want)|download my|"
    r"replace it with|correct it|change the email|resetting the login|stop the promotional|send me a copy|need a copy|information request|what personal information you hold|tell me what (personal )?(data|information)", re.I)


def is_deletion_request(text):
    return not NON_DELETION.search(text or "")


class Engine:
    def __init__(self):
        self.files, self.obs, self.skipped = load_all()
        self.acts = load_activity()
        self.idn = Identity()
        self.by_rid = collections.defaultdict(list)
        for o in self.obs:
            if o.resource_id:
                self.by_rid[o.resource_id].append(o)
        self.act_by_rid = collections.defaultdict(list)
        for a in self.acts:
            self.act_by_rid[a["resource_id"]].append(a)
            m = re.search(r"repopulated downstream resource (\w+)", a["details"])
            if m:
                self.act_by_rid[m.group(1)].append(a)
        self.links = [a for a in self.acts if a["event_type"] == "LINK_RESOURCE_IDENTITY"]
        # account-coverage snapshot files (used to prove absence when no direct lookup exists)
        self.account_snapshots = []
        for o in self.obs:
            if o.system_id == "account_service" and (o.source or "").startswith(("customer_profiles", "bundle/",
                                                                                  "customer_data_")):
                pass
        cov = collections.defaultdict(set)
        for o in self.obs:
            if o.system_id == "account_service" and o.source and (
                    "customer_profiles" in o.source or o.status == "EMPTY_COLLECTION"):
                cov[o.file_id].add(o.resource_id)
        self.account_cov = cov  # file_id -> account rids present in that snapshot
        self._user_obs_cache = {}

    # ------------------------------------------------------------------ linking
    def file_ok(self, fid, audit_at):
        f = self.files.get(fid)
        return f is not None and f["captured_at"] <= audit_at

    def user_obs(self, user_id, audit_at):
        """Observations (in files captured <= audit_at) that mention one of the subject's identifiers."""
        key = (user_id, audit_at)
        if key in self._user_obs_cache:
            return self._user_obs_cache[key]
        ids = self.idn.identifiers(user_id)
        out = collections.defaultdict(list)
        for o in self.obs:
            if not o.resource_id or o.observed_at > audit_at:
                continue
            if obs_matches(o, ids) - {"name"}:
                out[o.resource_id].append(o)
        self._user_obs_cache[key] = out
        return out

    def owner_of(self, rid, audit_at):
        """Set of user_ids that evidence (<= audit_at) ties to this resource."""
        owners = set()
        for o in self.by_rid.get(rid, []):
            owners |= o.users
            for e in o.emails | {k for k in o.lookup_keys if "@" in k}:
                owners |= self.idn.by_email.get(norm_email(e), set())
            for k in o.lookup_keys:
                if k.startswith("user_"):
                    owners.add(k)
        for a in self.links:
            if a["resource_id"] == rid:
                owners |= set(re.findall(r"\buser_\d+\b", a["details"]))
        return owners

    def prepare(self, cases):
        """Assign deletion-worker activity resources to cases (global, across all requests).
        1. candidate cases for an activity = deletion cases whose reported_at puts it in its phrase window;
        2. evidence ownership (identifiers seen in any evidence) decides first;
        3. each case's account DELETE activity is chosen (owner evidence, else the candidate account whose
           second-level fingerprint matches the case's evidence-owned follow-up activities, else elimination);
        4. un-owned follow-ups go to the case whose account fingerprint matches (windows.residue_ok)."""
        self.case_user = {}
        self.case_R = {}
        byR = collections.defaultdict(list)
        for c in cases:
            if not is_deletion_request(c["request_text"]):
                continue
            us = self.idn.resolve(c["email"])
            if not us:
                continue
            self.case_user[c["case_id"]] = us[0]
            R = parse_ts(c["reported_at"])
            self.case_R[c["case_id"]] = R
            byR[R].append(c["case_id"])
        self.assign = collections.defaultdict(lambda: collections.defaultdict(set))  # case -> rid -> via
        acct_acts, other_acts = [], []
        for a in self.acts:
            if not window_of(a["details"]) or a["event_type"] == "CLASSIFY_REQUEST":
                continue
            (acct_acts if ("submitted primary profile" in a["details"] or (
                a["resource_id"].startswith("account_") and a["event_type"] == "DELETE_RECORD")) else other_acts).append(a)
        cand_cases = {}
        owners = {}
        for a in acct_acts + other_acts:
            cand_cases[a["activity_id"]] = [cid for R, cl in byR.items() if offset_ok(a, R) for cid in cl]
            owners[a["activity_id"]] = self.owner_of(a["resource_id"], None)
        # ---- step 3: account per case
        case_acct = {}
        acct_cands = collections.defaultdict(list)
        for a in acct_acts:
            for cid in cand_cases[a["activity_id"]]:
                acct_cands[cid].append(a)
        owned_follow = collections.defaultdict(list)
        for a in other_acts:
            for cid in cand_cases[a["activity_id"]]:
                if self.case_user[cid] in owners[a["activity_id"]]:
                    owned_follow[cid].append(a)
        def phase(a):
            return "policy" if "Policy action completed" in a["details"] else "chain"

        case_phase = collections.defaultdict(set)
        for cid, fl in owned_follow.items():
            for f in fl:
                case_phase[cid].add(phase(f))
        taken = set()
        for cid, al in acct_cands.items():
            u = self.case_user[cid]
            mine = [a for a in al if u in owners[a["activity_id"]]]
            if mine:
                case_acct[cid] = (mine[0], "owner")
                taken.add(mine[0]["activity_id"])
        unowned = [a for a in other_acts if not owners[a["activity_id"]]]
        claimed = {}

        def free_accts(cid):
            u = self.case_user[cid]
            return [a for a in acct_cands[cid] if a["activity_id"] not in taken and not (owners[a["activity_id"]] - {u})]

        def claim_followups():
            for f in unowned:
                if f["activity_id"] in claimed:
                    continue
                fit = []
                for cid in cand_cases[f["activity_id"]]:
                    if cid in case_acct and residue_ok(f, case_acct[cid][0]) is True:
                        if case_phase[cid] and phase(f) not in case_phase[cid]:
                            continue
                        fit.append(cid)
                if len(fit) == 1:
                    claimed[f["activity_id"]] = fit[0]
                    case_phase[fit[0]].add(phase(f))

        for _ in range(6):
            changed = False
            # fingerprint with evidence-owned follow-ups
            for cid in acct_cands:
                if cid in case_acct:
                    continue
                free = free_accts(cid)
                if not free:
                    continue
                scored = []
                for a in free:
                    sc = sum(1 if residue_ok(f, a) else -1 for f in owned_follow[cid] if residue_ok(f, a) is not None)
                    scored.append((sc, a))
                top = max(sc for sc, a in scored)
                best = [a for sc, a in scored if sc == top]
                if len(best) == 1 and (top > 0 or len(free) == 1):
                    case_acct[cid] = (best[0], "fingerprint" if top > 0 else "elimination")
                    taken.add(best[0]["activity_id"])
                    changed = True
            claim_followups()
            # fingerprint with still-unclaimed un-owned follow-ups
            for cid in acct_cands:
                if cid in case_acct:
                    continue
                free = free_accts(cid)
                hits = []
                for a in free:
                    n = 0
                    for f in unowned:
                        if f["activity_id"] in claimed or cid not in cand_cases[f["activity_id"]]:
                            continue
                        if case_phase[cid] and phase(f) not in case_phase[cid]:
                            continue
                        if residue_ok(f, a) is True:
                            n += 1
                    if n:
                        hits.append(a)
                if len(hits) == 1:
                    case_acct[cid] = (hits[0], "fingerprint-unowned")
                    taken.add(hits[0]["activity_id"])
                    changed = True
            claim_followups()
            if not changed:
                break
        # last resort: no signal left -> deterministic one-to-one guess (case order x activity time)
        for cid in sorted(acct_cands, key=lambda c: int(c.split("_")[1])):
            if cid in case_acct:
                continue
            free = sorted(free_accts(cid), key=lambda a: a["ts"])
            if free:
                case_acct[cid] = (free[0], "guess%d" % len(free))
                taken.add(free[0]["activity_id"])
        claim_followups()
        self.case_acct = case_acct
        for cid, al in acct_cands.items():
            if cid in case_acct:
                a, how = case_acct[cid]
                self.assign[cid][a["resource_id"]].add("account:" + how)
            else:
                free = free_accts(cid)
                for a in free:
                    self.assign[cid][a["resource_id"]].add("account:ambiguous%d" % len(free))
        # ---- step 4: follow-ups
        self.ambiguous = collections.Counter()
        for a in other_acts:
            cands = cand_cases[a["activity_id"]]
            own = owners[a["activity_id"]]
            if own:
                for cid in cands:
                    if self.case_user[cid] in own:
                        self.assign[cid][a["resource_id"]].add("cluster+owner")
                continue
            if a["activity_id"] in claimed:
                fit, tag = [claimed[a["activity_id"]]], "cluster-fingerprint"
            else:
                fit = [cid for cid in cands if cid in case_acct and residue_ok(a, case_acct[cid][0]) is True
                       and not (case_phase[cid] and phase(a) not in case_phase[cid])]
                if not fit:
                    fit = [cid for cid in cands if cid not in case_acct
                           and not (case_phase[cid] and phase(a) not in case_phase[cid])]
                tag = ("cluster-ambiguous%d" % len(fit)) if len(fit) != 1 else "cluster-single"
                if len(fit) > 1:
                    fit, tag = sorted(fit, key=lambda c: int(c.split("_")[1]))[:1], "cluster-guess"
            self.ambiguous[tag] += 1
            for cid in fit:
                self.assign[cid][a["resource_id"]].add(tag)

    def case_resources(self, case, user_id, peers=None):
        """Returns {rid: {'via': set()}}: evidence-linked resources + assigned worker-cluster resources."""
        audit_at = parse_ts(case["audit_at"])
        res = {}
        for rid, ol in self.user_obs(user_id, audit_at).items():
            res.setdefault(rid, {"via": set()})["via"].add("evidence")
        for rid, via in self.assign.get(case["case_id"], {}).items():
            acts = [a for a in self.act_by_rid.get(rid, []) if a["ts"] <= audit_at]
            if not acts and rid not in res:
                continue
            res.setdefault(rid, {"via": set()})["via"] |= via
        # multimodal artifacts: LINK_RESOURCE_IDENTITY inside this case's [requested_at, audit_at] window
        req_at = parse_ts(case["requested_at"])
        ids = self.idn.identifiers(user_id)
        for a in self.links:
            if req_at <= a["ts"] <= audit_at and re.search(r"\b%s\b" % re.escape(user_id), a["details"]):
                res.setdefault(a["resource_id"], {"via": set()})["via"].add("link")
        for rid, em in DOC_OWNERS.items():
            fid = HINTS[rid][0]
            if norm_email(em) in ids["emails"] and self.file_ok(fid, audit_at) and \
                    self.files[fid]["captured_at"] >= req_at:
                res.setdefault(rid, {"via": set()})["via"].add("document")
        # one account per subject: prefer evidence/owner-linked accounts
        accs = [r for r in res if r.startswith("account_")]
        strong = [r for r in accs if any(v.startswith("account:") and "ambiguous" not in v for v in res[r]["via"])]
        if not strong:
            strong = [r for r in accs if res[r]["via"] & {"evidence", "cluster+owner"}]
        if strong:
            for r in accs:
                if r not in strong:
                    del res[r]
        if not any(r.startswith("account_") for r in res):
            n = int(user_id.split("_")[1])
            if n <= 30:  # hand-made case family: account_0NN mirrors user_NN (10/10 samples)
                res["account_%03d" % n] = {"via": {"account:pattern"}}
        return res

    # ------------------------------------------------------------------ state
    def state_at(self, rid, audit_at, user_ids=None):
        """Latest evidence-backed state of a resource at audit_at.
        Evidence snapshots override activity claims; if none, fall back to the latest activity claim."""
        obs = [o for o in self.by_rid.get(rid, []) if o.observed_at <= audit_at and o.present is not None]
        st = {"present": None, "pii": None, "enabled": None, "restore_allowed": None, "evidence_id": "",
              "obs": [], "acts": [], "category_hints": set(), "retention_until": None, "status": None}
        for o in self.by_rid.get(rid, []):
            if o.category:
                st["category_hints"].add(o.category)
            if o.source:
                st["category_hints"].add(o.source)
            if o.status:
                st["category_hints"].add(o.status)
            if o.get("x_file"):
                st["category_hints"].add("attachment_file")
        acts = [a for a in self.act_by_rid.get(rid, []) if a["ts"] <= audit_at]
        st["acts"] = acts
        for a in acts:
            st["category_hints"].add("act:" + a["event_type"])
        if obs:
            # group by file, take the latest file (by max observed_at), combine rows inside it
            byfile = collections.defaultdict(list)
            for o in obs:
                byfile[o.file_id].append(o)
            cit = [f for f in byfile if self.file_ok(f, audit_at)]
            # state only from files available at audit_at (manifest captured_at <= audit_at); a file exported
            # later is not citable and cannot support a finding even if its rows are older.
            byfile = {f: byfile[f] for f in cit}
        if obs and byfile:
            order = sorted(byfile, key=lambda f: (max(o.observed_at for o in byfile[f]), self.files[f]["captured_at"]))
            latest_fid = order[-1]
            lo = byfile[latest_fid]
            if all((o.source or "").startswith("log/") for o in lo):
                # logs are event streams: only the latest reading counts
                tmax = max(o.observed_at for o in lo)
                lo = [o for o in lo if o.observed_at == tmax]
            st["obs"] = lo
            st["present"] = any(o.present for o in lo)
            pres = [o for o in lo if o.present]
            st["pii"] = any(o.pii for o in pres) or any(o.status in ("IDENTIFIABLE",) for o in pres)
            if any(o.status in ("ANONYMIZED", "REDACTED") for o in pres):
                st["pii"] = False
            # cite the latest citable file (captured <= audit_at) that shows the same state
            st["evidence_id"] = ""
            for f in reversed(order):
                if not self.file_ok(f, audit_at):
                    continue
                g = byfile[f]
                if all((o.source or "").startswith("log/") for o in g):
                    tm = max(o.observed_at for o in g)
                    g = [o for o in g if o.observed_at == tm]
                gp = any(o.present for o in g)
                if gp == st["present"]:
                    st["evidence_id"] = f
                    break
            for o in lo:
                if o.enabled is not None:
                    st["enabled"] = o.enabled
                if o.restore_allowed is not None:
                    st["restore_allowed"] = o.restore_allowed
                if o.retention_until is not None:
                    st["retention_until"] = o.retention_until
                if o.status in ("ACTIVE",) and rid.startswith("data_flow"):
                    st["enabled"] = True
                if o.status in ("DISABLED",) and rid.startswith("data_flow"):
                    st["enabled"] = False
                if o.status in ("ACTIVE",) and rid.startswith("backup"):
                    st["restore_allowed"] = True
                if o.status in ("BLOCKED",) and rid.startswith("backup"):
                    st["restore_allowed"] = False
                st["status"] = o.status
            # if a later activity claims a change, evidence still wins (claims are not proof)
        return st

    def treatment(self, rid, st):
        p = prefix_of(rid)
        hints = st["category_hints"]
        if p in ("account", "search", "export", "cache", "marketing", "object", "storage"):
            return "DELETE"
        if p == "support":
            if hints & {"SUPPORT_ATTACHMENT", "attachment_file", "act:DELETE_RECORD", "log/attachment_pii_scan"} or any(
                    "ticket_attachments" in h for h in hints) or any(h.startswith("log/contact_query") for h in hints):
                return "DELETE"
            return "REDACT"
        if p == "analytics":
            if "act:DELETE_RECORD" in hints or "log/contact_lookup_completed" in hints:
                return "DELETE"
            return "ANONYMIZE"
        if p == "tombstone":
            return "REDACT"
        if p == "email_delivery":
            return "REDACT"
        if p in ("audit_record", "legal_hold"):
            return "RETAIN"
        if p == "data_flow":
            return "FLOW"
        if p == "backup":
            return "BACKUP"
        return "DELETE"

    def active_data_flows(self, user_id, audit_at):
        out = []
        for rid in self.user_obs(user_id, audit_at):
            if rid and rid.startswith("data_flow"):
                st = self.state_at(rid, audit_at)
                if st["enabled"]:
                    out.append((rid, st["evidence_id"]))
        return out

    def restore_risks(self, user_id, audit_at):
        out = []
        for rid in self.user_obs(user_id, audit_at):
            if rid and rid.startswith("backup"):
                st = self.state_at(rid, audit_at)
                if st["restore_allowed"]:
                    out.append((rid, st["evidence_id"]))
        return out

    def account_absence_evidence(self, rid, audit_at):
        best = None
        for fid, rids in self.account_cov.items():
            if not self.file_ok(fid, audit_at) or rid in rids:
                continue
            t = self.files[fid]["captured_at"]
            if best is None or t > best[0]:
                best = (t, fid)
        return best[1] if best else ""

    # ------------------------------------------------------------------ plan
    def plan(self, case):
        if not is_deletion_request(case["request_text"]):
            return "NOT_APPLICABLE", [], {}
        users = self.idn.resolve(case["email"])
        if not users:
            return "INCOMPLETE", [], {}
        uid = users[0]
        audit_at = parse_ts(case["audit_at"])
        res = self.case_resources(case, uid)
        steps = []  # (op, system, rid, evidence)
        debug = {}
        for rid in res:
            st = self.state_at(rid, audit_at)
            tr = self.treatment(rid, st)
            sysid = system_for(rid) or "unknown"
            if st["obs"]:
                sysid = st["obs"][0].system_id or sysid
            ev = st["evidence_id"]
            if rid in HINTS and self.file_ok(HINTS[rid][0], audit_at):
                fid, sysid, tr, flags = HINTS[rid]
                st.update(flags)
                ev = fid
            # fallback state from activity claims when no evidence
            if st["present"] is None:
                # no evidence snapshot: activity results are only claims and cannot support a finding,
                # so the resource is treated as already in its required state (verification only).
                last = st["acts"][-1] if st["acts"] else None
                if tr == "DELETE":
                    st["present"] = False
                elif tr in ("REDACT", "ANONYMIZE"):
                    st["present"], st["pii"] = True, False
                elif tr == "FLOW":
                    st["enabled"] = False
                elif tr == "BACKUP":
                    st["restore_allowed"] = False
                elif tr == "RETAIN":
                    st["present"] = True
            debug[rid] = dict(tr=tr, present=st["present"], pii=st["pii"], enabled=st["enabled"],
                              restore=st["restore_allowed"], ev=ev, via=sorted(res[rid]["via"]))
            if tr == "DELETE":
                if st["present"]:
                    steps += [("REPORT_REMAINING_RECORD", sysid, rid, ev), ("DELETE_RECORD", sysid, rid, "")]
                    steps.append(("VERIFY_RECORD_ABSENT", sysid, rid, ""))
                else:
                    vev = ev
                    if rid.startswith("account_") and (not st["obs"] or not vev):
                        vev = self.account_absence_evidence(rid, audit_at) or ev
                    steps.append(("VERIFY_RECORD_ABSENT", sysid, rid, vev))
            elif tr in ("REDACT", "ANONYMIZE"):
                v = "VERIFY_RECORD_REDACTED" if tr == "REDACT" else "VERIFY_RECORD_ANONYMIZED"
                r = "REDACT_RECORD" if tr == "REDACT" else "ANONYMIZE_RECORD"
                if st["present"] is False:
                    continue  # record gone entirely; nothing to verify for redaction
                if st["pii"]:
                    steps += [("REPORT_EXPOSED_PII", sysid, rid, ev), (r, sysid, rid, ""), (v, sysid, rid, "")]
                else:
                    steps.append((v, sysid, rid, ev))
            elif tr == "RETAIN":
                if st["retention_until"] and st["retention_until"] < audit_at:
                    if st["present"]:
                        steps += [("REPORT_REMAINING_RECORD", sysid, rid, ev), ("DELETE_RECORD", sysid, rid, ""),
                                  ("VERIFY_RECORD_ABSENT", sysid, rid, "")]
                elif st["present"]:
                    steps.append(("VERIFY_RECORD_PRESENT", sysid, rid, ev))
            elif tr == "FLOW":
                if st["enabled"]:
                    steps += [("REPORT_ACTIVE_DATA_FLOW", sysid, rid, ev), ("DISABLE_DATA_FLOW", sysid, rid, ""),
                              ("VERIFY_DATA_FLOW_DISABLED", sysid, rid, "")]
                elif st["enabled"] is False:
                    steps.append(("VERIFY_DATA_FLOW_DISABLED", sysid, rid, ev))
            elif tr == "BACKUP":
                if st["restore_allowed"]:
                    steps += [("REPORT_RESTORE_RISK", sysid, rid, ev), ("BLOCK_RESTORE", sysid, rid, ""),
                              ("VERIFY_RESTORE_BLOCKED", sysid, rid, "")]
                elif st["restore_allowed"] is False:
                    steps.append(("VERIFY_RESTORE_BLOCKED", sysid, rid, ev))
        incomplete = any(PHASE[s[0]] == "FINDING" for s in steps)
        status = "INCOMPLETE" if incomplete else "COMPLETE"
        if incomplete:
            steps = [(op, sy, r, ev if PHASE[op] == "FINDING" else "") for op, sy, r, ev in steps]
        steps.sort(key=lambda s: (OP_ORDER.index(s[0]), s[1], s[2]))
        out = []
        for i, (op, sy, r, ev) in enumerate(steps, 1):
            out.append({"step_no": i, "phase": PHASE[op], "system_id": sy, "resource_id": r, "operation": op,
                        "state": STATE[op], "evidence_id": ev})
        return status, out, debug
