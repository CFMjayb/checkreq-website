"""
timekeeping_activation.py — per-parish HR/Timekeeping activation (added
2026-08-17). Jay: "not all parishes will be HR-activated - that needs to be
a feature toggle somewhere."

TWO-LEVEL GATE. Timekeeping is available to a parish only when BOTH are true:
  1. DIOCESE level -- checkreq.org_features feature_key 'timekeeping'
     (org_features.py, built 2026-08-16). Unchanged by this module.
  2. PARISH level  -- portal.parishes.modules->>'timekeeping' == 'true'.
     THIS module owns that second level.

WHY portal.parishes.modules AND NOT A NEW COLUMN: `modules` is a JSONB column
that has existed unused since migration 023 and is already in
registry.update_parish()'s writable-field allowlist, so this needed no schema
change at all. It is a general per-parish feature MAP -- timekeeping is only
its first key. A dedicated `hr_enabled` boolean would have to be joined by a
second column for the next feature, and a third after that; this scales by
adding a key. Confirmed with Jay before building.

Reads/writes are always a JSONB MERGE (`modules || {...}`), never an
overwrite, so a future feature's own key in `modules` can't be clobbered by
someone toggling HR.

ABSENT KEY READS AS OFF. That is the safe default for a brand-new parish
(nobody is silently opted into a payroll workflow), but it is also why the
one-off backfill
(migrations/backfill_dme_hr_activation_2026-08-17.py) had to run BEFORE this
module's gate went live -- otherwise every existing DME parish would have
lost Timekeeping the moment it deployed. That backfill's approved rule:
HR ON for every DME parish row that has a payroll department code, OFF for
the 8 congregations added from the 2025 parochial report that have no
diocesan payroll.

WHY ITS OWN SCREEN rather than a checkbox on Manage Parishes
(parish_org_admin.py) -- recommended to and approved by Jay 2026-08-17:
  - Activation is a BULK operation (57 DME parishes on day one). Manage
    Parishes is a per-parish drill-down; toggling 57 rows one at a time is
    unusable. This screen is one list, one Save, using the same batched
    dirty-row pattern GL Mapping / Global Approvers already use.
  - It keeps Manage Parishes from becoming a feature-flag dashboard that
    grows a checkbox per module forever.
  - It matches the HR group's own existing gate split: CONFIGURATION screens
    (Payroll Periods, Time Categories) are setup_admin; OPERATIONAL screens
    (Timekeeping Review, Timekeeping Status) are hr_admin/beacon_admin.
    Activation is configuration, so it is setup_admin -- deliberately NOT
    reachable by an hr_admin alone. Jay was told this explicitly and chose
    to start strict; widening to include hr_admin later is the reversible
    direction, so that is the one that was left open.

New, single-purpose file per this project's standing modular-file convention
(timekeeping.py / timekeeping_roster.py / timekeeping_entries.py /
timekeeping_review.py / timekeeping_status.py / timekeeping_roster_changes.py
are its siblings). Deliberately does NOT import timekeeping.py -- timekeeping.py
imports THIS module for parish_hr_enabled(), so importing back would be a
cycle. _require_diocese_admin() below is therefore a small, documented
duplicate of timekeeping._require_diocese_admin(), the same duplication
precedent timekeeping_review.py/timekeeping_status.py already set for their
own _require_hr_admin().
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

import cornerstone_mode
import db
import rbac

router = APIRouter()

FEATURE_KEY = "timekeeping"

_current_user = None
_current_org = None
_render = None


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


# ── The gate itself (called by timekeeping.timekeeping_context()) ───────────

def parish_hr_enabled(parish: dict) -> bool:
    """Is this specific parish activated for HR/Timekeeping?

    Takes the already-loaded parish dict rather than an id so the common
    path (timekeeping_context, which already has the row) costs no extra
    query. A missing `modules`, a missing key, or any non-true value all
    read as OFF -- there is deliberately no "absent means on" fallback.
    """
    modules = (parish or {}).get("modules") or {}
    if isinstance(modules, str):  # belt-and-braces if a caller passed raw text
        import json
        try:
            modules = json.loads(modules)
        except ValueError:
            return False
    return modules.get(FEATURE_KEY) is True


# ── Diocese-side admin screen ──────────────────────────────────────────────

def _require_diocese_admin(request: Request):
    """(user, org, None) when allowed, (None, None, response) when not.
    Rejects a Cornerstone-Mode-selected entity outright, for the same reason
    timekeeping._require_diocese_admin() does: this screen lists the parishes
    of a DIOCESE, and a served parish's own linked org is a different,
    unrelated checkreq.organizations row -- scoping to whichever happened to
    be "current" would show the wrong (empty) list to someone holding
    setup_admin at both."""
    user = _current_user(request)
    if not user:
        return None, None, RedirectResponse("/login")
    org = _current_org(request)
    org_id = org["id"] if org else None
    if org_id is None or cornerstone_mode.is_cornerstone_org(org_id):
        return None, None, RedirectResponse("/portal")
    if not rbac.user_has_any_role(user["id"], ["setup_admin", "beacon_admin"], org_id=org_id):
        return None, None, JSONResponse({"error": "Setup Admin access required"}, status_code=403)
    return user, org, None


def list_parishes_with_hr(org_id: int) -> list[dict]:
    """Every parish under this diocese with its current HR flag, ordered so
    the ones the diocese actually runs payroll for (those with a department
    code) sort first -- that is the working set on this screen."""
    return db.query(
        "SELECT id, code, name, short_name, city, is_congregation, is_active, "
        "       coalesce(modules->>'timekeeping', '') = 'true' AS hr_enabled "
        "FROM portal.parishes WHERE org_id = %s "
        "ORDER BY (code IS NULL), code, coalesce(short_name, name)",
        (org_id,),
    )


def set_parish_hr(parish_id: int, org_id: int, enabled: bool) -> None:
    """JSONB merge, org-scoped. org_id in the WHERE clause is the choke point
    that stops a crafted parish_id from another diocese being toggled."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.parishes "
                "SET modules = modules || jsonb_build_object(%s::text, %s::boolean), "
                "    updated_at = now() "
                "WHERE id = %s AND org_id = %s",
                (FEATURE_KEY, enabled, parish_id, org_id),
            )
        conn.commit()


