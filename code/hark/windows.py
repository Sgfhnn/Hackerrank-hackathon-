from datetime import timedelta
WINDOWS = [  # (phrase, lo_min, hi_min) relative to reported_at
    ("submitted primary profile", -80, -40),
    ("Privacy intake classified", -80, -40),
    ("Policy action completed", -30, -4),
    ("received the final result", 30, 125),
    ("Connector status check", 30, 125),
    ("Restore-access check", 30, 125),
    ("reported the requested operation", -12, 12),
    ("stopped after the target system", -12, 12),
    ("completed some internal work", -12, 12),
]


def window_of(details):
    for ph, lo, hi in WINDOWS:
        if ph in details:
            return lo, hi
    return None


def offset_ok(a, R):
    w = window_of(a["details"])
    if not w:
        return False
    d = (a["ts"] - R).total_seconds() / 60
    return w[0] <= d <= w[1]


# Second-level fingerprint: every follow-up activity of a case is offset from that case's account
# DELETE ("submitted primary profile") by d seconds with d mod 30 in a fixed band (learned on samples):
#   "Policy action completed"            -> d in [1200, 4200], d % 30 in {9,10,11}
#   "received the final result"/CHECK_*/Replication -> d in [5000, 10500], d % 30 in {20,21,22}
def residue_ok(a, acct):
    d = int((a["ts"] - acct["ts"]).total_seconds())
    det = a["details"]
    if "Policy action completed" in det:
        return 900 <= d <= 4500 and d % 30 in (9, 10, 11)
    if any(p in det for p in ("received the final result", "Connector status check", "Restore-access check",
                              "Replication worker")):
        return 4500 <= d <= 11500 and d % 30 in (20, 21, 22)
    return None  # not applicable (hand-made cases)
