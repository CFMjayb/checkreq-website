"""
parish_access.py — self-service parish access-request flow (Parish Portal
S3, mirrors access_requests.py's exact shape/register() pattern).

Same "new feature area = new file" convention as access_requests.py/
admin_users.py — registered from main.py with a single register() call
(thin wiring only, per the standing rule against growing main.py).

The flow, mirroring the entity-level one: a user requests a role at a
specific PARISH (not an entity) via /parish-access-request; anyone holding
beacon_admin (diocese-wide, same reviewer as the entity flow -- see
parish_roles.py's module docstring for why a parish's own future
parish_admin reviewing their own parish isn't built yet) reviews the queue
at /admin/parish-access-requests.

Deliberately does NOT gate on "already has a live entity role" the way
access_requests.py's own submit route checks user_has_any_role -- parish
access is a completely separate grant system (portal.parish_user_roles),
so someone could legitimately hold zero entity roles but still want parish
access (a parish volunteer with no Beacon/checkreq footprint at all), or
already hold an entity role AND want parish access too (diocesan staff who
also need to view a specific parish). The only real guard is "no duplicate
pending request for the same thing," mirrored from the entity-level flow.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
import rbac
import registry
import parish_roles

router = APIRouter()

_current_user = None
_render = None


def register(app, *, current_user, render) -> None:
    global _current_user, _render
    _current_user, _render = current_user, render
    app.include_router(router)


def _require_beacon_admin(request: Request):
    user = _current_user(request)
    if not user:
        return None, RedirectResponse("/login")
    if not rbac.user_has_role(user["id"], "beacon_admin", org_id=None):
        return None, JSONResponse({"error": "Beacon Admin access required"}, status_code=403)
    return user, None


@router.get("/parish-access-request", response_class=HTMLResponse)
def parish_access_request_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    pending = parish_roles.get_pending_parish_access_request(user["id"])
    parishes = registry.list_all_parishes()
    roles = parish_roles.all_parish_roles()
    return _render(request, "parish_access_request.html", user, {
        "pending": pending,
        "parishes": parishes,
        "roles": roles,
    })


@router.post("/parish-access-request")
async def parish_access_request_submit(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    if parish_roles.get_pending_parish_access_request(user["id"]):
        return RedirectResponse("/parish-access-request", status_code=303)

    form = await request.form()
    try:
        parish_id = int(form.get("parish_id") or 0)
    except (TypeError, ValueError):
        parish_id = 0
    role_key = (form.get("role_key") or "").strip()
    note = (form.get("note") or "").strip() or None

    parish = db.query_one("SELECT id FROM portal.parishes WHERE id = %s AND is_active", (parish_id,))
    role = db.query_one("SELECT key FROM portal.parish_roles WHERE key = %s AND is_active", (role_key,))
    if not parish or not role:
        return _render(request, "parish_access_request.html", user, {
            "pending": None,
            "parishes": registry.list_all_parishes(),
            "roles": parish_roles.all_parish_roles(),
            "error": "Pick a valid parish and role.",
        })

    parish_roles.create_parish_access_request(user["id"], parish_id, role_key, note)
    return RedirectResponse("/parish-access-request", status_code=303)


@router.get("/admin/parish-access-requests", response_class=HTMLResponse)
def admin_parish_access_requests_page(request: Request, entity: str = ""):
    """Cross-entity by design, same as /admin/access-requests -- any
       beacon_admin reviews any diocese's parish requests."""
    user, err = _require_beacon_admin(request)
    if err:
        return err
    requests_ = parish_roles.list_pending_parish_access_requests()
    if entity:
        requests_ = [r for r in requests_ if r["org_code"] == entity]
    all_orgs_list = db.query("SELECT code, name FROM checkreq.organizations WHERE is_active ORDER BY name")
    return _render(request, "admin_parish_access_requests.html", user, {
        "requests": requests_, "all_orgs_list": all_orgs_list, "filter_entity": entity,
    })


@router.post("/admin/parish-access-requests/{request_id}/approve")
async def admin_parish_access_request_approve(request_id: int, request: Request):
    user, err = _require_beacon_admin(request)
    if err:
        return err
    form = await request.form()
    note = (form.get("note") or "").strip() or None
    try:
        parish_roles.approve_parish_access_request(request_id, user["id"], note)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return RedirectResponse("/admin/parish-access-requests?approved=1", status_code=303)


@router.post("/admin/parish-access-requests/{request_id}/reject")
async def admin_parish_access_request_reject(request_id: int, request: Request):
    user, err = _require_beacon_admin(request)
    if err:
        return err
    form = await request.form()
    note = (form.get("note") or "").strip() or None
    parish_roles.reject_parish_access_request(request_id, user["id"], note)
    return RedirectResponse("/admin/parish-access-requests?rejected=1", status_code=303)
