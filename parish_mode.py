"""
parish_mode.py — Parish Portal S4, "Diocese Mode / Parish Mode."

Jay, 2026-08-08: "I am also expecting that when someone like me logs in
that there will be a toggle somewhere for 'Diocese mode' / 'Parish Mode'.
The Parish Mode is like the Impersonate User in that an admin will be
able to see what a specific user or Parish sees." The "specific user" half
already exists (Impersonate a User, main.py's impersonate_start/_stop) --
this module is the "specific Parish" half.

Three design decisions confirmed with Jay via AskUserQuestion before
building any of this:
  1. Gated on the REAL identity's `cfo` role — the exact same check and
     re-verify-live-on-every-request discipline as Impersonate a User
     (main.py's impersonate_start/_current_user), not a new "beacon_admin"
     concept. This module deliberately duplicates that one small check
     rather than importing main.py (main.py imports THIS module, so the
     reverse would be circular) — the same accepted duplication
     admin_hub.py's own _ADMIN_TASK_ROLE_KEYS comment already documents.
  2. A new table, portal.parish_mode_log (migrations/026), mirroring
     checkreq.impersonation_log's shape — NOT a nullable column bolted onto
     that table. Same reasoning as parish_roles.py's own separate-grant-
     system decision: this codebase has been burned twice by a nullable
     "also applies to X" column (global_approvers.org_id, then the
     cross-entity CFO-notification bug it caused).
  3. Scope for THIS pass: the toggle mechanism itself, landing on a real
     but deliberately minimal placeholder page (parish name/entity/served
     tier, "coming soon" for congregational/clergy content) — not the real
     parish-facing portal content, which is a separate, larger design not
     yet scoped (S4's "portal shell" proper).

UNLIKE impersonation, Parish Mode does NOT change what _current_user()
returns anywhere in the app — a CFO in Parish Mode is still themselves,
everywhere, for every other route. It only unlocks one additional page
(/parish-view) and injects one read-only context value (`parish_view`) that
main.py's _render() passes to every template, purely so a persistent banner
can show while it's active (mirroring base.html's existing impersonation
bar) — deliberately the smallest possible footprint, since a parish has no
single "identity" to become the way a user does.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
import rbac
import registry

router = APIRouter()

_current_user = None
_render = None


def register(app, *, current_user, render) -> None:
    global _current_user, _render
    _current_user, _render = current_user, render
    app.include_router(router)


def _require_real_cfo(request: Request):
    """Mirrors main.py's impersonate_start/_picker exactly: gated on the
    REAL session identity (request.session['user_id'] directly, bypassing
    any impersonation already in effect — a non-CFO impersonated persona
    must not be able to reach this any more than they can chain-
    impersonate), re-checked live every call, never trusted from a cached
    session flag."""
    real_id = request.session.get("user_id")
    if not real_id:
        return None, RedirectResponse("/login")
    if not rbac.user_has_role(real_id, "cfo", org_id=None):
        return None, JSONResponse({"error": "CFO access required"}, status_code=403)
    return real_id, None


def _close_open_parish_mode(real_user_id: int) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.parish_mode_log SET ended_at = NOW() "
                "WHERE real_user_id = %s AND ended_at IS NULL",
                (real_user_id,),
            )


def current_parish_view(request: Request) -> dict | None:
    """The one thing main.py's _render() calls, every render, every route --
    same fail-closed discipline as main.py's own _current_user(): if the
    real identity has lost cfo since parish_view_id was set (role revoked
    mid-session), silently drop back to no parish view and close the log
    row, rather than trusting the session flag alone."""
    real_id = request.session.get("user_id")
    parish_id = request.session.get("parish_view_id")
    if not real_id or not parish_id:
        return None
    if not rbac.user_has_role(real_id, "cfo", org_id=None):
        request.session.pop("parish_view_id", None)
        _close_open_parish_mode(real_id)
        return None
    parish = db.query_one(
        "SELECT p.*, o.code AS org_code, o.name AS org_name "
        "FROM portal.parishes p JOIN checkreq.organizations o ON o.id = p.org_id "
        "WHERE p.id = %s",
        (parish_id,),
    )
    if not parish:
        request.session.pop("parish_view_id", None)
        _close_open_parish_mode(real_id)
        return None
    return parish


@router.get("/admin/parish-mode", response_class=HTMLResponse)
def parish_mode_picker(request: Request):
    real_id, err = _require_real_cfo(request)
    if err:
        return err
    return _render(request, "parish_mode.html", _current_user(request), {
        "parishes": registry.list_all_parishes(),
    })


@router.post("/admin/parish-mode/stop")
def parish_mode_stop(request: Request):
    real_id = request.session.get("user_id")
    if real_id:
        _close_open_parish_mode(real_id)
    request.session.pop("parish_view_id", None)
    return RedirectResponse("/portal", status_code=303)


@router.post("/admin/parish-mode/{parish_id}")
def parish_mode_start(parish_id: int, request: Request):
    real_id, err = _require_real_cfo(request)
    if err:
        return err

    target = db.query_one("SELECT id FROM portal.parishes WHERE id = %s AND is_active", (parish_id,))
    if not target:
        return RedirectResponse("/admin/parish-mode")

    _close_open_parish_mode(real_id)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.parish_mode_log (real_user_id, parish_id) VALUES (%s, %s)",
                (real_id, parish_id),
            )
    request.session["parish_view_id"] = parish_id
    return RedirectResponse("/parish-view", status_code=303)


@router.get("/parish-view", response_class=HTMLResponse)
def parish_view_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    parish = current_parish_view(request)
    if not parish:
        return RedirectResponse("/admin/parish-mode")
    return _render(request, "parish_view.html", user, {"parish": parish})
