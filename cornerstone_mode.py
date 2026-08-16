"""
cornerstone_mode.py -- Cornerstone Served Parishes Phase B (Cornerstone
Served Parishes Plan.md, decisions 2/4). New file per the standing main.py
rule.

UNLIKE parish_mode.py, this does NOT need its own session-tracking table or
"which identity are we previewing" mechanism -- a Cornerstone-served
parish is a first-class checkreq.organizations row (that was the whole
point of the architecture, see the plan's own diagram), so it already
plugs into the ordinary entity switcher / _current_org() session mechanism
Phase A just fixed. "Cornerstone Mode" is really: a convenience picker
(mirroring Parish Mode's own picker) that jumps straight to /select-entity
for a served parish-org you actually work with, plus a visual theme +
curated portal-tile list that appears automatically whenever the
CURRENTLY selected entity happens to be one of those parish-orgs -- no
separate "mode" flag to track or expire.

is_cornerstone_org() distinguishes a served PARISH-org (Memorial Episcopal
Church's own checkreq.organizations row) from a served top-level DIOCESE
(EDOM/Claggett/DSW/DME, which per the plan's decision 2 will ALSO carry
cornerstone_served=TRUE) -- the distinguishing fact is whether some
portal.parishes row links TO this org via linked_org_id, not the
cornerstone_served flag alone.

Defensive note: migrations 036/037 may not be applied yet on a given
environment (schema-change sign-off, per the standing rule) -- every query
here fails closed (returns False/empty) on a missing-column error rather
than crashing, since this module's whole job is "is this feature usable
right now", not an assumed-always-true fact.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import db
import rbac

router = APIRouter()

_current_user = None
_current_org = None
_render = None


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


def is_cornerstone_org(org_id: int | None) -> bool:
    """True only for a served PARISH-org (linked FROM some portal.parishes
    row) -- NOT true for a top-level diocese that merely has
    cornerstone_served=TRUE (every diocese today will, per decision 2).
    Fails closed (False) if migration 036 hasn't been applied yet."""
    if not org_id:
        return False
    try:
        row = db.query_one(
            "SELECT 1 FROM portal.parishes p "
            "JOIN checkreq.organizations o ON o.id = p.linked_org_id "
            "WHERE p.linked_org_id = %s AND o.cornerstone_served AND o.is_active",
            (org_id,),
        )
        return row is not None
    except Exception:
        return False


def get_cornerstone_picker_orgs(user_id: int) -> list[dict]:
    """Served parish-orgs where this user actually holds cornerstone_employee
    -- their own real grants, not every served parish (same "explicit grant
    required, no inheritance" rule as everything else in this app).
    Fails closed (empty list) if migrations 036/037 haven't landed yet."""
    try:
        return db.query(
            """
            SELECT o.id AS org_id, o.code, p.name AS parish_name, p.city,
                   pdio.name AS diocese_name
            FROM checkreq.user_roles ur
            JOIN checkreq.roles r ON r.key = ur.role_key AND r.is_active
            JOIN checkreq.organizations o ON o.id = ur.org_id AND o.is_active AND o.cornerstone_served
            JOIN portal.parishes p ON p.linked_org_id = o.id
            JOIN checkreq.organizations pdio ON pdio.id = p.org_id
            WHERE ur.user_id = %s AND ur.role_key = 'cornerstone_employee' AND ur.revoked_at IS NULL
            ORDER BY pdio.name, p.name
            """,
            (user_id,),
        )
    except Exception:
        return []


@router.get("/admin/cornerstone-mode", response_class=HTMLResponse)
def cornerstone_mode_picker(request: Request):
    """Visibility of the user-menu link that reaches this page is an
    existence check (holds cornerstone_employee ANYWHERE -- see base.html),
    matching Parish/Diocese Mode's own real_roles-based visibility pattern.
    This picker itself only ever lists the parish-orgs this SPECIFIC user
    is actually granted at."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    orgs = get_cornerstone_picker_orgs(user["id"])
    return _render(request, "cornerstone_mode.html", user, {"orgs": orgs})


@router.post("/admin/cornerstone-mode/{org_id}")
def cornerstone_mode_select(org_id: int, request: Request):
    """Just a normal entity-switch (reuses /select-entity's own
    authorization check -- _user_has_org_access() in main.py -- so a
    crafted org_id that this user doesn't actually hold cornerstone_employee
    at, or that isn't even a served parish-org, is rejected the identical
    way any other unauthorized entity-switch attempt would be)."""
    return RedirectResponse(f"/select-entity/{org_id}", status_code=303)
