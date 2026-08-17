"""
timekeeping_entries.py — Cornerstone Served Parishes Phase K, Stage 2 (a
real, working slice of it): the parish-side time entry grid (Parish Portal
Plan.md PP-804: "For each open period, the authorized parish user records
time per staff member ... Categories ship as Regular/Overtime/PTO-Sick/
Holiday ... Drafts save").

WHAT THIS FILE DOES: view the diocese's current OPEN payroll period as a
grid (one section per active staff member, categories as rows, calendar
dates in the period as columns -- matching the plan's own literal
description) and save entered hours as a draft. Every cell write is
recorded in portal.time_entry_edits (old/new hours, who, when) so the
audit trail exists from day one, even though nothing yet reads it back
(that's Stage 4).

WHAT THIS FILE DELIBERATELY DOES NOT DO (left for later stages, see this
session's own build-plan write-up):
  - Reopen-with-audit-trail (Stage 3's other half -- Submit/lock itself IS
    now built, see below, 2026-08-16).
  - Diocese-side review/adjust/status-board/Excel export (Stage 4/5 --
    PP-806/PP-807) -- list_submitted_periods_for_org() below is a
    READ-ONLY visibility list for the new hr_admin review screen
    (timekeeping_review.py), not adjustment/finalization.
  - Deadline reminders/escalation (PP-805).
A save is REFUSED unless the period is 'open' AND the parish's own submission
is still 'draft'/'reopened' (both checked below). The period half was
tightened 2026-08-17: it used to refuse only a 'processed' period, which meant
'closed' (and later 'future') periods were still writable -- with 25 of DME's
26 periods sitting at 'closed', that was every period but one. Now the rule is
Jay's own: "only 'open' is available for data entry and submission".

SUBMIT / LOCK (Stage 3, added 2026-08-16, Timekeeping HR Roster Review
Plan.md): POST /timekeeping/submit stamps the parish's own
time_entry_submissions row 'submitted' (submitted_by_user_id/submitted_at,
a timekeeping_events row) AND, in the same action, bundles any roster
changes the parish has queued since their last submission (via
timekeeping_roster_changes.submit_pending_for_parish()) -- the plan's own
"one Submit action carries both" design. The two halves are otherwise
completely independent (plan decision 2): a roster change still pending
diocese review from an earlier period never blocks this period's own
Submit, and Submit never blocks on anything the roster side is waiting on.

DIOCESE-SIDE STATUS BOARD + BACKFILL (added 2026-08-16, per Jay's direct
request: "screens to check the status of submissions for a period, and the
ability to enter missing data, and to generate a spreadsheet with the data
submitted for a period"): build_grid_context()/apply_hours_from_form()
below are the two pieces of this file's own logic extracted so
timekeeping_status.py (new sibling module -- the status board, the
diocese-side backfill screen, and the Excel export) can reuse the EXACT
same grid-building and hours-writing code the parish's own /timekeeping and
/timekeeping/save routes use, instead of a second, drift-prone copy.
entry_page()/entry_save() below are now thin wrappers around these two
functions with parish-side authorization/locking; timekeeping_status.py's
own backfill routes call the same two functions with diocese-side
authorization/locking instead (see that module's own docstring for the one
real behavioral difference: a diocese-side save may override an
already-'submitted' submission, but still never a period that is not 'open').
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
import timekeeping
import timekeeping_roster
import timekeeping_roster_changes

router = APIRouter()

_render = None


def register(app, *, render) -> None:
    global _render
    _render = render
    app.include_router(router)


# ── Data access ──────────────────────────────────────────────────────────────

def _dates_in_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def get_submission(period_id: int, parish_id: int) -> dict | None:
    return db.query_one(
        "SELECT * FROM portal.time_entry_submissions WHERE period_id = %s AND parish_id = %s",
        (period_id, parish_id),
    )


def get_or_create_submission(period_id: int, parish_id: int) -> dict:
    """Public (no longer underscore-prefixed) since 2026-08-16 -- also called
    from timekeeping_status.py's diocese-side backfill save route, which
    needs the exact same "make sure a submission row exists before writing
    hours against it" behavior the parish's own /timekeeping/save already
    has, not a second copy."""
    existing = get_submission(period_id, parish_id)
    if existing:
        return existing
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.time_entry_submissions (period_id, parish_id) "
                "VALUES (%s, %s) ON CONFLICT (period_id, parish_id) DO NOTHING RETURNING *",
                (period_id, parish_id),
            )
            row = cur.fetchone()
    return row or get_submission(period_id, parish_id)


def get_entries_map(period_id: int, parish_id: int) -> dict:
    """{(staff_id, category_id, work_date): hours_as_float} for every
    time_entries cell logged so far for this parish's staff in this
    period. Joined through staff_roster.parish_id so a stray cross-parish
    id could never leak another parish's hours into this grid even if one
    somehow existed."""
    rows = db.query(
        "SELECT te.staff_id, te.category_id, te.work_date, te.hours "
        "FROM portal.time_entries te "
        "JOIN portal.staff_roster sr ON sr.id = te.staff_id "
        "WHERE te.period_id = %s AND sr.parish_id = %s",
        (period_id, parish_id),
    )
    return {(r["staff_id"], r["category_id"], r["work_date"]): float(r["hours"]) for r in rows}


def list_submitted_periods_for_org(org_id: int) -> list[dict]:
    """Every parish submission still awaiting diocese review (status =
    'submitted') under this diocese -- the hr_admin review screen's
    time-entry-side list (Timekeeping HR Roster Review Plan.md). READ-ONLY
    for this pass -- adjusting/finalizing individual hours (PP-806/807's
    fuller diocese-side review) is a separate, larger, not-yet-scoped Stage
    4 build; this only answers "which periods need a look," matching the
    approved plan's own literal scope."""
    return db.query(
        "SELECT tes.id AS submission_id, tes.period_id, tes.parish_id, tes.status, "
        "       tes.submitted_by_user_id, tes.submitted_at, "
        "       p.name AS parish_name, pp.label AS period_label, "
        "       pp.period_start, pp.period_end, "
        "       u.display_name AS submitted_by_name, u.email AS submitted_by_email, "
        "       COALESCE(SUM(te.hours), 0) AS total_hours "
        "FROM portal.time_entry_submissions tes "
        "JOIN portal.payroll_periods pp ON pp.id = tes.period_id "
        "JOIN portal.parishes p ON p.id = tes.parish_id "
        "LEFT JOIN checkreq.app_users u ON u.id = tes.submitted_by_user_id "
        "LEFT JOIN portal.time_entries te ON te.period_id = tes.period_id "
        "     AND te.staff_id IN (SELECT id FROM portal.staff_roster WHERE parish_id = tes.parish_id) "
        "WHERE pp.org_id = %s AND tes.status = 'submitted' "
        "GROUP BY tes.id, tes.period_id, tes.parish_id, tes.status, tes.submitted_by_user_id, "
        "         tes.submitted_at, p.name, pp.label, pp.period_start, pp.period_end, "
        "         u.display_name, u.email "
        "ORDER BY tes.submitted_at",
        (org_id,),
    )