@router.get("/admin/timekeeping/parishes")
def activation_page(request: Request):
    user, org, err = _require_diocese_admin(request)
    if err:
        return err
    parishes = list_parishes_with_hr(org["id"])
    return _render(request, "timekeeping_activation.html", user, {
        "parishes": parishes,
        "current_org": org,
        "enabled_count": sum(1 for p in parishes if p["hr_enabled"]),
    })


@router.post("/admin/timekeeping/parishes")
async def activation_save(request: Request):
    user, org, err = _require_diocese_admin(request)
    if err:
        return err
    form = await request.form()

    # Every row on the page posts a hidden `present_<id>`; only the CHECKED
    # ones post `hr_<id>`. Driving the loop off the hidden field (not the
    # checkbox) is what makes an unchecked box mean "turn OFF" rather than
    # "not submitted, leave alone" -- the standard HTML checkbox trap.
    present = [k[len("present_"):] for k in form.keys() if k.startswith("present_")]

    # One query for the whole current state, not one per row. `current` doubles
    # as the authorization set: an id absent from it belongs to another diocese
    # (or doesn't exist) and is skipped, so a crafted parish_id can't be
    # toggled from here -- set_parish_hr() then re-scopes by org_id anyway.
    current = {str(p["id"]): bool(p["hr_enabled"])
               for p in list_parishes_with_hr(org["id"])}

    changed = 0
    for raw_id in present:
        if raw_id not in current:
            continue
        wanted = form.get(f"hr_{raw_id}") is not None
        if current[raw_id] == wanted:
            continue
        set_parish_hr(int(raw_id), org["id"], wanted)
        changed += 1

    return RedirectResponse(
        f"/admin/timekeeping/parishes?saved={changed}", status_code=303
    )
