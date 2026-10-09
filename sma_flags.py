"""
sma_flags.py -- 26-129 SMA letters (plan revision 12): the flags on a parish's row of the check sheet.

A flag is {"code": str, "severity": "block" | "warn" | "info", "text": str}. A parish with ANY "block" flag cannot be
posted. Flags are always RE-DERIVED from the letter's current data (never patched one at a time), so they can never
drift from the data: sma_store.refresh_letter() calls compute_flags() after every change and stores the result for
display.

  block   cannot be posted until fixed (or the parish is excluded)
  warn    look at it; posting is allowed
  info    context only

Pure function over dicts (no database, no network).
"""
from __future__ import annotations

import sma_calc as C
import sma_signers

BLOCK, WARN, INFO = "block", "warn", "info"
SEVERITY_ORDER = {BLOCK: 0, WARN: 1, INFO: 2}
MAX_NAME = 90
EXPECTED_LETTER_PAGES = 2
OK_LABELS = {"", "ok"}

_ADJ_WORDS = {
    "half": "a 50% reduction",
    "latest_year": "the latest-year test (Formula B)",
    "custom": "a custom amount",
}


def _flag(code: str, severity: str, text: str) -> dict:
    return {"code": code, "severity": severity, "text": text}


def _dollars(n) -> str:
    return "${:,.0f}".format(float(n))


def compute_flags(L: dict) -> list[dict]:
    """L is one letter row with these keys (missing ones are treated as 'nothing known'):
    status, parish_id, parish_active, tie_out, formula_total, total_allocation, adjustment_kind, model_total,
    letter_name, signers (list), current_version, quality (dict: noi, noe, filed, edited, suggested_adjustment,
    letter_pages, built_hash, current_hash, input_differs)."""
    status = L.get("status")
    q = L.get("quality") or {}
    flags: list[dict] = []
    if status == "excluded":
        return [_flag("excluded", INFO, "Excluded from this run. No letter will be posted for this parish.")]

    if not L.get("parish_id"):
        flags.append(_flag("unmatched", BLOCK, "Not matched to a Beacon parish yet. Choose the parish."))
    elif L.get("parish_active") is False:
        flags.append(_flag("inactive_parish", BLOCK, "The matched Beacon parish is inactive (closed or merged). "
                           "Exclude it, or match the right parish."))

    kind = L.get("adjustment_kind") or "none"
    suggested = q.get("suggested_adjustment")
    if q.get("input_differs"):
        flags.append(_flag("input_differs", BLOCK, "The model's own inputs differ from its yearly figures ("
                           + ", ".join(q["input_differs"]) + "). Check the model for this parish."))
    if L.get("tie_out") == "mismatch":
        if q.get("edited"):
            flags.append(_flag("figures_edited", WARN, "The figures were edited here, so they no longer tie to the "
                               "model. The reason is in the notes."))
        elif suggested and kind == "none":
            flags.append(_flag("adjustment_needed", BLOCK,
                               f"The model shows {_dollars(L.get('model_total') or 0)}, which is "
                               f"{_ADJ_WORDS[suggested]} applied to the formula result of {_dollars(L.get('formula_total') or 0)}. "
                               "Confirm that adjustment, or use the plain formula result."))
        else:
            flags.append(_flag("tie_mismatch", BLOCK, "Beacon's result differs from the model's. Check the figures "
                               "against the model."))
    if L.get("tie_out") == "none":
        flags.append(_flag("not_tied", WARN, "The model has no figures for this parish to check Beacon's result against."))
    if kind != "none":
        flags.append(_flag("adjustment_confirmed", INFO, f"Adjusted: the letter prints {_ADJ_WORDS.get(kind, 'an adjustment')} "
                           f"({_dollars(L.get('total_allocation') or 0)} instead of {_dollars(L.get('formula_total') or 0)})."))

    for key, label in (("noi", "operating income"), ("noe", "operating expense")):
        v = str(q.get(key) or "").strip()
        if v.lower() not in OK_LABELS:
            flags.append(_flag(f"model_{key}", WARN, f'The model marks this parish\'s {label} as "{v}".'))
    if str(q.get("filed") or "").strip().lower() == "no":
        year = q.get("year3")
        flags.append(_flag("not_filed", WARN, f"No {year} parochial report is on file for this parish." if year
                           else "No parochial report for the latest year is on file for this parish."))
    if q.get("blank"):
        flags.append(_flag("blank_figures", WARN, "These cells are blank in the model and were read as 0: "
                           + ", ".join(str(b) for b in q["blank"]) + "."))
    if all(L.get(k) is not None for k in ("noi_y1", "noi_y2", "noi_y3")):
        y1, y2, y3 = C.D(L["noi_y1"]), C.D(L["noi_y2"]), C.D(L["noi_y3"])
        if abs(y2 - (y1 + y3) / 2) > 1 and not q.get("edited"):
            flags.append(_flag("year2_differs", WARN, "The middle-year income is not the average of the other two, but the "
                               "letter says it is estimated that way. Check the model."))
    if q.get("build_error"):
        flags.append(_flag("build_failed", WARN, f"The last build of this letter failed: {q['build_error']} "
                           "Fix the cause, then press Build this letter now."))

    if L.get("total_allocation") is not None and int(L["total_allocation"]) <= 0:
        flags.append(_flag("zero_allocation", WARN, "The allocation is zero."))

    problems = sma_signers.validate_signers(L.get("signers") or [])
    if not (L.get("signers") or []) or problems:
        for text in (problems or ["No signer is chosen."]):
            flags.append(_flag("signers", BLOCK, text))

    name = (L.get("letter_name") or "").strip()
    if not name:
        flags.append(_flag("name_missing", BLOCK, "The letter has no parish name."))
    elif len(name) > MAX_NAME:
        flags.append(_flag("name_long", WARN, f"The parish name is {len(name)} characters, which may not fit the form."))

    if (L.get("current_version") or 0) > 0:
        pages = q.get("letter_pages")
        if pages is not None and pages != EXPECTED_LETTER_PAGES:
            flags.append(_flag("letter_pages", WARN, f"The letter ran to {pages} pages (expected {EXPECTED_LETTER_PAGES}). "
                               "The parish name or the adjustment words may be too long."))
        if q.get("built_hash") and q.get("built_hash") != q.get("current_hash"):
            flags.append(_flag("pdf_stale", BLOCK, "The figures, name or cover letter changed after this letter was "
                               "built. Rebuild it."))
    flags.sort(key=lambda f: SEVERITY_ORDER[f["severity"]])
    return flags


def blocking(flags: list[dict]) -> list[dict]:
    return [f for f in flags if f["severity"] == BLOCK]


def is_ready(L: dict, flags: list[dict]) -> bool:
    """Built, not excluded, and nothing blocks it."""
    return L.get("status") == "created" and (L.get("current_version") or 0) > 0 and not blocking(flags)
