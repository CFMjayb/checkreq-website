"""
payroll_decisions.py -- how a difference between Beacon's hours and the paid hours is SETTLED at close-out
(26-158 / 26-129, 2026-10-22, Jay: "line by line or by employee").

THE HARD RULE: hours only. Nothing here reads, stores or returns a rate, salary, wage or any amount of money.

A "difference" is Beacon's hours minus the paid hours, for one employee and one category of a closed pay period. Each differing
line gets ONE decision (table portal.time_variance_decisions, migration 098):

  next_period   Beacon is higher than paid, and the extra hours WILL BE PAID IN THE FOLLOWING PAY PERIOD.
                This period's line takes the paid hours. The difference is added to that person's line in the same category
                of the following period (flag `carried_in`), so it shows in Time Review and on the Checkwriters Excel to be keyed.
  manual_check  (shown as 'Manual check / void-reissue') The difference was settled OUTSIDE the regular payroll run, in either direction:
                Beacon is higher than paid and the extra hours were ALREADY PAID BY AN INTERPAY MANUAL CHECK, or Beacon is lower
                than paid, Beacon was right, and the over-paid check was VOIDED AND REISSUED. Either way the correction appears on the
                following period's register (as +hours for the manual check, as net negative hours for the void and reissue). This
                period's line stays as entered (the hours are correct for this period). Nothing is added to the following period, so the
                hours are never rekeyed in the next main payroll run, and that period's tie-out expects the (signed) amount on its register.
  beacon_wrong  Beacon is wrong and the paid hours are right (what Actualize always did): the line takes the paid hours.

next_period is only valid when Beacon is higher than paid (the extra hours were not paid yet). manual_check and beacon_wrong are
valid for any difference (Jay, 2026-10-24: a void and reissue is the mirror image of a manual check).

This file holds the rules that need no other Beacon module (pure checks and the reads of the decision table), so that
payroll_totals.variance() can use them without a circular import. Setting, clearing and APPLYING decisions lives in
payroll_closeout.py, next to Actualize.
"""
from __future__ import annotations

from decimal import Decimal

import db
from psycopg import errors as pgerr

NEXT_PERIOD = "next_period"
MANUAL_CHECK = "manual_check"
BEACON_WRONG = "beacon_wrong"
DECISIONS = (NEXT_PERIOD, MANUAL_CHECK, BEACON_WRONG)
LABELS = {
    NEXT_PERIOD: "Include in next period",
    MANUAL_CHECK: "Manual check / void-reissue",
    BEACON_WRONG: "Beacon wrong",
}
EXPLAIN = {
    NEXT_PERIOD: "Beacon is higher than paid, and the extra hours will be paid in the next pay period. This period takes the "
                 "paid hours and the difference is added to the next period's hours, to be keyed there.",
    MANUAL_CHECK: "Settled outside the regular run: a manual check for hours Beacon has that the register does not (Beacon higher), or "
                  "a check that paid more than Beacon, which was voided and reissued (Beacon lower, Beacon was right). This period's "
                  "hours stay as entered. Nothing is keyed. The next register is expected to show the amount.",
    BEACON_WRONG: "Beacon is wrong and the paid hours are right. Beacon takes the paid hours.",
}
NOTE_MAX = 300
TOLERANCE = Decimal("0.005")


def d2(v) -> Decimal:
    """Hours as a two-place Decimal (None counts as 0)."""
    return Decimal(str(v if v is not None else 0)).quantize(Decimal("0.01"))


def valid_for(decision: str, difference) -> str | None:
    """Why this decision cannot be used for a line with this difference (Beacon minus paid), or None when it can."""
    if decision not in DECISIONS:
        return "That is not one of the three decisions."
    diff = d2(difference)
    if abs(diff) < TOLERANCE:
        return "This line already matches the paid hours."
    if decision == NEXT_PERIOD and diff < 0:
        return (f"{LABELS[decision]} is only for hours Beacon has that were not paid. Here Beacon has fewer hours than were paid: "
                f"use {LABELS[MANUAL_CHECK]} if a check was voided and reissued, or {LABELS[BEACON_WRONG]}.")
    return None


def clean_note(text) -> str | None:
    """A short free note (a check number, a date). Trimmed, cut to 300 characters, empty means none."""
    t = " ".join(str(text or "").split())
    return t[:NOTE_MAX] or None


