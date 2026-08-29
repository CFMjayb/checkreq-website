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

CORRECTION, same day, first real test login (Jay): the first cut above only
covered the CFO-preview half. A user whose ONLY roles are parish-scoped
(portal.parish_user_roles, zero checkreq.roles at all -- e.g.
parishadmin@stswithens.org) was landing on the normal diocesan /portal,
entity picker and all, which is exactly backwards -- "the parish access is
to allow them access to the portal for a Parish... a parish mode should not
have an entity picker, they are in their entity." effective_parish_mode()
below is now the ONE function both main.py's /portal route and _render()
call: it returns a CFO's explicit preview if one is active, OR -- new --
the parish a NATIVE parish-only user belongs to (their whole session, no
toggle needed, since they have no "Diocese Mode" to switch to at all). Also
new, same correction: the dark-red `body.parish-mode` theme (base.css) now
applies everywhere a parish is being viewed, not just on /parish-view --
Jay: "it might be good to change the color palette when you are in parish
mode... so it is clear if you are in the Parish mode or the Diocesan Mode."
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
import rbac
import registry
import parish_roles
import cornerstone_mode
import org_features
# Parish-level half of the Timekeeping gate (2026-08-17). Safe one-way import:
# timekeeping_activation.py imports only cornerstone_mode/db/rbac, never this
# module, so there is no cycle.
import timekeeping_activation

router = APIRouter()

_current_user = None
_current_org = None
_render = None


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


def _is_beacon_admin(user_id: int) -> bool:
    return rbac.user_has_role(user_id, "beacon_admin", org_id=None)


def _require_effective_cfo(request: Request):
    """2026-08-16, Jay, live test: "aren't you trying to impersonate ALL of
    that user? How can you diagnose security/permission issues when you
    are impersonating a user if your permissions are mixed into it?" --
    correct. This USED to be gated on the REAL session identity only
    (request.session['user_id'] directly, bypassing any impersonation in
    effect) -- reasoned at the time as "a non-CFO impersonated persona must
    not be able to reach this any more than they can chain-impersonate."
    That reasoning solved the wrong problem: it prevented an impersonated
    persona from GAINING power, but in doing so let the REAL admin's own
    cfo status leak INTO the impersonated view regardless of whether the
    persona actually holds it -- exactly backwards from what impersonation
    is for (seeing exactly what they'd see). Now gated on the CURRENTLY
    EFFECTIVE identity (_current_user(), the persona while impersonating,
    same as the real user otherwise) -- this is still safe against
    escalation (it can only ever narrow access, never grant the persona
    something the real admin alone had), and re-checked live every call
    same as before. Still returns the REAL session user_id, not the
    persona's -- the audit trail (parish_mode_log, the session's own
    parish_view_id ownership) should reflect who physically clicked, not
    who they were viewing as."""
    real_id = request.session.get("user_id")
    if not real_id:
        return None, RedirectResponse("/login")
    current = _current_user(request)
    if not current or not rbac.user_has_role(current["id"], "cfo", org_id=None):
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
    parish = _parish_row(parish_id)
    if not parish:
        request.session.pop("parish_view_id", None)
        _close_open_parish_mode(real_id)
        return None
    return parish


def _parish_row(parish_id: int) -> dict | None:
    """2026-08-29: also carries the diocese's own parish_mode_color/
    logo_gcs_path/logo_content_type (org_branding.py reads these off
    whatever this function returns) -- but this runs on EVERY Parish Mode
    render, including real production parish logins, so it must never
    hard-crash just because migration 051 hasn't been applied yet on a
    given environment (unlike a plain SELECT *, naming these columns
    explicitly means an UndefinedColumn error, not a silently-missing key,
    the moment they don't exist). Falls back to the pre-051 query shape on
    ANY error and fills the three new keys in as None, matching
    org_branding.theme_style_block()'s own "missing == not set" reading."""
    try:
        return db.query_one(
            "SELECT p.*, o.code AS org_code, o.name AS org_name, "
            "o.parish_mode_color, o.logo_gcs_path, o.logo_content_type "
            "FROM portal.parishes p JOIN checkreq.organizations o ON o.id = p.org_id "
            "WHERE p.id = %s",
            (parish_id,),
        )
    except Exception:
        row = db.query_one(
            "SELECT p.*, o.code AS org_code, o.name AS org_name "
            "FROM portal.parishes p JOIN checkreq.organizations o ON o.id = p.org_id "
            "WHERE p.id = %s",
            (parish_id,),
        )
        if row:
            row.setdefault("parish_mode_color", None)
            row.setdefault("logo_gcs_path", None)
            row.setdefault("logo_content_type", None)
        return row


