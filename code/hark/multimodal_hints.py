"""PROVISIONAL multimodal facts (images/PDFs are owned by a teammate's OCR module).

Each PNG/PDF artifact is paired with exactly one LINK_RESOURCE_IDENTITY activity (activity ts = file
captured_at + ~7 min) naming the resource, the subject email and user_id. The resource belongs to the
case whose [requested_at, audit_at] window contains that LINK activity (NOT to every case of that user:
samples case_07 / case_17 show the LINKed resource of an earlier case is not repeated).

The state below was read by hand from a quick tesseract/pdftotext pass (tesseract --psm 6 / pdftotext -layout) and should be
replaced by the teammate's extractor. Keys: resource_id -> (file_id, system_id, treatment, state)
  treatment: DELETE | REDACT | ANONYMIZE | RETAIN | FLOW | BACKUP
  state: present / pii / enabled / restore_allowed flags as in Engine.state_at
"""

HINTS = {
    "account_5762": ("evidence_04", "account_service", "DELETE", dict(present=True, pii=True)),
    "search_428": ("evidence_09", "search_service", "DELETE", dict(present=True, pii=True)),
    "data_flow_1121": ("evidence_14", "integration_service", "FLOW", dict(present=True, enabled=True)),
    "analytics_7438": ("evidence_19", "analytics_platform", "ANONYMIZE", dict(present=True, pii=True)),
    "export_1260": ("evidence_23", "export_service", "DELETE", dict(present=True, pii=True)),
    "export_1842": ("evidence_18", "export_service", "DELETE", dict(present=True, pii=True)),
    "support_1034": ("evidence_28", "support_platform", "REDACT", dict(present=True, pii=True)),
    "backup_9287": ("evidence_32", "backup_service", "BACKUP", dict(present=True, restore_allowed=True)),
    "object_19": ("evidence_46", "object_storage", "DELETE", dict(present=True, pii=True)),
    "marketing_001": ("evidence_60", "marketing_platform", "DELETE", dict(present=True, pii=True)),
    "audit_record_003": ("evidence_37", "security_service", "RETAIN", dict(present=True, pii=False)),
    "tombstone_17": ("evidence_42", "identity_service", "REDACT", dict(present=True, pii=False)),
    # delivery_correction_attachment.pdf (evidence_58): support_001 attachment for priya.raman (sample case_07)
    "support_001": ("evidence_58", "support_platform", "DELETE", dict(present=True, pii=True)),
}

# documents without a LINK activity: resource -> owning email (read from the document text)
DOC_OWNERS = {"support_001": "priya.raman@gmail.com"}
