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
all leadership contacts for a parish"). This page originally called
databank_mcp_client.get_clergy_for_church() live on every page view;
Jay corrected that design the same day ("doesn't the real S6 access a
Postgres table to provide the parish and leadership information to
Beacon?") to match every other Databank-sourced table in this codebase --
Postgres-primary, refreshed nightly by `26-124 GCP Daily Jobs\
congregation_sync_job.py`. This page now only reads
congregation.get_congregation() -- a plain Postgres query, no Databank call
in the request path at all. Also gained a "Report a correction" submit,
reusing parish_requests.py's existing submission path
(kind='general_request', same pattern as Parish Finance's Ask-the-Business-
Office/SMA-direct-debit requests) rather than inventing a new request type.

New file per NFR-11 / the standing main.py rule. Read-only except for the
one correction-request POST, which itself only ever writes to the
already-existing portal.parish_requests table.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import json

import parish_mode
import databank_mcp_client
import congregation
import parish_requests

router = APIRouter()

_current_user = None
_render = None

CHURCH_CONTACT_ROLE = "church_office"


def _church_office_contact(parish: dict) -> dict | None:
    """Non-EDOM parishes have no Databank record, ever -- but for DME
    specifically, 26-138's load_dme_church_contacts.py already loaded the
    church's own address/phone/email (from Realm) into
    portal.parishes.contacts under role='church_office', using the SAME
    field names (company/address/address2/city/state/zip/wphone/email/
    recordtype) get_contact() already returns for a Databank contact -- so
    this can feed the exact same template card with no branching there.
    Returns None (card simply doesn't render) if nothing's been loaded yet
    for this parish, same "empty is a normal state" contract as
    congregation.get_congregation()."""
    contacts = parish.get("contacts") or {}
    if isinstance(contacts, str):
        contacts = json.loads(contacts)
    for entry in contacts.get("contacts") or []:
        if entry.get("role") == CHURCH_CONTACT_ROLE:
            return entry
    return None


def _display_name(row: dict) -> str:
    """Join name parts skipping any that are missing -- congregation_cache's
    middle_name/suffix are frequently NULL (Realm-sourced rows never
    populate middle_name at all), and Jinja stringifies a bare None as the
    literal word "None" rather than an empty string. Real bug found live
    2026-08-28 (Jay: "adding 'none' in the name makes no sense") from the
    template concatenating {{ c.middle_name }} directly."""
    parts = [row.get("first_name"), row.get("middle_name"), row.get("last_name"), row.get("suffix")]
    return " ".join(p for p in parts if p)


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

    # Databank is EDOM-only -- Databank itself is "an EDOM-owned CRM" (see
    # 26-124's CLAUDE.md), and every other served org (DME/Realm, Claggett,
    # the Cornerstone-served parish-orgs) will NEVER have a
    # databank_contact_id, ever, by design -- showing "not linked yet, contact
    # the diocesan office" there is not a transient gap, it's a permanently
    # wrong message for an integration that will never exist for that org.
    # Real bug found live 2026-08-28 (Jay: "you need to remove the not
    # connected to databank - that will never apply") -- confirmed via
    # checkreq.organizations that only org_id 1 (EDOM) is Databank-integrated.
    contact, error = None, None
    if parish.get("org_code") == "EDOM":
        if parish.get("databank_contact_id"):
            contact, error = databank_mcp_client.get_contact(parish["databank_contact_id"])
        else:
            error = "This parish isn't linked to a Databank record yet — contact the diocesan office."
    else:
        contact = _church_office_contact(parish)

    congregation_info = congregation.get_congregation(parish["id"])
    rows = [dict(r, display_name=_display_name(r)) for r in congregation_info["rows"]]

    return _render(request, "parish_information.html", user, {
        "parish": parish, "contact": contact, "error": error,
        "clergy": [r for r in rows if r.get("role_category") == "clergy"],
        "lay_leadership": [r for r in rows if r.get("role_category") != "clergy"],
        "congregation_as_of": congregation_info["as_of"],
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
