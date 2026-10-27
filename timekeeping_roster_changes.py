"""
timekeeping_roster_changes.py — Timekeeping & HR, Roster Review & Approval
(Timekeeping HR Roster Review Plan.md, approved with Jay 2026-08-16). This
is the Stage 3/4 staff-roster half: a parish PROPOSES a change
(add/edit/deactivate/reactivate) instead of writing portal.staff_roster
directly; a diocese-side hr_admin reviews and either approves (the change
goes live) or rejects (with a reason, the parish can revise and resubmit).

New, single-purpose file per this project's standing modular-file
convention (timekeeping.py / timekeeping_roster.py / timekeeping_entries.py
are its siblings). Deliberately does NOT import timekeeping_roster.py, which
imports THIS module for its own propose_* calls — _apply_change() below
duplicates timekeeping_roster.create_staff()/update_staff()'s exact field
shape directly rather than importing them back, the same small, documented
duplication this project already accepts elsewhere to avoid a circular
import (see timekeeping.py's own docstring on why parish_documents.py's
parish_context() is duplicated rather than cross-imported).

Migration: migrations/041_staff_roster_changes.sql (portal.staff_roster_changes
+ the new 'hr_admin' checkreq.roles row). Depends on 038_timekeeping.sql
already being applied.

STATUS MODEL — deliberately just 3 values, matching the migration's own
CHECK constraint (pending/approved/rejected). There is no separate
"queued" vs "submitted-awaiting-review" enum value; that distinction is
carried by payroll_period_id instead:
  - status='pending', payroll_period_id IS NULL     -> still queued by the
    parish, not yet part of any Submit action. Visible only on the parish's
    own roster page (list_queued below); nothing is visible to the diocese
    yet.
  - status='pending', payroll_period_id IS NOT NULL -> bundled into a real
    Submit (timekeeping_entries.py's /timekeeping/submit route stamped this
    row's payroll_period_id/submitted_by_user_id/submitted_at) — now sits
    in the diocese's review queue (list_pending_review_for_org below).
  - status='approved' / 'rejected' -> terminal. reviewed_by_user_id/
    reviewed_at/review_note stamped, and the row is NEVER deleted (this
    app's standing never-truly-delete convention). A rejected change is not
    reopened or edited in place — per the plan's own "can revise and
    resubmit" wording, the parish proposes a FRESH row to try again; the
    rejected row stays as permanent history.

HARD CONSTRAINT, inherited from 038_timekeeping.sql: proposed_* fields are
name/position/employee-number ONLY — never compensation data. Same
allowlist discipline as timekeeping_roster.py's own create_staff/update_staff.
"""
from __future__ import annotations

import time

import db
import notifications
import payroll_totals
import roster_departures

_CHANGE_TYPE_LABELS = {
    "add": "Add staff member",
    "edit": "Edit staff member",
    "deactivate": "Deactivate staff member",
    "reactivate": "Reactivate staff member",
}


# Status change (Jay, 2026-10-25): the parish says WHY a person is leaving or coming back, and from what date. The words are fixed
# lists so the diocese reads the same thing every time; "Other" needs a note.
DEACTIVATE_REASONS = {
    "resignation": "Voluntary resignation",
    "termination": "Termination",
    "retirement": "Retirement",
    "end_of_term": "End of a position or contract",
    "other": "Other (explain in the note)",
}
REACTIVATE_REASONS = {
    "rehired": "Rehired",
    "returned": "Returned from leave",
    "other": "Other (explain in the note)",
}
# A person whose termination is approved but whose last day has not come (see roster_departures.py) is "leaving": the parish can
# ask to keep them on. It is a 'reactivate' request (approving one cancels the leaving date), with these reasons.
STAY_REASONS = {
    "staying": "Staying on staff (cancel the leaving date)",
    "other": "Other (explain in the note)",
}
# A parish can take back a change nobody has answered yet. The table's status only allows pending / approved / rejected, so a
# withdrawal is a 'rejected' row carrying this exact note (no schema change); the screens read it back as "Withdrawn".
WITHDRAWN_NOTE = "Withdrawn by the parish."

# Where the reason and note are kept: two nullable columns (migrations/102_roster_change_reason.sql). The code works with or
# without them, so it can be deployed before that migration is applied; until then a status change still records who, what and
# the effective date, only the reason and note are not kept.
_COLS = {"at": 0.0, "have": frozenset()}


