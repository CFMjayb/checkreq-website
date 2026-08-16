"""
gl_vendors_reference.py — Cornerstone Served Parishes Plan.md, Phase J
(item 16): two view-only web pages for the GL Chart of Accounts and
Vendors reference data.

Fills in the two placeholder rows admin_setup.py's own SETUP_TABS list has
carried since 2026-08-01 ("GL Accounts (reference)"/"Vendors (reference)",
built: False, url: None) — see that module's docstring/session history for
the full context.

New file per the standing main.py rule. Read-only, no writes at all: both
`checkreq.gl_accounts` and `checkreq.vendors` are synced nightly from QBO
(gl_sync_job.py/vendor_sync_job.py, 26-124 GCP Daily Jobs) and are not
staff-editable anywhere in this app — there is nothing to save here, ever.

2026-08-16, Jay: queries `checkreq.gl_accounts`/`checkreq.vendors` directly
via db.py rather than qbo-mcp-server's REST endpoints (the plan's original
literal wording) — faster, no network hop, and no live-QBO-budget-lookup
latency for a screen that's just browsing reference data. Same pattern
main.py's own New Request GL-account/vendor picker already uses. The one
real tradeoff, accepted deliberately: `checkreq.gl_accounts` has no stored
`annual_budget` column (that figure only ever existed via a live QBO
Budget call qbo_mcp_client.get_gl_accounts() made internally) — every row
here reports `annual_budget: None`, which the template already renders as
"—", same as an account with no budget configured at all.

Gated identically to admin_setup.py's GL Mapping screen (_require_setup_
admin): setup_admin, entity-scoped to the current session org — the
closest existing precedent for "view financial reference data" in this
app (Setup Tables is the one admin area with genuinely per-entity data;
GL Mapping's own screen already displays this exact gl_accounts data,
joined against Program Areas).
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

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


def _require_view_access(request: Request):
    """(user, org, None) when allowed, (None, None, response) when not —
    same shape and same gate as admin_setup.py's own _require_setup_admin:
    setup_admin, entity-scoped. No selected entity means no access — never
    let org_id=None fall through to rbac.py's "check every org" meaning."""
    user = _current_user(request)
    if not user:
        return None, None, RedirectResponse("/login")
    org = _current_org(request)
    org_id = org["id"] if org else None
    if org_id is None or not rbac.user_has_role(user["id"], "setup_admin", org_id=org_id):
        return None, None, JSONResponse({"error": "Setup Admin access required"}, status_code=403)
    return user, org, None


@router.get("/admin/setup/gl-accounts", response_class=HTMLResponse)
def gl_accounts_view_page(request: Request):
    user, org, err = _require_view_access(request)
    if err:
        return err

    rows = db.query(
        "SELECT account_number, account_name, account_type, is_active "
        "FROM checkreq.gl_accounts WHERE org_id = %s ORDER BY account_number",
        (org["id"],),
    )
    for r in rows:
        r["annual_budget"] = None
    return _render(request, "admin_gl_accounts_view.html", user, {
        "rows": rows, "error": None, "current_org": org,
    })


@router.get("/admin/setup/vendors", response_class=HTMLResponse)
def vendors_view_page(request: Request):
    user, org, err = _require_view_access(request)
    if err:
        return err

    rows = db.query(
        "SELECT display_name, company_name, address, email, is_active "
        "FROM checkreq.vendors WHERE org_id = %s ORDER BY display_name",
        (org["id"],),
    )
    return _render(request, "admin_vendors_view.html", user, {
        "rows": rows, "error": None, "current_org": org,
    })
