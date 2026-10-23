"""
donor_routes_portal_statements.py -- Beacon Donor Management: the parishioner's own statements, under the Giving tab of the member screen.

  GET /my/statements              the list of the person's own years (a sub-tab of Giving, beside Contributions and Pledges)
  GET /my/statements/{year}.pdf   that year's statement as a PDF, built on the spot from the person's OWN posted gifts and shown in the browser's PDF viewer

The person and the parish come from the signed-in portal session (donor_routes_portal._auth), never from the address or the page. The PDF is not stored: Beacon
writes one audit line. Both pages are private (no-store, no-index). A closed door is a short plain page, never a stack trace.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

import donor_portal_statements as PS
import donor_routes_portal as RP
import statement_pdf as SPDF
from donor_core import NotFound, PermissionDenied

router = APIRouter()


def register(app) -> None:
    app.include_router(router)


@router.get("/my/statements", response_class=HTMLResponse)
def statements_get(request: Request):
    ps, resp = RP._auth(request)
    if resp:
        return resp
    if not ps["settings"].get("giving_enabled"):
        return RP._not_giving(request, ps)
    return RP._person_page(request, ps, "giving", {"stmts": PS.member_years(ps)}, sub="statements")


@router.get("/my/statements/{year}.pdf")
def statement_pdf(request: Request, year: str):
    ps, resp = RP._auth(request)
    if resp:
        return resp
    if not ps["settings"].get("giving_enabled"):
        return RP._not_giving(request, ps)
    try:
        data = PS.member_statement(ps, year, logo_loader=SPDF.logo_data_uri)
    except NotFound as e:
        return RP._notice(request, ps, "Not found", e.message, status=404)
    except PermissionDenied as e:
        return RP._notice(request, ps, "Not available", e.message, status=404)
    except PS.TooMany as e:
        return RP._notice(request, ps, "Please wait a moment", e.message, status=429)
    try:
        pdf = SPDF.render_pdf(data["sheet"])
    except RuntimeError:
        return RP._notice(request, ps, "Please wait a moment", PS.BUSY_MESSAGE, status=503)
    resp = Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="Statement-{int(data["year"])}.pdf"'})
    return RP._private(resp)