def _native_parish_ids(user_id: int) -> list[int]:
    """Every distinct parish this user holds ANY parish role at."""
    rows = db.query(
        "SELECT DISTINCT parish_id FROM portal.parish_user_roles "
        "WHERE user_id = %s AND revoked_at IS NULL",
        (user_id,),
    )
    return [r["parish_id"] for r in rows]


def effective_parish_mode(request: Request, user: dict | None) -> tuple[dict | None, bool]:
    """THE single function main.py's _render() and /portal both call.
    Returns (parish_row_or_None, is_preview). is_preview distinguishes a
    CFO's explicit /admin/parish-mode toggle (shows the Exit banner) from a
    native parish-only user's default, whole-session state (no banner --
    there is no Diocese Mode for them to exit to). A native user holding
    roles at more than one parish and with no explicit choice yet returns
    (None, False) -- the /parish-view route below renders a picker for that
    case rather than guessing which one they meant."""
    preview = current_parish_view(request)
    if preview:
        return preview, True

    if not user or rbac.user_has_any_role(user["id"]):
        # Holds at least one checkreq.roles grant somewhere -- diocesan/
        # entity staff, even if they ALSO happen to hold a parish role.
        # Their default context stays Diocese Mode; they were never asked
        # to give that up just because they also help out at one parish.
        return None, False

    ids = _native_parish_ids(user["id"])
    if not ids:
        return None, False
    if len(ids) == 1:
        return _parish_row(ids[0]), False

    chosen = request.session.get("native_parish_id")
    if chosen and chosen in ids:
        return _parish_row(chosen), False
    return None, False


@router.get("/admin/parish-mode", response_class=HTMLResponse)
def parish_mode_picker(request: Request):
    """2026-08-16 fix (Cornerstone Served Parishes Plan.md, decision 5):
    was registry.list_all_parishes() -- the SAME unscoped, cross-diocese
    function the parish access-request dropdown uses -- so this picker
    showed every diocese's parishes in one flat list, with each row's own
    diocese name needed just to tell them apart. Scoped to the currently
    selected entity instead: a beacon_admin reaches a different diocese's
    parishes by switching entities first (top-nav), same as every other
    screen in this app already works. list_all_parishes() itself is
    untouched -- still unscoped for its other, genuinely cross-diocese
    caller.

    Same-day follow-up (Jay, live test): "you should only be able to access
    Parish Mode when you are in Diocese Mode." A Cornerstone-served
    parish-org has no parishes of its own underneath it (portal.parishes.
    org_id always points at the diocese) -- this used to just silently
    return an empty list when Cornerstone Mode was active; now blocked
    outright, matching Jay's stated rule rather than a confusing empty
    picker."""
    real_id, err = _require_effective_cfo(request)
    if err:
        return err
    org = _current_org(request)
    if org and cornerstone_mode.is_cornerstone_org(org["id"]):
        return RedirectResponse("/portal")
    parishes = registry.list_parishes(org["id"]) if org else []
    return _render(request, "parish_mode.html", _current_user(request), {
        "parishes": parishes,
        "current_org": org,
    })


@router.post("/admin/parish-mode/stop")
def parish_mode_stop(request: Request):
    """"Diocese Mode" -- the ONE way back out of either Parish Mode preview
    OR Cornerstone Mode (base.html's user-menu only ever shows this button
    in those two states, never both pickers at once, per Jay's 2026-08-16
    "the only switcher is back to Diocese Mode" rule).

    2026-08-16 fix, live test: this used to ONLY clear a Parish Mode preview
    flag -- a real no-op whenever Cornerstone Mode was active instead (that
    session flag was never set to begin with), so the button appeared to do
    nothing and the user stayed stuck in the served parish-org. Cornerstone
    Mode is a real entity switch (current_org_id), not a session-flag
    preview like Parish Mode, so leaving it means resetting current_org_id
    back to the resolved diocese too."""
    real_id = request.session.get("user_id")
    if real_id:
        _close_open_parish_mode(real_id)
    request.session.pop("parish_view_id", None)
    current_org_id = request.session.get("current_org_id")
    if current_org_id:
        diocese_org_id = cornerstone_mode.resolve_diocese_org_id(current_org_id)
        if diocese_org_id != current_org_id:
            request.session["current_org_id"] = diocese_org_id
    return RedirectResponse("/portal", status_code=303)


