"""
parish_access.py — self-service parish access-request flow (Parish Portal
S3, mirrors access_requests.py's exact shape/register() pattern).

Same "new feature area = new file" convention as access_requests.py/
admin_users.py — registered from main.py with a single register() call
(thin wiring only, per the standing rule against growing main.py).

The flow, mirroring the entity-level one: a user requests a role at a
specific PARISH (not an entity) via /parish-access-request; the queue at
/admin/parish-access-requests is reviewed by TWO different kinds of people
(Jay, 2026-08-08: "The Parish Admin will have to grant access to someone
who requests it. We (a Beacon Admin, Diocesan Admin) can also grant
permissions, as needed"):
  - anyone holding checkreq.roles' beacon_admin, in ANY entity -- this
    already covers both "Beacon Admin" (granted across every org) and
    "Diocesan Admin" (granted for just one org/diocese) in Jay's own
    vocabulary, since checkreq.user_roles has no separate "diocesan_admin"
    role -- an org-scoped beacon_admin grant already IS what he means by
    that. Sees and can act on EVERY pending request, cross-diocese, same as
    the entity-level flow.
  - anyone holding portal.parish_user_roles' parish_admin, for the SPECIFIC
    parish a request names -- sees and can act ONLY on requests for the
    parish(es) they administer, never another parish's queue. A pure Parish
    Admin (no checkreq.roles grant at all -- a parish volunteer promoted to
    administer their own parish, nothing diocesan) can reach this page and
    act on their own parish's requests without needing any entity role.

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


def _is_beacon_admin(user: dict) -> bool:
    return rbac.user_has_role(user["id"], "beacon_admin", org_id=None)


def _require_parish_reviewer(request: Request):
    """Either kind of reviewer (see module docstring) may reach the queue
    and list route -- a beacon_admin sees everything, a parish_admin-only
    user sees (and, in the approve/reject routes below, may only act on)
    the parish(es) they hold parish_admin for. Returns (user, None) on
    success; a bare 403 here means "not a reviewer of any kind," not "not a
    reviewer of THIS request" -- that second, per-request check lives in
    the approve/reject routes themselves, since it needs the request's own
    parish_id first."""
    user = _current_user(request)
    if not user:
        return None, RedirectResponse("/login")
    if _is_beacon_admin(user):
        return user, None
    if parish_roles.get_parish_ids_with_role(user["id"], "parish_admin"):
        return user, None
    return None, JSONResponse({"error": "Beacon Admin or Parish Admin access required"}, status_code=403)


def _native_parish_ids(user_id: int) -> list[int]:
    """Every distinct parish this user holds ANY parish role at -- same
    small duplication as parish_mode.py's own private helper of the same
    name (that module's docstring already documents why this codebase
    accepts the duplication rather than a cross-module import cycle)."""
    rows = db.query(
        "SELECT DISTINCT parish_id FROM portal.parish_user_roles "
        "WHERE user_id = %s AND revoked_at IS NULL",
        (user_id,),
    )
    return [r["parish_id"] for r in rows]


def _request_form_context(user: dict, error: str | None = None) -> dict:
    """Shared by the GET page and a failed POST's re-render. Jay, 2026-08-08:
    "When a person logs in to a Parish, they should only be able to request
    access for someone in their Parish -- the Church should be pre-filled
    for that parish and not be editable." A NATIVE parish-only user (holds
    a parish role somewhere, but no checkreq.roles grant anywhere -- i.e.
    not diocesan staff) gets the parish field locked to their own parish(es)
    rather than the full 95-parish list; diocesan staff and a genuine
    first-timer (zero native parishes yet) keep the full picker, since
    staff legitimately need to request access to any parish, and a
    first-timer has no "own parish" yet to lock to."""
    is_staff = rbac.user_has_any_role(user["id"])
    own_ids = [] if is_staff else _native_parish_ids(user["id"])
    if own_ids:
        parishes = [p for p in registry.list_all_parishes() if p["id"] in own_ids]
        locked = len(own_ids) == 1
    else:
        parishes = registry.list_all_parishes()
        locked = False
    ctx = {
        "parishes": parishes,
        "roles": parish_roles.all_parish_roles(),
        "locked_parish": locked,
        "error": error,
    }
    return ctx


@router.get("/parish-access-request", response_class=HTMLResponse)
def parish_access_request_page(request: Request, entity: str = ""):
    """Consolidated per Jay's direct feedback, 2026-08-08: "Request Access
    replaces Request Parish Access and Parish Access Requests -- the
    sub-screen should do all of this, depending on your RBAC." One page:
    the submit form (everyone), plus an inline review queue for anyone who
    qualifies as a reviewer (beacon_admin, or parish_admin somewhere)."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    pending = parish_roles.get_pending_parish_access_request(user["id"])
    ctx = {"pending": pending, **_request_form_context(user)}

    is_admin = _is_beacon_admin(user)
    parish_admin_ids = parish_roles.get_parish_ids_with_role(user["id"], "parish_admin")
    if is_admin or parish_admin_ids:
        scoped_parish_ids = None if is_admin else parish_admin_ids
        requests_ = parish_roles.list_pending_parish_access_requests(scoped_parish_ids)
        if entity:
            requests_ = [r for r in requests_ if r["org_code"] == entity]
        all_orgs_list = db.query("SELECT code, name FROM checkreq.organizations WHERE is_active ORDER BY name")
        ctx.update({
            "is_reviewer": True,
            "requests": requests_,
            "all_orgs_list": all_orgs_list,
            "filter_entity": entity,
            "is_beacon_admin_reviewer": is_admin,
        })
    else:
        ctx["is_reviewer"] = False

    return _render(request, "parish_access_request.html", user, ctx)


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

    # Re-check the same own-parish restriction server-side -- the locked
    # field in the form is a UI convenience, never trusted on its own.
    form_ctx = _request_form_context(user)
    allowed_ids = {p["id"] for p in form_ctx["parishes"]}
    parish = db.query_one("SELECT id FROM portal.parishes WHERE id = %s AND is_active", (parish_id,))
    role = db.query_one("SELECT key FROM portal.parish_roles WHERE key = %s AND is_active", (role_key,))
    if not parish or not role or parish_id not in allowed_ids:
        return _render(request, "parish_access_request.html", user, {
            "pending": None,
            **_request_form_context(user, error="Pick a valid parish and role."),
        })

    parish_roles.create_parish_access_request(user["id"], parish_id, role_key, note)
    return RedirectResponse("/parish-access-request", status_code=303)