def build_grid_context(parish: dict, diocese_org_id: int, period: dict | None,
                        can_enter: bool, bypass_submission_lock: bool = False) -> dict:
    """The shared "grid data for one parish/period" builder (extracted
    2026-08-16 so timekeeping_status.py's diocese-side backfill screen can
    reuse it -- see the module docstring's "DIOCESE-SIDE STATUS BOARD +
    BACKFILL" section). Used by both the parish's own GET /timekeeping
    (period is always the diocese's current OPEN one) and the diocese-side
    backfill screen (period is whichever specific one an hr_admin picked
    from the status board -- open, closed, or even processed, for viewing).

    Caller does ALL authorization -- this function only assembles data,
    given an already-decided `can_enter` (the parish route derives it from
    timekeeping.can_manage_timekeeping(); the diocese route already gated on
    hr_admin/beacon_admin at the route level, so it just passes True).

    bypass_submission_lock=True is the diocese's own "enter missing data on
    an already-submitted period" override -- an hr_admin can still view/edit
    a parish's grid even once that parish has submitted it (locked normally
    computes True in that case). It does NOT bypass a period that is not
    'open', which always locks regardless of this flag -- a closed or future
    period is diocese-wide read-only, not something any one screen should
    quietly reopen."""
    if not period:
        return {
            "parish": parish, "period": None, "staff": [], "categories": [],
            "dates": [], "entries": {}, "can_enter": can_enter, "submission": None,
            "locked": False,
        }
    # only_hours_capturing: salaried / housing-only people are real roster
    # members but have no hours to enter, so they get no grid row (see
    # timekeeping_roster.list_staff's own docstring).
    staff = timekeeping_roster.list_staff(parish["id"], only_hours_capturing=True)
    categories = timekeeping.list_categories(diocese_org_id)
    dates = _dates_in_range(period["period_start"], period["period_end"])
    entries = get_entries_map(period["id"], parish["id"])
    submission = get_submission(period["id"], parish["id"])
    # Only an 'open' period is writable (Jay, 2026-08-17: "a pay period is
    # either 'open', 'closed', or 'future' - only 'open' is available for data
    # entry and submission"). Inverted from the old `== "processed"` test, which
    # refused writes for exactly one status and therefore left BOTH 'closed'
    # and (once migration 048 added it) 'future' periods writable -- with 25 of
    # DME's 26 periods sitting at 'closed', that was every period but one.
    # Written as "allow only open" rather than "deny this list" so a status
    # value added later can never accidentally become writable.
    locked = period["status"] != "open"
    if not locked and not bypass_submission_lock and submission and submission["status"] == "submitted":
        locked = True
    return {
        "parish": parish, "period": period, "staff": staff, "categories": categories,
        "dates": dates, "entries": entries, "can_enter": can_enter,
        "submission": submission, "locked": locked,
    }


