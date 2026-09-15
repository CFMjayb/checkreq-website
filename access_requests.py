"""
access_requests.py — self-service access-request flow (RBAC, Plan §9).

Live and wired in from main.py. A truly roleless user (rbac.user_has_any_role
False, no Program Area, no live parish role) is routed here instead of
/portal by main.py's own gate. They see a short explanation and a Request
Access form (role + entity + note) rather than an empty portal.

2026-09-15, Jay's Entity-vs-Parish login split changed this flow's real
audience: an Entity login now always holds at least ENTITY_BASE_ROLE
('entity_member', granted at Add User time from within the target entity,
or the first time any other role is granted) -- so it never reaches this
page via the roleless redirect above. This page's main visitor going forward
is instead a login that already has SOME footing asking for MORE (a portal
tile, "Request Access", gated on holding an Entity login -- see main.py's
`is_entity_login` synthetic pseudo-role) -- a real, functional role at an
entity it already belongs to, not a brand-new entity. _requestable_orgs()
enforces this: the entity picker (and the server-side check on submit) is
scoped to rbac.get_entity_org_ids(user_id) -- entities this login already
holds ANY live role at. Reaching a genuinely NEW entity is an admin action
(Add User from within that entity, or a direct role grant on the Users &
Roles detail page), never self-service. ENTITY_BASE_ROLE itself is excluded
from the requestable role list -- a login already has it or doesn't; it
can't be "requested."

Submitting creates one row in checkreq.access_requests; reloading while
pending shows that status instead of the form again. Anyone holding
beacon_admin (any entity — approving an access request for Entity X must
not require holding beacon_admin FOR X specifically) reviews the queue at
/admin/access-requests and approves (grants the role directly via
rbac.grant_role) or rejects (the requester can then resubmit).

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


def _requestable_orgs(user_id: int) -> list[dict]:
    """2026-09-15, Jay: "a user requests a role, they should only be able
       to select entities that they have the default role for." Reaching a
       BRAND NEW entity is an admin action (Add User, from within that
       entity, or granting a role directly on the Users & Roles detail
       page) -- self-service Request Access is for asking for MORE at an
       entity this login already has some footing in, never a way to cross
       into an entity it has zero connection to yet."""
    org_ids = rbac.get_entity_org_ids(user_id)
    if not org_ids:
        return []
    return db.query(
        "SELECT id, code, name FROM checkreq.organizations "
        "WHERE id = ANY(%s) AND is_active ORDER BY name",
        (org_ids,),
    )


def _requestable_roles() -> list[dict]:
    """Every real role EXCEPT the baseline (ENTITY_BASE_ROLE) -- an Entity
       login already has entity_member the moment it exists (granted at Add
       User time, or the first time any other role is granted); requesting
       it again is meaningless, so it's left off this picker entirely."""
    return [r for r in rbac.all_roles() if r["key"] != rbac.ENTITY_BASE_ROLE]


@router.get("/access-request", response_class=HTMLResponse)
def access_request_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    pending = rbac.get_pending_access_request(user["id"])
    orgs = _requestable_orgs(user["id"])
    roles = _requestable_roles()
    return _render(request, "access_request.html", user, {
        "pending": pending,
        "orgs": orgs,
        "roles": roles,
    })


@router.post("/access-request")
async def access_request_submit(request: Request):
    """2026-09-15: no longer blocks a user who already holds a live role --
       under Jay's Entity-vs-Parish login split, an Entity login is
       EXPECTED to hold at least entity_member and still come here to ask
       for more (that's the whole point of the tile). The real guard now is
       "only at an entity this login already belongs to" (_requestable_orgs
       below), enforced server-side even though the picker itself is
       already scoped -- a crafted POST naming an org outside that set is
       still rejected."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    if rbac.get_pending_access_request(user["id"]):
        return RedirectResponse("/access-request", status_code=303)

    form = await request.form()
    try:
        org_id = int(form.get("org_id") or 0)
    except (TypeError, ValueError):
        org_id = 0
    role_key = (form.get("role_key") or "").strip()
    note = (form.get("note") or "").strip() or None

    allowed_org_ids = set(rbac.get_entity_org_ids(user["id"]))
    org = db.query_one("SELECT id FROM checkreq.organizations WHERE id = %s AND is_active", (org_id,))
    role = db.query_one("SELECT key FROM checkreq.roles WHERE key = %s AND is_active AND key != %s",
                         (role_key, rbac.ENTITY_BASE_ROLE))
    if not org or org_id not in allowed_org_ids or not role:
        pending = None
        return _render(request, "access_request.html", user, {
            "pending": pending, "orgs": _requestable_orgs(user["id"]), "roles": _requestable_roles(),
            "error": "Pick a valid entity (one you already belong to) and role.",
        })

    rbac.create_access_request(user["id"], org_id, role_key, note)
    return RedirectResponse("/access-request", status_code=303)


@router.get("/admin/access-requests", response_class=HTMLResponse)
def admin_access_requests_page(request: Request, entity: str = ""):
    """2026-08-02 feedback batch, Item 5 (standing rule): this queue is
    cross-entity by design (any beacon_admin reviews any entity's requests)
    -- gets the same entity-filter treatment as All Requests/AP Review."""
    user, err = _require_beacon_admin(request)
    if err:
        return err
    requests_ = rbac.list_pending_access_requests()
    if entity:
        requests_ = [r for r in requests_ if r["org_code"] == entity]
    all_orgs_list = db.query("SELECT code, name FROM checkreq.organizations WHERE is_active ORDER BY name")
    return _render(request, "admin_access_requests.html", user, {
        "requests": requests_, "all_orgs_list": all_orgs_list, "filter_entity": entity,
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