def _reason_columns() -> frozenset:
    if time.time() - _COLS["at"] > 60:
        try:
            rows = db.query("SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'portal' AND table_name = 'staff_roster_changes' "
                            "AND column_name IN ('proposed_reason', 'proposed_note')")
            _COLS["have"] = frozenset(r["column_name"] for r in rows)
        except Exception:
            _COLS["have"] = frozenset()
        _COLS["at"] = time.time()
    return _COLS["have"]


def reasons_kept() -> bool:
    """True once the reason columns exist, so the screens know whether to promise that a reason is recorded."""
    return {"proposed_reason", "proposed_note"} <= _reason_columns()


def reason_label(change: dict) -> str | None:
    key = change.get("proposed_reason")
    if change.get("change_type") == "deactivate":
        table = DEACTIVATE_REASONS
    else:
        table = {**REACTIVATE_REASONS, **STAY_REASONS}
    return table.get(key) if key else None


def _staff_name_from_change(change: dict) -> str:
    """Best display name for a change row — the proposed name for an 'add'
    (no existing staff_id to join against yet), the CURRENT roster name
    otherwise (current_first_name/current_last_name, joined in by every
    query below that needs display)."""
    if change.get("change_type") == "add":
        first, last = change.get("proposed_first_name"), change.get("proposed_last_name")
    else:
        first, last = change.get("current_first_name"), change.get("current_last_name")
    return f"{first or ''} {last or ''}".strip() or "(unnamed)"


# ── Parish side: propose ─────────────────────────────────────────────────────

def list_queued(parish_id: int) -> list[dict]:
    """This parish's own not-yet-submitted proposals — still pending,
    payroll_period_id IS NULL. Shown on the roster page so a parish
    admin/HR person can see what they've queued up for the next Submit."""
    return db.query(
        "SELECT src.*, sr.first_name AS current_first_name, sr.last_name AS current_last_name "
        "FROM portal.staff_roster_changes src "
        "LEFT JOIN portal.staff_roster sr ON sr.id = src.staff_id "
        "WHERE src.parish_id = %s AND src.status = 'pending' AND src.payroll_period_id IS NULL "
        "ORDER BY src.created_at DESC",
        (parish_id,),
    )


def list_all_for_parish(parish_id: int, limit: int = 50) -> list[dict]:
    """Every proposal for this parish, any status — queued,
    submitted-awaiting-review, approved, or rejected — most recent first,
    so the parish can see the full status of what they've proposed (plan:
    "Show the parish admin/HR person their own pending changes ... and its
    status")."""
    return db.query(
        "SELECT src.*, sr.first_name AS current_first_name, sr.last_name AS current_last_name "
        "FROM portal.staff_roster_changes src "
        "LEFT JOIN portal.staff_roster sr ON sr.id = src.staff_id "
        "WHERE src.parish_id = %s "
        "ORDER BY src.created_at DESC LIMIT %s",
        (parish_id, limit),
    )


