"""
auth_routes.py — every /login, /auth/*, and /logout route for the 26-129
website, plus the shared post-auth gate they all funnel into.

Pulled out of main.py on 2026-08-07 (Jay: "the login programming should be a
separate program file, shouldn't it?") — main.py was already flagged in
Parish Portal Plan.md Section 1 as "too big -- do not feed it," and this
session had just been about to add three more routes (password/code) straight
into it. Everything here was already self-contained (confirmed by grep before
moving: no other part of main.py referenced any of these names) except
_current_user, which main.py keeps -- see create_router()'s docstring for why.

Four identity flavors, one shared gate:
  - Microsoft (auth_azure.py)       -- multi-tenant Entra ID, OAuth/OIDC
  - Google (auth_google.py)         -- Workspace + personal Gmail, OAuth/OIDC
  - Emailed one-time code (auth_code.py)     -- universal fallback, step 1
  - Beacon-managed password (auth_password.py) -- opt-in, set from a signed-
    in session only; first sign-in for a fallback-domain user is always code

_complete_login() is the ONE thing every one of the four routes below calls
before establishing a session -- it is physically incapable of inserting an
app_users row (see its own docstring; this is the fix for the real
2026-07-17/18 duplicate-profile incident). A new identity provider added here
in the future must call it too, not invent its own gate.

Front-style single screen (Jay, 2026-08-07): every option -- password, email
code, Microsoft, Google -- is visible on one /login render, no domain-based
redirect first. checkreq.identity_provider_domains and _provider_for_domain()
still exist and still matter (the OAuth login_hint prefill below reads
nothing from it directly, but the table remains the seed data source for any
future "highlight the org's usual provider" enhancement) -- they just no
longer gate which buttons a visitor sees.
"""
from __future__ import annotations

import os
import secrets as pysecrets
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

import auth_azure
import auth_code
import auth_google
import auth_password
import db

ON_CLOUD_RUN = bool(os.environ.get("INSTANCE_CONNECTION_NAME"))

# Must exactly match a redirect URI registered on the Azure AD app registration.
REDIRECT_URI = os.environ.get("AZURE_REDIRECT_URI", "http://localhost:8123/auth/callback")
# Must exactly match an authorized redirect URI on the Google OAuth 2.0 Client ID.
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:8123/auth/google/callback")

_NOT_REGISTERED_MESSAGE = (
    "This email address isn't set up in Beacon yet. Contact your "
    "administrator to be added, then try signing in again."
)

_INCORRECT_CREDENTIALS_MESSAGE = "Incorrect email or password."

# "Remember this browser's email" (2026-07-26). A plain, non-credential
# cookie -- NOT the access gate (app_users, via _complete_login, is that
# regardless); it only pre-fills the email field on repeat visits.
_REMEMBER_COOKIE = "beacon_last_email"
_REMEMBER_MAX_AGE = 60 * 60 * 24 * 365  # ~1 year


def _remember_email(response: Response, email: str) -> None:
    response.set_cookie(
        _REMEMBER_COOKIE, email, max_age=_REMEMBER_MAX_AGE,
        httponly=True, samesite="lax", secure=ON_CLOUD_RUN,
    )


def _domain_of(email: str) -> str:
    email = (email or "").strip().lower()
    return email.rsplit("@", 1)[-1] if "@" in email else ""


def _provider_for_domain(domain: str) -> str | None:
    """checkreq.identity_provider_domains lookup. NOT an access allowlist --
    app_users (via _complete_login, below) remains the only real access
    gate. Not currently used to route the /login render (Front-style single
    screen shows every option regardless) -- kept as the seed data for a
    possible future "highlight this org's usual provider" hint."""
    if not domain:
        return None
    row = db.query_one(
        "SELECT provider FROM checkreq.identity_provider_domains "
        "WHERE domain = %s AND is_active",
        (domain,),
    )
    return row["provider"] if row else None


def _log_login_attempt(email: str, provider: str, domain: str, reason: str) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkreq.login_attempts (email, provider, domain, reason) "
                "VALUES (%s, %s, %s, %s)",
                (email, provider, domain, reason),
            )