def apply_hours_from_form(period: dict, parish: dict, diocese_org_id: int, user: dict,
                           form, edit_source: str = "parish") -> int:
    """Writes hours_<staff>_<cat>_<date> fields from a submitted form into
    portal.time_entries, with a portal.time_entry_edits audit row per real
    change (extracted 2026-08-16, see the module docstring). edit_source
    distinguishes the parish's own entry from a diocese-side backfill/
    override -- portal.time_entry_edits.edit_source's CHECK constraint
    already allows both 'parish' and 'diocese' (migration 038), no schema
    change needed for this. Shared by the parish's own /timekeeping/save
    route (edit_source='parish') and timekeeping_status.py's diocese-side
    backfill save route (edit_source='diocese'), so the two never drift.

    Caller must have already performed every authorization/lock check --
    this function only ever validates that a given (staff, category, date)
    cell is REAL and belongs to this parish/period, never who is allowed to
    write it."""
    # Same restriction as the grid itself -- a posted cell for someone whose
    # hours are not collected must not be accepted, not merely hidden.
    staff_ids = {s["id"] for s in timekeeping_roster.list_staff(
        parish["id"], only_hours_capturing=True)}
    category_ids = {c["id"] for c in timekeeping.list_categories(diocese_org_id)}
    valid_dates = set(_dates_in_range(period["period_start"], period["period_end"]))

    saved = 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            for key, value in form.multi_items():
                if not key.startswith("hours_"):
                    continue
                parts = key.split("_", 3)
                if len(parts) != 4:
                    continue
                _, staff_s, cat_s, date_s = parts
                try:
                    staff_id, category_id = int(staff_s), int(cat_s)
                    work_date = date.fromisoformat(date_s)
                except ValueError:
                    continue
                # Never trust a client-supplied id/date outside this
                # parish's own roster / this period's own real date range --
                # a crafted field name could otherwise write into another
                # parish's staff row or a date outside the period.
                if staff_id not in staff_ids or category_id not in category_ids or work_date not in valid_dates:
                    continue

                hours_str = (value or "").strip()
                if hours_str == "":
                    hours_str = "0"
                try:
                    hours = float(hours_str)
                except ValueError:
                    continue
                hours = max(0.0, min(24.0, hours))

                cur.execute(
                    "SELECT id, hours FROM portal.time_entries "
                    "WHERE period_id = %s AND staff_id = %s AND category_id = %s AND work_date = %s",
                    (period["id"], staff_id, category_id, work_date),
                )
                existing = cur.fetchone()
                if existing and abs(float(existing["hours"]) - hours) < 0.001:
                    continue  # no real change -- skip the write and the audit row

                if existing:
                    old_hours = float(existing["hours"])
                    cur.execute(
                        "UPDATE portal.time_entries SET hours = %s, last_edited_by_user_id = %s, "
                        "last_edited_at = NOW() WHERE id = %s",
                        (hours, user["id"], existing["id"]),
                    )
                    entry_id = existing["id"]
                else:
                    old_hours = None
                    cur.execute(
                        "INSERT INTO portal.time_entries "
                        "(period_id, staff_id, category_id, work_date, hours, last_edited_by_user_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                        (period["id"], staff_id, category_id, work_date, hours, user["id"]),
                    )
                    entry_id = cur.fetchone()["id"]

                cur.execute(
                    "INSERT INTO portal.time_entry_edits "
                    "(time_entry_id, old_hours, new_hours, edited_by_user_id, edit_source) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (entry_id, old_hours, hours, user["id"], edit_source),
                )
                saved += 1
    return saved


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/timekeeping", response_class=HTMLResponse)
def entry_page(request: Request):
    user, parish, diocese_org, err = timekeeping.timekeeping_context(request)
    if err:
        return err

    period = timekeeping.get_current_open_period(diocese_org["id"])
    can_enter = timekeeping.can_manage_timekeeping(user, parish)
    ctx = build_grid_context(parish, diocese_org["id"], period, can_enter)
    return _render(request, "timekeeping_entry.html", user, ctx)


