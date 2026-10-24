# DECISIONS.md — shared decision log (team rule)

All agents append. Newest last.

## 2026-10-24 ~16:11 EAT — @muse, task 4 (multimodal + intent)

1. **Intent gate is rules-first, safe-default.** Explicit negations and
   keep-account signals are checked before any deletion pattern, because
   the non-deletion texts themselves contain the words "deletion",
   "erase" and "close" ("I am not asking for deletion", "Do not close
   the account"). Ambiguous text defaults to NOT_DELETION with
   `needs_llm=True`, never to deletion: wrongly deleting is the
   irreversible error, and AGENTS.md section 6.5 only authorizes
   deletion when the person actually asked for it.
2. **Audit-of-deletion phrasing counts as a deletion case.** "Verify that
   my account was deleted", "confirm the profile is gone" presuppose a
   deletion request; the public samples label these COMPLETE, not
   NOT_APPLICABLE.
3. **Extracted facts are recorded as shown, conflicts are not resolved
   in the extraction layer.** marketing_001: the screenshot header shows
   Soo-jin Park / sj.park@naver.com, while system_activity activity_4008
   links marketing_001 to user_09 / kwame-mensah@proton.me. Both claims
   are recorded in code/extracted/images.json; the state engine resolves
   identity, extraction does not.
4. **evidence_46 (object storage) grounds no finding by itself.** The
   screenshot shows 8 objects but no subject identifier; the state
   engine must link its objects to a user through other evidence before
   citing it.
5. **export_1260 expiry is recorded, not interpreted.** The export was
   Ready at capture (2026-06-18) with an expiry of 2026-06-25; whether a
   later audit still counts it is a state-engine call using audit_at.
