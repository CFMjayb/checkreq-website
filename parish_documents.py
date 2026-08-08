"""
parish_documents.py — Parish Portal S5: document archive (both areas) +
diocese-wide resource library (Parish Portal Plan.md Section 2/4, PP-501-507).

New file per NFR-11 / the standing main.py rule. Register()-injection
pattern, same as every other Parish Portal module.

DESIGN CALL, made after live-verifying the real SharePoint content (see
migration 029's header comment for the full detail): SharePoint itself is
the one and only source of truth for every file here -- there is NO Postgres
mirror/metadata table for document content. Every listing is a live Graph
API call. This is a deliberate deviation from Parish Portal Plan.md's own
`portal.documents` schema sketch (Section 2), made because this codebase has
been burned repeatedly by exactly the class of bug a mirror table invites --
two stores of the same fact drifting apart (Firestore-vs-Postgres across
26-102/26-107/26-124's whole 2026-06/07 saga; the global_approvers.org_id/
cross-entity-CFO-notification incident). A real parish's SharePoint folder
already has real, diocese-managed files sitting in it today (property deeds,
payroll subfolders) -- mirroring that into a second table on day one would
either need an ongoing sync job (another thing to drift) or would show a
stale/incomplete picture the moment anyone touched a file outside this app.
Browsing live costs one extra Graph round-trip per page view, which is a
fine trade for a low-traffic internal tool. If Jay ever wants per-document
metadata (categories, view-tracking, targeted per-doc notifications) that
SharePoint itself can't hold, THAT would be the moment to add a real
metadata table keyed by SharePoint's own driveItem id -- not before.

Folder conventions (established here, not pre-existing -- confirmed live,
2026-08-08, that no parish folder had any consistent read-only/editable
split already):
  - "Read-only" area = a parish's resolved folder's own root contents
    (whatever the diocese has already put there, or uploads via
    /admin/parish-documents going forward), MINUS the "Parish Files"
    subfolder itself (which has its own separate area, not nested display).
  - "Editable" area = the "Parish Files" subfolder, created lazily on first
    upload (never pre-created) -- gated on the parish_documents or
    parish_admin role for that specific parish, or beacon_admin.
  - Diocese-wide Resource Library = a single "Resource Library" folder at
    the DioNet drive root (org.sp_resource_library_folder), also created
    lazily. Read-only for everyone; admin upload/manage gated setup_admin/
    beacon_admin (admin_hub.py card).

Folder-name resolution (resolve_parish_folder): primary match is the
parish's own `code` column as a "{code}. " prefix (confirmed live: all 95
real EDOM parishes already have this, and it matches the real SharePoint
folder names exactly -- e.g. code "090" -> "090. St Philips Episcopal
Church, Annapolis"). Fuzzy fallback (difflib against "{name}") covers a gap
or a not-yet-code-matched parish. Result is cached on
portal.parishes.sp_folder_path (registry.update_parish) so normal page
loads never re-list+re-fuzzy-match; force=True (the admin "Re-resolve"
button) always re-checks live.
"""
from __future__ import annotations

import difflib
import re

from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response

import db
import rbac
import registry
import parish_roles
import parish_mode
import sharepoint_client

router = APIRouter()

_current_user = None
_render = None

EDITABLE_SUBFOLDER = "Parish Files"
_FUZZY_THRESHOLD = 0.72
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25MB -- generous for scanned documents


def register(app, *, current_user, render) -> None:
    global _current_user, _render
    _current_user, _render = current_user, render
    app.include_router(router)


# ── Folder resolution ────────────────────────────────────────────────────────

