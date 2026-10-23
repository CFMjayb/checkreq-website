"""
dates.py -- due dates from an anchor date. Pure: no database.

Anchors (hr.template_items.due_basis): start_date (hire / first day), separation_date (last day), benefits_effective
(the 1st of the month after the hire date, Plan section 4), opened (the day the case was opened).
"""
from __future__ import annotations

import datetime as dt


def benefits_effective(start: dt.date | None) -> dt.date | None:
    """The 1st of the month after the date of hire. A hire dated the 1st also starts the 1st of the NEXT month
    (Jay, 2026-10-22)."""
    if not start:
        return None
    return dt.date(start.year + (start.month == 12), start.month % 12 + 1, 1)


def add_days(d: dt.date, n: int, business: bool = False) -> dt.date:
    """d + n calendar days, or n business days (Monday-Friday; holidays are not modeled) when business is True."""
    if not business:
        return d + dt.timedelta(days=n)
    step = 1 if n >= 0 else -1
    left = abs(n)
    cur = d
    while left:
        cur += dt.timedelta(days=step)
        if cur.weekday() < 5:
            left -= 1
    return cur


def anchor(basis: str, case: dict) -> dt.date | None:
    if basis == "start_date":
        return case.get("start_date")
    if basis == "separation_date":
        return case.get("separation_date")
    if basis == "benefits_effective":
        return case.get("benefits_effective_date") or benefits_effective(case.get("start_date"))
    if basis == "opened":
        o = case.get("opened_at")
        if isinstance(o, dt.datetime):
            return o.date()
        return o or dt.date.today()
    return None


def due_dates(item: dict, case: dict) -> tuple[dt.date | None, dt.date | None]:
    """(due_date, hard_deadline_date) for a template item on a case; None where the anchor date is not known yet."""
    a = anchor(item["due_basis"], case)
    if not a:
        return None, None
    business = bool(item.get("business_days"))
    due = add_days(a, int(item.get("due_offset_days") or 0), business)
    hard = item.get("hard_deadline_days")
    hard_d = add_days(a, int(hard), business) if hard not in (None, "") else None
    return due, hard_d


def bucket(basis: str, offset: int) -> tuple[int, str]:
    """(sort key, label) for grouping a case's items in time, the way Gusto groups Before / On / After Day 1."""
    o = int(offset or 0)
    if basis == "start_date":
        if o < 0:
            return 0, "Before day 1"
        if o == 0:
            return 1, "Day 1"
        if o <= 7:
            return 2, "First week"
        if o <= 31:
            return 3, "First month"
        return 4, "Later"
    if basis == "separation_date":
        if o < 0:
            return 0, "Before the last day"
        if o == 0:
            return 1, "Last day"
        return 2, "After the last day"
    if basis == "benefits_effective":
        return 3, "After benefits start"
    return 0, "On request"


def days_until(d: dt.date | None, today: dt.date | None = None) -> int | None:
    if not d:
        return None
    return (d - (today or dt.date.today())).days
