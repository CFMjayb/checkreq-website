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

from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import rbac
import cornerstone_mode
import email_pdf
import roster_auto_case
import roster_onboarding
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


def attach_pdf_urls(org_id: int, changes: list[dict]) -> list[dict]:
    """Roster Review's PDF button (26-158, 2026-10-27): puts `pdf_url` on every request -- the viewer address of the stored, masked
    copy of the email that raised it, or None. `source_ref` is the email's message id (set by the payroll reader); a request a parish
    typed has none, and an email whose copy has not been stored yet (the reader stores it on its next run) shows no button either.
    email_pdf.url_for keeps ONE cached bucket listing per diocese, so a page with many requests costs one call, and a bucket that cannot
    be read answers 'none stored' rather than breaking the page. The address is the same viewer the Time Review screens use, which has
    the same gate as this screen (HR Admin or Beacon Admin of this diocese; the diocese's own folder)."""
    for c in changes:
        ref = c.get("source_ref")
        c["pdf_url"] = email_pdf.url_for(org_id, ref) if ref else None
    return changes


@router.get("/admin/timekeeping/review", response_class=HTMLResponse)
def review_page(request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    # Roster Review (Jay, 2026-10-11): only the roster changes. The submitted-periods list is gone: all the time review is
    # done on Time Review (the old Time Status board).
    roster_changes = timekeeping_roster_changes.list_pending_review_for_org(org["id"])
    # Time Review's warning flag links here with ?parish_id=: show just that department (and say so, with a way back to all)
    only_parish = None
    raw = request.query_params.get("parish_id")
    if raw and raw.isdigit():
        shown = [c for c in roster_changes if c["parish_id"] == int(raw)]
        if shown:
            only_parish = {"id": int(raw), "name": shown[0]["parish_name"]}
            roster_changes = shown
    attach_pdf_urls(org["id"], roster_changes)
    return _render(request, "timekeeping_review.html", user, {
        "current_org": org,
        "roster_changes": roster_changes,
        "only_parish": only_parish,
        "roster_reason": timekeeping_roster_changes.reason_label,
        # Move to Onboarding (26-158/26-162): which new-hire rows get the pop-up, and what the redirect after one says
        "onboarding_offered_ids": roster_onboarding.offered_ids(org["id"], roster_changes),
        "onboarding": roster_onboarding.banner_context(org["id"], request.query_params),
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
    leaves_on, new_staff_id = timekeeping_roster_changes.approve_change_detailed(change, user["id"], note)
    tail = ""
    if leaves_on is not None:
        # a termination whose last day is still ahead: the banner says the person stays on the roster until then
        tail += "&leaves=" + quote(leaves_on.strftime("%m/%d/%y"), safe="")
    auto = roster_auto_case.case_for_staff(new_staff_id) if new_staff_id else None
    if auto:
        # 26-162: every roster add makes an onboarding case, so approving a new hire has already opened one: the page says so
        # (the old Move to Onboarding box, if an open page still sends it, changes nothing)
        tail += f"&onboarding_case={int(auto['id'])}"
    elif roster_onboarding.ticked(form):
        # "Move to Onboarding" was ticked. The approval above has already happened and always stands: whatever this step
        # cannot do becomes one sentence on the page (roster_onboarding.py has every rule, and checks them again here).
        tail += roster_onboarding.redirect_query(roster_onboarding.open_for_new_hire(org["id"], change, new_staff_id, user["id"]))
    return RedirectResponse("/admin/timekeeping/review?approved=1" + tail, status_code=303)


@router.post("/admin/timekeeping/review/roster-changes/{change_id}/reactivate")
async def roster_change_reactivate(change_id: int, request: Request):
    """A pending add for someone already on the roster as inactive: reactivate her instead of adding a duplicate."""
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
    if timekeeping_roster_changes.reactivate_existing_instead(change, user["id"], note) is None:
        return RedirectResponse(
            "/admin/timekeeping/review?error=That+person+is+not+on+the+roster+as+inactive%2C+so+there+is+nobody+to+reactivate.",
            status_code=303,
        )
    return RedirectResponse("/admin/timekeeping/review?reactivated=1", status_code=303)


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
