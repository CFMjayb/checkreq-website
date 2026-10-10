"""
payroll_screens.py -- the hr_admin screens for emailed and period-total hours (26-158, plan B3),
plus the parish's read-only view of what the diocese recorded.

New file, per the standing rule: the existing timekeeping screens gain only wiring (one include on
the backfill grid, extra columns on the status board). All rules live in payroll_totals.py.

ROUTES (all hr_admin or beacon_admin at the diocese, except the parish view):
  POST /admin/timekeeping/status/{period}/{parish}/totals/accept-all     accept every plain line
  POST /admin/timekeeping/status/{period}/{parish}/totals/{line}/{act}   accept, reject, edit, ...
  POST /admin/timekeeping/status/{period}/standing                       carry forward standing hours
  POST /admin/timekeeping/status/{period}/final | unfinal                mark the hours Final
  GET  /admin/timekeeping/status/{period}/variance                       Beacon vs the paid register
  GET/POST /admin/timekeeping/parish-profile/{parish}[/save|/sender-add|/sender-remove]
  GET  /timekeeping/received                                             the parish's read-only view

Register this BEFORE timekeeping_status: its literal /variance, /final, /standing paths must be
matched before the generic /status/{period}/{parish} route.
"""
from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import cornerstone_mode
import db
import org_time
import payroll_totals as pt
import rbac
import registry
import timekeeping

router = APIRouter()

_current_user = None
_current_org = None
_render = None

PATTERN_LABELS = {
    "every_period": "Every period",
    "most_periods": "Most periods",
    "two_period_batches": "Two periods at a time",
    "standing_hours": "Standing hours (the same each period)",
    "occasional": "Occasionally",
    "none": "No hours expected",
}
NOT_EXPECTED_PATTERNS = ("standing_hours", "none")
CHANNEL_LABELS = {"email": "Email", "parish": "Parish (in Beacon)", "diocese": "Diocese", "standing": "Standing hours"}
STATE_LABELS = {
    "ok": "OK", "needs_review": "Needs review", "proposal": "Different figure waiting",
    "conflict": "Conflict with the daily grid", "pending_hire": "New hire not on the roster yet",
}


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


def _require_hr_admin(request: Request):
    """(user, org, None) when allowed, (None, None, response) when not. Same rule as every other
    diocese-wide Timekeeping screen: hr_admin or beacon_admin, never a Cornerstone-Mode entity."""
    user = _current_user(request)
    if not user:
        return None, None, RedirectResponse("/login")
    org = _current_org(request)
    org_id = org["id"] if org else None
    if org_id is None or cornerstone_mode.is_cornerstone_org(org_id):
        return None, None, RedirectResponse("/portal")
    if not rbac.user_has_any_role(user["id"], ["hr_admin", "beacon_admin"], org_id=org_id):
        return None, None, _forbidden()
    return user, org, None


def _forbidden():
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "HR Admin access required"}, status_code=403)


