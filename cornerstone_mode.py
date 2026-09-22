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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db
import rbac

# ── Fund Account Masks (CFM Items) ──────────────────────────────────────────
# 2026-09-18: the Fund Summary Report (26-106/26-107 qbo-mcp-server) reads its
# per-company account masks from this same `fund_account_masks` table (moved
# off an Excel tab the same session -- see 26-106's Plan.md). This is the
# first real capability on the CFM Items placeholder page: a served client's
# own masks, editable while a diocesan staffer is already working inside that
# client's Cornerstone-Mode context. Company code is resolved the same way
# parish_finance.py's _company_code() does elsewhere in this app (org's own
# `code`, lowercased) -- no DME-style override needed here, since DME is a
# diocese, not a served parish-org, and can never reach this route (see
# is_cornerstone_org() below).
#
# 2026-09-19: this table has NO dev/prod split by design -- qbo-mcp-server's
# own fund_mask_api.py hardcodes _pg_conn("dev") for the identical reason
# (QBO itself has no dev/prod realm per company, so there's nothing to
# mirror to cfmqbo_prod). Every query below explicitly targets `cfmqbo`
# regardless of which environment this app is running as (PGDATABASE would
# otherwise resolve to cfmqbo_prod on the live production service) --
# without this, production's CFM Items page would either 500 (table doesn't
# exist there) or, worse, write into a silent second copy qbo-mcp-server's
# own Fund Summary Report would never see.
_FUND_MASK_DB = "cfmqbo"


def _fund_mask_company_code(org: dict) -> str:
    return (org.get("code") or "").lower()


def get_fund_account_masks(company: str) -> list[dict]:
    return db.query(
        "SELECT id, account_mask, display_label, sort_order, fund_group, active "
        "FROM fund_account_masks WHERE lower(company) = %s AND active "
        "ORDER BY sort_order, account_mask",
        (company,), dbname=_FUND_MASK_DB,
    )


def add_fund_account_mask(company: str, account_mask: str, display_label: str,
                           sort_order: int, fund_group: str, updated_by: str) -> None:
    db.query(
        "INSERT INTO fund_account_masks (company, account_mask, display_label, sort_order, fund_group, updated_by) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (company, account_mask) DO UPDATE SET "
        "display_label = EXCLUDED.display_label, sort_order = EXCLUDED.sort_order, "
        "fund_group = EXCLUDED.fund_group, active = TRUE, updated_at = now(), updated_by = EXCLUDED.updated_by",
        (company, account_mask, display_label, sort_order, fund_group, updated_by), dbname=_FUND_MASK_DB,
    )


def update_fund_account_mask(mask_id: int, company: str, display_label: str,
                              sort_order: int, fund_group: str, updated_by: str) -> None:
    db.query(
        "UPDATE fund_account_masks SET display_label = %s, sort_order = %s, fund_group = %s, "
        "updated_at = now(), updated_by = %s WHERE id = %s AND lower(company) = %s",
        (display_label, sort_order, fund_group, updated_by, mask_id, company), dbname=_FUND_MASK_DB,
    )


def deactivate_fund_account_mask(mask_id: int, company: str) -> None:
    db.query(
        "UPDATE fund_account_masks SET active = FALSE, updated_at = now() "
        "WHERE id = %s AND lower(company) = %s",
        (mask_id, company), dbname=_FUND_MASK_DB,
    )

router = APIRouter()

_current_user = None
_current_org = None
_render = None
_accessible_diocese_orgs = None
_select_entity_core = None


def register(app, *, current_user, current_org, render, accessible_diocese_orgs, select_entity_core) -> None:
    global _current_user, _current_org, _render, _accessible_diocese_orgs, _select_entity_core
    _current_user, _current_org, _render = current_user, current_org, render
    _accessible_diocese_orgs = accessible_diocese_orgs
    # M8 (Security Assessment 2026-09-19): the actual authorize+set-session
    # mutation main.py's own POST /select-entity/{org_id} uses -- see
    # cornerstone_mode_select() below for why this can no longer be reached
    # via an HTTP redirect the way it was before /select-entity became
    # POST-only.
    _select_entity_core = select_entity_core
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


