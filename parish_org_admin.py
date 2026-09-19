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

from urllib.parse import quote

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
import gcs_client
import org_branding
import rbac
import registry
import sharepoint_client
import upload_guard

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


def _create_sharepoint_folder_for_parish(org: dict, parish: dict) -> str | None:
    """2026-09-13: closes the gap Jay hit hand-creating SharePoint folders
    for every parish added this session (101 Redemption/Locust Point, 102
    Transfiguration/Braddock Heights, 103 Living Grace/Urbana, 104 Church
    on the Square/Canton) -- going forward, adding a parish here also
    creates its matching document folder.

    Folder naming confirmed against a LIVE DioNet listing before writing
    this (not guessed): every one of the 4 parishes above already has a
    real folder, and each one's exact name is reconstructable byte-for-byte
    from just this app's own `name`/`city` columns -- e.g. parish 421
    (code "102") has `sp_folder_path = "102. Transfiguration Episcopal
    Church, Braddock Heights"`, which is exactly `"{code}. {name}, {city}"`.
    Checked all 6 of the 101-106 parishes this way, zero mismatches; also
    confirmed every EDOM parish carrying a `code` also carries a `city` (0
    counterexamples), but the no-city case is still handled explicitly
    below since a future parish could be added without one --
    parish_documents.py's own `_split_folder_label()` already treats a
    folder with no comma (no city) as a real, valid shape.

    EDOM-specific for now, by construction rather than a special case:
    this only fires when the org actually has `sp_parish_hostname`
    configured (DioNet), which is the same guard resolve_parish_folder()
    already uses. Checked DME's own organizations row directly before
    writing this: sp_parish_hostname is NULL there -- DME's congregation
    document story is Realm-sourced info (26-138), not a DioNet-style
    per-parish SharePoint folder at all, so there is genuinely no
    equivalent convention to reuse for DME today. If DME (or a future
    diocese) ever gets one, this same function works unchanged the moment
    that org's own sp_parish_hostname/sp_parish_site_path/
    sp_parish_library_folder are populated -- no per-diocese branching
    needed here.

    Returns None on success (and stamps sp_folder_path/sp_folder_resolved_at
    on the parish row, so parish_documents.py's very first page view for
    this parish doesn't need its own live-resolve pass), or an error string
    on failure -- callers must treat a failure as non-blocking, same
    fail-open philosophy this app already applies to attachment archival
    (see main.py's own archive_warning pattern): a SharePoint hiccup must
    never roll back or block the parish record itself, which is why this
    is called AFTER create_parish() has already committed, not inside the
    same transaction."""
    if not org.get("sp_parish_hostname"):
        return None  # this diocese has no DioNet-style parish folder convention configured (e.g. DME) -- nothing to create
    code = (parish.get("code") or "").strip()
    if not code:
        return None  # no code yet -- creating a folder without one would guess at a name nobody can resolve back to later; stays a manual follow-up

    name = parish["name"]
    city = (parish.get("city") or "").strip()
    folder_name = f"{code}. {name}, {city}" if city else f"{code}. {name}"

    try:
        token = sharepoint_client.get_access_token()
        site_id = sharepoint_client.get_site_id(
            token, org["sp_parish_hostname"], org["sp_parish_site_path"]
        )
        parent = org.get("sp_parish_library_folder") or ""
        sharepoint_client.ensure_folder(token, site_id, parent, folder_name)
    except Exception as exc:
        return str(exc)

    registry.update_parish(parish["id"], org["id"], sp_folder_path=folder_name)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.parishes SET sp_folder_resolved_at = NOW() WHERE id = %s",
                (parish["id"],),
            )
    return None


@router.get("/admin/manage-parishes", response_class=HTMLResponse)
def manage_parishes_page(request: Request, error: str = ""):
    user, org, err = _require_setup_admin(request)
    if err:
        return err

    # include_inactive=True (2026-09-13): a closed/merged parish sets
    # is_active=FALSE (see set_parish_status below) -- without this, the
    # moment someone closes a parish here it would silently vanish from
    # this exact screen, with no way to look at it again or reopen it.
    parishes = registry.list_parishes(org["id"], include_inactive=True)
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


@router.post("/admin/manage-parishes/add")
async def add_parish(request: Request):
    """2026-09-13, Jay: there was no UI anywhere to add a new parish to
    portal.parishes -- every one of EDOM's ~95 / DME's ~65 rows was created
    by a one-off Python script calling registry.create_parish() directly.
    Entity-scoped like everything else on this screen: the new row always
    lands in the CURRENTLY SELECTED org, never a form-supplied one.

    The "External CRM ID" field is a deliberately GENERIC label/form-field
    name over the same portal.parishes.databank_contact_id column the
    existing per-row edit control (below) already exposes -- for an EDOM
    parish this is a Databank contact id, for a DME parish it would be a
    Realm contact id, two unrelated vendor systems sharing one column
    (migration 034 predates DME/Realm entirely). Confirmed via the actual
    26-124/26-138 code before deciding NOT to unify this with
    portal.parishes.realm_church_id (26-138, migration 050): DME's own
    sync scripts already have a separate, working match mechanism against
    realm_church_id and never touch databank_contact_id at all, and
    migration 050's own comment describes realm_church_id as playing "the
    same role" as databank_contact_id deliberately, not accidentally --
    unifying them into one field here would just break 26-138's existing
    convention for no real benefit. So this form's "External CRM ID"
    value is stored into databank_contact_id regardless of which org is
    selected; a DME parish's Realm id (if ever needed here) would still
    need its own separate field, not built in this pass."""
    user, org, err = _require_setup_admin(request)
    if err:
        return err

    form = await request.form()
    name = (form.get("name") or "").strip()
    code = (form.get("code") or "").strip() or None
    city = (form.get("city") or "").strip() or None
    external_crm_id = (form.get("external_crm_id") or "").strip() or None

    if not name:
        return RedirectResponse(
            "/admin/manage-parishes?error=Parish+name+is+required.", status_code=303
        )

    if code:
        existing_code = db.query_one(
            "SELECT id FROM portal.parishes WHERE org_id = %s AND code = %s",
            (org["id"], code),
        )
        if existing_code:
            return RedirectResponse(
                f"/admin/manage-parishes?error=Code+'{code}'+is+already+in+use+by+another+parish+in+this+entity.",
                status_code=303,
            )

    parish = registry.create_parish(
        org["id"], name, code=code, city=city, databank_contact_id=external_crm_id,
    )

    folder_warning = _create_sharepoint_folder_for_parish(org, parish)
    redirect_url = "/admin/manage-parishes?added=1"
    if folder_warning:
        redirect_url += f"&folder_warning={quote(folder_warning)}"
    return RedirectResponse(redirect_url, status_code=303)


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