@router.get("/admin/parish-access-requests")
def admin_parish_access_requests_redirect():
    """Consolidated into /parish-access-request, 2026-08-08 -- kept as a
    redirect so no old link/bookmark 404s."""
    return RedirectResponse("/parish-access-request", status_code=308)


def _authorize_for_request(user: dict, request_id: int):
    """Shared by approve/reject below: a beacon_admin may act on anything;
       a parish_admin-only reviewer may act ONLY on a request whose own
       parish_id matches one they hold parish_admin for -- checked against
       the request's real parish_id, never just "administers some parish
       somewhere." Returns None on success, or the error response to
       return immediately."""
    if _is_beacon_admin(user):
        return None
    req = parish_roles.get_parish_access_request(request_id)
    if not req or not parish_roles.user_has_parish_role(user["id"], "parish_admin", req["parish_id"]):
        return JSONResponse({"error": "You don't administer this request's parish."}, status_code=403)
    return None


@router.post("/admin/parish-access-requests/{request_id}/approve")
async def admin_parish_access_request_approve(request_id: int, request: Request):
    user, err = _require_parish_reviewer(request)
    if err:
        return err
    scope_err = _authorize_for_request(user, request_id)
    if scope_err:
        return scope_err
    form = await request.form()
    note = (form.get("note") or "").strip() or None
    try:
        parish_roles.approve_parish_access_request(request_id, user["id"], note)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return RedirectResponse("/parish-access-request?approved=1", status_code=303)


@router.post("/admin/parish-access-requests/{request_id}/reject")
async def admin_parish_access_request_reject(request_id: int, request: Request):
    user, err = _require_parish_reviewer(request)
    if err:
        return err
    scope_err = _authorize_for_request(user, request_id)
    if scope_err:
        return scope_err
    form = await request.form()
    note = (form.get("note") or "").strip() or None
    parish_roles.reject_parish_access_request(request_id, user["id"], note)
    return RedirectResponse("/parish-access-request?rejected=1", status_code=303)
