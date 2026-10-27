"""
roster_auto_case.py -- every person added to the roster gets an onboarding case (26-162 / 26-158).

Jay, 2026-10-27: "every roster add makes a case". The drift this closes: 14 active DME roster people had no HR case because only
the parish "Onboard a new staff member" path made one; the payroll-register reconciler, Roster Review and the Employees "Add
employee" screen added people to portal.staff_roster and nothing else.

The paths that add a roster person, and what each now does:

  Employees > Add employee ................ timekeeping_employees.employees_add        -> ensure_case(origin="employees_add")
  Roster Review approving a new-hire ....... timekeeping_roster_changes.approve_change_detailed -> origin="roster_review"
  legacy parish create (no caller today) ... timekeeping_roster.create_staff            -> origin="parish_staff"
  payroll-register reconciler (a script) ... migrations/reconcile_dme_employees.py      -> origin="payroll_register"
  completing an HR case ................... logic/hr_lifecycle/roster_link.on_complete  -> the case already exists

test_roster_auto_case.py fails if a new file INSERTs into portal.staff_roster without being listed there and calling this module.

THE ADD ALWAYS WINS. Every caller runs this AFTER the roster row is saved, and it never raises: whatever stops the case (the
organization does not use the HR engine, no active checklist, the person already has a case, a name the engine refuses, any
unexpected error) comes back as a small dict and one log line with ids only, and the roster add stands exactly as it did before.

WHO GETS A CASE. Only where the organization has the `hr_lifecycle` switch on AND the roster row is an employing unit: a unit that
is not a church (EDOM's Diocesan Office, Claggett's Claggett Center), or any department where the organization files staff by
department (`hr_staff_by_department`, DME). A church's own staff (EDOM parishes) are the parish's employees, not the diocese's; they
get their benefits case through the parish "Onboard" path. This is the same rule logic/hr_lifecycle/cases._check_unit applies.

Someone who already has any case under the same roster link, or an onboarding case under the same name at the same department,
gets no second one (re-adding, a backfilled case, the parish path). A person found on the payroll register gets a case with no
start date, because the register does not say when they were hired.

Nothing from logic/ is imported at module level (an install without the onboarding engine still loads this): _hr() imports it on
demand. No name, number or address is ever printed or logged here.
"""
from __future__ import annotations

import json

import db
import org_features

FEATURE = "hr_lifecycle"
BY_DEPARTMENT = "hr_staff_by_department"

# where the person came from, in the words the case note and history use
ORIGINS = {
    "employees_add": "added on the Employees screen",
    "roster_review": "approved as a new hire in Roster Review",
    "parish_staff": "added on a parish staff roster",
    "payroll_register": "found on the payroll register",
}


def _hr():
    """(cases, templates, HrError) or None when the onboarding engine cannot be imported."""
    try:
        from logic.hr_lifecycle import cases, templates
        from logic.hr_lifecycle.errors import HrError
    except Exception:  # ImportError, or anything the package raises while it loads
        return None
    return cases, templates, HrError


def applies(org_id, parish_id) -> str | None:
    """None when a person on this roster row should get a case; else a short reason code:
    not_enabled / no_unit / not_a_unit / no_checklist."""
    if not org_id or not org_features.is_enabled(org_id, FEATURE):
        return "not_enabled"
    if not parish_id:
        return "no_unit"
    row = db.query_one("SELECT coalesce(is_congregation, true) AS cong FROM portal.parishes WHERE id = %s AND org_id = %s",
                       (parish_id, org_id))
    if not row:
        return "no_unit"
    if row["cong"] and not org_features.is_enabled(org_id, BY_DEPARTMENT):
        return "not_a_unit"
    hr = _hr()
    if hr is None:
        return "not_enabled"
    cases, templates, _err = hr
    try:
        if templates.active_template("onboard", cases.org_code(org_id)) is None:
            return "no_checklist"
    except Exception:
        return "not_enabled"
    return None


