"""
donor_routes_portal.py -- Beacon Donor Management: the Parishioner Self-Service screens (/my/...) and the three small staff routes
that answer what a parishioner sends.

Routes only. Every rule lives in donor_portal_login (sign-in and session), donor_portal (own details, own giving, Get Help),
donor_pledge_requests (pledge requests) and donor_portal_admin (staff turn a parishioner login on or off from the person's System
tab). Read the Plan's Build addendum for the whole design.

The parishioner is NOT a Beacon user. Nothing here calls the staff identity (_current_user) and a staff session satisfies none of it:
every page asks donor_portal_login.current() for the parishioner session row, which is checked on EVERY request. The person and the
parish come from that row, never from the query, a form or the path. Every id a browser does send (a pledge request's campaign or
pledge, a contact, a joint partner, a choice index) is checked against the session by the service that uses it, and a forged one
is answered as "not found".

Every page this module sends carries Cache-Control: private, no-store and X-Robots-Tag: noindex. There are no inline event handlers,
Jinja autoescape is on, every POST carries the CSRF token (the sign-in form too: csrf_token(request) stamps one before sign-in), and
nothing is logged but ids.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

import db
import donor_pledge_requests as PR
import donor_portal as PP
import donor_portal_admin as PA
import donor_portal_login as L
import donor_portal_password as PW
import donor_roles
import donor_web as W
import gcs_client
import org_branding
import upload_guard
from donor_core import DonorError, InvalidInput, NotFound, label

portal = APIRouter()
staff = APIRouter(dependencies=[Depends(W.check_path_ids)])
FLASH_KEY = "portal_flash"
_templates = None


def register(app, templates) -> None:
    """Add the /my routes and the staff answer routes. Called by donor_register.register (the only wiring in main.py's reach)."""
    global _templates
    _templates = templates
    app.include_router(staff)          # before donor_routes_people, so /people/messages/... is never read as /people/{person_id}/...
    app.include_router(portal)


# ── response helpers: every page is private ─────────────────────────────────────────────────────
def _private(resp):
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def _redirect(url: str):
    return _private(RedirectResponse(url, status_code=303))


def _flash(request: Request, kind: str, text: str) -> None:
    request.session[FLASH_KEY] = [kind, text[:300]]


def _page(request: Request, template: str, ps: dict | None, extra: dict | None = None, *, status: int = 200,
          active: str = "", sub: str = ""):
    flash = request.session.pop(FLASH_KEY, None) if FLASH_KEY in request.session else None
    data = {
        "ps": ps, "active": active, "sub": sub, "label": label, "d": W.fmt_date, "money": W.fmt_money, "when": W.fmt_when,
        "giving_on": bool(ps and ps.get("settings", {}).get("giving_enabled")),
        "flash_ok": flash[1] if flash and flash[0] == "ok" else None,
        "flash_err": flash[1] if flash and flash[0] == "err" else None,
    }
    if extra:
        data.update(extra)
    return _private(_templates.TemplateResponse(request, template, data, status_code=status))


def _notice(request: Request, ps: dict | None, title: str, text: str, *, status: int = 200):
    return _page(request, "donor_portal_notice.html", ps, {"title": title, "text": text}, status=status)


def _person_page(request: Request, ps: dict, tab: str, extra: dict | None = None, *, sub: str = "", status: int = 200, target=None):
    """A signed-in page: the SHARED person screen (templates/donor_person.html, the one staff use) in self mode. The tab picks the panel
    (donor_tab_personal / donor_tab_giving / donor_tab_help); `sub` picks Contributions or Pledges inside Giving. The person screen's own
    layout, identity bar, tab bar, fields and styles all come from the shared templates; this only supplies a restricted context."""
    tabs = [("personal", "Personal")] + ([("giving", "Giving")] if ps.get("settings", {}).get("giving_enabled") else []) + [("help", "Get help")]
    data = {**PP.self_context(ps, target), "tab": tab, "tabs": tabs}
    if extra:
        data.update(extra)
    return _page(request, "donor_person.html", ps, data, status=status, active=tab, sub=sub)


def _auth(request: Request):
    """(ps, None) for a signed-in parishioner whose session row is live, else (None, a redirect to the sign-in page)."""
    token = request.session.get(L.SESSION_KEY)
    ps = L.current(token, L.client_ip(request)) if token else None
    if not ps:
        if token:
            request.session.pop(L.SESSION_KEY, None)
            _flash(request, "err", "Your session ended. Please sign in again.")
        return None, _redirect("/my")
    ps["settings"] = donor_roles.settings_get(ps["parish_id"])
    return ps, None


def _landing(settings: dict) -> str:
    return "/my/personal"                        # Jay, 2026-10-10: a member always lands on Personal, never Giving


def _after_code_signin(request: Request, res: dict):
    """A code just proved the address: remember that for a few minutes, and offer a password to someone who has none ("Not now" skips it)."""
    request.session[FRESH_KEY] = int(time.time())
    request.session.pop(EMAIL_KEY, None)
    if not PW.has_password(res["person_id"], res["parish_id"]):
        return _redirect("/my/set-password")
    return _redirect(_landing(donor_roles.settings_get(res["parish_id"])))


def _fresh(request: Request) -> bool:
    ts = request.session.get(FRESH_KEY)
    return isinstance(ts, (int, float)) and 0 <= time.time() - ts <= PW.FRESH_PROOF_SECONDS


def _start_session(request: Request, session_token: str) -> None:
    old = request.session.get(L.SESSION_KEY)
    if old and old != session_token:
        L.sign_out(old, L.client_ip(request), "replaced")
    request.session[L.SESSION_KEY] = session_token
    request.session.pop(L.CHALLENGE_KEY, None)


def _not_giving(request: Request, ps: dict):
    return _notice(request, ps, "Not available yet", "Giving records are not available online for this parish yet. "
                   "Please use Get Help and the parish office will help.", status=404)


# ── sign-in ─────────────────────────────────────────────────────────────────────────────────────
PARISH_KEY = "portal_parish"          # the parish whose /my/<link> the visitor opened (its logo and name); it never decides who may sign in
EMAIL_KEY = "portal_email"            # the address typed on page one, kept only to show on page two and to ask for a code (the signed cookie)
FRESH_KEY = "portal_fresh_proof"      # when an emailed code last proved the person controls their address (lets a password be changed without the old one)
VIA_PW_KEY = "portal_via_password"    # the pending "choose" step came from a password, not a code
CHANGE_KEY = "portal_email_pending"   # a code for a new sign-in address was just sent


def _signin_page(request: Request, *, error: str | None = None, status: int = 200):
    """The sign-in page: the opened parish's logo, name and email box, or (no parish link opened) a line saying to use the parish's own
    link. A staff session is told to use the staff portal, without being given its address."""
    pid = request.session.get(PARISH_KEY)
    extra = {"parish": L.parish_card(pid) if pid else None, "is_staff": request.session.get("user_id") is not None}
    if error:
        extra["error"] = error
    return _page(request, "donor_portal_signin.html", None, extra, status=status)


@portal.get("/my", response_class=HTMLResponse)
def my_home(request: Request):
    token = request.session.get(L.SESSION_KEY)
    if token:
        ps = L.current(token, L.client_ip(request))
        if ps:
            return _redirect(_landing(donor_roles.settings_get(ps["parish_id"])))
        request.session.pop(L.SESSION_KEY, None)
    return _signin_page(request)


@portal.get("/my/signin")
def signin_get():
    return _redirect("/my")


@portal.post("/my/start")
async def start_post(request: Request):
    """Page one: remember the typed address and go to the password page, which is the same for everyone."""
    form = await request.form()
    email = str(form.get("email") or "").strip()[:254]
    if not email:
        return _signin_page(request, error="Enter your email address.", status=400)
    request.session[EMAIL_KEY] = email
    return _redirect("/my/password")


def _password_page(request: Request, *, error: str | None = None, status: int = 200):
    """Page two. Identical for every address (an account, none, or one with no password yet)."""
    email = request.session.get(EMAIL_KEY)
    if not email:
        return _redirect("/my")
    pid = request.session.get(PARISH_KEY)
    return _page(request, "donor_portal_password.html", None, {"email": email, "parish": L.parish_card(pid) if pid else None,
                                                                "error": error}, status=status)


@portal.get("/my/password", response_class=HTMLResponse)
def password_get(request: Request):
    return _password_page(request)


@portal.post("/my/password")
async def password_post(request: Request):
    started = time.monotonic()
    form = await request.form()
    email = request.session.get(EMAIL_KEY)
    if not email:
        return _redirect("/my")
    ip = L.client_ip(request)
    res = await run_in_threadpool(PW.password_signin, email, str(form.get("password") or ""), ip)
    wait = PW.MIN_PASSWORD_SECONDS - (time.monotonic() - started)       # matched or not, the answer takes the same time
    if wait > 0:
        await asyncio.sleep(wait)
    if res["status"] == "signed_in":
        _start_session(request, res["session_token"])
        request.session.pop(EMAIL_KEY, None)
        return _redirect(_landing(donor_roles.settings_get(res["parish_id"])))
    if res["status"] == "choose":
        request.session[L.CHALLENGE_KEY] = res["challenge_token"]
        request.session[VIA_PW_KEY] = True
        request.session.pop(EMAIL_KEY, None)
        return _redirect("/my/choose")
    return _password_page(request, error=PW.BAD_SIGNIN, status=400)


@portal.post("/my/signin")
async def signin_post(request: Request):
    started = time.monotonic()
    form = await request.form()
    email = str(form.get("email") or request.session.get(EMAIL_KEY) or "").strip()
    if not email:
        return _signin_page(request, error="Enter your email address.", status=400)
    ip = L.client_ip(request)
    res = L.request_code(email, ip)
    request.session[L.CHALLENGE_KEY] = res["token"]
    if res["send"]:                                  # sent NOW, before the answer (a job after the answer was not run on Cloud Run)
        await run_in_threadpool(L.send_code_email, res["send"][0], res["send"][1], ip)     # in a worker thread: the server's loop stays free
    # Matched or not, throttled or not, the answer takes the same time: the send takes about a second for an address that has a
    # login and nothing for one that has not, which would tell a stranger who is a member. Pad every answer up to the floor.
    wait = L.MIN_SIGNIN_SECONDS - (time.monotonic() - started)
    if wait > 0:
        await asyncio.sleep(wait)
    return _redirect("/my/code")


@portal.get("/my/code", response_class=HTMLResponse)
def code_get(request: Request):
    if not request.session.get(L.CHALLENGE_KEY):
        return _redirect("/my")
    return _page(request, "donor_portal_code.html", None)


@portal.post("/my/code")
async def code_post(request: Request):
    form = await request.form()
    token = request.session.get(L.CHALLENGE_KEY)
    if not token:
        return _redirect("/my")
    code = "".join(ch for ch in str(form.get("code") or "") if ch.isdigit())[:6]
    ip = L.client_ip(request)
    res = L.verify_code(token, code, ip)
    if res["status"] == "signed_in":
        _start_session(request, res["session_token"])
        return _after_code_signin(request, res)
    if res["status"] == "choose":
        request.session.pop(VIA_PW_KEY, None)
        return _redirect("/my/choose")
    if res["status"] == "blocked":
        request.session.pop(L.CHALLENGE_KEY, None)
        return _notice(request, None, "Please contact the parish office",
                       "We could not sign you in from this email address. Please contact the parish office and they will help you.")
    return _page(request, "donor_portal_code.html", None,
                 {"error": "That code did not work. Check it and try again, or go back and ask for a new one."}, status=400)


@portal.get("/my/choose", response_class=HTMLResponse)
def choose_get(request: Request):
    choices = L.pending_choices(request.session.get(L.CHALLENGE_KEY))
    if not choices:
        return _redirect("/my")
    return _page(request, "donor_portal_choose.html", None, {"choices": choices})


@portal.post("/my/choose")
async def choose_post(request: Request):
    form = await request.form()
    res = L.choose(request.session.get(L.CHALLENGE_KEY), form.get("option"), L.client_ip(request))
    if not res:
        _flash(request, "err", "That choice did not work. Please sign in again.")
        request.session.pop(L.CHALLENGE_KEY, None)
        return _redirect("/my")
    _start_session(request, res["session_token"])
    if request.session.pop(VIA_PW_KEY, None):                 # the choice came after a password: no code proved anything just now
        return _redirect(_landing(donor_roles.settings_get(res["parish_id"])))
    return _after_code_signin(request, res)


@portal.post("/my/signout")
async def signout_post(request: Request):
    L.sign_out(request.session.pop(L.SESSION_KEY, None), L.client_ip(request))
    request.session.pop(L.CHALLENGE_KEY, None)
    _flash(request, "ok", "You are signed out.")
    return _redirect("/my")


# ── a password of their own ─────────────────────────────────────────────────────────────────────
def _setpw_page(request: Request, ps: dict, *, error: str | None = None, status: int = 200):
    has = PW.has_password(ps["person_id"], ps["parish_id"])
    return _page(request, "donor_portal_setpw.html", ps, {"has_password": has, "needs_current": has and not _fresh(request), "error": error}, status=status)


@portal.get("/my/set-password", response_class=HTMLResponse)
def setpw_get(request: Request):
    ps, resp = _auth(request)
    if resp:
        return resp
    return _setpw_page(request, ps)


@portal.post("/my/set-password")
async def setpw_post(request: Request):
    ps, resp = _auth(request)
    if resp:
        return resp
    form = await request.form()
    new, again = str(form.get("new_password") or ""), str(form.get("confirm_password") or "")
    if new != again:
        return _setpw_page(request, ps, error="The two passwords are not the same. Please type them again.", status=400)
    has = PW.has_password(ps["person_id"], ps["parish_id"])
    if has and not _fresh(request) and not await run_in_threadpool(PW.check_current_password, ps["person_id"], ps["parish_id"],
                                                                     str(form.get("current_password") or "")):
        return _setpw_page(request, ps, error="That is not your current password.", status=400)
    try:
        await run_in_threadpool(PW.set_password, ps, new, keep_token=request.session.get(L.SESSION_KEY), ip=L.client_ip(request))
    except PW.PasswordError as e:
        return _setpw_page(request, ps, error=str(e), status=400)
    _flash(request, "ok", "Your password is saved. Next time you can sign in with your email address and password.")
    return _redirect(_landing(ps["settings"]))


# ── changing the email they sign in with ────────────────────────────────────────────────────────
def _sign_in_email(ps: dict) -> str:
    row = db.query_one("SELECT login_email FROM donor.parishioner_login WHERE person_id = %s AND parish_id = %s AND is_enabled",
                       (ps["person_id"], ps["parish_id"]))
    return (row or {}).get("login_email") or ""


@portal.get("/my/email", response_class=HTMLResponse)
def email_get(request: Request):
    ps, resp = _auth(request)
    if resp:
        return resp
    return _page(request, "donor_portal_email.html", ps, {"current_email": _sign_in_email(ps)})


@portal.post("/my/email")
async def email_post(request: Request):
    ps, resp = _auth(request)
    if resp:
        return resp
    started = time.monotonic()
    form = await request.form()
    ip = L.client_ip(request)
    try:
        res = PW.email_change_request(ps, form.get("new_email"), ip)
    except InvalidInput as e:
        return _page(request, "donor_portal_email.html", ps, {"current_email": _sign_in_email(ps), "error": e.message}, status=400)
    if res["send"]:
        await run_in_threadpool(PW.send_change_code_email, res["send"][0], res["send"][1], ip)
    wait = L.MIN_SIGNIN_SECONDS - (time.monotonic() - started)           # the answer takes the same time whether a code was sent or not
    if wait > 0:
        await asyncio.sleep(wait)
    request.session[CHANGE_KEY] = True
    return _redirect("/my/email/confirm")


@portal.get("/my/email/confirm", response_class=HTMLResponse)
def email_confirm_get(request: Request):
    ps, resp = _auth(request)
    if resp:
        return resp
    if not request.session.get(CHANGE_KEY):
        return _redirect("/my/email")
    return _page(request, "donor_portal_email_confirm.html", ps)


@portal.post("/my/email/confirm")
async def email_confirm_post(request: Request):
    ps, resp = _auth(request)
    if resp:
        return resp
    if not request.session.get(CHANGE_KEY):
        return _redirect("/my/email")
    form = await request.form()
    res = await run_in_threadpool(PW.email_change_confirm, ps, str(form.get("code") or ""), L.client_ip(request),
                                  keep_token=request.session.get(L.SESSION_KEY))
    if res["status"] == "changed":
        request.session.pop(CHANGE_KEY, None)
        _flash(request, "ok", f"You now sign in with {res['new_email']}. We let your old address know.")
        return _redirect("/my/personal")
    return _page(request, "donor_portal_email_confirm.html", ps,
                 {"error": "That code did not work. Check it and try again, or ask for a new one."}, status=400)


# ── Personal ────────────────────────────────────────────────────────────────────────────────────
@portal.get("/my/personal", response_class=HTMLResponse)
def personal_get(request: Request):
    ps, resp = _auth(request)
    if resp:
        return resp
    try:                                     # ?person=<id> opens a member of the signed-in person's own household (checked on the server)
        return _person_page(request, ps, "personal", {"has_password": PW.has_password(ps["person_id"], ps["parish_id"])},
                            target=request.query_params.get("person"))
    except NotFound:
        return _notice(request, ps, "Not found", "That person could not be found in your family.", status=404)


@portal.post("/my/personal")
async def personal_post(request: Request):
    ps, resp = _auth(request)
    if resp:
        return resp
    form = await request.form()
    target = form.get("person_id")
    back = "/my/personal"
    try:
        r = PP.personal_save(ps, form, target)
        if str(target or "") not in ("", str(ps["person_id"])):
            back = f"/my/personal?person={int(str(target))}"          # personal_save has already refused anyone outside the family
        _flash(request, "ok", ("Saved: " + ", ".join(dict.fromkeys(r["changed"])) + ".") if r["changed"] else "Nothing to change.")
    except NotFound:
        return _notice(request, ps, "Not found", "That could not be found.", status=404)
    except DonorError as e:
        _flash(request, "err", e.message)
        if str(target or "") not in ("", str(ps["person_id"])) and str(target).isdigit():
            back = f"/my/personal?person={int(str(target))}"
    return _redirect(back)


# ── Giving ──────────────────────────────────────────────────────────────────────────────────────
@portal.get("/my/giving", response_class=HTMLResponse)
def giving_get(request: Request, year: str = ""):
    ps, resp = _auth(request)
    if resp:
        return resp
    if not ps["settings"].get("giving_enabled"):
        return _not_giving(request, ps)
    today = dt.date.today()
    y: int | None = today.year
    if year == "all":
        y = None
    elif year.isdigit() and 1900 <= int(year) <= 2200:
        y = int(year)
    return _person_page(request, ps, "giving", {"giving": PP.giving_view(ps, y, today=today), "this_year": today.year}, sub="contributions")


@portal.get("/my/pledges", response_class=HTMLResponse)
def pledges_get(request: Request):
    ps, resp = _auth(request)
    if resp:
        return resp
    if not ps["settings"].get("giving_enabled"):
        return _not_giving(request, ps)
    return _person_page(request, ps, "giving", {"v": PR.portal_pledges(ps)}, sub="pledges")


@portal.post("/my/pledges/request")
async def pledge_request_post(request: Request):
    ps, resp = _auth(request)
    if resp:
        return resp
    if not ps["settings"].get("giving_enabled"):
        return _not_giving(request, ps)
    form = await request.form()
    try:
        r = PR.request_create(ps, form)
        _flash(request, "ok", {"new": "Received. Finance will look at your pledge and confirm it.",
                               "change": "Received. Your pledge stays as it is until Finance confirms the change.",
                               "cancel": "Received. Your pledge stays as it is until Finance confirms."}[r["kind"]])
        return _redirect("/my/pledges")
    except NotFound:
        return _notice(request, ps, "Not found", "That campaign or pledge could not be found.", status=404)
    except DonorError as e:
        keep = {k: str(form.get(k) or "")[:300] for k in ("kind", "campaign_id", "pledge_id", "amount", "frequency", "start_date",
                                                          "end_date", "note", "joint_with_person_id")}
        return _person_page(request, ps, "giving", {"v": PR.portal_pledges(ps), "form_error": e.message, "fv": keep,
                                                    "needs_confirm": bool((e.details or {}).get("needs_confirm"))}, sub="pledges", status=400)


# ── Get Help ────────────────────────────────────────────────────────────────────────────────────
@portal.get("/my/help", response_class=HTMLResponse)
def help_get(request: Request):
    ps, resp = _auth(request)
    if resp:
        return resp
    return _person_page(request, ps, "help", {"sent": PP.messages_for_person(ps)})


@portal.post("/my/help")
async def help_post(request: Request):
    ps, resp = _auth(request)
    if resp:
        return resp
    form = await request.form()
    try:
        PP.message_create(ps, form.get("subject"), form.get("body"))
        _flash(request, "ok", "Message submitted.")
    except DonorError as e:
        return _person_page(request, ps, "help",
                            {"sent": PP.messages_for_person(ps), "form_error": e.message,
                             "fv": {"subject": str(form.get("subject") or "")[:120], "body": str(form.get("body") or "")[:2000]}}, status=400)
    return _redirect("/my/help")


# ── The per-parish sign-in link: /my/<link name> (registered AFTER every fixed /my/... page, so a link can never shadow one) ─────────
@portal.get("/my/{slug}", response_class=HTMLResponse)
def my_parish(slug: str, request: Request):
    parish = L.parish_for_slug(slug)
    if parish:
        request.session[PARISH_KEY] = parish["id"]
    else:
        request.session.pop(PARISH_KEY, None)                 # an unknown link looks exactly like plain /my
    return my_home(request)


@portal.get("/my/{slug}/logo")
def my_parish_logo(slug: str):
    """The parish's logo for its sign-in page. Public by nature (it is on a page anyone can open); only for a parish whose portal is on."""
    parish = L.parish_for_slug(slug)
    if not parish or not parish["has_logo"]:
        return Response(status_code=404)
    row = db.query_one("SELECT logo_gcs_path, logo_content_type FROM portal.parishes WHERE id = %s", (parish["id"],))
    result = gcs_client.download_bytes(org_branding.LOGO_BUCKET, row["logo_gcs_path"]) if row else None
    if not result:
        return Response(status_code=404)
    data, _ = result
    media_type, extra = upload_guard.serve_logo_headers(data, row["logo_content_type"], "logo")
    return Response(content=data, media_type=media_type, headers={**extra, "Cache-Control": "public, max-age=3600", "X-Robots-Tag": "noindex, nofollow"})


# ── Staff: answer what a parishioner sent ───────────────────────────────────────────────────────
def _staff_gate(request: Request, cap: str, feature: str, active: str):
    user, parish, ctx, resp = W.gate(request, need=None, feature=feature, active=active)
    if resp:
        return None, None, resp
    if not ctx.can(cap):
        return None, None, W.page(request, "donor_off.html", user, parish, ctx, active, {"reason": "permission", "feature": feature}, status_code=403)
    return user, ctx, None


@staff.post("/pledges/requests/{request_id}/approve")
async def staff_request_approve(request_id: int, request: Request):
    user, ctx, resp = _staff_gate(request, "pledges.manage", "giving", "pledges")
    if resp:
        return resp
    form = await request.form()
    back = "/pledges"
    try:
        r = PR.request_approve(ctx, request_id, {k: form.get(k) for k in ("amount", "frequency", "start_date", "end_date", "decision_note")},
                               user.get("email"))
        return W.back(request, back, ok={"new": "Pledge added from the request.", "change": "Pledge changed from the request.",
                                         "cancel": "Pledge cancelled (kept on the record)."}[r["kind"]])
    except DonorError as e:
        return W.back(request, back, err=e.message)


@staff.post("/pledges/requests/{request_id}/decline")
async def staff_request_decline(request_id: int, request: Request):
    user, ctx, resp = _staff_gate(request, "pledges.manage", "giving", "pledges")
    if resp:
        return resp
    form = await request.form()
    try:
        PR.request_decline(ctx, request_id, form.get("reason"), user.get("email"))
        return W.back(request, "/pledges", ok="Request declined (kept on the record).")
    except DonorError as e:
        return W.back(request, "/pledges", err=e.message)


# The parishioner login: created and managed ONLY from the person's System tab (the panel is drawn by donor_routes_people). These three
# routes need roles.manage (a Parish Admin, or the diocese's Beacon Admin or Setup Admin), re-check it in the service, and re-check
# that the person is connected to THIS parish. Static paths here, so /people/{person_id}/... in donor_routes_people never swallows them.
@staff.post("/people/{person_id}/parishioner-login/enable")
async def staff_login_enable(person_id: int, request: Request):
    user, ctx, resp = _staff_gate(request, "roles.manage", "people", "people")
    if resp:
        return resp
    form = await request.form()
    url = f"/people/{person_id}?tab=system"
    try:
        r = PA.login_enable(ctx, person_id, form.get("login_email"))
        return W.back(request, url, ok="Parishioner login turned on." if r["changed"] else "Nothing to change.")
    except DonorError as e:
        return W.back(request, url, err=e.message)


@staff.post("/people/{person_id}/parishioner-login/disable")
async def staff_login_disable(person_id: int, request: Request):
    user, ctx, resp = _staff_gate(request, "roles.manage", "people", "people")
    if resp:
        return resp
    form = await request.form()
    url = f"/people/{person_id}?tab=system"
    try:
        # The System tab's Turn off button asks for no reason (a switch should not need a form); the service still requires one and the
        # change log still records it, so a click with none typed is logged as "Turned off by staff".
        r = PA.login_disable(ctx, person_id, (form.get("reason") or "").strip() or "Turned off by staff")
        return W.back(request, url, ok="Parishioner login turned off." + (f" {r['sessions_ended']} live session(s) ended." if r["sessions_ended"] else ""))
    except DonorError as e:
        return W.back(request, url, err=e.message)


@staff.post("/people/{person_id}/parishioner-login/signout")
async def staff_login_signout(person_id: int, request: Request):
    user, ctx, resp = _staff_gate(request, "roles.manage", "people", "people")
    if resp:
        return resp
    url = f"/people/{person_id}?tab=system"
    try:
        r = PA.login_sign_out_everywhere(ctx, person_id)
        return W.back(request, url, ok=f"Signed out everywhere ({r['sessions_ended']} live session(s) ended).")
    except DonorError as e:
        return W.back(request, url, err=e.message)


@staff.post("/people/messages/{message_id}/done")
async def staff_message_done(message_id: int, request: Request):
    user, ctx, resp = _staff_gate(request, "people.edit", "people", "people")
    if resp:
        return resp
    try:
        PP.message_done(ctx, message_id)
        return W.back(request, "/people", ok="Message marked done.")
    except DonorError as e:
        return W.back(request, "/people", err=e.message)