def propose_add(parish_id: int, first_name: str, last_name: str, position: str | None,
                 employee_number: str | None, user_id: int, hire_date=None) -> dict:
    """hire_date (optional) is kept as the change's effective date (as_of_date) and becomes the roster row's effective date."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.staff_roster_changes "
                "(parish_id, change_type, proposed_first_name, proposed_last_name, "
                " proposed_position, proposed_employee_number, created_by_user_id, as_of_date) "
                "VALUES (%s, 'add', %s, %s, %s, %s, %s, %s) RETURNING *",
                (parish_id, first_name, last_name, position or None, employee_number or None, user_id, hire_date),
            )
            return cur.fetchone()


def propose_edit(parish_id: int, staff_id: int, first_name: str, last_name: str,
                  position: str | None, employee_number: str | None, user_id: int) -> dict:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.staff_roster_changes "
                "(parish_id, staff_id, change_type, proposed_first_name, proposed_last_name, "
                " proposed_position, proposed_employee_number, created_by_user_id) "
                "VALUES (%s, %s, 'edit', %s, %s, %s, %s, %s) RETURNING *",
                (parish_id, staff_id, first_name, last_name, position or None, employee_number or None, user_id),
            )
            return cur.fetchone()


def propose_toggle(parish_id: int, staff_id: int, new_active: bool, user_id: int, *,
                   as_of_date=None, reason: str | None = None, note: str | None = None) -> dict:
    """new_active=False -> a 'deactivate' proposal; True -> 'reactivate'. as_of_date is the last day (deactivate) or the return
    date (reactivate); reason is a key of DEACTIVATE_REASONS / REACTIVATE_REASONS; note is free words (never personal data)."""
    change_type = "reactivate" if new_active else "deactivate"
    cols = ["parish_id", "staff_id", "change_type", "created_by_user_id", "as_of_date"]
    vals = [parish_id, staff_id, change_type, user_id, as_of_date]
    have = _reason_columns()
    if "proposed_reason" in have and reason:
        cols.append("proposed_reason")
        vals.append(reason)
    if "proposed_note" in have and note:
        cols.append("proposed_note")
        vals.append(note)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO portal.staff_roster_changes ({', '.join(cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *", tuple(vals))
            return cur.fetchone()


def withdraw_change(change_id: int, parish_id: int, user_id: int) -> dict | None:
    """The parish takes back one of its OWN changes that nobody has answered (queued, or waiting in Roster Review). Returns the
    row, or None when there is nothing to withdraw (already answered, or not this parish's). The row is kept as history."""
    return db.query_one(
        "UPDATE portal.staff_roster_changes SET status = 'rejected', reviewed_by_user_id = %s, reviewed_at = NOW(), "
        "review_note = %s WHERE id = %s AND parish_id = %s AND status = 'pending' RETURNING *",
        (user_id, WITHDRAWN_NOTE, change_id, parish_id))


def submit_pending_for_parish(parish_id: int, period_id: int, user_id: int) -> int:
    """Bundles every still-queued proposal for this parish into the Submit
    action that just happened (timekeeping_entries.py's
    POST /timekeeping/submit) — stamps payroll_period_id/
    submitted_by_user_id/submitted_at on each, which is what makes them
    visible in the diocese's review queue. Returns the count bundled (0 is
    the normal case — most Submits carry no roster changes at all, per the
    plan's own "if no changes needed, submit hours only" flow)."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.staff_roster_changes SET payroll_period_id = %s, "
                "submitted_by_user_id = %s, submitted_at = NOW() "
                "WHERE parish_id = %s AND status = 'pending' AND payroll_period_id IS NULL",
                (period_id, user_id, parish_id),
            )
            return cur.rowcount


# ── Diocese side: review ─────────────────────────────────────────────────────

def list_pending_review_for_org(org_id: int) -> list[dict]:
    """Every submitted-but-not-yet-reviewed roster change across every
    parish under this diocese — the hr_admin review queue. Scoped via
    payroll_periods.org_id (the diocese that owns the period it rode in
    on), never a bare parish_id join alone, so a change can never surface
    under the wrong diocese's queue."""
    roster_departures.apply_due_departures(org_id)   # a person whose last day has passed is inactive before Roster Review reads the roster
    rows = db.query(
        "SELECT src.*, p.name AS parish_name, "
        "       sr.first_name AS current_first_name, sr.last_name AS current_last_name, sr.is_active AS current_active, "
        "       u.display_name AS created_by_name, u.email AS created_by_email, "
        "       pp.label AS period_label, pp.period_start, pp.period_end "
        "FROM portal.staff_roster_changes src "
        "JOIN portal.payroll_periods pp ON pp.id = src.payroll_period_id "
        "JOIN portal.parishes p ON p.id = src.parish_id "
        "LEFT JOIN portal.staff_roster sr ON sr.id = src.staff_id "
        "LEFT JOIN checkreq.app_users u ON u.id = src.created_by_user_id "
        "WHERE pp.org_id = %s AND src.status = 'pending' "
        "ORDER BY src.submitted_at",
        (org_id,),
    )
    # A pending ADD for someone who is already on the roster (Roster Review shows it and offers Reactivate when she is
    # inactive, instead of approving a duplicate).
    today = roster_departures.today_for_org(org_id)
    for r in rows:
        # 26-158: approving a termination whose last day is today or later keeps the person on the roster until then (the screen says so)
        r["leaves_on"] = (roster_departures.leaves_later(r["as_of_date"], today)
                          if r["change_type"] == "deactivate" and r["current_active"] else None)
        ex = existing_staff_for_add(r)
        r["existing_staff_id"] = ex["id"] if ex else None
        r["existing_active"] = bool(ex["is_active"]) if ex else None
        r["existing_name"] = f"{ex['first_name']} {ex['last_name']}" if ex else None
        r["existing_employee_number"] = ex["employee_number"] if ex else None
    return rows


def count_pending_review_for_parish(org_id: int, parish_id: int) -> int:
    """How many submitted roster changes of ONE department wait in Roster Review (the diocese's own count of them; scoped through
    the period's diocese like list_pending_review_for_org, never by parish alone). Time Review shows this as a warning flag."""
    row = db.query_one(
        "SELECT count(*) AS n FROM portal.staff_roster_changes src "
        "JOIN portal.payroll_periods pp ON pp.id = src.payroll_period_id "
        "WHERE pp.org_id = %s AND src.parish_id = %s AND src.status = 'pending'", (org_id, parish_id))
    return int(row["n"]) if row else 0


def existing_staff_for_add(change: dict) -> dict | None:
    """For a pending 'add': the roster person at the same parish it is really about (same employee number, or the same
    surname with a first name that fits: Mary / Mary Frances, Tim / Timothy), active or not. None for any other change type."""
    if change.get("change_type") != "add":
        return None
    emp = (change.get("proposed_employee_number") or "").strip()
    first, last = change.get("proposed_first_name"), (change.get("proposed_last_name") or "").strip().lower()
    rows = db.query(
        "SELECT id, first_name, last_name, employee_number, is_active FROM portal.staff_roster "
        "WHERE parish_id = %s ORDER BY is_active DESC, id", (change["parish_id"],))
    if emp:
        for r in rows:
            if (r["employee_number"] or "") == emp:
                return r
    if last:
        for r in rows:
            if (r["last_name"] or "").strip().lower() == last and payroll_totals._first_names_fit(first, r["first_name"]):
                return r
    return None


def get_change_for_review(change_id: int, org_id: int) -> dict | None:
    """Scoped lookup for the approve/reject routes — same choke-point
    discipline as registry.get_parish(): returns None if change_id doesn't
    exist, isn't submitted yet (payroll_period_id IS NULL — can't be acted
    on by the diocese before the parish even submits it), isn't still
    pending, or belongs to a different diocese, so a crafted id from
    another org's queue can never be approved/rejected through this org's
    own routes."""
    return db.query_one(
        "SELECT src.*, sr.first_name AS current_first_name, sr.last_name AS current_last_name "
        "FROM portal.staff_roster_changes src "
        "LEFT JOIN portal.staff_roster sr ON sr.id = src.staff_id "
        "JOIN portal.payroll_periods pp ON pp.id = src.payroll_period_id "
        "WHERE src.id = %s AND pp.org_id = %s AND src.status = 'pending'",
        (change_id, org_id),
    )


_UNSET = object()


def leaving_date_for(change: dict):
    """26-158: for a DEACTIVATE whose last day is today or later (the diocese's own today) and whose person is still active,
    that last day: approving it keeps the person on the roster until then (roster_departures.py). Otherwise None: it takes
    effect at once, exactly as before."""
    if change.get("change_type") != "deactivate" or not change.get("staff_id"):
        return None
    st = db.query_one("SELECT s.is_active, p.org_id FROM portal.staff_roster s JOIN portal.parishes p ON p.id = s.parish_id "
                      "WHERE s.id = %s AND s.parish_id = %s", (change["staff_id"], change["parish_id"]))
    if not st or not st["is_active"]:
        return None
    return roster_departures.leaves_later(change.get("as_of_date"), roster_departures.today_for_org(st["org_id"]))


def _apply_change(change: dict, leaves_on=_UNSET) -> int | None:
    """Writes the approved change into the REAL portal.staff_roster —
    duplicates timekeeping_roster.create_staff()/update_staff()'s exact
    field shape rather than importing that module (see this file's own
    docstring for why: timekeeping_roster.py imports THIS module for its
    propose_* calls, so importing it back here would be circular).

    leaves_on (26-158): the last day of a termination that takes effect LATER (leaving_date_for); the person stays active and
    only the date is recorded. Left out, it is worked out here."""
    if leaves_on is _UNSET:
        leaves_on = leaving_date_for(change)
    parish_id = change["parish_id"]
    ct = change["change_type"]
    new_staff_id = None
    with db.connect() as conn:
        with conn.cursor() as cur:
            if ct == "add":
                cur.execute(
                    "INSERT INTO portal.staff_roster "
                    "(parish_id, first_name, last_name, position, employee_number, created_by_user_id, effective_date) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (parish_id, change["proposed_first_name"], change["proposed_last_name"],
                     change["proposed_position"], change["proposed_employee_number"],
                     change["created_by_user_id"], change.get("as_of_date")),
                )
                new_staff_id = cur.fetchone()["id"]
            elif ct == "edit" and change.get("proposed_captures_hours") is not None:
                # A FIELD-LEVEL change: raised by the payroll-register reconciler
                # (reconcile_dme_employees.py) or recorded by the diocese. These
                # carry the hours flag, and any proposed_* column left empty means
                # "leave this field alone". The whole-row UPDATE below would have
                # blanked the employee number and position and never applied the
                # hours flag (found 2026-10-10: the reconciler's queued rows
                # could not be approved safely).
                cur.execute(
                    "UPDATE portal.staff_roster SET "
                    "first_name = COALESCE(%s, first_name), last_name = COALESCE(%s, last_name), "
                    "position = COALESCE(%s, position), employee_number = COALESCE(%s, employee_number), "
                    "captures_hours = %s, effective_date = COALESCE(%s, effective_date), "
                    "updated_at = NOW() WHERE id = %s AND parish_id = %s",
                    (change["proposed_first_name"], change["proposed_last_name"],
                     change["proposed_position"], change["proposed_employee_number"],
                     change["proposed_captures_hours"], change.get("as_of_date"),
                     change["staff_id"], parish_id),
                )
            elif ct == "edit":
                # A parish proposal: the whole row as the parish submitted it
                # (timekeeping_roster.py's edit form always posts every field).
                cur.execute(
                    "UPDATE portal.staff_roster SET first_name = %s, last_name = %s, "
                    "position = %s, employee_number = %s, updated_at = NOW() "
                    "WHERE id = %s AND parish_id = %s",
                    (change["proposed_first_name"], change["proposed_last_name"],
                     change["proposed_position"], change["proposed_employee_number"],
                     change["staff_id"], parish_id),
                )
            elif ct == "deactivate" and leaves_on is not None:
                # a last day still ahead: stays active, only the date is recorded (a person already leaving gets the new date);
                # roster_departures.apply_due_departures switches them off once the day has passed
                cur.execute(
                    "UPDATE portal.staff_roster SET inactive_as_of = %s, updated_at = NOW() WHERE id = %s AND parish_id = %s",
                    (leaves_on, change["staff_id"], parish_id))
            elif ct in ("deactivate", "reactivate"):
                # a reactivated person is no longer "inactive as of" any date (the Employees screen does the same)
                cur.execute(
                    "UPDATE portal.staff_roster SET is_active = %s, "
                    "inactive_as_of = CASE WHEN %s THEN NULL ELSE COALESCE(%s, inactive_as_of) END, updated_at = NOW() "
                    "WHERE id = %s AND parish_id = %s",
                    (ct == "reactivate", ct == "reactivate", change.get("as_of_date"), change["staff_id"], parish_id),
                )
    return new_staff_id


