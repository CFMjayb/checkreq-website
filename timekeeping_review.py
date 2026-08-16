"""
timekeeping_review.py — Timekeeping & HR, Stage 4: the diocese-side review
screen (Timekeeping HR Roster Review Plan.md, approved with Jay
2026-08-16). Gated on the new hr_admin role -- ONE screen/role covers BOTH
halves of the diocese-side Timekeeping review (submitted hours AND pending
roster changes), per the plan's decision 1 -- not two separate screens or
roles.

SCOPE, deliberately narrow for this pass: roster-change approve/reject is
fully built (the actual point of this plan). The submitted-time-entry list
is READ-ONLY -- a visibility list only, with no adjust/finalize/period-lock
action. Building real hours review/adjustment (PP-806/807's fuller Stage 4)
is a separate, larger, not-yet-scoped follow-on; this screen just shows
what's sitting there so an hr_admin knows what needs attention, matching
the approved plan's own literal ask ("showing: (a) submitted time-entry
periods needing review (per parish), and (b) pending roster changes needing
approval/rejection (per parish)").

New, single-purpose file per this project's standing modular-file
convention (timekeeping.py / timekeeping_roster.py / timekeeping_entries.py /
timekeeping_roster_changes.py are its siblings).
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import rbac
import cornerstone_mode
import timekeeping_entries
import timekeeping_roster_changes

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
    Mirrors timekeeping.py's own _require_diocese_admin() almost exactly --
    same "reject a Cornerstone-Mode-selected entity outright" rule, since
    this screen reviews diocese-wide payroll periods/roster changes, never
    a served parish's own linked org (see that function's own docstring for
    the full reasoning). beacon_admin passes too, matching every other
    diocese-admin gate in this project's own settled bootstrap-safety-net
    pattern (e.g. timekeeping.py's _require_diocese_admin, admin_setup.py's
    _require_setup_admin)."""
    user = _current_user(request)
    if not user:
        return None, None, RedirectResponse("/login")
    org = _current_org(request)
    org_id = org["id"] if org else None
    if org_id is None or cornerstone_mode.is_cornerstone_org(org_id):
        return None, None, RedirectResponse("/portal")
    if not rbac.user_has_any_role(user["id"], ["hr_admin", "beacon_admin"], org_id=org_id):
        return None, None, JSONResponse({"error": "HR Admin access required"}, status_code=403)
    return user, org, None


@router.get("/admin/timekeeping/review", response_class=HTMLResponse)
def review_page(request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    submitted_periods = timekeeping_entries.list_submitted_periods_for_org(org["id"])
    roster_changes = timekeeping_roster_changes.list_pending_review_for_org(org["id"])
    return _render(request, "timekeeping_review.html", user, {
        "current_org": org,
        "submitted_periods": submitted_periods,
        "roster_changes": roster_changes,
    })


@router.post("/admin/timekeeping/review/roster-changes/{change_id}/approve")
async def roster_change_approve(change_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    form = await request.form()
    note = (form.get("review_note") or "").strip() or None
    change = timekeeping_roster_changes.get_change_for_review(change_id, org["id"])
    if not change:
        return RedirectResponse(
            "/admin/timekeeping/review?error=That+change+was+not+found+or+has+already+been+reviewed.",
            status_code=303,
        )
    timekeeping_roster_changes.approve_change(change, user["id"], note)
    return RedirectResponse("/admin/timekeeping/review?approved=1", status_code=303)


@router.post("/admin/timekeeping/review/roster-changes/{change_id}/reject")
async def roster_change_reject(change_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    form = await request.form()
    note = (form.get("review_note") or "").strip() or None
    change = timekeeping_roster_changes.get_change_for_review(change_id, org["id"])
    if not change:
        return RedirectResponse(
            "/admin/timekeeping/review?error=That+change+was+not+found+or+has+already+been+reviewed.",
            status_code=303,
        )
    timekeeping_roster_changes.reject_change(change, user["id"], note)
    return RedirectResponse("/admin/timekeeping/review?rejected=1", status_code=303)
