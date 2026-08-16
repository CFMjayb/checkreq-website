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

Folder conventions, REVISED 2026-08-08 per Jay's direct feedback into an
explicit 3-subfolder protocol (the original cut only had "root minus Parish
Files" as an implicit read-only area, plus Parish Files -- no dedicated
diocese-managed subfolder, and no upload-TO-the-diocese path at all):
  - "Read Only Files" (READONLY_SUBFOLDER) -- diocese-managed, read-only to
    the parish. Admin uploads (/admin/parish-documents) now go HERE, created
    lazily on first admin upload. Backward-compatible: any pre-existing
    LOOSE file sitting at a parish's folder ROOT (e.g. St Philips' property
    deed, present before this convention existed) still shows in the same
    "read-only" listing, merged with this subfolder's contents -- nothing
    already there had to be moved.
  - "Parish Files" (EDITABLE_SUBFOLDER) -- unchanged from the original cut:
    the parish's own library, created lazily on first parish upload. Gated
    on parish_documents/parish_admin for that parish, or beacon_admin.
  - "For the Diocese" (TO_DIOCESE_SUBFOLDER) -- NEW: a parish uploads here
    to send something TO the diocese (distinct from Parish Files, which is
    the parish's own reference library, not an outbox). Same upload/delete
    permission as Parish Files; also readable by diocesan admins via
    /admin/parish-documents so staff can retrieve what was submitted.
  - Diocese-wide Resource Library = a single "Resource Library" folder at
    the DioNet drive root (org.sp_resource_library_folder), also created
    lazily. Read-only for everyone; admin upload/manage gated setup_admin/
    beacon_admin (admin_hub.py card). Confirmed live, 2026-08-08: no more
    natural existing SharePoint location for this exists at DioNet (the
    drive root holds only the 95 per-parish folders, "200. Test Folder",
    two special-fund folders, and this one) -- current placement stands.

Every listed file/folder now carries its own "rel_path" (relative to the
parish's resolved root, or the library root) -- the exact address delete/
download need. This replaced an earlier area+filename addressing scheme
that couldn't express "this file lives inside a named subfolder" once a
third subfolder (For the Diocese) existed alongside the root-loose legacy
files and Parish Files.

Folder-name resolution (resolve_parish_folder): primary match is the
parish's own `code` column as a "{code}. " prefix (confirmed live: all 95
real EDOM parishes already have this, and it matches the real SharePoint
folder names exactly -- e.g. code "090" -> "090. St Philips Episcopal
Church, Annapolis"). Fuzzy fallback (difflib against "{name}") covers a gap
or a not-yet-code-matched parish. Result is WRITTEN to
portal.parishes.sp_folder_path/sp_folder_resolved_at for admin-page
display, but as of 2026-08-08 is no longer READ to skip the live check --
Jay: "you need to make sure you read the sharepoint folder on each entry to
the screen as I made some folder changes and the screen did not update."
(root cause: a permanently-cached folder NAME silently 404s -- returning an
empty listing, not an error -- the moment the real folder is renamed/moved
in SharePoint outside the app; re-resolving live every page view is the
fix). force= is kept as a parameter for backward compatibility with the
admin "Re-resolve" button's call site, but every code path now behaves as
force=True regardless of the value passed.
"""
from __future__ import annotations

import difflib
import re

from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response

import db
import rbac
import registry
import parish_roles
import parish_mode
import cornerstone_mode
import sharepoint_client

router = APIRouter()

_current_user = None
_current_org = None
_render = None

READONLY_SUBFOLDER = "Read Only Files"
EDITABLE_SUBFOLDER = "Parish Files"
TO_DIOCESE_SUBFOLDER = "For the Diocese"
_SPECIAL_SUBFOLDERS = {READONLY_SUBFOLDER, EDITABLE_SUBFOLDER, TO_DIOCESE_SUBFOLDER}
_FUZZY_THRESHOLD = 0.72
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25MB -- generous for scanned documents


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
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


def resolve_parish_folder(org: dict, parish: dict, force: bool = True) -> str | None:
    """Returns the matched SharePoint folder NAME (relative to
    org.sp_parish_library_folder), or None if no org config / no match.
    Always re-checks live (see module docstring, 2026-08-08) -- `force` is
    kept only so the admin "Re-resolve" button's call site doesn't need to
    change; every caller behaves identically regardless of its value now."""
    if not org or not org.get("sp_parish_hostname"):
        return None

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
# Every entry gets a "rel_path" -- the exact address delete/download need,
# relative to the parish's own resolved root. Root-loose legacy files get
# rel_path=name (unchanged addressing); subfolder contents get
# rel_path="{Subfolder}/{name}".

def list_readonly(org: dict, folder_name: str) -> list[dict]:
    """Read Only Files subfolder + any pre-existing loose file sitting at
    the parish's root (backward compat -- see module docstring)."""
    token, site_id = _site_and_token(org)
    base = _parish_root(org, folder_name)
    out = []
    for e in sharepoint_client.list_folder(token, site_id, base):
        if e["name"] in _SPECIAL_SUBFOLDERS:
            continue
        e = dict(e, rel_path=e["name"])
        out.append(e)
    for e in sharepoint_client.list_folder(token, site_id, f"{base}/{READONLY_SUBFOLDER}"):
        e = dict(e, rel_path=f"{READONLY_SUBFOLDER}/{e['name']}")
        out.append(e)
    return out


def list_editable(org: dict, folder_name: str) -> list[dict]:
    token, site_id = _site_and_token(org)
    base = _parish_root(org, folder_name)
    out = []
    for e in sharepoint_client.list_folder(token, site_id, f"{base}/{EDITABLE_SUBFOLDER}"):
        e = dict(e, rel_path=f"{EDITABLE_SUBFOLDER}/{e['name']}")
        out.append(e)
    return out


def list_to_diocese(org: dict, folder_name: str) -> list[dict]:
    token, site_id = _site_and_token(org)
    base = _parish_root(org, folder_name)
    out = []
    for e in sharepoint_client.list_folder(token, site_id, f"{base}/{TO_DIOCESE_SUBFOLDER}"):
        e = dict(e, rel_path=f"{TO_DIOCESE_SUBFOLDER}/{e['name']}")
        out.append(e)
    return out


def upload_readonly(org: dict, folder_name: str, filename: str, data: bytes, content_type: str) -> None:
    """Admin upload -- now goes into the Read Only Files subfolder (created
    lazily), never the parish's folder root directly."""
    token, site_id = _site_and_token(org)
    base = _parish_root(org, folder_name)
    sharepoint_client.ensure_folder(token, site_id, base, READONLY_SUBFOLDER)
    sharepoint_client.upload_bytes(token, site_id, f"{base}/{READONLY_SUBFOLDER}", filename, data, content_type)


def upload_editable(org: dict, folder_name: str, filename: str, data: bytes, content_type: str) -> None:
    token, site_id = _site_and_token(org)
    base = _parish_root(org, folder_name)
    sharepoint_client.ensure_folder(token, site_id, base, EDITABLE_SUBFOLDER)
    sharepoint_client.upload_bytes(token, site_id, f"{base}/{EDITABLE_SUBFOLDER}", filename, data, content_type)


def upload_to_diocese(org: dict, folder_name: str, filename: str, data: bytes, content_type: str) -> None:
    token, site_id = _site_and_token(org)
    base = _parish_root(org, folder_name)
    sharepoint_client.ensure_folder(token, site_id, base, TO_DIOCESE_SUBFOLDER)
    sharepoint_client.upload_bytes(token, site_id, f"{base}/{TO_DIOCESE_SUBFOLDER}", filename, data, content_type)


def delete_parish_file(org: dict, folder_name: str, rel_path: str) -> None:
    token, site_id = _site_and_token(org)
    sharepoint_client.delete_file(token, site_id, f"{_parish_root(org, folder_name)}/{rel_path}")


def download_parish_file(org: dict, folder_name: str, rel_path: str) -> bytes:
    token, site_id = _site_and_token(org)
    return sharepoint_client.download_bytes(token, site_id, f"{_parish_root(org, folder_name)}/{rel_path}")


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
    # 2026-08-16 (Jay, Cornerstone Mode tiles): a genuine cornerstone_employee
    # at this parish's own linked AP org has real, full working access to it
    # -- the whole point of the grant -- so they can manage its documents
    # too, same as a native parish_admin/parish_documents holder can.
    # linked_org_id is only ever set for a Cornerstone-served parish, so
    # this is a no-op for every ordinary (non-served) parish.
    if parish.get("linked_org_id") and rbac.user_has_role(user["id"], "cornerstone_employee", parish["linked_org_id"]):
        return True
    return parish_roles.user_has_any_parish_role(
        user["id"], ["parish_documents", "parish_admin"], parish["id"]
    )


def _parish_context(request: Request):
    """(user, parish, org, error_response). error_response is None on
    success. Mirrors the small-duplication pattern parish_mode.py's own
    docstring documents (admin_hub.py's _ADMIN_TASK_ROLE_KEYS) -- this
    module deliberately calls parish_mode.effective_parish_mode() (a READ)
    rather than importing anything that would create a cycle.

    2026-08-16 (Jay, live test, Cornerstone Mode): "Document Library, and
    Resources would show [under Cornerstone Mode] -- correct?" A diocesan
    staffer working inside a served parish's own AP org (current_org is
    that parish's linked_org_id) has no active Parish Mode preview and no
    native parish role -- effective_parish_mode() alone would send them to
    /parish-view for nothing. Falls back to resolving the SAME parish via
    cornerstone_mode.get_parish_for_org() when that's the case, so
    Cornerstone Mode reaches its own parish's documents directly."""
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
    readonly_files, editable_files, to_diocese_files, list_error = [], [], [], None
    if folder:
        try:
            readonly_files = list_readonly(org, folder)
            editable_files = list_editable(org, folder)
            to_diocese_files = list_to_diocese(org, folder)
        except RuntimeError as exc:
            list_error = str(exc)
    return _render(request, "parish_documents.html", user, {
        "parish": parish, "folder": folder,
        "readonly_files": readonly_files, "editable_files": editable_files,
        "to_diocese_files": to_diocese_files,
        "can_edit": _can_edit_parish_docs(user, parish),
        "list_error": list_error,
    })


@router.post("/parish-documents/upload")
async def parish_documents_upload(request: Request, file: UploadFile = File(...), target: str = Form("parish")):
    """target='parish' -> Parish Files (the parish's own library);
    target='diocese' -> For the Diocese (an outbox to the diocesan office).
    Same permission gate either way."""
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
    content_type = file.content_type or "application/octet-stream"
    if target == "diocese":
        upload_to_diocese(org, folder, file.filename, data, content_type)
    else:
        upload_editable(org, folder, file.filename, data, content_type)
    return RedirectResponse("/parish-documents?uploaded=1", status_code=303)


@router.post("/parish-documents/delete")
async def parish_documents_delete(request: Request):
    user, parish, org, err = _parish_context(request)
    if err:
        return err
    if not _can_edit_parish_docs(user, parish):
        return JSONResponse({"error": "You don't have permission to remove files here."}, status_code=403)
    form = await request.form()
    rel_path = (form.get("rel_path") or "").strip()
    # A parish may only delete its OWN uploads (Parish Files / For the
    # Diocese) -- never Read Only Files, even if a request were crafted by
    # hand to try.
    if not (rel_path.startswith(f"{EDITABLE_SUBFOLDER}/") or rel_path.startswith(f"{TO_DIOCESE_SUBFOLDER}/")):
        return JSONResponse({"error": "You can't remove that file."}, status_code=403)
    folder = resolve_parish_folder(org, parish)
    if folder and rel_path:
        delete_parish_file(org, folder, rel_path)
    return RedirectResponse("/parish-documents?deleted=1", status_code=303)


@router.get("/parish-documents/download/{rel_path:path}")
def parish_documents_download(rel_path: str, request: Request):
    user, parish, org, err = _parish_context(request)
    if err:
        return err
    folder = resolve_parish_folder(org, parish)
    if not folder:
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        data = download_parish_file(org, folder, rel_path)
    except RuntimeError:
        return JSONResponse({"error": "Not found"}, status_code=404)
    filename = rel_path.rsplit("/", 1)[-1]
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
    selected, files, to_diocese_files, folder, list_error = None, [], [], None, None
    if parish_id:
        selected = next((p for p in parishes if p["id"] == parish_id), None)
        if selected:
            org = db.query_one("SELECT * FROM checkreq.organizations WHERE id = %s", (selected["org_id"],))
            folder = resolve_parish_folder(org, selected)
            if folder:
                try:
                    files = list_readonly(org, folder)
                    to_diocese_files = list_to_diocese(org, folder)
                except RuntimeError as exc:
                    list_error = str(exc)
    return _render(request, "admin_parish_documents.html", user, {
        "parishes": parishes, "selected": selected, "files": files,
        "to_diocese_files": to_diocese_files,
        "folder": folder, "list_error": list_error,
    })


@router.post("/admin/parish-documents/{parish_id}/upload")
async def admin_parish_documents_upload(parish_id: int, request: Request, file: UploadFile = File(...)):
    """Admin uploads always go into Read Only Files -- see module docstring."""
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
    """Handles a rel_path from either section shown on this page -- Read
    Only Files (staff cleaning up their own uploads) or For the Diocese
    (staff clearing a processed parish submission). Never Parish Files --
    that's the parish's own area, not shown or manageable here."""
    user, err = _require_docs_admin(request)
    if err:
        return err
    parish = db.query_one("SELECT * FROM portal.parishes WHERE id = %s", (parish_id,))
    if not parish:
        return RedirectResponse("/admin/parish-documents")
    form = await request.form()
    rel_path = (form.get("rel_path") or "").strip()
    org = db.query_one("SELECT * FROM checkreq.organizations WHERE id = %s", (parish["org_id"],))
    folder = resolve_parish_folder(org, parish)
    if folder and rel_path:
        delete_parish_file(org, folder, rel_path)
    return RedirectResponse(f"/admin/parish-documents?parish_id={parish_id}&deleted=1", status_code=303)


@router.get("/admin/parish-documents/{parish_id}/download/{rel_path:path}")
def admin_parish_documents_download(parish_id: int, rel_path: str, request: Request):
    user, err = _require_docs_admin(request)
    if err:
        return err
    parish = db.query_one("SELECT * FROM portal.parishes WHERE id = %s", (parish_id,))
    if not parish:
        return JSONResponse({"error": "Not found"}, status_code=404)
    org = db.query_one("SELECT * FROM checkreq.organizations WHERE id = %s", (parish["org_id"],))
    folder = resolve_parish_folder(org, parish)
    if not folder:
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        data = download_parish_file(org, folder, rel_path)
    except RuntimeError:
        return JSONResponse({"error": "Not found"}, status_code=404)
    filename = rel_path.rsplit("/", 1)[-1]
    return Response(content=data, media_type="application/octet-stream",
                     headers={"Content-Disposition": f'inline; filename="{filename}"'})


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
