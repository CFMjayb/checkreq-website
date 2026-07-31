"""
main.py — 26-129 EDOM/Claggett Online Check Request website (FastAPI).

STATUS (2026-07-18): Real Azure AD login has been live and verified with a
real sign-in since 2026-07-17 (see auth_azure.py) — Authorization Code flow
against the Cornerstone (cfmins.org) tenant, NOT episcopalmaryland.org. Still
not deployed publicly (that's a separate decision from "auth exists") —
confirm the production redirect URI is registered on the Azure app before
exposing this beyond localhost.

Portal/entity-switcher redesign (2026-07-18): login now lands on /portal, not
straight into /new-request. A user picks an "entity" (organization) once —
stored in request.session["current_org_id"] — and that choice is
session-authoritative everywhere downstream, including on the new-request
POST handler, which ignores any client-supplied org_id and re-reads the
session value directly. See /select-entity below.

Working end-to-end against live checkreq Postgres tables:
  - GL account / vendor / program-area dropdowns (from the dual-write jobs)
  - Check-request submission (writes payment_requests + payment_request_gl_lines)
  - Approval chain computed via approval_engine.py at submission time
  - "My Requests" list

NOT built yet (see handoff notes):
  - Invoice-for-payment PDF upload/extraction (manual-entry only for now)
  - New-user provisioning UX: a first-time login auto-creates an app_users
    row (see /auth/callback below), but nothing yet assigns them to a
    program area or approval role — an admin still has to do that separately.
  - Day-5 escalation / reminder emails (backup_approver_id exists but is
    unused) and the dormant cfo_override mechanism — both explicitly
    deferred, see AP Review Workflow Plan.md Section 7.

BUILT 2026-07-26 (AP Review Workflow Plan.md, all Section 8 decisions
answered directly by Jay): the approval action workflow (GET /my-approvals,
POST /requests/{request_number}/approve|reject), the AP Review screen
(GET /admin/ap-review, gated on is_ap_reviewer) and its
POST /requests/{request_number}/post-to-qbo trigger (qbo_mcp_client.py's
first real caller), and cleanup_gcs_attachment()'s first real call site.
checkreq.approval_actions is the real per-approver/per-step gate — see that
table's own comment in migrations/011_approval_actions.sql for why a new
table was unavoidable (current_approver_id/serial_group_current are scalar
display-only fields that cannot represent a parallel multi-approver group).

BUILT 2026-07-26, later same day (Multi-Provider Authentication
Plan.md, all Section 6 decisions answered directly by Jay, not re-asked):
Azure AD widened to multi-tenant (auth_azure.py) + a second, independent
Google OAuth/OIDC provider (auth_google.py) + a shared, INSERT-free
_complete_login() gate (below) called by BOTH provider callback routes —
this is the structural fix for the exact class of bug that created Jay's own
blank jboggs@episcopalmaryland.org duplicate profile on 2026-07-17/18.
Login-page routing is auto-detect-by-email-domain (checkreq.
identity_provider_domains), not the plan's own recommended two-button
design — Jay's explicit choice (plan Section 6, decision 3). Real external
actions still needed before this actually works for a second tenant/domain
(neither can be done from this codebase's own credentials — see auth_azure.py
/ auth_google.py docstrings and CLAUDE.md's Current State entry for this
build): the Azure Portal "Supported account types" -> Multitenant change, and
creating a real Google OAuth 2.0 Client ID + checkreq-google-credentials
secret in the Google Cloud Console. Until both land, cfmins.org/Microsoft
login is unaffected (works exactly as before); a real second-tenant/Google
login attempt will fail with a provider-side error, not a Beacon bug.
"""
from __future__ import annotations

import asyncio
import base64
import os
import secrets as pysecrets
import threading
from datetime import date, datetime
from urllib.parse import quote

from fastapi import FastAPI, Request, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import db
import approval_engine
import auth_azure
import auth_google
import gcs_client
import sharepoint_client
import document_extract
import email_client
import qbo_mcp_client
import app_settings

ATTACHMENTS_BUCKET = "cfm-checkreq-attachments"

# New Vendor Onboarding (New Vendor Onboarding Plan.md, 2026-07-25, approved).
# requires_w9: payment_requests.amount > this threshold, computed once at
# vendor_request creation time -- not re-evaluated later even if the amount
# changes (a change needs its own re-approval anyway, per the plan's Section 4).
VENDOR_W9_AMOUNT_THRESHOLD = 2000

# Section 6, decision 4: "not resolved in this pass -- defaulting to the same
# EDOM mailbox sending on Claggett's behalf too, until Jay provides a
# dedicated Claggett address. This is a one-line config change whenever he
# does." businessoffice@episcopalmaryland.org is already an allowed 26-122
# sender (used elsewhere in this codebase for AP-related correspondence).
W9_SENDER_EMAIL = os.environ.get("W9_SENDER_EMAIL", "businessoffice@episcopalmaryland.org")

# Approval-by-email (Jay, 2026-07-30): how long a one-click approve/reject
# email token stays valid before an approver has to sign in normally instead.
# Long enough to survive a slow multi-step chain and a weekend; short enough
# that a very old, unactioned email can't approve something years later.
APPROVAL_EMAIL_TOKEN_DAYS = 10

# Shared-secret auth for /internal/send-daily-digest -- a machine-to-machine
# call (Cloud Scheduler, not a signed-in user), same X-API-Key-header
# convention email_client.py already uses for its own outbound call to
# 26-122, just inverted (this is an inbound check).
_INTERNAL_KEY_SECRET_NAME = "checkreq-internal-key"
_cached_internal_key: str | None = None


def _get_internal_key() -> str:
    global _cached_internal_key
    env = os.environ.get("CHECKREQ_INTERNAL_KEY", "").strip()
    if env:
        return env
    if _cached_internal_key is None:
        from google.cloud import secretmanager
        project = os.environ.get("FIRESTORE_PROJECT", "cfm-qbo-mcp")
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project}/secrets/{_INTERNAL_KEY_SECRET_NAME}/versions/latest"
        _cached_internal_key = client.access_secret_version(name=name).payload.data.decode("utf-8").strip()
    return _cached_internal_key

app = FastAPI(title="Beacon")

# INSTANCE_CONNECTION_NAME only ever exists in the Cloud Run environment (see
# db.py's connection logic) -- reused here and by /dev/auth-as below as the
# one signal that distinguishes "really deployed" from "someone's local dev
# server". https_only=True is correct once this app is reachable at a real
# https:// Cloud Run URL, but would silently break every local dev session
# (cookie never gets set over plain http://localhost), so it can't be
# unconditional.
ON_CLOUD_RUN = bool(os.environ.get("INSTANCE_CONNECTION_NAME"))

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "dev-only-not-secure"),
    https_only=ON_CLOUD_RUN,
)


@app.middleware("http")
async def canonicalize_localhost(request: Request, call_next):
    """Bounce 127.0.0.1 -> localhost before any session cookie can be set.

    The registered Azure AD redirect URI is fixed to http://localhost:8123/...
    -- 127.0.0.1 and localhost are different cookie origins even though they're
    the same machine, so starting the OAuth flow on 127.0.0.1 sets the session
    cookie under the wrong origin and the callback (which always lands on
    localhost) can't read it back, producing "Login state mismatch" on the
    very first login attempt every time (confirmed live 2026-07-19). This
    middleware runs before SessionMiddleware ever gets a chance to touch the
    session on a 127.0.0.1 request, so the redirect always wins the race.
    """
    host = request.headers.get("host", "")
    if host.startswith("127.0.0.1"):
        new_netloc = host.replace("127.0.0.1", "localhost")
        target = f"{request.url.scheme}://{new_netloc}{request.url.path}"
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(target, status_code=307)
    return await call_next(request)


templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


def _asset_version(rel_path: str) -> int:
    """Cache-busting query param for our own static/ files, keyed to each
    file's mtime -- StaticFiles doesn't send Cache-Control, so browsers fall
    back to heuristic caching and can keep serving a stale CSS/JS file across
    real edits (confirmed to bite both this session's own testing browser
    AND a real user's browser, 2026-07-24). {{ asset_version('css/x.css') }}
    in a template makes the URL itself change whenever the file does, so no
    browser cache (heuristic or otherwise) can ever serve a stale copy."""
    full = os.path.join(os.path.dirname(__file__), "static", rel_path)
    try:
        return int(os.path.getmtime(full))
    except OSError:
        return 0


templates.env.globals["asset_version"] = _asset_version

if os.environ.get("ENABLE_DEV_AUTH_BYPASS") == "1":
    print("*** DEV AUTH BYPASS ENABLED — LOCAL ONLY — /dev/auth-as/{email} is live ***")

# Must exactly match a redirect URI registered on the Azure AD app registration.
REDIRECT_URI = os.environ.get("AZURE_REDIRECT_URI", "http://localhost:8123/auth/callback")
# Must exactly match an authorized redirect URI on the Google OAuth 2.0
# Client ID (not created yet as of 2026-07-26 -- see auth_google.py).
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:8123/auth/google/callback")

# Portal module tiles. A plain list is enough for this scope (6 tiles) --
# revisit as a real "modules" config table if this grows much past that.
# "gate" (AP Review Workflow Plan.md, 2026-07-26): None = visible to every
# logged-in user; "ap_reviewer" = only shown when the current user has
# is_ap_reviewer=TRUE (checked in portal.html, since MODULES itself is a
# module-level constant shared by every request, not per-user).
MODULES = [
    {"key": "check_request", "title": "Check Request", "desc": "Submit a classic check request.", "url": "/new-request", "enabled": True, "gate": None},
    {"key": "my_requests", "title": "My Requests", "desc": "Track requests you've submitted.", "url": "/my-requests", "enabled": True, "gate": None},
    {"key": "invoice_payment", "title": "Invoice for Payment", "desc": "Upload and match an invoice.", "url": None, "enabled": False, "gate": None},
    {"key": "vendor_requests", "title": "Vendor Requests", "desc": "Request a new vendor be added.", "url": None, "enabled": False, "gate": None},
    # Flipped enabled 2026-07-26 (was a disabled "Coming Soon" placeholder) --
    # AP Review Workflow Plan.md Section 2a: visible to every logged-in user,
    # same as My Requests -- an empty queue is a harmless empty state, not
    # worth its own permission gate.
    {"key": "approval_queue", "title": "Approval Queue", "desc": "Review requests awaiting your approval.", "url": "/my-approvals", "enabled": True, "gate": None},
    {"key": "ap_review", "title": "AP Review", "desc": "Final review and QBO posting for fully-approved requests.", "url": "/admin/ap-review", "enabled": True, "gate": "ap_reviewer"},
]


def _real_user(request: Request) -> dict | None:
    """The actual authenticated identity (session['user_id']) -- set only by
    /auth/callback and /dev/auth-as. Never touched by impersonation. Use this
    (not _current_user) for anything privilege-gating: who's allowed to
    impersonate, what the banner/nav shows, audit attribution."""
    uid = request.session.get("user_id")
    if not uid:
        return None
    return db.query_one("SELECT * FROM checkreq.app_users WHERE id = %s", (uid,))


def _close_open_impersonation(real_user_id: int) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.impersonation_log SET ended_at = NOW() "
                "WHERE real_user_id = %s AND ended_at IS NULL",
                (real_user_id,),
            )


def _current_user(request: Request) -> dict | None:
    """THE only sanctioned identity read for routes/templates -- new routes
    must call this, never request.session.get('user_id') directly, or they
    silently bypass impersonation. Returns the impersonated user's row while
    an active impersonation session is live (CFO-only, re-checked live every
    call, never trusted from session); otherwise the real user."""
    real = _real_user(request)
    if not real:
        return None
    imp_id = request.session.get("impersonating_user_id")
    if imp_id and real["is_cfo"]:
        imp = db.query_one(
            "SELECT * FROM checkreq.app_users WHERE id = %s AND is_active", (imp_id,)
        )
        if imp:
            return imp
        # Target vanished/was deactivated mid-session -- fail closed, drop
        # back to the real identity instead of silently continuing.
        request.session.pop("impersonating_user_id", None)
        _close_open_impersonation(real["id"])
    return real


def _current_org(request: Request) -> dict | None:
    """The session-selected entity, re-validated against the live
    organizations table on every call (not just trusted from session).
    Includes the sp_* columns (permanent per-entity SharePoint archive
    location) needed by the attachment-archival flow."""
    org_id = request.session.get("current_org_id")
    if not org_id:
        return None
    return db.query_one(
        "SELECT id, code, name, sp_hostname, sp_site_path, sp_library_folder "
        "FROM checkreq.organizations WHERE id = %s AND is_active",
        (org_id,),
    )


def _client_ip(request: Request) -> str | None:
    """Real client IP for the approval-chain audit trail (Jay, 2026-07-29:
    "the exact approvers with their date/time and IP"). Cloud Run terminates
    TLS at a proxy, so request.client.host would be the proxy's own address,
    not the real caller -- X-Forwarded-For's first entry is the original
    client (standard convention, and the header Cloud Run itself sets).
    Falls back to request.client.host for local dev, where no proxy sits
    in front."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _render(request: Request, template: str, user: dict, extra: dict | None = None):
    """Renders a template that extends base.html, always including the
    header's user/entity-switcher context so every page shows it
    consistently -- not just /portal. Also always includes real_user +
    impersonating, since the impersonation banner/nav-link must gate on the
    REAL identity, not whichever identity `user` currently resolves to."""
    real = _real_user(request)
    ctx = {
        "user": user,
        "real_user": real,
        "impersonating": bool(request.session.get("impersonating_user_id")) and bool(real and real["is_cfo"]),
        "current_org": _current_org(request),
        "all_orgs": db.query("SELECT id, code, name FROM checkreq.organizations WHERE is_active ORDER BY name"),
    }
    if extra:
        ctx.update(extra)
    return templates.TemplateResponse(request, template, ctx)


@app.get("/health")
def health():
    return {"status": "ok"}


_NOT_REGISTERED_MESSAGE = (
    "This email address isn't set up in Beacon yet. Contact your "
    "administrator to be added, then try signing in again."
)

# "Remember this browser's email" (2026-07-26, Jay: wants an SSO-like feel --
# the actual sign-in is already invisible on a domain-joined device thanks to
# Azure AD's native seamless SSO, the only remaining manual step was typing
# the email into Beacon's own page every time). A plain, non-httponly-relevant
# cookie -- it is NOT a credential and NOT the access gate (app_users remains
# the only real gate, via _complete_login below); it only pre-fills/skips the
# email-entry step so returning users land on a "Continue as X" button
# instead of a blank input. Sameite=lax to survive the OAuth-provider
# redirect chain, matching the session cookie's own settings.
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
    """checkreq.identity_provider_domains lookup (Multi-Provider
    Authentication Plan.md, Section 6 decision 3 -- auto-detect by email
    domain, Jay's explicit choice over the plan's own recommended two-button
    design). NOT an access allowlist -- this only steers which provider's
    OAuth flow the login page routes to; app_users (via _complete_login,
    below) remains the only real access gate either way."""
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
    """The single, shared post-auth gate for every identity provider
    (Multi-Provider Authentication Plan.md, Section 4) -- called by BOTH
    /auth/callback (Microsoft) and /auth/google/callback. Looks up
    checkreq.app_users by email ONLY -- never inserts a new row. A
    successful sign-in against Microsoft or Google proves WHO someone is; it
    does not by itself prove they're allowed to use Beacon -- that's a
    separate, prior provisioning step owned by an administrator (same as
    user_program_areas needing to be populated before someone can do
    anything real).

    This is the structural fix for the exact bug class that created Jay's
    own blank jboggs@episcopalmaryland.org duplicate profile on
    2026-07-17/18: the OLD /auth/callback did an unconditional
    INSERT ... ON CONFLICT, meaning ANY successful sign-in from an email not
    yet seen silently created a zero-permission row. This helper contains no
    INSERT at all, full stop -- physically incapable of creating a row, for
    either provider, now or for any future third provider added the same way.

    Returns (user_id, None) on success (session already stamped with
    user_id), or (None, reason) on rejection where reason is
    'not_registered' or 'deactivated' -- both cases are logged to
    checkreq.login_attempts before returning (Plan.md Section 6 decision 2)."""
    email = email.strip().lower()
    domain = _domain_of(email)
    row = db.query_one("SELECT * FROM checkreq.app_users WHERE email = %s", (email,))

    if not row:
        _log_login_attempt(email, provider, domain, "not_registered")
        return None, "not_registered"

    if not row["is_active"]:
        _log_login_attempt(email, provider, domain, "deactivated")
        return None, "deactivated"

    id_col = "azure_ad_object_id" if provider == "microsoft" else "google_subject_id"
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE checkreq.app_users SET display_name = %s, "
                f"{id_col} = %s, last_login_provider = %s, last_login_at = NOW() "
                f"WHERE id = %s",
                (display_name, provider_subject_id, provider, row["id"]),
            )
    request.session["user_id"] = row["id"]
    return row["id"], None


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = "", email: str = "", switch: str = ""):
    """Renders the styled sign-in card -- now email-first (Multi-Provider
    Authentication Plan.md, Section 3): the card asks for an email address
    and submits to /auth/route, which looks up the domain and redirects to
    the correct provider. The actual per-provider OAuth redirects are
    separate routes (/auth/start, /auth/google/start) -- this route must
    render, not redirect, or the cathedral-themed login page is unreachable
    in normal use.

    "Remember this browser" (2026-07-26): if beacon_last_email is set and
    the caller didn't explicitly ask to switch accounts (?switch=1), show a
    one-click "Continue as X" state instead of a blank email input -- the
    actual sign-in step is already invisible on a domain-joined device via
    Azure AD's native seamless SSO; this removes the one remaining manual
    step (typing the email) on repeat visits. Not the access gate --
    app_users (via _complete_login) remains that regardless."""
    if _current_user(request):
        return RedirectResponse("/portal")
    remembered_email = "" if switch else (request.cookies.get(_REMEMBER_COOKIE) or "")
    return templates.TemplateResponse(request, "login.html", {
        "error": error, "email": email, "remembered_email": remembered_email,
    })


@app.get("/auth/route", response_class=HTMLResponse)
def auth_route(request: Request, email: str = ""):
    """Email-first login routing (Plan.md Section 3 / Section 6 decision 3).
    Looks up the typed email's domain in checkreq.identity_provider_domains
    and redirects to the matching provider's start route. An unmapped domain
    gets a clear, non-crashing error -- this table needs to be populated for
    each new served org/domain, same pre-provisioning need as app_users /
    user_program_areas (a real, documented operational gap, not a bug)."""
    email = email.strip().lower()
    domain = _domain_of(email)
    provider = _provider_for_domain(domain)

    if not provider:
        return templates.TemplateResponse(request, "login.html", {
            "error": f"We don't yet recognize \"{domain or email}\" as a supported sign-in domain. "
                     f"Contact your administrator to have it added, then try again.",
            "email": email,
        })

    if provider == "microsoft":
        return RedirectResponse(f"/auth/start?email={quote(email)}")
    return RedirectResponse(f"/auth/google/start?email={quote(email)}")


@app.get("/auth/start")
def auth_start(request: Request, email: str = ""):
    state = pysecrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    return RedirectResponse(auth_azure.get_auth_url(REDIRECT_URI, state, login_hint=email or None))


@app.get("/auth/callback", response_class=HTMLResponse)
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


@app.get("/auth/google/start")
def auth_google_start(request: Request, email: str = ""):
    state = pysecrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    return RedirectResponse(auth_google.get_auth_url(GOOGLE_REDIRECT_URI, state, login_hint=email or None))


@app.get("/auth/google/callback", response_class=HTMLResponse)
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


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _current_user(request):
        return RedirectResponse("/login")
    return RedirectResponse("/portal")


@app.get("/portal", response_class=HTMLResponse)
def portal(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    return _render(request, "portal.html", user, {"modules": MODULES})


@app.get("/select-entity/{org_id}")
def select_entity(org_id: int, request: Request, next: str = "/portal"):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    org = db.query_one(
        "SELECT id FROM checkreq.organizations WHERE id = %s AND is_active", (org_id,)
    )
    if not org:
        return RedirectResponse("/portal")

    request.session["current_org_id"] = org_id
    # Only ever redirect to a same-app relative path -- never trust `next`
    # as an open redirect target.
    target = next if next.startswith("/") and not next.startswith("//") else "/portal"
    return RedirectResponse(target, status_code=303)


@app.get("/admin/impersonate", response_class=HTMLResponse)
def impersonate_picker(request: Request):
    """CFO-only. Gated on the REAL identity, not _current_user() -- while
    already impersonating, only the real underlying CFO may reach this, a
    non-CFO impersonated persona must not be able to chain-impersonate."""
    real = _real_user(request)
    if not real:
        return RedirectResponse("/login")
    if not real["is_cfo"]:
        return JSONResponse({"error": "CFO access required"}, status_code=403)

    users = db.query(
        "SELECT id, email, display_name, is_cfo FROM checkreq.app_users "
        "WHERE is_active AND id != %s ORDER BY display_name",
        (real["id"],),
    )
    return _render(request, "impersonate.html", _current_user(request), {"users": users})


@app.post("/admin/impersonate/stop")
def impersonate_stop(request: Request):
    real = _real_user(request)
    if real:
        _close_open_impersonation(real["id"])
    request.session.pop("impersonating_user_id", None)
    request.session.pop("current_org_id", None)
    return RedirectResponse("/portal", status_code=303)


@app.post("/admin/impersonate/{user_id}")
def impersonate_start(user_id: int, request: Request):
    real = _real_user(request)
    if not real:
        return RedirectResponse("/login")
    if not real["is_cfo"]:
        return JSONResponse({"error": "CFO access required"}, status_code=403)

    target = db.query_one(
        "SELECT id FROM checkreq.app_users WHERE id = %s AND is_active", (user_id,)
    )
    if not target:
        return RedirectResponse("/admin/impersonate")

    _close_open_impersonation(real["id"])
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkreq.impersonation_log (real_user_id, impersonated_user_id) "
                "VALUES (%s, %s)",
                (real["id"], user_id),
            )
    request.session["impersonating_user_id"] = user_id
    # Force a fresh entity pick -- program-area access differs per identity,
    # a carried-over org selection from the real user isn't meaningful here.
    request.session.pop("current_org_id", None)
    return RedirectResponse("/portal", status_code=303)


@app.get("/dev/auth-as/{email}")
def dev_auth_as(email: str, request: Request):
    """Local-only self-verification bypass for the real MSAL login, which
    cannot be completed non-interactively. Double-gated: requires an
    explicit opt-in env var AND refuses whenever ON_CLOUD_RUN is true. Never
    add ENABLE_DEV_AUTH_BYPASS to the Dockerfile or any deploy config."""
    if os.environ.get("ENABLE_DEV_AUTH_BYPASS") != "1" or ON_CLOUD_RUN:
        return JSONResponse({"error": "not available"}, status_code=404)

    user = db.query_one("SELECT * FROM checkreq.app_users WHERE email = %s", (email,))
    if not user:
        return JSONResponse({"error": f"{email} not found in checkreq.app_users"}, status_code=404)

    request.session["user_id"] = user["id"]
    return RedirectResponse("/portal", status_code=303)


@app.get("/new-request", response_class=HTMLResponse)
def new_request_form(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    org = _current_org(request)
    if not org:
        return RedirectResponse("/portal")

    return _render(request, "new_request.html", user, {
        "today": date.today().isoformat(),
        # Blank-state values for the live voucher preview's initial DOM --
        # new_request.js overwrites these via data-field targeting as the
        # user types. Real values (for the PDF) come from _voucher_context().
        "voucher_org_name": org["name"],
        "voucher_request_number": "Pending",
        "voucher_date": date.today().strftime("%B %d, %Y"),
        "voucher_vendor": "—",
        "voucher_amount": "$0.00",
        "voucher_amount_words": "—",
        "voucher_program_area": "—",
        "voucher_description": "—",
        "voucher_gl_lines": [],
        "voucher_total": "$0.00",
        "voucher_requested_by": user.get("display_name") or user.get("email"),
        "voucher_chain_summary": "—",
        "editing_request_number": None,
        "edit_data": None,
    })


@app.get("/requests/{request_number}/edit", response_class=HTMLResponse)
def edit_request_form(request_number: str, request: Request, add_error: str = ""):
    """Renders the SAME new_request.html template used for a brand-new
    submission, pre-filled with an existing payment_request's values, plus a
    hidden editing_request_number field so new_request_submit's POST handler
    knows this is an edit, not a new submission (see that route's own
    docstring-comment for the full edit/reset design).

    Who can edit: the original submitter only (matches my_requests.html's own
    submitter_user_id scoping) -- Jay's spec didn't give a clear signal on
    whether a CFO/admin should also be able to, so this defaults to
    submitter-only, a deliberately easy decision to widen later if asked.
    Locking: whenever _request_is_editable(status) is False (status ==
    'Posted to QBO'), matching Jay's exact locking rule.

    Extended 2026-07-25 (attachment view/delete/add): also renders the
    current attachment list (_active_attachments) so the editor can see
    every document on file, including the generated CR itself -- add_error
    is a redirect-carried banner from the add_attachment route below (the
    remove route's own errors are surfaced the same way the rest of this
    app already handles a rejected mutation: a JSON 4xx, since removal is a
    single-click action with no form state worth preserving on failure)."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    pr = db.query_one(
        "SELECT * FROM checkreq.payment_requests WHERE request_number = %s",
        (request_number,),
    )
    if not pr:
        return JSONResponse({"error": "Request not found"}, status_code=404)
    if pr["submitter_user_id"] != user["id"]:
        # Matches request_pdf's existing authorization-response convention.
        return JSONResponse({"error": "Not authorized to edit this request"}, status_code=403)
    if not _request_is_editable(pr["status"]):
        return JSONResponse(
            {"error": "This request has been posted to QBO and can no longer be edited."},
            status_code=403,
        )

    # Edit operates in the request's ORIGINAL org context, not whatever
    # entity happens to be selected in the session right now -- GL accounts/
    # vendors/program areas are all org-scoped, and a stale session org could
    # otherwise silently mismatch the request being edited. Mirrors the
    # impersonation flow's own precedent of resetting current_org_id on a
    # context switch (see impersonate_start). Side effect worth knowing:
    # this changes the user's "current entity" for the rest of their
    # session, same as impersonation already does -- not new behavior for
    # this codebase, just applied here too.
    request.session["current_org_id"] = pr["org_id"]

    gl_lines = db.query(
        """
        SELECT gl.gl_account_id, gl.amount, gl.memo, ga.account_number, ga.account_name
        FROM checkreq.payment_request_gl_lines gl
        JOIN checkreq.gl_accounts ga ON ga.id = gl.gl_account_id
        WHERE gl.payment_request_id = %s ORDER BY gl.id
        """,
        (pr["id"],),
    )

    vendor_prefill = None
    new_vendor_prefill = None
    if pr["vendor_id"]:
        v = db.query_one("SELECT id, display_name FROM checkreq.vendors WHERE id = %s", (pr["vendor_id"],))
        if v:
            vendor_prefill = {"id": v["id"], "display_name": v["display_name"]}
    elif pr["vendor_request_id"]:
        vr = db.query_one("SELECT * FROM checkreq.vendor_requests WHERE id = %s", (pr["vendor_request_id"],))
        if vr:
            new_vendor_prefill = {
                "entity_type": vr["entity_type"],
                "first_name": vr["first_name"], "last_name": vr["last_name"],
                "company_name": vr["company_name"], "dba_name": vr["dba_name"],
                "address_line1": vr["address_line1"], "address_line2": vr["address_line2"],
                "city": vr["city"], "state": vr["state"], "zip": vr["zip"], "phone": vr["phone"],
                "contact_name": vr["contact_name"], "contact_email": vr["contact_email"],
            }

    edit_data = {
        "editing_request_number": pr["request_number"],
        "program_area_id": pr["program_area_id"],
        "requested_pay_date": pr["requested_pay_date"].isoformat() if pr["requested_pay_date"] else None,
        "description": pr["description"] or "",
        "special_instructions": pr["special_instructions"] or "",
        "gl_lines": [
            {"gl_account_id": g["gl_account_id"], "amount": float(g["amount"]), "memo": g["memo"] or ""}
            for g in gl_lines
        ],
        "vendor": vendor_prefill,
        "new_vendor": new_vendor_prefill,
    }

    ctx = _voucher_context(pr["id"]) or {}
    ctx.update({
        "today": date.today().isoformat(),
        "editing_request_number": pr["request_number"],
        "edit_data": edit_data,
        "attachments": _active_attachments(pr["id"]),
        "add_error": add_error,
    })
    return _render(request, "new_request.html", user, ctx)


