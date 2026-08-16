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

import db
import notifications

_CHANGE_TYPE_LABELS = {
    "add": "Add staff member",
    "edit": "Edit staff member",
    "deactivate": "Deactivate staff member",
    "reactivate": "Reactivate staff member",
}


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
                 employee_number: str | None, user_id: int) -> dict:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.staff_roster_changes "
                "(parish_id, change_type, proposed_first_name, proposed_last_name, "
                " proposed_position, proposed_employee_number, created_by_user_id) "
                "VALUES (%s, 'add', %s, %s, %s, %s, %s) RETURNING *",
                (parish_id, first_name, last_name, position or None, employee_number or None, user_id),
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


def propose_toggle(parish_id: int, staff_id: int, new_active: bool, user_id: int) -> dict:
    """new_active=False -> a 'deactivate' proposal; True -> 'reactivate'."""
    change_type = "reactivate" if new_active else "deactivate"
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.staff_roster_changes "
                "(parish_id, staff_id, change_type, created_by_user_id) "
                "VALUES (%s, %s, %s, %s) RETURNING *",
                (parish_id, staff_id, change_type, user_id),
            )
            return cur.fetchone()


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
    return db.query(
        "SELECT src.*, p.name AS parish_name, "
        "       sr.first_name AS current_first_name, sr.last_name AS current_last_name, "
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


def _apply_change(change: dict) -> None:
    """Writes the approved change into the REAL portal.staff_roster —
    duplicates timekeeping_roster.create_staff()/update_staff()'s exact
    field shape rather than importing that module (see this file's own
    docstring for why: timekeeping_roster.py imports THIS module for its
    propose_* calls, so importing it back here would be circular)."""
    parish_id = change["parish_id"]
    ct = change["change_type"]
    with db.connect() as conn:
        with conn.cursor() as cur:
            if ct == "add":
                cur.execute(
                    "INSERT INTO portal.staff_roster "
                    "(parish_id, first_name, last_name, position, employee_number, created_by_user_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (parish_id, change["proposed_first_name"], change["proposed_last_name"],
                     change["proposed_position"], change["proposed_employee_number"],
                     change["created_by_user_id"]),
                )
            elif ct == "edit":
                cur.execute(
                    "UPDATE portal.staff_roster SET first_name = %s, last_name = %s, "
                    "position = %s, employee_number = %s, updated_at = NOW() "
                    "WHERE id = %s AND parish_id = %s",
                    (change["proposed_first_name"], change["proposed_last_name"],
                     change["proposed_position"], change["proposed_employee_number"],
                     change["staff_id"], parish_id),
                )
            elif ct in ("deactivate", "reactivate"):
                cur.execute(
                    "UPDATE portal.staff_roster SET is_active = %s, updated_at = NOW() "
                    "WHERE id = %s AND parish_id = %s",
                    (ct == "reactivate", change["staff_id"], parish_id),
                )


def approve_change(change: dict, reviewer_user_id: int, review_note: str | None = None) -> None:
    """Applies the change to the real roster FIRST, then stamps this row
    approved — if _apply_change() somehow raised, the row stays 'pending'
    rather than being marked approved with nothing having actually
    happened."""
    _apply_change(change)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.staff_roster_changes SET status = 'approved', "
                "reviewed_by_user_id = %s, reviewed_at = NOW(), review_note = %s WHERE id = %s",
                (reviewer_user_id, review_note, change["id"]),
            )
    _notify_outcome(change, approved=True, review_note=review_note)


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
    _notify_outcome(change, approved=False, review_note=review_note)


def _notify_outcome(change: dict, approved: bool, review_note: str | None) -> None:
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
    else:
        reason = f" Reason: {review_note}" if review_note else ""
        message = f"{label} ({name}) for {parish_name} was rejected.{reason}"
    notifications.create_notification(
        change["created_by_user_id"],
        "roster_change_reviewed",
        message,
        "/timekeeping/roster",
    )
