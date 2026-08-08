"""
account.py — self-service "Set / Change Your Password" for an
already-signed-in Beacon user.

The one real UI caller `auth_password.set_password()` never had: since S1
(2026-08-07) built the emailed-code + opt-in-password universal login
fallback, the backend has been able to set a password, but no page ever
let a signed-in user actually reach that function. Flagged as a deferred
follow-up in every session since — closed here.

Per auth_password.py's own docstring, a password may ONLY be set or
changed from an already-authenticated session — there is no separate
password-reset flow (a successful emailed-code sign-in already re-proves
identity just as well). Requiring the current password before allowing a
change (when one already exists) is this route's own added precaution, not
something auth_password.py itself enforces — it stops a merely-still-open
browser session from silently taking over a password-based login.

New file, not folded into auth_routes.py — auth_routes.py is specifically
the SIGNED-OUT login surface (/login, /auth/*, /logout); this is
signed-in-only, matching the "new capability = new file" discipline every
other module here follows (access_requests.py, parish_access.py, ...).
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import auth_password

router = APIRouter()

_current_user = None
_render = None


def register(app, *, current_user, render) -> None:
    global _current_user, _render
    _current_user, _render = current_user, render
    app.include_router(router)


@router.get("/account/password", response_class=HTMLResponse)
def account_password_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    return _render(request, "account_password.html", user, {
        "has_password": auth_password.has_password(user["email"]),
    })


@router.post("/account/password", response_class=HTMLResponse)
async def account_password_submit(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    already_has_password = auth_password.has_password(user["email"])

    def _error(message: str):
        return _render(request, "account_password.html", user, {
            "has_password": already_has_password, "error": message,
        })

    form = await request.form()
    current_password = str(form.get("current_password", ""))
    new_password = str(form.get("new_password", ""))
    confirm_password = str(form.get("confirm_password", ""))

    if already_has_password and not auth_password.verify_password(user["email"], current_password):
        return _error("Your current password is incorrect.")
    if new_password != confirm_password:
        return _error("New password and confirmation don't match.")

    try:
        auth_password.set_password(user["id"], user["email"], new_password)
    except auth_password.WeakPasswordError as exc:
        return _error(str(exc))

    # has_password reflects the POST-action state (a password now exists,
    # so the next visit to this page must show the current_password field);
    # just_set is the PRE-action state, kept separate so the success banner
    # says "set" the first time and "updated" every time after -- collapsing
    # these into one flag (both True post-success) would always say
    # "updated," even on someone's very first password.
    return _render(request, "account_password.html", user, {
        "has_password": True, "success": True, "just_set": not already_has_password,
    })