@app.get("/api/program-areas/{org_id}")
def api_program_areas(org_id: int, request: Request):
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not signed in"}, status_code=401)

    if user["is_cfo"]:
        # CFO oversees everything — bypasses per-user assignment.
        return db.query(
            "SELECT id, title FROM checkreq.program_areas WHERE org_id = %s AND is_active ORDER BY sort_order",
            (org_id,),
        )
    return db.query(
        """
        SELECT pa.id, pa.title
        FROM checkreq.program_areas pa
        JOIN checkreq.user_program_areas upa ON upa.program_area_id = pa.id
        WHERE pa.org_id = %s AND pa.is_active AND upa.user_id = %s
        ORDER BY pa.sort_order
        """,
        (org_id, user["id"]),
    )


def _request_is_editable(status: str) -> bool:
    """Locking rule, Jay's exact words (2026-07-25): 'You can make changes
    until it is posted to QBO.' Everything else remains editable -- there is
    no live path that sets 'Posted to QBO' yet (no approval-action workflow
    exists to drive a request to that status), so today every request is
    editable; this check is still implemented for real now so it's already
    correct once that posting path exists.

    Extended 2026-07-26 (Task 2, Cancel a Check Request): a cancelled
    request must also stop being editable -- same lock, same reasoning,
    just a second terminal status alongside 'Posted to QBO'."""
    return status not in ("Posted to QBO", "Cancelled")


def _user_can_submit_for(user: dict, program_area_id: int) -> bool:
    """Access-control gate: CFO bypasses; everyone else needs an explicit
    checkreq.user_program_areas assignment. No silent fallback — an
    unassigned user gets a clear rejection, not quiet access."""
    if user["is_cfo"]:
        return True
    row = db.query_one(
        "SELECT 1 FROM checkreq.user_program_areas WHERE user_id = %s AND program_area_id = %s",
        (user["id"], program_area_id),
    )
    return row is not None


# ── Budget/Overspend Tracking (rewritten 2026-07-31 -- Approval Workflow
# Corrections Plan.md) ──
# Original 2026-07-26 decisions still in force:
#   1. Fiscal year = calendar year for both EDOM and Claggett -- no FY-offset
#      logic anywhere in this feature.
#   2. Program Area + GL Account is the budget-scoping key -- no QBO Class
#      disambiguation needed.
#   3. "Actual spend" = the account's REAL QBO GL balance for the calendar
#      year to date (qbo_mcp_client.get_budget_status()'s actual_spend,
#      itself qbo-mcp-server's fetch_gl() net_change -- a true QBO-computed
#      running-balance difference, inherently net of credits/refunds, never
#      a manual sum of individual debit/credit lines) PLUS this GL line's
#      own amount, compared against the account's QBO-native annual budget.
#
# Superseded 2026-07-31 (Jay's direct correction): allow_overspend is no
# longer a hard block/no-block Yes-No switch -- it's now
# overspend_buffer_amount, a dollar buffer, and there is no longer a state
# that blocks submission outright. Three tiers instead:
#   Tier 1 (within budget) -- proceeds normally.
#   Tier 2 (over budget, within the account's buffer) -- proceeds
#     automatically, but the CFO is notified (email + in-app, once the
#     notification bell exists) and it's logged for reporting.
#   Tier 3 (over budget beyond the buffer) -- the submitter must explicitly
#     confirm before the request is created; confirming adds a real CFO
#     approval step to the chain (on top of whatever else already applies).

def _evaluate_gl_line_budgets(
    org: dict, program_area_id: int, gl_lines: list[tuple[int, float, str]]
) -> dict:
    """Per-GL-line budget check. Each line is evaluated INDEPENDENTLY against
    (this account's real QBO year-to-date spend + that line's own amount)
    vs. its QBO-native annual budget -- matches the live-preview UI's own
    per-line framing. Deliberately does NOT sum multiple lines on the SAME
    submission that happen to code to the same GL account -- a documented
    simplification, not something Jay was asked about; see CLAUDE.md.

    A GL account with no QBO Budget data at all (budget_found=False) is
    silently skipped -- there's nothing to compare against. Likewise a GL
    line with no program_area_gl_accounts mapping row at all (shouldn't
    normally happen -- the picker only ever offers mapped accounts -- but
    guarded rather than assumed).

    Returns {"ok": [...], "buffer_notice": [...], "cfo_required": [...]} --
    each non-"ok" entry is a dict with gl_account_id, account_number,
    account_name, annual_budget, projected, buffer_amount, line_amount, and
    a human-readable `detail` string. Callers: new_request_submit (the
    authoritative, server-side gate) and the /api/budget-check-submission
    pre-flight endpoint the UI calls before showing a tier-3 confirmation."""
    result = {"ok": [], "buffer_notice": [], "cfo_required": []}
    if not gl_lines:
        return result
    company = (org.get("code") or "").lower()
    fiscal_year = date.today().year

    for acct_id, amt, _memo in gl_lines:
        row = db.query_one(
            """
            SELECT ga.account_number, ga.account_name, pga.overspend_buffer_amount
            FROM checkreq.gl_accounts ga
            JOIN checkreq.program_area_gl_accounts pga
                ON pga.gl_account_id = ga.id AND pga.program_area_id = %s
            WHERE ga.id = %s
            """,
            (program_area_id, acct_id),
        )
        if not row:
            continue  # no Program Area/GL Account mapping -- nothing to check against

        status, err = qbo_mcp_client.get_budget_status(company, row["account_number"], fiscal_year)
        if err or not status or not status.get("budget_found"):
            continue  # no QBO budget data for this account -- can't enforce

        projected = round(status["actual_spend"] + amt, 2)
        annual_budget = float(status["annual_budget"])
        buffer_amount = float(row["overspend_buffer_amount"])
        label = row["account_name"] or row["account_number"]

        if projected <= annual_budget:
            result["ok"].append({"gl_account_id": acct_id, "account_number": row["account_number"]})
            continue

        entry = {
            "gl_account_id": acct_id,
            "account_number": row["account_number"],
            "account_name": row["account_name"],
            "annual_budget": annual_budget,
            "projected": projected,
            "buffer_amount": buffer_amount,
            "line_amount": amt,
        }
        if projected <= annual_budget + buffer_amount:
            entry["detail"] = (
                f"GL {row['account_number']} ({label}): annual budget ${annual_budget:,.2f}, "
                f"projected spend ${projected:,.2f} after this request (${amt:,.2f} this line) -- "
                f"within the account's ${buffer_amount:,.2f} allowed buffer. Proceeding; the CFO "
                f"will be notified."
            )
            result["buffer_notice"].append(entry)
        else:
            entry["detail"] = (
                f"GL {row['account_number']} ({label}): this line would bring the account's "
                f"year-to-date spend to ${projected:,.2f} against a ${annual_budget:,.2f} annual "
                f"budget plus a ${buffer_amount:,.2f} buffer -- beyond what's allowed without CFO "
                f"approval. Submitting will require CFO sign-off before this can proceed."
            )
            result["cfo_required"].append(entry)
    return result


def _send_budget_buffer_notice_email(request_number: str, org_name: str, details: list[str]) -> None:
    """Tier 2 (over budget, within the account's allowed buffer) -- FYI
    only, no action needed, sent to every is_cfo user (Jay's plan: "the CFO
    is notified... no approval needed"). Fails soft, matching every other
    notification in this app -- one CFO's bad email address must never
    crash the submission that already succeeded by the time this runs."""
    cfos = db.query("SELECT email, display_name FROM checkreq.app_users WHERE is_cfo = TRUE")
    if not cfos:
        return
    subject = f"Budget notice: {request_number} is over budget (within buffer)"
    body_html = (
        f"<p>FYI — <strong>{request_number}</strong> ({org_name}) was submitted over budget on "
        f"one or more GL lines, but within that account's allowed buffer. No action is needed.</p>"
        f"<ul>" + "".join(f"<li>{d}</li>" for d in details) + "</ul>"
    )
    body_text = (
        f"FYI -- {request_number} ({org_name}) was over budget, within buffer:\n\n"
        + "\n".join(details)
    )
    for c in cfos:
        try:
            email_client.send_email(
                to=c["email"], subject=subject, body_html=body_html, body_text=body_text,
                sender=W9_SENDER_EMAIL,
            )
        except Exception as exc:
            print(f"[budget-buffer-notice] failed for {c['email']}: {exc}")


# ── Approval Action Workflow helpers (AP Review Workflow Plan.md, Section 1a/2b) ──

def _serial_group_display_approver(chain: list[dict], serial_group: int | None) -> int | None:
    """current_approver_id is DISPLAY-ONLY (the real gate is always
    checkreq.approval_actions, see _materialize_approval_actions below) --
    a single FK genuinely cannot represent "either of these two people,"
    so this returns the approver's id only when the given serial_group has
    exactly one approver in the chain, and None whenever it has more than
    one (Jay's decision 6: this parallel-group rule applies generally, to
    any multi-approver serial_group -- program-area or global -- not just
    today's real global_approvers case)."""
    if serial_group is None:
        return None
    approvers = [c["approver_user_id"] for c in chain if c["serial_group"] == serial_group]
    return approvers[0] if len(approvers) == 1 else None


def _materialize_approval_actions(cur, payment_request_id: int, chain: list[dict]) -> None:
    """Persists every step of a just-computed approval chain as its own
    'pending' checkreq.approval_actions row -- the real per-approver,
    per-step state this whole workflow is built on (Section 1a).
    approval_engine.build_approval_chain() can legitimately return multiple
    approvers in the same serial_group (a global_approvers parallel step);
    only a per-row table, not a scalar field, can correctly gate "every
    approver in this group must act before it advances." Called at the
    same two moments main.py already computes a chain: the original
    submission INSERT and the edit-triggered approval-reset UPDATE.

    any_one_suffices (Approval Workflow Corrections, 2026-07-31): each
    chain step dict may carry "any_one_suffices": True (set by
    approval_engine.py for the entity global-approver group, and by
    _self_payment_cfo_chain() for the self-payment CFO group) -- stored
    per-row since a step's origin isn't otherwise recoverable once
    materialized, and _perform_approval's group-clear check reads it back
    from here rather than re-deriving the chain."""
    for step in chain:
        cur.execute(
            "INSERT INTO checkreq.approval_actions "
            "(payment_request_id, serial_group, approver_user_id, status, any_one_suffices) "
            "VALUES (%s, %s, %s, 'pending', %s)",
            (payment_request_id, step["serial_group"], step["approver_user_id"],
             bool(step.get("any_one_suffices", False))),
        )


def _supersede_pending_approval_actions(cur, payment_request_id: int) -> None:
    """Edit-triggered approval reset: mark every still-pending
    approval_actions row 'skipped' (never deleted -- matches this app's
    established "never truly delete" philosophy, same reasoning already
    applied to payment_request_attachments.removed_at and to Cancel) before
    a fresh chain is materialized for the request via
    _materialize_approval_actions."""
    cur.execute(
        "UPDATE checkreq.approval_actions SET status = 'skipped', "
        "comment = 'Superseded by edit' WHERE payment_request_id = %s AND status = 'pending'",
        (payment_request_id,),
    )


# ── Approval-by-email (Jay, 2026-07-30) ──────────────────────────────────────
# "We want an email trigger when a CR requires approval... the ability to
# approve or reject within the email itself, with the email sending a
# trigger back to the system to record that. The daily email would show a
# list of all the items with an approve/reject button on each." Two email
# types share the same token mechanism below: an immediate "it's your turn"
# email (fired at submission and at every chain advance) and a daily digest
# (see /internal/send-daily-digest) listing everything still pending for an
# approver. Both link to GET /email-action/{token} -- a public, unauthenticated,
# token-gated confirmation page (same pattern as the existing
# /vendor-w9-upload/{token} route) rather than a bare one-click GET, so an
# email client's own link-prescanning/prefetching can never silently trigger
# a real approval or rejection by itself.

def _mint_approval_email_token(cur, payment_request_id: int, approver_user_id: int,
                                 serial_group: int) -> str:
    """One single-use, time-limited token per (request, approver, serial
    group). Takes an open cursor so it can be minted in the same transaction
    as the approval_actions rows it corresponds to (submission, edit-reset,
    and chain-advance all already hold one)."""
    token = pysecrets.token_urlsafe(32)
    cur.execute(
        "INSERT INTO checkreq.approval_email_tokens "
        "(token, payment_request_id, approver_user_id, serial_group, expires_at) "
        "VALUES (%s, %s, %s, %s, NOW() + INTERVAL '%s days')",
        (token, payment_request_id, approver_user_id, serial_group, APPROVAL_EMAIL_TOKEN_DAYS),
    )
    return token


def _approval_email_context(payment_request_id: int) -> dict | None:
    """Request-summary fields shared by the trigger email, the daily digest,
    and the /email-action/{token} landing page -- one query, one place that
    knows how to resolve a request's vendor display name (mirrors the same
    v.display_name / vendor_requests fallback used throughout my_approvals)."""
    row = db.query_one(
        """
        SELECT pr.id, pr.request_number, pr.amount, pr.description, pr.requested_pay_date,
               pr.status, pr.serial_group_current,
               o.name AS org_name, o.code AS org_code,
               pa.title AS program_area_title,
               u.display_name AS submitter_name, u.email AS submitter_email,
               v.display_name AS vendor_display_name,
               vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
               vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
               vr.dba_name AS vr_dba_name
        FROM checkreq.payment_requests pr
        JOIN checkreq.organizations o ON o.id = pr.org_id
        JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
        JOIN checkreq.app_users u ON u.id = pr.submitter_user_id
        LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
        LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
        WHERE pr.id = %s
        """,
        (payment_request_id,),
    )
    if not row:
        return None
    if row.get("vendor_display_name"):
        row["vendor_name"] = row["vendor_display_name"]
    elif row.get("vr_entity_type"):
        row["vendor_name"] = _vendor_request_row_display_name(
            row["vr_entity_type"], row["vr_company_name"], row["vr_dba_name"],
            row["vr_first_name"], row["vr_last_name"],
        )
    else:
        row["vendor_name"] = "—"
    return row


def _approval_action_email_html(request_body: str, sign_in_url: str) -> str:
    """Shared HTML chrome (header band + footer sign-in link) both the
    trigger email and the daily digest wrap their own inner content in --
    keeps one visual identity across both without duplicating the wrapper."""
    return f"""
<div style="font-family: Arial, Helvetica, sans-serif; max-width: 640px; margin: 0 auto;">
  <div style="background:#1F4E79; color:#fff; padding:16px 24px; border-radius:6px 6px 0 0;">
    <h2 style="margin:0; font-size:18px;">Beacon — Check Request Approvals</h2>
  </div>
  <div style="border:1px solid #ddd; border-top:none; padding:24px; border-radius:0 0 6px 6px;">
    {request_body}
    <p style="color:#888; font-size:12px; margin-top:24px; border-top:1px solid #eee; padding-top:12px;">
      Prefer to review in the app? <a href="{sign_in_url}">Sign in to Beacon</a>.
    </p>
  </div>
</div>
"""


def _request_summary_table_html(ctx: dict) -> str:
    needed_by = ctx["requested_pay_date"].strftime("%Y-%m-%d") if ctx.get("requested_pay_date") else "—"
    return f"""
    <table style="width:100%; border-collapse:collapse; margin:12px 0;">
      <tr><td style="padding:4px 0; color:#555; width:150px;">Request #</td><td style="padding:4px 0; font-weight:bold;">{ctx['request_number']}</td></tr>
      <tr><td style="padding:4px 0; color:#555;">Entity</td><td style="padding:4px 0;">{ctx['org_code']}</td></tr>
      <tr><td style="padding:4px 0; color:#555;">Vendor</td><td style="padding:4px 0;">{ctx['vendor_name']}</td></tr>
      <tr><td style="padding:4px 0; color:#555;">Amount</td><td style="padding:4px 0; font-weight:bold;">${float(ctx['amount']):,.2f}</td></tr>
      <tr><td style="padding:4px 0; color:#555;">Program Area</td><td style="padding:4px 0;">{ctx['program_area_title']}</td></tr>
      <tr><td style="padding:4px 0; color:#555;">Needed By</td><td style="padding:4px 0;">{needed_by}</td></tr>
      <tr><td style="padding:4px 0; color:#555;">Submitted By</td><td style="padding:4px 0;">{ctx.get('submitter_name') or ctx.get('submitter_email') or '—'}</td></tr>
      <tr><td style="padding:4px 0; color:#555; vertical-align:top;">Description</td><td style="padding:4px 0;">{ctx.get('description') or '—'}</td></tr>
    </table>
    """


def _email_action_buttons_html(action_url: str, compact: bool = False) -> str:
    """Approve/Reject as two SEPARATE table rows, not two inline <a> tags
    side by side -- Jay found live on his iPhone (2026-07-30) that inline
    buttons wrap onto separate lines on a narrow screen with zero gap
    between them and visually collide. A tiny one-column table forces each
    button onto its own row with real padding between them regardless of
    viewport width or which mail client renders it (this shape -- a table,
    not flex/inline-block -- is also the most broadly compatible pattern
    across mail clients, several of which still use very old rendering
    engines that don't reliably support inline-block margins)."""
    pad = "5px 12px" if compact else "9px 18px"
    size = "0.85em" if compact else "1em"
    return f"""
    <table cellpadding="0" cellspacing="0" style="margin:{'4px 0' if compact else '16px 0'};">
      <tr><td style="padding-bottom:6px;">
        <a href="{action_url}?action=approve" style="display:inline-block; background:#2E75B6; color:#fff; padding:{pad}; text-decoration:none; border-radius:4px; font-weight:bold; font-size:{size};">Approve</a>
      </td></tr>
      <tr><td>
        <a href="{action_url}?action=reject" style="display:inline-block; background:#fff; color:#b3261e; border:1px solid #b3261e; padding:{pad}; text-decoration:none; border-radius:4px; font-weight:bold; font-size:{size};">Reject</a>
      </td></tr>
    </table>
    """


def _send_approval_needed_email(ctx: dict, approver_email: str, approver_name: str | None,
                                  token: str, request: Request) -> dict:
    base = str(request.base_url).rstrip("/")
    action_url = f"{base}/email-action/{token}"
    sign_in_url = f"{base}/my-approvals"
    subject = f"Approval needed: {ctx['request_number']} ({ctx['vendor_name']}) — ${float(ctx['amount']):,.2f}"
    body = f"""
    <p>Hello {approver_name or ''},</p>
    <p>A check request is waiting for your review as an approver in the chain.</p>
    {_request_summary_table_html(ctx)}
    {_email_action_buttons_html(action_url)}
    """
    body_html = _approval_action_email_html(body, sign_in_url)
    body_text = (
        f"Beacon — Approval needed\n\n"
        f"Request #: {ctx['request_number']}\nEntity: {ctx['org_code']}\nVendor: {ctx['vendor_name']}\n"
        f"Amount: ${float(ctx['amount']):,.2f}\nProgram Area: {ctx['program_area_title']}\n"
        f"Submitted By: {ctx.get('submitter_name') or ctx.get('submitter_email') or '—'}\n"
        f"Description: {ctx.get('description') or '—'}\n\n"
        f"Approve: {action_url}?action=approve\nReject: {action_url}?action=reject\n\n"
        f"Sign in: {sign_in_url}"
    )
    return email_client.send_email(
        to=approver_email, subject=subject, body_html=body_html, body_text=body_text,
        sender=W9_SENDER_EMAIL,
    )


def _send_daily_digest_email(approver: dict, rows: list[dict], request: Request) -> dict:
    """One email per approver per day (see /internal/send-daily-digest),
    listing everything currently pending for them with its own
    Approve/Reject buttons -- each row mints its own fresh token rather than
    reusing whatever the original trigger email sent, so a digest link never
    goes stale just because it's a few days newer than the original notice."""
    base = str(request.base_url).rstrip("/")
    sign_in_url = f"{base}/my-approvals"
    items_html = ""
    text_lines = []
    for r in rows:
        with db.connect() as conn:
            with conn.cursor() as cur:
                token = _mint_approval_email_token(cur, r["pr_id"], approver["id"], r["serial_group"])
        action_url = f"{base}/email-action/{token}"
        needed_by = r["requested_pay_date"].strftime("%Y-%m-%d") if r.get("requested_pay_date") else "—"
        items_html += f"""
        <tr>
          <td style="padding:8px; border-bottom:1px solid #eee;"><strong>{r['request_number']}</strong><br><span style="color:#888; font-size:0.85em;">{r['org_code']}</span></td>
          <td style="padding:8px; border-bottom:1px solid #eee;">{r['vendor_name']}</td>
          <td style="padding:8px; border-bottom:1px solid #eee; text-align:right;">${float(r['amount']):,.2f}</td>
          <td style="padding:8px; border-bottom:1px solid #eee;">{needed_by}</td>
          <td style="padding:8px; border-bottom:1px solid #eee;">
            {_email_action_buttons_html(action_url, compact=True)}
          </td>
        </tr>
        """
        text_lines.append(
            f"- {r['request_number']} ({r['org_code']}) {r['vendor_name']} ${float(r['amount']):,.2f} "
            f"-- Approve: {action_url}?action=approve  Reject: {action_url}?action=reject"
        )

    plural = "s" if len(rows) != 1 else ""
    body = f"""
    <p>Hello {approver.get('display_name') or ''},</p>
    <p>You have <strong>{len(rows)}</strong> check request{plural} waiting for your approval:</p>
    <table style="width:100%; border-collapse:collapse; margin:16px 0; font-size:0.92rem;">
      <tr style="background:#f5f5f5;">
        <th style="padding:8px; text-align:left;">Request</th>
        <th style="padding:8px; text-align:left;">Vendor</th>
        <th style="padding:8px; text-align:right;">Amount</th>
        <th style="padding:8px; text-align:left;">Needed By</th>
        <th style="padding:8px; text-align:left;">Action</th>
      </tr>
      {items_html}
    </table>
    """
    body_html = _approval_action_email_html(body, sign_in_url)
    body_text = (
        f"Beacon -- {len(rows)} check request{plural} waiting for your approval:\n\n"
        + "\n".join(text_lines) + f"\n\nSign in: {sign_in_url}"
    )
    subject = f"Beacon: {len(rows)} check request{plural} awaiting your approval"
    return email_client.send_email(
        to=approver["email"], subject=subject, body_html=body_html, body_text=body_text,
        sender=W9_SENDER_EMAIL,
    )


