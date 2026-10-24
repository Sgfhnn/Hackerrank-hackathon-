"""Deletion-intent classifier for `request_text` (Muse, task 4).

First gate of the audit (AGENTS.md section 6.5): a case is only a deletion
case when the person actually authorized deletion. A request for access,
correction, export, marketing opt-out or account help is NOT_APPLICABLE,
even when other fields resemble a deletion case.

Design: rules first, LLM fallback second.

  1. Explicit negations / keep-account signals win over everything
     ("I am not asking for deletion", "Do not close the account",
     "leave my profile in place", "I still use it"). These texts often
     contain the words "deletion" / "erase" / "close" themselves, so they
     must be checked before any deletion pattern.
  2. Strong deletion patterns (imperative or audit-of-deletion phrasing)
     in every language present in the dataset (EN, ES, PT, FR, JA, KO).
     Audit phrasing counts: "verify that my account was deleted",
     "confirm the profile is gone" presuppose a deletion request and the
     sample answers treat them as deletion cases.
  3. Non-deletion categories: correction, email change, export/download,
     information access, invoice copy, marketing opt-out, login help.
  4. Weak deletion patterns ("close my account", "remove my profile")
     when no non-deletion category matched.
  5. Default: NOT_DELETION with low confidence and needs_llm=True.
     Defaulting an ambiguous request to deletion would be the unsafe
     direction; the LLM fallback (or a human) decides those.

The classifier never looks at case_id and never hardcodes labels. The
LLM prompt is kept in this file (INTENT_LLM_PROMPT) so prompts ship in
source, per the team rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DELETION = "DELETION_REQUESTED"
NOT_DELETION = "NOT_DELETION"

INTENT_LLM_PROMPT = """You are the intent gate for a deletion-audit system.
Read the customer's message and decide whether it authorizes the permanent
deletion/erasure of their account and personal data.

Answer DELETION_REQUESTED only when the person asks for their account or
personal data to be deleted, erased, removed or permanently closed, or asks
you to verify/audit a deletion they already requested.
Answer NOT_DELETION for anything else, including: data access or a copy of
their information, downloading/exporting their data, correcting or updating
details (name, email, phone, address), marketing opt-out while keeping the
account, login/password help, invoices/receipts, or any message that says
to keep the account or not to delete anything.

Respond with exactly one JSON object, no other text:
{"intent": "DELETION_REQUESTED" | "NOT_DELETION", "category": "<short snake_case category>", "reason": "<one short sentence>"}

