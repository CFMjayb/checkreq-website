"""
access_requests.py — self-service access-request flow (RBAC, Plan §9).

NOT YET WIRED IN. This module is code-complete and reviewable, but main.py
does not import or register it, and checkreq.access_requests /
checkreq.user_roles / checkreq.roles do not exist in production yet
(migrations/019_rbac.sql). Wiring this in (a two-line register() call in
main.py, same pattern as admin_setup.py) is a Stage 7 step (Role-Based
Access Control Plan.md §7) that happens only after Jay runs the migration
and Stage 0-2 are verified against real data.

The flow, per the plan: a user who authenticates successfully but holds no
live role (rbac.user_has_any_role returns False) is routed here instead of
/portal. They see a short explanation and a Request Access form (role +
single entity + note) rather than an empty portal. Submitting creates one
row in checkreq.access_requests; reloading while pending shows that status
instead of the form again. Anyone holding beacon_admin (any entity —
approving an access request for Entity X must not require holding
beacon_admin FOR X specifically) reviews the queue at /admin/access-requests
and approves (grants the role directly via rbac.grant_role) or rejects
(the requester can then resubmit).

Kept as its own small module rather than folded into main.py or
admin_setup.py, per the project's standing "new feature area = new file"
convention (see feedback_modular_file_organization.md) — this is a distinct
concern from both the core request-submission routes and the setup-table
admin screens.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
import rbac

router = APIRouter()

# main.py owns the identity/entity/render helpers; register() below injects
# them so this module never imports main (which will import this one).
_current_user = None
_render = None


def register(app, *, current_user, render) -> None:
    global _current_user, _render
    _current_user, _render = current_user, render
    app.include_router(router)


def _require_beacon_admin(request: Request):
    """(user, None) when allowed, (None, response) when not. Cross-entity by
       design (org_id=None) -- Plan §9 point 6: reviewing an access request
       for Entity X must not require holding beacon_admin FOR X
       specifically; one small admin group covers every entity."""
    user = _current_user(request)
    if not user:
        return None, RedirectResponse("/login")
    if not rbac.user_has_role(user["id"], "beacon_admin", org_id=None):
        return None, JSONResponse({"error": "Beacon Admin access required"}, status_code=403)
    return user, None


@router.get("/access-request", response_class=HTMLResponse)
def access_request_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    pending = rbac.get_pending_access_request(user["id"])
    orgs = db.query("SELECT id, code, name FROM checkreq.organizations WHERE is_active ORDER BY name")
    roles = rbac.all_roles()
    return _render(request, "access_request.html", user, {
        "pending": pending,
        "orgs": orgs,
        "roles": roles,
    })


@router.post("/access-request")
async def access_request_submit(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    # A user who already has a live role, or an existing pending request,
    # should not be able to queue a second one from a stale form submit.
    if rbac.user_has_any_role(user["id"]):
        return RedirectResponse("/portal", status_code=303)
    if rbac.get_pending_access_request(user["id"]):
        return RedirectResponse("/access-request", status_code=303)

    form = await request.form()
    try:
        org_id = int(form.get("org_id") or 0)
    except (TypeError, ValueError):
        org_id = 0
    role_key = (form.get("role_key") or "").strip()
    note = (form.get("note") or "").strip() or None

    org = db.query_one("SELECT id FROM checkreq.organizations WHERE id = %s AND is_active", (org_id,))
    role = db.query_one("SELECT key FROM checkreq.roles WHERE key = %s AND is_active", (role_key,))
    if not org or not role:
        pending = None
        orgs = db.query("SELECT id, code, name FROM checkreq.organizations WHERE is_active ORDER BY name")
        roles = rbac.all_roles()
        return _render(request, "access_request.html", user, {
            "pending": pending, "orgs": orgs, "roles": roles,
            "error": "Pick a valid entity and role.",
        })

    rbac.create_access_request(user["id"], org_id, role_key, note)
    return RedirectResponse("/access-request", status_code=303)


@router.get("/admin/access-requests", response_class=HTMLResponse)
def admin_access_requests_page(request: Request):
    user, err = _require_beacon_admin(request)
    if err:
        return err
    return _render(request, "admin_access_requests.html", user, {
        "requests": rbac.list_pending_access_requests(),
    })


@router.post("/admin/access-requests/{request_id}/approve")
async def admin_access_request_approve(request_id: int, request: Request):
    user, err = _require_beacon_admin(request)
    if err:
        return err
    form = await request.form()
    note = (form.get("note") or "").strip() or None
    try:
        rbac.approve_access_request(request_id, user["id"], note)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return RedirectResponse("/admin/access-requests?approved=1", status_code=303)


@router.post("/admin/access-requests/{request_id}/reject")
async def admin_access_request_reject(request_id: int, request: Request):
    user, err = _require_beacon_admin(request)
    if err:
        return err
    form = await request.form()
    note = (form.get("note") or "").strip() or None
    rbac.reject_access_request(request_id, user["id"], note)
    return RedirectResponse("/admin/access-requests?rejected=1", status_code=303)
