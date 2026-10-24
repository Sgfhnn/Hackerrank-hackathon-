# What the 32 solved samples teach us

Short field guide for the team, built from `dataset/sample_deletion_requests.csv`
and `dataset/sample_output.csv`. Every number below is reproducible with
`python3 code/evaluation/analyze_samples.py`. Hark's `code/hark/FINDINGS.md` goes
deeper on how resources are linked to cases; this note is about the **shape of the
answer**: which operations appear together, when, and how status relates to evidence.

Caveat: 32 samples is small, none of them is `NOT_APPLICABLE`, and the hidden set has
518 rows. Treat the rules as strong hints, not laws.

## 1. The five rules that explain almost everything

1. **Status is decided only by findings.** `INCOMPLETE` if and only if the answer has at
   least one FINDING step (32/32 samples). `COMPLETE` rows contain only VERIFICATION
   steps (10/10). `NOT_APPLICABLE` is `[]` (no sample, but the contract says so).
2. **Every finding becomes exactly one remediation on the same system and resource**
   (32/32), and every remediated resource gets a matching verification (32/32).
   Findings, remediations and verifications therefore come in triples; the plan is
   derived mechanically from the findings.
3. **The finding type picks the treatment** (see table below). Backups and security
   audit records are never deleted: backups get `BLOCK_RESTORE`, audit records only
   get `VERIFY_RECORD_PRESENT`.
4. **`account_service` is always verified absent.** All 32 samples contain a
   `VERIFY_RECORD_ABSENT` on the user's account, even when the account was never the
   problem (it is the first step of the ABSENT group because `account_service` sorts
   first among systems).
5. **The security audit record is verified present** in 24/32 samples
   (`security_service`, `VERIFY_RECORD_PRESENT`; retention `rule_12` in
   `dataset/deletion_rules.csv`). The 8
   exceptions are the hand-made starter cases `case_01, 02, 04, 05, 06, 07, 08, 10`
   (see section 5).

## 2. Finding -> remediation -> verification

| Finding (state) | Typical system | Remediation (state) | Verification (state) | Rule(s) in `deletion_rules.csv` | Samples |
| --- | --- | --- | --- | --- | --- |
| `REPORT_REMAINING_RECORD` (PRESENT) | `search_service` 12, `support_platform` 3 (attachments), `export_service` 2, `account_service` 3, `analytics_platform` 1 | `DELETE_RECORD` (ABSENT) | `VERIFY_RECORD_ABSENT` | 01 profile, 02 secondary id, 03 search entry, 04 cache, 06 attachment, 09 export, 10 marketing: DELETE | 21 findings |
| `REPORT_EXPOSED_PII` (PRESENT) | `support_platform` 5, `analytics_platform` 5, `identity_service` 1 (tombstone) | support: `REDACT_RECORD`; analytics: `ANONYMIZE_RECORD`; tombstone: `REDACT_RECORD` | `VERIFY_RECORD_REDACTED` / `VERIFY_RECORD_ANONYMIZED` / `VERIFY_RECORD_REDACTED` | 05 support message REDACT, 07/08 analytics ANONYMIZE, 15 tombstone REDACT | 11 findings |
| `REPORT_ACTIVE_DATA_FLOW` (ACTIVE) | `integration_service` 6 | `DISABLE_DATA_FLOW` (DISABLED) | `VERIFY_DATA_FLOW_DISABLED` | (no rule row; a live flow can recreate deleted data) | 6 |
| `REPORT_RESTORE_RISK` (ACTIVE) | `backup_service` 3 | `BLOCK_RESTORE` (BLOCKED) | `VERIFY_RESTORE_BLOCKED` | 14 backup archive: RETAIN, restore access blocked | 3 |
| *(none, retention)* | `security_service` | none | `VERIFY_RECORD_PRESENT` | 12 security audit log: RETAIN | 24 |

Two details worth remembering:

* **An analytics record is usually anonymized, not deleted.** In 5 of 6 analytics
  findings (`case_11, 18, 22, 29, 30`) the answer is `EXPOSED_PII -> ANONYMIZE_RECORD`
  (`rule_07`). The exception is `case_12`, where `analytics_9914` is reported as a
  *remaining record* and deleted, so the treatment follows the record's data
  category and not only the system. Do not hard-code "analytics = anonymize".
