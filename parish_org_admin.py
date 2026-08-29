"""
parish_org_admin.py -- Cornerstone Served Parishes, Phase A: the "Manage
Parishes" admin screen (Cornerstone Served Parishes Plan.md, decision 1's
screen). New file per the standing main.py rule.

Lets a diocese admin designate one of its parishes as Cornerstone-served --
turning that on creates a SECOND, linked checkreq.organizations row for the
parish itself (own code, own QBO realm), which is what lets it plug into
the existing AP machinery (Program Areas, Vendors, GL Accounts, Approval
Rules, Check Request, Budget Checks) completely unchanged. The parish's own
portal.parishes registry row (Parish Portal features) is untouched either
way -- see the plan's own architecture diagram.

Gated on setup_admin/beacon_admin, entity-scoped (this screen only ever
shows/acts on the CURRENT diocese's own parishes) -- matches Setup Tables'
own gating precedent (admin_hub.py's docstring: this is the one admin area
that's genuinely entity-scoped data, everything else in that hub is
deliberately or necessarily cross-entity).

IMPORTANT, surfaced in the UI itself, not silently assumed: the QBO OAuth
registration (add_company.py, Secret Manager) for a new parish-org's realm
is a PREREQUISITE step outside Beacon (Cornerstone Served Parishes Plan.md,
decision 3) -- this screen only RECORDS the resulting code + realm ID, it
does not create the QBO connection itself. Also, qbo-mcp-server's
_COMPANY_TO_CODE dict (checkreq_api.py) is still a hardcoded map with no
real "add a new organization" endpoint (a known, separately-flagged gap
from the DSW/DME onboarding, per that project's own CLAUDE.md) -- a newly
created parish-org's /api/checkreq/* calls (Setup Tables workbook,
budget-status, etc.) will 404/error with "Unknown company" until someone
hand-patches that dict, same as DSW/DME needed. Flagged in the UI so this
isn't a silent trap.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
import gcs_client
import org_branding
import rbac
import registry

router = APIRouter()

_current_user = None
_current_org = None
_render = None


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


def _require_setup_admin(request: Request):
    """(user, org, None) when allowed, (None, None, response) when not --
    entity-scoped, matching admin_setup.py's own _require_setup_admin
    exactly (Setup Tables' precedent: the one admin area that's genuinely
    entity-scoped data, not cross-entity by design/necessity)."""
    user = _current_user(request)
    if not user:
        return None, None, RedirectResponse("/login")
    org = _current_org(request)
    org_id = org["id"] if org else None
    if org_id is None or not rbac.user_has_any_role(user["id"], ["setup_admin", "beacon_admin"], org_id=org_id):
        return None, None, JSONResponse({"error": "Setup Admin access required"}, status_code=403)
    return user, org, None


@router.get("/admin/manage-parishes", response_class=HTMLResponse)
def manage_parishes_page(request: Request, error: str = ""):
    user, org, err = _require_setup_admin(request)
    if err:
        return err

    parishes = registry.list_parishes(org["id"])
    org_ids = [p["linked_org_id"] for p in parishes if p.get("linked_org_id")]
    linked_orgs = {}
    if org_ids:
        rows = db.query(
            "SELECT id, code, name, qbo_realm_id, is_active FROM checkreq.organizations WHERE id = ANY(%s)",
            (org_ids,),
        )
        linked_orgs = {r["id"]: r for r in rows}
    for p in parishes:
        p["linked_org"] = linked_orgs.get(p.get("linked_org_id"))

    return _render(request, "manage_parishes.html", user, {
        "parishes": parishes, "current_org": org, "error": error,
    })


@router.post("/admin/manage-parishes/{parish_id}/enable-cornerstone")
async def enable_cornerstone(parish_id: int, request: Request):
    """Creates (or reactivates) the parish's own linked checkreq.
    organizations row and marks it cornerstone_served. Never overwrites an
    existing live link -- a parish that already has one just has this
    screen's toggle be a no-op display state, not a re-create."""
    user, org, err = _require_setup_admin(request)
    if err:
        return err

    parish = registry.get_parish(parish_id, org["id"])
    if not parish:
        return RedirectResponse("/admin/manage-parishes")

    form = await request.form()
    code = (form.get("code") or "").strip()
    realm_id = (form.get("qbo_realm_id") or "").strip()
    if not code or not realm_id:
        return RedirectResponse(
            f"/admin/manage-parishes?error=Code+and+QBO+Realm+ID+are+both+required.", status_code=303
        )

    existing_code = db.query_one("SELECT id FROM checkreq.organizations WHERE code = %s", (code,))
    if existing_code and existing_code["id"] != parish.get("linked_org_id"):
        return RedirectResponse(
            f"/admin/manage-parishes?error=Code+'{code}'+is+already+in+use+by+another+entity.",
            status_code=303,
        )

    if parish.get("linked_org_id"):
        # Reactivating/updating an existing link -- never a second row.
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE checkreq.organizations SET code = %s, qbo_realm_id = %s, "
                    "cornerstone_served = TRUE, is_active = TRUE WHERE id = %s",
                    (code, realm_id, parish["linked_org_id"]),
                )
    else:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO checkreq.organizations (code, name, qbo_realm_id, cornerstone_served, is_active) "
                    "VALUES (%s, %s, %s, TRUE, TRUE) RETURNING id",
                    (code, parish["name"], realm_id),
                )
                new_org_id = cur.fetchone()["id"]
        registry.update_parish(parish_id, org["id"], linked_org_id=new_org_id)

    return RedirectResponse("/admin/manage-parishes?enabled=1", status_code=303)


