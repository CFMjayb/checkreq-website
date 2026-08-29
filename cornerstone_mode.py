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
_accessible_diocese_orgs = None


def register(app, *, current_user, current_org, render, accessible_diocese_orgs) -> None:
    global _current_user, _current_org, _render, _accessible_diocese_orgs
    _current_user, _current_org, _render = current_user, current_org, render
    _accessible_diocese_orgs = accessible_diocese_orgs
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


def get_cornerstone_picker_orgs(user_id: int) -> list[dict]:
    """Served parish-orgs where this user actually holds cornerstone_employee
    -- their own real grants, not every served parish (same "explicit grant
    required, no inheritance" rule as everything else in this app).

    2026-08-29, Jay's direct correction: Cornerstone Mode used to require
    already being inside a specific diocese's Diocese Mode before this list
    would show anything (scoped to `WHERE pdio.id = current_org`) -- a real
    chicken-and-egg problem for the new standing Cornerstone Menu landing
    page (see the router below), which must be reachable BEFORE any diocese
    is selected at all. Now returns every served client this user holds the
    grant at, across every diocese -- `diocese_name` stays in the SELECT so
    the picker can still show which diocese each client belongs to. Fails
    closed (empty list) if migrations 036/037 haven't landed yet."""
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
    """2026-08-29, rebuilt per Jay's direct request into a standing
    "Cornerstone Menu" landing page for anyone holding cornerstone_employee
    ANYWHERE (the nav link in base.html already gates on exactly that,
    org_id=None -- "holds it anywhere" -- Jay's own confirmed answer to
    "should this menu appear for anyone with the grant at any one client").
    Two entry points from one page: pick a diocese (Diocesan Mode) or pick a
    served client (the original Cornerstone Mode picker) -- Jay's own
    framing was "enter diocesan mode and work on a diocese, [or] enter and
    work on any one of our cornerstone clients", not one gated behind the
    other.

    This closes the real chicken-and-egg gap the OLD gate had: it required
    `org` (an already-selected diocese) before this page would render at
    all, but the whole point of the new landing page is to be reachable
    BEFORE any diocese has been picked -- e.g. a pure Cornerstone employee
    who holds no diocese-level role at all, only cornerstone_employee at a
    handful of clients, would otherwise never have anywhere to land.

    Still bounces to /portal in the two cases that remain genuinely
    incompatible with this page, unchanged from before: an active Parish
    Mode preview (must exit to Diocese Mode first, same "the only switcher
    is back to Diocese Mode" rule as everywhere else), and already sitting
    inside a served client's own AP context (is_cornerstone_org(current
    org) -- same reasoning, re-checked here as defense in depth even though
    base.html's nav no longer shows this link in that state either)."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    org = _current_org(request)
    if _parish_preview_active(request) or (org and is_cornerstone_org(org["id"])):
        return RedirectResponse("/portal")
    orgs = get_cornerstone_picker_orgs(user["id"])
    dioceses = _accessible_diocese_orgs(user["id"])
    return _render(request, "cornerstone_mode.html", user, {"orgs": orgs, "dioceses": dioceses})


@router.post("/admin/cornerstone-mode/{org_id}")
def cornerstone_mode_select(org_id: int, request: Request):
    """Same relaxed gate as the picker above, re-checked independently (a
    direct POST must not bypass what the picker's own display already
    enforces) -- then a normal entity-switch, reusing /select-entity's own
    authorization check (_user_has_org_access() in main.py) so a crafted
    org_id this user doesn't actually hold cornerstone_employee at, or that
    isn't even a served parish-org, is rejected the identical way any other
    unauthorized entity-switch attempt would be."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    org = _current_org(request)
    if _parish_preview_active(request) or (org and is_cornerstone_org(org["id"])):
        return RedirectResponse("/portal")
    return RedirectResponse(f"/select-entity/{org_id}", status_code=303)


@router.get("/admin/cfm-items", response_class=HTMLResponse)
def cfm_items(request: Request):
    """2026-08-29, Jay: a new menu option on a served client's own org page,
    labeled "CFM Items" -- a growing home for Cornerstone-specific tools
    while working inside that client's context (CORNERSTONE_ONLY_MODULES in
    main.py is what actually surfaces the tile). Placeholder only for now
    ("we will be building these items in the days ahead") -- nothing to
    show yet beyond the client's own name and a plain "coming soon" note.

    Only makes sense inside a served client's own context -- bounces to
    /portal otherwise, same reasoning as the is_cornerstone_org checks
    above (reachable in practice only via the tile, which itself only
    renders under that same condition, but re-checked here as the route's
    own independent gate, not just trusting the tile's visibility)."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    org = _current_org(request)
    if not org or not is_cornerstone_org(org["id"]):
        return RedirectResponse("/portal")
    return _render(request, "cfm_items.html", user, {})