* **A support item is redacted when it is a message and deleted when it is an
  attachment** (`rule_05` vs `rule_06`). Samples with support deletions: `case_07`,
  `case_10`, `case_13` (all `REMAINING_RECORD`); samples with support redaction:
  `case_11, 18, 19, 29, 30` (all `EXPOSED_PII`). Whether a given support record is a
  message or an attachment is not visible in the output, only in the evidence.

## 3. Which operations appear together

Finding signatures of the 22 INCOMPLETE samples (and the 10 COMPLETE ones):

| Signature | n | Cases | Steps |
| --- | ---: | --- | --- |
| remaining record only | 10 | 05, 06, 07, 08, 10, 12, 13, 16, 23, 24 | 3-8 |
| restore risk only | 3 | 09, 14, 17 | 5 |
| active data flow only | 1 | 15 | 5 |
| exposed PII only (tombstone) | 1 | 32 | 5 |
| remaining + exposed PII | 2 | 19, 22 | 8 |
| remaining + active flow | 1 | 20 | 8 |
| remaining + exposed PII + active flow ("full house") | 4 | 11, 18, 29, 30 | **14** |
| no finding (`COMPLETE`) | 10 | 01-04, 21, 25-28, 31 | 1-6 |

What this says:

* **`REPORT_RESTORE_RISK` always appears alone** (3/3). A restore problem never
  co-occurs with a record or flow problem in these samples, and the backup resource
  is never deleted.
* **The "full house" shape is the only way to reach 14 steps**, and it is always
  exactly: 1 search record + 1 analytics PII + 1 support PII + 1 data flow
  (4/4). Its `reported_status` is COMPLETE in 3 and NOT_STARTED in 1.
* **Search is the most common leak**: 12 of 22 INCOMPLETE rows contain a search
  entry (`06, 11, 13, 16, 18, 19, 20, 22, 23, 24, 29, 30`). Account records remain in
  only 3 cases (`05, 16, 24`), export files in 2 (`08, 23`), support attachments in 3
  (`07, 10, 13`).
* **Analytics exposure travels with support exposure and a data flow** in the
  full-house family, but appears with only a search record in `case_22`, and support
  exposure appears with only a search record in `case_19`. Do not assume that finding
  one of the three implies the other two.
* **`COMPLETE` rows list the compliant resources found for the subject** (`case_26,
  27, 28`: account absent, audit record present, support redacted, analytics
  anonymized, flow disabled, restore blocked = 6 steps; `case_31`: account, audit
  record, identity tombstone redacted), while an `INCOMPLETE` row verifies the
  account, the audit record and the resources it fixed; the only verification steps
  without a matching remediation are account and security in 22/22 incomplete rows.
  So "compliant but not touched" resources appear only in COMPLETE answers.

### Operation order inside each phase

The ordering rule is satisfied by all 32 sample rows (canonical-order compliance
1.00 in `code/evaluation/main.py`). Observed concrete orders:

* FINDING: remaining record, exposed PII, active flow, restore risk.
* REMEDIATION: disable flow, then delete, redact, anonymize. A data flow is always
  disabled **before** any record is touched (`case_11, 18, 20, 29, 30`).
  `BLOCK_RESTORE` never co-occurs with other remediation in the samples, so its
  position is untested.
* VERIFICATION: absent, present (security), redacted, anonymized, flow disabled,
  restore blocked. Inside the absent group the order is alphabetical by `system_id`
  (`case_13`: account, search, support; `case_23`: account, export, search).
* `step_no` is consecutive from 1 in every row.

Open question: the statement says "ascending `resource_id`". No sample has two steps
with the same operation **and** system, so we cannot tell whether `search_898` sorts
before `search_1007` (numeric) or after (string). `code/evaluation/scorer.py`
accepts either when validating; the team should pick one and note it in
`DECISIONS.md`. The 518 requests are listed in natural order (`case_33 ... case_550`),
which is also not the plain string order.

## 4. How status relates to the evidence

**The workflow's `reported_status` carries almost no signal.**

| reported_status | true COMPLETE | true INCOMPLETE |
| --- | ---: | ---: |
| `COMPLETE` | 6 | **11** |
| `PARTIALLY_COMPLETE` | 1 | 4 |
| `NOT_STARTED` | 3 | 7 |

