"""
roster_link.py -- completing a case updates Beacon's employee roster (portal.staff_roster), Plan section 9.

  completed onboarding (types 1-2) with an employing unit and no roster row yet -> add the person to the roster under
      that unit ("Diocesan Office" for EDOM, "Claggett Center" for Claggett), effective the hire date;
  completed offboarding -> inactivate the person's roster row as of the last day, or, when that day is still ahead or its pay
      period is not closed yet, keep them on as "leaving" until then (roster_departures.keep_until);
  parish benefits cases (type 3) -> never touch the roster: those people are the parish's employees, not the diocese's.

Every change also writes an APPROVED portal.staff_roster_changes row (source_ref 'hr_case:<id>'), the same shape the
Employees screen writes for a direct diocese-side edit (timekeeping_employees._log_change), so the roster keeps one dated
history whichever door a change came through. Duplicated here on purpose rather than imported, as the codebase does for
per-module gates; the CHECK on that table needs staff_id NULL for an 'add'.

The caller passes its cursor: this runs inside close_case()'s transaction.
"""
from __future__ import annotations


def _log(cur, *, parish_id: int, staff_id: int | None, change_type: str, as_of, actor_id: int, case: dict) -> None:
    cur.execute(
        "INSERT INTO portal.staff_roster_changes "
        "(parish_id, staff_id, change_type, proposed_first_name, proposed_last_name, proposed_position, "
        " proposed_employee_number, proposed_captures_hours, as_of_date, status, created_by_user_id, reviewed_by_user_id, "
        " reviewed_at, review_note, source_ref) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'approved', %s, %s, now(), %s, %s)",
        (parish_id, None if change_type == "add" else staff_id, change_type, case["first_name"], case["last_name"],
         case.get("position"), case.get("payroll_employee_ref"), False, as_of, actor_id, actor_id,
         f"From HR case {case['case_number']} (Onboarding and Offboarding).", f"hr_case:{case['id']}"))


def on_complete(cur, case: dict, actor_id: int) -> str:
    """Apply the roster change for a case being completed. Returns a short note for the case history."""
    kind = case["case_kind"]
    if kind == "onboard":
        if case.get("staff_id"):
            return "already on the roster"
        if not case.get("parish_id"):
            return "not added to the roster: no employing unit on the case"
        emp_no = case.get("payroll_employee_ref")
        # someone may already have added the person on the Employees screen (the checklist asks for it): link, never duplicate
        existing = None
        if emp_no:
            cur.execute("SELECT s.id FROM portal.staff_roster s JOIN portal.parishes p ON p.id = s.parish_id "
                        "WHERE p.org_id = %s AND s.employee_number = %s", (case["org_id"], emp_no))
            existing = cur.fetchone()
        if not existing:
            cur.execute("SELECT id FROM portal.staff_roster WHERE parish_id = %s AND lower(last_name) = lower(%s) "
                        "AND lower(first_name) = lower(%s) AND is_active ORDER BY id LIMIT 1",
                        (case["parish_id"], case["last_name"], case["first_name"]))
            existing = cur.fetchone()
        if existing:
            cur.execute("UPDATE hr.cases SET staff_id = %s WHERE id = %s", (existing["id"], case["id"]))
            return "already on the roster, linked"
        cur.execute(
            "INSERT INTO portal.staff_roster (parish_id, first_name, last_name, position, employee_number, captures_hours, "
            " effective_date, is_active, created_by_user_id) VALUES (%s, %s, %s, %s, %s, FALSE, %s, TRUE, %s) RETURNING id",
            (case["parish_id"], case["first_name"], case["last_name"], case.get("position"), emp_no,
             case.get("start_date"), actor_id))
        staff_id = cur.fetchone()["id"]
        _log(cur, parish_id=case["parish_id"], staff_id=staff_id, change_type="add", as_of=case.get("start_date"),
             actor_id=actor_id, case={**case, "payroll_employee_ref": emp_no})
        cur.execute("UPDATE hr.cases SET staff_id = %s WHERE id = %s", (staff_id, case["id"]))
        return "added to the roster"
    if kind == "offboard":
        staff_id = case.get("staff_id")
        if not staff_id and case.get("linked_case_id"):
            cur.execute("SELECT staff_id FROM hr.cases WHERE id = %s", (case["linked_case_id"],))
            row = cur.fetchone()
            staff_id = row["staff_id"] if row else None
        if not staff_id:
            return "roster not changed: no roster row linked to this person"
        cur.execute("SELECT s.id, s.parish_id, s.is_active FROM portal.staff_roster s JOIN portal.parishes p "
                    "ON p.id = s.parish_id WHERE s.id = %s AND p.org_id = %s", (staff_id, case["org_id"]))
        row = cur.fetchone()
        if not row:
            return "roster not changed: the roster row is not in this entity"
        if not row["is_active"]:
            return "already inactive on the roster"
        # Jay, 2026-10-27: a separation date still ahead (or in a pay period not closed yet) keeps the person on until then, exactly
        # like an approved Roster Review termination; roster_departures switches them off later, and the approved history row below
        # is what lets that sweep act. (Imported here, not at the top: that module needs only db and org_time.)
        import roster_departures
        stays_until = roster_departures.keep_until(case["org_id"], case.get("separation_date"))
        if stays_until is not None:
            cur.execute("UPDATE portal.staff_roster SET inactive_as_of = %s, updated_at = now() WHERE id = %s", (stays_until, staff_id))
        else:
            cur.execute("UPDATE portal.staff_roster SET is_active = FALSE, inactive_as_of = %s, updated_at = now() WHERE id = %s",
                        (case.get("separation_date"), staff_id))
        _log(cur, parish_id=row["parish_id"], staff_id=staff_id, change_type="deactivate",
             as_of=case.get("separation_date"), actor_id=actor_id, case=case)
        if stays_until is not None:
            return (f"stays on the roster until {stays_until.strftime('%m/%d/%y')} and until its pay period is closed, "
                    "then inactivated automatically")
        return "inactivated on the roster"
    return "roster not used for parish benefits cases"
