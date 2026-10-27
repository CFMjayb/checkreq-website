"""
hr_my_steps.py -- "My steps": the checklist steps assigned to the signed-in person (26-162, 2026-10-27).

Jay: a step can be assigned to anyone with a login at the entity (for EDOM and Claggett, anyone with an episcopalmaryland.org, edom.org
or claggettcenter.org login), and that person needs a way to see it. They usually hold no HR role, so they cannot open the Onboarding
screens; this page is their whole view, and it shows only what they need to do the step:

    GET  /hr/onboarding/my-steps                              the person's own open steps, soonest first
    POST /hr/onboarding/my-steps/{case_id}/{item_key}/done    mark one of THEIR steps done, with an optional note

Nothing else about the case is reachable from here: no coverage, no history, no documents, no personal details beyond the new hire's name,
the kind of case and the step. A step that is not assigned to the signed-in person is "not found". Marking it done is stamped with their
name and the time, exactly like any other person doing it. The user menu shows "My Steps" with a count while any are waiting (cached for
a minute per person, and it can never break a page).

New file per the standing rule: hr_cases.register() wires it (main.py only hands in the template engine).
"""
from __future__ import annotations

import time
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from logic.hr_lifecycle import cases
from logic.hr_lifecycle.errors import HrError

router = APIRouter()
_current_user = None
_render = None

NAV_TTL = 60
_nav_cache: dict[int, tuple[float, dict]] = {}
_PRIVATE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache", "X-Robots-Tag": "noindex, nofollow"}


def register(app, *, templates, current_user, render) -> None:
    global _current_user, _render
    _current_user, _render = current_user, render
    templates.env.globals["my_steps_nav"] = nav_summary      # used by base.html's user menu
    app.include_router(router)


def nav_summary(user) -> dict:
    """{"total": how many open steps are waiting on this person}. Cached per person; any failure means 'none' (a menu entry must
    never break a page)."""
    try:
        uid = user["id"] if user else None
        if not uid:
            return {"total": 0}
        hit = _nav_cache.get(uid)
        if hit and time.time() - hit[0] < NAV_TTL:
            return hit[1]
        out = {"total": cases.my_steps_count(uid)}
        _nav_cache[uid] = (time.time(), out)
        return out
    except Exception:
        return {"total": 0}


def _back(msg_key: str, msg: str) -> RedirectResponse:
    return RedirectResponse("/hr/onboarding/my-steps?" + urlencode({msg_key: msg}), status_code=303)


@router.get("/hr/onboarding/my-steps")
def my_steps_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    _nav_cache.pop(user["id"], None)           # the page is always live, so its menu count agrees with it
    steps = cases.my_steps(user["id"])
    resp = _render(request, "hr_my_steps.html", user, {"steps": steps})
    for k, v in _PRIVATE.items():
        resp.headers[k] = v
    return resp


@router.post("/hr/onboarding/my-steps/{case_id}/{item_key}/done")
async def done_post(request: Request, case_id: int, item_key: str):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    try:
        cases.complete_my_step(user["id"], case_id, item_key, form.get("note"))
    except HrError as e:
        return _back("error", e.message)
    _nav_cache.pop(user["id"], None)
    return _back("saved", "Marked done. Thank you.")
