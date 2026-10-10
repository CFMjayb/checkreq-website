"""
parish_portal_admin.py -- the two pop-ups on Administrative Tasks > Manage Parishes (Jay, 2026-10-10):

  Portal    the whole "Turn on and accounts" section that used to live inside each parish's People & Giving Settings page: the three
            switches (People and membership, Giving, Parishioner self-service) plus the two diocese exceptions, the member sign-in link name
            and the QuickBooks accounts. It lives here because a diocese admin cannot reach a parish's Settings page until People & Giving is
            already on for that parish, so the first switch-on had to be done by a script.
  Finance   the two diocese-side finance fields (Middendorf loan GL account, SMA direct-debit status), moved out of the grid, where an
            inline form per row was unusable.

Each button is an ordinary link to a page that shows the form, so it works without scripts. With scripts, static/js/manage_parishes.js opens
the same form in a pop-up (?fragment=1 returns just the form). Saving is a normal form post that comes back to Manage Parishes with a banner.

Nothing here has its own rules. Saving the Portal form calls donor_roles.settings_update, the one service that decides who may change which
setting (the switches and the member link need the diocese's activation ability, a Beacon Admin; the QuickBooks accounts also allow Finance), so
a Setup Admin sees the values but cannot change the switches. The Finance form posts to the existing route in parish_finance.py. The gate is the
Manage Parishes gate, passed in by parish_org_admin: Setup Admin or Beacon Admin at the CURRENT diocese, and the parish must belong to it.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import db
import donor_portal_login as PL
import donor_roles
import registry
from donor_core import DonorError

router = APIRouter()
FLASH_KEY = "mp_flash"
_current_user = None
_current_org = None
_render = None
_gate = None


def register(app, *, current_user, current_org, render, gate) -> None:
    global _current_user, _current_org, _render, _gate
    _current_user, _current_org, _render, _gate = current_user, current_org, render, gate
    app.include_router(router)


def donor_available() -> bool:
    """Is Donor Management in this database at all? Production does not have the donor schema until its migrations are applied, and the Portal
    button must not appear (or fail) there. Fails closed."""
    try:
        row = db.query_one("SELECT to_regclass('donor.parish_settings') AS t")
        return bool(row and row["t"])
    except Exception:
        return False


def states_for(parish_ids: list[int]) -> dict:
    """{parish_id: {people, giving, portal, slug}} for the grid's small status pills. {} when Donor Management is not in this database."""
    if not parish_ids or not donor_available():
        return {}
    try:
        rows = db.query("SELECT parish_id, people_enabled, giving_enabled, portal_enabled, portal_slug FROM donor.parish_settings "
                        "WHERE parish_id = ANY(%s)", (parish_ids,))
    except Exception:
        return {}
    return {r["parish_id"]: {"people": r["people_enabled"], "giving": r["giving_enabled"], "portal": r["portal_enabled"], "slug": r["portal_slug"]}
            for r in rows}


def _flash(request: Request, kind: str, text: str) -> None:
    request.session[FLASH_KEY] = [kind, text[:300]]


def _target(request: Request, parish_id: int):
    """(user, org, parish, None) or (None, None, None, response). A parish of another diocese is 'not found', never a hint."""
    user, org, err = _gate(request)
    if err:
        return None, None, None, err
    parish = registry.get_parish(parish_id, org["id"])
    if not parish:
        return None, None, None, HTMLResponse("That parish was not found.", status_code=404)
    return user, org, parish, None


def _page(request: Request, user, fragment: bool, template: str, data: dict):
    if fragment:
        return _render(request, template, user, data)
    return _render(request, "manage_parish_popup_page.html", user, {**data, "fragment_template": template})


@router.get("/admin/manage-parishes/{parish_id}/portal", response_class=HTMLResponse)
def portal_get(parish_id: int, request: Request, fragment: int = 0):
    user, org, parish, resp = _target(request, parish_id)
    if resp:
        return resp
    if not donor_available():
        return HTMLResponse("Donor Management is not set up in this database yet.", status_code=404)
    ctx = donor_roles.build_ctx(user, parish)
    s = ctx.settings
    can_activate = bool(ctx.can("parish.activate"))
    data = {"parish": parish, "s": s, "can_activate": can_activate, "can_accounts": can_activate or bool(ctx.can("funds.manage")),
            "suggested": s.get("portal_slug") or PL.default_slug(parish["id"]), "host": request.url.netloc, "title": f"Portal: {parish['name']}"}
    return _page(request, user, bool(fragment), "manage_parish_portal.html", data)


@router.post("/admin/manage-parishes/{parish_id}/portal")
async def portal_post(parish_id: int, request: Request):
    user, org, parish, resp = _target(request, parish_id)
    if resp:
        return resp
    if not donor_available():
        return HTMLResponse("Donor Management is not set up in this database yet.", status_code=404)
    form = await request.form()
    ctx = donor_roles.build_ctx(user, parish)
    try:
        donor_roles.settings_update(ctx, donor_roles.activation_changes(form))
        _flash(request, "ok", f"Portal settings saved for {parish['name']}.")
    except DonorError as e:
        _flash(request, "err", e.message)
    return RedirectResponse("/admin/manage-parishes", status_code=303)


@router.get("/admin/manage-parishes/{parish_id}/finance", response_class=HTMLResponse)
def finance_get(parish_id: int, request: Request, fragment: int = 0):
    user, org, parish, resp = _target(request, parish_id)
    if resp:
        return resp
    return _page(request, user, bool(fragment), "manage_parish_finance.html", {"parish": parish, "title": f"Finance: {parish['name']}"})
