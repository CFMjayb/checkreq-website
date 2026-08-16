"""
cornerstone_documents.py -- "From Cornerstone" document section (Cornerstone
Served Parishes, Phase B follow-up, 2026-08-16). Jay's own spec, verbatim:
"when you are in Cornerstone mode, you need to see a 'From Cornerstone'
file section that has files in the CFM Sharepoint 'Services Team -
Documents/Church Files' -- each served entity will have a folder under
this that starts with their Code, for example, AHP. Under this will be a
folder 'Beacon Documents' and then two folders 'From Parish' and 'To
Parish', under 'To Parish' there will be read-only and read-write
(self-explanatory) -- write this new module for inclusion in the Documents
feature."

This is a DIFFERENT SharePoint site/tenant than parish_documents.py's
DioNet-based Documents feature -- see cfm_sharepoint_client.py's own
docstring for why that needed a separate credential/token cache, not a
shared one. Folder resolution is keyed on the served parish-org's OWN code
(checkreq.organizations.code, e.g. "AHP"/"MEC" -- the same code shown in
the entity switcher/Cornerstone Mode picker), NOT the DioNet parish's own
numeric code (portal.parishes.code) -- these are two unrelated numbering
schemes on two unrelated SharePoint sites.

"For inclusion in the Documents feature" (Jay's own words) means this
module owns the SharePoint access logic + its own upload/delete/download
action routes, but does NOT get its own top-level page/tile -- its listing
functions are called directly by parish_documents.py's existing
`/parish-documents` route/template (the same page Cornerstone Mode's
"Document Library" tile already points at), appearing as an additional
section only when the currently-selected entity is itself a
Cornerstone-served parish-org.

Permissions, per Jay's own "self-explanatory" framing, applied from the
Cornerstone-Mode staffer's perspective (in Cornerstone Mode, CFM staff IS
this entity's own back office -- same reasoning parish_documents.py's own
_can_edit_parish_docs() extension used for a cornerstone_employee grant):
  - From Parish: full manage (this entity's own outbox to send things TO
    CFM/the diocese).
  - To Parish / Read-Only: view + download only, no upload/delete -- CFM-
    diocese-managed content the entity can only see.
  - To Parish / Read-Write: full manage -- content the entity can also
    edit/collaborate on.
Gated the same way parish_documents.py's own edit check is: beacon_admin,
or a live cornerstone_employee grant at this specific served org.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response

import rbac
import cornerstone_mode
import cfm_sharepoint_client as cfm_sp
import sharepoint_client  # reused unchanged -- generic Graph mechanics only

router = APIRouter()

_current_user = None
_current_org = None

CHURCH_FILES_ROOT = "Church Files"
BEACON_DOCS_SUBFOLDER = "Beacon Documents"
FROM_PARISH_SUBFOLDER = "From Parish"
TO_PARISH_SUBFOLDER = "To Parish"
# 2026-08-16, Jay: live inspection of the real AHP folder (the one entity
# that already had this structure set up) found the actual convention is
# HYPHENATED -- "Read-Only"/"Read-Write" -- not "Read Only"/"Read Write" as
# first assumed. Fixed to match the real existing folder, not the other way
# around (Jay's own call: "you can change folder names if needed" refers to
# fixing any OTHER inconsistent folder, e.g. a stray unhyphenated one this
# module itself lazily created before this fix -- not renaming AHP's).
TO_PARISH_READONLY = "Read-Only"
TO_PARISH_READWRITE = "Read-Write"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # matches parish_documents.py's own limit

_TO_PARISH_RW_AREA = f"{TO_PARISH_SUBFOLDER}/{TO_PARISH_READWRITE}"


def register(app, *, current_user, current_org) -> None:
    """No `render` needed -- this module contributes data to
    parish_documents.html via parish_documents.py's own route, not a page
    of its own; it only needs to register its upload/delete/download
    ACTION routes."""
    global _current_user, _current_org
    _current_user, _current_org = current_user, current_org
    app.include_router(router)


def _site_and_token() -> tuple[str, str]:
    token = cfm_sp.get_access_token()
    site_id = sharepoint_client.get_site_id(token, cfm_sp.HOSTNAME, cfm_sp.SITE_PATH)
    return token, site_id


def resolve_entity_folder(org_code: str) -> str | None:
    """Prefix-match on the served org's own code (e.g. "AHP") against
    Church Files' immediate subfolders -- Jay: "a folder under this that
    starts with their Code." Always re-checks live, same "never trust a
    cached name" lesson parish_documents.py's own resolve_parish_folder()
    already learned the hard way (a stale cached name silently 404s if the
    real folder is later renamed in SharePoint)."""
    token, site_id = _site_and_token()
    entries = sharepoint_client.list_folder(token, site_id, CHURCH_FILES_ROOT)
    folder_names = [e["name"] for e in entries if e["is_folder"]]
    code = (org_code or "").strip().upper()
    if not code:
        return None
    return next((n for n in folder_names if n.upper().startswith(code)), None)


def _beacon_docs_root(entity_folder: str) -> str:
    return f"{CHURCH_FILES_ROOT}/{entity_folder}/{BEACON_DOCS_SUBFOLDER}"


def _listed(entries: list[dict], area: str) -> list[dict]:
    return [dict(e, rel_path=f"{area}/{e['name']}") for e in entries]


def list_from_parish(entity_folder: str) -> list[dict]:
    token, site_id = _site_and_token()
    entries = sharepoint_client.list_folder(token, site_id, f"{_beacon_docs_root(entity_folder)}/{FROM_PARISH_SUBFOLDER}")
    return _listed(entries, FROM_PARISH_SUBFOLDER)


def list_to_parish_readonly(entity_folder: str) -> list[dict]:
    token, site_id = _site_and_token()
    area = f"{TO_PARISH_SUBFOLDER}/{TO_PARISH_READONLY}"
    entries = sharepoint_client.list_folder(token, site_id, f"{_beacon_docs_root(entity_folder)}/{area}")
    return _listed(entries, area)


def list_to_parish_readwrite(entity_folder: str) -> list[dict]:
    token, site_id = _site_and_token()
    entries = sharepoint_client.list_folder(token, site_id, f"{_beacon_docs_root(entity_folder)}/{_TO_PARISH_RW_AREA}")
    return _listed(entries, _TO_PARISH_RW_AREA)


def upload_from_parish(entity_folder: str, filename: str, data: bytes, content_type: str) -> None:
    token, site_id = _site_and_token()
    entity_root = f"{CHURCH_FILES_ROOT}/{entity_folder}"
    sharepoint_client.ensure_folder(token, site_id, entity_root, BEACON_DOCS_SUBFOLDER)
    base = _beacon_docs_root(entity_folder)
    sharepoint_client.ensure_folder(token, site_id, base, FROM_PARISH_SUBFOLDER)
    sharepoint_client.upload_bytes(token, site_id, f"{base}/{FROM_PARISH_SUBFOLDER}", filename, data, content_type)


def upload_to_parish_readwrite(entity_folder: str, filename: str, data: bytes, content_type: str) -> None:
    token, site_id = _site_and_token()
    entity_root = f"{CHURCH_FILES_ROOT}/{entity_folder}"
    sharepoint_client.ensure_folder(token, site_id, entity_root, BEACON_DOCS_SUBFOLDER)
    base = _beacon_docs_root(entity_folder)
    sharepoint_client.ensure_folder(token, site_id, base, TO_PARISH_SUBFOLDER)
    to_parish_base = f"{base}/{TO_PARISH_SUBFOLDER}"
    sharepoint_client.ensure_folder(token, site_id, to_parish_base, TO_PARISH_READWRITE)
    sharepoint_client.upload_bytes(token, site_id, f"{to_parish_base}/{TO_PARISH_READWRITE}", filename, data, content_type)


# ── Auth ──────────────────────────────────────────────────────────────────

def can_edit(user: dict, org_id: int) -> bool:
    if rbac.user_has_role(user["id"], "beacon_admin", org_id=None):
        return True
    return rbac.user_has_role(user["id"], "cornerstone_employee", org_id)


def _cornerstone_context(request: Request):
    """(user, org, entity_folder, error_response). error_response is None
    on success. A None entity_folder with no error means this org's own
    Church Files folder hasn't been matched yet (a real, expected state
    for a newly-served entity not yet set up on the SharePoint side)."""
    user = _current_user(request)
    if not user:
        return None, None, None, RedirectResponse("/login")
    org = _current_org(request)
    if not org or not cornerstone_mode.is_cornerstone_org(org["id"]):
        return None, None, None, RedirectResponse("/portal")
    entity_folder = resolve_entity_folder(org["code"])
    return user, org, entity_folder, None


# ── Routes (action routes only -- listing is called directly by
#    parish_documents.py's own page route) ──────────────────────────────────

@router.post("/cornerstone-documents/upload")
async def cornerstone_documents_upload(request: Request, file: UploadFile = File(...), target: str = Form("from_parish")):
    user, org, entity_folder, err = _cornerstone_context(request)
    if err:
        return err
    if not can_edit(user, org["id"]):
        return JSONResponse({"error": "You don't have permission to add files here."}, status_code=403)
    if not entity_folder:
        return RedirectResponse("/parish-documents?error=nofolder", status_code=303)
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        return RedirectResponse("/parish-documents?error=toolarge", status_code=303)
    content_type = file.content_type or "application/octet-stream"
    if target == "to_parish_rw":
        upload_to_parish_readwrite(entity_folder, file.filename, data, content_type)
    else:
        upload_from_parish(entity_folder, file.filename, data, content_type)
    return RedirectResponse("/parish-documents?uploaded=1", status_code=303)


@router.post("/cornerstone-documents/delete")
async def cornerstone_documents_delete(request: Request):
    user, org, entity_folder, err = _cornerstone_context(request)
    if err:
        return err
    if not can_edit(user, org["id"]):
        return JSONResponse({"error": "You don't have permission to remove files here."}, status_code=403)
    form = await request.form()
    rel_path = (form.get("rel_path") or "").strip()
    # Only From Parish / To Parish/Read-Write are ever deletable -- never
    # Read-Only, even if a request were crafted by hand to try (mirrors
    # parish_documents.py's own delete-restriction pattern exactly).
    if not (rel_path.startswith(f"{FROM_PARISH_SUBFOLDER}/") or rel_path.startswith(f"{_TO_PARISH_RW_AREA}/")):
        return JSONResponse({"error": "You can't remove that file."}, status_code=403)
    if entity_folder and rel_path:
        token, site_id = _site_and_token()
        sharepoint_client.delete_file(token, site_id, f"{_beacon_docs_root(entity_folder)}/{rel_path}")
    return RedirectResponse("/parish-documents?deleted=1", status_code=303)


@router.get("/cornerstone-documents/download/{rel_path:path}")
def cornerstone_documents_download(rel_path: str, request: Request):
    user, org, entity_folder, err = _cornerstone_context(request)
    if err:
        return err
    if not entity_folder:
        return JSONResponse({"error": "Not found"}, status_code=404)
    token, site_id = _site_and_token()
    try:
        data = sharepoint_client.download_bytes(token, site_id, f"{_beacon_docs_root(entity_folder)}/{rel_path}")
    except RuntimeError:
        return JSONResponse({"error": "Not found"}, status_code=404)
    filename = rel_path.rsplit("/", 1)[-1]
    return Response(content=data, media_type="application/octet-stream",
                     headers={"Content-Disposition": f'inline; filename="{filename}"'})
