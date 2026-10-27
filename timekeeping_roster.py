"""
timekeeping_roster.py — Cornerstone Served Parishes Phase K, Stage 1: staff
roster CRUD (Parish Portal Plan.md PP-803: "Each participating parish
maintains a roster of staff whose time is reported -- name, position, and
any attributes the payroll process needs (the diocese can maintain a
roster on a parish's behalf). No compensation data is stored in the
portal.").

New, single-purpose file per this project's standing modular-file
convention -- delegates all context resolution/authorization to
timekeeping.py (parish_context/timekeeping_context/can_manage_timekeeping)
rather than duplicating it, so this file is ONLY the roster's own CRUD
routes + queries.

HARD, NON-NEGOTIABLE CONSTRAINT, restated from the migration itself:
portal.staff_roster must NEVER gain a pay-rate/salary/wage/compensation
column. This module's own create/update field allowlists are deliberately
narrow (first_name/last_name/position/employee_number/is_active only) --
widening them to accept anything compensation-shaped is exactly the
mistake this comment exists to prevent.

Reachable both from a parish's own login/Parish-Mode-preview AND from
Cornerstone Mode (PP-803's "diocese can maintain a roster on a parish's
behalf") -- timekeeping_context() resolves the right parish either way,
with no separate diocese-side screen needed.

ROSTER REVIEW & APPROVAL (Timekeeping HR Roster Review Plan.md, approved
2026-08-16): the three mutating routes below (create/update/toggle-active)
no longer write portal.staff_roster directly -- each now PROPOSES a change
via timekeeping_roster_changes.py, which stays pending until a diocese-side
hr_admin approves it (see that module's own docstring for the full status
model). create_staff()/update_staff() below are UNCHANGED and still the
canonical "apply a real write to portal.staff_roster" functions (still used
internally by timekeeping_roster_changes.py's own apply logic, duplicated
there rather than imported back here to avoid a circular import -- see that
module's docstring) -- they're just no longer called directly by any route
in THIS file."""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
import roster_departures
import timekeeping
import timekeeping_roster_changes
from logic.hr_lifecycle import privacy
from logic.hr_lifecycle.errors import HrError

router = APIRouter()

_render = None


def register(app, *, render) -> None:
    global _render
    _render = render
    app.include_router(router)


# ── Data access ──────────────────────────────────────────────────────────────

def list_staff(parish_id: int, include_inactive: bool = False,
               only_hours_capturing: bool = False) -> list[dict]:
    """The parish's staff roster.

    only_hours_capturing=True restricts to people whose hours are actually
    collected (portal.staff_roster.captures_hours, migration 047). The TIME
    ENTRY GRID passes this; the ROSTER SCREEN deliberately does not, because a
    salaried person is still a real roster member you need to see, edit and
    deactivate -- they just don't get a row to type hours into. Without this
    split the grid would list every salaried employee with nothing to enter
    (122 of DME's 205 on the initial load), which is the noise Jay asked to
    avoid: "Some employees are salaried so we won't be capturing hours for
    those folks."
    """
    # 26-158: a person whose last day has passed is inactive before this parish's roster is read (the Staff screen, the time-entry grid)
    roster_departures.apply_due_departures(parish_id=parish_id)
    where = ["parish_id = %s"]
    params: list = [parish_id]
    if not include_inactive:
        where.append("is_active")
    if only_hours_capturing:
        where.append("captures_hours")
    return db.query(
        "SELECT * FROM portal.staff_roster WHERE " + " AND ".join(where)
        + " ORDER BY last_name, first_name",
        tuple(params),
    )


def get_staff(staff_id: int, parish_id: int) -> dict | None:
    """Scoped lookup -- same choke-point discipline as registry.get_parish():
    returns None if staff_id doesn't exist OR belongs to a different
    parish, so a crafted id from another parish's roster can never be
    edited through this parish's own routes."""
    return db.query_one(
        "SELECT * FROM portal.staff_roster WHERE id = %s AND parish_id = %s",
        (staff_id, parish_id),
    )


def create_staff(parish_id: int, first_name: str, last_name: str, position: str | None,
                  employee_number: str | None, created_by_user_id: int) -> dict:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.staff_roster "
                "(parish_id, first_name, last_name, position, employee_number, created_by_user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                (parish_id, first_name, last_name, position or None, employee_number or None, created_by_user_id),
            )
            return cur.fetchone()


