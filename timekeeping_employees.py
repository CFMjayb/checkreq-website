"""
timekeeping_employees.py — diocese-side HR employee management (added
2026-08-17). Jay: "where do I load the employees for HR? ... can you
pre-populate and then create a screen card that allows adds/mods/and
inactivations with a as-of date."

This is the diocese-wide roster screen confirmed wanted back on 2026-08-16
("a real diocese-wide 'manage any parish's roster with filters' screen IS
wanted, not just Parish/Cornerstone Mode access"). It complements, and does
not replace, the two roster surfaces that already exist:

  timekeeping_roster.py         a PARISH manages its own roster; mutating
                                routes PROPOSE a change for review.
  timekeeping_review.py         an hr_admin approves/rejects those proposals.
  THIS MODULE                   an hr_admin manages ANY parish's roster in
                                the diocese directly, across all parishes,
                                with filters. No proposal step -- the
                                reviewer IS the actor, so asking them to
                                approve their own edit would be theatre.

EVERY MUTATION IS STILL LOGGED. Each add/edit/deactivate/reactivate writes a
portal.staff_roster_changes row with status='approved', the actor as both
created_by and reviewed_by, and the as-of date. That keeps ONE history for
the roster regardless of which door a change came through -- a parish
proposal that was approved and a diocese direct edit land in the same table
in the same shape, so replaying that table gives the full dated story. This
is the payoff of migration 047 putting as_of_date there rather than inventing
a separate audit table.

AS-OF DATE semantics, per the design approved 2026-08-17:
  add / edit  -> staff_roster.effective_date = as_of
  deactivate  -> staff_roster.inactive_as_of = as_of, is_active = FALSE
  reactivate  -> staff_roster.inactive_as_of = NULL, is_active = TRUE,
                 effective_date = as_of
is_active is kept alongside inactive_as_of rather than replaced by it, so
every existing `WHERE is_active` query keeps working untouched.

GATE: hr_admin or beacon_admin -- the OPERATIONAL half of the HR group's own
split (Timekeeping Review / Timekeeping Status), not the setup_admin
CONFIGURATION half (Payroll Periods / Time Categories / HR Activation). Day-to-
day employee maintenance is operations. A Cornerstone-Mode-selected entity is
rejected outright, same as every other diocese-wide Timekeeping screen.

NEVER A COMPENSATION FIELD. portal.staff_roster's hard rule applies to this
screen as much as to the loader: the editable set below is deliberately
name / position / employee number / captures_hours / active only.
captures_hours is a boolean about WHETHER hours are collected -- not a pay
basis, not a rate.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

import cornerstone_mode
import db
import rbac

router = APIRouter()

_current_user = None
_current_org = None
_render = None


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


def _require_hr_admin(request: Request):
    """(user, org, None) when allowed, (None, None, response) when not.
    Mirrors timekeeping_review.py / timekeeping_status.py's own gate -- the
    same small documented duplication this codebase already accepts for
    per-module permission checks, rather than a cross-import."""
    user = _current_user(request)
    if not user:
        return None, None, RedirectResponse("/login")
    org = _current_org(request)
    org_id = org["id"] if org else None
    if org_id is None or cornerstone_mode.is_cornerstone_org(org_id):
        return None, None, RedirectResponse("/portal")
    if not rbac.user_has_any_role(user["id"], ["hr_admin", "beacon_admin"],
                                  org_id=org_id):
        return None, None, JSONResponse({"error": "HR Admin access required"},
                                        status_code=403)
    return user, org, None


def _as_of(form, field: str = "as_of_date") -> dt.date | None:
    raw = (form.get(field) or "").strip()
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return None


def _staff_in_org(staff_id: int, org_id: int) -> dict | None:
    """The scoping choke point: never load a staff row by id alone. Joins
    through the parish so a crafted staff_id from another diocese resolves to
    None rather than being editable."""
    return db.query_one(
        "SELECT s.*, p.org_id, p.code AS parish_code, "
        "       coalesce(p.short_name, p.name) AS parish_label "
        "FROM portal.staff_roster s JOIN portal.parishes p ON p.id = s.parish_id "
        "WHERE s.id = %s AND p.org_id = %s",
        (staff_id, org_id),
    )


def _log_change(cur, *, parish_id: int, staff_id: int | None, change_type: str,
                as_of: dt.date, actor_id: int, first=None, last=None,
                position=None, employee_number=None, captures_hours=None,
                note: str = "") -> None:
    """Write the history row for one roster mutation.

    staff_id is FORCED to NULL for change_type='add'. That is not a quirk to
    work around -- portal.staff_roster_changes has a CHECK constraint
    (migration 041) requiring exactly that:

        (change_type = 'add' AND staff_id IS NULL)
        OR (change_type <> 'add' AND staff_id IS NOT NULL)

    because in the PARISH PROPOSAL flow an 'add' describes someone who does not
    exist yet, so there is nothing to point at; timekeeping_roster_changes.py's
    own approved-add path leaves it NULL for the same reason. This module adds
    the person first and so *could* supply an id, but doing that would make
    diocese-added and parish-added history rows structurally different, which
    is exactly the drift the single shared history table exists to avoid.

    Nothing is lost: the proposed_* columns below carry the added person's
    name, position and employee number, and employee_number is unique per
    diocese, so an 'add' row is still traceable to the employee it created --
    just by value rather than by foreign key. Enforced here rather than left to
    each caller, because a caller that forgets raises a CheckViolation at
    runtime (which is how this was found).
    """
    if change_type == "add":
        staff_id = None
    cur.execute(
        "INSERT INTO portal.staff_roster_changes "
        "(parish_id, staff_id, change_type, proposed_first_name, "
        " proposed_last_name, proposed_position, proposed_employee_number, "
        " proposed_captures_hours, as_of_date, status, created_by_user_id, "
        " reviewed_by_user_id, reviewed_at, review_note) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'approved', %s, %s, "
        "        now(), %s)",
        (parish_id, staff_id, change_type, first, last, position,
         employee_number, captures_hours, as_of, actor_id, actor_id,
         note or "Direct diocese-side HR edit (no parish proposal step)."),
    )


def list_employees(org_id: int, *, parish_id: int | None = None,
                   q: str = "", show: str = "active",
                   hours: str = "all") -> list[dict]:
    where = ["p.org_id = %s"]
    params: list = [org_id]
    if parish_id:
        where.append("s.parish_id = %s")
        params.append(parish_id)
    if show == "active":
        where.append("s.is_active")
    elif show == "inactive":
        where.append("NOT s.is_active")
    if hours == "yes":
        where.append("s.captures_hours")
    elif hours == "no":
        where.append("NOT s.captures_hours")
    if q:
        where.append("(s.last_name ILIKE %s OR s.first_name ILIKE %s "
                     "OR s.employee_number ILIKE %s)")
        params += [f"%{q}%"] * 3
    return db.query(
        "SELECT s.*, p.code AS parish_code, "
        "       coalesce(p.short_name, p.name) AS parish_label "
        "FROM portal.staff_roster s JOIN portal.parishes p ON p.id = s.parish_id "
        "WHERE " + " AND ".join(where)
        + " ORDER BY p.code NULLS LAST, s.last_name, s.first_name",
        tuple(params),
    )


@router.get("/admin/timekeeping/employees")
def employees_page(request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    qp = request.query_params
    parish_id = qp.get("parish_id")
    parish_id = int(parish_id) if parish_id and parish_id.isdigit() else None
    q = (qp.get("q") or "").strip()
    show = qp.get("show") or "active"
    hours = qp.get("hours") or "all"

    employees = list_employees(org["id"], parish_id=parish_id, q=q,
                               show=show, hours=hours)
    parishes = db.query(
        "SELECT id, code, coalesce(short_name, name) AS label "
        "FROM portal.parishes WHERE org_id = %s AND is_active "
        "ORDER BY code NULLS LAST, label", (org["id"],))
    totals = db.query_one(
        "SELECT count(*) AS total, "
        "       count(*) FILTER (WHERE s.is_active) AS active, "
        "       count(*) FILTER (WHERE s.is_active AND s.captures_hours) AS hourly "
        "FROM portal.staff_roster s JOIN portal.parishes p ON p.id = s.parish_id "
        "WHERE p.org_id = %s", (org["id"],))
    return _render(request, "timekeeping_employees.html", user, {
        "employees": employees, "parishes": parishes, "current_org": org,
        "f_parish_id": parish_id, "f_q": q, "f_show": show, "f_hours": hours,
        "totals": totals, "today": dt.date.today().isoformat(),
    })


def _redirect_back(request: Request, **extra) -> RedirectResponse:
    qp = dict(request.query_params)
    qp.update({k: v for k, v in extra.items() if v is not None})
    tail = "&".join(f"{k}={v}" for k, v in qp.items() if v != "")
    return RedirectResponse(
        "/admin/timekeeping/employees" + (f"?{tail}" if tail else ""),
        status_code=303)


@router.post("/admin/timekeeping/employees/add")
async def employees_add(request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    form = await request.form()
    as_of = _as_of(form)
    first = (form.get("first_name") or "").strip()
    last = (form.get("last_name") or "").strip()
    parish_raw = (form.get("parish_id") or "").strip()
    if not (first and last and parish_raw.isdigit() and as_of):
        return _redirect_back(request,
                              error="First name, last name, parish and a valid "
                                    "as-of date are all required.")
    parish = db.query_one(
        "SELECT id FROM portal.parishes WHERE id = %s AND org_id = %s",
        (int(parish_raw), org["id"]))
    if not parish:                      # crafted id from another diocese
        return _redirect_back(request, error="Unknown parish.")

    position = (form.get("position") or "").strip() or None
    emp_no = (form.get("employee_number") or "").strip() or None
    captures = form.get("captures_hours") is not None

    if emp_no:
        dupe = db.query_one(
            "SELECT s.id FROM portal.staff_roster s "
            "JOIN portal.parishes p ON p.id = s.parish_id "
            "WHERE p.org_id = %s AND s.employee_number = %s",
            (org["id"], emp_no))
        if dupe:
            return _redirect_back(
                request, error=f"Employee number {emp_no} already exists in "
                               "this diocese. The payroll register keys on it, "
                               "so it has to stay unique.")

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.staff_roster "
                "(parish_id, first_name, last_name, position, employee_number, "
                " captures_hours, effective_date, is_active, created_by_user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s) RETURNING id",
                (parish["id"], first, last, position, emp_no, captures, as_of,
                 user["id"]))
            new_id = cur.fetchone()["id"]
            _log_change(cur, parish_id=parish["id"], staff_id=new_id,
                        change_type="add", as_of=as_of, actor_id=user["id"],
                        first=first, last=last, position=position,
                        employee_number=emp_no, captures_hours=captures)
        conn.commit()
    return _redirect_back(request, saved=f"Added {last}, {first} as of {as_of}.")


@router.post("/admin/timekeeping/employees/save")
async def employees_save(request: Request):
    """Batched save for the whole visible table.

    WHY BATCHED, not a form per row: an editable table cannot legally put a
    <form> inside a <tr> (only <td>/<th> may be a tr's children), so per-row
    forms get hoisted or dropped by the browser and the row breaks. More
    importantly it matches the pattern Jay asked for directly on 2026-08-02
    after deleting a GL mapping by accident -- a destructive action marks the
    row and waits for Save Changes rather than firing the instant it is
    clicked. Deactivation here is marked the same way.

    Every row on the page posts `present_<id>`; that hidden field, not the
    editable inputs, is what makes an unchecked "reports hours" box mean FALSE
    instead of "absent, leave alone". `present_` also doubles as the
    authorization set, intersected with what this diocese actually owns.
    """
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    form = await request.form()

    posted = [k[len("present_"):] for k in form.keys() if k.startswith("present_")]
    owned = {str(e["id"]): e for e in list_employees(org["id"], show="all")}

    edited, activated, deactivated, problems = 0, 0, 0, []
    with db.connect() as conn:
        with conn.cursor() as cur:
            for raw_id in posted:
                row = owned.get(raw_id)
                if row is None:          # another diocese's id, or deleted meanwhile
                    continue
                sid = int(raw_id)
                who = f"{row['last_name']}, {row['first_name']}"
                as_of = _as_of(form, f"as_of_{raw_id}")
                action = (form.get(f"action_{raw_id}") or "").strip()
                first = (form.get(f"first_name_{raw_id}") or "").strip()
                last = (form.get(f"last_name_{raw_id}") or "").strip()
                position = (form.get(f"position_{raw_id}") or "").strip() or None
                emp_no = (form.get(f"employee_number_{raw_id}") or "").strip() or None
                captures = form.get(f"captures_hours_{raw_id}") is not None

                field_change = (
                    (first, last, position, emp_no, captures)
                    != (row["first_name"], row["last_name"], row["position"],
                        row["employee_number"], bool(row["captures_hours"])))
                wants_active = action == "reactivate"
                state_change = action in ("deactivate", "reactivate") and \
                    bool(row["is_active"]) != wants_active

                if not field_change and not state_change:
                    continue
                if not as_of:
                    problems.append(f"{who}: skipped -- a change needs an as-of date.")
                    continue
                if field_change and not (first and last):
                    problems.append(f"{who}: skipped -- first and last name are "
                                    "both required.")
                    continue
                if field_change and emp_no and emp_no != row["employee_number"]:
                    if any(o["employee_number"] == emp_no and o["id"] != sid
                           for o in owned.values()):
                        problems.append(f"{who}: skipped -- employee number "
                                        f"{emp_no} is already used in this "
                                        "diocese, and the payroll register keys "
                                        "on it.")
                        continue

                if field_change:
                    cur.execute(
                        "UPDATE portal.staff_roster SET first_name = %s, "
                        "  last_name = %s, position = %s, employee_number = %s, "
                        "  captures_hours = %s, effective_date = %s, "
                        "  updated_at = now() WHERE id = %s AND parish_id = %s",
                        (first, last, position, emp_no, captures, as_of, sid,
                         row["parish_id"]))
                    _log_change(cur, parish_id=row["parish_id"], staff_id=sid,
                                change_type="edit", as_of=as_of,
                                actor_id=user["id"], first=first, last=last,
                                position=position, employee_number=emp_no,
                                captures_hours=captures)
                    edited += 1

                if state_change:
                    if wants_active:
                        cur.execute(
                            "UPDATE portal.staff_roster SET is_active = TRUE, "
                            "  inactive_as_of = NULL, effective_date = %s, "
                            "  updated_at = now() WHERE id = %s AND parish_id = %s",
                            (as_of, sid, row["parish_id"]))
                        activated += 1
                    else:
                        cur.execute(
                            "UPDATE portal.staff_roster SET is_active = FALSE, "
                            "  inactive_as_of = %s, updated_at = now() "
                            "WHERE id = %s AND parish_id = %s",
                            (as_of, sid, row["parish_id"]))
                        deactivated += 1
                    _log_change(
                        cur, parish_id=row["parish_id"], staff_id=sid,
                        change_type="reactivate" if wants_active else "deactivate",
                        as_of=as_of, actor_id=user["id"])
        conn.commit()

    bits = []
    if edited:
        bits.append(f"{edited} updated")
    if deactivated:
        bits.append(f"{deactivated} deactivated")
    if activated:
        bits.append(f"{activated} reactivated")
    saved = ", ".join(bits) + "." if bits else "No changes to save."
    return _redirect_back(request, saved=saved,
                          error=" ".join(problems) if problems else None)