def _notify_approvers_for_group(payment_request_id: int, serial_group: int | None,
                                  request: Request) -> None:
    """Fires the 'it's your turn' email to every approver in the given
    serial_group (more than one for a parallel global-approvers step) --
    called right after that group's approval_actions rows are committed, at
    submission, at an edit-triggered chain reset, and whenever
    _perform_approval() clears a group and advances to the next one.
    A None serial_group (no approval rule configured for this program area)
    or an empty chain is a silent no-op -- nothing to notify anyone about."""
    if serial_group is None:
        return
    ctx = _approval_email_context(payment_request_id)
    if not ctx:
        return
    approvers = db.query(
        "SELECT aa.id AS action_id, u.id AS user_id, u.email, u.display_name "
        "FROM checkreq.approval_actions aa "
        "JOIN checkreq.app_users u ON u.id = aa.approver_user_id "
        "WHERE aa.payment_request_id = %s AND aa.serial_group = %s AND aa.status = 'pending'",
        (payment_request_id, serial_group),
    )
    for a in approvers:
        with db.connect() as conn:
            with conn.cursor() as cur:
                token = _mint_approval_email_token(cur, payment_request_id, a["user_id"], serial_group)
        try:
            _send_approval_needed_email(ctx, a["email"], a["display_name"], token, request)
        except Exception as exc:
            # Fails soft, matching this app's established philosophy for
            # every other email call site -- a notification failure must
            # never block or roll back the approval action that triggered
            # it. The approver can always still act via /my-approvals.
            print(f"[approval-email] notify failed for user {a['user_id']} on "
                  f"{ctx['request_number']}: {exc}")


def _perform_approval(pr: dict, my_action: dict, actor_user_id: int, note: str | None,
                        ip: str | None, source: str, impersonated_by: int | None = None) -> dict:
    """Shared approval-recording + chain-advance core, used by both the
    authenticated web route (/requests/{n}/approve) and the token-based
    email route (/email-action/{token}). Identical logic to the pre-existing
    approve_request() body, extracted verbatim (down to the same-cursor
    re-read gotcha noted below) except for the added action_source column
    and the new return value used to know whether to fire a chain-advance
    notification. `source` is 'web' or 'email' for the audit trail."""
    audit_comment = f"Approved (serial group {pr['serial_group_current']})."
    if note:
        audit_comment += f" Note: {note}"

    new_group_started = None
    fully_approved = False

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.approval_actions SET status = 'approved', acted_at = NOW(), "
                "acted_by_user_id = %s, comment = %s, ip_address = %s, action_source = %s WHERE id = %s",
                (actor_user_id, note, ip, source, my_action["id"]),
            )
            cur.execute(
                "INSERT INTO checkreq.audit_log "
                "(payment_request_id, action_by_user_id, action_type, comment, serial_group, "
                " previous_status, new_status, impersonated_by_user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (pr["id"], actor_user_id, "Approved", audit_comment,
                 pr["serial_group_current"], pr["status"], pr["status"], impersonated_by),
            )

            # One-sign-off-suffices (Approval Workflow Corrections, 2026-07-31):
            # for a group materialized with any_one_suffices=True (the entity
            # Global Approvers step, or the self-payment CFO step), the FIRST
            # approval clears the whole group immediately -- every other
            # still-pending row in it is skipped right here, the same way a
            # rejection already cascades skips elsewhere, rather than waiting
            # for every approver to act. Ordinary Program-Area approval_rules
            # groups (any_one_suffices=False, the default) are unaffected --
            # they still wait for every pending row to clear naturally via the
            # remaining-count check below.
            if my_action.get("any_one_suffices"):
                cur.execute(
                    "UPDATE checkreq.approval_actions SET status = 'skipped' "
                    "WHERE payment_request_id = %s AND serial_group = %s AND status = 'pending'",
                    (pr["id"], pr["serial_group_current"]),
                )

            # Same-cursor re-read gotcha (found live during the original
            # build): must use `cur`, not db.query_one/db.query -- those open
            # a separate connection/transaction that cannot see the
            # just-committed-within-THIS-transaction 'approved' row above,
            # so the group would never appear to clear.
            cur.execute(
                "SELECT COUNT(*) AS c FROM checkreq.approval_actions "
                "WHERE payment_request_id = %s AND serial_group = %s AND status = 'pending'",
                (pr["id"], pr["serial_group_current"]),
            )
            remaining = cur.fetchone()["c"]

            if remaining == 0:
                cur.execute(
                    "SELECT DISTINCT serial_group FROM checkreq.approval_actions "
                    "WHERE payment_request_id = %s AND serial_group > %s ORDER BY serial_group",
                    (pr["id"], pr["serial_group_current"]),
                )
                later_groups = cur.fetchall()
                if later_groups:
                    next_group = later_groups[0]["serial_group"]
                    cur.execute(
                        "SELECT approver_user_id FROM checkreq.approval_actions "
                        "WHERE payment_request_id = %s AND serial_group = %s",
                        (pr["id"], next_group),
                    )
                    next_approvers = cur.fetchall()
                    next_display_approver = (
                        next_approvers[0]["approver_user_id"] if len(next_approvers) == 1 else None
                    )
                    cur.execute(
                        "UPDATE checkreq.payment_requests SET serial_group_current = %s, "
                        "current_approver_id = %s, updated_at = NOW() WHERE id = %s",
                        (next_group, next_display_approver, pr["id"]),
                    )
                    new_group_started = next_group
                else:
                    cur.execute(
                        "UPDATE checkreq.payment_requests SET status = 'Approved', "
                        "current_approver_id = NULL, updated_at = NOW() WHERE id = %s",
                        (pr["id"],),
                    )
                    cur.execute(
                        "INSERT INTO checkreq.audit_log "
                        "(payment_request_id, action_by_user_id, action_type, comment, "
                        " previous_status, new_status, impersonated_by_user_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (pr["id"], actor_user_id, "Fully Approved",
                         "All approval steps complete -- ready for AP review.",
                         pr["status"], "Approved", impersonated_by),
                    )
                    fully_approved = True

    return {"new_group_started": new_group_started, "fully_approved": fully_approved}