def _normalize(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _fuzzy_match_folder(parish: dict, folder_names: list[str]) -> str | None:
    target = _normalize(parish["name"])
    best, best_score = None, 0.0
    for name in folder_names:
        stripped = re.sub(r"^\d+\.\s*", "", name)
        score = difflib.SequenceMatcher(None, _normalize(stripped), target).ratio()
        if score > best_score:
            best, best_score = name, score
    return best if best_score >= _FUZZY_THRESHOLD else None


def _site_and_token(org: dict) -> tuple[str, str]:
    token = sharepoint_client.get_access_token()
    site_id = sharepoint_client.get_site_id(token, org["sp_parish_hostname"], org["sp_parish_site_path"])
    return token, site_id


def resolve_parish_folder(org: dict, parish: dict, force: bool = False) -> str | None:
    """Returns the matched SharePoint folder NAME (relative to
    org.sp_parish_library_folder), or None if no org config / no match.
    Cached on portal.parishes.sp_folder_path unless force=True."""
    if not org or not org.get("sp_parish_hostname"):
        return None
    if not force and parish.get("sp_folder_path"):
        return parish["sp_folder_path"]

    token, site_id = _site_and_token(org)
    root = org.get("sp_parish_library_folder") or ""
    entries = sharepoint_client.list_folder(token, site_id, root)
    folder_names = [e["name"] for e in entries if e["is_folder"]]

    match = None
    code = (parish.get("code") or "").strip()
    if code:
        prefix = f"{code}. "
        match = next((n for n in folder_names if n.startswith(prefix)), None)
    if not match:
        match = _fuzzy_match_folder(parish, folder_names)

    if match:
        registry.update_parish(parish["id"], parish["org_id"], sp_folder_path=match)
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE portal.parishes SET sp_folder_resolved_at = NOW() WHERE id = %s",
                    (parish["id"],),
                )
    return match


def _parish_root(org: dict, folder_name: str) -> str:
    root = (org.get("sp_parish_library_folder") or "").strip("/")
    return f"{root}/{folder_name}" if root else folder_name


# ── Parish document areas ────────────────────────────────────────────────────

def list_readonly(org: dict, folder_name: str) -> list[dict]:
    token, site_id = _site_and_token(org)
    entries = sharepoint_client.list_folder(token, site_id, _parish_root(org, folder_name))
    return [e for e in entries if e["name"] != EDITABLE_SUBFOLDER]


def list_editable(org: dict, folder_name: str) -> list[dict]:
    token, site_id = _site_and_token(org)
    base = f"{_parish_root(org, folder_name)}/{EDITABLE_SUBFOLDER}"
    return sharepoint_client.list_folder(token, site_id, base)


def upload_readonly(org: dict, folder_name: str, filename: str, data: bytes, content_type: str) -> None:
    token, site_id = _site_and_token(org)
    sharepoint_client.upload_bytes(token, site_id, _parish_root(org, folder_name), filename, data, content_type)


def upload_editable(org: dict, folder_name: str, filename: str, data: bytes, content_type: str) -> None:
    token, site_id = _site_and_token(org)
    base = _parish_root(org, folder_name)
    sharepoint_client.ensure_folder(token, site_id, base, EDITABLE_SUBFOLDER)
    sharepoint_client.upload_bytes(token, site_id, f"{base}/{EDITABLE_SUBFOLDER}", filename, data, content_type)


def delete_readonly(org: dict, folder_name: str, filename: str) -> None:
    token, site_id = _site_and_token(org)
    sharepoint_client.delete_file(token, site_id, f"{_parish_root(org, folder_name)}/{filename}")


def delete_editable(org: dict, folder_name: str, filename: str) -> None:
    token, site_id = _site_and_token(org)
    sharepoint_client.delete_file(token, site_id, f"{_parish_root(org, folder_name)}/{EDITABLE_SUBFOLDER}/{filename}")


def download_parish_file(org: dict, folder_name: str, area: str, filename: str) -> bytes:
    token, site_id = _site_and_token(org)
    base = _parish_root(org, folder_name)
    path = f"{base}/{filename}" if area == "readonly" else f"{base}/{EDITABLE_SUBFOLDER}/{filename}"
    return sharepoint_client.download_bytes(token, site_id, path)


# ── Resource library (diocese-wide) ──────────────────────────────────────────

def _library_root(org: dict) -> str:
    return (org.get("sp_resource_library_folder") or "Resource Library").strip("/")


def list_library(org: dict) -> list[dict]:
    token, site_id = _site_and_token(org)
    return sharepoint_client.list_folder(token, site_id, _library_root(org))