def _complete_login(request: Request, email: str, display_name: str, provider: str, provider_subject_id: str) -> tuple[int | None, str | None]:
    """The single, shared post-auth gate for every identity provider.
    Looks up checkreq.app_users by email ONLY -- never inserts a new row. A
    successful sign-in (Microsoft, Google, emailed code, or password) proves
    WHO someone is; it does not by itself prove they're allowed to use
    Beacon -- that's a separate, prior provisioning step owned by an
    administrator.

    This is the structural fix for the exact bug class that created Jay's
    own blank jboggs@episcopalmaryland.org duplicate profile on
    2026-07-17/18: the OLD /auth/callback did an unconditional
    INSERT ... ON CONFLICT, meaning ANY successful sign-in from an email not
    yet seen silently created a zero-permission row. This helper contains no
    INSERT at all, full stop -- physically incapable of creating a row, for
    any provider, now or for any future one added the same way.

    Returns (user_id, None) on success (session already stamped with
    user_id), or (None, reason) on rejection where reason is
    'not_registered' or 'deactivated' -- both cases are logged to
    checkreq.login_attempts before returning. provider_subject_id is stored
    for observability only for code/password (there's no external subject id
    to record) -- pass "" for those."""
    email = email.strip().lower()
    domain = _domain_of(email)
    row = db.query_one("SELECT * FROM checkreq.app_users WHERE email = %s", (email,))

    if not row:
        _log_login_attempt(email, provider, domain, "not_registered")
        return None, "not_registered"

    if not row["is_active"]:
        _log_login_attempt(email, provider, domain, "deactivated")
        return None, "deactivated"

    updates = ["display_name = %s", "last_login_provider = %s", "last_login_at = NOW()"]
    params: list = [display_name, provider]
    if provider == "microsoft":
        updates.append("azure_ad_object_id = %s")
        params.append(provider_subject_id)
    elif provider == "google":
        updates.append("google_subject_id = %s")
        params.append(provider_subject_id)
    params.append(row["id"])

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE checkreq.app_users SET {', '.join(updates)} WHERE id = %s", tuple(params))
    request.session["user_id"] = row["id"]
    return row["id"], None