def _perform_rejection(pr: dict, my_action: dict, actor_user_id: int, reason: str,
                         ip: str | None, source: str, impersonated_by: int | None = None) -> None:
    """Shared rejection-recording core (identical to the pre-existing
    reject_request() body, extracted verbatim except for action_source)."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.approval_actions SET status = 'rejected', acted_at = NOW(), "
                "acted_by_user_id = %s, comment = %s, ip_address = %s, action_source = %s WHERE id = %s",
                (actor_user_id, reason, ip, source, my_action["id"]),
            )
            cur.execute(
                "UPDATE checkreq.approval_actions SET status = 'skipped' "
                "WHERE payment_request_id = %s AND status = 'pending'",
                (pr["id"],),
            )
            cur.execute(
                "UPDATE checkreq.payment_requests SET status = 'Rejected', "
                "current_approver_id = NULL, serial_group_current = NULL, updated_at = NOW() "
                "WHERE id = %s",
                (pr["id"],),
            )
            cur.execute(
                "INSERT INTO checkreq.audit_log "
                "(payment_request_id, action_by_user_id, action_type, comment, serial_group, "
                " previous_status, new_status, impersonated_by_user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (pr["id"], actor_user_id, "Rejected", reason, pr["serial_group_current"],
                 pr["status"], "Rejected", impersonated_by),
            )


# ── Approval Workflow Corrections (Jay, 2026-07-31) ──────────────────────────
# Four live corrections, all confirmed against the real code before being
# designed, then approved through several rounds of live refinement -- see
# Approval Workflow Corrections Plan.md for the full write-up. This section
# covers the two submission-time pieces (self-payment override, self-
# approval shortcut); the three-tier budget check lives in
# _evaluate_gl_line_budgets below, and the entity-scoped Global Approvers
# change is in approval_engine.py.

def _is_self_payment(vendor_id: int | None, submitter_user_id: int) -> bool:
    """True only when an EXISTING vendor is selected and that vendor row is
    linked (checkreq.vendors.linked_user_id) to the submitter's own login --
    e.g. an employee reimbursement where the vendor record represents the
    employee. A brand-new vendor (using_new_vendor) has no linked_user_id
    concept at all -- self-payment detection is deliberately scoped to
    existing, already-linked vendor rows only, per the plan's own stated
    limitation, not silently extended to guess at new-vendor submissions."""
    if not vendor_id:
        return False
    row = db.query_one("SELECT linked_user_id FROM checkreq.vendors WHERE id = %s", (vendor_id,))
    return bool(row and row["linked_user_id"] == submitter_user_id)


def _cfo_approver_rows(exclude_user_id: int) -> list[dict]:
    """Every is_cfo user except the given one -- shared by the self-payment
    chain and the tier-3 budget-overage CFO step. Excluding the acting
    submitter even if they happen to hold the is_cfo flag is deliberate in
    both call sites: letting someone approve a step whose entire purpose is
    a check on their OWN submission would defeat the point of the rule; a
    genuinely separate person must act."""
    return db.query(
        "SELECT id, email, display_name FROM checkreq.app_users "
        "WHERE is_cfo = TRUE AND id != %s",
        (exclude_user_id,),
    )


def _self_payment_cfo_chain(submitter_user_id: int) -> list[dict]:
    """Self-payment always requires CFO approval, any ONE of them, regardless
    of amount or the submitter's own authorization -- bypasses the normal
    Program-Area chain and the entity global-approver step entirely (Jay:
    "that always requires the CFO's approval, regardless of their
    authorization level or the dollar amount")."""
    return [
        {"serial_group": 1, "approver_user_id": u["id"], "approver_email": u["email"],
         "approver_name": u["display_name"], "backup_approver_id": None,
         "any_one_suffices": True}
        for u in _cfo_approver_rows(submitter_user_id)
    ]


def _maybe_auto_approve_self(payment_request_id: int, first_serial_group: int | None,
                               user_id: int, ip: str | None, request: Request) -> int | None:
    """Self-approval shortcut: if the submitter is themselves an authorized
    approver for the first step of the chain their own request just
    entered (i.e. they appear in checkreq.approval_rules for this Program
    Area, within their own approval_limit), that satisfies the step
    automatically -- no separate person needs to also sign off. Never
    called for the self-payment CFO chain (see new_request_submit) --
    _self_payment_cfo_chain() already excludes the submitter from its own
    approver list, so this function would simply find no matching pending
    row for them there anyway, but the caller doesn't even attempt it in
    that case.

    Runs strictly AFTER the submission transaction has committed, so
    _perform_approval's own separate connection can see the just-inserted
    approval_actions rows (the same same-cursor-visibility rule this
    codebase already learned once). Returns the newly-started serial_group
    if this auto-approval advanced the chain, else None -- matching
    _perform_approval's own 'new_group_started' contract, so the caller can
    chain straight into _notify_approvers_for_group for whichever group is
    actually now current."""
    if first_serial_group is None:
        return None
    pr = db.query_one("SELECT * FROM checkreq.payment_requests WHERE id = %s", (payment_request_id,))
    if not pr or pr["status"] != "UnderReview" or pr["serial_group_current"] != first_serial_group:
        return None
    my_action = db.query_one(
        "SELECT * FROM checkreq.approval_actions WHERE payment_request_id = %s "
        "AND approver_user_id = %s AND serial_group = %s AND status = 'pending'",
        (payment_request_id, user_id, first_serial_group),
    )
    if not my_action:
        return None
    result = _perform_approval(
        pr, my_action, user_id,
        "Auto-satisfied -- submitter is already an authorized approver for this budget area.",
        ip, "web",
    )
    return result["new_group_started"]


def _require_ap_reviewer(request: Request):
    """Mirrors _require_vendor_approver exactly -- returns (user, None) if
    allowed, or (None, error_response) if not. Callers do
    `user, err = _require_ap_reviewer(request); if err: return err`."""
    user = _current_user(request)
    if not user:
        return None, RedirectResponse("/login")
    if not user.get("is_ap_reviewer"):
        return None, JSONResponse({"error": "AP-reviewer access required"}, status_code=403)
    return user, None


def _user_display_name(user_id: int | None) -> str | None:
    if not user_id:
        return None
    row = db.query_one("SELECT display_name, email FROM checkreq.app_users WHERE id = %s", (user_id,))
    return (row.get("display_name") or row.get("email")) if row else None


def _send_rejection_email(pr: dict, new_status: str, reason: str, request: Request,
                            rejected_by_name: str | None = None) -> dict:
    """Decision 2 (AP Review Workflow Plan.md, Section 8): any rejection --
    mid-chain ('Rejected') or AP-stage ('Returned by AP') -- emails the
    submitter, reusing the exact same 26-122/email_client.py path already
    used for the New Vendor Onboarding W-9 request email
    (_send_w9_request_email above). Fails soft by design -- email_client.
    send_email() never raises; the caller surfaces whatever comes back
    ({"status": "sent"} or {"error": "..."}) as a visible-but-non-blocking
    email_warning, matching this app's established archive_warning pattern.
    pr must include submitter_email/submitter_name/org_name/id and
    request_number (the reject/ap_return/email-action routes' own SELECT
    joins these in).

    Jay, 2026-07-30 (after seeing the plain original version live): the
    submitter had no way to tell WHAT was rejected without opening the app --
    added vendor/amount and who rejected it, and attached the actual
    check-voucher PDF (same render_check_voucher_pdf() the on-demand
    GET /requests/{n}/pdf route uses) so the submitter has the full document
    in hand immediately, not just a bare notice."""
    verb = "returned by the Business Office (AP) for corrections" if new_status == "Returned by AP" else "rejected"
    edit_url = f"{str(request.base_url).rstrip('/')}/requests/{pr['request_number']}/edit"
    subject = f"Check Request {pr['request_number']} was {verb}"
    vendor_name = _vendor_display_name_for_request(pr["id"]) or "—"
    amount_str = f"${float(pr['amount']):,.2f}" if pr.get("amount") is not None else "—"
    rejected_by = rejected_by_name or "—"
    body_html = (
        f"<p>Hello {pr.get('submitter_name') or ''},</p>"
        f"<p>Your check request <strong>{pr['request_number']}</strong> "
        f"({pr['org_name']}) was {verb}.</p>"
        f"<table style=\"border-collapse:collapse; margin:12px 0;\">"
        f"<tr><td style=\"padding:3px 12px 3px 0; color:#555;\">Vendor</td><td style=\"padding:3px 0;\">{vendor_name}</td></tr>"
        f"<tr><td style=\"padding:3px 12px 3px 0; color:#555;\">Amount</td><td style=\"padding:3px 0; font-weight:bold;\">{amount_str}</td></tr>"
        f"<tr><td style=\"padding:3px 12px 3px 0; color:#555;\">{'Returned by' if new_status == 'Returned by AP' else 'Rejected by'}</td><td style=\"padding:3px 0;\">{rejected_by}</td></tr>"
        f"</table>"
        f"<p><strong>Reason:</strong> {reason}</p>"
        f"<p>The original check request is attached. You can edit and resubmit it here: <a href=\"{edit_url}\">{edit_url}</a></p>"
    )
    body_text = (
        f"Hello {pr.get('submitter_name') or ''},\n\n"
        f"Your check request {pr['request_number']} ({pr['org_name']}) was {verb}.\n\n"
        f"Vendor: {vendor_name}\nAmount: {amount_str}\n"
        f"{'Returned by' if new_status == 'Returned by AP' else 'Rejected by'}: {rejected_by}\n\n"
        f"Reason: {reason}\n\nEdit and resubmit here: {edit_url}"
    )

    attachments = None
    try:
        pdf_bytes = render_check_voucher_pdf(pr["id"])
        attachments = [{
            "name": f"{pr['request_number']}.pdf",
            "content_type": "application/pdf",
            "content_base64": base64.b64encode(pdf_bytes).decode("ascii"),
        }]
    except Exception as exc:
        # Fails soft -- a PDF render hiccup must never block the rejection
        # notice itself from going out.
        print(f"[rejection-email] PDF render failed for {pr['request_number']}: {exc}")

    return email_client.send_email(
        to=pr["submitter_email"], subject=subject, body_html=body_html, body_text=body_text,
        sender=W9_SENDER_EMAIL, attachments=attachments,
    )


@app.get("/api/vendors/{org_id}")
def api_vendors(org_id: int, request: Request, q: str = ""):
    # Was missing this check entirely (every sibling /api/* route has it) --
    # found live 2026-07-25 while hardening for the first public Cloud Run
    # deploy. Matches /api/program-areas' pattern: login required, no
    # further org-membership restriction (same as that endpoint).
    if not _current_user(request):
        return JSONResponse({"error": "Not signed in"}, status_code=401)
    if q:
        return db.query(
            "SELECT id, display_name FROM checkreq.vendors "
            "WHERE org_id = %s AND is_active AND display_name ILIKE %s ORDER BY display_name LIMIT 25",
            (org_id, f"%{q}%"),
        )
    return db.query(
        "SELECT id, display_name FROM checkreq.vendors WHERE org_id = %s AND is_active "
        "ORDER BY display_name LIMIT 25",
        (org_id,),
    )


@app.get("/api/gl-accounts/{org_id}")
def api_gl_accounts(org_id: int, program_area_id: int, request: Request, q: str = ""):
    """Filtered/ordered by the Program Area <-> GL Account mapping
    (checkreq.program_area_gl_accounts) -- that mapping's display_text/
    allow_post/sort_order columns were added and deployed on the
    qbo-mcp-server side (for the staff Setup Tables workbook) 2026-07-25,
    but this route was never actually updated to use it -- it was still
    querying checkreq.gl_accounts directly, unfiltered, in raw
    account-number order, always showing the account's own name never a
    display_text override. Found live by Jay testing a real submission the
    same day. allow_post=false rows (header/grouping records) are never
    selectable here. Mirrors qbo-mcp-server's
    get_program_area_gl_accounts() sort logic exactly -- same hierarchical
    dot-notation ordering, same malformed-sort_order safety guard."""
    if not _current_user(request):
        return JSONResponse({"error": "Not signed in"}, status_code=401)

    base_sql = r"""
        SELECT ga.id, ga.account_number,
               COALESCE(NULLIF(pga.display_text, ''), ga.account_name) AS account_name,
               pga.sort_order
        FROM checkreq.program_area_gl_accounts pga
        JOIN checkreq.program_areas pa ON pa.id = pga.program_area_id
        JOIN checkreq.gl_accounts ga ON ga.id = pga.gl_account_id
        WHERE pa.org_id = %s AND pga.program_area_id = %s AND pga.allow_post AND ga.is_active
    """
    order_sql = r"""
        ORDER BY CASE WHEN pga.sort_order ~ '^[0-9]+(\.[0-9]+)*$' THEN 0 ELSE 1 END,
                 CASE WHEN pga.sort_order ~ '^[0-9]+(\.[0-9]+)*$'
                      THEN string_to_array(pga.sort_order, '.')::int[] ELSE NULL END,
                 ga.account_number
        LIMIT 50
    """
    if q:
        return db.query(
            base_sql + " AND (ga.account_number ILIKE %s OR ga.account_name ILIKE %s OR pga.display_text ILIKE %s) " + order_sql,
            (org_id, program_area_id, f"%{q}%", f"%{q}%", f"%{q}%"),
        )
    return db.query(base_sql + order_sql, (org_id, program_area_id))


@app.get("/api/approval-chain-preview")
def api_approval_chain_preview(program_area_id: int, amount: float, request: Request):
    """Live-preview endpoint for the check-voucher's Approval Chain Preview
    line -- just exposes what new_request_submit already computes via
    approval_engine at submission time, no new business logic here."""
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not signed in"}, status_code=401)
    org = _current_org(request)
    if not org:
        return JSONResponse({"error": "No entity selected"}, status_code=400)
    chain = approval_engine.build_approval_chain(program_area_id, org["id"], amount)
    return {"summary": approval_engine.describe_chain(chain)}


@app.get("/api/budget-status")
def api_budget_status(request: Request, program_area_id: int, gl_account_id: int, amount: float = 0):
    """Live-preview endpoint -- same "just exposes what new_request_submit
    already computes" pattern as /api/approval-chain-preview above.
    `amount` is THIS GL LINE's own typed amount only (not summed across
    other lines on the same submission that might share the same GL
    account -- see _evaluate_gl_line_budgets' docstring for why). Returns a
    soft {"budget_found": false} rather than an error for "nothing to show"
    cases (no mapping row, no QBO budget data, qbo-mcp-server unreachable)
    -- the UI simply shows nothing rather than breaking, matching this
    codebase's established soft-error convention (e.g. /api/extract-document).

    Rewritten 2026-07-31 for the three-tier design -- `tier` is
    'ok' | 'buffer_notice' | 'cfo_required', driving the green-check vs.
    warning-badge treatment on the GL Coding screen (Jay's direct request
    for a visible budget-checked confirmation)."""
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not signed in"}, status_code=401)
    org = _current_org(request)
    if not org:
        return {"budget_found": False}

    row = db.query_one(
        """
        SELECT ga.account_number, pga.overspend_buffer_amount
        FROM checkreq.gl_accounts ga
        JOIN checkreq.program_area_gl_accounts pga
            ON pga.gl_account_id = ga.id AND pga.program_area_id = %s
        WHERE ga.id = %s AND ga.org_id = %s
        """,
        (program_area_id, gl_account_id, org["id"]),
    )
    if not row:
        return {"budget_found": False}

    status, err = qbo_mcp_client.get_budget_status(
        (org.get("code") or "").lower(), row["account_number"], date.today().year
    )
    if err or not status or not status.get("budget_found"):
        return {"budget_found": False}

    projected = round(status["actual_spend"] + amount, 2)
    annual_budget = float(status["annual_budget"])
    buffer_amount = float(row["overspend_buffer_amount"])
    if projected <= annual_budget:
        tier = "ok"
    elif projected <= annual_budget + buffer_amount:
        tier = "buffer_notice"
    else:
        tier = "cfo_required"
    return {
        "budget_found":    True,
        "annual_budget":   annual_budget,
        "actual_spend":    status["actual_spend"],
        "amount":          amount,
        "projected":       projected,
        "buffer_amount":   buffer_amount,
        "tier":            tier,
    }


@app.post("/api/budget-check-submission")
async def api_budget_check_submission(request: Request):
    """Pre-flight check the GL Coding screen calls right before actually
    submitting, so a tier-3 (over budget beyond the account's buffer) line
    can show a real confirmation dialog BEFORE the request is created --
    Jay's direct request: "the user can be asked if they want to submit
    this." Accepts the same program_area_id/gl_account_id[]/gl_amount[]
    shape the real submission form posts, and just runs
    _evaluate_gl_line_budgets against them -- new_request_submit re-runs
    the exact same check server-side as the authoritative gate regardless
    of what this pre-flight call found, so there's no way to bypass it by
    skipping or spoofing this endpoint."""
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not signed in"}, status_code=401)
    org = _current_org(request)
    if not org:
        return JSONResponse({"error": "No entity selected"}, status_code=400)

    form = await request.form()
    program_area_id = int(form["program_area_id"])
    gl_account_ids = form.getlist("gl_account_id")
    gl_amounts = form.getlist("gl_amount")
    gl_lines = [
        (int(a), float(amt), "")
        for a, amt in zip(gl_account_ids, gl_amounts)
        if a and amt
    ]
    result = _evaluate_gl_line_budgets(org, program_area_id, gl_lines)
    return {
        "buffer_notice": [e["detail"] for e in result["buffer_notice"]],
        "cfo_required": [e["detail"] for e in result["cfo_required"]],
    }


_EXTRACT_MAX_BYTES = 10 * 1024 * 1024
_EXTRACT_ALLOWED_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp"}


@app.post("/api/extract-document")
async def api_extract_document(request: Request, file: UploadFile):
    """Reads an uploaded invoice/receipt and tries to prefill the check-
    request form. Extraction failure must never block the manual-entry
    path -- always returns a JSON body (never a 500 the JS can't handle),
    with a soft {"error": ...} on any problem."""
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not signed in"}, status_code=401)
    org = _current_org(request)
    if not org:
        return JSONResponse({"error": "No entity selected"}, status_code=400)

    content = await file.read()
    if len(content) > _EXTRACT_MAX_BYTES:
        return JSONResponse({"error": "File is too large (max 10MB)."})
    mime_type = file.content_type or ""
    if mime_type not in _EXTRACT_ALLOWED_TYPES:
        return JSONResponse({"error": f"Unsupported file type ({mime_type or 'unknown'}). "
                                       f"Use a PDF or JPG/PNG/GIF/WebP image -- note iPhone photos "
                                       f"default to HEIC, which isn't supported; export as JPG or PDF."})

    try:
        result = await asyncio.to_thread(document_extract.extract_fields, content, mime_type)
    except Exception as exc:
        # Log the real detail server-side, but never echo a raw internal
        # exception (could contain SDK/API error text) back to the client --
        # tightened 2026-07-25 for the first public Cloud Run deploy.
        print(f"[extract-document] {type(exc).__name__}: {exc}")
        return JSONResponse({"error": "Couldn't read this document. Please fill in the form manually."})

    matched_vendor_id = None
    vendor_name = result.get("vendor_name")
    if vendor_name:
        match = db.query_one(
            "SELECT id FROM checkreq.vendors WHERE org_id = %s AND is_active AND display_name ILIKE %s "
            "ORDER BY display_name LIMIT 1",
            (org["id"], f"%{vendor_name}%"),
        )
        if match:
            matched_vendor_id = match["id"]

    result["matched_vendor_id"] = matched_vendor_id
    return result


_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
         "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _three_digits_to_words(n: int) -> str:
    s = ""
    if n >= 100:
        s += f"{_ONES[n // 100]} Hundred "
        n %= 100
    if n >= 20:
        s += f"{_TENS[n // 10]} "
        n %= 10
    elif n >= 10:
        s += f"{_ONES[n]} "
        n = 0
    if n > 0:
        s += f"{_ONES[n]} "
    return s.strip()


def _amount_to_words(amount: float) -> str:
    """Mirrors new_request.js's client-side amountInWords() -- same
    algorithm, kept in sync deliberately since one drives the live preview
    and this one drives the final PDF."""
    amount = round(amount, 2)
    dollars = int(amount)
    cents = round((amount - dollars) * 100)
    scales = [("", 1), ("Thousand", 1_000), ("Million", 1_000_000), ("Billion", 1_000_000_000)]
    remaining = dollars
    parts = []
    for name, size in reversed(scales):
        chunk = remaining // size
        if chunk > 0:
            parts.append(f"{_three_digits_to_words(chunk)}{' ' + name if name else ''}")
            remaining %= size
    dollars_words = " ".join(parts).strip() or "Zero"
    return f"{dollars_words} and {cents:02d}/100 Dollars"


def _voucher_context(payment_request_id: int) -> dict | None:
    """Builds the same voucher_* context keys new_request_form seeds with
    blanks -- used to render check_voucher.html with real data for the PDF."""
    pr = db.query_one(
        """
        SELECT pr.*, o.name AS org_name, pa.title AS program_area_title,
               v.display_name AS vendor_name,
               vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
               vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
               vr.dba_name AS vr_dba_name, vr.status AS vr_status,
               u.display_name AS submitter_name, u.email AS submitter_email
        FROM checkreq.payment_requests pr
        JOIN checkreq.organizations o ON o.id = pr.org_id
        JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
        LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
        LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
        JOIN checkreq.app_users u ON u.id = pr.submitter_user_id
        WHERE pr.id = %s
        """,
        (payment_request_id,),
    )
    if not pr:
        return None

    # A payment_request may reference a not-yet-QBO-vendor "new vendor"
    # request instead of an existing checkreq.vendors row (New Vendor
    # Onboarding Plan.md) -- resolve a display name from whichever is set.
    # exactly one of vendor_id/vendor_request_id is ever set per request,
    # enforced in new_request_submit, not a DB constraint (per the plan's
    # own stated preference for this codebase).
    vendor_name = pr["vendor_name"]
    if not vendor_name and pr.get("vr_entity_type"):
        vendor_name = _vendor_request_row_display_name(pr["vr_entity_type"], pr["vr_company_name"],
                                                         pr["vr_dba_name"], pr["vr_first_name"],
                                                         pr["vr_last_name"])
        if pr.get("vr_status") == "pending_approval":
            vendor_name = f"{vendor_name} (new vendor, pending approval)"
    gl_lines = db.query(
        """
        SELECT gl.amount, gl.memo, ga.account_number, ga.account_name
        FROM checkreq.payment_request_gl_lines gl
        JOIN checkreq.gl_accounts ga ON ga.id = gl.gl_account_id
        WHERE gl.payment_request_id = %s ORDER BY gl.id
        """,
        (payment_request_id,),
    )
    amount = float(pr["amount"])

    # Jay, 2026-07-29: "once the approval process is completed, the PDF
    # should be revised to have a section of the approval chain with the
    # exact approvers with their date/time and IP." Real, already-acted
    # records only -- distinct from voucher_chain_summary above, which is
    # the pre-approval "who is/will be in this chain" preview text and
    # never reflects who actually acted or when.
    approval_records = db.query(
        """
        SELECT aa.serial_group, aa.status, aa.acted_at, aa.ip_address,
               u.display_name, u.email
        FROM checkreq.approval_actions aa
        JOIN checkreq.app_users u ON u.id = aa.approver_user_id
        WHERE aa.payment_request_id = %s AND aa.status IN ('approved', 'rejected')
        ORDER BY aa.acted_at
        """,
        (payment_request_id,),
    )

    return {
        "voucher_org_name": pr["org_name"],
        "voucher_request_number": pr["request_number"],
        "voucher_date": pr["requested_pay_date"].strftime("%B %d, %Y") if pr["requested_pay_date"] else "—",
        "voucher_vendor": vendor_name or "—",
        "voucher_amount": f"${amount:,.2f}",
        "voucher_amount_words": _amount_to_words(amount),
        "voucher_program_area": pr["program_area_title"],
        "voucher_description": pr["description"] or "—",
        "voucher_gl_lines": [
            {"account": f"{g['account_number']} - {g['account_name']}", "amount": float(g["amount"]), "memo": g["memo"]}
            for g in gl_lines
        ],
        "voucher_total": f"${amount:,.2f}",
        "voucher_requested_by": pr["submitter_name"] or pr["submitter_email"],
        "voucher_chain_summary": pr["approval_chain_summary"] or "—",
        "voucher_budget_checked_at": (
            pr["budget_checked_at"].strftime("%Y-%m-%d %H:%M:%S UTC") if pr.get("budget_checked_at") else None
        ),
        "voucher_approval_records": [
            {
                "name": a["display_name"] or a["email"],
                "status": a["status"],
                "acted_at": a["acted_at"].strftime("%Y-%m-%d %H:%M:%S UTC") if a["acted_at"] else "—",
                "ip_address": a["ip_address"] or "—",
            }
            for a in approval_records
        ],
    }


def _vendor_request_row_display_name(entity_type: str, company_name: str | None, dba_name: str | None,
                                      first_name: str | None, last_name: str | None) -> str:
    """Vendor display name for a checkreq.vendor_requests row, mirroring
    the plan's Section 2 preview rule: individual -> 'First Last',
    entity -> Company Name. Used both by the live voucher preview's server
    side (_voucher_context) and anywhere else a vendor_request needs a
    human-readable label (the approval queue, the W-9 email)."""
    if entity_type == "entity":
        return (company_name or dba_name or "New Vendor").strip()
    full = f"{first_name or ''} {last_name or ''}".strip()
    return full or "New Vendor"


def _vendor_request_display_name(vr: dict) -> str:
    """Same as _vendor_request_row_display_name but takes a full
    vendor_requests row dict (as returned by db.query/query_one)."""
    return _vendor_request_row_display_name(
        vr.get("entity_type"), vr.get("company_name"), vr.get("dba_name"),
        vr.get("first_name"), vr.get("last_name"),
    )


def _custom_titlecase(name: str) -> str:
    """Archived-filename vendor casing: lowercase each word, capitalize only
    the first character if it's alphabetic (so '29TH' -> '29th', not
    '29Th'), strip spaces. Reproduces "29TH STREET TAVERN" -> "29thStreetTavern"
    (Jay-confirmed example). Known, accepted limitation: acronym vendor names
    like "ABC LLC" become "AbcLlc", not "ABCLlc" -- no casing rule gets every
    vendor name right without a manual per-vendor override."""
    words = []
    for w in name.split():
        w = w.lower()
        if w and w[0].isalpha():
            w = w[0].upper() + w[1:]
        words.append(w)
    return "".join(words)


_EXT_BY_CONTENT_TYPE = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _guess_extension(filename: str, content_type: str) -> str:
    if content_type in _EXT_BY_CONTENT_TYPE:
        return _EXT_BY_CONTENT_TYPE[content_type]
    if "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    return "bin"


def _compute_archived_filename(org_code: str, pay_date_str: str | None, vendor_name: str | None,
                                amount: float, request_number: str, index: int, ext: str) -> str:
    """{ENTITY_CODE} {YYYY.MM.DD} {VendorNameNoSpaces} {amount} {request_number}[-N].ext
    (Jay-confirmed format). The request_number suffix guarantees uniqueness --
    without it, Graph API's PUT-to-path silently overwrites on any
    same-day/same-vendor/same-amount collision (confirmed: none of this
    codebase's existing sharepoint_auth.py implementations do a pre-upload
    existence check). index > 1 (multiple attachments on one request) gets a
    plain "-2"/"-3" suffix -- the always-present generated PDF is always
    index 1, with no suffix."""
    date_str = pay_date_str.replace("-", ".") if pay_date_str else datetime.today().strftime("%Y.%m.%d")
    vendor_part = _custom_titlecase(vendor_name) if vendor_name else "NoVendor"
    base = f"{org_code.upper()} {date_str} {vendor_part} {amount:.2f} {request_number}"
    if index > 1:
        base += f"-{index}"
    return f"{base}.{ext}"


def _vendor_display_name_for_request(payment_request_id: int) -> str | None:
    """Resolves the vendor display name used by the archived-filename
    convention (_compute_archived_filename) -- either an existing vendor's
    display_name, or a new-vendor-onboarding row's computed display name.
    Extracted out of _archive_attachments (2026-07-25, attachment
    view/delete/add work) so the Add-attachment-during-edit path
    (add_attachment route) can reuse the identical lookup instead of
    duplicating it -- pure extraction, no behavior change for the original
    submission-time archival path below."""
    vendor_row = db.query_one(
        "SELECT v.display_name, vr.entity_type, vr.company_name, vr.dba_name, "
        "vr.first_name, vr.last_name "
        "FROM checkreq.payment_requests pr "
        "LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id "
        "LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id "
        "WHERE pr.id = %s",
        (payment_request_id,),
    )
    if not vendor_row:
        return None
    vendor_name = vendor_row["display_name"]
    if not vendor_name and vendor_row.get("entity_type"):
        vendor_name = _vendor_request_row_display_name(
            vendor_row["entity_type"], vendor_row["company_name"], vendor_row["dba_name"],
            vendor_row["first_name"], vendor_row["last_name"],
        )
    return vendor_name


def _active_attachments(payment_request_id: int) -> list[dict]:
    """Attachments currently visible on the Edit page -- excludes any
    soft-removed (removed_at IS NOT NULL) rows. The generated PDF always
    sorts first (matching its always-index-1 archived-filename convention),
    then user uploads in the order they were added."""
    return db.query(
        """
        SELECT id, source, original_filename, archived_filename, content_type,
               size_bytes, uploaded_at, sp_file_path
        FROM checkreq.payment_request_attachments
        WHERE payment_request_id = %s AND removed_at IS NULL
        ORDER BY CASE WHEN source = 'generated_pdf' THEN 0 ELSE 1 END, uploaded_at, id
        """,
        (payment_request_id,),
    )


def _archive_attachments(org: dict, payment_request_id: int, request_number: str,
                          pay_date_str: str | None, total_amount: float, user_id: int,
                          uploaded_attachments: list[tuple[str, str, bytes]]) -> None:
    """Uploads the always-present generated check-voucher PDF (source=
    'generated_pdf') plus any optional user-uploaded supporting documents
    (source='user_upload') to GCS staging then the entity's permanent
    SharePoint archive, recording both locations in
    checkreq.payment_request_attachments. Raises on any failure -- the
    caller (new_request_submit) treats this as a soft/recoverable error
    that must not roll back or fail an otherwise-successful submission,
    since it runs AFTER that submission's own DB transaction has already
    committed."""
    if not (org.get("sp_hostname") and org.get("sp_site_path") and org.get("sp_library_folder")):
        raise RuntimeError(
            f"No SharePoint archive location configured for {org['name']} -- "
            f"set sp_hostname/sp_site_path/sp_library_folder in checkreq.organizations."
        )

    vendor_name = _vendor_display_name_for_request(payment_request_id)

    items = [{
        "source": "generated_pdf",
        "original_filename": f"{request_number}.pdf",
        "content_type": "application/pdf",
        "data": render_check_voucher_pdf(payment_request_id),
    }]
    for filename, content_type, data in uploaded_attachments:
        items.append({
            "source": "user_upload",
            "original_filename": filename,
            "content_type": content_type or "application/octet-stream",
            "data": data,
        })

    token = sharepoint_client.get_access_token()
    site_id = sharepoint_client.get_site_id(token, org["sp_hostname"], org["sp_site_path"])

    uploaded_gcs = []  # (bucket, blob_path) uploaded so far -- best-effort rollback if a later item fails
    saved_rows = []
    try:
        for i, item in enumerate(items, start=1):
            ext = _guess_extension(item["original_filename"], item["content_type"])
            archived_filename = _compute_archived_filename(
                org["code"], pay_date_str, vendor_name, total_amount, request_number, i, ext,
            )
            blob_path = f"{request_number}/{archived_filename}"
            gcs_client.upload_bytes(ATTACHMENTS_BUCKET, blob_path, item["data"], item["content_type"])
            uploaded_gcs.append((ATTACHMENTS_BUCKET, blob_path))

            sp_result = sharepoint_client.upload_bytes(
                token, site_id, org["sp_library_folder"], archived_filename, item["data"], item["content_type"],
            )
            saved_rows.append({
                "source": item["source"],
                "original_filename": item["original_filename"],
                "archived_filename": archived_filename,
                "content_type": item["content_type"],
                "size_bytes": len(item["data"]),
                "gcs_bucket": ATTACHMENTS_BUCKET,
                "gcs_blob_path": blob_path,
                "sp_file_path": f"{org['sp_library_folder'].strip('/')}/{archived_filename}",
                "sp_web_url": sp_result.get("webUrl"),
            })
    except Exception:
        for bucket, blob_path in uploaded_gcs:
            try:
                gcs_client.delete_blob(bucket, blob_path)
            except Exception:
                pass  # best-effort only -- the original error is what matters, re-raised below
        raise

    with db.connect() as conn:
        with conn.cursor() as cur:
            for row in saved_rows:
                cur.execute(
                    """
                    INSERT INTO checkreq.payment_request_attachments
                        (payment_request_id, source, original_filename, archived_filename,
                         content_type, size_bytes, gcs_bucket, gcs_blob_path, sp_file_path,
                         sp_web_url, uploaded_by_user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (payment_request_id, row["source"], row["original_filename"], row["archived_filename"],
                     row["content_type"], row["size_bytes"], row["gcs_bucket"], row["gcs_blob_path"],
                     row["sp_file_path"], row["sp_web_url"], user_id),
                )


def cleanup_gcs_attachment(payment_request_id: int) -> None:
    """Deletes every GCS-staged copy for a request and stamps gcs_deleted_at
    -- NOT called from anywhere yet. There is no "approved -> posted to QBO"
    trigger built in this app (no approval-action workflow, no QBO-posting
    trigger wired to anything -- see CLAUDE.md). Call this once that trigger
    exists; the bucket's own 180-day lifecycle rule is the real backstop
    until then."""
    rows = db.query(
        "SELECT id, gcs_bucket, gcs_blob_path FROM checkreq.payment_request_attachments "
        "WHERE payment_request_id = %s AND gcs_deleted_at IS NULL AND gcs_blob_path IS NOT NULL",
        (payment_request_id,),
    )
    for row in rows:
        gcs_client.delete_blob(row["gcs_bucket"], row["gcs_blob_path"])
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE checkreq.payment_request_attachments SET gcs_deleted_at = NOW() WHERE id = %s",
                    (row["id"],),
                )


_W9_PDF_PATH = os.path.join(os.path.dirname(__file__), "static", "forms", "fw9.pdf")


def _send_w9_request_email(vr: dict, org_name: str, request: Request) -> dict:
    """Sends the W-9 request email for a just-approved vendor_request whose
    requires_w9 is TRUE (Section 4/4a). Attaches the official public IRS
    Form W-9 PDF (fetched 2026-07-25 from irs.gov -- static/forms/fw9.pdf,
    a standard non-proprietary government form, per the plan's own
    resolution of this open question) and embeds the unique, unguessable
    upload link. Reuses 26-122 Cloud Email Server's existing send_email
    mechanism via email_client.py -- no new email infrastructure.

    Returns whatever email_client.send_email() returns
    ({"status": "sent"} or {"error": "..."}) -- never raises. The caller
    (vendor_request_approve) stamps w9_email_sent_at only on a real
    {"status": "sent"}, matching this project's existing archive_warning
    graceful-degradation pattern for a recoverable-but-visible failure."""
    vendor_name = _vendor_request_display_name(vr)
    base_url = str(request.base_url).rstrip("/")
    upload_url = f"{base_url}/vendor-w9-upload/{vr['upload_token']}"
    subject = f"W-9 Request — {vendor_name}"
    body_html = (
        f"<p>Hello {vr.get('contact_name') or ''},</p>"
        f"<p>{org_name} is setting up <strong>{vendor_name}</strong> as a new payee and "
        f"needs a completed IRS Form W-9 on file before any payment can be issued.</p>"
        f"<p>A blank W-9 is attached for reference. Please complete it and upload it "
        f"using the secure link below:</p>"
        f'<p><a href="{upload_url}">{upload_url}</a></p>'
        f"<p>Thank you,<br>{org_name} Business Office</p>"
    )
    body_text = (
        f"Hello {vr.get('contact_name') or ''},\n\n"
        f"{org_name} is setting up {vendor_name} as a new payee and needs a completed "
        f"IRS Form W-9 on file before any payment can be issued.\n\n"
        f"A blank W-9 is attached for reference. Please complete it and upload it using "
        f"this secure link:\n{upload_url}\n\n"
        f"Thank you,\n{org_name} Business Office"
    )

    attachments = None
    try:
        with open(_W9_PDF_PATH, "rb") as f:
            pdf_bytes = f.read()
        attachments = [{
            "name": "Form W-9 (blank).pdf",
            "content_type": "application/pdf",
            "content_base64": base64.b64encode(pdf_bytes).decode("ascii"),
        }]
    except OSError:
        pass  # send without the attachment rather than blocking the email entirely

    return email_client.send_email(
        to=vr["contact_email"],
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        sender=W9_SENDER_EMAIL,
        attachments=attachments,
    )


def _vendor_request_by_upload_token(token: str) -> dict | None:
    """404-safe lookup for the unauthenticated /vendor-w9-upload/{token}
    route (Section 4a). Returns None (never a distinguishing error) for a
    bad token OR a token whose vendor_request has left 'approved' status --
    the upload window closes automatically the moment approval status
    changes (rejected / posted_to_qbo), without relying on the vendor to
    notice."""
    if not token:
        return None
    return db.query_one(
        """
        SELECT vr.*, o.name AS org_name, o.code AS org_code, o.sp_hostname,
               o.sp_site_path, o.sp_library_folder
        FROM checkreq.vendor_requests vr
        JOIN checkreq.organizations o ON o.id = vr.org_id
        WHERE vr.upload_token = %s AND vr.status = 'approved'
        """,
        (token,),
    )


# Type-abbreviation prefix used both by _next_request_number() and by the My
# Requests table/legend (Task 1/5, 2026-07-26). "??" is a deliberate,
# visible fallback rather than a silent guess if a third request_type is ever
# added without updating this dict too.
_REQUEST_TYPE_ABBR = {"check_request": "CR", "invoice_payment": "IV"}


def _next_request_number(request_type: str) -> str:
    """New format (2026-07-26, Task 5): '{CR|IV}{YY}-{NNN}', e.g. 'CR26-001',
    resetting per calendar YEAR (not per day, the old 'CR-YYYYMMDD-NNN'
    format's behavior) and prefixed by request type. Sequence is the max
    existing sequence number under this exact prefix + 1 -- extracted via a
    trailing-digits regex rather than COUNT(*), so a gap (e.g. a deleted test
    row) can never cause a duplicate request_number."""
    prefix = _REQUEST_TYPE_ABBR.get(request_type, "XX")
    yy = date.today().strftime("%y")
    full_prefix = f"{prefix}{yy}-"
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                r"SELECT COALESCE(MAX(CAST(SUBSTRING(request_number FROM '(\d+)$') AS INTEGER)), 0) AS max_seq "
                r"FROM checkreq.payment_requests WHERE request_number LIKE %s",
                (f"{full_prefix}%",),
            )
            max_seq = cur.fetchone()["max_seq"]
    return f"{full_prefix}{max_seq + 1:03d}"


@app.post("/new-request")
async def new_request_submit(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    # Session is authoritative for org_id -- never trust the posted form
    # field (kept only for no-JS resilience). Closes a real edge case: a
    # stale hidden field could otherwise submit against the wrong org if
    # the entity was switched in another tab mid-fill.
    org = _current_org(request)
    if not org:
        return RedirectResponse("/portal")
    org_id = org["id"]

    form = await request.form()
    program_area_id = int(form["program_area_id"])

    if not _user_can_submit_for(user, program_area_id):
        return JSONResponse(
            {"error": "You are not assigned to this program area. "
                      "Ask an admin to add you in checkreq.user_program_areas."},
            status_code=403,
        )

    request_type = form.get("request_type", "check_request")

    # Editing an existing request (see edit_request_form) instead of
    # submitting a new one -- editing_request_number is a hidden field that
    # route's template renders only when editing. Re-verify ownership and
    # the locking rule server-side too (never trust that the GET route's own
    # checks are the only gate -- a POST can be replayed/crafted directly).
    editing_request_number = form.get("editing_request_number") or None
    existing_pr = None
    if editing_request_number:
        existing_pr = db.query_one(
            "SELECT * FROM checkreq.payment_requests WHERE request_number = %s",
            (editing_request_number,),
        )
        if not existing_pr:
            return JSONResponse({"error": "Request not found."}, status_code=404)
        if existing_pr["submitter_user_id"] != user["id"]:
            return JSONResponse({"error": "Not authorized to edit this request."}, status_code=403)
        if not _request_is_editable(existing_pr["status"]):
            return JSONResponse(
                {"error": "This request has been posted to QBO and can no longer be edited."},
                status_code=403,
            )
        if existing_pr["org_id"] != org_id:
            # Shouldn't happen (edit_request_form pins the session's entity
            # to the request's own org before rendering the form), but guard
            # rather than silently letting a cross-org edit through if the
            # entity switcher was used mid-edit.
            return JSONResponse(
                {"error": "Entity mismatch -- please reopen this request's Edit link and try again."},
                status_code=400,
            )

    # New Vendor Onboarding (New Vendor Onboarding Plan.md, Section 2): the
    # "Add new vendor" panel and the existing-vendor Tom Select are mutually
    # exclusive -- using_new_vendor (a hidden field set by new_request.js
    # when the panel is shown) decides which branch below applies. Exactly
    # one of vendor_id / new_vendor_* is ever honored, enforced here in
    # application code rather than a DB constraint, per the plan's own
    # stated preference for this codebase.
    using_new_vendor = form.get("using_new_vendor") == "1"
    vendor_id = int(form["vendor_id"]) if (not using_new_vendor and form.get("vendor_id")) else None

    new_vendor_fields: dict | None = None
    if using_new_vendor:
        nv_entity_type = form.get("new_vendor_entity_type", "individual")
        if nv_entity_type not in ("individual", "entity"):
            return JSONResponse({"error": "Invalid new-vendor entity type."}, status_code=400)
        new_vendor_fields = {
            "entity_type": nv_entity_type,
            "first_name": form.get("new_vendor_first_name", "").strip() or None,
            "last_name": form.get("new_vendor_last_name", "").strip() or None,
            "company_name": form.get("new_vendor_company_name", "").strip() or None,
            "dba_name": form.get("new_vendor_dba_name", "").strip() or None,
            "address_line1": form.get("new_vendor_address_line1", "").strip() or None,
            "address_line2": form.get("new_vendor_address_line2", "").strip() or None,
            "city": form.get("new_vendor_city", "").strip() or None,
            "state": form.get("new_vendor_state", "").strip() or None,
            "zip": form.get("new_vendor_zip", "").strip() or None,
            "phone": form.get("new_vendor_phone", "").strip() or None,
            "contact_name": form.get("new_vendor_contact_name", "").strip(),
            "contact_email": form.get("new_vendor_contact_email", "").strip(),
        }
        # Server-side validation -- HTML5 `required` on the client is not a
        # real guarantee, same reasoning as the Pay Date check just below.
        if nv_entity_type == "individual" and not (new_vendor_fields["first_name"] and new_vendor_fields["last_name"]):
            return JSONResponse({"error": "First Name and Last Name are required for an individual vendor."}, status_code=400)
        if nv_entity_type == "entity" and not new_vendor_fields["company_name"]:
            return JSONResponse({"error": "Company Name is required for an entity vendor."}, status_code=400)
        if not new_vendor_fields["address_line1"] or not new_vendor_fields["city"] \
                or not new_vendor_fields["state"] or not new_vendor_fields["zip"]:
            return JSONResponse({"error": "Address Line 1, City, State, and Zip are required for a new vendor."}, status_code=400)
        if not new_vendor_fields["contact_name"] or not new_vendor_fields["contact_email"]:
            return JSONResponse({"error": "Contact Name and Contact Email are required for a new vendor "
                                           "(the person to email about the W-9 -- may differ from the vendor itself)."}, status_code=400)

    # Real bug found live 2026-07-25 (Jay): document.upload-to-prefill's
    # setTextboxValue() (see applyExtractedFields() in new_request.js) writes
    # display text into Tom Select's search box WITHOUT ever calling
    # addItem() when there's no matched_vendor_id -- so the underlying
    # <select>'s value stays empty. Combined with using_new_vendor staying
    # "0" (the "Add a new vendor" panel was never opened), a submission could
    # go through with NEITHER a real vendor_id NOR a new_vendor_fields dict at
    # all. HTML5 `required` on a Tom Select-controlled <select> is not a
    # reliable guard here -- Tom Select keeps the backing <select> at
    # display:none, and the HTML5 spec excludes display:none elements from
    # constraint validation entirely, regardless of required/value state.
    # This server-side check is the definitive fix, matching every other
    # "required" field in this route's existing validation above.
    if not using_new_vendor and vendor_id is None:
        return JSONResponse(
            {"error": "Please select a vendor from the list, or add a new vendor."},
            status_code=400,
        )

    requested_pay_date = form.get("requested_pay_date") or None
    if not requested_pay_date:
        # HTML5 `required` on the field is not a real guarantee (a client
        # can post around it) -- this is the request date of the CR itself,
        # not optional, and it also drives the archived filename's date
        # component, so a missing value can't be allowed through.
        return JSONResponse({"error": "Pay Date is required."}, status_code=400)
    description = form.get("description", "")
    special_instructions = form.get("special_instructions", "")

    gl_account_ids = form.getlist("gl_account_id")
    gl_amounts = form.getlist("gl_amount")
    gl_memos = form.getlist("gl_memo")
    gl_lines = [
        (int(a), float(amt), memo)
        for a, amt, memo in zip(gl_account_ids, gl_amounts, gl_memos)
        if a and amt
    ]
    total_amount = round(sum(amt for _, amt, _ in gl_lines), 2)

    # Approval Workflow Corrections (2026-07-31): three-tier budget check,
    # runs BEFORE any database write, for both the new-submission and edit
    # branches below (this check sits above the branch split). Tier 3 (over
    # budget beyond the account's buffer) never blocks outright anymore --
    # instead the submitter must explicitly confirm (confirmed_overbudget=1,
    # set by the UI's confirmation dialog after a first attempt without it)
    # before the request is created; confirming still requires real CFO
    # approval, added to the chain below.
    budget_result = _evaluate_gl_line_budgets(org, program_area_id, gl_lines)
    confirmed_overbudget = form.get("confirmed_overbudget") == "1"
    if budget_result["cfo_required"] and not confirmed_overbudget:
        return JSONResponse(
            {
                "needs_overbudget_confirmation": True,
                "cfo_required": [e["detail"] for e in budget_result["cfo_required"]],
            },
            status_code=409,
        )
    overspend_flagged = bool(budget_result["buffer_notice"] or budget_result["cfo_required"])
    overspend_detail = "\n".join(
        e["detail"] for e in budget_result["buffer_notice"] + budget_result["cfo_required"]
    ) or None

    # Optional user attachments (not required -- see form). Read all bytes
    # now, while we still have the async UploadFile objects; everything
    # downstream (archival) works with plain bytes.
    uploaded_attachments: list[tuple[str, str, bytes]] = []
    for f in form.getlist("attachments"):
        if getattr(f, "filename", None):
            content = await f.read()
            if content:
                uploaded_attachments.append((f.filename, f.content_type or "application/octet-stream", content))

    # Approval Workflow Corrections (Jay, 2026-07-31): a self-payment --
    # this vendor row is linked to the submitter's own login -- always
    # requires CFO approval, bypassing the normal Program-Area chain and
    # the entity global-approver step entirely, regardless of amount or the
    # submitter's own authorization. Only meaningful for an EXISTING vendor
    # (using_new_vendor has no linked_user_id concept -- see
    # _is_self_payment's own docstring).
    is_self_payment = (not using_new_vendor) and _is_self_payment(vendor_id, user["id"])
    if is_self_payment:
        chain = _self_payment_cfo_chain(user["id"])
        chain_summary = (
            "Self-payment -- requires CFO approval (any one), regardless of amount "
            "or the submitter's own authorization."
            if chain else
            "Self-payment -- no CFO configured to approve this. Needs setup."
        )
    else:
        chain = approval_engine.build_approval_chain(program_area_id, org_id, total_amount)
        chain_summary = approval_engine.describe_chain(chain)

    # Tier 3 budget overage: append a real CFO approval step on top of
    # whatever chain was already computed above. Skipped when this is
    # already a self-payment chain -- that chain is ALREADY CFO-only, and
    # a second, redundant CFO group would add nothing (the same sign-off
    # already covers both reasons a CFO needed to look at this).
    if budget_result["cfo_required"] and not is_self_payment:
        next_group = (max((c["serial_group"] for c in chain), default=0)) + 1
        cfo_budget_group = [
            {"serial_group": next_group, "approver_user_id": u["id"], "approver_email": u["email"],
             "approver_name": u["display_name"], "backup_approver_id": None, "any_one_suffices": True}
            for u in _cfo_approver_rows(user["id"])
        ]
        chain = chain + cfo_budget_group
        chain_summary += "\n" + (
            f"Group {next_group}: CFO approval required -- over budget beyond the account's allowed buffer."
            if cfo_budget_group else
            "Over budget beyond buffer -- no CFO configured to approve. Needs setup."
        )
    first_step = chain[0] if chain else None
    # current_approver_id/serial_group_current are display-only fields (see
    # _serial_group_display_approver's own docstring) -- the real gate is
    # checkreq.approval_actions, materialized below at both the new-
    # submission and edit-reset call sites.
    first_serial_group = first_step["serial_group"] if first_step else None
    first_display_approver = _serial_group_display_approver(chain, first_serial_group)

    imp_id = request.session.get("impersonating_user_id")
    impersonated_by = _real_user(request)["id"] if imp_id else None

    if existing_pr:
        # ── EDIT branch ──────────────────────────────────────────────────
        # Approval reset rule, Jay's exact words (2026-07-25): "If you do
        # edit the CR's Vendor or Amount, you have to erase the approval
        # workflow and restart." Compare the NEW vendor identity (vendor_id
        # or vendor_request_id, whichever applies) and the NEW total against
        # what was stored on the row BEFORE this update. Vendor-identity
        # comparison: if using_new_vendor and the request already had a
        # vendor_request_id, that same row is updated in place below (same
        # id, not a new one) -- so by this rule that is NOT a vendor change
        # (the plan gives no reason to force a re-approval just because the
        # new vendor's address/contact fields were corrected). Any other
        # combination (switching between an existing vendor and a new one,
        # or picking a different existing vendor_id) IS a vendor-identity
        # change.
        request_number = existing_pr["request_number"]
        payment_request_id = existing_pr["id"]
        old_vendor_id = existing_pr["vendor_id"]
        old_vendor_request_id = existing_pr["vendor_request_id"]
        old_total = round(float(existing_pr["amount"]), 2)

        if using_new_vendor:
            vendor_changed = old_vendor_request_id is None
        else:
            vendor_changed = (vendor_id != old_vendor_id)
        amount_changed = (total_amount != old_total)
        # Decision 1, second half (AP Review Workflow Plan.md, Section 2c):
        # a real gap the plan found -- a request left in a terminal-but-
        # fixable state ('Rejected' mid-chain, or 'Returned by AP' from the
        # AP screen) must ALWAYS re-enter the chain on edit, regardless of
        # whether vendor/amount changed. Without this, editing only a
        # description/GL-line memo on such a request would save
        # successfully but silently stay in that terminal status forever,
        # with no path back into review.
        status_forces_reset = existing_pr["status"] in ("Rejected", "Returned by AP")
        reset_approval = vendor_changed or amount_changed or status_forces_reset

        with db.connect() as conn:
            with conn.cursor() as cur:
                # Vendor bookkeeping first, so the resolved vendor_request_id
                # is known before the payment_requests UPDATE below. NOTE:
                # only Vendor/Amount changes reset approval per Jay's rule
                # above -- this vendor_requests write itself always applies
                # (a corrected address on an unrelated field shouldn't be
                # silently discarded just because it doesn't trigger a
                # re-approval).
                new_vendor_request_id = old_vendor_request_id
                if using_new_vendor:
                    requires_w9 = total_amount > VENDOR_W9_AMOUNT_THRESHOLD
                    if old_vendor_request_id:
                        # Same underlying vendor_request row this payment
                        # request already pointed to -- update its fields in
                        # place rather than creating a duplicate row (a
                        # payment_request has at most one vendor_request,
                        # per the schema's 1:1 FK design).
                        cur.execute(
                            """
                            UPDATE checkreq.vendor_requests SET
                                entity_type = %s, first_name = %s, last_name = %s,
                                company_name = %s, dba_name = %s, address_line1 = %s,
                                address_line2 = %s, city = %s, state = %s, zip = %s,
                                phone = %s, contact_name = %s, contact_email = %s,
                                requires_w9 = %s
                            WHERE id = %s
                            """,
                            (new_vendor_fields["entity_type"], new_vendor_fields["first_name"],
                             new_vendor_fields["last_name"], new_vendor_fields["company_name"],
                             new_vendor_fields["dba_name"], new_vendor_fields["address_line1"],
                             new_vendor_fields["address_line2"], new_vendor_fields["city"],
                             new_vendor_fields["state"], new_vendor_fields["zip"],
                             new_vendor_fields["phone"], new_vendor_fields["contact_name"],
                             new_vendor_fields["contact_email"], requires_w9, old_vendor_request_id),
                        )
                    else:
                        # Switching FROM an existing vendor (or none) TO a
                        # brand-new one -- same INSERT shape as the new-
                        # submission path.
                        upload_token = pysecrets.token_urlsafe(32)
                        cur.execute(
                            """
                            INSERT INTO checkreq.vendor_requests
                                (org_id, payment_request_id, entity_type, first_name, last_name,
                                 company_name, dba_name, address_line1, address_line2, city, state, zip,
                                 phone, contact_name, contact_email, requires_w9, upload_token,
                                 created_by_user_id)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            RETURNING id
                            """,
                            (org_id, payment_request_id, new_vendor_fields["entity_type"],
                             new_vendor_fields["first_name"], new_vendor_fields["last_name"],
                             new_vendor_fields["company_name"], new_vendor_fields["dba_name"],
                             new_vendor_fields["address_line1"], new_vendor_fields["address_line2"],
                             new_vendor_fields["city"], new_vendor_fields["state"], new_vendor_fields["zip"],
                             new_vendor_fields["phone"], new_vendor_fields["contact_name"],
                             new_vendor_fields["contact_email"], requires_w9, upload_token, user["id"]),
                        )
                        new_vendor_request_id = cur.fetchone()["id"]
                else:
                    # Picking (or keeping) an existing vendor. Any prior
                    # vendor_request row this payment_request pointed to is
                    # simply unlinked, not deleted -- preserves its own
                    # approval/W-9 history in case it's independently
                    # relevant, rather than destroying data on an edit.
                    new_vendor_request_id = None

                # Budget Overspend Tracking Plan.md (2026-07-26): recomputed
                # and written on EVERY edit, not just a reset_approval edit --
                # GL lines are always replaced wholesale below regardless of
                # whether vendor/amount changed (e.g. a same-total
                # reallocation between GL lines can still change which
                # accounts are over budget).
                if reset_approval:
                    cur.execute(
                        """
                        UPDATE checkreq.payment_requests SET
                            program_area_id = %s, vendor_id = %s, vendor_request_id = %s,
                            amount = %s, requested_pay_date = %s, description = %s,
                            special_instructions = %s, status = %s, current_approver_id = %s,
                            serial_group_current = %s, approval_chain_summary = %s,
                            cfo_override = FALSE, cfo_override_date = NULL,
                            overspend_flagged = %s, overspend_detail = %s, budget_checked_at = NOW(),
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (program_area_id, vendor_id, new_vendor_request_id, total_amount,
                         requested_pay_date, description, special_instructions,
                         "UnderReview",
                         first_display_approver, first_serial_group,
                         chain_summary, overspend_flagged, overspend_detail, payment_request_id),
                    )
                    # AP Review Workflow Plan.md, Section 1a: mark any still-
                    # pending rows from the OLD chain skipped, then
                    # materialize the freshly-recomputed chain's rows.
                    _supersede_pending_approval_actions(cur, payment_request_id)
                    _materialize_approval_actions(cur, payment_request_id, chain)
                else:
                    cur.execute(
                        """
                        UPDATE checkreq.payment_requests SET
                            program_area_id = %s, vendor_id = %s, vendor_request_id = %s,
                            requested_pay_date = %s, description = %s,
                            special_instructions = %s,
                            overspend_flagged = %s, overspend_detail = %s, budget_checked_at = NOW(),
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (program_area_id, vendor_id, new_vendor_request_id,
                         requested_pay_date, description, special_instructions,
                         overspend_flagged, overspend_detail, payment_request_id),
                    )

                # Replace GL lines wholesale -- delete + reinsert, matching
                # this codebase's stated preference for straightforward code
                # over cleverness (a diff-based reconciliation would need to
                # match old-to-new lines by some key that doesn't exist).
                cur.execute(
                    "DELETE FROM checkreq.payment_request_gl_lines WHERE payment_request_id = %s",
                    (payment_request_id,),
                )
                for acct_id, amt, memo in gl_lines:
                    cur.execute(
                        "INSERT INTO checkreq.payment_request_gl_lines "
                        "(payment_request_id, gl_account_id, amount, memo) VALUES (%s, %s, %s, %s)",
                        (payment_request_id, acct_id, amt, memo),
                    )

                # Approval Workflow Corrections (2026-07-31): same
                # budget_overage_log bookkeeping as a new submission -- an
                # edit can just as easily re-trigger tier-2/tier-3 on
                # different GL lines.
                for tier, entries in (("buffer_notice", budget_result["buffer_notice"]),
                                       ("cfo_required", budget_result["cfo_required"])):
                    for e in entries:
                        cur.execute(
                            "INSERT INTO checkreq.budget_overage_log "
                            "(payment_request_id, gl_account_id, tier, annual_budget, projected_spend, buffer_amount) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            (payment_request_id, e["gl_account_id"], tier,
                             e["annual_budget"], e["projected"], e["buffer_amount"]),
                        )

                if reset_approval:
                    if vendor_changed or amount_changed:
                        audit_comment = (
                            f"Vendor and/or amount changed on edit (was ${old_total:,.2f}, "
                            f"now ${total_amount:,.2f}) -- approval workflow reset.\n{chain_summary}"
                        )
                    else:
                        # status_forces_reset case (Decision 1) -- neither
                        # vendor nor amount changed, but the request was
                        # 'Rejected'/'Returned by AP' and must always
                        # re-enter the chain regardless of what changed.
                        audit_comment = (
                            f"Request was '{existing_pr['status']}' -- edited and resubmitted "
                            f"for approval (re-enters the chain regardless of what changed).\n{chain_summary}"
                        )
                    cur.execute(
                        "INSERT INTO checkreq.audit_log "
                        "(payment_request_id, action_by_user_id, action_type, comment, "
                        " previous_status, new_status, impersonated_by_user_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (payment_request_id, user["id"], "Edited — Approval Reset", audit_comment,
                         existing_pr["status"], "UnderReview", impersonated_by),
                    )
                else:
                    cur.execute(
                        "INSERT INTO checkreq.audit_log "
                        "(payment_request_id, action_by_user_id, action_type, comment, "
                        " previous_status, new_status, impersonated_by_user_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (payment_request_id, user["id"], "Edited",
                         "Fields updated (vendor and amount unchanged; approval workflow untouched).",
                         existing_pr["status"], existing_pr["status"], impersonated_by),
                    )

        # Deliberately NOT touching payment_request_attachments or re-running
        # the GCS/SharePoint archival pipeline here -- the archived copy is a
        # point-in-time snapshot from original submission (out of scope for
        # an edit, per this task's own spec). GET /requests/{request_number}/pdf
        # always regenerates fresh from the live DB on every hit, so it
        # already reflects this edit with zero extra work.
        if budget_result["buffer_notice"]:
            _send_budget_buffer_notice_email(
                request_number, org["name"], [e["detail"] for e in budget_result["buffer_notice"]],
            )
        if reset_approval and first_serial_group is not None:
            notify_group = first_serial_group
            if not is_self_payment:
                advanced = _maybe_auto_approve_self(
                    payment_request_id, first_serial_group, user["id"], _client_ip(request), request,
                )
                if advanced is not None:
                    notify_group = advanced
            _notify_approvers_for_group(payment_request_id, notify_group, request)
        return RedirectResponse(f"/my-requests?edited={request_number}", status_code=303)

    # ── NEW SUBMISSION branch (unchanged from before this session) ────────
    request_number = _next_request_number(request_type)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO checkreq.payment_requests
                    (request_number, request_type, org_id, program_area_id, submitter_user_id,
                     vendor_id, amount, requested_pay_date, description, special_instructions,
                     status, current_approver_id, serial_group_current, approval_chain_summary,
                     overspend_flagged, overspend_detail, budget_checked_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                RETURNING id
                """,
                (request_number, request_type, org_id, program_area_id, user["id"],
                 vendor_id, total_amount, requested_pay_date, description, special_instructions,
                 "UnderReview",
                 first_display_approver, first_serial_group,
                 chain_summary, overspend_flagged, overspend_detail),
            )
            payment_request_id = cur.fetchone()["id"]

            for acct_id, amt, memo in gl_lines:
                cur.execute(
                    "INSERT INTO checkreq.payment_request_gl_lines "
                    "(payment_request_id, gl_account_id, amount, memo) VALUES (%s, %s, %s, %s)",
                    (payment_request_id, acct_id, amt, memo),
                )

            # Approval Workflow Corrections (Jay, 2026-07-31): one
            # budget_overage_log row per tier-2/tier-3 GL line, for CFO
            # reporting (how often, how much, which accounts) -- not just a
            # one-off comment on the request. Written in the same
            # transaction as everything else, since it's plain bookkeeping,
            # not an external side effect (unlike the tier-2 email below).
            for tier, entries in (("buffer_notice", budget_result["buffer_notice"]),
                                   ("cfo_required", budget_result["cfo_required"])):
                for e in entries:
                    cur.execute(
                        "INSERT INTO checkreq.budget_overage_log "
                        "(payment_request_id, gl_account_id, tier, annual_budget, projected_spend, buffer_amount) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (payment_request_id, e["gl_account_id"], tier,
                         e["annual_budget"], e["projected"], e["buffer_amount"]),
                    )

            # AP Review Workflow Plan.md, Section 1a: materialize the whole
            # computed chain as its own set of 'pending' approval_actions
            # rows -- this, not current_approver_id, is the real gate
            # /requests/{request_number}/approve checks.
            _materialize_approval_actions(cur, payment_request_id, chain)

            # action_type "Submitted" describes the ACTION the user just took
            # (submitting) and stays "Submitted" even though the request's
            # STATUS the row transitions into is now "UnderReview" -- these
            # are two different things (Task 3, 2026-07-26): action_type is a
            # verb describing what happened, new_status is the state that
            # resulted. Conflating them would make the audit trail read as if
            # the request were literally in a status called "Submitted".
            cur.execute(
                "INSERT INTO checkreq.audit_log "
                "(payment_request_id, action_by_user_id, action_type, comment, new_status, impersonated_by_user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (payment_request_id, user["id"], "Submitted", chain_summary, "UnderReview", impersonated_by),
            )

            # New Vendor Onboarding (Section 1/2): the vendor_requests row
            # references payment_request_id (NOT NULL), so it can only be
            # created after the payment_request row above already has an
            # id -- then payment_requests.vendor_request_id is set to point
            # back, in the same transaction (both inserts commit together
            # or not at all).
            if new_vendor_fields:
                requires_w9 = total_amount > VENDOR_W9_AMOUNT_THRESHOLD
                upload_token = pysecrets.token_urlsafe(32)
                cur.execute(
                    """
                    INSERT INTO checkreq.vendor_requests
                        (org_id, payment_request_id, entity_type, first_name, last_name,
                         company_name, dba_name, address_line1, address_line2, city, state, zip,
                         phone, contact_name, contact_email, requires_w9, upload_token,
                         created_by_user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (org_id, payment_request_id, new_vendor_fields["entity_type"],
                     new_vendor_fields["first_name"], new_vendor_fields["last_name"],
                     new_vendor_fields["company_name"], new_vendor_fields["dba_name"],
                     new_vendor_fields["address_line1"], new_vendor_fields["address_line2"],
                     new_vendor_fields["city"], new_vendor_fields["state"], new_vendor_fields["zip"],
                     new_vendor_fields["phone"], new_vendor_fields["contact_name"],
                     new_vendor_fields["contact_email"], requires_w9, upload_token, user["id"]),
                )
                vendor_request_id = cur.fetchone()["id"]
                cur.execute(
                    "UPDATE checkreq.payment_requests SET vendor_request_id = %s WHERE id = %s",
                    (vendor_request_id, payment_request_id),
                )

    # Tier-2 budget overage: FYI-only CFO notification, after commit (a real
    # external email send, matching every other notification's post-commit
    # placement in this route).
    if budget_result["buffer_notice"]:
        _send_budget_buffer_notice_email(
            request_number, org["name"], [e["detail"] for e in budget_result["buffer_notice"]],
        )

    # Approval Workflow Corrections (Jay, 2026-07-31): if the submitter is
    # themselves an authorized approver for the first step, that step is
    # auto-satisfied here -- runs AFTER the transaction above has committed
    # (same same-cursor-visibility rule as the notify call below). Never
    # attempted for a self-payment chain -- _self_payment_cfo_chain() already
    # excludes the submitter from its own approver list, so there would be
    # nothing for them to auto-approve anyway.
    notify_group = first_serial_group
    if not is_self_payment:
        advanced = _maybe_auto_approve_self(
            payment_request_id, first_serial_group, user["id"], _client_ip(request), request,
        )
        if advanced is not None:
            notify_group = advanced

    # Fire the "it's your turn" email to whoever is now first in the chain --
    # after the transaction above has committed (the query inside reads
    # approval_actions on its own separate connection). No-ops cleanly if
    # notify_group is None (no approval rule configured for this program
    # area, or a self-payment chain with no CFO configured).
    _notify_approvers_for_group(payment_request_id, notify_group, request)

    # Archive the generated check-voucher PDF (always) + any optional
    # user-uploaded supporting attachments -- to GCS staging then the
    # entity's permanent SharePoint archive. Deliberately runs AFTER the
    # submission above has already committed: the PDF must display the
    # real, final request_number, and archival is a secondary, recoverable
    # step -- a transient storage hiccup here must never roll back or fail
    # an otherwise-successful check-request submission.
    archive_warning = None
    try:
        _archive_attachments(org, payment_request_id, request_number, requested_pay_date,
                              total_amount, user["id"], uploaded_attachments)
    except Exception as exc:
        archive_warning = str(exc)

    redirect_url = f"/my-requests?submitted={request_number}"
    if archive_warning:
        from urllib.parse import quote
        redirect_url += f"&archive_warning={quote(archive_warning)}"
    return RedirectResponse(redirect_url, status_code=303)


@app.post("/requests/{request_number}/cancel")
def cancel_request(request_number: str, request: Request):
    """Task 2 (2026-07-26): a soft cancel, matching this whole app's "never
    truly delete, keep the audit trail" philosophy (same reasoning already
    applied to attachments -- gcs_deleted_at is stamped, never NULLed back
    out -- and to why editing never touches archived files). Sets
    status='Cancelled' (a new value in the same free-text status column --
    no migration needed, payment_requests.status has no CHECK constraint),
    writes one audit_log row, and otherwise leaves GL lines/attachments/
    vendor_request completely untouched.

    Same ownership + locking convention as edit_request_form/
    new_request_submit's edit branch: submitter-only, and only while
    _request_is_editable() is still true (now also excludes 'Cancelled'
    itself, so a request can't be cancelled twice)."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    pr = db.query_one(
        "SELECT * FROM checkreq.payment_requests WHERE request_number = %s",
        (request_number,),
    )
    if not pr:
        return JSONResponse({"error": "Request not found"}, status_code=404)
    if pr["submitter_user_id"] != user["id"]:
        return JSONResponse({"error": "Not authorized to cancel this request"}, status_code=403)
    if not _request_is_editable(pr["status"]):
        return JSONResponse(
            {"error": "This request can no longer be cancelled (already posted to QBO or already cancelled)."},
            status_code=403,
        )

    imp_id = request.session.get("impersonating_user_id")
    impersonated_by = _real_user(request)["id"] if imp_id else None

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.payment_requests SET status = 'Cancelled', updated_at = NOW() WHERE id = %s",
                (pr["id"],),
            )
            cur.execute(
                "INSERT INTO checkreq.audit_log "
                "(payment_request_id, action_by_user_id, action_type, comment, "
                " previous_status, new_status, impersonated_by_user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (pr["id"], user["id"], "Cancelled", "Cancelled by submitter.",
                 pr["status"], "Cancelled", impersonated_by),
            )

    return RedirectResponse(f"/my-requests?cancelled={request_number}", status_code=303)