def update_staff(staff_id: int, parish_id: int, **fields) -> dict | None:
    """fields may include: first_name, last_name, position, employee_number,
    is_active -- deliberately NOT extensible beyond this allowlist (see the
    module docstring's hard constraint)."""
    allowed = {"first_name", "last_name", "position", "employee_number", "is_active"}
    sets = [f"{k} = %s" for k in fields if k in allowed]
    if not sets:
        return get_staff(staff_id, parish_id)
    vals = [fields[k] for k in fields if k in allowed]
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE portal.staff_roster SET {', '.join(sets)}, updated_at = NOW() "
                f"WHERE id = %s AND parish_id = %s RETURNING *",
                tuple(vals) + (staff_id, parish_id),
            )
            return cur.fetchone()


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/timekeeping/roster", response_class=HTMLResponse)
def roster_page(request: Request):
    user, parish, diocese_org, err = timekeeping.timekeeping_context(request)
    if err:
        return err
    staff = list_staff(parish["id"], include_inactive=True)
    changes = timekeeping_roster_changes.list_all_for_parish(parish["id"])
    pending = [c for c in changes if c["status"] == "pending"]
    # onboarding is offered only where the diocese has switched parish benefits requests on (parish_onboarding.available)
    import parish_onboarding
    added = request.query_params.get("added")
    return _render(request, "timekeeping_roster.html", user, {
        "parish": parish,
        "staff": staff,
        "can_manage": timekeeping.can_manage_timekeeping(user, parish),
        # Roster Review & Approval (2026-08-16): the parish's own view of
        # everything they've proposed -- queued, submitted-awaiting-review,
        # approved, or rejected -- so they can see status without asking.
        "pending_changes": changes,
        # 2026-10-25 (Jay): a person with a change waiting shows it on their row, with a Withdraw button; a new hire not yet on
        # the roster is a row of its own.
        "pending_by_staff": {c["staff_id"]: c for c in pending if c["staff_id"]},
        # 26-158: people whose termination was approved but whose last day has not passed ("Leaving MM/DD/YY"; still active)
        "leaving_by_staff": {s["id"]: s["inactive_as_of"] for s in staff if roster_departures.is_leaving(s)},
        "stay_reasons": timekeeping_roster_changes.STAY_REASONS,
        "pending_adds": [c for c in pending if c["change_type"] == "add"],
        "pending_count": len(pending),
        "deactivate_reasons": timekeeping_roster_changes.DEACTIVATE_REASONS,
        "reactivate_reasons": timekeeping_roster_changes.REACTIVATE_REASONS,
        "withdrawn_note": timekeeping_roster_changes.WITHDRAWN_NOTE,
        "reasons_kept": timekeeping_roster_changes.reasons_kept(),
        "change_reason": timekeeping_roster_changes.reason_label,
        "can_onboard": parish_onboarding.available(parish),
        "added_id": int(added) if added and added.isdigit() else None,
        "today": dt.date.today().isoformat(),
    })


@router.post("/timekeeping/roster/create")
async def roster_create(request: Request):
    user, parish, diocese_org, err = timekeeping.timekeeping_context(request)
    if err:
        return err
    if not timekeeping.can_manage_timekeeping(user, parish):
        return JSONResponse(
            {"error": "You don't have permission to manage this parish's staff roster."}, status_code=403
        )
    form = await request.form()
    first_name = (form.get("first_name") or "").strip()
    last_name = (form.get("last_name") or "").strip()
    if not first_name or not last_name:
        return RedirectResponse(
            "/timekeeping/roster?error=Both+first+and+last+name+are+required.", status_code=303
        )
    hire_date = None
    raw_hire = (form.get("hire_date") or "").strip()
    if raw_hire:
        try:
            hire_date = dt.date.fromisoformat(raw_hire)
        except ValueError:
            return RedirectResponse("/timekeeping/roster?error=Hire+date+must+be+a+date.", status_code=303)
    # Roster Review & Approval (2026-08-16): PROPOSE the add, don't write
    # portal.staff_roster directly -- an hr_admin must approve it first (see
    # timekeeping_roster_changes.py's own docstring for the full flow).
    change = timekeeping_roster_changes.propose_add(
        parish["id"], first_name, last_name,
        (form.get("position") or "").strip(),
        (form.get("employee_number") or "").strip(),
        user["id"], hire_date=hire_date,
    )
    # the screen offers "Continue to onboarding" for this new person (the id says which one)
    return RedirectResponse(f"/timekeeping/roster?change_proposed=1&added={change['id']}", status_code=303)