def approve_change_detailed(change: dict, reviewer_user_id: int, review_note: str | None = None) -> tuple:
    """Applies the change to the real roster FIRST, then stamps this row
    approved — if _apply_change() somehow raised, the row stays 'pending'
    rather than being marked approved with nothing having actually
    happened.

    Returns (leaves_on, new_staff_id): leaves_on is the last day when this was a termination that takes effect LATER (the person
    stays on the roster until then; the screen and the notice to the parish say so), else None; new_staff_id is the id of the
    roster row an 'add' created, else None (roster_onboarding links an onboarding case to it)."""
    leaves_on = leaving_date_for(change)
    new_staff_id = _apply_change(change, leaves_on)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.staff_roster_changes SET status = 'approved', "
                "reviewed_by_user_id = %s, reviewed_at = NOW(), review_note = %s WHERE id = %s",
                (reviewer_user_id, review_note, change["id"]),
            )
    if change["change_type"] == "add" and new_staff_id:
        # 26-158: hours already recorded for this pending new hire move onto the new roster row.
        payroll_totals.on_roster_add_approved(change["id"], new_staff_id)
    if change["change_type"] == "reactivate" and change.get("staff_id"):
        # 26-158: hours an email gave for her (waiting in "named, not entered yet") are entered now, to wait for review.
        payroll_totals.on_staff_reactivated(change["staff_id"], reviewer_user_id)
    _notify_outcome(change, approved=True, review_note=review_note, leaves_on=leaves_on)
    return leaves_on, new_staff_id