@app.get("/requests/{request_number}/attachments/{attachment_id}/view")
def view_attachment(request_number: str, attachment_id: int, request: Request):
    """Streams the real archived file from SharePoint (the permanent record
    -- GCS is transient staging only, see gcs_client.py's own docstring)
    through THIS app's own service-credential Graph access
    (sharepoint_client.get_access_token(), the same client-credentials grant
    _archive_attachments already uses) rather than redirecting to the stored
    sp_web_url.

    This was verified, not assumed: sp_web_url points at
    episcopalmaryland.sharepoint.com -- a DIFFERENT Azure AD tenant than the
    one this app's own users sign into (cfmins.org). A user with no
    account/guest access on that tenant would hit a Microsoft login wall they
    can't get past -- the exact cross-tenant gap already documented
    elsewhere in this project (Chris McCloud's identity mismatch, EDOM
    federation deferred). Proxying through our own already-working Graph
    credential sidesteps that entirely and keeps the authorization check on
    THIS app's own session, matching every other authenticated route here.

    Authorization mirrors request_pdf exactly: owner, CFO, or an approver
    for this request's program area -- deliberately not restricted to
    "editable" requests only, since viewing a document on a posted/cancelled
    request should still work (same reasoning request_pdf already applies)."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    pr = db.query_one(
        "SELECT id, submitter_user_id, program_area_id, org_id FROM checkreq.payment_requests "
        "WHERE request_number = %s",
        (request_number,),
    )
    if not pr:
        return JSONResponse({"error": "Request not found"}, status_code=404)

    allowed = (
        user["is_cfo"]
        or pr["submitter_user_id"] == user["id"]
        or _user_can_submit_for(user, pr["program_area_id"])
    )
    if not allowed:
        return JSONResponse({"error": "Not authorized to view this attachment"}, status_code=403)

    att = db.query_one(
        "SELECT * FROM checkreq.payment_request_attachments "
        "WHERE id = %s AND payment_request_id = %s AND removed_at IS NULL",
        (attachment_id, pr["id"]),
    )
    if not att:
        return JSONResponse({"error": "Attachment not found"}, status_code=404)

    org = db.query_one(
        "SELECT sp_hostname, sp_site_path FROM checkreq.organizations WHERE id = %s",
        (pr["org_id"],),
    )
    if not org or not (org.get("sp_hostname") and org.get("sp_site_path")):
        return JSONResponse({"error": "Entity SharePoint configuration missing"}, status_code=500)

    token = sharepoint_client.get_access_token()
    site_id = sharepoint_client.get_site_id(token, org["sp_hostname"], org["sp_site_path"])
    content = sharepoint_client.download_bytes(token, site_id, att["sp_file_path"])

    return Response(
        content=content,
        media_type=att["content_type"] or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{att["archived_filename"]}"'},
    )


@app.post("/requests/{request_number}/attachments/{attachment_id}/remove")
def remove_attachment(request_number: str, attachment_id: int, request: Request):
    """The Edit page's "X" -- soft-delete only, matching this whole app's
    established "never truly delete" philosophy (same reasoning already
    applied to gcs_deleted_at, which is stamped and never NULLed back out,
    and to Cancel, a soft status rather than a row delete). Stamps
    removed_at; never touches the real SharePoint file or the GCS blob, and
    never deletes the payment_request_attachments row itself -- the file
    stays in the permanent SharePoint archive, it's just excluded from
    _active_attachments() (and therefore from the Edit-page list and from
    view_attachment's own lookup) going forward.

    Same ownership + locking convention as edit_request_form/cancel_request:
    submitter-only, and only while _request_is_editable() is still true."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    pr = db.query_one(
        "SELECT * FROM checkreq.payment_requests WHERE request_number = %s",
        (request_number,),
    )
    if not pr:
        return JSONResponse({"error": "Request not found"}, status_code=404)
    if pr["submitter_user_id"] != user["id"]:
        return JSONResponse({"error": "Not authorized to modify this request"}, status_code=403)
    if not _request_is_editable(pr["status"]):
        return JSONResponse(
            {"error": "This request can no longer be modified (posted to QBO or cancelled)."},
            status_code=403,
        )

    att = db.query_one(
        "SELECT * FROM checkreq.payment_request_attachments "
        "WHERE id = %s AND payment_request_id = %s AND removed_at IS NULL",
        (attachment_id, pr["id"]),
    )
    if not att:
        return JSONResponse({"error": "Attachment not found (or already removed)"}, status_code=404)

    imp_id = request.session.get("impersonating_user_id")
    impersonated_by = _real_user(request)["id"] if imp_id else None
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.payment_request_attachments SET removed_at = NOW() WHERE id = %s",
                (attachment_id,),
            )
            cur.execute(
                "INSERT INTO checkreq.audit_log "
                "(payment_request_id, action_by_user_id, action_type, comment, impersonated_by_user_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (pr["id"], user["id"], "Attachment Removed",
                 f"Removed from view: {att['original_filename']} "
                 f"(file remains archived in SharePoint, not deleted).",
                 impersonated_by),
            )

    return RedirectResponse(f"/requests/{request_number}/edit", status_code=303)


