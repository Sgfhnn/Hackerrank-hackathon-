"""Identity resolution: request email -> user_id(s) + every current/historical identifier."""
import csv, os, re
from ingest import DATASET, parse_ts, norm_email, norm_phone


class Identity:
    def __init__(self):
        self.users = {}
        for r in csv.DictReader(open(os.path.join(DATASET, "user_identities.csv"))):
            self.users[r["user_id"]] = dict(r)
        self.audit = []
        for r in csv.DictReader(open(os.path.join(DATASET, "user_audit_log.csv"))):
            r = dict(r)
            r["ts"] = parse_ts(r["event_time"])
            self.audit.append(r)
        self.by_email = {}
        for u, r in self.users.items():
            self.by_email.setdefault(norm_email(r["email"]), set()).add(u)
        for a in self.audit:
            if a["field_name"] == "email":
                for v in (a["old_value"], a["new_value"]):
                    self.by_email.setdefault(norm_email(v), set()).add(a["user_id"])

    def resolve(self, email):
        """Return list of user_ids whose current or historical email matches."""
        return sorted(self.by_email.get(norm_email(email), set()))

    def identifiers(self, user_id):
        r = self.users.get(user_id, {})
        ids = {"user_id": user_id, "emails": set(), "phones": set(), "names": set(), "hashes": set(),
               "audit_ids": [], "history": []}
        if r:
            ids["emails"].add(norm_email(r["email"]))
            ids["phones"].add(norm_phone(r["phone"]))
            ids["names"].add(r["name"])
            ids["hashes"].add(r["email_hash"])
        for a in self.audit:
            if a["user_id"] != user_id:
                continue
            ids["audit_ids"].append(a["audit_id"])
            ids["history"].append(a)
            f = a["field_name"]
            for v in (a["old_value"], a["new_value"]):
                if not v:
                    continue
                if f == "email":
                    ids["emails"].add(norm_email(v))
                elif f == "phone":
                    ids["phones"].add(norm_phone(v))
                elif f == "name":
                    ids["names"].add(v)
                elif f == "email_hash":
                    ids["hashes"].add(v)
        return ids


def obs_matches(o, ids):
    """Does observation o mention any identifier of this subject? Returns matched kinds."""
    m = set()
    if ids["user_id"] in o.users:
        m.add("user_id")
    lk = {k.lower() for k in o.lookup_keys}
    if (o.emails | {k for k in lk if "@" in k}) & ids["emails"]:
        m.add("email")
    if ids["user_id"] in lk:
        m.add("user_id")
    if o.phones & ids["phones"]:
        m.add("phone")
    if o.hashes & ids["hashes"]:
        m.add("hash")
    if o.names & ids["names"]:
        m.add("name")
    return m
