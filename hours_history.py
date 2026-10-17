"""
hours_history.py -- the hours history screens (26-158 / 26-129, 2026-10-17).

Jay: "a screen of history of hours, with a row for each period with totals for each time category."

    /admin/timekeeping/employees/{staff_id}/hours-history     one person
    /admin/timekeeping/parish/{parish_id}/hours-history       one department (every person at that parish)

One row per pay period (newest first, periods that have not started are left out), with the hours Beacon COUNTS for each kind of
hours (Reg, OT, Sick, Vac, Holiday, plus any other kind that holds hours), the total, and the PAID total where the paid hours have
been loaded. A row also says how many lines are still waiting for review, because those are not counted yet. HOURS ONLY: nothing
here reads, stores or shows pay.

The counting is payroll_totals._period_rows, the same rule Time Review, the Excel export and the Variance report use, so this
screen can never disagree with them. Pay #1 to #20 were loaded from the paid labor distribution, so for those periods Beacon's
hours ARE the paid hours (there is no separate paid figure).

hr_admin or beacon_admin of the diocese, like every Timekeeping screen; the person / parish is looked up through the diocese so a
crafted id from another diocese resolves to nothing.
"""
from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

import category_order
import db
import payroll_totals as pt

router = APIRouter()

_current_user = None
_current_org = None
_render = None
_gate = None  # payroll_screens._require_hr_admin


def register(app, *, current_user, current_org, render, gate) -> None:
    global _current_user, _current_org, _render, _gate
    _current_user, _current_org, _render, _gate = current_user, current_org, render, gate
    app.include_router(router)


def _f(v) -> float | None:
    return None if v is None else float(v)


def build_history(org_id: int, *, staff_id: int | None = None, parish_id: int | None = None) -> dict:
    """The history for one person (staff_id) or one department (parish_id), newest period first. Hours only."""
    periods = db.query(
        "SELECT id, label, period_start, period_end, pay_date, status, hours_finalized_at FROM portal.payroll_periods "
        "WHERE org_id = %s AND status <> 'future' ORDER BY period_start DESC", (org_id,))
    cats = category_order.sort_categories(db.query(
        "SELECT id, key, label, sort_order, is_active FROM portal.timekeeping_categories WHERE org_id = %s ORDER BY sort_order",
        (org_id,)))
    where, params = ["p.org_id = %s"], [org_id]
    if staff_id is not None:
        where.append("rh.staff_id = %s")
        params.append(staff_id)
    if parish_id is not None:
        where.append("sr.parish_id = %s")
        params.append(parish_id)
    paid_by_period: dict = {}
    for r in db.query(
            "SELECT rh.period_id, rh.hours FROM portal.time_register_hours rh JOIN portal.staff_roster sr ON sr.id = rh.staff_id "
            "JOIN portal.parishes p ON p.id = sr.parish_id WHERE " + " AND ".join(where), tuple(params)):
        paid_by_period[r["period_id"]] = paid_by_period.get(r["period_id"], Decimal(0)) + Decimal(r["hours"])

    rows, used = [], set()
    for per in periods:
        by_cat: dict = {}
        waiting = 0
        for r in pt._period_rows(org_id, per["id"]):
            if staff_id is not None and r["staff_id"] != staff_id:
                continue
            if parish_id is not None and r["parish_id"] != parish_id:
                continue
            h = r["counted_hours"]
            if h is None:
                waiting += 1  # a line waiting for a decision is not counted yet
                continue
            by_cat[r["category_id"]] = by_cat.get(r["category_id"], Decimal(0)) + Decimal(h)
        used |= {cid for cid, v in by_cat.items() if v}
        rows.append({"period": per, "by_cat": by_cat, "total": sum(by_cat.values(), Decimal(0)),
                     "paid": paid_by_period.get(per["id"]), "waiting": waiting})
    # the kinds shown: every active one, and an inactive one only if it holds hours somewhere in this history
    shown = [c for c in cats if c["is_active"] or c["id"] in used]
    totals = {"by_cat": {c["id"]: sum((r["by_cat"].get(c["id"], Decimal(0)) for r in rows), Decimal(0)) for c in shown},
              "total": sum((r["total"] for r in rows), Decimal(0)),
              "paid": sum((r["paid"] for r in rows if r["paid"] is not None), Decimal(0)) if paid_by_period else None}
    out_rows = []
    for r in rows:
        out_rows.append({"period": r["period"], "waiting": r["waiting"], "total": _f(r["total"]), "paid": _f(r["paid"]),
                         "cells": {c["id"]: _f(r["by_cat"].get(c["id"])) for c in shown}})
    return {"rows": out_rows, "cats": shown,
            "totals": {"by_cat": {k: _f(v) for k, v in totals["by_cat"].items()}, "total": _f(totals["total"]),
                       "paid": _f(totals["paid"])}}


def _back_error(msg: str) -> RedirectResponse:
    return RedirectResponse("/admin/timekeeping/employees?error=" + quote_plus(msg), status_code=303)


@router.get("/admin/timekeeping/employees/{staff_id}/hours-history")
def employee_history(staff_id: int, request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    person = db.query_one(
        "SELECT s.id, s.first_name, s.last_name, s.employee_number, s.is_active, s.hours_basis, s.standing_hours, "
        "s.captures_hours, s.parish_id, p.code AS parish_code, p.name AS parish_name, COALESCE(p.short_name, p.name) AS dept_name "
        "FROM portal.staff_roster s JOIN portal.parishes p ON p.id = s.parish_id WHERE s.id = %s AND p.org_id = %s",
        (staff_id, org["id"]))
    if not person:
        return _back_error("That employee was not found.")
    hist = build_history(org["id"], staff_id=staff_id)
    return _render(request, "timekeeping_hours_history.html", user, {
        "current_org": org, "person": person, "parish": None, "hist": hist,
        "title": f"{person['last_name']}, {person['first_name']}", "back_url": "/admin/timekeeping/employees",
        "back_label": "Back to Employees", "parish_id": person["parish_id"]})


@router.get("/admin/timekeeping/parish/{parish_id}/hours-history")
def parish_history(parish_id: int, request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    parish = db.query_one(
        "SELECT id, code, name, COALESCE(short_name, name) AS dept_name FROM portal.parishes WHERE id = %s AND org_id = %s",
        (parish_id, org["id"]))
    if not parish:
        return RedirectResponse("/admin/timekeeping/status?error=" + quote_plus("That department was not found."), status_code=303)
    hist = build_history(org["id"], parish_id=parish_id)
    return _render(request, "timekeeping_hours_history.html", user, {
        "current_org": org, "person": None, "parish": parish, "hist": hist,
        "title": f"{parish['code'] + ' ' if parish['code'] else ''}{parish['dept_name']}",
        "back_url": "/admin/timekeeping/status", "back_label": "Back to Time Review", "parish_id": parish["id"]})