@app.post("/requests/{request_number}/attachments/add")
async def add_attachment(request_number: str, request: Request):
    """The Edit page's "Add" button -- per Jay's request (2026-07-26):
    "Additional files can also be uploaded during edit." A standalone
    action (its own small <form>, not folded into new_request_submit's big
    edit POST) because that edit-submit path deliberately does NOT touch
    payment_request_attachments or re-run archival at all (see its own
    comment, right where the edit branch returns) -- keeping this separate
    avoids entangling attachment uploads with the vendor/amount
    approval-reset logic, and lets an addition succeed or fail on its own
    without interacting with the rest of the edit form.

    Reuses the exact archived-filename convention and GCS+SharePoint upload
    pipeline _archive_attachments established, but one file at a time after
    the fact rather than as part of the initial "generated PDF + optional
    uploads" batch -- written as its own function rather than stretching
    that batch function to also support a post-hoc single add, since the
    two have different transaction/rollback shapes (the original batch
    inserts DB rows only after every file uploads cleanly; a one-off add
    here commits each file's own row immediately so one failing file in a
    multi-file Add doesn't lose the ones that already succeeded)."""
    from urllib.parse import quote

    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    pr = db.query_one(
        "SELECT * FROM checkreq.payment_requests WHERE request_number = %s",
        (request_number,),
    )
    if not pr:
        return JSONResponse({"error": "Request not found"}, status_code=404)
    if pr["submitter_user_id"] != user["id"]:
        return JSONResponse({"error": "Not authorized to modify this request"}, status_code=403)
    if not _request_is_editable(pr["status"]):
        return JSONResponse(
            {"error": "This request can no longer be modified (posted to QBO or cancelled)."},
            status_code=403,
        )

    org = db.query_one(
        "SELECT id, code, name, sp_hostname, sp_site_path, sp_library_folder "
        "FROM checkreq.organizations WHERE id = %s",
        (pr["org_id"],),
    )
    if not org or not (org.get("sp_hostname") and org.get("sp_site_path") and org.get("sp_library_folder")):
        return RedirectResponse(
            f"/requests/{request_number}/edit?add_error="
            f"{quote('No SharePoint archive location configured for this entity.')}",
            status_code=303,
        )

    form = await request.form()
    files = [f for f in form.getlist("attachments") if getattr(f, "filename", None)]
    if not files:
        return RedirectResponse(
            f"/requests/{request_number}/edit?add_error={quote('No file selected.')}", status_code=303,
        )

    vendor_name = _vendor_display_name_for_request(pr["id"])
    pay_date_str = pr["requested_pay_date"].isoformat() if pr["requested_pay_date"] else None
    total_amount = float(pr["amount"])

    # Next archived-filename index must be unique across every attachment
    # EVER created for this request, not just the currently-active ones --
    # reusing an index a removed row already used would silently overwrite
    # that row's real file in SharePoint the moment it's re-uploaded
    # (upload_bytes() overwrites unconditionally on a filename collision,
    # by design -- see its own docstring). COUNT(*) with no removed_at
    # filter is the simplest index that can never collide with a prior one.
    next_index = db.query_one(
        "SELECT COUNT(*) AS c FROM checkreq.payment_request_attachments WHERE payment_request_id = %s",
        (pr["id"],),
    )["c"]

    token = sharepoint_client.get_access_token()
    site_id = sharepoint_client.get_site_id(token, org["sp_hostname"], org["sp_site_path"])

    imp_id = request.session.get("impersonating_user_id")
    impersonated_by = _real_user(request)["id"] if imp_id else None

    errors = []
    for f in files:
        content = await f.read()
        if not content:
            continue
        try:
            next_index += 1
            content_type = f.content_type or "application/octet-stream"
            ext = _guess_extension(f.filename, content_type)
            archived_filename = _compute_archived_filename(
                org["code"], pay_date_str, vendor_name, total_amount, request_number, next_index, ext,
            )
            blob_path = f"{request_number}/{archived_filename}"
            gcs_client.upload_bytes(ATTACHMENTS_BUCKET, blob_path, content, content_type)
            try:
                sp_result = sharepoint_client.upload_bytes(
                    token, site_id, org["sp_library_folder"], archived_filename, content, content_type,
                )
            except Exception:
                gcs_client.delete_blob(ATTACHMENTS_BUCKET, blob_path)
                raise

            with db.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO checkreq.payment_request_attachments
                            (payment_request_id, source, original_filename, archived_filename,
                             content_type, size_bytes, gcs_bucket, gcs_blob_path, sp_file_path,
                             sp_web_url, uploaded_by_user_id)
                        VALUES (%s, 'user_upload', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (pr["id"], f.filename, archived_filename, content_type, len(content),
                         ATTACHMENTS_BUCKET, blob_path,
                         f"{org['sp_library_folder'].strip('/')}/{archived_filename}",
                         sp_result.get("webUrl"), user["id"]),
                    )
                    cur.execute(
                        "INSERT INTO checkreq.audit_log "
                        "(payment_request_id, action_by_user_id, action_type, comment, impersonated_by_user_id) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (pr["id"], user["id"], "Attachment Added", f"Added: {f.filename}", impersonated_by),
                    )
        except Exception as exc:
            errors.append(f"{f.filename}: {exc}")

    redirect_url = f"/requests/{request_number}/edit"
    if errors:
        redirect_url += f"?add_error={quote('; '.join(errors))}"
    return RedirectResponse(redirect_url, status_code=303)


@app.get("/my-requests", response_class=HTMLResponse)
def my_requests(request: Request, submitted: str = "", archive_warning: str = "", edited: str = "",
                cancelled: str = "", view: str = "mine"):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    # Task 6 (2026-07-26): CFO-only "My Requests" vs "All Requests" toggle.
    # "All Requests" deliberately stays scoped to the currently selected
    # entity (org_id = current_org_id), NOT a global cross-entity view --
    # Jay's explicit call. A non-CFO passing ?view=all gets silently treated
    # as "mine" rather than an error -- same "no silent broadening of access"
    # posture as _user_can_submit_for, just expressed as a quiet fallback
    # since this is a read-only list view, not a submission.
    show_all = (view == "all") and bool(user["is_cfo"])

    # Task 3 (2026-07-26 batch): Vendor column -- resolve from either an
    # onboarded checkreq.vendors row OR a not-yet-onboarded vendor_requests
    # row, same "exactly one of the two is ever set" design new_request_submit
    # already enforces. Both branches below select the same vr_* fields and
    # resolve the display name the same way in the loop below, reusing
    # _vendor_request_row_display_name() (the same helper _voucher_context()
    # already uses for this exact resolution) rather than duplicating it.
    #
    # Task 4 (2026-07-26 batch, real bug): the "mine" branch had NO org_id
    # filter at all -- a user's requests across BOTH EDOM and Claggett showed
    # regardless of the session's currently-selected entity, while "All
    # Requests" was already correctly scoped. Fixed by adding the identical
    # `pr.org_id = %s` scoping "All Requests" already had, keyed off the same
    # _current_org(request) used everywhere else in this app.
    org = _current_org(request)
    if not org:
        return RedirectResponse("/portal")

    if show_all:
        rows = db.query(
            """
            SELECT pr.id AS pr_id, pr.request_number, pr.request_type, pr.amount, pr.status,
                   pr.approval_chain_summary, pr.created_at, pr.requested_pay_date, o.code AS org_code,
                   pa.title AS program_area_title, u.display_name AS submitter_name,
                   u.email AS submitter_email,
                   v.display_name AS vendor_display_name,
                   vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
                   vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
                   vr.dba_name AS vr_dba_name
            FROM checkreq.payment_requests pr
            JOIN checkreq.organizations o ON o.id = pr.org_id
            JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
            JOIN checkreq.app_users u ON u.id = pr.submitter_user_id
            LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
            LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
            WHERE pr.org_id = %s
            ORDER BY pr.requested_pay_date
            """,
            (org["id"],),
        )
    else:
        rows = db.query(
            """
            SELECT pr.id AS pr_id, pr.request_number, pr.request_type, pr.amount, pr.status,
                   pr.approval_chain_summary, pr.created_at, pr.requested_pay_date, o.code AS org_code,
                   pa.title AS program_area_title,
                   v.display_name AS vendor_display_name,
                   vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
                   vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
                   vr.dba_name AS vr_dba_name
            FROM checkreq.payment_requests pr
            JOIN checkreq.organizations o ON o.id = pr.org_id
            JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
            LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
            LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
            WHERE pr.submitter_user_id = %s AND pr.org_id = %s
            ORDER BY pr.requested_pay_date
            """,
            (user["id"], org["id"]),
        )

    # Task 4 (2026-07-26): status-pill popup history. Embedded server-side at
    # render time (one extra query total, keyed by payment_request_id) rather
    # than a separate per-click API endpoint -- fewer moving parts, and this
    # codebase already has a precedent for embedding data server-side into
    # the page rather than adding a new round-trip API for something this
    # small (new_request_form's EDIT_DATA JS global for edit prefill).
    history_by_id: dict[int, list[dict]] = {}
    pr_ids = [r["pr_id"] for r in rows]
    if pr_ids:
        history_rows = db.query(
            "SELECT payment_request_id, action_type, action_date, comment, "
            "previous_status, new_status FROM checkreq.audit_log "
            "WHERE payment_request_id = ANY(%s) ORDER BY action_date",
            (pr_ids,),
        )
        for h in history_rows:
            history_by_id.setdefault(h["payment_request_id"], []).append(h)

    for r in rows:
        # Locking rule (Jay, 2026-07-25, extended 2026-07-26 for Cancel):
        # "You can make changes until it is posted to QBO." Drives whether
        # the Edit/Cancel links render below.
        r["editable"] = _request_is_editable(r["status"])
        r["type_abbr"] = _REQUEST_TYPE_ABBR.get(r["request_type"], "??")
        r["history"] = history_by_id.get(r["pr_id"], [])
        # Task 3 (2026-07-26 batch): Vendor column -- either an onboarded
        # vendor's own display_name, or (not yet onboarded) a computed name
        # from the linked vendor_requests row, via the same helper
        # _voucher_context() already uses for this exact resolution.
        if r.get("vendor_display_name"):
            r["vendor_name"] = r["vendor_display_name"]
        elif r.get("vr_entity_type"):
            r["vendor_name"] = _vendor_request_row_display_name(
                r["vr_entity_type"], r["vr_company_name"], r["vr_dba_name"],
                r["vr_first_name"], r["vr_last_name"],
            )
        else:
            r["vendor_name"] = "—"

    return _render(request, "my_requests.html", user, {
        "rows": rows, "submitted": submitted, "archive_warning": archive_warning,
        "edited": edited, "cancelled": cancelled, "show_all": show_all,
    })


# ── Approval Action Workflow (AP Review Workflow Plan.md, Section 2) ────────
# GET /my-approvals: an approver's queue -- fundamentally a different query
# shape than My Requests (submitter_user_id = me): here it's "requests where
# I am owed an action right now," across every submitter. Visible to every
# logged-in user (MODULES' approval_queue tile, gate=None) -- an empty queue
# is a harmless empty state, not worth its own permission gate.

@app.get("/my-approvals", response_class=HTMLResponse)
def my_approvals(request: Request, view: str = "mine", approved: str = "",
                  rejected: str = "", email_warning: str = ""):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    org = _current_org(request)
    if not org:
        return RedirectResponse("/portal")

    # Jay, 2026-07-29: "how can I see items already taken an action on?"
    # Once approved/rejected, a request drops off the pending queue entirely
    # -- this branch shows the requester's OWN past actions instead, sourced
    # straight from approval_actions (the real per-approver record), not the
    # pending-queue query below.
    if view == "history":
        history_rows = db.query(
            """
            SELECT pr.request_number, pr.request_type, pr.amount, o.code AS org_code,
                   pa.title AS program_area_title,
                   v.display_name AS vendor_display_name,
                   vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
                   vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
                   vr.dba_name AS vr_dba_name,
                   aa.status AS action_status, aa.acted_at, aa.comment
            FROM checkreq.approval_actions aa
            JOIN checkreq.payment_requests pr ON pr.id = aa.payment_request_id
            JOIN checkreq.organizations o ON o.id = pr.org_id
            JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
            LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
            LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
            WHERE aa.approver_user_id = %s AND aa.status IN ('approved', 'rejected')
              AND pr.org_id = %s
            ORDER BY aa.acted_at DESC
            """,
            (user["id"], org["id"]),
        )
        for r in history_rows:
            r["type_abbr"] = _REQUEST_TYPE_ABBR.get(r["request_type"], "??")
            if r.get("vendor_display_name"):
                r["vendor_name"] = r["vendor_display_name"]
            elif r.get("vr_entity_type"):
                r["vendor_name"] = _vendor_request_row_display_name(
                    r["vr_entity_type"], r["vr_company_name"], r["vr_dba_name"],
                    r["vr_first_name"], r["vr_last_name"],
                )
            else:
                r["vendor_name"] = "—"
        return _render(request, "my_approvals_history.html", user, {"rows": history_rows})

    # Same ?view=all CFO toggle convention Task 6 (2026-07-26) already built
    # for My Requests -- "My Approvals" (default, my own pending items) vs.
    # "All Pending Approvals" (every request currently awaiting anyone's
    # action, scoped to the session's selected entity, same precedent).
    show_all = (view == "all") and bool(user["is_cfo"])

    if show_all:
        rows = db.query(
            """
            SELECT DISTINCT pr.id AS pr_id, pr.request_number, pr.request_type, pr.amount,
                   pr.status, pr.approval_chain_summary, pr.created_at, pr.requested_pay_date,
                   o.code AS org_code,
                   pa.title AS program_area_title, u.display_name AS submitter_name,
                   u.email AS submitter_email,
                   v.display_name AS vendor_display_name,
                   vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
                   vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
                   vr.dba_name AS vr_dba_name
            FROM checkreq.payment_requests pr
            JOIN checkreq.approval_actions aa
              ON aa.payment_request_id = pr.id AND aa.serial_group = pr.serial_group_current
             AND aa.status = 'pending'
            JOIN checkreq.organizations o ON o.id = pr.org_id
            JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
            JOIN checkreq.app_users u ON u.id = pr.submitter_user_id
            LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
            LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
            WHERE pr.status = 'UnderReview' AND pr.org_id = %s
            ORDER BY pr.created_at
            """,
            (org["id"],),
        )
    else:
        rows = db.query(
            """
            SELECT pr.id AS pr_id, pr.request_number, pr.request_type, pr.amount, pr.status,
                   pr.approval_chain_summary, pr.created_at, pr.requested_pay_date,
                   o.code AS org_code,
                   pa.title AS program_area_title,
                   v.display_name AS vendor_display_name,
                   vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
                   vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
                   vr.dba_name AS vr_dba_name
            FROM checkreq.payment_requests pr
            JOIN checkreq.approval_actions aa
              ON aa.payment_request_id = pr.id AND aa.serial_group = pr.serial_group_current
             AND aa.approver_user_id = %s AND aa.status = 'pending'
            JOIN checkreq.organizations o ON o.id = pr.org_id
            JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
            LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
            LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
            WHERE pr.status = 'UnderReview'
            ORDER BY pr.created_at
            """,
            (user["id"],),
        )

    # Full-chain visibility (Decision 3): reuse My Requests' exact
    # status-pill-popup mechanism (Task 4, 2026-07-26), sourced from
    # approval_actions joined to app_users instead of audit_log -- shows an
    # approver plainly who else is in the chain and who has already acted.
    chain_by_id: dict[int, list[dict]] = {}
    pr_ids = [r["pr_id"] for r in rows]
    if pr_ids:
        chain_rows = db.query(
            """
            SELECT aa.payment_request_id, aa.serial_group, aa.status, aa.acted_at, aa.comment,
                   u.display_name, u.email
            FROM checkreq.approval_actions aa
            JOIN checkreq.app_users u ON u.id = aa.approver_user_id
            WHERE aa.payment_request_id = ANY(%s)
            ORDER BY aa.serial_group, aa.id
            """,
            (pr_ids,),
        )
        for c in chain_rows:
            chain_by_id.setdefault(c["payment_request_id"], []).append(c)

    for r in rows:
        r["type_abbr"] = _REQUEST_TYPE_ABBR.get(r["request_type"], "??")
        r["chain"] = chain_by_id.get(r["pr_id"], [])
        if r.get("vendor_display_name"):
            r["vendor_name"] = r["vendor_display_name"]
        elif r.get("vr_entity_type"):
            r["vendor_name"] = _vendor_request_row_display_name(
                r["vr_entity_type"], r["vr_company_name"], r["vr_dba_name"],
                r["vr_first_name"], r["vr_last_name"],
            )
        else:
            r["vendor_name"] = "—"

    return _render(request, "my_approvals.html", user, {
        "rows": rows, "show_all": show_all, "approved": approved,
        "rejected": rejected, "email_warning": email_warning,
    })


