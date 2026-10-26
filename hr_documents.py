"""
hr_documents.py -- 26-162 Phase 2: upload and view the documents of an HR case (Onboarding & Offboarding).

    POST /hr/onboarding/case/{id}/documents/upload       one file, for a checklist item (item_key) or by kind of document (doc_type)
    GET  /hr/onboarding/case/{id}/documents/{doc}/file   the file itself (?download=1 forces a download, ?copy=1 reads the second copy)

Same gate and same access as the rest of the case page (hr_cases._gate: hr_lifecycle on for the entity, and hr_admin, benefits_admin
or beacon_admin there). Every rule -- where a file goes, who may open it, the two copies, the checks on the file -- is in
logic/hr_lifecycle/documents.py; this module only reads the request, calls one function and shapes the answer.

BOTH ROUTES ARE PLAIN `def`, not `async def`: Starlette runs a plain route in its worker thread pool, so the SharePoint calls (several
per upload) never block the server's event loop (the 2026-10-09 stall; see the 26-129 CLAUDE.md entry on step 3 of the stall fix). The upload takes its file
as an UploadFile parameter for the same reason: no `await request.form()` is needed.

A file is only ever served back through upload_guard.serve_headers: typed from its own bytes, shown inline only if it is a PDF or an
image, anything else as a download; the response is private and never cached (these are personnel documents).
"""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

import upload_guard
from logic.hr_lifecycle import documents
from logic.hr_lifecycle.errors import HrError

router = APIRouter()

_gate = None
_allowed_org_ids = None


def register(app, *, gate, allowed_org_ids) -> None:
    """`gate(request)` -> (user, org, None) or (None, None, response); `allowed_org_ids(user_id)` -> the entities the person may use.
    Both belong to hr_cases and are handed over here, never imported (hr_cases imports this module)."""
    global _gate, _allowed_org_ids
    _gate, _allowed_org_ids = gate, allowed_org_ids
    app.include_router(router)


def _back(case_id: int, anchor: str = "", **msg) -> RedirectResponse:
    q = urlencode({k: v for k, v in msg.items() if v})
    return RedirectResponse(f"/hr/onboarding/case/{case_id}" + (f"?{q}" if q else "") + (f"#{anchor}" if anchor else ""), status_code=303)


@router.post("/hr/onboarding/case/{case_id}/documents/upload")
def upload_post(request: Request, case_id: int, file: UploadFile = File(None), doc_type: str = Form(""), item_key: str = Form("")):
    user, org, err = _gate(request)
    if err:
        return err
    anchor = f"item-{item_key}" if item_key else "hrDocs"
    if file is None or not (file.filename or "").strip():
        return _back(case_id, anchor, error="Choose a file to upload.")
    data = file.file.read(documents.MAX_BYTES + 1)
    try:
        r = documents.file_document(
            case_id, _allowed_org_ids(user["id"]) or [org["id"]], user_id=user["id"], filename=file.filename, data=data,
            declared_type=file.content_type, doc_type_key=(doc_type or "").strip() or None, item_key=(item_key or "").strip() or None)
    except HrError as e:
        return _back(case_id, anchor, error=e.message)
    where = "in both places" if r["copies"] > 1 else "in the HR Administration folder"
    tail = " It replaces the earlier one (that one is kept under Earlier versions)." if r["replaced"] else ""
    return _back(case_id, anchor, saved=f"Filed: {r['doc_label']} ({r['file_name']}), {where}.{tail}")


@router.get("/hr/onboarding/case/{case_id}/documents/{doc_id}/file")
def file_get(request: Request, case_id: int, doc_id: int, download: int = 0, copy: int = 0):
    user, org, err = _gate(request)
    if err:
        return err
    try:
        data, d = documents.read_file(case_id, doc_id, _allowed_org_ids(user["id"]) or [org["id"]], user["id"], copy=bool(copy))
    except HrError as e:
        status = 403 if e.code == "refused" else e.status
        return Response(e.message, status_code=status, media_type="text/plain", headers={"Cache-Control": "private, no-store"})
    media, disposition = upload_guard.serve_headers(data, d["file_name"], force_download=bool(download))
    return Response(data, media_type=media, headers={"Content-Disposition": disposition, "Cache-Control": "private, no-store",
                                                     "X-Robots-Tag": "noindex, nofollow"})
