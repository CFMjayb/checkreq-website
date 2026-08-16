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


def resolve_diocese_org_id(org_id: int) -> int:
    """2026-08-16, real bugs found live by Jay ("switching between the three
    modes doesn't work right now"): if org_id is a served parish's own
    AP-org (linked FROM some portal.parishes row), return that parish's
    DIOCESE org_id instead -- the "step back one level" resolution both
    "Diocese Mode" (main.py's stop route) and the Parish Mode picker
    (parish_mode.py) need. A served parish-org has no parishes of its own
    underneath it -- portal.parishes.org_id always points at the diocese --
    so treating it as-is for either purpose left Diocese Mode as a no-op
    and Parish Mode's picker showing "No parishes yet in this entity".
    Returns org_id unchanged if it's already a diocese, or on any DB error
    (same fail-closed philosophy as the rest of this module)."""
    try:
        row = db.query_one("SELECT org_id FROM portal.parishes WHERE linked_org_id = %s", (org_id,))
        return row["org_id"] if row else org_id
    except Exception:
        return org_id


def get_parish_for_org(org_id: int) -> dict | None:
    """The portal.parishes row THIS served org is linked FROM -- the reverse
    of resolve_diocese_org_id's own diocese lookup. Used by
    parish_documents.py so a diocesan staffer working inside a served
    parish's own AP org (Cornerstone Mode) reaches that SAME parish's
    Document Library, without needing a separate Parish Mode preview on
    top (Jay, 2026-08-16: "Document Library, and Resources would show"
    under Cornerstone Mode). Fails closed (None) on any DB error."""
    try:
        return db.query_one(
            "SELECT p.*, o.code AS org_code, o.name AS org_name "
            "FROM portal.parishes p JOIN checkreq.organizations o ON o.id = p.org_id "
            "WHERE p.linked_org_id = %s",
            (org_id,),
        )
    except Exception:
        return None


def _parish_preview_active(request: Request) -> bool:
    """Lightweight duplicate of parish_mode.current_parish_view()'s own
    session-key check -- avoids a circular import, since parish_mode.py now
    needs to import THIS module (resolve_diocese_org_id / is_cornerstone_org
    for its own Diocese-Mode-only gating, see that file). Fails safe in the
    deny direction only: a stale flag just blocks Cornerstone Mode entry a
    little too eagerly, never opens anything it shouldn't."""
    return bool(request.session.get("parish_view_id"))


def get_cornerstone_picker_orgs(user_id: int, diocese_org_id: int) -> list[dict]:
    """Served parish-orgs where this user actually holds cornerstone_employee
    -- their own real grants, not every served parish (same "explicit grant
    required, no inheritance" rule as everything else in this app) --
    scoped to the CURRENT diocese only. Jay, 2026-08-16, live test:
    "Cornerstone Mode is available to Diocese Mode only and only relates to
    parishes served for that Diocese" -- not a cross-diocese list of every
    parish this person might work at anywhere. Fails closed (empty list) if
    migrations 036/037 haven't landed yet."""
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
              AND pdio.id = %s
            ORDER BY p.name
            """,
            (user_id, diocese_org_id),
        )
    except Exception:
        return []


@router.get("/admin/cornerstone-mode", response_class=HTMLResponse)
def cornerstone_mode_picker(request: Request):
    """Jay, 2026-08-16, live test: "Cornerstone Mode is available to Diocese
    Mode only" -- reachable only from a plain diocese context, never
    stacked on top of an active Parish Mode preview or an already-active
    Cornerstone Mode session; the one way out of either of those is
    "Diocese Mode" first (base.html's user-menu already only shows this
    link in that state, this is the matching server-side gate -- same
    "tile visibility must match its route's own gate" discipline as
    admin_hub.py's earlier self-correction)."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    org = _current_org(request)
    if _parish_preview_active(request) or not org or is_cornerstone_org(org["id"]):
        return RedirectResponse("/portal")
    orgs = get_cornerstone_picker_orgs(user["id"], org["id"])
    return _render(request, "cornerstone_mode.html", user, {"orgs": orgs})


@router.post("/admin/cornerstone-mode/{org_id}")
def cornerstone_mode_select(org_id: int, request: Request):
    """Same Diocese-Mode-only gate as the picker above, re-checked
    independently (a direct POST must not bypass what the picker's own
    display already enforces) -- then a normal entity-switch, reusing
    /select-entity's own authorization check (_user_has_org_access() in
    main.py) so a crafted org_id this user doesn't actually hold
    cornerstone_employee at, or that isn't even a served parish-org, is
    rejected the identical way any other unauthorized entity-switch
    attempt would be."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    org = _current_org(request)
    if _parish_preview_active(request) or not org or is_cornerstone_org(org["id"]):
        return RedirectResponse("/portal")
    return RedirectResponse(f"/select-entity/{org_id}", status_code=303)