def create_router(templates) -> APIRouter:
    """Returns an APIRouter with every login/logout route registered.
    Takes main.py's already-configured Jinja2Templates instance (with
    asset_version already wired into its globals) rather than building a
    second one, and deliberately does NOT import main.py -- main.py imports
    this module and calls create_router(), so an import the other direction
    would be circular.

    _current_user (impersonation-aware) stays in main.py, used everywhere
    across the whole app, not just login -- the one thing here that needs
    "is anyone signed in at all" (login_page's redirect-if-already-in check)
    only needs the raw session value, not the full impersonation-aware
    lookup, so it's inlined below instead of importing main.py's helper."""
    router = APIRouter()

    @router.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, error: str = "", email: str = "", switch: str = "", mode: str = ""):
        """Front-style single screen (Jay, 2026-08-07): email + password +
        "email me a code" + Microsoft/Google buttons all visible at once, no
        domain lookup before rendering. mode='code_sent' instead renders step
        2 of the emailed-code flow (see /auth/code/request below)."""
        if request.session.get("user_id"):
            return RedirectResponse("/portal")
        remembered_email = "" if switch else (request.cookies.get(_REMEMBER_COOKIE) or "")
        return templates.TemplateResponse(request, "login.html", {
            "error": error, "email": email, "remembered_email": remembered_email, "mode": mode,
        })

    @router.get("/auth/start")
    def auth_start(request: Request, email: str = ""):
        state = pysecrets.token_urlsafe(24)
        request.session["oauth_state"] = state
        return RedirectResponse(auth_azure.get_auth_url(REDIRECT_URI, state, login_hint=email or None))

    @router.get("/auth/callback", response_class=HTMLResponse)
    def auth_callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""):
        if error:
            return templates.TemplateResponse(request, "login.html", {"error": f"{error}: {error_description}"})

        expected_state = request.session.pop("oauth_state", None)
        if not state or state != expected_state:
            return templates.TemplateResponse(request, "login.html", {"error": "Login state mismatch — please try signing in again."})

        try:
            result = auth_azure.acquire_token(code, REDIRECT_URI)
        except ValueError as exc:
            return templates.TemplateResponse(request, "login.html", {"error": str(exc)})

        claims = result.get("id_token_claims", {})
        email = (claims.get("preferred_username") or claims.get("email") or "").strip().lower()
        display_name = claims.get("name", "")
        oid = claims.get("oid", "")

        if not email:
            return templates.TemplateResponse(request, "login.html", {"error": "Microsoft did not return an email/UPN claim — cannot identify user."})

        user_id, reject_reason = _complete_login(request, email, display_name, "microsoft", oid)
        if reject_reason == "not_registered":
            return templates.TemplateResponse(request, "login.html", {"error": _NOT_REGISTERED_MESSAGE})
        if reject_reason == "deactivated":
            return templates.TemplateResponse(request, "login.html", {"error": f"{email} is deactivated in checkreq.app_users — contact an admin."})

        resp = RedirectResponse("/portal", status_code=303)
        _remember_email(resp, email)
        return resp

    @router.get("/auth/google/start")
    def auth_google_start(request: Request, email: str = ""):
        state = pysecrets.token_urlsafe(24)
        request.session["oauth_state"] = state
        return RedirectResponse(auth_google.get_auth_url(GOOGLE_REDIRECT_URI, state, login_hint=email or None))

    @router.get("/auth/google/callback", response_class=HTMLResponse)
    def auth_google_callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""):
        if error:
            return templates.TemplateResponse(request, "login.html", {"error": f"{error}: {error_description}"})

        expected_state = request.session.pop("oauth_state", None)
        if not state or state != expected_state:
            return templates.TemplateResponse(request, "login.html", {"error": "Login state mismatch — please try signing in again."})

        try:
            claims = auth_google.acquire_token(code, GOOGLE_REDIRECT_URI, state)
        except ValueError as exc:
            return templates.TemplateResponse(request, "login.html", {"error": str(exc)})

        email = claims.get("email", "")
        display_name = claims.get("name", "")
        sub = claims.get("sub", "")

        if not email:
            return templates.TemplateResponse(request, "login.html", {"error": "Google did not return an email claim — cannot identify user."})

        user_id, reject_reason = _complete_login(request, email, display_name, "google", sub)
        if reject_reason == "not_registered":
            return templates.TemplateResponse(request, "login.html", {"error": _NOT_REGISTERED_MESSAGE})
        if reject_reason == "deactivated":
            return templates.TemplateResponse(request, "login.html", {"error": f"{email} is deactivated in checkreq.app_users — contact an admin."})

        resp = RedirectResponse("/portal", status_code=303)
        _remember_email(resp, email)
        return resp

    @router.post("/auth/password/verify", response_class=HTMLResponse)
    async def auth_password_verify_route(request: Request):
        """Beacon-managed password sign-in (addendum decision 1). One
        generic error either way (wrong email, no password set, wrong
        password, locked out) -- auth_password.verify_password() already
        collapses all of those to a single False, matching this app's
        existing anti-enumeration posture."""
        form = await request.form()
        email = str(form.get("email", "")).strip().lower()
        password = str(form.get("password", ""))
        if not password:
            return templates.TemplateResponse(request, "login.html", {"error": _INCORRECT_CREDENTIALS_MESSAGE, "email": email})

        if not auth_password.verify_password(email, password):
            return templates.TemplateResponse(request, "login.html", {"error": _INCORRECT_CREDENTIALS_MESSAGE, "email": email})

        row = db.query_one("SELECT display_name FROM checkreq.app_users WHERE email = %s", (email,))
        user_id, reject_reason = _complete_login(request, email, row["display_name"] if row else "", "password", "")
        if reject_reason == "not_registered":
            return templates.TemplateResponse(request, "login.html", {"error": _NOT_REGISTERED_MESSAGE})
        if reject_reason == "deactivated":
            return templates.TemplateResponse(request, "login.html", {"error": f"{email} is deactivated in checkreq.app_users — contact an admin."})

        resp = RedirectResponse("/portal", status_code=303)
        _remember_email(resp, email)
        return resp

    @router.post("/auth/code/request", response_class=HTMLResponse)
    async def auth_code_request_route(request: Request):
        """Step 1 of the emailed one-time code flow. Always renders the same
        "check your email" screen (mode=code_sent) regardless of whether the
        email is registered or the issue-rate-limit was hit -- see
        auth_code.issue_code()'s docstring for why this must never branch on
        that."""
        form = await request.form()
        email = str(form.get("email", "")).strip().lower()
        if email:
            auth_code.issue_code(email, requesting_ip=request.client.host if request.client else "")
        return templates.TemplateResponse(request, "login.html", {"mode": "code_sent", "email": email})

    @router.post("/auth/code/verify", response_class=HTMLResponse)
    async def auth_code_verify_route(request: Request):
        form = await request.form()
        email = str(form.get("email", "")).strip().lower()
        code = str(form.get("code", ""))
        if not auth_code.verify_code(email, code, requesting_ip=request.client.host if request.client else ""):
            return templates.TemplateResponse(request, "login.html", {
                "error": "That code is incorrect or has expired.", "mode": "code_sent", "email": email,
            })

        row = db.query_one("SELECT display_name FROM checkreq.app_users WHERE email = %s", (email,))
        user_id, reject_reason = _complete_login(request, email, row["display_name"] if row else "", "code", "")
        if reject_reason == "not_registered":
            return templates.TemplateResponse(request, "login.html", {"error": _NOT_REGISTERED_MESSAGE})
        if reject_reason == "deactivated":
            return templates.TemplateResponse(request, "login.html", {"error": f"{email} is deactivated in checkreq.app_users — contact an admin."})

        resp = RedirectResponse("/portal", status_code=303)
        _remember_email(resp, email)
        return resp

    @router.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    return router
