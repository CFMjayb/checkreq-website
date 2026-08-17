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

import org_features
import rbac

router = APIRouter()

_current_user = None
_current_org = None
_render = None

# Kept in sync with main.py's ADMIN_TASK_ROLE_KEYS -- not imported directly
# (this module must never import main, which imports this one), same small
# duplication admin_setup.py/access_requests.py/admin_users.py already each
# accept for their own local `_require_*` guards.
#
# 'hr_admin' added 2026-08-16 (Timekeeping HR Roster Review Plan.md) -- an
# hr_admin-only user (holding no other admin role) would otherwise be
# bounced straight back to /portal before ever seeing the Timekeeping
# Review card below; main.py's own ADMIN_TASK_ROLE_KEYS constant needs the
# identical addition for the "Administrative Tasks" portal tile itself to
# even appear for such a user -- see this project's own wiring snippet for
# that one-line change.
_ADMIN_TASK_ROLE_KEYS = ["cfo", "setup_admin", "beacon_admin", "vendor_approver", "hr_admin"]


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


# (title, desc, url, role_key_or_None-for-real-roles-cfo-check, group)
#
# `group` added 2026-08-16, Jay: "the Admin Tasks screen is getting busy...
# group them: AP related, Parish related, and the ones that are related to
# Beacon overall (which should be at the beginning of the list)." Purely a
# display grouping (see _CARD_GROUPS below for the fixed display order) --
# does not change any card's own visibility gate.
_CARDS = [
    {"title": "User Management", "desc": "Who can sign in, and what each person may do in each entity.",
     "url": "/admin/setup/users", "role": "beacon_admin", "group": "beacon"},
    {"title": "Access Requests", "desc": "Review self-service requests from users with no role yet.",
     "url": "/admin/access-requests", "role": "beacon_admin", "group": "beacon"},
    {"title": "Feedback Log", "desc": "Everything submitted through the Feedback screen.",
     "url": "/admin/feedback", "role": "cfo", "group": "beacon"},
    {"title": "Test Mode", "desc": "Redirect outgoing emails to a test address while testing.",
     "url": "/admin/test-mode", "role": "setup_admin", "group": "beacon"},
    # "real_cfo" is a sentinel handled specially below -- gated on the REAL
    # identity (not impersonated) and hidden entirely while impersonating,
    # matching base.html's own prior rule for this exact link.
    {"title": "Impersonate a User", "desc": "Act as another user for testing or support.",
     "url": "/admin/impersonate", "role": "real_cfo", "group": "beacon"},

    {"title": "All Requests", "desc": "Every check request, every entity, every status.",
     "url": "/admin/all-requests", "role": "cfo", "group": "ap"},
    {"title": "Vendor Approvals", "desc": "Approve or reject new-vendor requests; confirm W-9 receipt.",
     "url": "/admin/vendor-requests", "role": "vendor_approver", "group": "ap"},
    {"title": "Setup Tables", "desc": "Program areas, GL account mapping, entities, global approvers.",
     "url": "/admin/setup", "role": "setup_admin", "group": "ap"},

    # Parish Mode (S4, 2026-08-08) -- gated identically to Impersonate a
    # User (real_cfo sentinel), same reasoning: hidden while already
    # impersonating, since only the real underlying CFO may reach it.
    {"title": "Parish Mode", "desc": "See a specific parish's (currently minimal) portal view.",
     "url": "/admin/parish-mode", "role": "real_cfo", "group": "parish"},
    # Parish Portal S4+S5 (2026-08-08) -- three new diocesan-side management
    # screens. `role` is a LIST here (setup_admin OR beacon_admin) -- see
    # the visibility check below, which now accepts either shape.
    {"title": "Announcements", "desc": "Post dated, targeted announcements to parish users.",
     "url": "/admin/announcements", "role": ["setup_admin", "beacon_admin"], "group": "parish"},
    {"title": "Parish Documents", "desc": "Upload documents into a specific parish's read-only archive.",
     "url": "/admin/parish-documents", "role": ["setup_admin", "beacon_admin"], "group": "parish"},
    {"title": "Resource Library", "desc": "Manage the diocese-wide shared resource library.",
     "url": "/admin/resource-library", "role": ["setup_admin", "beacon_admin"], "group": "parish"},
    # Cornerstone Served Parishes Phase A (2026-08-16) -- entity-scoped, same
    # reasoning as Setup Tables (see _ENTITY_SCOPED_TITLES below): this
    # screen only ever shows/acts on the current diocese's own parishes.
    {"title": "Manage Parishes", "desc": "Designate a parish Cornerstone Served, or view its status.",
     "url": "/admin/manage-parishes", "role": "setup_admin", "group": "parish"},
    # Cornerstone Served Parishes Phase K (2026-08-16) -- diocese-wide
    # Timekeeping config, entity-scoped the same way Manage Parishes is
    # (timekeeping.py's own _require_diocese_admin() independently rejects
    # a Cornerstone-Mode-selected entity too, since these are keyed to the
    # DIOCESE org, never a served parish's own linked org).
    # 2026-08-16, Jay: moved to their own "HR" group at the bottom, and now
    # gated on the new checkreq.org_features "timekeeping" flag (see
    # org_features.py) in addition to the existing role check -- these
    # cards (and the whole HR group heading itself) simply don't appear
    # for a diocese that hasn't had Timekeeping/HR turned on, DME being the
    # first and currently only one. feature_key is checked generically in
    # the loop below, same pattern as the role/entity-scoped checks.
    # Added 2026-08-17 (Jay: "not all parishes will be HR-activated - that
    # needs to be a feature toggle somewhere"). Sits FIRST in the HR group
    # because it is the prerequisite for everything else here -- a parish that
    # isn't activated has no roster and no entry grid. `setup_admin`, matching
    # the two configuration cards below rather than the hr_admin operational
    # ones; see timekeeping_activation.py's docstring for why that split was
    # chosen (and why widening to hr_admin is the deliberately-open direction).
    {"title": "HR Activation", "desc": "Choose which parishes use Timekeeping / HR.",
     "url": "/admin/timekeeping/parishes", "role": "setup_admin", "group": "hr", "feature_key": "timekeeping"},
    # Added 2026-08-17 (Jay: "where do I load the employees for HR? ... create a
    # screen card that allows adds/mods/and inactivations with a as-of date").
    # hr_admin/beacon_admin -- the OPERATIONAL half of this group's gate split,
    # not the setup_admin configuration half: day-to-day employee maintenance is
    # operations, unlike Payroll Periods / Time Categories / HR Activation.
    {"title": "Employees", "desc": "Add, change or inactivate staff across every parish, with an as-of date.",
     "url": "/admin/timekeeping/employees", "role": ["hr_admin", "beacon_admin"], "group": "hr", "feature_key": "timekeeping"},
    # role widened to include hr_admin 2026-08-17 (Jay: the "Payroll diocesan
    # admin" who opens each period IS hr_admin) -- must stay in step with
    # timekeeping._require_diocese_admin(), since a visible tile whose route
    # 403s, or a hidden tile whose route works, is the mismatch this project's
    # own "tile must match its route's gate" principle exists to prevent.
    {"title": "Payroll Periods", "desc": "Define diocese payroll periods, and open or close each one.",
     "url": "/admin/timekeeping/periods", "role": ["setup_admin", "hr_admin"], "group": "hr", "feature_key": "timekeeping"},
    {"title": "Time Categories", "desc": "Configure the categories parishes report hours against.",
     "url": "/admin/timekeeping/categories", "role": ["setup_admin", "hr_admin"], "group": "hr", "feature_key": "timekeeping"},
    # Timekeeping HR Roster Review Plan.md (2026-08-16) -- Stage 4's
    # diocese-side review screen. Entity-scoped like the two cards above
    # (same DIOCESE org_id, never a served parish's own linked org -- see
    # timekeeping_review.py's own _require_hr_admin()). `role` is a LIST
    # (hr_admin OR beacon_admin) -- see the visibility check below, which
    # now handles a list for entity-scoped cards too, not just cross-entity
    # ones.
    {"title": "Timekeeping Review", "desc": "Approve or reject proposed staff roster changes; see submitted hours awaiting review.",
     "url": "/admin/timekeeping/review", "role": ["hr_admin", "beacon_admin"], "group": "hr", "feature_key": "timekeeping"},
    # Added 2026-08-16, same session as the Roster Review screen above --
    # Jay: "screens to check the status of submissions for a period, and
    # the ability to enter missing data, and to generate a spreadsheet with
    # the data submitted for a period." Same gate/group/feature_key as
    # Timekeeping Review (both are timekeeping_status.py's own
    # _require_hr_admin(), a deliberate small duplicate of the same check,
    # matching this codebase's established per-module permission-check
    # precedent) -- entity-scoped like every other HR card.
    {"title": "Timekeeping Status", "desc": "See every parish's submission status for a period, enter missing hours, and export the period to Excel.",
     "url": "/admin/timekeeping/status", "role": ["hr_admin", "beacon_admin"], "group": "hr", "feature_key": "timekeeping"},
]