def approve_change(change: dict, reviewer_user_id: int, review_note: str | None = None):
    """approve_change_detailed(), keeping only its first answer: the last day when this was a termination that takes effect
    LATER (else None). Every caller that does not need the new roster id uses this one."""
    return approve_change_detailed(change, reviewer_user_id, review_note)[0]


def reactivate_existing_instead(change: dict, reviewer_user_id: int, review_note: str | None = None) -> int | None:
    """A pending ADD that is really someone already on the roster as INACTIVE (a rehire): reactivate that person rather than
    creating a duplicate. The add request is stamped approved with a note saying so, and the hours recorded for the pending
    hire move onto the reactivated person. Returns her roster id, or None when there is no inactive match to reactivate."""
    ex = existing_staff_for_add(change)
    if not ex or ex["is_active"]:
        return None
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.staff_roster SET is_active = TRUE, inactive_as_of = NULL, effective_date = CURRENT_DATE, "
                "updated_at = NOW() WHERE id = %s AND parish_id = %s", (ex["id"], change["parish_id"]))
            cur.execute(
                "UPDATE portal.staff_roster_changes SET status = 'approved', reviewed_by_user_id = %s, reviewed_at = NOW(), "
                "review_note = %s WHERE id = %s",
                (reviewer_user_id, review_note or "Reactivated the existing roster person instead of adding a duplicate.",
                 change["id"]))
    payroll_totals.on_roster_add_approved(change["id"], ex["id"])
    payroll_totals.on_staff_reactivated(ex["id"], reviewer_user_id)
    _notify_outcome(change, approved=True, review_note=review_note)
    return ex["id"]


