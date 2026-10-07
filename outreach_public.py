"""
outreach_public.py -- 26-156: the recipient-facing side of the shared Email Response
Engine. Public and unauthenticated: the private token in the path IS the
authorization, exactly like /email-action/{token} and /vendor-w9-upload/{token}.

    GET  /respond/{token}         show the response page. Records a link visit but NEVER
                                  records an answer (M365 link scanning pre-fetches links,
                                  so a GET must not be able to act). ?a=<choice> from an email
                                  button preselects that answer.
    POST /respond/{token}         the only thing that records a response (one confirm click).
    GET  /respond/{token}/o.gif   the open pixel. A best-effort signal that never changes a status.

CSRF: the "/respond/" prefix is in csrf_guard.EXEMPT_PREFIXES (the token is the
authorization, there is no session to forge). Every response carries no-store,
noindex and no-referrer headers because the token is in the URL.

New file per the standing rule: main.py only imports this and calls register().
"""
from __future__ import annotations

import base64
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import org_time
import outreach
import outreach_kinds  # noqa: F401  -- importing registers the 'poll' kind and its question types

router = APIRouter()
_templates = None

_CHOICE_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")

# 1x1 transparent GIF.
_PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
_PRIVATE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
}


def register(app, *, templates) -> None:
    global _templates
    _templates = templates
    app.include_router(router)


def _client_ip(request: Request) -> str | None:
    """Same rule as main._client_ip (L1, Security Assessment 2026-09-19): the LAST
    X-Forwarded-For entry is the one Google's front end appended, so the one entry a
    client cannot forge."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else None


def _when(dt, org_id) -> str:
    if not dt:
        return ""
    try:
        return org_time.format_local(dt, org_time.zone_name_for_org(org_id))
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M")


def _page(request: Request, state: str, *, status_code: int = 200, found: dict | None = None,
          token: str = "", extra: dict | None = None):
    ctx = {"state": state, "token": token, "campaign": None, "rec": None, "error_list": [],
           "closes_text": "", "responded_text": "", "by": None, "kind_template": None}
    if found:
        c, r = found["campaign"], found["recipient"]
        ctx.update(campaign=c, rec=r,
                   closes_text=_when(c["closes_at"], c["org_id"]),
                   responded_text=_when(r["responded_at"], c["org_id"]))
    if extra:
        ctx.update(extra)
    resp = _templates.TemplateResponse(request, "respond.html", ctx, status_code=status_code)
    for k, v in _PRIVATE_HEADERS.items():
        resp.headers[k] = v
    return resp


@router.get("/respond/{token}/o.gif")
def respond_pixel(token: str, request: Request):
    try:
        outreach.record_open(token, _client_ip(request), request.headers.get("user-agent"))
    except Exception as exc:  # a pixel must never error: log and still return the image
        print(f"[outreach] open pixel not recorded: {exc!r}")
    return Response(content=_PIXEL, media_type="image/gif", headers=_PRIVATE_HEADERS)


@router.get("/respond/{token}", response_class=HTMLResponse)
def respond_form(token: str, request: Request, a: str | None = None, edit: str | None = None,
                 d: str | None = None):
    """What the person sees. A GET NEVER records an answer. Four things can be shown:
      auto      an email button was tapped (?a=choice): the page records that answer from its OWN
                script (a POST), so a link scanner or preview that only fetches the page records
                nothing, while a person sees no extra step. Without script, or if the browser looks
                automated, the normal confirm form is shown instead.
      received  they have answered: shows what they said, with a way to change it.
      active    the form (first answer, or ?edit=1 to change one).
      closed / expired / cancelled / ...  the engine's other states.
    ?d=1|2 is only the post-answer redirect marker (recorded / updated); it is not logged as a visit."""
    found = outreach.lookup(token)
    if not found:
        return _page(request, "not_found", status_code=404, token=token)
    c, r, state = found["campaign"], found["recipient"], found["state"]
    # ?a= is only ever one of our own choice keys (yes, no, o1..o12): anything else is dropped, so a
    # crafted link cannot write long or odd text into the event log.
    a = a if a and _CHOICE_RE.fullmatch(a) else None
    if state != "not_open" and d is None:
        outreach.record_visit(r, _client_ip(request), request.headers.get("user-agent"), choice=a)
    if state != "active":
        return _page(request, state, found=found, token=token)
    other = outreach.group_responder(c, r)
    if other:
        return _page(request, "group_done", found=found, token=token,
                     extra={"by": {"name": other["name"], "when": _when(other["responded_at"], c["org_id"])}})
    kind = outreach.get_kind(c["kind"])
    responded = r["status"] == "responded"
    received = {"answers": kind.received_answers(c, r), "can_change": bool(c["allow_change"]),
                "flash": {"1": "recorded", "2": "updated"}.get(d or ""),
                # when the CURRENT answer was recorded (a change moves it), not the first answer's time
                "responded_text": _when(outreach.last_response_at(r["id"]) or r["responded_at"], c["org_id"])} if responded else None
    if responded and not c["allow_change"]:
        # "Answers are final": show what they said, with no form and no way to change it.
        return _page(request, "received", found=found, token=token, extra=received)
    quick = kind.quick_answer(c, r, a) if a else None
    if quick:
        return _page(request, "auto", found=found, token=token,
                     extra={**kind.page_context(c, r, a), "kind_template": kind.template, "quick": quick})
    if responded and edit != "1":
        return _page(request, "received", found=found, token=token, extra=received)
    ctx = kind.page_context(c, r, a)
    return _page(request, "active", found=found, token=token,
                 extra={**ctx, "kind_template": kind.template})


@router.post("/respond/{token}", response_class=HTMLResponse)
async def respond_submit(token: str, request: Request):
    form = await request.form()
    data = {k: [outreach.strip_nul(str(v)) for v in form.getlist(k)] for k in form.keys()}
    via = (data.pop("_via", [""])[0] or "form")      # how it arrived: the email-button page, or the form
    via = via if via in outreach.VIAS else "form"
    result = outreach.record_response(token, data, _client_ip(request), request.headers.get("user-agent"), via=via)
    state = result["state"]
    if state == "not_found":
        return _page(request, "not_found", status_code=404, token=token)
    found = {"campaign": result["campaign"], "recipient": result["recipient"]} if "campaign" in result else None
    if state == "error":
        kind = outreach.get_kind(result["campaign"]["kind"])
        ctx = kind.page_context(result["campaign"], result["recipient"], None, submitted=data)
        return _page(request, "active", status_code=400, found=found, token=token,
                     extra={**ctx, "kind_template": kind.template, "error_list": result["errors"]})
    if state == "done":
        # Post/redirect/get: the page they land on is the same "received" page a later visit shows
        # (what they answered, and a way to change it), and a refresh cannot re-submit anything.
        return RedirectResponse(f"/respond/{token}?d={2 if result['changed'] else 1}", status_code=303,
                                headers=_PRIVATE_HEADERS)
    if state == "group_done":
        by = result["by"]
        return _page(request, "group_done", found=found, token=token,
                     extra={"by": {"name": by["name"], "when": _when(by["responded_at"], result["campaign"]["org_id"])}})
    return _page(request, state, found=found, token=token)
