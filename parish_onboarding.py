"""
parish_onboarding.py -- "Continue to onboarding" for a new staff member, in Parish Mode (26-162 Phase 3 / 26-129, 2026-10-25).

Jay: the Staff screen's "Propose a new staff member" becomes "Onboard a new staff member": the parish fills in the person, submits,
and is then offered "Continue to onboarding". Onboarding is optional for now (some people are only added to payroll), but every
parish hire has to be submitted to the diocese for health benefits enrollment, and that submission arrives on the diocese's
Onboarding & Offboarding board.

What it does: collects what the benefits office needs about the hire and opens a diocese case of kind "Parish enrollment request"
(benefits_enroll) with source "parish", through the one engine the diocese side already uses (logic.hr_lifecycle.cases.open_case).
The case starts in status "requested", which is what the board counts as "Parish requests waiting" and what puts the red dot on the
Onboarding & Offboarding tile. The parish never sees the diocese's checklist.

    GET  /timekeeping/roster/onboarding?change_id=N    the form (filled in from the staff change the parish just made)
    POST /timekeeping/roster/onboarding                send it to the diocese

Who: exactly who may use the Staff screen (timekeeping.timekeeping_context + can_manage_timekeeping), and only where the diocese has
switched on both Onboarding & Offboarding (hr_lifecycle) and parish benefits requests (benefits_enrollment). The diocese is always
the parish's own (parish["org_id"]); nothing in the browser chooses it. No personal data is collected: names, position, hours,
hire date, clergy or lay, housing yes or no, and a note that is checked for Social Security numbers, birth dates and bank details.
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import db
import org_features
import timekeeping
import timekeeping_roster_changes
from logic.hr_lifecycle import cases, templates
from logic.hr_lifecycle.errors import HrError

router = APIRouter()
_render = None

FEATURES = ("hr_lifecycle", "benefits_enrollment")


def register(app, *, render) -> None:
    global _render
    _render = render
    app.include_router(router)


def available(parish: dict) -> bool:
    """Onboarding is offered for this parish only when its diocese has switched on both features and has a parish-enrollment
    checklist loaded (otherwise the button would lead to an error)."""
    try:
        org_id = parish["org_id"]
        if not all(org_features.is_enabled(org_id, f) for f in FEATURES):
            return False
        return bool(templates.active_template("benefits_enroll", cases.org_code(org_id)))
    except Exception:           # never let this take the Staff screen down
        return False


def _staff_url(**msg) -> str:
    q = "&".join(f"{k}={quote(str(v))}" for k, v in msg.items() if v)
    return "/timekeeping/roster" + (("?" + q) if q else "")


def _gate(request: Request):
    """(user, parish, None) when the person may onboard for this parish; (None, None, response) otherwise."""
    user, parish, _diocese, err = timekeeping.timekeeping_context(request)
    if err:
        return None, None, err
    if not timekeeping.can_manage_timekeeping(user, parish):
        return None, None, RedirectResponse(_staff_url(error="You don't have permission to manage this parish's staff."), status_code=303)
    if not available(parish):
        return None, None, RedirectResponse(_staff_url(error="Onboarding is not turned on for your diocese yet."), status_code=303)
    return user, parish, None


@router.get("/timekeeping/roster/onboarding", response_class=HTMLResponse)
def onboarding_form(request: Request):
    user, parish, err = _gate(request)
    if err:
        return err
    values = {"is_clergy": "no", "housing_provided": ""}
    raw = request.query_params.get("change_id")
    if raw and raw.isdigit():
        ch = db.query_one(
            "SELECT * FROM portal.staff_roster_changes WHERE id = %s AND parish_id = %s AND change_type = 'add'",
            (int(raw), parish["id"]))
        if ch:
            values.update({"first_name": ch["proposed_first_name"], "last_name": ch["proposed_last_name"],
                           "position": ch["proposed_position"],
                           "start_date": ch["as_of_date"].isoformat() if ch.get("as_of_date") else ""})
    for k in ("first_name", "last_name", "position", "start_date", "hours_per_week", "is_clergy", "housing_provided", "notes"):
        if request.query_params.get(k):
            values[k] = request.query_params.get(k)
    return _render(request, "parish_onboarding.html", user, {
        "parish": parish, "v": values, "error": request.query_params.get("error"),
        "today": dt.date.today().isoformat()})


@router.post("/timekeeping/roster/onboarding")
async def onboarding_send(request: Request):
    user, parish, err = _gate(request)
    if err:
        return err
    form = await request.form()
    raw = {
        "first_name": form.get("first_name"), "last_name": form.get("last_name"), "position": form.get("position"),
        "is_clergy": form.get("is_clergy") or "no", "housing_provided": form.get("housing_provided") or "",
        "hours_per_week": (form.get("hours_per_week") or "").strip() or None,
        "start_date": (form.get("start_date") or "").strip() or None,
        "notes": (form.get("notes") or "").strip() or None,
        # the employing unit is always this parish; the diocese is always the parish's own
        "parish_id": parish["id"],
    }
    try:
        case_id = cases.open_case(parish["org_id"], "benefits_enroll", raw, user["id"], source="parish")
    except HrError as exc:
        q = "&".join(f"{k}={quote(str(form.get(k) or ''))}" for k in
                     ("first_name", "last_name", "position", "start_date", "hours_per_week", "is_clergy", "housing_provided", "notes"))
        return RedirectResponse(f"/timekeeping/roster/onboarding?{q}&error={quote(exc.message)}", status_code=303)
    number = db.query_one("SELECT case_number FROM hr.cases WHERE id = %s", (case_id,))
    name = f"{(raw['first_name'] or '').strip()} {(raw['last_name'] or '').strip()}".strip()
    return RedirectResponse(_staff_url(onboarding_sent=name, case=(number or {}).get("case_number")), status_code=303)