@router.post("/timekeeping/roster/{staff_id}/update")
async def roster_update(staff_id: int, request: Request):
    user, parish, diocese_org, err = timekeeping.timekeeping_context(request)
    if err:
        return err
    if not timekeeping.can_manage_timekeeping(user, parish):
        return JSONResponse(
            {"error": "You don't have permission to manage this parish's staff roster."}, status_code=403
        )
    form = await request.form()
    first_name = (form.get("first_name") or "").strip()
    last_name = (form.get("last_name") or "").strip()
    if not first_name or not last_name:
        return RedirectResponse(
            "/timekeeping/roster?error=Both+first+and+last+name+are+required.", status_code=303
        )
    # Confirm the target staff member is real and belongs to THIS parish
    # before proposing anything against its id (same scoped-lookup
    # discipline get_staff() already documents) -- still a plain existence
    # check, not a write.
    existing = get_staff(staff_id, parish["id"])
    if not existing:
        return RedirectResponse("/timekeeping/roster?error=Staff+member+not+found.", status_code=303)
    # Roster Review & Approval (2026-08-16): PROPOSE the edit, don't write
    # portal.staff_roster directly.
    timekeeping_roster_changes.propose_edit(
        parish["id"], staff_id, first_name, last_name,
        (form.get("position") or "").strip() or None,
        (form.get("employee_number") or "").strip() or None,
        user["id"],
    )
    return RedirectResponse("/timekeeping/roster?change_proposed=1", status_code=303)


def _back(**msg) -> RedirectResponse:
    q = "&".join(f"{k}={quote(str(v))}" for k, v in msg.items() if v)
    return RedirectResponse("/timekeeping/roster" + (("?" + q) if q else ""), status_code=303)


@router.post("/timekeeping/roster/{staff_id}/toggle-active")
async def roster_toggle_active(staff_id: int, request: Request):
    """Status change (Jay, 2026-10-25): the pop-up asks what the change is (resignation, termination ...), from what date and,
    for 'Other', a note, then asks the person to confirm. It is still only a PROPOSAL: the diocese's HR Admin approves it."""
    user, parish, diocese_org, err = timekeeping.timekeeping_context(request)
    if err:
        return err
    if not timekeeping.can_manage_timekeeping(user, parish):
        return JSONResponse(
            {"error": "You don't have permission to manage this parish's staff roster."}, status_code=403
        )
    existing = get_staff(staff_id, parish["id"])
    if not existing:
        return _back(error="Staff member not found.")
    name = f"{existing['first_name']} {existing['last_name']}"
    if any(c["status"] == "pending" and c["staff_id"] == staff_id
           for c in timekeeping_roster_changes.list_all_for_parish(parish["id"])):
        return _back(error=f"A change for {name} is already waiting. Withdraw it first, then make the new one.")
    form = await request.form()
    # 26-158: a LEAVING person (termination approved, last day ahead) is still active. "Keep on staff" (stay=1) asks to cancel the
    # leaving date; anything else sent for them is a new termination request, which replaces the date when approved.
    stay = form.get("stay") == "1"
    if stay and not roster_departures.is_leaving(existing):
        return _back(error=f"{name} is not set to leave, so there is nothing to cancel.")
    new_active = (not existing["is_active"]) or stay
    table = (timekeeping_roster_changes.STAY_REASONS if stay
             else timekeeping_roster_changes.REACTIVATE_REASONS if new_active
             else timekeeping_roster_changes.DEACTIVATE_REASONS)
    reason = (form.get("reason") or "").strip()
    if reason not in table:
        return _back(error="Choose what the change is.")
    note = (form.get("note") or "").strip()
    if reason == "other" and not note:
        return _back(error="Explain the change in the note.")
    if len(note) > 300:
        return _back(error="Keep the note to 300 characters or fewer.")
    try:
        note = privacy.check_text(note, "Note") if note else None   # no Social Security numbers, birth dates or bank details
    except HrError as exc:
        return _back(error=exc.message)
    raw_date = (form.get("effective_date") or "").strip()
    try:
        effective = dt.date.fromisoformat(raw_date) if raw_date else dt.date.today()
    except ValueError:
        return _back(error="The date must be a real date.")
    # Roster Review & Approval (2026-08-16): PROPOSE the deactivate/
    # reactivate, don't flip is_active directly.
    timekeeping_roster_changes.propose_toggle(
        parish["id"], staff_id, new_active, user["id"], as_of_date=effective, reason=reason, note=note)
    return _back(change_proposed=1)


@router.post("/timekeeping/roster/changes/{change_id}/withdraw")
def roster_withdraw_change(change_id: int, request: Request):
    """The parish takes back one of its own changes that nobody has answered yet (Jay, 2026-10-25: there was no way to unwind
    a proposed deactivation). Only this parish's own pending change; the row is kept as history."""
    user, parish, diocese_org, err = timekeeping.timekeeping_context(request)
    if err:
        return err
    if not timekeeping.can_manage_timekeeping(user, parish):
        return JSONResponse(
            {"error": "You don't have permission to manage this parish's staff roster."}, status_code=403
        )
    row = timekeeping_roster_changes.withdraw_change(change_id, parish["id"], user["id"])
    if not row:
        return _back(error="That change was already answered by the diocese, or is not on this parish's list.")
    return _back(withdrawn=1)