@router.post("/timekeeping/save")
async def entry_save(request: Request):
    user, parish, diocese_org, err = timekeeping.timekeeping_context(request)
    if err:
        return err
    if not timekeeping.can_manage_timekeeping(user, parish):
        return JSONResponse(
            {"error": "You don't have permission to enter time for this parish."}, status_code=403
        )

    period = timekeeping.get_current_open_period(diocese_org["id"])
    if not period:
        return RedirectResponse("/timekeeping?error=noperiod", status_code=303)
    # Only an 'open' period accepts writes -- see the comment on `locked` in
    # build_grid_context() for why this is "allow only open", not "deny
    # processed".
    if period["status"] != "open":
        return RedirectResponse("/timekeeping?error=notopen", status_code=303)

    submission = get_or_create_submission(period["id"], parish["id"])
    if submission and submission["status"] not in ("draft", "reopened"):
        # No UI path reaches 'submitted' yet (Stage 3 isn't built), but this
        # guard stays live regardless -- it's the rule the schema exists to
        # enforce, not just the paths this pass happens to expose.
        return RedirectResponse("/timekeeping?error=locked", status_code=303)

    form = await request.form()
    saved = apply_hours_from_form(period, parish, diocese_org["id"], user, form, edit_source="parish")
    return RedirectResponse(f"/timekeeping?saved={saved}", status_code=303)


@router.post("/timekeeping/submit")
def entry_submit(request: Request):
    """Stage 3, added 2026-08-16 (Timekeeping HR Roster Review Plan.md): the
    parish's own single Submit action for the current open period -- locks
    the time-entry submission AND bundles any roster changes queued since
    the last submission, per the plan's "one Submit action carries both"
    design. No reopen mechanism exists yet (that's still Stage 4) -- an
    already-'submitted' period can't be resubmitted through this route
    today; the parish has to ask the diocese."""
    user, parish, diocese_org, err = timekeeping.timekeeping_context(request)
    if err:
        return err
    if not timekeeping.can_manage_timekeeping(user, parish):
        return JSONResponse(
            {"error": "You don't have permission to submit time for this parish."}, status_code=403
        )

    period = timekeeping.get_current_open_period(diocese_org["id"])
    if not period:
        return RedirectResponse("/timekeeping?error=noperiod", status_code=303)
    # Only an 'open' period accepts writes -- see the comment on `locked` in
    # build_grid_context() for why this is "allow only open", not "deny
    # processed".
    if period["status"] != "open":
        return RedirectResponse("/timekeeping?error=notopen", status_code=303)

    submission = get_or_create_submission(period["id"], parish["id"])
    if submission and submission["status"] == "submitted":
        return RedirectResponse("/timekeeping?error=already_submitted", status_code=303)

    event_type = "resubmitted" if submission and submission["status"] == "reopened" else "submitted"
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.time_entry_submissions SET status = 'submitted', "
                "submitted_by_user_id = %s, submitted_at = NOW(), updated_at = NOW() WHERE id = %s",
                (user["id"], submission["id"]),
            )
            cur.execute(
                "INSERT INTO portal.timekeeping_events (submission_id, event_type, event_by_user_id) "
                "VALUES (%s, %s, %s)",
                (submission["id"], event_type, user["id"]),
            )

    # Independent of the lock above (plan decision 2) -- bundles whatever
    # roster changes this parish has queued since their last submission,
    # 0 is the normal/expected case.
    bundled = timekeeping_roster_changes.submit_pending_for_parish(parish["id"], period["id"], user["id"])

    return RedirectResponse(f"/timekeeping?submitted=1&roster_changes={bundled}", status_code=303)