@app.post("/requests/{request_number}/approve")
async def approve_request(request_number: str, request: Request):
    """AP Review Workflow Plan.md, Section 2b. The real gate is
    checkreq.approval_actions, not current_approver_id (display-only) --
    this correctly handles a parallel multi-approver serial_group, which a
    single scalar FK cannot represent.

    Jay, 2026-07-28 (My Approvals feedback): "There also needs to be a way
    to write notes during the approval process. These notes must become
    part of the documentation of approval." Optional approval_comment form
    field (unlike Reject's reason, this is optional -- an approval doesn't
    inherently need justification the way a rejection does) is stamped onto
    THIS approver's own approval_actions.comment row (already the field the
    View Chain popup reads) and folded into the audit_log entry, not just
    passed through and discarded."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    form = await request.form()
    note = (form.get("approval_comment") or "").strip() or None

    pr = db.query_one(
        "SELECT * FROM checkreq.payment_requests WHERE request_number = %s",
        (request_number,),
    )
    if not pr:
        return JSONResponse({"error": "Request not found"}, status_code=404)
    if pr["status"] != "UnderReview":
        return JSONResponse({"error": "This request is not currently awaiting approval."}, status_code=400)

    my_action = db.query_one(
        "SELECT * FROM checkreq.approval_actions WHERE payment_request_id = %s "
        "AND approver_user_id = %s AND serial_group = %s AND status = 'pending'",
        (pr["id"], user["id"], pr["serial_group_current"]),
    )
    if not my_action:
        return JSONResponse(
            {"error": "This isn't waiting on your approval right now (not your turn, "
                      "or you've already acted)."},
            status_code=403,
        )

    imp_id = request.session.get("impersonating_user_id")
    impersonated_by = _real_user(request)["id"] if imp_id else None

    result = _perform_approval(
        pr, my_action, user["id"], note, _client_ip(request), "web", impersonated_by,
    )
    if result["new_group_started"] is not None:
        _notify_approvers_for_group(pr["id"], result["new_group_started"], request)

    return RedirectResponse(f"/my-approvals?approved={request_number}", status_code=303)


@app.post("/requests/{request_number}/reject")
async def reject_request(request_number: str, request: Request):
    """AP Review Workflow Plan.md, Section 2c. Mid-chain reject -- from
    anywhere in the chain, unconditionally terminal for that chain:
    status='Rejected', current_approver_id/serial_group_current cleared,
    every still-pending approval_actions row (including later groups)
    flipped to 'skipped' so it stops appearing in anyone else's queue.
    Decision 1: uses the plain 'Rejected' status (distinct from the
    AP-stage 'Returned by AP' -- see ap_return_request below). Decision 2:
    emails the submitter."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    form = await request.form()
    reason = (form.get("rejected_reason") or "").strip()
    if not reason:
        return JSONResponse({"error": "A rejection reason is required."}, status_code=400)

    pr = db.query_one(
        "SELECT pr.*, u.email AS submitter_email, u.display_name AS submitter_name, "
        "o.name AS org_name "
        "FROM checkreq.payment_requests pr "
        "JOIN checkreq.app_users u ON u.id = pr.submitter_user_id "
        "JOIN checkreq.organizations o ON o.id = pr.org_id "
        "WHERE pr.request_number = %s",
        (request_number,),
    )
    if not pr:
        return JSONResponse({"error": "Request not found"}, status_code=404)
    if pr["status"] != "UnderReview":
        return JSONResponse({"error": "This request is not currently awaiting approval."}, status_code=400)

    my_action = db.query_one(
        "SELECT * FROM checkreq.approval_actions WHERE payment_request_id = %s "
        "AND approver_user_id = %s AND serial_group = %s AND status = 'pending'",
        (pr["id"], user["id"], pr["serial_group_current"]),
    )
    if not my_action:
        return JSONResponse(
            {"error": "This isn't waiting on your approval right now (not your turn, "
                      "or you've already acted)."},
            status_code=403,
        )

    imp_id = request.session.get("impersonating_user_id")
    impersonated_by = _real_user(request)["id"] if imp_id else None

    _perform_rejection(pr, my_action, user["id"], reason, _client_ip(request), "web", impersonated_by)

    email_result = _send_rejection_email(pr, "Rejected", reason, request, user.get("display_name") or user.get("email"))
    redirect_url = f"/my-approvals?rejected={request_number}"
    if email_result.get("status") != "sent":
        from urllib.parse import quote
        redirect_url += f"&email_warning={quote(email_result.get('error') or 'unknown error sending rejection email')}"
    return RedirectResponse(redirect_url, status_code=303)


# ── Approval-by-email landing page (Jay, 2026-07-30) ─────────────────────────
# Public, unauthenticated, token-gated -- same pattern as the existing
# /vendor-w9-upload/{token} route. GET shows a confirmation page (never
# mutates state on its own, so email-client link-prescanning can't trigger a
# real action); POST is the only thing that actually approves/rejects.

def _approval_email_token_lookup(token: str) -> dict | None:
    """Computes is_used/is_expired in SQL (NOW() at the DB, not this
    process's clock) rather than comparing datetimes in Python -- simpler
    and avoids any tzinfo mismatch between what psycopg hands back and
    what this process's own clock thinks 'now' is."""
    return db.query_one(
        "SELECT *, (used_at IS NOT NULL) AS is_used, (NOW() > expires_at) AS is_expired "
        "FROM checkreq.approval_email_tokens WHERE token = %s",
        (token,),
    )


def _approval_email_token_state(tok: dict | None, ctx: dict | None, my_action: dict | None) -> str:
    """One shared 'what should this page show' resolver for both GET and
    POST -- 'active' is the only state where an action is actually still
    possible."""
    if not tok:
        return "not_found"
    if tok["is_used"]:
        return "used"
    if tok["is_expired"]:
        return "expired"
    if not ctx or ctx["status"] != "UnderReview" or not my_action:
        # Covers every other way this step could have stopped being live:
        # already acted on via the web UI, the request was edited (which
        # resets and re-chains), cancelled, or rejected by someone else in
        # a parallel serial_group.
        return "stale"
    return "active"


@app.get("/email-action/{token}", response_class=HTMLResponse)
def email_action_form(token: str, request: Request, action: str = "approve"):
    tok = _approval_email_token_lookup(token)
    ctx = _approval_email_context(tok["payment_request_id"]) if tok else None
    my_action = None
    if tok and ctx:
        my_action = db.query_one(
            "SELECT * FROM checkreq.approval_actions WHERE payment_request_id = %s "
            "AND approver_user_id = %s AND serial_group = %s AND status = 'pending'",
            (tok["payment_request_id"], tok["approver_user_id"], tok["serial_group"]),
        )
    state = _approval_email_token_state(tok, ctx, my_action)
    return templates.TemplateResponse(request, "email_action.html", {
        "state": state, "ctx": ctx, "token": token,
        "action": action if action in ("approve", "reject") else "approve", "error": "",
    })


@app.post("/email-action/{token}", response_class=HTMLResponse)
async def email_action_submit(token: str, request: Request):
    form = await request.form()
    action = form.get("action", "approve")
    note_or_reason = (form.get("note_or_reason") or "").strip()

    tok = _approval_email_token_lookup(token)
    ctx = _approval_email_context(tok["payment_request_id"]) if tok else None
    my_action = None
    if tok and ctx:
        my_action = db.query_one(
            "SELECT * FROM checkreq.approval_actions WHERE payment_request_id = %s "
            "AND approver_user_id = %s AND serial_group = %s AND status = 'pending'",
            (tok["payment_request_id"], tok["approver_user_id"], tok["serial_group"]),
        )
    state = _approval_email_token_state(tok, ctx, my_action)

    if state == "active" and action == "reject" and not note_or_reason:
        return templates.TemplateResponse(request, "email_action.html", {
            "state": "active", "ctx": ctx, "token": token, "action": "reject",
            "error": "A rejection reason is required.",
        })

    if state != "active":
        return templates.TemplateResponse(request, "email_action.html", {
            "state": state, "ctx": ctx, "token": token, "action": action, "error": "",
        })

    pr = db.query_one(
        "SELECT pr.*, u.email AS submitter_email, u.display_name AS submitter_name, "
        "o.name AS org_name FROM checkreq.payment_requests pr "
        "JOIN checkreq.app_users u ON u.id = pr.submitter_user_id "
        "JOIN checkreq.organizations o ON o.id = pr.org_id WHERE pr.id = %s",
        (tok["payment_request_id"],),
    )
    ip = _client_ip(request)

    if action == "reject":
        _perform_rejection(pr, my_action, tok["approver_user_id"], note_or_reason, ip, "email")
        _send_rejection_email(pr, "Rejected", note_or_reason, request,
                               _user_display_name(tok["approver_user_id"]))
        result_state = "done_reject"
    else:
        result = _perform_approval(
            pr, my_action, tok["approver_user_id"], note_or_reason or None, ip, "email",
        )
        if result["new_group_started"] is not None:
            _notify_approvers_for_group(pr["id"], result["new_group_started"], request)
        result_state = "done_approve"

    # Marked used AFTER the action succeeds, not before -- if _perform_approval/
    # _perform_rejection ever raised, the token should stay valid for a retry
    # rather than being burned on a failed attempt.
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.approval_email_tokens SET used_at = NOW() WHERE id = %s",
                (tok["id"],),
            )

    return templates.TemplateResponse(request, "email_action.html", {
        "state": result_state, "ctx": ctx, "token": token, "action": action, "error": "",
    })


@app.post("/internal/send-daily-digest")
async def send_daily_digest(request: Request):
    """Cloud Scheduler -> this endpoint, once daily (Jay, 2026-07-30: "a
    daily email that summarizes all the different things they need to
    do"). Machine-to-machine, gated by a shared-secret header rather than
    session auth -- there is no signed-in user driving this call. One email
    per approver, covering every request currently waiting on them across
    every org/entity, not scoped to whichever entity a human happened to
    have selected in their session."""
    supplied = request.headers.get("x-internal-key", "")
    if not supplied or supplied != _get_internal_key():
        return JSONResponse({"error": "unauthorized"}, status_code=403)

    approvers = db.query(
        "SELECT DISTINCT u.id, u.email, u.display_name "
        "FROM checkreq.approval_actions aa "
        "JOIN checkreq.app_users u ON u.id = aa.approver_user_id "
        "JOIN checkreq.payment_requests pr ON pr.id = aa.payment_request_id "
        "WHERE aa.status = 'pending' AND aa.serial_group = pr.serial_group_current "
        "AND pr.status = 'UnderReview'"
    )

    sent = 0
    skipped_empty = 0
    errors = []
    for a in approvers:
        pending = db.query(
            "SELECT DISTINCT aa.payment_request_id AS pr_id, aa.serial_group "
            "FROM checkreq.approval_actions aa "
            "JOIN checkreq.payment_requests pr ON pr.id = aa.payment_request_id "
            "WHERE aa.approver_user_id = %s AND aa.status = 'pending' "
            "AND aa.serial_group = pr.serial_group_current AND pr.status = 'UnderReview'",
            (a["id"],),
        )
        rows = []
        for p in pending:
            ctx = _approval_email_context(p["pr_id"])
            if not ctx:
                continue
            ctx["pr_id"] = p["pr_id"]
            ctx["serial_group"] = p["serial_group"]
            rows.append(ctx)
        if not rows:
            skipped_empty += 1
            continue
        try:
            result = _send_daily_digest_email(a, rows, request)
        except Exception as exc:
            errors.append({"approver": a["email"], "error": str(exc)})
            continue
        if result.get("status") == "sent":
            sent += 1
        else:
            errors.append({"approver": a["email"], "error": result.get("error")})

    return JSONResponse({"approvers_notified": sent, "skipped_empty": skipped_empty, "errors": errors})


# ── Feedback (Task 10, UI/UX batch, 2026-07-26) ──────────────────────────────
# Jay: "We need a feedback section on the main row to gather people's
# feedback." Deliberately simple -- a comment box, no workflow/status, just a
# durable place for comments to land for later staff review. No admin-only
# gate on submission (any signed-in user); a light CFO-only listing page is
# included as the nice-to-have the task called out, not the core ask.

@app.get("/feedback", response_class=HTMLResponse)
def feedback_form(request: Request, submitted: bool = False):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    return _render(request, "feedback.html", user, {"submitted": submitted})


@app.post("/feedback")
async def feedback_submit(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    form = await request.form()
    comment = (form.get("comment") or "").strip()
    if not comment:
        return RedirectResponse("/feedback", status_code=303)

    org = _current_org(request)  # nullable -- feedback can be given before any entity is picked
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkreq.app_feedback (org_id, submitted_by_user_id, comment) "
                "VALUES (%s, %s, %s)",
                (org["id"] if org else None, user["id"], comment),
            )
    return RedirectResponse("/feedback?submitted=1", status_code=303)


# ── Administrative: system-wide request log (Jay, 2026-07-29) ───────────────
# "some sort of administratives pill on the main menu for people that have
# administrative access. They're gonna be able to review all the logs, like
# all the CRs and where they are, what has happened to them." Distinct from
# My Requests' own "All Requests" toggle -- that one stays scoped to the
# session's selected entity by Jay's own earlier explicit call (Task 6,
# 2026-07-26); this view is deliberately NOT entity-scoped, showing every
# request across every organization and every status in one place.