def for_period(period_id: int) -> dict:
    """The decisions made against this period, keyed (staff_id, category_id). {} when the table does not exist yet
    (migration 098 not applied), so the tie-out and the email reader still work."""
    try:
        rows = db.query("SELECT * FROM portal.time_variance_decisions WHERE period_id = %s", (period_id,))
    except pgerr.UndefinedTable:
        return {}
    return {(r["staff_id"], r["category_id"]): r for r in rows}


def is_stale(decision_row: dict, difference) -> bool:
    """The paid hours (or Beacon's) changed since the decision was made, so it was made about a different difference."""
    return abs(d2(decision_row["hours"]) - d2(difference)) >= TOLERANCE


def manual_expected(period_id: int) -> dict:
    """Hours the PREVIOUS period's manual checks are expected to add to THIS period's register, keyed (staff_id, category_id)."""
    out: dict = {}
    try:
        rows = db.query(
            "SELECT staff_id, category_id, SUM(hours) AS h FROM portal.time_variance_decisions "
            "WHERE target_period_id = %s AND decision = 'manual_check' GROUP BY staff_id, category_id", (period_id,))
    except pgerr.UndefinedTable:
        return out
    for r in rows:
        out[(r["staff_id"], r["category_id"])] = Decimal(r["h"])
    return out


def carried_in(period_id: int, staff_id: int, category_id: int) -> Decimal:
    """Hours already carried INTO this period for this person and category by an applied 'paid next period' decision.
    An emailed figure that arrives later is stored on top of these, never over them."""
    try:
        r = db.query_one(
            "SELECT COALESCE(SUM(hours), 0) AS h FROM portal.time_variance_decisions "
            "WHERE target_period_id = %s AND staff_id = %s AND category_id = %s AND decision = 'next_period' "
            "AND applied_at IS NOT NULL", (period_id, staff_id, category_id))
    except pgerr.UndefinedTable:
        return Decimal(0)
    return Decimal(r["h"]) if r else Decimal(0)


def manual_by_staff(period_id: int) -> list:
    """Per person and department: the signed hours the PREVIOUS period settled by manual check or void-reissue that this period's
    register is expected to show. For the Checkwriters Excel's own column (never keyed, only added into a total)."""
    try:
        rows = db.query(
            "SELECT v.staff_id, SUM(v.hours) AS hours, sr.employee_number, sr.first_name, sr.last_name, p.code AS parish_code, "
            "p.name AS parish_name, COALESCE(p.short_name, p.name) AS dept_name "
            "FROM portal.time_variance_decisions v JOIN portal.staff_roster sr ON sr.id = v.staff_id "
            "JOIN portal.parishes p ON p.id = sr.parish_id "
            "WHERE v.target_period_id = %s AND v.decision = 'manual_check' "
            "GROUP BY v.staff_id, sr.employee_number, sr.first_name, sr.last_name, p.code, p.name, p.short_name", (period_id,))
    except pgerr.UndefinedTable:
        return []
    return rows


def prior_period_notes(period_id: int) -> dict:
    """What earlier periods' decisions mean for THIS period (for the close-out page):
      carried_in    lines whose hours were added here and are to be keyed in this period's payroll run
      manual_checks lines already paid by an interpay manual check: NOT to be keyed again, expected on this period's register."""
    try:
        rows = db.query(
            "SELECT v.decision, v.hours, v.note, v.applied_at, c.key AS cat_key, c.label AS cat_label, sr.employee_number, "
            "sr.first_name, sr.last_name, p.code AS parish_code, COALESCE(p.short_name, p.name) AS dept_name, "
            "pp.label AS from_label "
            "FROM portal.time_variance_decisions v JOIN portal.staff_roster sr ON sr.id = v.staff_id "
            "JOIN portal.parishes p ON p.id = sr.parish_id JOIN portal.timekeeping_categories c ON c.id = v.category_id "
            "JOIN portal.payroll_periods pp ON pp.id = v.period_id "
            "WHERE v.target_period_id = %s AND v.decision IN ('next_period', 'manual_check') "
            "ORDER BY p.code, sr.last_name, sr.first_name, c.sort_order", (period_id,))
    except pgerr.UndefinedTable:
        rows = []
    return {"carried_in": [r for r in rows if r["decision"] == NEXT_PERIOD and r["applied_at"] is not None],
            "manual_checks": [r for r in rows if r["decision"] == MANUAL_CHECK]}