def reject_change(change: dict, reviewer_user_id: int, review_note: str | None = None) -> None:
    """Never touches portal.staff_roster — rejection only stamps status +
    a reason; the row itself is never deleted (this app's standing
    never-truly-delete convention)."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.staff_roster_changes SET status = 'rejected', "
                "reviewed_by_user_id = %s, reviewed_at = NOW(), review_note = %s WHERE id = %s",
                (reviewer_user_id, review_note, change["id"]),
            )
    if change["change_type"] == "add":
        # 26-158: hours recorded for a rejected new hire are rejected too (the rows are kept).
        payroll_totals.on_roster_add_rejected(change["id"], reviewer_user_id)
    _notify_outcome(change, approved=False, review_note=review_note)


def _notify_outcome(change: dict, approved: bool, review_note: str | None, leaves_on=None) -> None:
    """Bell-only notification to the ORIGINAL PROPOSER (created_by_user_id)
    — per the plan's decision 3, the existing generic notification bell is
    enough for v1, no new email plumbing. Fails open via
    notifications.create_notification()'s own try/except, so a
    notification hiccup can never undo or block the real approve/reject
    action that already committed above."""
    if not change.get("created_by_user_id"):
        return
    parish = db.query_one("SELECT name FROM portal.parishes WHERE id = %s", (change["parish_id"],))
    parish_name = parish["name"] if parish else "your parish"
    label = _CHANGE_TYPE_LABELS.get(change["change_type"], "Roster change")
    name = _staff_name_from_change(change)
    if approved:
        message = f"{label} ({name}) for {parish_name} was approved."
        if leaves_on is not None:
            message += f" {name} stays on the roster until {roster_departures.fmt(leaves_on)}, then becomes inactive automatically."
    else:
        reason = f" Reason: {review_note}" if review_note else ""
        message = f"{label} ({name}) for {parish_name} was rejected.{reason}"
    notifications.create_notification(
        change["created_by_user_id"],
        "roster_change_reviewed",
        message,
        "/timekeeping/roster",
    )
