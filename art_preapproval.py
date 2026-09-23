"""
art_preapproval.py -- ART (Authorized Recurring Transactions) preapproval
check for Invoice Intake (Tier 3, Invoice Processing Intake Plan.md, built
2026-08-02). Sibling to art_completeness.py, not an addition to it --
`approval_engine.py` stays vendor-unaware exactly as its own docstring
already promises, and this module is the one place "does this vendor have
an active ART entry" gets answered.

Simplified per Jay's direct answer this session: an active ART entry means
the request skips the approval chain entirely and lands straight on AP
Review -- no confirmation step of any kind. `art_list.preapproval_scope`
(full_skip / dollar_cap / one_step_confirmation) is left as-is in the
schema, unused for behavioral branching here -- informational only, so
nothing needs a migration or a re-decision if a real distinction is wanted
later.
"""
from __future__ import annotations

from datetime import date

import db


def _amount_flag(art: dict, amount: float) -> str | None:
    """None if `amount` fits this one entry's own amount_check_mode/exact/
    range data, else an informational mismatch message."""
    mode = art.get("amount_check_mode")
    amt = abs(float(amount))
    if mode == "exact" and art.get("amount_exact") is not None:
        exact = float(art["amount_exact"])
        if abs(amt - exact) >= 0.01:
            return f"Amount ${amt:,.2f} differs from this vendor's usual ${exact:,.2f} -- please double-check."
    elif mode == "range" and art.get("amount_min") is not None and art.get("amount_max") is not None:
        lo, hi = float(art["amount_min"]), float(art["amount_max"])
        if not (lo <= amt <= hi):
            return f"Amount ${amt:,.2f} is outside this vendor's usual ${lo:,.2f}-${hi:,.2f} range -- please double-check."
    return None


def vendor_preapproval_status(
    vendor_id: int | None, org_id: int, amount: float | None = None,
) -> dict | None:
    """None if there's no active, currently-authorized ART entry for this
    vendor+org (a brand-new vendor via using_new_vendor has no vendor_id and
    thus no ART entry either). Otherwise:
    {"art_id", "vendor_display_name", "is_monkey_see_monkey_do",
     "special_handling_notes", "amount_flag": str|None}.

    amount_flag is a purely informational note surfaced to AP Review --
    never gates the chain -- set only when `amount` is supplied (the
    request's confirmed total, known at finalize time but not necessarily
    at intake time) and the entry's own amount_check_mode/exact/range data
    suggests this invoice's amount is unusual for the vendor.

    Reuses the same is_active/authorized_from/authorized_through checks
    art_completeness.check_one() already applies, so "active" means the
    identical thing in both places.

    Since migration 063 (art_list widened to UNIQUE(vendor_id, org_id,
    group_label), so one vendor can carry several property-specific
    entries -- e.g. BGE, one row per property) a vendor can have MORE than
    one active row here. There's no field on the request itself naming
    which property/group_label it's for, so when `amount` is known this
    picks whichever active row that amount actually fits (exact/range);
    when none fit, or when `amount` is still unknown (intake time), it
    falls back to the first active row by id -- an arbitrary but stable
    choice, same as the single-row behavior this function always had
    before 063. A real amount-to-property mismatch still surfaces via
    amount_flag either way, it just may name the wrong one of several
    real properties on a genuine mismatch."""
    if not vendor_id:
        return None

    candidates = db.query(
        """
        SELECT al.id, al.is_monkey_see_monkey_do, al.special_handling_notes,
               al.amount_check_mode, al.amount_exact, al.amount_min, al.amount_max,
               v.display_name AS vendor_display_name
        FROM checkreq.art_list al
        JOIN checkreq.vendors v ON v.id = al.vendor_id
        WHERE al.vendor_id = %s AND al.org_id = %s AND al.is_active
          AND (al.authorized_from IS NULL OR al.authorized_from <= CURRENT_DATE)
          AND (al.authorized_through IS NULL OR al.authorized_through >= CURRENT_DATE)
        ORDER BY al.id
        """,
        (vendor_id, org_id),
    )
    if not candidates:
        return None

    art = candidates[0]
    amount_flag = _amount_flag(art, amount) if amount is not None else None
    if amount is not None and amount_flag is not None and len(candidates) > 1:
        for other in candidates[1:]:
            other_flag = _amount_flag(other, amount)
            if other_flag is None:
                art, amount_flag = other, None
                break

    return {
        "art_id": art["id"],
        "vendor_display_name": art["vendor_display_name"],
        "is_monkey_see_monkey_do": art["is_monkey_see_monkey_do"],
        "special_handling_notes": art["special_handling_notes"],
        "amount_flag": amount_flag,
    }