@router.post("/admin/manage-parishes/{parish_id}/disable-cornerstone")
def disable_cornerstone(parish_id: int, request: Request):
    """Soft only -- deactivates the linked org (is_active=FALSE,
    cornerstone_served left TRUE for history), never deletes it or clears
    linked_org_id. Matches this app's standing never-hard-delete
    philosophy. Re-enabling later reactivates this SAME row rather than
    creating a duplicate (see enable_cornerstone above)."""
    user, org, err = _require_setup_admin(request)
    if err:
        return err

    parish = registry.get_parish(parish_id, org["id"])
    if not parish or not parish.get("linked_org_id"):
        return RedirectResponse("/admin/manage-parishes")

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.organizations SET is_active = FALSE WHERE id = %s",
                (parish["linked_org_id"],),
            )

    return RedirectResponse("/admin/manage-parishes?disabled=1", status_code=303)


@router.post("/admin/manage-parishes/{parish_id}/upload-logo")
async def upload_parish_logo(parish_id: int, request: Request, logo: UploadFile):
    """2026-08-29, Jay: a parish's own logo, shown next to its name in the
    Parish Mode main content area (parish_view.html) -- distinct from a
    diocese's own header logo (admin_setup.py's equivalent route, migration
    051). Same validation/storage pattern (org_branding.py), own GCS
    prefix (parish_logo_path) and own column pair (portal.parishes,
    migration 052) so the two never collide."""
    user, org, err = _require_setup_admin(request)
    if err:
        return err
    parish = registry.get_parish(parish_id, org["id"])
    if not parish:
        return RedirectResponse("/admin/manage-parishes?error=Unknown+parish.", status_code=303)
    content_type = logo.content_type or ""
    if content_type not in org_branding.ALLOWED_LOGO_CONTENT_TYPES:
        return RedirectResponse(
            "/admin/manage-parishes?error=Logo+must+be+a+PNG,+JPEG,+SVG,+or+WebP+image.",
            status_code=303,
        )
    data = await logo.read()
    if len(data) > org_branding.MAX_LOGO_BYTES:
        return RedirectResponse("/admin/manage-parishes?error=Logo+file+is+too+large+(2MB+max).", status_code=303)
    blob_path = org_branding.parish_logo_path(parish_id, content_type)
    gcs_client.upload_bytes(org_branding.LOGO_BUCKET, blob_path, data, content_type)
    registry.update_parish(parish_id, org["id"], logo_gcs_path=blob_path, logo_content_type=content_type)
    return RedirectResponse("/admin/manage-parishes?saved=1", status_code=303)


@router.post("/admin/manage-parishes/{parish_id}/remove-logo")
def remove_parish_logo(parish_id: int, request: Request):
    user, org, err = _require_setup_admin(request)
    if err:
        return err
    parish = registry.get_parish(parish_id, org["id"])
    if not parish:
        return RedirectResponse("/admin/manage-parishes")
    if parish.get("logo_gcs_path"):
        try:
            gcs_client.delete_blob(org_branding.LOGO_BUCKET, parish["logo_gcs_path"])
        except Exception:
            pass  # best-effort -- clearing the DB pointer is what actually stops it from showing
    registry.update_parish(parish_id, org["id"], logo_gcs_path=None, logo_content_type=None)
    return RedirectResponse("/admin/manage-parishes?saved=1", status_code=303)
