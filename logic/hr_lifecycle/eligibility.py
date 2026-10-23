"""
eligibility.py -- scheduled hours to benefits eligibility. Pure: no database.

CPG: the Denominational Health Plan covers clergy and lay employees scheduled for at least 1,500 hours a year
(Resolution A177); lay employees scheduled for at least 1,000 hours a year must be given a pension. The thresholds are
per-entity settings (hr.entity_settings) so a diocese with a more generous policy can lower them; HR can also override
one case with a note.
"""
from __future__ import annotations

from decimal import Decimal

WEEKS = 52


def annual_hours(hours_per_week) -> Decimal | None:
    if hours_per_week in (None, ""):
        return None
    return Decimal(str(hours_per_week)) * WEEKS


def compute(hours_per_week, health_threshold: int = 1500, pension_threshold: int = 1000) -> tuple[bool | None, bool | None]:
    """(health_eligible, pension_eligible); (None, None) when the hours are not known yet."""
    yearly = annual_hours(hours_per_week)
    if yearly is None:
        return None, None
    return yearly >= health_threshold, yearly >= pension_threshold


def parse_hours(raw) -> Decimal | None:
    """Hours per week from a form box: blank -> None; 0 to 80 with up to two decimals."""
    if raw in (None, ""):
        return None
    try:
        h = Decimal(str(raw).strip())
    except Exception:
        raise ValueError("Hours per week must be a number, like 40 or 22.5.")
    if h < 0 or h > 80:
        raise ValueError("Hours per week must be between 0 and 80.")
    return h.quantize(Decimal("0.01"))
