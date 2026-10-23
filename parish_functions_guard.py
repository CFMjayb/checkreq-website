"""
parish_functions_guard.py -- keep the parish-related staff screens out of an entity that has no parishes (2026-10-22).

Jay: "I would like to not allow any parish-related functionality to exist in the Claggett entity ... we could have additional
entities like this in the future."  The tiles, the Parish Mode link and the Administrative Tasks cards are hidden by their own
gates (main.py `_render`, base.html, admin_hub.py), all asking org_features.parish_functions_allowed().  This middleware is the
matching ROUTE side, in one place: while the CURRENT entity (the session's `current_org_id`) may not have parish functions, a
request to any path below is turned away -- so a hidden tile can never be reached by typing its address, and a new parish screen
only needs its prefix added to GUARDED_PREFIXES.

Deliberately NOT in this list: the screens a person reaches only while a parish is in effect (Parish Home, Parish Documents,
Finance, ...). They need a parish, and a parish only exists under a diocese that has the switch on; Parish Mode itself refuses to
start or to continue (parish_mode.py) when the entity or the parish's diocese does not.

No entity selected -> passes through (each screen's own gate handles that, as before).
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse

import org_features

GUARDED_PREFIXES = (
    "/admin/parish-mode",
    "/admin/parish-requests",
    "/admin/parish-access-requests",
    "/admin/manage-parishes",
    "/admin/announcements",
    "/admin/parish-documents",
    "/admin/resource-library",
    "/admin/allocation-statements",
    "/congregational-vitality",
)


def is_guarded_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in GUARDED_PREFIXES)


class ParishFunctionsGuardMiddleware(BaseHTTPMiddleware):
    """Added inside SessionMiddleware (earlier add_middleware call), see main.py -- it reads request.session."""

    async def dispatch(self, request, call_next):
        if not is_guarded_path(request.url.path):
            return await call_next(request)
        org_id = request.session.get("current_org_id")
        if not org_id:
            return await call_next(request)
        try:
            allowed = org_features.parish_functions_allowed(org_id)
        except Exception:  # noqa: BLE001 -- fail closed: a screen that cannot be checked is not shown
            allowed = False
        if allowed:
            return await call_next(request)
        if request.method in ("GET", "HEAD"):
            return RedirectResponse("/portal", status_code=303)
        return JSONResponse({"error": "Parish functions are not available for this entity."}, status_code=403)