@app.get("/admin/all-requests", response_class=HTMLResponse)
def admin_all_requests(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not user.get("is_cfo"):
        return JSONResponse({"error": "CFO access required"}, status_code=403)

    rows = db.query(
        """
        SELECT pr.id AS pr_id, pr.request_number, pr.request_type, pr.amount, pr.status,
               pr.created_at, pr.updated_at, o.code AS org_code,
               pa.title AS program_area_title, u.display_name AS submitter_name,
               u.email AS submitter_email,
               v.display_name AS vendor_display_name,
               vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
               vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
               vr.dba_name AS vr_dba_name
        FROM checkreq.payment_requests pr
        JOIN checkreq.organizations o ON o.id = pr.org_id
        JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
        JOIN checkreq.app_users u ON u.id = pr.submitter_user_id
        LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
        LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
        ORDER BY pr.created_at DESC
        """
    )

    history_by_id: dict[int, list[dict]] = {}
    pr_ids = [r["pr_id"] for r in rows]
    if pr_ids:
        history_rows = db.query(
            "SELECT payment_request_id, action_type, action_date, comment, "
            "previous_status, new_status FROM checkreq.audit_log "
            "WHERE payment_request_id = ANY(%s) ORDER BY action_date",
            (pr_ids,),
        )
        for h in history_rows:
            history_by_id.setdefault(h["payment_request_id"], []).append(h)

    for r in rows:
        r["type_abbr"] = _REQUEST_TYPE_ABBR.get(r["request_type"], "??")
        r["history"] = history_by_id.get(r["pr_id"], [])
        if r.get("vendor_display_name"):
            r["vendor_name"] = r["vendor_display_name"]
        elif r.get("vr_entity_type"):
            r["vendor_name"] = _vendor_request_row_display_name(
                r["vr_entity_type"], r["vr_company_name"], r["vr_dba_name"],
                r["vr_first_name"], r["vr_last_name"],
            )
        else:
            r["vendor_name"] = "—"

    return _render(request, "admin_all_requests.html", user, {"rows": rows})


@app.get("/admin/feedback", response_class=HTMLResponse)
def feedback_list(request: Request):
    """CFO-only listing -- the task's own stated "nice-to-have," not the
    core ask (which is just collecting feedback). No edit/resolve workflow;
    read-only, matching the deliberately-simple scope of this feature."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not user.get("is_cfo"):
        return JSONResponse({"error": "CFO access required"}, status_code=403)

    rows = db.query(
        """
        SELECT f.id, f.comment, f.created_at, o.code AS org_code,
               u.display_name AS submitter_name, u.email AS submitter_email
        FROM checkreq.app_feedback f
        LEFT JOIN checkreq.organizations o ON o.id = f.org_id
        JOIN checkreq.app_users u ON u.id = f.submitted_by_user_id
        ORDER BY f.created_at DESC
        """
    )
    return _render(request, "admin_feedback.html", user, {"rows": rows})


# ── Test Mode (Jay, 2026-07-28) ──────────────────────────────────────────────
# "I would like you to put the entire system in Test mode whereby any emails
# that go out can be routed to a designated email for testing." A live,
# CFO-only in-app toggle (checkreq.app_settings) rather than a Cloud Run env
# var -- Jay flips this on/off himself while actively testing, and env-var
# changes on the live service have repeatedly needed his own elevated gcloud
# login in this project's history (a redeploy-free toggle avoids that entirely
# going forward). The actual redirect happens once, centrally, in
# email_client.py's _apply_test_mode() -- every send_email() call site (W-9
# request, rejection notice, and any future one) is covered automatically.

@app.get("/admin/test-mode", response_class=HTMLResponse)
def test_mode_form(request: Request, saved: bool = False):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not user.get("is_cfo"):
        return JSONResponse({"error": "CFO access required"}, status_code=403)

    enabled = app_settings.get_setting("email_test_mode", "false") == "true"
    address = app_settings.get_setting("email_test_mode_address", "") or ""
    return _render(request, "admin_test_mode.html", user, {"enabled": enabled, "address": address, "saved": saved})


@app.post("/admin/test-mode")
async def test_mode_save(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not user.get("is_cfo"):
        return JSONResponse({"error": "CFO access required"}, status_code=403)

    form = await request.form()
    enabled = form.get("enabled") == "1"
    address = (form.get("address") or "").strip()

    # An enabled toggle with no address configured would leave
    # _apply_test_mode() with nothing to redirect to -- it falls back to
    # sending real emails unchanged in that case (fail-open), but that's a
    # silent no-op that would confuse whoever just turned this on expecting
    # it to actually redirect. Refuse instead of silently accepting it.
    if enabled and not address:
        return _render(
            request, "admin_test_mode.html", user,
            {"enabled": False, "address": address,
             "error": "Enter a test email address before turning Test Mode on."},
        )

    app_settings.set_setting("email_test_mode", "true" if enabled else "false", user["id"])
    app_settings.set_setting("email_test_mode_address", address, user["id"])
    return RedirectResponse("/admin/test-mode?saved=1", status_code=303)


# ── New Vendor Onboarding: vendor-approval queue ─────────────────────────────
# Gated on is_vendor_approver (Section 6, decision 1) -- a new, dedicated
# role, deliberately NOT folded into is_cfo or GlobalApprovers. No per-org
# scoping table exists for this flag (the plan found no evidence it needs
# one), so a vendor-approver sees every org's pending requests, same
# blanket-access shape as is_cfo's own bypass elsewhere in this app.

def _require_vendor_approver(request: Request):
    """Returns (user, None) if allowed, or (None, error_response) if not --
    callers do `user, err = _require_vendor_approver(request); if err: return err`."""
    user = _current_user(request)
    if not user:
        return None, RedirectResponse("/login")
    if not user.get("is_vendor_approver"):
        return None, JSONResponse({"error": "Vendor-approver access required"}, status_code=403)
    return user, None


@app.get("/admin/vendor-requests", response_class=HTMLResponse)
def vendor_requests_list(request: Request, email_warning: str = ""):
    user, err = _require_vendor_approver(request)
    if err:
        return err

    rows = db.query(
        """
        SELECT vr.id, vr.entity_type, vr.first_name, vr.last_name, vr.company_name,
               vr.dba_name, vr.contact_name, vr.contact_email, vr.requires_w9,
               vr.w9_email_sent_at, vr.w9_received, vr.status, vr.rejected_reason,
               vr.created_at, o.name AS org_name, o.code AS org_code, pr.request_number, pr.amount
        FROM checkreq.vendor_requests vr
        JOIN checkreq.organizations o ON o.id = vr.org_id
        JOIN checkreq.payment_requests pr ON pr.id = vr.payment_request_id
        ORDER BY (vr.status = 'pending_approval') DESC, vr.created_at DESC
        """
    )
    for r in rows:
        r["display_name"] = _vendor_request_display_name(r)

    return _render(request, "vendor_requests.html", user, {"rows": rows, "email_warning": email_warning})


@app.post("/admin/vendor-requests/{vr_id}/approve")
def vendor_request_approve(vr_id: int, request: Request):
    """Approving (Section 3): sets status='approved' + stamps
    approved_by_user_id/approved_at; if requires_w9, sends the W-9 request
    email immediately and stamps w9_email_sent_at -- but ONLY on a real
    send success, so a delivery failure stays visible (surfaced via
    ?email_warning=) rather than silently claimed. Never touches QBO --
    that's the not-yet-built review queue's job (Section 3/5)."""
    user, err = _require_vendor_approver(request)
    if err:
        return err

    vr = db.query_one(
        "SELECT vr.*, o.name AS org_name FROM checkreq.vendor_requests vr "
        "JOIN checkreq.organizations o ON o.id = vr.org_id WHERE vr.id = %s",
        (vr_id,),
    )
    if not vr or vr["status"] != "pending_approval":
        return RedirectResponse("/admin/vendor-requests", status_code=303)

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.vendor_requests SET status = 'approved', "
                "approved_by_user_id = %s, approved_at = NOW() WHERE id = %s",
                (user["id"], vr_id),
            )

    email_warning = ""
    if vr["requires_w9"]:
        result = _send_w9_request_email(vr, vr["org_name"], request)
        if result.get("status") == "sent":
            with db.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE checkreq.vendor_requests SET w9_email_sent_at = NOW() WHERE id = %s",
                        (vr_id,),
                    )
        else:
            email_warning = result.get("error") or "unknown error sending the W-9 request email"

    redirect_url = "/admin/vendor-requests"
    if email_warning:
        from urllib.parse import quote
        redirect_url += f"?email_warning={quote(email_warning)}"
    return RedirectResponse(redirect_url, status_code=303)


@app.post("/admin/vendor-requests/{vr_id}/reject")
async def vendor_request_reject(vr_id: int, request: Request):
    user, err = _require_vendor_approver(request)
    if err:
        return err

    form = await request.form()
    reason = form.get("rejected_reason", "").strip() or None

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.vendor_requests SET status = 'rejected', "
                "approved_by_user_id = %s, approved_at = NOW(), rejected_reason = %s "
                "WHERE id = %s AND status = 'pending_approval'",
                (user["id"], reason, vr_id),
            )
    return RedirectResponse("/admin/vendor-requests", status_code=303)


@app.post("/admin/vendor-requests/{vr_id}/w9-received")
def vendor_request_w9_received(vr_id: int, request: Request):
    """Staff confirms a vendor's uploaded W-9 was actually reviewed and is
    correct -- the upload itself never flips this flag on its own (Section
    4a: 'Staff still confirms it, rather than the upload alone flipping the
    gate' -- catches a wrong/incomplete document before it silently
    unblocks the not-yet-built QBO-posting step)."""
    user, err = _require_vendor_approver(request)
    if err:
        return err

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.vendor_requests SET w9_received = TRUE WHERE id = %s",
                (vr_id,),
            )
    return RedirectResponse("/admin/vendor-requests", status_code=303)


# ── AP Review screen (AP Review Workflow Plan.md, Section 3/4) ─────────────
# Gated on is_ap_reviewer (Section 1b) -- a new, dedicated role, deliberately
# NOT folded into is_cfo or is_vendor_approver. No per-org scoping table for
# this flag either (Decision 4: any single AP reviewer can act alone, same
# blanket-access precedent as is_vendor_approver) -- an AP reviewer sees
# every org's fully-chain-approved requests, org-wide.

@app.get("/admin/ap-review", response_class=HTMLResponse)
def ap_review_list(request: Request, posted: str = "", returned: str = "",
                    post_error: str = "", email_warning: str = "", view: str = "pending"):
    user, err = _require_ap_reviewer(request)
    if err:
        return err

    # Jay, 2026-07-29: "some sort of need in the AP review to also have a
    # completed tab as well." A request leaves this queue the moment it's
    # posted (status flips to 'Posted to QBO') with no way to look back at
    # it from here -- same gap as My Approvals' own history request.
    if view == "completed":
        completed_rows = db.query(
            """
            SELECT pr.request_number, pr.request_type, pr.amount, pr.qbo_bill_id,
                   pr.qbo_bill_url, pr.updated_at, o.code AS org_code,
                   pa.title AS program_area_title,
                   v.display_name AS vendor_display_name,
                   vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
                   vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
                   vr.dba_name AS vr_dba_name
            FROM checkreq.payment_requests pr
            JOIN checkreq.organizations o ON o.id = pr.org_id
            JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
            LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
            LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
            WHERE pr.status = 'Posted to QBO'
            ORDER BY pr.updated_at DESC
            """
        )
        for r in completed_rows:
            r["type_abbr"] = _REQUEST_TYPE_ABBR.get(r["request_type"], "??")
            if r.get("vendor_display_name"):
                r["vendor_name"] = r["vendor_display_name"]
            elif r.get("vr_entity_type"):
                r["vendor_name"] = _vendor_request_row_display_name(
                    r["vr_entity_type"], r["vr_company_name"], r["vr_dba_name"],
                    r["vr_first_name"], r["vr_last_name"],
                )
            else:
                r["vendor_name"] = "—"
        return _render(request, "ap_review_completed.html", user, {"rows": completed_rows})

    rows = db.query(
        """
        SELECT pr.id AS pr_id, pr.request_number, pr.request_type, pr.amount,
               pr.approval_chain_summary, pr.created_at, pr.vendor_request_id,
               pr.overspend_flagged, pr.overspend_detail,
               o.code AS org_code, o.name AS org_name,
               pa.title AS program_area_title, u.display_name AS submitter_name,
               u.email AS submitter_email,
               v.display_name AS vendor_display_name,
               vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
               vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
               vr.dba_name AS vr_dba_name, vr.status AS vr_status,
               vr.requires_w9 AS vr_requires_w9, vr.w9_received AS vr_w9_received
        FROM checkreq.payment_requests pr
        JOIN checkreq.organizations o ON o.id = pr.org_id
        JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
        JOIN checkreq.app_users u ON u.id = pr.submitter_user_id
        LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
        LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
        WHERE pr.status = 'Approved'
        ORDER BY pr.created_at
        """
    )
    for r in rows:
        r["type_abbr"] = _REQUEST_TYPE_ABBR.get(r["request_type"], "??")
        if r.get("vendor_display_name"):
            r["vendor_name"] = r["vendor_display_name"]
        elif r.get("vr_entity_type"):
            r["vendor_name"] = _vendor_request_row_display_name(
                r["vr_entity_type"], r["vr_company_name"], r["vr_dba_name"],
                r["vr_first_name"], r["vr_last_name"],
            )
        else:
            r["vendor_name"] = "—"

        # Section 4, step 2: the same vendor gate New Vendor Onboarding
        # Plan.md's own Section 5 specifies -- 'Post to QBO' waits here,
        # visibly, until it clears. Also the interlock point Section 6
        # flags for a future CFO-overspend flag: this per-row loop and the
        # template's per-row cell layout deliberately leave room for one
        # more status indicator alongside vendor_gate_wait, without needing
        # to restructure either when that (separate, not-yet-built) feature
        # lands.
        if r["vendor_request_id"]:
            if r["vr_status"] != "approved":
                r["vendor_gate_wait"] = f"Vendor not yet approved (status: {r['vr_status']})"
            elif r["vr_requires_w9"] and not r["vr_w9_received"]:
                r["vendor_gate_wait"] = "W-9 not yet received"
            else:
                r["vendor_gate_wait"] = None
        else:
            r["vendor_gate_wait"] = None

    return _render(request, "ap_review.html", user, {
        "rows": rows, "posted": posted, "returned": returned,
        "post_error": post_error, "email_warning": email_warning,
    })


@app.post("/requests/{request_number}/post-to-qbo")
def post_to_qbo(request_number: str, request: Request):
    """AP Review Workflow Plan.md, Section 4 -- sequencing and failure
    handling, implemented exactly per that section's own numbered steps:

      1. Re-select + confirm status == 'Approved' at the moment of the
         click (guards a double-click/race).
      2. If vendor_request_id is set, check the New Vendor Onboarding gate
         (vendor_requests.status == 'approved' AND (NOT requires_w9 OR
         w9_received)) -- if it fails, post NOTHING; leave status at
         'Approved', show a clear wait reason (an expected wait state, not
         an error -- the row just stays here until the vendor side clears).
      3/4. If the gate passes and qbo_vendor_id is still NULL, call
         qbo_mcp_client.create_vendor() -- its first real caller. On
         success, stamp vendor_requests.qbo_vendor_id + status=
         'posted_to_qbo' in its own committed step, separate from the Bill
         call that follows. This makes a retry after a Bill failure
         naturally idempotent: step 3 is skipped automatically next time
         since qbo_vendor_id is already set -- nothing extra to track.
         On failure: stop here entirely, surface QBO's real Fault detail
         (this codebase's hard-won 2026-07-22 lesson from 26-124's
         JE-collision incident -- never a generic error), leave status
         'Approved', safe to retry.
      5. Resolve the real QBO vendor id to bill against: checkreq.vendors.
         qbo_vendor_id for an existing-vendor request, or the just-resolved
         vendor_requests.qbo_vendor_id for a new-vendor request.
      6. Call qbo_mcp_client.create_bill() with the request's GL lines,
         doc_number=request_number, private_note=approval_chain_summary.
         On success: stamp qbo_bill_id/qbo_bill_url, status=
         'Posted to QBO', audit_log row. On failure: same philosophy as
         step 3 -- surface the real error, leave status 'Approved'.
      7. Only once status actually reaches 'Posted to QBO': call the
         existing, already-written cleanup_gcs_attachment() -- its first
         real caller (Section 5) -- wrapped in its own try/except, matching
         _archive_attachments'/_send_w9_request_email's own established "a
         storage hiccup is recoverable and must never block or un-post a
         real transaction" philosophy. A cleanup failure here is logged,
         not surfaced -- the Bill is already posted and that's the outcome
         that matters.
    """
    from urllib.parse import quote

    user, err = _require_ap_reviewer(request)
    if err:
        return err

    pr = db.query_one(
        "SELECT pr.*, o.code AS org_code FROM checkreq.payment_requests pr "
        "JOIN checkreq.organizations o ON o.id = pr.org_id "
        "WHERE pr.request_number = %s",
        (request_number,),
    )
    if not pr:
        return JSONResponse({"error": "Request not found"}, status_code=404)
    if pr["status"] != "Approved":
        return RedirectResponse(
            f"/admin/ap-review?post_error="
            f"{quote(request_number + ': someone already acted on this request (status is now ' + pr['status'] + ').')}",
            status_code=303,
        )

    company = pr["org_code"]

    # Step 2/3/4: New Vendor Onboarding gate + vendor creation, only if this
    # request used the "Add a new vendor" panel instead of an existing
    # checkreq.vendors row.
    qbo_vendor_id = None
    if pr["vendor_request_id"]:
        vr = db.query_one(
            "SELECT * FROM checkreq.vendor_requests WHERE id = %s", (pr["vendor_request_id"],),
        )
        if not vr or vr["status"] != "approved":
            return RedirectResponse(
                f"/admin/ap-review?post_error={quote(request_number + ': vendor not yet approved.')}",
                status_code=303,
            )
        if vr["requires_w9"] and not vr["w9_received"]:
            return RedirectResponse(
                f"/admin/ap-review?post_error={quote(request_number + ': W-9 not yet received.')}",
                status_code=303,
            )

        if vr["qbo_vendor_id"]:
            qbo_vendor_id = vr["qbo_vendor_id"]
        else:
            display_name = _vendor_request_display_name(vr)
            result, vendor_error = qbo_mcp_client.create_vendor(
                company, display_name,
                company_name=vr.get("company_name") or "",
                address_line1=vr.get("address_line1") or "", address_line2=vr.get("address_line2") or "",
                city=vr.get("city") or "", state=vr.get("state") or "", zip_code=vr.get("zip") or "",
                phone=vr.get("phone") or "", email=vr.get("contact_email") or "",
            )
            if vendor_error:
                return RedirectResponse(
                    f"/admin/ap-review?post_error="
                    f"{quote(f'{request_number}: vendor creation failed -- {vendor_error}')}",
                    status_code=303,
                )
            qbo_vendor_id = result["vendor_id"]
            with db.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE checkreq.vendor_requests SET qbo_vendor_id = %s, "
                        "status = 'posted_to_qbo' WHERE id = %s",
                        (qbo_vendor_id, vr["id"]),
                    )
    else:
        v = db.query_one("SELECT qbo_vendor_id FROM checkreq.vendors WHERE id = %s", (pr["vendor_id"],))
        if not v or not v.get("qbo_vendor_id"):
            return RedirectResponse(
                f"/admin/ap-review?post_error="
                f"{quote(request_number + ': this vendor has no qbo_vendor_id on file -- cannot post.')}",
                status_code=303,
            )
        qbo_vendor_id = v["qbo_vendor_id"]

    # Step 5/6: resolve GL lines and post the Bill.
    gl_lines = db.query(
        "SELECT gl.amount, gl.memo, ga.account_number FROM checkreq.payment_request_gl_lines gl "
        "JOIN checkreq.gl_accounts ga ON ga.id = gl.gl_account_id "
        "WHERE gl.payment_request_id = %s ORDER BY gl.id",
        (pr["id"],),
    )
    # Jay, 2026-07-29, real gaps found on the first live post (CR26-007):
    # (1) no line item description -- a blank GL-line memo (optional by
    # design) left the Bill's own line with nothing at all; fall back to
    # the request's own Description so a line is never blank.
    # (2) the Bill memo showed "Group 1: mdickson-patrick" -- the internal
    # approval_chain_summary means nothing to anyone reading the Bill in
    # QBO. Use the request's own Description instead, matching (1)'s fix.
    bill_lines = [
        {"account_ref": g["account_number"], "amount": float(g["amount"]),
         "description": g["memo"] or pr["description"] or ""}
        for g in gl_lines
    ]

    # Jay, 2026-07-29: "no attachments existed on the document" -- fetch this
    # request's own already-archived files (SharePoint, the same source
    # view_attachment reads from) and carry them onto the QBO Bill too.
    # Best-effort: a download hiccup here must never block the Bill itself
    # from posting (matches _archive_attachments'/cleanup_gcs_attachment's
    # own established "storage hiccups are recoverable" philosophy) --
    # logged server-side, not surfaced as a hard failure.
    qbo_attachments = []
    active_atts = _active_attachments(pr["id"])
    if active_atts:
        org_sp = db.query_one(
            "SELECT sp_hostname, sp_site_path FROM checkreq.organizations WHERE id = %s",
            (pr["org_id"],),
        )
        if org_sp and org_sp.get("sp_hostname") and org_sp.get("sp_site_path"):
            try:
                sp_token = sharepoint_client.get_access_token()
                sp_site_id = sharepoint_client.get_site_id(sp_token, org_sp["sp_hostname"], org_sp["sp_site_path"])
                for att in active_atts:
                    try:
                        content = sharepoint_client.download_bytes(sp_token, sp_site_id, att["sp_file_path"])
                        qbo_attachments.append({
                            "filename": att["archived_filename"],
                            "content_base64": base64.b64encode(content).decode("ascii"),
                        })
                    except Exception as exc:
                        print(f"[post_to_qbo] attachment download failed for {request_number} "
                              f"({att['archived_filename']}): {exc}")
            except Exception as exc:
                print(f"[post_to_qbo] SharePoint auth failed for {request_number}: {exc}")

    result, bill_error = qbo_mcp_client.create_bill(
        company, qbo_vendor_id,
        date.today().isoformat(),
        bill_lines, doc_number=pr["request_number"],
        private_note=pr["description"] or f"Check Request {pr['request_number']}",
        due_date=pr["requested_pay_date"].isoformat() if pr["requested_pay_date"] else None,
        attachments=qbo_attachments,
    )
    if bill_error:
        return RedirectResponse(
            f"/admin/ap-review?post_error={quote(f'{request_number}: Bill creation failed -- {bill_error}')}",
            status_code=303,
        )

    imp_id = request.session.get("impersonating_user_id")
    impersonated_by = _real_user(request)["id"] if imp_id else None

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.payment_requests SET status = 'Posted to QBO', "
                "qbo_bill_id = %s, qbo_bill_url = %s, updated_at = NOW() WHERE id = %s",
                (result.get("bill_id"), result.get("qbo_url"), pr["id"]),
            )
            cur.execute(
                "INSERT INTO checkreq.audit_log "
                "(payment_request_id, action_by_user_id, action_type, comment, "
                " previous_status, new_status, impersonated_by_user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (pr["id"], user["id"], "Posted to QBO",
                 f"QBO Bill {result.get('bill_number') or result.get('bill_id')} created.",
                 "Approved", "Posted to QBO", impersonated_by),
            )

    # Step 7: cleanup_gcs_attachment()'s first real call site.
    try:
        cleanup_gcs_attachment(pr["id"])
    except Exception as exc:
        print(f"[post-to-qbo] cleanup_gcs_attachment failed for {request_number}: {exc}")

    return RedirectResponse(f"/admin/ap-review?posted={request_number}", status_code=303)


@app.post("/requests/{request_number}/ap-return")
async def ap_return_request(request_number: str, request: Request):
    """AP Review Workflow Plan.md, Section 3 ('Return to Submitter') +
    Decision 1: AP-stage rejection uses a NEW, distinct 'Returned by AP'
    status -- NOT the same terminal 'Rejected' the mid-chain reject route
    uses -- since these are semantically different situations (a hard
    denial vs. 'AP found a data problem, fix and resend'). Unconditionally
    re-enters the request into Edit: the actual "always re-enter the chain
    regardless of what changed" mechanics live in new_request_submit's edit
    branch (status_forces_reset), not here -- this route only needs to set
    the status. Decision 2: emails the submitter, same path as the
    mid-chain reject route."""
    user, err = _require_ap_reviewer(request)
    if err:
        return err

    form = await request.form()
    reason = (form.get("return_reason") or "").strip()
    if not reason:
        return JSONResponse({"error": "A reason is required."}, status_code=400)

    pr = db.query_one(
        "SELECT pr.*, u.email AS submitter_email, u.display_name AS submitter_name, "
        "o.name AS org_name "
        "FROM checkreq.payment_requests pr "
        "JOIN checkreq.app_users u ON u.id = pr.submitter_user_id "
        "JOIN checkreq.organizations o ON o.id = pr.org_id "
        "WHERE pr.request_number = %s",
        (request_number,),
    )
    if not pr:
        return JSONResponse({"error": "Request not found"}, status_code=404)
    if pr["status"] != "Approved":
        return JSONResponse({"error": "This request is not on the AP review queue right now."}, status_code=400)

    imp_id = request.session.get("impersonating_user_id")
    impersonated_by = _real_user(request)["id"] if imp_id else None

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.payment_requests SET status = 'Returned by AP', "
                "current_approver_id = NULL, serial_group_current = NULL, updated_at = NOW() "
                "WHERE id = %s",
                (pr["id"],),
            )
            cur.execute(
                "INSERT INTO checkreq.audit_log "
                "(payment_request_id, action_by_user_id, action_type, comment, "
                " previous_status, new_status, impersonated_by_user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (pr["id"], user["id"], "AP Rejected", reason, pr["status"], "Returned by AP", impersonated_by),
            )

    email_result = _send_rejection_email(pr, "Returned by AP", reason, request,
                                          user.get("display_name") or user.get("email"))
    from urllib.parse import quote
    redirect_url = f"/admin/ap-review?returned={request_number}"
    if email_result.get("status") != "sent":
        redirect_url += f"&email_warning={quote(email_result.get('error') or 'unknown error sending return email')}"
    return RedirectResponse(redirect_url, status_code=303)


# ── W-9 upload — the ONE deliberate unauthenticated route in this app ───────
# (Section 4a). The person on the other end (an external vendor contact) has
# no account here. Hardened per the plan: 404 (not a distinguishing error)
# on a bad/wrong-state token; same size/mime-type allowlist as the existing
# extraction route; NEVER routed through document_extract.py's AI pipeline
# (a completed W-9 contains a real SSN/EIN -- confirmed with Jay, no
# extraction on W-9 uploads, ever); uploaded file goes straight to the same
# GCS + SharePoint archive pattern already built for check-request
# attachments.

_W9_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
_W9_UPLOAD_ALLOWED_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp"}


@app.get("/vendor-w9-upload/{token}", response_class=HTMLResponse)
def vendor_w9_upload_form(token: str, request: Request):
    vr = _vendor_request_by_upload_token(token)
    if not vr:
        return JSONResponse({"error": "Not found"}, status_code=404)

    return templates.TemplateResponse(request, "vendor_w9_upload.html", {
        "already_uploaded": bool(vr["w9_uploaded_at"]),
        "vendor_name": _vendor_request_display_name(vr),
        "org_name": vr["org_name"],
        "token": token,
        "error": "",
    })


@app.post("/vendor-w9-upload/{token}", response_class=HTMLResponse)
async def vendor_w9_upload_submit(token: str, request: Request, file: UploadFile):
    vr = _vendor_request_by_upload_token(token)
    if not vr:
        return JSONResponse({"error": "Not found"}, status_code=404)

    vendor_name = _vendor_request_display_name(vr)

    content = await file.read()
    if len(content) > _W9_UPLOAD_MAX_BYTES:
        return templates.TemplateResponse(request, "vendor_w9_upload.html", {
            "already_uploaded": False, "vendor_name": vendor_name, "org_name": vr["org_name"],
            "token": token, "error": "File is too large (max 10MB).",
        })
    mime_type = file.content_type or ""
    if mime_type not in _W9_UPLOAD_ALLOWED_TYPES:
        return templates.TemplateResponse(request, "vendor_w9_upload.html", {
            "already_uploaded": False, "vendor_name": vendor_name, "org_name": vr["org_name"],
            "token": token,
            "error": f"Unsupported file type ({mime_type or 'unknown'}). Please upload a PDF, "
                     f"or a JPG/PNG/GIF/WebP photo of the completed form.",
        })

    ext = _guess_extension(file.filename or "w9", mime_type)
    archived_filename = (
        f"{vr['org_code'].upper()} W9 {_custom_titlecase(vendor_name)} "
        f"{datetime.today().strftime('%Y.%m.%d')}.{ext}"
    )

    gcs_path = None
    sp_path = None
    try:
        gcs_path = f"vendor_w9/{vr['id']}/{archived_filename}"
        gcs_client.upload_bytes(ATTACHMENTS_BUCKET, gcs_path, content, mime_type)

        if vr.get("sp_hostname") and vr.get("sp_site_path") and vr.get("sp_library_folder"):
            sp_token = sharepoint_client.get_access_token()
            site_id = sharepoint_client.get_site_id(sp_token, vr["sp_hostname"], vr["sp_site_path"])
            sharepoint_client.upload_bytes(
                sp_token, site_id, vr["sp_library_folder"], archived_filename, content, mime_type,
            )
            sp_path = f"{vr['sp_library_folder'].strip('/')}/{archived_filename}"
    except Exception as exc:
        # Never show the external vendor a raw internal error, and never
        # silently claim success either -- log server-side; the admin page
        # is where a staff member finds out archival needs manual attention.
        print(f"[vendor-w9-upload] archive error for token {token[:8]}...: {exc}")

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.vendor_requests SET w9_file_gcs_path = %s, w9_file_sp_path = %s, "
                "w9_uploaded_at = NOW() WHERE id = %s",
                (gcs_path, sp_path, vr["id"]),
            )

    return templates.TemplateResponse(request, "vendor_w9_upload.html", {
        "already_uploaded": True, "vendor_name": vendor_name, "org_name": vr["org_name"],
        "token": token, "error": "",
    })


def _html_to_pdf_bytes(html: str) -> bytes:
    """Renders arbitrary HTML/CSS to PDF bytes using a real headless Chromium
    instance via Playwright. Replaces xhtml2pdf (2026-07-25) after Jay
    reported the generated Check Request PDF was "poorly rendered,
    impossible to read... not even like the screen." Confirmed empirically,
    not guessed: rendering the exact same check_voucher.html/.css through
    xhtml2pdf produced a page where nearly every field sat in its own
    stray bordered box with huge dead vertical whitespace between label and
    value (a side-by-side pdfplumber word-position dump showed ~35-40pt gaps
    between elements a real browser renders 2-3pt apart), the Amount box
    was split in two by an unwanted rule, letter-spacing in 'em' units threw
    silent "Not a float" parser warnings, and CSS custom properties
    (var(--token)) never resolved at all without a manual pre-resolution
    workaround (removed along with this rewrite). None of that is fixable
    CSS-tuning -- xhtml2pdf's layout engine simply cannot reproduce this
    modern, already-correct design. Using a real browser engine instead
    makes the PDF pixel-faithful to the already-verified-correct live
    preview pane, since it IS that same rendering engine -- var() resolves
    natively, no CSS translation layer of any kind is needed.

    Launches a fresh headless Chromium instance in a dedicated OS thread on
    every call. This keeps the function safely callable both from a plain
    sync route (GET /requests/{request_number}/pdf) and from deep inside an
    async route (new_request_submit -> _archive_attachments): Playwright's
    sync API refuses to run on a thread that already has an asyncio event
    loop attached, but a brand-new thread never has one, regardless of what
    the calling thread is doing. Not optimized for high volume (a fresh
    browser launch per call) -- an accepted tradeoff for a low-traffic
    internal AP tool where PDF generation is roughly one-per-submission /
    one-per-view, never a hot path."""
    result: dict = {}

    def _run():
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    page = browser.new_page()
                    # networkidle so the Google Fonts <link> below finishes
                    # loading before the page is rasterized to PDF.
                    page.set_content(html, wait_until="networkidle", timeout=20000)
                    result["pdf"] = page.pdf(
                        format="Letter",
                        print_background=True,
                        margin={"top": "0.4in", "bottom": "0.4in", "left": "0.4in", "right": "0.4in"},
                    )
                finally:
                    browser.close()
        except Exception as exc:  # re-raised on the caller's thread below
            result["error"] = exc

    t = threading.Thread(target=_run)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result["pdf"]


def render_check_voucher_pdf(payment_request_id: int) -> bytes:
    """Renders check_voucher.html with real data for one payment request and
    converts it to PDF bytes via a real headless-Chromium render (see
    _html_to_pdf_bytes). Shared by the on-demand
    GET /requests/{request_number}/pdf route (always regenerates fresh, never
    stored) AND new_request_submit's attachment pipeline (which archives one
    point-in-time copy to GCS+SharePoint at submission time) -- one
    implementation, not duplicated."""
    ctx = _voucher_context(payment_request_id)
    fragment = templates.env.get_template("check_voucher.html").render(ctx)

    static_css_dir = os.path.join(os.path.dirname(__file__), "static", "css")
    with open(os.path.join(static_css_dir, "tokens.css"), encoding="utf-8") as f:
        tokens_css = f.read()
    with open(os.path.join(static_css_dir, "check_voucher.css"), encoding="utf-8") as f:
        voucher_css = f.read()

    # Real Chromium resolves var(--token) natively -- no CSS-variable
    # pre-resolution needed (that was purely an xhtml2pdf workaround). The
    # Google Fonts <link> mirrors base.html's -- this fragment normally only
    # inherits fonts from the app shell; standalone here, it needs its own.
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Merriweather:wght@300;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
{tokens_css}
{voucher_css}
html, body {{ margin: 0; padding: 0; background: #fff; }}
body {{ padding: 20px 24px; }}
</style>
</head><body>{fragment}</body></html>"""

    return _html_to_pdf_bytes(html)


@app.get("/requests/{request_number}/pdf")
def request_pdf(request_number: str, request: Request):
    """Regenerates the check-voucher PDF fresh from the live DB record every
    time this route is hit -- always current, never stale, distinct from the
    point-in-time archived copy created at submission (see
    render_check_voucher_pdf's docstring)."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    pr = db.query_one(
        "SELECT id, submitter_user_id, program_area_id FROM checkreq.payment_requests WHERE request_number = %s",
        (request_number,),
    )
    if not pr:
        return JSONResponse({"error": "Request not found"}, status_code=404)

    allowed = (
        user["is_cfo"]
        or pr["submitter_user_id"] == user["id"]
        or _user_can_submit_for(user, pr["program_area_id"])
    )
    if not allowed:
        return JSONResponse({"error": "Not authorized to view this request"}, status_code=403)

    pdf_bytes = render_check_voucher_pdf(pr["id"])
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{request_number}.pdf"'},
    )


@app.get("/requests/{request_number}/view", response_class=HTMLResponse)
def request_view(request_number: str, request: Request):
    """Jay, 2026-07-28 (My Approvals feedback): "There is no way to view the
    CR on the screen -- PDF isn't sufficient." A real in-app HTML view --
    same check_voucher.html fragment request_pdf renders to PDF, shown
    directly instead of a downloaded file, plus the attachment list
    (approvers need to see the actual invoice, not just the coded amount).
    Same authorization as request_pdf, PLUS anyone in this request's
    approval chain (an approver may not otherwise pass
    _user_can_submit_for -- their own program-area assignment and their
    approval-routing assignment are two different tables), PLUS any
    is_ap_reviewer/is_vendor_approver (2026-07-29 -- Request # is now a
    link from AP Review and Vendor Approvals too, and neither role implies
    program-area membership on the specific request being reviewed)."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    pr = db.query_one(
        "SELECT id, submitter_user_id, program_area_id, status FROM checkreq.payment_requests "
        "WHERE request_number = %s",
        (request_number,),
    )
    if not pr:
        return JSONResponse({"error": "Request not found"}, status_code=404)

    is_approver = db.query_one(
        "SELECT 1 FROM checkreq.approval_actions WHERE payment_request_id = %s AND approver_user_id = %s",
        (pr["id"], user["id"]),
    )
    allowed = (
        user["is_cfo"]
        or pr["submitter_user_id"] == user["id"]
        or _user_can_submit_for(user, pr["program_area_id"])
        or bool(is_approver)
        or bool(user.get("is_ap_reviewer"))
        or bool(user.get("is_vendor_approver"))
    )
    if not allowed:
        return JSONResponse({"error": "Not authorized to view this request"}, status_code=403)

    ctx = _voucher_context(pr["id"]) or {}
    return _render(request, "request_view.html", user, {
        **ctx,
        "request_number": request_number,
        "pr_status": pr["status"],
        "attachments": _active_attachments(pr["id"]),
    })
