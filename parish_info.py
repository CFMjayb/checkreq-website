"""
parish_info.py — "Parish Information" page (2026-08-08 UI feedback batch).
Replaces the plain Diocese/City/Served Tier/Status card that used to sit at
the top of parish_view.html (Jay: "the big white pill in the main area is
junk... can't you use the standard API to poll the contact information?")
with a real, live Databank lookup keyed on the parish's own
databank_contact_id (populated the same session this page was requested).

New file per NFR-11 / the standing main.py rule. Read-only, no writes.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import parish_mode
import databank_mcp_client

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

    return _render(request, "parish_information.html", user, {
        "parish": parish, "contact": contact, "error": error,
    })
