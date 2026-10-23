"""
privacy.py -- refuse free text that looks like personal data before it reaches schema hr.

Notes on a case, an item or an enrollment are for working notes ("form sent to CPG 10/24", "waiting on the rector").
The signed forms carry the SSNs, birth dates and bank numbers; a note must not. This is a guard, not a classifier: it
refuses the obvious shapes and tells the person why, so nothing sensitive is stored by accident.
"""
from __future__ import annotations

import re

from .errors import HrError

MAX_NOTE = 1000

_SSN = re.compile(r"\b\d{3}[- ]?\d{2}[- ]?\d{4}\b")
_LONG_DIGITS = re.compile(r"\b\d{8,17}\b")  # bank account / routing / card-like runs
_DOB_WORDS = re.compile(r"\b(dob|d\.o\.b|date of birth|birth ?date|born)\b", re.I)
_SSN_WORDS = re.compile(r"\b(ssn|social security|itin|tin)\b", re.I)
_BANK_WORDS = re.compile(r"\b(routing|account (no|number|#)|acct)\b", re.I)
_DATE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")


def check_text(text: str | None, field: str = "Note") -> str | None:
    """The cleaned text (or None for blank). Raises HrError('pii') for text that looks like an SSN, a bank number,
    or a birth date, and HrError('invalid') when it is too long."""
    if text is None:
        return None
    t = str(text).strip()
    if not t:
        return None
    if len(t) > MAX_NOTE:
        raise HrError("invalid", f"{field} is too long (keep it under {MAX_NOTE} characters).")
    if _SSN.search(t) or (_SSN_WORDS.search(t) and re.search(r"\d{4}", t)):
        raise HrError("pii", f"{field} looks like it holds a Social Security number. Keep those on the signed form, not in Beacon.")
    if _LONG_DIGITS.search(t) or (_BANK_WORDS.search(t) and re.search(r"\d{4}", t)):
        raise HrError("pii", f"{field} looks like it holds a bank or account number. Keep those on the signed form, not in Beacon.")
    if _DOB_WORDS.search(t) and _DATE.search(t):
        raise HrError("pii", f"{field} looks like it holds a date of birth. Keep that on the signed form, not in Beacon.")
    return t
