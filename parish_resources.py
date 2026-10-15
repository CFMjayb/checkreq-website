"""
parish_resources.py -- the "Resources for Parishes" menu in Parish Mode (26-XXX Congregational Vitality, phase 3 layout).

Parish Home has one tile, "Resources for Parishes". It opens this small menu, which holds three things (Jay, 2026-10-15):
  * Neighborhood View            (parish_neighborhood: shown only when the church location is confirmed and a run exists)
  * Parish Sustainability Review (parish_sustain: shown only to the people who see the parish's Finance section)
  * Resource Document Archive    (the shared diocese-wide library, parish_documents' /resource-library: always shown)

The parish is always the one Parish Mode resolved; no parish id is taken from the request. Each tile comes from its own module's
tile function, which never raises, so a failure in one cannot take the menu down.

New file per the standing main.py rule: main.py gets one import and one register line.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import parish_mode

router = APIRouter()

_current_user = None
_render = None
_templates = None


def _tile(name: str, user, parish) -> dict:
    fn = _templates.env.globals.get(name) if _templates else None
    try:
        return fn(user, parish) if fn else {"show": False}
    except Exception:  # noqa: BLE001  (a tile must never break the menu)
        return {"show": False}


def tiles_for(user, parish) -> list[dict]:
    """The menu's tiles, in order: [{"href", "label", "desc"}]. The archive is always there."""
    out = []
    for name in ("neighborhood_tile", "sustain_tile"):
        t = _tile(name, user, parish)
        if t.get("show"):
            out.append({"href": t["href"], "label": t["label"], "desc": t["desc"]})
    out.append({"href": "/resource-library", "label": "Resource Document Archive",
                "desc": "Shared diocese-wide resources and documents."})
    return out


def register(app, *, current_user, render, templates) -> None:
    global _current_user, _render, _templates
    _current_user, _render, _templates = current_user, render, templates
    app.include_router(router)


@router.get("/parish-resources", response_class=HTMLResponse)
def parish_resources_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    parish, _is_preview = parish_mode.effective_parish_mode(request, user)
    if not parish:
        return RedirectResponse("/parish-view")
    return _render(request, "parish_resources.html", user, {"parish": parish, "tiles": tiles_for(user, parish)})