def upload_library(org: dict, filename: str, data: bytes, content_type: str) -> None:
    token, site_id = _site_and_token(org)
    root = _library_root(org)
    sharepoint_client.ensure_folder(token, site_id, "", root)
    sharepoint_client.upload_bytes(token, site_id, root, filename, data, content_type)


def delete_library(org: dict, filename: str) -> None:
    token, site_id = _site_and_token(org)
    sharepoint_client.delete_file(token, site_id, f"{_library_root(org)}/{filename}")


def download_library_file(org: dict, filename: str) -> bytes:
    token, site_id = _site_and_token(org)
    return sharepoint_client.download_bytes(token, site_id, f"{_library_root(org)}/{filename}")


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _can_edit_parish_docs(user: dict, parish: dict) -> bool:
    if rbac.user_has_role(user["id"], "beacon_admin", org_id=None):
        return True
    return parish_roles.user_has_any_parish_role(
        user["id"], ["parish_documents", "parish_admin"], parish["id"]
    )


def _parish_context(request: Request):
    """(user, parish, org, error_response). error_response is None on
    success. Mirrors the small-duplication pattern parish_mode.py's own
    docstring documents (admin_hub.py's _ADMIN_TASK_ROLE_KEYS) -- this
    module deliberately calls parish_mode.effective_parish_mode() (a READ)
    rather than importing anything that would create a cycle."""
    user = _current_user(request)
    if not user:
        return None, None, None, RedirectResponse("/login")
    parish, _is_preview = parish_mode.effective_parish_mode(request, user)
    if not parish:
        return None, None, None, RedirectResponse("/parish-view")
    org = db.query_one("SELECT * FROM checkreq.organizations WHERE id = %s", (parish["org_id"],))
    return user, parish, org, None


def _require_docs_admin(request: Request):
    user = _current_user(request)
    if not user:
        return None, RedirectResponse("/login")
    if not rbac.user_has_any_role(user["id"], ["setup_admin", "beacon_admin"], org_id=None):
        return None, JSONResponse({"error": "Setup Admin or Beacon Admin access required"}, status_code=403)
    return user, None


# ── Parish-facing routes ──────────────────────────────────────────────────────

@router.get("/parish-documents", response_class=HTMLResponse)
def parish_documents_page(request: Request):
    user, parish, org, err = _parish_context(request)
    if err:
        return err
    folder = resolve_parish_folder(org, parish)
    readonly_files, editable_files, list_error = [], [], None
    if folder:
        try:
            readonly_files = list_readonly(org, folder)
            editable_files = list_editable(org, folder)
        except RuntimeError as exc:
            list_error = str(exc)
    return _render(request, "parish_documents.html", user, {
        "parish": parish, "folder": folder,
        "readonly_files": readonly_files, "editable_files": editable_files,
        "can_edit": _can_edit_parish_docs(user, parish),
        "list_error": list_error,
    })


@router.post("/parish-documents/upload")
async def parish_documents_upload(request: Request, file: UploadFile = File(...)):
    user, parish, org, err = _parish_context(request)
    if err:
        return err
    if not _can_edit_parish_docs(user, parish):
        return JSONResponse({"error": "You don't have permission to add files here."}, status_code=403)
    folder = resolve_parish_folder(org, parish)
    if not folder:
        return RedirectResponse("/parish-documents?error=nofolder", status_code=303)
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        return RedirectResponse("/parish-documents?error=toolarge", status_code=303)
    upload_editable(org, folder, file.filename, data, file.content_type or "application/octet-stream")
    return RedirectResponse("/parish-documents?uploaded=1", status_code=303)


@router.post("/parish-documents/delete")
async def parish_documents_delete(request: Request):
    user, parish, org, err = _parish_context(request)
    if err:
        return err
    if not _can_edit_parish_docs(user, parish):
        return JSONResponse({"error": "You don't have permission to remove files here."}, status_code=403)
    form = await request.form()
    filename = (form.get("filename") or "").strip()
    folder = resolve_parish_folder(org, parish)
    if folder and filename:
        delete_editable(org, folder, filename)
    return RedirectResponse("/parish-documents?deleted=1", status_code=303)