Customer message:
\"\"\"{request_text}\"\"\"
"""


@dataclass
class IntentResult:
    intent: str  # DELETION or NOT_DELETION
    category: str  # deletion, correction, export, access, ...
    confidence: float  # 0..1 for the rule layer
    matched: list[str] = field(default_factory=list)
    language: str = "en"
    needs_llm: bool = False


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


# 1. Explicit negations / keep-account signals (checked first).
_NEGATION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("not_asking_for_deletion", _rx(r"not asking for deletion|not a request to erase|not requesting deletion")),
    ("do_not_delete_or_close", _rx(r"do not close|don't delete|do not delete|don'?t change anything")),
    ("leave_in_place", _rx(r"leave (the|my) (account|profile).{0,30}(active|in place)|leave my profile")),
    ("keep_account", _rx(r"keep the account|please keep the account")),
    ("still_uses_account", _rx(r"i still (need|use|want)|still need the account|want to continue using")),
    ("without_removing", _rx(r"without removing")),
]

# 2a. Strong deletion patterns, English.
_STRONG_EN: list[tuple[str, re.Pattern]] = [
    ("delete_verb", _rx(r"\bdelet(e|es|ed|ing|ion)\b")),
    ("erase_verb", _rx(r"\beras(e|es|ed|ing)\b")),
    ("remove_account_data", _rx(r"remove (my|the|any|records|my profile|the profile)")),
    ("remove_search_result", _rx(r"remove that search result")),
    ("permanent_close", _rx(r"close this account permanently|closed? (my|this) account and process|close my account and")),
]

# 2b. Audit-of-deletion phrasing (a deletion was requested; verify it).
_AUDIT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("audit_of_deletion", _rx(r"(verify|confirm|check|inspect|audit|tell me whether|whether).{0,90}(deleted|deletion|removed|is gone)")),
    ("deleted_then_verify", _rx(r"(deleted|removed).{0,60}(verify|confirm|check)")),
]

# 2c. Strong deletion patterns, other dataset languages.
_STRONG_I18N: list[tuple[str, str, re.Pattern]] = [
    ("es", "es_delete", re.compile(r"eliminaci[oó]n|eliminar|supresi[oó]n", re.IGNORECASE)),
    ("pt", "pt_delete", re.compile(r"exclus[aã]o|excluir|elimina[cç][aã]o|apagar a conta", re.IGNORECASE)),
    ("fr", "fr_delete", re.compile(r"suppression|supprimer", re.IGNORECASE)),
    ("ja", "ja_delete", re.compile(r"削除")),
    ("ko", "ko_delete", re.compile(r"삭제")),
]

# 3. Non-deletion categories.
_NON_DELETION_CATEGORIES: list[tuple[str, re.Pattern]] = [
    ("correction", _rx(r"misspelled|correct it|replace it with the new|out of date.{0,40}replace")),
    ("email_change", _rx(r"change the email address used for this account")),
    ("export", _rx(r"download my account information|portable format")),
    ("access", _rx(r"what personal information you hold|copy of the information held|send me a copy of the information")),
    ("invoice_copy", _rx(r"copy of last year'?s invoices")),
    ("marketing_optout", _rx(r"stop the promotional emails|remove me from marketing lists only")),
    ("account_help", _rx(r"resetting the login|help resetting")),
]

# 4. Weak deletion patterns (only when nothing above matched).
_WEAK_DELETION: list[tuple[str, re.Pattern]] = [
    ("close_account", _rx(r"close (my|this|the) account|account (to be )?closed|closing my account")),
    ("no_longer_need_account", _rx(r"no longer (need|use) this account")),
]


def detect_language(text: str) -> str:
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\uac00-\ud7a3]", text):
        return "ko"
    low = text.lower()
    if any(w in low for w in ("suppression", "je demande", "veuillez")):
        return "fr"
    if any(w in low for w in ("exclusão", "solicito a", "conta associada")):
        return "pt"
    if any(w in low for w in ("eliminación", "solicito la", "cuenta")):
        return "es"
    return "en"


def classify_intent_rules(request_text: str) -> IntentResult:
    """Rule layer only. Returns an IntentResult; needs_llm flags ambiguity."""
    text = (request_text or "").strip()
    lang = detect_language(text)

    for name, pat in _NEGATION_PATTERNS:
        if pat.search(text):
            return IntentResult(NOT_DELETION, "explicit_keep_or_negation", 0.97, [name], lang)

    for name, pat in _STRONG_EN + _AUDIT_PATTERNS:
        if pat.search(text):
            return IntentResult(DELETION, "deletion", 0.95, [name], lang)

    for code, name, pat in _STRONG_I18N:
        if pat.search(text):
            return IntentResult(DELETION, "deletion", 0.95, [name], code)

    for category, pat in _NON_DELETION_CATEGORIES:
        if pat.search(text):
            return IntentResult(NOT_DELETION, category, 0.93, [category], lang)

    for name, pat in _WEAK_DELETION:
        if pat.search(text):
            return IntentResult(DELETION, "deletion", 0.8, [name], lang)

    return IntentResult(NOT_DELETION, "unknown", 0.4, [], lang, needs_llm=True)


def classify_intent(request_text: str, llm_call=None) -> IntentResult:
    """Rules first; if ambiguous and an llm_call is supplied, ask the LLM.

    llm_call is a callable taking the formatted prompt and returning the
    model's text (expected: the JSON object described in INTENT_LLM_PROMPT).
    Keeping the call injectable means no provider or key is baked in here.
    """
    result = classify_intent_rules(request_text)
    if not result.needs_llm or llm_call is None:
        return result

    import json

    raw = llm_call(INTENT_LLM_PROMPT.format(request_text=request_text))
    try:
        parsed = json.loads(raw)
        intent = parsed.get("intent")
        if intent in (DELETION, NOT_DELETION):
            return IntentResult(
                intent,
                parsed.get("category", "llm_fallback"),
                0.85,
                ["llm_fallback"],
                result.language,
            )
    except (ValueError, AttributeError):
        pass
    # Unparseable LLM answer: stay on the safe side.
    return result


if __name__ == "__main__":
    import sys

    print(classify_intent(" ".join(sys.argv[1:])))