# Fixed display order + section headings -- per Jay's explicit request,
# Beacon-overall items come first, not alphabetically or by insertion order.
# "HR" added 2026-08-16, deliberately LAST -- Jay: "the HR-related options
# ... should be in their own HR section at the bottom."
_CARD_GROUPS = [
    ("beacon", "Beacon"),
    ("ap", "AP"),
    ("parish", "Parish"),
    ("hr", "HR"),
]

# Titles whose underlying route is genuinely entity-scoped (not cross-entity
# by design/necessity like everything else in this hub) -- see admin_hub()'s
# own docstring for why this distinction matters.
_ENTITY_SCOPED_TITLES = {"Setup Tables", "Manage Parishes", "Payroll Periods", "Time Categories",
                          "Timekeeping Review", "Timekeeping Status", "HR Activation",
                          "Employees"}


@router.get("/admin", response_class=HTMLResponse)
def admin_hub(request: Request):
    """2026-08-16 correction: a card's visibility must match its OWN
    underlying route's actual gate, not be blanket-converted to
    entity-scoped -- a first pass here did that uniformly and it was wrong.
    Most of these routes are legitimately, deliberately cross-entity
    (existence check: "holds the role somewhere"), either because the
    screen itself manages data across every entity by design (Organizations/
    Global Approvers, Users & Roles), because it's a functionally necessary
    bootstrap mechanism (Access Requests -- a brand-new org has zero
    existing role holders, so SOMEONE needs cross-entity power to grant its
    first-ever role, or onboarding a new org could never get off the
    ground), because the underlying setting is genuinely app-wide and not
    per-entity at all (Test Mode -- app_settings has no org dimension), or
    simply because that route hasn't been converted (Vendor Approvals'
    QUERY is now scoped to granted orgs, per ap_review_list's fix, but its
    GATE deliberately stayed an existence check -- the card must match the
    gate, not the query). Only Setup Tables is genuinely entity-scoped data
    underneath (Program Areas/GL Mapping require picking one entity at a
    time already) -- its gate (admin_setup.py's _require_setup_admin) was
    converted to match, so its card is the one real exception here."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not rbac.user_has_any_role(user["id"], _ADMIN_TASK_ROLE_KEYS, org_id=None):
        return RedirectResponse("/portal")

    org = _current_org(request)
    org_id = org["id"] if org else None
    is_impersonating = bool(request.session.get("impersonating_user_id"))
    real_uid = request.session.get("user_id")
    cards = []
    for c in _CARDS:
        if c["role"] == "real_cfo":
            visible = (not is_impersonating) and real_uid and rbac.user_has_role(real_uid, "cfo", org_id=None)
        elif c["title"] in _ENTITY_SCOPED_TITLES:
            # 2026-08-16: generalized from a hardcoded "setup_admin" check
            # (every entity-scoped card used to require exactly that role)
            # to check the CARD'S OWN c["role"] instead -- Timekeeping
            # Review's required role is hr_admin/beacon_admin, not
            # setup_admin. Verified this doesn't change behavior for the
            # 4 pre-existing entity-scoped titles, which all have
            # c["role"] == "setup_admin" already.
            if org_id is None:
                visible = False
            elif isinstance(c["role"], list):
                visible = rbac.user_has_any_role(user["id"], c["role"], org_id=org_id)
            else:
                visible = rbac.user_has_role(user["id"], c["role"], org_id=org_id)
        elif isinstance(c["role"], list):
            visible = rbac.user_has_any_role(user["id"], c["role"], org_id=None)
        else:
            visible = rbac.user_has_role(user["id"], c["role"], org_id=None)
        # 2026-08-16, Jay: HR cards (and any future feature-gated card)
        # additionally require the diocese feature itself to be turned on
        # -- checked here, generically, rather than folded into the role
        # logic above, so a role check and a feature check never have to
        # be reconciled inside the same branch. A card with no feature_key
        # is unaffected (org_features.is_enabled only ever runs for the
        # small set that opts into it).
        if visible and c.get("feature_key"):
            visible = bool(org_id) and org_features.is_enabled(org_id, c["feature_key"])
        if visible:
            cards.append(c)

    # Grouped for display only (Jay, 2026-08-16: "the Admin Tasks screen is
    # getting busy... group them") -- a group with nothing visible in it is
    # simply omitted, not shown as an empty section.
    grouped = []
    for key, label in _CARD_GROUPS:
        group_cards = [c for c in cards if c["group"] == key]
        if group_cards:
            grouped.append({"label": label, "cards": group_cards})

    return _render(request, "admin_hub.html", user, {"groups": grouped})
