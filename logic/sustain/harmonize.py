"""Clean one row of sustain.parish_year_v for the page. Pure: no I/O. See CONGREGATIONAL_VITALITY_PLAN.md sections 2 and 3.

The view already applies field_map.csv (it harmonizes the form eras). What this adds, in one place so every page and list agrees:
  * Decimal to float, so the arithmetic after it needs no care.
  * "0 attendance with giving above 0" is treated as not reported (Jay, 2026-10-15, answer 5). A congregation that gives and has
    no Sunday attendance did not report attendance; 0 would draw a false cliff in the trend. Zero attendance with no giving
    (a closed or inactive congregation) stays 0, as filed.
  * The privacy rule again, in case a row did not come through the view: with fewer than 10 pledge cards (or an unknown number)
    the amount pledged is hidden, so no page can show it or an average pledge made from it.
  * The demographics section (average age, share reported white) exists from the 2024 form. A zero average age means the section
    was not filled in (the age and the share go together), so both are treated as not reported: 35 EDOM parishes in 2024 and 39 in
    2025 read 0, and a 0% white share on such a row is a blank, not an answer. A real age with a 0% share is kept.
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

PLEDGE_PRIVACY_MIN = 10  # fewer pledge cards than this: hide the amount pledged and the average pledge, for every viewer

NUMERIC_FIELDS = (
    "asa_in_person", "asa_online", "members", "pledge_cards", "amount_pledged", "plate_pledge", "investments_for_operations",
    "other_operating_revenue", "operating_revenue", "operating_expenses", "total_revenues", "total_expenses",
    "restricted_contributions", "total_cash", "unrestricted_reserves", "full_time_clergy", "avg_age", "race_ethnicity_white_percent",
)


def harmonize_year(row: Mapping) -> dict:
    """Return a cleaned copy of one view row, with `flags` listing what was changed (a list of short codes)."""
    out = dict(row)
    for key in NUMERIC_FIELDS:
        if isinstance(out.get(key), Decimal):
            out[key] = float(out[key])
    flags: list[str] = []

    plate = out.get("plate_pledge")
    if out.get("asa_in_person") == 0 and plate is not None and plate > 0:
        out["asa_in_person"] = None
        flags.append("asa_zero_with_giving")

    cards = out.get("pledge_cards")
    if cards is None or cards < PLEDGE_PRIVACY_MIN:
        out["amount_pledged"] = None

    age = out.get("avg_age")
    if age is None or age <= 0:
        out["avg_age"] = None
        out["race_ethnicity_white_percent"] = None
        if age is not None:
            flags.append("demographics_not_reported")

    out["flags"] = flags
    return out


def average_pledge(row: Mapping):
    """Amount pledged per pledge card, or None when it must stay hidden (fewer than 10 pledge cards) or cannot be worked out."""
    cards, amount = row.get("pledge_cards"), row.get("amount_pledged")
    if cards is None or cards < PLEDGE_PRIVACY_MIN or amount is None:
        return None
    return float(amount) / cards
