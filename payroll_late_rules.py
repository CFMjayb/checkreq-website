"""
payroll_late_rules.py -- the small, database-only rules of the Late Email Queue (26-158 / 26-129, 2026-10-27).
Plan: 26-158 DME Payroll Inbox Mining\\Plan - Late Email Queue.md (approved by Jay 2026-10-27).

THE HARD RULE: hours only. Nothing here reads, stores or returns a rate, salary, wage or any amount of money.

An emailed-hours message that must NOT be recorded straight away is held in portal.time_late_email (migration 105) for a person to decide.
This file holds what other Beacon modules need without importing the actions (payroll_totals.finalize_period asks waiting_count() to
refuse "Mark hours Final" while anything waits, so it must not import payroll_totals back): the vocabulary, waiting_count(), and the
repeat check line_state(). The actions (record, carry, manual check, dismiss, pick the church, the alert) live in payroll_late.py.

THE REPEAT CHECK (Jay, 2026-10-27: hours already submitted and accepted must not be entered again): a line of a held email is compared
with what is already entered for that person, that kind of hours and that pay period:
  same        the same hours are already entered (accepted or waiting for review): a repeat, never entered twice
  different   other hours are entered: a correction, not a repeat (it follows the normal rules when recorded)
  new         nothing is entered yet
  unmatched   the reader could not match the line to an employee, so it cannot be compared until the church and person are picked
A line whose earlier entry was REJECTED counts as new: the existing rule is that the same figure arriving again reopens a rejected line.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

import db
from psycopg import errors as pgerr

REASONS = ("period_final", "period_closed", "parish_unknown", "no_period")
KINDS = ("hours", "change")
STATUSES = ("waiting", "recorded", "carried", "manual_check", "dismissed")
REASON_LABEL = {"period_final": "After Final", "period_closed": "After Close", "parish_unknown": "Church unknown",
                "no_period": "No pay period yet"}
STATUS_LABEL = {"waiting": "Waiting", "recorded": "Recorded", "carried": "Carried to next period",
                "manual_check": "Paid by manual check", "dismissed": "Dismissed"}
SAME, DIFFERENT, NEW, UNMATCHED = "same", "different", "new", "unmatched"


def d2(x) -> Decimal | None:
    try:
        return Decimal(str(x)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None


def waiting_count(org_id: int) -> int:
    """How many held emails wait for a decision (0 where migration 105 has not been applied yet, so nothing breaks there)."""
    try:
        r = db.query_one("SELECT count(*) AS n FROM portal.time_late_email WHERE org_id = %s AND status = 'waiting'", (org_id,))
    except pgerr.UndefinedTable:
        return 0
    return int(r["n"]) if r else 0


def line_state(org_id: int, period_id: int | None, parish_code: str | None, employee_number: str | None,
               category_key: str, hours) -> dict:
    """The repeat check for ONE person and ONE kind of hours: {'state': same|different|new|unmatched, 'entered': Decimal|None,
    'review_status': str|None}. 'entered' is what Beacon already holds for that person, kind and period."""
    if not (period_id and parish_code and employee_number):
        return {"state": UNMATCHED, "entered": None, "review_status": None}
    row = db.query_one(
        "SELECT t.hours, t.review_status FROM portal.time_period_totals t "
        "JOIN portal.staff_roster sr ON sr.id = t.staff_id "
        "JOIN portal.parishes p ON p.id = sr.parish_id "
        "JOIN portal.timekeeping_categories c ON c.id = t.category_id "
        "WHERE t.period_id = %s AND p.org_id = %s AND p.code = %s AND sr.employee_number = %s "
        "AND c.org_id = %s AND c.key = %s AND t.review_status <> 'rejected' "
        "ORDER BY t.id DESC LIMIT 1",
        (period_id, org_id, str(parish_code).strip(), str(employee_number).strip(), org_id, str(category_key).strip().lower()))
    if not row:
        return {"state": NEW, "entered": None, "review_status": None}
    entered, wanted = d2(row["hours"]), d2(hours)
    same = entered is not None and wanted is not None and entered == wanted
    return {"state": SAME if same else DIFFERENT, "entered": entered, "review_status": row["review_status"]}
