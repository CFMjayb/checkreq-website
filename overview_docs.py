"""
overview_docs.py -- the Beacon Overview library: CFM Items > Beacon Overview (26-163, 2026-10-25).

Jay: "a new Beacon Overview item under CFM Items ... stored in Beacon ... all Cornerstone staff." Plain-language overview documents
(What Runs Overnight, the Beacon - Overview manual, The CFM Database, and whatever is added next) live in checkreq.overview_docs, so
Cowork can add or update one with a single upsert and no deploy. Plan: 26-163 Beacon Overview Library\\Plan.md.

    GET /cfm/overview                  the library: cards grouped by category
    GET /cfm/overview/{slug}           one document, read inside a sandboxed iframe (no scripts)
    GET /api/cfm/overview-docs         tool beacon_list_overview_docs
    GET /api/cfm/overview-docs/{slug}  tool beacon_get_overview_doc

Everything is read-only: there is no write route (a document is published by Tools\\publish_overview_docs.py). Who may read: anyone
holding cornerstone_employee at ANY organization (logic/overview_docs/store.py), whatever entity they are working in. The pages send
everyone else to the portal, the two tools answer 403, and an unpublished or unknown document is a 404.

All rules live in logic/overview_docs/ (no FastAPI there). This module only checks access, calls one function and shapes the answer,
so each tool is one named call and can move to an MCP server later without rework.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import cornerstone_mode
from logic.overview_docs import render as ov_render
from logic.overview_docs import store as ov_store

router = APIRouter()

_current_user = None
_current_org = None
_render = None

_ZONE = ZoneInfo("America/New_York")      # the library is CFM-wide, so one house time zone


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


def staff_reach_admin_hub(any_org_roles: set, org_id) -> bool:
    """Should the Administrative Tasks portal tile show for this person just because they are Cornerstone staff? True for
    cornerstone_employee at any organization, except inside a Cornerstone-served client's own entity (Cornerstone Mode), where the
    CFM Items tile is their way to the library and the plain hub tile never showed for this role before."""
    if ov_store.STAFF_ROLE not in (any_org_roles or ()):
        return False
    return not (org_id is not None and cornerstone_mode.is_cornerstone_org(org_id))


def _date_label(ts) -> str:
    """'Oct 25, 2026' in Eastern time."""
    if not isinstance(ts, datetime):
        return ""
    d = ts.astimezone(_ZONE) if ts.tzinfo else ts
    return f"{d:%b} {d.day}, {d.year}"


def _row(d: dict) -> dict:
    return {**d, "updated_label": _date_label(d.get("updated_at"))}


def _where_back(org) -> tuple[str, str]:
    """Back to CFM Items inside a Cornerstone-served client, else to Administrative Tasks."""
    if org and cornerstone_mode.is_cornerstone_org(org["id"]):
        return "/admin/cfm-items", "CFM Items"
    return "/admin", "Administrative Tasks"


def _page_guard(request: Request):
    """(user, redirect): the redirect is set when this person should not be here."""
    user = _current_user(request)
    if not user:
        return None, RedirectResponse("/login")
    if not ov_store.is_cornerstone_staff(user["id"]):
        return user, RedirectResponse("/portal")
    return user, None


def _api_guard(request: Request):
    user = _current_user(request)
    if not user:
        return None, JSONResponse({"error": "Sign in required."}, status_code=401)
    if not ov_store.is_cornerstone_staff(user["id"]):
        return user, JSONResponse({"error": "The Beacon Overview library is for Cornerstone staff."}, status_code=403)
    return user, None


def _list() -> list[dict]:
    try:
        return ov_store.list_docs()
    except psycopg.errors.UndefinedTable:        # migration 103 not applied on this environment yet: an empty shelf, not a 500
        return []


def _get(slug: str):
    try:
        return ov_store.get_doc(slug)
    except psycopg.errors.UndefinedTable:
        return None


# ---------------------------------------------------------------- pages

@router.get("/cfm/overview", response_class=HTMLResponse)
def library_page(request: Request):
    user, redirect = _page_guard(request)
    if redirect:
        return redirect
    docs = [_row(d) for d in _list()]
    groups: list[dict] = []
    for d in docs:                               # list_docs() is already ordered by category
        if not groups or groups[-1]["category"] != d["category"]:
            groups.append({"category": d["category"], "docs": []})
        groups[-1]["docs"].append(d)
    back_url, back_label = _where_back(_current_org(request))
    return _render(request, "overview_library.html", user,
                   {"groups": groups, "doc_count": len(docs), "back_url": back_url, "back_label": back_label})


@router.get("/cfm/overview/{slug}", response_class=HTMLResponse)
def document_page(slug: str, request: Request):
    user, redirect = _page_guard(request)
    if redirect:
        return redirect
    doc = _get(slug)
    if not doc:
        return HTMLResponse("<h1>Not found</h1><p>That document is not in the library.</p>", status_code=404)
    return _render(request, "overview_reader.html", user,
                   {"doc": _row(doc), "srcdoc": ov_render.build_srcdoc(doc, "light")})


# ---------------------------------------------------------------- the two read tools

@router.get("/api/cfm/overview-docs")
def api_list(request: Request):
    """Tool beacon_list_overview_docs: the published documents, no body text."""
    _, denied = _api_guard(request)
    if denied:
        return denied
    return JSONResponse({"docs": [_json_safe(_row(d)) for d in _list()]})


@router.get("/api/cfm/overview-docs/{slug}")
def api_get(slug: str, request: Request):
    """Tool beacon_get_overview_doc: one published document, including the stored body_html exactly as published."""
    _, denied = _api_guard(request)
    if denied:
        return denied
    doc = _get(slug)
    if not doc:
        return JSONResponse({"error": "Not found."}, status_code=404)
    return JSONResponse(_json_safe(_row(doc)))


def _json_safe(d: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}