@router.get("/parish-documents/download/{area}/{filename:path}")
def parish_documents_download(area: str, filename: str, request: Request):
    user, parish, org, err = _parish_context(request)
    if err:
        return err
    if area not in ("readonly", "editable"):
        return JSONResponse({"error": "Unknown area"}, status_code=400)
    folder = resolve_parish_folder(org, parish)
    if not folder:
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        data = download_parish_file(org, folder, area, filename)
    except RuntimeError:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return Response(content=data, media_type="application/octet-stream",
                     headers={"Content-Disposition": f'inline; filename="{filename}"'})


@router.get("/resource-library", response_class=HTMLResponse)
def resource_library_page(request: Request):
    """Read-only for everyone signed in -- not gated on Parish Mode itself,
    since diocesan staff browsing outside a parish preview should also be
    able to see it. Resolves the org from the current parish view if one is
    active, else falls back to the first active org with a configured
    library (today, always EDOM -- the only tenant)."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    parish, _ = parish_mode.effective_parish_mode(request, user)
    if parish:
        org = db.query_one("SELECT * FROM checkreq.organizations WHERE id = %s", (parish["org_id"],))
    else:
        org = db.query_one(
            "SELECT * FROM checkreq.organizations WHERE sp_resource_library_folder IS NOT NULL "
            "AND is_active ORDER BY id LIMIT 1"
        )
    files, list_error = [], None
    if org and org.get("sp_parish_hostname"):
        try:
            files = list_library(org)
        except RuntimeError as exc:
            list_error = str(exc)
    return _render(request, "resource_library.html", user, {"files": files, "org": org, "list_error": list_error})


@router.get("/resource-library/download/{filename:path}")
def resource_library_download(filename: str, request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    parish, _ = parish_mode.effective_parish_mode(request, user)
    org = (db.query_one("SELECT * FROM checkreq.organizations WHERE id = %s", (parish["org_id"],))
           if parish else
           db.query_one("SELECT * FROM checkreq.organizations WHERE sp_resource_library_folder IS NOT NULL "
                        "AND is_active ORDER BY id LIMIT 1"))
    if not org or not org.get("sp_parish_hostname"):
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        data = download_library_file(org, filename)
    except RuntimeError:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return Response(content=data, media_type="application/octet-stream",
                     headers={"Content-Disposition": f'inline; filename="{filename}"'})


# ── Diocesan admin routes ─────────────────────────────────────────────────────

@router.get("/admin/parish-documents", response_class=HTMLResponse)
def admin_parish_documents_page(request: Request, parish_id: int = 0):
    user, err = _require_docs_admin(request)
    if err:
        return err
    parishes = registry.list_all_parishes()
    selected, files, folder, list_error = None, [], None, None
    if parish_id:
        selected = next((p for p in parishes if p["id"] == parish_id), None)
        if selected:
            org = db.query_one("SELECT * FROM checkreq.organizations WHERE id = %s", (selected["org_id"],))
            folder = resolve_parish_folder(org, selected)
            if folder:
                try:
                    files = list_readonly(org, folder)
                except RuntimeError as exc:
                    list_error = str(exc)
    return _render(request, "admin_parish_documents.html", user, {
        "parishes": parishes, "selected": selected, "files": files,
        "folder": folder, "list_error": list_error,
    })


@router.post("/admin/parish-documents/{parish_id}/upload")
async def admin_parish_documents_upload(parish_id: int, request: Request, file: UploadFile = File(...)):
    user, err = _require_docs_admin(request)
    if err:
        return err
    parish = db.query_one("SELECT * FROM portal.parishes WHERE id = %s", (parish_id,))
    if not parish:
        return RedirectResponse("/admin/parish-documents")
    org = db.query_one("SELECT * FROM checkreq.organizations WHERE id = %s", (parish["org_id"],))
    folder = resolve_parish_folder(org, parish)
    if folder:
        data = await file.read()
        if len(data) <= MAX_UPLOAD_BYTES:
            upload_readonly(org, folder, file.filename, data, file.content_type or "application/octet-stream")
    return RedirectResponse(f"/admin/parish-documents?parish_id={parish_id}&uploaded=1", status_code=303)


@router.post("/admin/parish-documents/{parish_id}/delete")
async def admin_parish_documents_delete(parish_id: int, request: Request):
    user, err = _require_docs_admin(request)
    if err:
        return err
    parish = db.query_one("SELECT * FROM portal.parishes WHERE id = %s", (parish_id,))
    if not parish:
        return RedirectResponse("/admin/parish-documents")
    form = await request.form()
    filename = (form.get("filename") or "").strip()
    org = db.query_one("SELECT * FROM checkreq.organizations WHERE id = %s", (parish["org_id"],))
    folder = resolve_parish_folder(org, parish)
    if folder and filename:
        delete_readonly(org, folder, filename)
    return RedirectResponse(f"/admin/parish-documents?parish_id={parish_id}&deleted=1", status_code=303)


@router.post("/admin/parish-documents/{parish_id}/resolve")
def admin_parish_documents_resolve(parish_id: int, request: Request):
    """Force a fresh live Graph lookup (a code correction, or a SharePoint
    folder that didn't exist yet last time) -- bypasses the cache."""
    user, err = _require_docs_admin(request)
    if err:
        return err
    parish = db.query_one("SELECT * FROM portal.parishes WHERE id = %s", (parish_id,))
    if parish:
        org = db.query_one("SELECT * FROM checkreq.organizations WHERE id = %s", (parish["org_id"],))
        resolve_parish_folder(org, parish, force=True)
    return RedirectResponse(f"/admin/parish-documents?parish_id={parish_id}", status_code=303)


@router.post("/admin/parish-documents/{parish_id}/override")
async def admin_parish_documents_override(parish_id: int, request: Request):
    """Manual override -- a fuzzy match (or even a code match) can point at
    the wrong folder; staff can always type the exact real folder name here,
    and it wins permanently (resolve_parish_folder only re-resolves when the
    cache is empty or force=True, so a manual value is never silently
    replaced by a later auto-match)."""
    user, err = _require_docs_admin(request)
    if err:
        return err
    form = await request.form()
    value = (form.get("folder_path") or "").strip() or None
    parish = db.query_one("SELECT org_id FROM portal.parishes WHERE id = %s", (parish_id,))
    if parish:
        registry.update_parish(parish_id, parish["org_id"], sp_folder_path=value)
    return RedirectResponse(f"/admin/parish-documents?parish_id={parish_id}", status_code=303)


@router.get("/admin/resource-library", response_class=HTMLResponse)
def admin_resource_library_page(request: Request):
    user, err = _require_docs_admin(request)
    if err:
        return err
    org = db.query_one(
        "SELECT * FROM checkreq.organizations WHERE sp_parish_hostname IS NOT NULL "
        "AND is_active ORDER BY id LIMIT 1"
    )
    files, list_error = [], None
    if org:
        try:
            files = list_library(org)
        except RuntimeError as exc:
            list_error = str(exc)
    return _render(request, "admin_resource_library.html", user, {"files": files, "org": org, "list_error": list_error})


@router.post("/admin/resource-library/upload")
async def admin_resource_library_upload(request: Request, file: UploadFile = File(...)):
    user, err = _require_docs_admin(request)
    if err:
        return err
    org = db.query_one(
        "SELECT * FROM checkreq.organizations WHERE sp_parish_hostname IS NOT NULL "
        "AND is_active ORDER BY id LIMIT 1"
    )
    if org:
        data = await file.read()
        if len(data) <= MAX_UPLOAD_BYTES:
            upload_library(org, file.filename, data, file.content_type or "application/octet-stream")
    return RedirectResponse("/admin/resource-library?uploaded=1", status_code=303)


@router.post("/admin/resource-library/delete")
async def admin_resource_library_delete(request: Request):
    user, err = _require_docs_admin(request)
    if err:
        return err
    org = db.query_one(
        "SELECT * FROM checkreq.organizations WHERE sp_parish_hostname IS NOT NULL "
        "AND is_active ORDER BY id LIMIT 1"
    )
    form = await request.form()
    filename = (form.get("filename") or "").strip()
    if org and filename:
        delete_library(org, filename)
    return RedirectResponse("/admin/resource-library?deleted=1", status_code=303)
