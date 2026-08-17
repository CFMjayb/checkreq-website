"""
timekeeping.py — Cornerstone Served Parishes, Phase K: Timekeeping & HR
(Cornerstone Served Parishes Plan.md Phase K / item 24; full spec in
Parish Portal Plan.md Section 4 PP-801-808 and Section 7 Stage S8, decision
6). This is the SHARED base module for the whole Timekeeping feature: the
parish-context resolver, the authorization check, and the diocese-side
Payroll Periods / Time Categories admin screens (PP-802/PP-804's "diocese
can rename, add, or hide categories"). Two sibling modules build on top of
it: `timekeeping_roster.py` (Stage 1's staff roster CRUD) and
`timekeeping_entries.py` (Stage 2's entry grid) -- both import this module
rather than duplicating context resolution, same small-file-per-concern
discipline as parish_documents.py/cornerstone_documents.py/cornerstone_mode.py.
`timekeeping_context()` was named `served_parish_context()` until the
2026-08-16 gating revision below -- renamed since it no longer requires
Cornerstone-served status, only that the parish's own diocese has the
`org_features` "timekeeping" flag on.

STATUS (this build pass): Stage 1 (this file's diocese-side admin +
timekeeping_roster.py's roster CRUD) is fully built. Stage 2
(timekeeping_entries.py) is a real, working slice -- view the current open
period's grid and save a draft -- but does NOT implement submit/lock
(Stage 3) or diocese-side review/adjust/finalize + the Excel export
(Stage 4/5). See the project's own Timekeeping build-plan write-up (in this
session's report) for the full staged breakdown and what's left.

GATING (revised 2026-08-16, per Jay's direct decision -- supersedes the
original decision-6 framing below): Timekeeping is available to ANY parish
under a diocese that has the `org_features` "timekeeping" flag turned on
(checkreq.org_features, see org_features.py) -- NOT gated on the parish
itself being Cornerstone-served. A diocese can turn Timekeeping on for
every one of its parishes with one admin toggle (Diocese Features on
/admin/setup/organizations), regardless of whether any of them have their
own linked AP org. `timekeeping_context()` below is the one place this is
checked. PP-801's OTHER half -- "only to authorized roles (Parish Finance;
configurable)" -- is unchanged, a separate, still-real constraint:
_can_manage_timekeeping() gates on parish_finance/parish_admin (plus the
diocese-side equivalents, PP-803's "the diocese can maintain a roster on a
parish's behalf" -- beacon_admin, setup_admin at the parish's own diocese,
or a genuine cornerstone_employee grant at the parish's linked AP org, same
three-way pattern parish_documents._can_edit_parish_docs()/
cornerstone_documents.can_edit() already use for "CFM staff acting as this
entity's own back office" -- this one branch still only applies to a
served parish, since a non-served parish has no linked AP org to hold that
grant at). The PRD's "(configurable)" qualifier on the authorized-role rule
is NOT built in this pass -- flagged as an open question in this session's
report rather than guessed at.

SCOPING, mirrors 038_timekeeping.sql's own header comment exactly:
Payroll Periods and Time Categories are scoped to the DIOCESE's own
checkreq.organizations row (a parish's `org_id`, never its own
`linked_org_id`) -- diocese-wide config shared by every served parish under
it. The diocese-side admin routes below therefore only make sense from
Diocese Mode, never Cornerstone Mode -- _require_diocese_admin() explicitly
rejects a Cornerstone-Mode-selected entity rather than silently scoping
periods/categories to the wrong org_id (a served parish's own linked org).
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
import rbac
import parish_mode
import parish_roles
import cornerstone_mode
import org_features
# Parish-level half of the two-level Timekeeping gate (2026-08-17). One-way
# import only: timekeeping_activation.py deliberately does NOT import this
# module back, which is why its own _require_diocese_admin() is a documented
# duplicate of the one below rather than a shared import.
import timekeeping_activation

router = APIRouter()

_current_user = None
_current_org = None
_render = None


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


# ── Parish context resolution (shared with timekeeping_roster.py / timekeeping_entries.py) ──

def parish_context(request: Request):
    """(user, parish, diocese_org, err). Mirrors parish_documents.py's own
    _parish_context() almost exactly -- same small, documented duplication
    pattern this codebase already accepts elsewhere (parish_mode.py's own
    docstring: admin_hub.py's _ADMIN_TASK_ROLE_KEYS) rather than a
    cross-import that would risk a cycle. Resolves the CURRENT parish
    regardless of how the viewer arrived: a CFO's Parish Mode preview, a
    native parish-only login, or Cornerstone Mode bridging via
    cornerstone_mode.get_parish_for_org() when the currently-selected
    entity IS a served parish-org and no Parish Mode preview is active.
    err is None on success."""
    user = _current_user(request)
    if not user:
        return None, None, None, RedirectResponse("/login")
    parish, _is_preview = parish_mode.effective_parish_mode(request, user)
    if not parish:
        org_ctx = _current_org(request)
        if org_ctx and cornerstone_mode.is_cornerstone_org(org_ctx["id"]):
            parish = cornerstone_mode.get_parish_for_org(org_ctx["id"])
    if not parish:
        return None, None, None, RedirectResponse("/parish-view")
    diocese_org = db.query_one("SELECT * FROM checkreq.organizations WHERE id = %s", (parish["org_id"],))
    return user, parish, diocese_org, None


def served_parish_context(request: Request):
    """Deprecated alias, kept only so an old import site can't silently
    break -- see timekeeping_context() below, the real function since the
    2026-08-16 gating revision."""
    return timekeeping_context(request)


def timekeeping_context(request: Request):
    """Same as parish_context() but additionally requires Timekeeping to be
    on at BOTH levels of the two-level gate:

      1. DIOCESE -- checkreq.org_features key 'timekeeping' (org_features.py,
         2026-08-16 gating revision, see the module docstring).
      2. PARISH  -- portal.parishes.modules->>'timekeeping' (added 2026-08-17,
         owned by timekeeping_activation.py). Jay: "not all parishes will be
         HR-activated - that needs to be a feature toggle somewhere."

    Renders a clear, honest "not available" page naming WHICH level is off,
    rather than a bare redirect -- distinct from the /parish-view bounce a
    genuinely-missing parish context gets in parish_context() itself. The two
    reasons need different wording because they have different fixes: the
    diocese-level one is Diocese Features, the parish-level one is HR
    Activation, and telling someone to go ask for the wrong one wastes a
    round trip."""
    user, parish, diocese_org, err = parish_context(request)
    if err:
        return None, None, None, err
    if not org_features.is_enabled(parish["org_id"], "timekeeping"):
        return None, None, None, _render(
            request, "timekeeping_unavailable.html", user,
            {"parish": parish, "reason": "diocese"})
    if not timekeeping_activation.parish_hr_enabled(parish):
        return None, None, None, _render(
            request, "timekeeping_unavailable.html", user,
            {"parish": parish, "reason": "parish"})
    return user, parish, diocese_org, None


def can_manage_timekeeping(user: dict, parish: dict) -> bool:
    """PP-801 ("authorized roles: Parish Finance; configurable") + PP-803
    ("the diocese can maintain a roster on a parish's behalf"). Used for
    BOTH staff-roster management (timekeeping_roster.py) and time entry
    (timekeeping_entries.py) -- the PRD names the same authorized role for
    both, so this is one shared check, not two parallel ones that could
    drift. Parish-side: parish_finance or parish_admin at this specific
    parish. Diocese-side: beacon_admin (anywhere), setup_admin at the
    parish's own diocese org, or a genuine cornerstone_employee grant at
    the parish's linked AP org -- the same three-way "CFM staff acting as
    this entity's own back office" pattern parish_documents.
    _can_edit_parish_docs() and cornerstone_documents.can_edit() already
    use."""
    if rbac.user_has_role(user["id"], "beacon_admin", org_id=None):
        return True
    if rbac.user_has_role(user["id"], "setup_admin", parish["org_id"]):
        return True
    if parish.get("linked_org_id") and rbac.user_has_role(user["id"], "cornerstone_employee", parish["linked_org_id"]):
        return True
    return parish_roles.user_has_any_parish_role(user["id"], ["parish_finance", "parish_admin"], parish["id"])


# ── Diocese-side admin: Payroll Periods + Time Categories ──────────────────

def _require_diocese_admin(request: Request):
    """(user, org, None) when allowed, (None, None, response) when not.
    Rejects a Cornerstone-Mode-selected entity outright (redirect to
    /portal) rather than letting it fall through -- Payroll Periods/Time
    Categories are diocese-wide config keyed on the DIOCESE org_id, and a
    served parish's own linked org is a different, unrelated
    checkreq.organizations row; silently scoping to whichever one happens
    to be "current" would create periods/categories against the wrong org
    the moment someone holding setup_admin at BOTH happened to be in
    Cornerstone Mode when they clicked through. Same "only reachable from
    Diocese Mode" rule parish_mode.py/cornerstone_mode.py already apply to
    their own pickers."""
    user = _current_user(request)
    if not user:
        return None, None, RedirectResponse("/login")
    org = _current_org(request)
    org_id = org["id"] if org else None
    if org_id is None or cornerstone_mode.is_cornerstone_org(org_id):
        return None, None, RedirectResponse("/portal")
    # hr_admin added 2026-08-17. Jay, asked who moves a period from 'future' to
    # 'open' each fortnight: "Payroll diocesan admin" -- and confirmed that is
    # the existing hr_admin role, not a new one. Opening and closing a payroll
    # period is payroll OPERATIONS, not setup-table configuration, and the
    # alternative (giving a payroll clerk setup_admin) would also hand them
    # Program Areas, GL account mapping, approval rules and entity settings.
    #
    # This gate covers Payroll Periods and Time Categories ONLY. HR Activation
    # deliberately stays setup_admin -- Jay's explicit choice when asked -- and
    # is unaffected because timekeeping_activation.py carries its own copy of
    # this check rather than importing this one (see its docstring).
    if not rbac.user_has_any_role(user["id"], ["setup_admin", "beacon_admin", "hr_admin"],
                                  org_id=org_id):
        return None, None, JSONResponse(
            {"error": "Setup Admin or HR Admin access required"}, status_code=403)
    return user, org, None


# -- Time categories --------------------------------------------------------

def list_categories(org_id: int, include_inactive: bool = False) -> list[dict]:
    if include_inactive:
        return db.query(
            "SELECT * FROM portal.timekeeping_categories WHERE org_id = %s ORDER BY sort_order",
            (org_id,),
        )
    return db.query(
        "SELECT * FROM portal.timekeeping_categories WHERE org_id = %s AND is_active ORDER BY sort_order",
        (org_id,),
    )


def create_category(org_id: int, key: str, label: str, sort_order: int) -> dict | None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.timekeeping_categories (org_id, key, label, sort_order) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (org_id, key) DO NOTHING RETURNING *",
                (org_id, key, label, sort_order),
            )
            return cur.fetchone()


def update_category(category_id: int, org_id: int, **fields) -> dict | None:
    allowed = {"label", "sort_order", "is_active"}
    sets = [f"{k} = %s" for k in fields if k in allowed]
    if not sets:
        return db.query_one(
            "SELECT * FROM portal.timekeeping_categories WHERE id = %s AND org_id = %s",
            (category_id, org_id),
        )
    vals = [fields[k] for k in fields if k in allowed]
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE portal.timekeeping_categories SET {', '.join(sets)} "
                f"WHERE id = %s AND org_id = %s RETURNING *",
                tuple(vals) + (category_id, org_id),
            )
            return cur.fetchone()


@router.get("/admin/timekeeping/categories", response_class=HTMLResponse)
def categories_page(request: Request):
    user, org, err = _require_diocese_admin(request)
    if err:
        return err
    categories = list_categories(org["id"], include_inactive=True)
    return _render(request, "timekeeping_categories.html", user, {
        "categories": categories, "current_org": org,
    })


@router.post("/admin/timekeeping/categories/create")
async def categories_create(request: Request):
    user, org, err = _require_diocese_admin(request)
    if err:
        return err
    form = await request.form()
    key = (form.get("key") or "").strip().lower().replace(" ", "_")
    label = (form.get("label") or "").strip()
    try:
        sort_order = int(form.get("sort_order") or 50)
    except ValueError:
        sort_order = 50
    if not key or not label:
        return RedirectResponse(
            "/admin/timekeeping/categories?error=Key+and+label+are+both+required.", status_code=303
        )
    created = create_category(org["id"], key, label, sort_order)
    if not created:
        return RedirectResponse(
            f"/admin/timekeeping/categories?error=A+category+with+key+'{key}'+already+exists.",
            status_code=303,
        )
    return RedirectResponse("/admin/timekeeping/categories?saved=1", status_code=303)


@router.post("/admin/timekeeping/categories/{category_id}/update")
async def categories_update(category_id: int, request: Request):
    user, org, err = _require_diocese_admin(request)
    if err:
        return err
    form = await request.form()
    label = (form.get("label") or "").strip()
    try:
        sort_order = int(form.get("sort_order") or 0)
    except ValueError:
        sort_order = 0
    if not label:
        return RedirectResponse("/admin/timekeeping/categories?error=Label+is+required.", status_code=303)
    update_category(category_id, org["id"], label=label, sort_order=sort_order)
    return RedirectResponse("/admin/timekeeping/categories?saved=1", status_code=303)


@router.post("/admin/timekeeping/categories/{category_id}/toggle-active")
def categories_toggle_active(category_id: int, request: Request):
    user, org, err = _require_diocese_admin(request)
    if err:
        return err
    existing = db.query_one(
        "SELECT is_active FROM portal.timekeeping_categories WHERE id = %s AND org_id = %s",
        (category_id, org["id"]),
    )
    if existing:
        update_category(category_id, org["id"], is_active=not existing["is_active"])
    return RedirectResponse("/admin/timekeeping/categories?saved=1", status_code=303)


# -- Payroll periods ---------------------------------------------------------

def list_periods(org_id: int) -> list[dict]:
    return db.query(
        "SELECT * FROM portal.payroll_periods WHERE org_id = %s ORDER BY period_start DESC",
        (org_id,),
    )


def get_current_open_period(org_id: int) -> dict | None:
    """The open period the parish entry grid (timekeeping_entries.py) targets.
    None if the diocese has no open period at all.

    DATE-DRIVEN as of 2026-08-17 (Jay approved the change): prefers the open
    period whose date range CONTAINS today, and only falls back to "most
    recent open" when today sits outside every open period's range.

    Why this changed. The original version was purely "most recent open",
    which silently assumed at most ONE period is ever open. That assumption
    broke the moment a real published pay calendar was loaded: DME's 26
    periods for 2026 all exist up front, so "most recent" meant December, and
    keeping it correct would have required someone to remember to flip the
    next period open every two weeks forever -- a standing manual chore whose
    only failure mode is silent (staff enter hours against the wrong period).
    Ordering by "does today fall inside this period" makes the calendar itself
    the source of truth.

    The fallback still matters and is deliberate, not a leftover: between a
    period ending Sunday and the next one starting, or for a diocese that
    hand-opens a single catch-up period well after the fact, there may be no
    open period containing today -- returning the most recent open one is the
    behaviour that was already relied on, so it is preserved rather than
    replaced.
    """
    return db.query_one(
        "SELECT * FROM portal.payroll_periods "
        "WHERE org_id = %s AND status = 'open' "
        "ORDER BY (CURRENT_DATE BETWEEN period_start AND period_end) DESC, "
        "         period_start DESC "
        "LIMIT 1",
        (org_id,),
    )


def create_period(org_id: int, period_start, period_end, label: str | None,
                   submission_deadline) -> dict:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.payroll_periods "
                "(org_id, period_start, period_end, label, submission_deadline) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING *",
                (org_id, period_start, period_end, label or None, submission_deadline or None),
            )
            return cur.fetchone()


@router.get("/admin/timekeeping/periods", response_class=HTMLResponse)
def periods_page(request: Request):
    user, org, err = _require_diocese_admin(request)
    if err:
        return err
    periods = list_periods(org["id"])
    return _render(request, "timekeeping_periods.html", user, {
        "periods": periods, "current_org": org,
    })


@router.post("/admin/timekeeping/periods/create")
async def periods_create(request: Request):
    user, org, err = _require_diocese_admin(request)
    if err:
        return err
    form = await request.form()
    period_start = (form.get("period_start") or "").strip()
    period_end = (form.get("period_end") or "").strip()
    if not period_start or not period_end:
        return RedirectResponse(
            "/admin/timekeeping/periods?error=Start+and+end+dates+are+both+required.", status_code=303
        )
    if period_end < period_start:
        return RedirectResponse(
            "/admin/timekeeping/periods?error=End+date+must+be+on+or+after+the+start+date.", status_code=303
        )
    try:
        create_period(
            org["id"], period_start, period_end,
            (form.get("label") or "").strip() or None,
            (form.get("submission_deadline") or "").strip() or None,
        )
    except Exception:
        return RedirectResponse(
            "/admin/timekeeping/periods?error=That+period+already+exists+for+this+diocese.",
            status_code=303,
        )
    return RedirectResponse("/admin/timekeeping/periods?created=1", status_code=303)