def get_cornerstone_picker_orgs(user_id: int, diocese_org_id: int | None = None) -> list[dict]:
    """Served parish-orgs where this user actually holds cornerstone_employee
    -- their own real grants, not every served parish (same "explicit grant
    required, no inheritance" rule as everything else in this app).

    2026-08-29, Jay's direct correction: Cornerstone Mode used to require
    already being inside a specific diocese's Diocese Mode before this list
    would show anything (scoped to `WHERE pdio.id = current_org`) -- a real
    chicken-and-egg problem for the new standing Cornerstone Menu landing
    page (see the router below), which must be reachable BEFORE any diocese
    is selected at all. So this became unconditionally cross-diocese --
    every served client this user holds the grant at, across every diocese.

    2026-09-19, Jay caught the real regression that created: "I went into
    Cornerstone Mode while in EDOM and can only see Clients under EDOM, when
    I change to DSW... I still only see EDOM's cornerstone clients." The
    2026-08-29 fix went too far the other way -- it dropped diocese scoping
    ENTIRELY, so switching the header's current diocese never changed this
    list at all (it just happened to look diocese-scoped for Jay, since his
    own cornerstone_employee grants all sit under EDOM's clients). Fixed by
    re-adding an optional `diocese_org_id` filter: when the caller is
    already sitting inside a real diocese (the normal case), the list is
    scoped to that diocese's own served clients; `diocese_org_id=None`
    (nothing selected yet -- the landing-page case) still returns every
    client across every diocese, preserving the original chicken-and-egg
    fix for a pure Cornerstone employee with no diocese-level role anywhere.
    `diocese_name` stays in the SELECT either way, since it's still useful
    context on the landing page's cross-diocese list. Fails closed (empty
    list) if migrations 036/037 haven't landed yet."""
    try:
        sql = """
            SELECT o.id AS org_id, o.code, p.name AS parish_name, p.city,
                   pdio.name AS diocese_name
            FROM checkreq.user_roles ur
            JOIN checkreq.roles r ON r.key = ur.role_key AND r.is_active
            JOIN checkreq.organizations o ON o.id = ur.org_id AND o.is_active AND o.cornerstone_served
            JOIN portal.parishes p ON p.linked_org_id = o.id
            JOIN checkreq.organizations pdio ON pdio.id = p.org_id
            WHERE ur.user_id = %s AND ur.role_key = 'cornerstone_employee' AND ur.revoked_at IS NULL
        """
        params: list = [user_id]
        if diocese_org_id is not None:
            sql += " AND pdio.id = %s"
            params.append(diocese_org_id)
        sql += " ORDER BY pdio.name, p.name"
        return db.query(sql, tuple(params))
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
    # org is either None or a real diocese here -- a served client's own org
    # already bounced to /portal above (is_cornerstone_org check), so it's
    # always safe to use org["id"] directly as the diocese scope.
    orgs = get_cornerstone_picker_orgs(user["id"], org["id"] if org else None)
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
    unauthorized entity-switch attempt would be.

    M8 (Security Assessment 2026-09-19): used to just issue an HTTP
    redirect to POST /select-entity/{org_id} -- worked only because that
    route was GET at the time. A browser ALWAYS follows a 3xx redirect as
    GET regardless of what the target route actually requires, so once
    /select-entity became POST-only, redirecting into it here would 405
    instead of switching anything. Now calls _select_entity_core()
    directly -- the exact same authorize-then-set-session logic
    /select-entity's own route body calls -- injected via register() (see
    this module's own top, same DI pattern as accessible_diocese_orgs)
    rather than importing main.py, which would be circular."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    org = _current_org(request)
    if _parish_preview_active(request) or (org and is_cornerstone_org(org["id"])):
        return RedirectResponse("/portal")
    if not _select_entity_core(request, org_id):
        return RedirectResponse("/admin/cornerstone-mode")
    return RedirectResponse("/portal", status_code=303)


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
    company = _fund_mask_company_code(org)
    masks = get_fund_account_masks(company)
    return _render(request, "cfm_items.html", user, {"fund_masks": masks, "fund_mask_company": company})


def _fund_mask_write_denied(user: dict, org: dict):
    """M6 (Security Assessment 2026-09-19): the three fund-mask WRITE routes
    below used to require only "signed in + the current entity is a served
    client" -- no role at all -- while writing straight into
    fund_account_masks in cfmqbo, the live configuration qbo-mcp-server's
    Fund Summary Report reads for real. Reaching a served client's context
    already requires some grant there (/select-entity's own check), but
    that could be a mere entity_member. Writing report configuration now
    requires cornerstone_employee or beacon_admin AT this specific client
    -- the same role the Cornerstone Mode picker already requires to list
    it. Returns None when allowed, else the 403 to return."""
    if not rbac.user_has_any_role(user["id"], ["cornerstone_employee", "beacon_admin"], org_id=org["id"]):
        return JSONResponse(
            {"error": "Cornerstone Employee or Beacon Admin access at this client is required."},
            status_code=403,
        )
    return None


@router.post("/admin/cfm-items/fund-masks/add")
async def cfm_items_fund_mask_add(request: Request):
    """Add (or reactivate/update) one Fund Summary Report account mask row
    for the currently-selected served client's own QBO company -- see the
    module-level comment above for why this lives here. Same access gate as
    the page itself (must be inside this specific client's own org context),
    plus the M6 role gate (_fund_mask_write_denied)."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    org = _current_org(request)
    if not org or not is_cornerstone_org(org["id"]):
        return RedirectResponse("/portal")
    denied = _fund_mask_write_denied(user, org)
    if denied:
        return denied
    form = await request.form()
    account_mask = (form.get("account_mask") or "").strip()
    if account_mask:
        try:
            sort_order = int(form.get("sort_order") or 999)
        except ValueError:
            sort_order = 999
        add_fund_account_mask(
            _fund_mask_company_code(org), account_mask,
            (form.get("display_label") or "").strip(),
            sort_order, (form.get("fund_group") or "").strip(),
            user.get("email", ""),
        )
    return RedirectResponse("/admin/cfm-items", status_code=303)


@router.post("/admin/cfm-items/fund-masks/{mask_id}/update")
async def cfm_items_fund_mask_update(mask_id: int, request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    org = _current_org(request)
    if not org or not is_cornerstone_org(org["id"]):
        return RedirectResponse("/portal")
    denied = _fund_mask_write_denied(user, org)  # M6
    if denied:
        return denied
    form = await request.form()
    try:
        sort_order = int(form.get("sort_order") or 999)
    except ValueError:
        sort_order = 999
    update_fund_account_mask(
        mask_id, _fund_mask_company_code(org),
        (form.get("display_label") or "").strip(),
        sort_order, (form.get("fund_group") or "").strip(),
        user.get("email", ""),
    )
    return RedirectResponse("/admin/cfm-items", status_code=303)


@router.post("/admin/cfm-items/fund-masks/{mask_id}/delete")
def cfm_items_fund_mask_delete(mask_id: int, request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    org = _current_org(request)
    if not org or not is_cornerstone_org(org["id"]):
        return RedirectResponse("/portal")
    denied = _fund_mask_write_denied(user, org)  # M6
    if denied:
        return denied
    deactivate_fund_account_mask(mask_id, _fund_mask_company_code(org))
    return RedirectResponse("/admin/cfm-items", status_code=303)
