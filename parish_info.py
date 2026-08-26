"""
parish_info.py — "Parish Information" page (2026-08-08 UI feedback batch).
Replaces the plain Diocese/City/Served Tier/Status card that used to sit at
the top of parish_view.html (Jay: "the big white pill in the main area is
junk... can't you use the standard API to poll the contact information?")
with a real, live Databank lookup keyed on the parish's own
databank_contact_id (populated the same session this page was requested).

Clergy listing added 2026-08-25, once 26-120's ClergyDirectory vendor
blocker closed: the church-level /contacts lookup below already returns
Databank's "internal_contact_id" field, which turns out to be the SAME id
space ClergyDirectory calls "webacct"/"churchwebacct" -- confirmed live,
not assumed (contactid 206 / internal_contact_id 3050504 for All Hallows
Parish, Davidsonville, matches its churchwebacct in the live clergy data
exactly). So the existing contact lookup bridges straight into a clergy
lookup with no new matching/sync step needed.

Rebuilt onto congregation.py 2026-08-26 (Parish Portal S6, Jay: "Build the
real S6 now... make sure your postgres tables have the ability to store
all leadership contacts for a parish"). This page no longer calls
databank_mcp_client.get_clergy_for_church() directly -- congregation.py
owns the live-fetch/cache/fallback logic (portal.congregation_cache,
migration 049), so a Databank hiccup degrades to "data as of X" instead of
an empty page. Also gained a "Report a correction" submit, reusing
parish_requests.py's existing submission path (kind='general_request',
same pattern as Parish Finance's Ask-the-Business-Office/SMA-direct-debit
requests) rather than inventing a new request type.

New file per NFR-11 / the standing main.py rule. Read-only except for the
one correction-request POST, which itself only ever writes to the
already-existing portal.parish_requests table.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import parish_mode
import databank_mcp_client
import congregation
import parish_requests

router = APIRouter()

_current_user = None
_render = None


def register(app, *, current_user, render) -> None:
    global _current_user, _render
    _current_user, _render = current_user, render
    app.include_router(router)


@router.get("/parish-information", response_class=HTMLResponse)
def parish_information_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    parish, _is_preview = parish_mode.effective_parish_mode(request, user)
    if not parish:
        return RedirectResponse("/parish-view")

    contact, error = None, None
    if parish.get("databank_contact_id"):
        contact, error = databank_mcp_client.get_contact(parish["databank_contact_id"])
    else:
        error = "This parish isn't linked to a Databank record yet — contact the diocesan office."

    church_webacct = contact.get("internal_contact_id") if contact else None
    congregation_info = congregation.get_congregation(parish["id"], church_webacct)

    return _render(request, "parish_information.html", user, {
        "parish": parish, "contact": contact, "error": error,
        "clergy": congregation_info["rows"],
        "clergy_as_of": congregation_info["as_of"],
        "clergy_from_cache": congregation_info["from_cache"],
        "clergy_error": congregation_info["error"],
        "clergy_live_error": congregation_info["live_error"],
    })


@router.post("/parish-information/correction")
async def parish_information_correction_submit(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    parish, _is_preview = parish_mode.effective_parish_mode(request, user)
    if not parish:
        return RedirectResponse("/parish-view")
    form = await request.form()
    message = (form.get("message") or "").strip()
    if not message:
        return RedirectResponse("/parish-information?correction_error=1", status_code=303)
    parish_requests.create_request(
        parish["id"], user["id"], "general_request",
        f"Congregation info correction — {parish['name']}", message,
    )
    return RedirectResponse("/parish-information?correction_submitted=1", status_code=303)