_PARISH_STATUSES = {"active", "closed", "merged"}


@router.post("/admin/manage-parishes/{parish_id}/status")
async def set_parish_status(parish_id: int, request: Request):
    """2026-09-13, Jay: `portal.parishes.status` has existed since migration
    023 (default 'active', its own column comment already said "a closed/
    merged parish's history stays") but nothing ever set it to anything
    else, and no screen could change it. Real trigger: parish 271 (Church
    of the Advent - Federal Hill, code 014) is genuinely closed -- it was
    one of the 6 parishes with no databank_contact_id, which is exactly why
    it never matched a real Databank church record.

    Deliberately ties status to the ALREADY-existing is_active flag rather
    than adding a second, independent on/off switch: is_active is what
    congregation_sync_job.py (26-124's nightly clergy refresh),
    load_lay_leadership_from_relationships.py, and this screen's own
    default listing all actually check today. 'active' -> is_active=TRUE;
    'closed'/'merged' -> is_active=FALSE, so a closed parish automatically
    stops being nightly-synced and stops showing in every other screen's
    normal (active-only) listing, with zero changes needed anywhere else.
    Never hard-deleted, never removed from this screen (see
    include_inactive=True above) -- matches this app's standing philosophy.

    'merged' does not yet record WHAT a parish merged into -- that's a
    real, separate feature (a merged_into_parish_id reference + probably
    surfacing a redirect/notice on the old parish's own pages) not asked
    for here; this just gives the status value a place to live so it's not
    lost, same spirit as 'closed' before this session did anything with it."""
    user, org, err = _require_setup_admin(request)
    if err:
        return err
    parish = registry.get_parish(parish_id, org["id"])
    if not parish:
        return RedirectResponse("/admin/manage-parishes")
    form = await request.form()
    status = (form.get("status") or "").strip()
    if status not in _PARISH_STATUSES:
        return RedirectResponse(
            "/admin/manage-parishes?error=Status+must+be+one+of:+active,+closed,+merged.", status_code=303
        )
    registry.update_parish(parish_id, org["id"], status=status, is_active=(status == "active"))
    return RedirectResponse("/admin/manage-parishes?saved=1", status_code=303)


@router.post("/admin/manage-parishes/{parish_id}/databank-contact-id")
async def set_databank_contact_id(parish_id: int, request: Request):
    """2026-09-13, Jay: this column existed in the schema (migration 034)
    and was already READ by congregation_sync_job.py (26-124, nightly
    clergy refresh) and the one-off lay-leadership relationship import, but
    had no admin UI anywhere -- every value on file today (86 of 88 active
    EDOM parishes) was set by a one-off backfill script matching a Databank
    export, never through Beacon itself. A parish added to Beacon going
    forward with this left blank silently gets NO congregation info, ever,
    with no error anywhere -- this closes that gap. Blank clears it back to
    NULL (registry.update_parish already treats None as a real value, same
    as every other nullable field this screen edits)."""
    user, org, err = _require_setup_admin(request)
    if err:
        return err
    parish = registry.get_parish(parish_id, org["id"])
    if not parish:
        return RedirectResponse("/admin/manage-parishes")
    form = await request.form()
    value = (form.get("databank_contact_id") or "").strip() or None
    registry.update_parish(parish_id, org["id"], databank_contact_id=value)
    return RedirectResponse("/admin/manage-parishes?saved=1", status_code=303)


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
            "/admin/manage-parishes?error=Logo+must+be+a+PNG,+JPEG,+or+WebP+image.",
            status_code=303,
        )
    data = await logo.read()
    if len(data) > org_branding.MAX_LOGO_BYTES:
        return RedirectResponse("/admin/manage-parishes?error=Logo+file+is+too+large+(2MB+max).", status_code=303)
    # H2/M12 (Security Assessment 2026-09-19): bytes must really be an
    # allowed raster format; stored type is the sniffed one.
    ok, sniffed = upload_guard.sniff_allowed(data, content_type)
    if not ok or sniffed not in org_branding.ALLOWED_LOGO_CONTENT_TYPES:
        return RedirectResponse(
            "/admin/manage-parishes?error=Logo+file+contents+must+be+a+real+PNG,+JPEG,+or+WebP+image.",
            status_code=303,
        )
    content_type = sniffed
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