def _back(period_id: int, parish_id: int, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    q = ""
    if msg:
        q = "?pmsg=" + quote_plus(msg)
    elif err:
        q = "?perr=" + quote_plus(err)
    return RedirectResponse(f"/admin/timekeeping/status/{period_id}/{parish_id}{q}", status_code=303)


def _status_back(period_id: int, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    q = ""
    if msg:
        q = "?saved=" + quote_plus(msg)
    elif err:
        q = "?error=" + quote_plus(err)
    return RedirectResponse(f"/admin/timekeeping/status/{period_id}{q}", status_code=303)


# ---------------------------------------------- helpers the existing screens call

def section_context(org_id: int, period: dict, parish_id: int) -> dict:
    """What _payroll_totals_section.html shows on the diocese's backfill grid for one parish."""
    rows = [r for r in pt.period_rows(org_id, period["id"]) if r["parish_id"] == parish_id and r["line_id"] is not None]
    for r in rows:
        r["message_url"] = pt.message_link(r["source_ref"])
        r["proposed_url"] = pt.message_link(r["proposed_ref"])
        r["state_label"] = STATE_LABELS.get(r["state"], r["state"])
    return {"rows": rows, "writable": period["status"] in pt.WRITABLE_PERIOD_STATUSES,
            "parish_id": parish_id, "period_id": period["id"]}


def board_rows(org_id: int, period: dict, rows: list[dict]) -> list[dict]:
    """Adds the payroll columns to the status-board rows (in place, and returned)."""
    extras = pt.board_extras(org_id, period["id"])
    zone = org_time.zone_name_for_org(org_id)
    for r in rows:
        e = extras.get(r["parish_id"], {})
        r["pattern"] = e.get("pattern")
        r["pattern_label"] = PATTERN_LABELS.get(e.get("pattern"))
        r["channel"] = e.get("channel")
        r["channel_label"] = CHANNEL_LABELS.get(e.get("channel"), "")
        r["emails"] = e.get("emails", 0)
        r["first_received"] = org_time.format_local(e.get("first_received_at"), zone) if e.get("first_received_at") else None
        r["needs_review"] = e.get("needs_review", 0) + e.get("pending_hires", 0)
        r["conflicts"] = e.get("conflicts", 0)
        r["proposals"] = e.get("proposals", 0)
        r["not_expected"] = (e.get("pattern") in NOT_EXPECTED_PATTERNS and not r.get("submission_id"))
        r["by_email"] = (e.get("channel") == "email" and r.get("submission_status") != "submitted")
    return rows


def board_summary(org_id: int, period: dict) -> dict:
    rows = pt.period_rows(org_id, period["id"])
    return {"left": len(pt.unresolved(rows)), "final": bool(period.get("hours_finalized_at")),
            "final_at": org_time.format_local(period.get("hours_finalized_at"), org_time.zone_name_for_org(org_id))
            if period.get("hours_finalized_at") else None}


# ------------------------------------------------------------------ review actions

@router.post("/admin/timekeeping/status/{period_id}/{parish_id}/totals/accept-all")
async def totals_accept_all(period_id: int, parish_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    try:
        res = pt.accept_all(org["id"], period_id, parish_id, user["id"])
    except pt.PayrollError as e:
        return _back(period_id, parish_id, err=e.message)
    msg = f"Accepted {res['accepted']} line(s)."
    if res["skipped"]:
        msg += f" {res['skipped']} need a look on their own (flagged, a different figure waiting, or a conflict)."
    return _back(period_id, parish_id, msg=msg)


@router.post("/admin/timekeeping/status/{period_id}/{parish_id}/totals/{line_id}/{action}")
async def totals_line_action(period_id: int, parish_id: int, line_id: int, action: str, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    form = await request.form()
    try:
        pt.review_line(org["id"], line_id, action, user["id"], hours=form.get("hours"), note=form.get("note"))
    except pt.PayrollError as e:
        return _back(period_id, parish_id, err=e.message)
    return _back(period_id, parish_id, msg="Saved.")


@router.post("/admin/timekeeping/status/{period_id}/standing")
async def standing_carry_forward(period_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    try:
        res = pt.carry_forward_standing(org["id"], period_id, user["id"])
    except pt.PayrollError as e:
        return _status_back(period_id, err=e.message)
    return _status_back(period_id, msg=(
        f"Standing hours: {res['recorded']} line(s) recorded, {res['confirmed_automatically']} confirmed "
        f"automatically, {res['unchanged']} already there."))


@router.post("/admin/timekeeping/status/{period_id}/final")
async def mark_final(period_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    try:
        pt.finalize_period(org["id"], period_id, user["id"])
    except pt.PayrollError as e:
        return _status_back(period_id, err=e.message)
    return _status_back(period_id, msg="The hours are marked Final for Checkwriters.")


@router.post("/admin/timekeeping/status/{period_id}/unfinal")
async def mark_not_final(period_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    try:
        pt.unfinalize_period(org["id"], period_id)
    except pt.PayrollError as e:
        return _status_back(period_id, err=e.message)
    return _status_back(period_id, msg="The hours are no longer marked Final.")


# ------------------------------------------------------------------ variance report

@router.get("/admin/timekeeping/status/{period_id}/variance", response_class=HTMLResponse)
def variance_page(period_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    try:
        period = pt.get_period(org["id"], period_id)
        report = pt.variance(org["id"], period_id)
    except pt.PayrollError as e:
        return _status_back(period_id, err=e.message)
    return _render(request, "timekeeping_variance.html", user,
                   {"current_org": org, "period": period, "report": report})


# ------------------------------------------------------------------ parish profile

def _profile_ctx(org: dict, parish: dict) -> dict:
    prof = db.query_one("SELECT * FROM portal.parish_payroll_profile WHERE parish_id = %s", (parish["id"],))
    senders = pt.get_senders(org["id"], parish.get("code"))
    mine = senders[0] if senders else {"time_submitters": [], "report_recipients": []}
    return {"current_org": org, "parish": parish, "profile": prof, "patterns": PATTERN_LABELS,
            "submitters": mine["time_submitters"], "recipients": mine["report_recipients"]}


@router.get("/admin/timekeeping/parish-profile/{parish_id}", response_class=HTMLResponse)
def profile_page(parish_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    parish = registry.get_parish(parish_id, org["id"])
    if not parish:
        return RedirectResponse("/admin/timekeeping/status?error=That+parish+was+not+found.", status_code=303)
    return _render(request, "timekeeping_parish_profile.html", user, _profile_ctx(org, parish))


@router.post("/admin/timekeeping/parish-profile/{parish_id}/save")
async def profile_save(parish_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    form = await request.form()
    try:
        pt.set_profile(org["id"], parish_id=parish_id, pattern=str(form.get("pattern") or ""),
                       notes=form.get("notes"), user_id=user["id"])
    except pt.PayrollError as e:
        return RedirectResponse(f"/admin/timekeeping/parish-profile/{parish_id}?error={quote_plus(e.message)}", status_code=303)
    return RedirectResponse(f"/admin/timekeeping/parish-profile/{parish_id}?saved=1", status_code=303)


@router.post("/admin/timekeeping/parish-profile/{parish_id}/sender-add")
async def profile_sender_add(parish_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    parish = registry.get_parish(parish_id, org["id"])
    form = await request.form()
    try:
        if not parish or not parish.get("code"):
            raise pt.PayrollError("not_found", "That parish was not found.")
        pt.add_learned_sender(org["id"], parish_code=parish["code"], email=str(form.get("email") or ""),
                              name=form.get("name"), source="manual")
    except pt.PayrollError as e:
        return RedirectResponse(f"/admin/timekeeping/parish-profile/{parish_id}?error={quote_plus(e.message)}", status_code=303)
    return RedirectResponse(f"/admin/timekeeping/parish-profile/{parish_id}?saved=1", status_code=303)


@router.post("/admin/timekeeping/parish-profile/{parish_id}/sender-remove")
async def profile_sender_remove(parish_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    parish = registry.get_parish(parish_id, org["id"])
    form = await request.form()
    if parish and parish.get("code"):
        pt.remove_sender(org["id"], parish_code=parish["code"], email=str(form.get("email") or ""))
    return RedirectResponse(f"/admin/timekeeping/parish-profile/{parish_id}?saved=1", status_code=303)


# ------------------------------------------------------------ the parish's read-only view

@router.get("/timekeeping/received", response_class=HTMLResponse)
def received_page(request: Request):
    """What the diocese recorded for THIS parish from its emails, for the current pay period and any
    later one that already has hours. The parish is always the viewer's own (never a parameter)."""
    user, parish, diocese_org, err = timekeeping.timekeeping_context(request)
    if err:
        return err
    periods = []
    cur = timekeeping.get_current_open_period(diocese_org["id"])
    if cur:
        periods.append(cur)
    for p in pt.list_periods(diocese_org["id"]):
        if p["status"] == "future" and (not cur or p["period_start"] > cur["period_start"]):
            periods.append(p)
    blocks = []
    for p in periods:
        rows = pt.parish_view(diocese_org["id"], parish["id"], p["id"])
        if rows or (cur and p["id"] == cur["id"]):
            for r in rows:
                r["state_label"] = ("Recorded" if r["state"] == "ok" else "Waiting for the diocese to review")
            blocks.append({"period": p, "rows": rows})
    return _render(request, "timekeeping_received.html", user, {"parish": parish, "blocks": blocks})
