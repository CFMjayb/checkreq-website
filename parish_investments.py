"""
parish_investments.py -- the "Investments" item under Finance (2026-10-20, DME Beacon Documents site).

Jay: a NEW parish role, "Finance-Investments" (migration 095), "will see the Finance menu and see Investments
under it, and they will see the Investments folder"; CFM and diocesan roles (Beacon Admin, Setup Admin, ...) see
all of it. This module is just the page and its download route -- who may see it is decided in ONE place,
parish_documents.can_view_investments(), shared with the Documents routes so the two can never disagree.

  GET /parish-finance/investments                      the parish's Investments folder as an expand/collapse tree
  GET /parish-finance/investments/download/{rel_path}  view/download one file (always under Investments/)

Read-only on purpose: the diocese files statements here (SharePoint, the "Investments" subfolder of the parish's
folder on the Beacon Documents site); the parish only reads them. Like every other document area, SharePoint is
the one source of truth -- a live listing on each page view, no copy in Postgres.

The Finance page (parish_finance.py) shows the link and registers these routes from its own register(), so no
main.py change is needed; the page-level "can this person see Finance at all" gate stays Parish Finance, which
the Finance-Investments role also grants (parish_roles.ROLE_ALSO_GRANTS).
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import parish_documents as pd
import path_guard
import sharepoint_client

router = APIRouter()

_render = None


def register(app, *, render) -> None:
    global _render
    _render = render
    app.include_router(router)


def _is_investments_path(rel_path: str) -> bool:
    """True only for a path whose FIRST segment is the Investments folder (loose spelling, same rule as the
    Documents gate). This route can therefore never serve HRPayroll, Parish Files, or anything else."""
    return pd._restricted_key((rel_path or "").split("/", 1)[0]) == pd._restricted_key(pd.INVESTMENTS_SUBFOLDER)


@router.get("/parish-finance/investments", response_class=HTMLResponse)
def investments_page(request: Request):
    user, parish, org, err = pd._parish_context(request)
    if err:
        return err
    if not pd.can_view_investments(user, parish):
        return _render(request, "parish_investments.html", user, {"parish": parish, "can_view": False})
    folder, folder_stale = pd.get_parish_folder(org, parish)
    files, list_error = [], None
    if folder:
        try:
            files = pd.list_investments(org, folder)
        except RuntimeError as exc:
            list_error = str(exc)
    return _render(request, "parish_investments.html", user, {
        "parish": parish, "can_view": True, "folder": folder, "folder_stale": folder_stale,
        "files": files, "list_error": list_error,
    })


@router.get("/parish-finance/investments/download/{rel_path:path}")
def investments_download(rel_path: str, request: Request, download: int = 0):
    user, parish, org, err = pd._parish_context(request)
    if err:
        return err
    if not _is_investments_path(rel_path) or not pd.can_view_investments(user, parish):
        return JSONResponse({"error": "Not found"}, status_code=404)
    folder, _stale = pd.get_parish_folder(org, parish)
    if not folder:
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        data = pd.download_parish_file(org, folder, rel_path)
    except (RuntimeError, path_guard.PathGuardError):
        return JSONResponse({"error": "Not found"}, status_code=404)
    filename = rel_path.rsplit("/", 1)[-1]
    media_type, disposition = sharepoint_client.serve_headers(data, filename, bool(download))
    return Response(content=data, media_type=media_type, headers={"Content-Disposition": disposition})