def case_for_staff(staff_id) -> dict | None:
    """{id, case_number} of the case linked to this roster row, or None. Never raises (a missing hr schema reads as none)."""
    if not staff_id:
        return None
    try:
        row = db.query_one("SELECT id, case_number FROM hr.cases WHERE staff_id = %s ORDER BY id LIMIT 1", (staff_id,))
    except Exception:
        return None
    return dict(row) if row else None


def _existing(st: dict) -> dict | None:
    """A case that already covers this person: linked to the roster row, or an onboarding case (not cancelled) under the same name at
    the same department."""
    return db.query_one(
        "SELECT id, case_number FROM hr.cases WHERE org_id = %s AND ("
        " staff_id = %s OR (case_kind = 'onboard' AND status <> 'cancelled' AND coalesce(parish_id, 0) = %s "
        "  AND lower(first_name) = lower(%s) AND lower(last_name) = lower(%s))) ORDER BY id LIMIT 1",
        (st["org_id"], st["id"], st["parish_id"], st["first_name"], st["last_name"]))


def _result(status: str, reason: str | None = None, **extra) -> dict:
    return {"status": status, "reason": reason, **extra}


def ensure_case(staff_id, *, origin: str, actor_id: int | None = None, hire_date_known: bool = True) -> dict:
    """Open an onboarding case for this roster person if they have none. NEVER raises.

    Returns {"status": "opened", "case_id", "case_number"}, {"status": "skipped", "reason"} or {"status": "failed", "reason"}.
    hire_date_known=False (the payroll register) leaves the case's start date empty instead of using the date the person was added."""
    try:
        st = db.query_one("SELECT s.*, p.org_id FROM portal.staff_roster s JOIN portal.parishes p ON p.id = s.parish_id "
                          "WHERE s.id = %s", (staff_id,))
        if not st:
            return _result("failed", "missing")
        why = applies(st["org_id"], st["parish_id"])
        if why:
            return _result("skipped", why)
        have = _existing(st)
        if have:
            return _result("skipped", "has_case", case_id=have["id"], case_number=have["case_number"])
        cases, _templates, HrError = _hr()
        how = ORIGINS.get(origin, "added to the roster")
        raw = {
            "first_name": st["first_name"], "last_name": st["last_name"], "position": st.get("position"),
            "parish_id": st["parish_id"], "payroll_employee_ref": st.get("employee_number"), "is_clergy": False,
            "notes": (f"Opened automatically: this person was {how}. Work through the steps, or close the case as completed if "
                      "onboarding already happened."),
        }
        if hire_date_known and st.get("effective_date"):
            raw["start_date"] = st["effective_date"]
        try:
            case_id = cases.open_case(st["org_id"], "onboard", raw, actor_id, source="hr")
        except HrError as e:
            return _result("skipped" if e.code == "refused" else "failed", "duplicate" if e.code == "refused" else "details")
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE hr.cases SET staff_id = %s, last_activity_at = now(), updated_at = now() "
                            "WHERE id = %s AND staff_id IS NULL", (st["id"], case_id))
                # hr.cases.source allows only 'hr' or 'parish', so where the case came from is kept as a history line (ids only)
                cur.execute("INSERT INTO hr.case_events (case_id, event, detail, actor_user_id) VALUES (%s, 'from_roster_add', %s, %s)",
                            (case_id, json.dumps({"origin": origin, "staff_id": st["id"]}), actor_id))
            conn.commit()
        row = db.query_one("SELECT case_number FROM hr.cases WHERE id = %s", (case_id,))
        return _result("opened", case_id=case_id, case_number=row["case_number"] if row else None)
    except Exception as exc:  # the add already stands: no detail of the person is ever printed
        print(f"[roster_auto_case] could not open a case for roster row {staff_id} ({type(exc).__name__})")
        return _result("failed", "error")