@router.post("/admin/parish-mode/{parish_id}")
def parish_mode_start(parish_id: int, request: Request):
    """2026-08-16: the picker above only ever SHOWS parishes under the
    current entity, but that's a display-layer filter, not authorization --
    a crafted POST could previously start Parish Mode for ANY parish_id
    regardless of diocese, since the gate checks cfo cross-entity
    (org_id=None). Now independently re-verified against this SPECIFIC
    parish's own org, matching the same "tile/action must match" discipline
    applied elsewhere this session (parish_access.py's _authorize_for_request,
    admin_hub.py's card-vs-route fix). Same-day follow-up: also checked
    against the CURRENTLY EFFECTIVE identity (the impersonated persona's
    own cfo status, not the real admin's -- see _require_effective_cfo's
    own docstring), and independently re-checks the Diocese-Mode-only gate
    (a crafted POST while Cornerstone Mode is active must not bypass what
    the picker route above already blocks)."""
    real_id, err = _require_effective_cfo(request)
    if err:
        return err

    org = _current_org(request)
    if org and cornerstone_mode.is_cornerstone_org(org["id"]):
        return RedirectResponse("/portal")

    target = db.query_one("SELECT id, org_id FROM portal.parishes WHERE id = %s AND is_active", (parish_id,))
    current = _current_user(request)
    if not target or not current or not rbac.user_has_role(current["id"], "cfo", target["org_id"]):
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

    parish, is_preview = effective_parish_mode(request, user)
    if not parish:
        # Either a CFO with nothing active (send them to the picker) or a
        # native multi-parish user with no choice made yet (render THEIR
        # own small picker instead -- never the full cross-diocese list a
        # CFO gets from /admin/parish-mode).
        own_ids = [] if is_preview else (_native_parish_ids(user["id"]) if not rbac.user_has_any_role(user["id"]) else [])
        if own_ids:
            return _render(request, "parish_choose.html", user, {
                "parishes": [_parish_row(pid) for pid in own_ids],
            })
        return RedirectResponse("/admin/parish-mode" if _is_beacon_admin(user["id"]) else "/portal")

    can_review = _is_beacon_admin(user["id"]) or parish_roles.user_has_parish_role(user["id"], "parish_admin", parish["id"])
    has_other_native_parishes = (not is_preview) and (not rbac.user_has_any_role(user["id"])) \
        and len(_native_parish_ids(user["id"])) > 1
    return _render(request, "parish_view.html", user, {
        "parish": parish, "is_preview": is_preview, "can_review": can_review,
        "can_switch": is_preview or has_other_native_parishes,
        "switch_url": "/admin/parish-mode" if is_preview else "/parish-view/switch",
        # Timekeeping tile gate revised 2026-08-16: diocese-wide org_features
        # flag, not Cornerstone-served status -- mirrors timekeeping.py's own
        # timekeeping_context() check exactly, so the tile and the route it
        # links to never disagree.
        # Extended 2026-08-17 for the second half of that same gate: the
        # per-parish HR activation flag. Both halves are required here for the
        # same reason the comment above gives -- checking only the diocese flag
        # would show the tile to a parish that is NOT HR-activated, whose only
        # possible destination is timekeeping_unavailable.html. This project's
        # own standing principle (see the 2026-08-16 RBAC entry in CLAUDE.md):
        # a tile's visibility must match its underlying route's actual gate.
        "timekeeping_enabled": (
            org_features.is_enabled(parish["org_id"], "timekeeping")
            and timekeeping_activation.parish_hr_enabled(parish)
        ),
    })


@router.get("/parish-view/switch")
def parish_view_switch(request: Request):
    """The rare multi-parish native user's "view a different one of my own
    parishes" link -- just clears the remembered choice so /parish-view
    re-renders the (own-parishes-only) picker."""
    request.session.pop("native_parish_id", None)
    return RedirectResponse("/parish-view", status_code=303)


@router.post("/parish-view/choose/{parish_id}")
def parish_view_choose(parish_id: int, request: Request):
    """The rare multi-parish-native-user case: pick which of THEIR OWN
    parishes to view. Validated against their real grants, never trusted
    from the posted id alone."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    own_ids = _native_parish_ids(user["id"])
    if parish_id in own_ids:
        request.session["native_parish_id"] = parish_id
    return RedirectResponse("/parish-view", status_code=303)
