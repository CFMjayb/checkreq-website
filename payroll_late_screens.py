"""
payroll_late_screens.py -- the Late emails screen: Time Review > Actions > Late emails (26-158 / 26-129, 2026-10-27).
Plan: 26-158 DME Payroll Inbox Mining\\Plan - Late Email Queue.md. The rules and the actions are in payroll_late.py.

THE HARD RULE: hours only, never pay.

  GET  /admin/timekeeping/late                       the queue (waiting, or ?view=decided)
  POST /admin/timekeeping/late/dismiss-repeats       dismiss every email whose every line is already entered
  POST /admin/timekeeping/late/{id}/{action}         action = reopen | carry | manual | dismiss | dismiss-repeat | church

Gate: the same as every diocese-wide Time Review screen (HR Admin or Beacon Admin, never a Cornerstone-Mode entity). Every POST needs the
form token (csrf_guard checks it for every POST). Registered from payroll_screens.register, the way the other Time Review screens are, so
the fixed route /late sits before the generic /status/{period}/{parish} route.
"""
from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import db
import payroll_late as late
import payroll_late_rules as R
import payroll_totals as pt

router = APIRouter()

HOME = "/admin/timekeeping/late"

_render = None
_gate = None  # payroll_screens._require_hr_admin


def register(app, *, current_user, current_org, render, gate) -> None:
    global _render, _gate
    _render, _gate = render, gate
    app.include_router(router)


def _back(done: str | None = None, error: str | None = None) -> RedirectResponse:
    q = ("?done=" + quote_plus(done)) if done else (("?error=" + quote_plus(error)) if error else "")
    return RedirectResponse(HOME + q, status_code=303)


def _summary(action: str, out: dict) -> str:
    where = {"reopen": "in its pay period", "carry": "in the next pay period",
             "manual": "in its pay period, to be paid by manual check"}[action]
    n = out["recorded"]
    parts = [f"Recorded {n} line{'s' if n != 1 else ''} {where} (waiting for review)."]
    if out.get("repeats_skipped"):
        parts.append(f"{out['repeats_skipped']} already entered, skipped.")
    if out.get("not_entered_list"):
        parts.append(f"{out['not_entered_list']} person(s) are on the not-entered list.")
    if action == "manual":
        parts.append("Mark them Manual check on the Variance report.")
    if out.get("errors"):
        parts.append("Refused: " + "; ".join(out["errors"])[:200])
    return " ".join(parts)


@router.get(HOME, response_class=HTMLResponse)
def late_page(request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    view = "decided" if request.query_params.get("view") == "decided" else "waiting"
    items = late.list_items(org["id"], decided=(view == "decided"))
    parishes = []
    if view == "waiting" and any(i.get("needs_church") for i in items):
        parishes = db.query("SELECT id, code, COALESCE(short_name, name) AS name FROM portal.parishes "
                            "WHERE org_id = %s AND is_active AND code IS NOT NULL ORDER BY code", (org["id"],))
    return _render(request, "timekeeping_late.html", user, {
        "current_org": org, "view": view, "items": items, "parishes": parishes, "when": late._when_text,
        "waiting_n": R.waiting_count(org["id"]),
        "repeats_n": sum(1 for i in items if i.get("kind") == "hours" and i.get("all_repeats")) if view == "waiting" else 0,
        "done": request.query_params.get("done"), "error": request.query_params.get("error")})


@router.post(HOME + "/dismiss-repeats")
async def late_dismiss_repeats(request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    n = late.dismiss_repeats(org["id"], user["id"])["dismissed"]
    if n:
        return _back(done=f"Dismissed {n} email{'s' if n != 1 else ''} whose hours were already entered.")
    return _back(error="No email is a repeat of hours already entered.")


@router.post(HOME + "/{item_id}/{action}")
async def late_action(item_id: int, action: str, request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    form = await request.form()
    try:
        if action in late.MODES:
            return _back(done=_summary(action, late.record_item(org["id"], item_id, user["id"], action)))
        if action == "dismiss":
            late.dismiss(org["id"], item_id, user["id"], reason="not_hours")
            return _back(done="Dismissed as not hours.")
        if action == "dismiss-repeat":
            late.dismiss(org["id"], item_id, user["id"], reason="repeat")
            return _back(done="Dismissed: the hours were already entered.")
        if action == "church":
            try:
                parish_id = int(form.get("parish_id") or 0)
            except (TypeError, ValueError):
                parish_id = 0
            if not parish_id:
                return _back(error="Pick a department from the list.")
            out = late.pick_church(org["id"], item_id, parish_id, user["id"])
            return _back(done="Church picked: the people are on that department's not-entered list on Time Review."
                         if out.get("moved") else "Church picked. Now decide what to do with the hours.")
        return _back(error="Unknown action.")
    except pt.PayrollError as e:
        return _back(error=e.message)
