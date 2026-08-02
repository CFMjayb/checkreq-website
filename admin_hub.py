"""
admin_hub.py — the "Administrative Tasks" landing page (2026-08-02 feedback
batch, Item 1: "the green bar at the top has way too many options... I
think one of the tiles needs to be administrative tasks").

Consolidates 8 links that previously lived directly in base.html's top nav
into one hub page, reached via a single portal tile. Each card is gated on
its own specific role (same gate its real route already enforces) so a user
sees only the cards they can actually use -- the hub itself is reachable by
anyone holding ANY of those roles (rbac.user_has_any_role), matching the
portal tile's own gate (main.py's ADMIN_TASK_ROLE_KEYS).

AP Review is deliberately NOT included here -- it keeps its own separate
portal tile, unaffected by this consolidation.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import rbac

router = APIRouter()

_current_user = None
_render = None

# Kept in sync with main.py's ADMIN_TASK_ROLE_KEYS -- not imported directly
# (this module must never import main, which imports this one), same small
# duplication admin_setup.py/access_requests.py/admin_users.py already each
# accept for their own local `_require_*` guards.
_ADMIN_TASK_ROLE_KEYS = ["cfo", "setup_admin", "beacon_admin", "vendor_approver"]


def register(app, *, current_user, render) -> None:
    global _current_user, _render
    _current_user, _render = current_user, render
    app.include_router(router)


# (title, desc, url, role_key_or_None-for-real-roles-cfo-check)
_CARDS = [
    {"title": "All Requests", "desc": "Every check request, every entity, every status.",
     "url": "/admin/all-requests", "role": "cfo"},
    {"title": "Vendor Approvals", "desc": "Approve or reject new-vendor requests; confirm W-9 receipt.",
     "url": "/admin/vendor-requests", "role": "vendor_approver"},
    {"title": "Setup Tables", "desc": "Program areas, GL account mapping, entities, global approvers.",
     "url": "/admin/setup", "role": "setup_admin"},
    {"title": "Users & Roles", "desc": "Who can sign in, and what each person may do in each entity.",
     "url": "/admin/setup/users", "role": "beacon_admin"},
    {"title": "Access Requests", "desc": "Review self-service requests from users with no role yet.",
     "url": "/admin/access-requests", "role": "beacon_admin"},
    {"title": "Feedback Log", "desc": "Everything submitted through the Feedback screen.",
     "url": "/admin/feedback", "role": "cfo"},
    {"title": "Test Mode", "desc": "Redirect outgoing emails to a test address while testing.",
     "url": "/admin/test-mode", "role": "setup_admin"},
    # "real_cfo" is a sentinel handled specially below -- gated on the REAL
    # identity (not impersonated) and hidden entirely while impersonating,
    # matching base.html's own prior rule for this exact link.
    {"title": "Impersonate a User", "desc": "Act as another user for testing or support.",
     "url": "/admin/impersonate", "role": "real_cfo"},
]


@router.get("/admin", response_class=HTMLResponse)
def admin_hub(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not rbac.user_has_any_role(user["id"], _ADMIN_TASK_ROLE_KEYS, org_id=None):
        return RedirectResponse("/portal")

    is_impersonating = bool(request.session.get("impersonating_user_id"))
    real_uid = request.session.get("user_id")
    cards = []
    for c in _CARDS:
        if c["role"] == "real_cfo":
            visible = (not is_impersonating) and real_uid and rbac.user_has_role(real_uid, "cfo", org_id=None)
        else:
            visible = rbac.user_has_role(user["id"], c["role"], org_id=None)
        if visible:
            cards.append(c)

    return _render(request, "admin_hub.html", user, {"cards": cards})