65% of requests reported `COMPLETE` are actually `INCOMPLETE`, and 3 of 10
`NOT_STARTED` requests are already clean. Ignore it for the decision, which matches
the problem statement ("a claim, not proof").

**Evidence citation pattern**

* `INCOMPLETE`: all FINDING steps of a case cite the **same single `file_id`**
  (22/22). REMEDIATION and the future VERIFICATION steps use `evidence_id = ""`
  (22/22).
* `COMPLETE`: every VERIFICATION step cites evidence; one `file_id` for the whole case
  in 9/10 (the exception, `case_04`, cites two: `evidence_54` for the account and
  `evidence_56` for the analytics profile).
* The cited file is captured at or before `audit_at` in 33/33 citations; the lag is
  0-0.6 h for 28 of 33 (median 0.25 h). The five older citations (12.5-134.7 h) are
  exactly the starter cases `01, 02, 04, 05, 10`. No sample cites a file captured
  after `audit_at`.
* No sample cites an `activity_id` or `audit_id`; all 33 citations are `file_id`s.
  Snapshot files are shared by many cases (for example `evidence_92` serves nine
  samples, `evidence_94` and `evidence_03` three each), so the cited file does not identify the case; the
  record contents do.
* Claims versus state: activity rows such as "deletion SUCCESS" never decide the
  state. Hark documents a sample (`case_24`) where a successful delete was undone
  by a replica re-sync, so the account is still reported remaining. Evidence at
  `audit_at` wins.

**Identity**

* 3 of 32 requests use a **historical** email (`case_10, 29, 30`, i.e. an email that
  appears as `old_value` in `user_audit_log.csv`) and all three are `INCOMPLETE`;
  the other 29 use the current email. Three cases cannot prove a rule, but the
  request email is evidently not always the identifier under which the data was stored.
* Case-insensitive matching matters: 15 SQL snapshot files store some emails
  upper-cased (for example `customer_data_inventory_20260213.sql`).

## 5. Two families of samples

* **Starter-style cases (3-digit resource ids)**: `case_01..08, 10` use
  `account_001..010`, `support_002`, `export_001`, etc. They have at most one
  finding, lack the `security_service` verification, and cite older files. Hidden
  cases from the same family (look for 3-digit resource ids) probably behave the same,
  so do not add the audit-record step blindly there.
* **Generated cases (4-digit ids)**: the rest. They always include the account and
  audit-record verification and follow the signatures in section 3.

## 6. NOT_APPLICABLE: nothing to learn from the samples, but the 518 rows have some

All 32 samples are deletion requests, but 10 of the 518 real requests, `case_533`
to `case_542` (the last ten ids, all `reported_status` NOT_STARTED), do **not**
authorise deletion: phone update, promo-email opt-out, data download, login help,
surname correction, marketing-list removal, access request, invoice copy, email change
and a data-copy request. Six of the ten contain deletion words inside a negation or a limited
scope ("I am not asking for deletion", "without removing any of my records", "remove
me from marketing lists only", "do not close the account", "don't delete or change
anything"), so a keyword filter is not enough; the intent classifier has to read the
whole sentence. The expected answer is `NOT_APPLICABLE` with `[]` regardless of what
the evidence shows. `code/evaluation/custom_cases.py` has three such regression cases
(paraphrases of `case_537`, `538`, `539`). Note that every NOT_APPLICABLE request seen
so far has the highest ids, so do not let an id-based shortcut leak into the code.

## 7. Checklist for the planner

1. Decide intent first; `NOT_APPLICABLE` -> `[]`.
2. Resolve current + historical identifiers (email, hash, phone), case-insensitively.
3. Pick the latest citable snapshot per resource (`captured_at <= audit_at`).
4. Emit findings only for non-compliant state: remaining records, exposed PII,
   active flows, restore risks.
5. Derive remediation 1:1 from findings (disable/block first, then delete, redact,
   anonymize), then verification: account absent, audit record present, then each
   remediated resource's check.
6. If there are no findings, emit the full inventory of compliant resources as
   verification steps with evidence.
7. Sort with the documented rule, number steps from 1, run
   `python3 code/evaluation/main.py check --pred output.csv`.
