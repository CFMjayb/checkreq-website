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
  - Approval action UI (approve/reject/escalate) — chain is computed and
    stored, but nothing drives it forward yet
  - QBO posting trigger on final approval
  - File attachment storage (no bucket wired up)
  - New-user provisioning UX: a first-time login auto-creates an app_users
    row (see /auth/callback below), but nothing yet assigns them to a
    program area or approval role — an admin still has to do that separately.
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import secrets as pysecrets
from datetime import date, datetime

from fastapi import FastAPI, Request, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from xhtml2pdf import pisa

import db
import approval_engine
import auth_azure
import gcs_client
import sharepoint_client
import document_extract

ATTACHMENTS_BUCKET = "cfm-checkreq-attachments"

app = FastAPI(title="26-129 Check Request")

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

# Portal module tiles. A plain list is enough for this scope (5 tiles) --
# revisit as a real "modules" config table if this grows much past that.
MODULES = [
    {"key": "check_request", "title": "Check Request", "desc": "Submit a classic check request.", "url": "/new-request", "enabled": True},
    {"key": "my_requests", "title": "My Requests", "desc": "Track requests you've submitted.", "url": "/my-requests", "enabled": True},
    {"key": "invoice_payment", "title": "Invoice for Payment", "desc": "Upload and match an invoice.", "url": None, "enabled": False},
    {"key": "vendor_requests", "title": "Vendor Requests", "desc": "Request a new vendor be added.", "url": None, "enabled": False},
    {"key": "approval_queue", "title": "Approval Queue", "desc": "Review requests awaiting your approval.", "url": None, "enabled": False},
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


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    """Renders the styled sign-in card. The actual redirect to Microsoft is
    a separate route (/auth/start) -- this route must render, not redirect,
    or the cathedral-themed login page is unreachable in normal use."""
    if _current_user(request):
        return RedirectResponse("/portal")
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.get("/auth/start")
def auth_start(request: Request):
    state = pysecrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    return RedirectResponse(auth_azure.get_auth_url(REDIRECT_URI, state))


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

    # Auto-provision the local profile row on first successful login (Plan.md
    # R1: submitter identity captured automatically, no manual entry). This is
    # NOT the same as tenant provisioning (a real cfmins.org identity must
    # already exist for login to succeed at all) — just our local profile.
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO checkreq.app_users (email, display_name, azure_ad_object_id, last_login_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (email) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    azure_ad_object_id = EXCLUDED.azure_ad_object_id,
                    last_login_at = NOW()
                RETURNING id, is_active
                """,
                (email, display_name, oid),
            )
            row = cur.fetchone()

    if not row["is_active"]:
        return templates.TemplateResponse(request, "login.html", {"error": f"{email} is deactivated in checkreq.app_users — contact an admin."})

    request.session["user_id"] = row["id"]
    return RedirectResponse("/portal", status_code=303)


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
    })


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
               COALESCE(NULLIF(pga.display_text, ''), ga.account_name) AS account_name
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
    chain = approval_engine.build_approval_chain(program_area_id, amount)
    return {"summary": approval_engine.describe_chain(chain)}


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


def _resolve_css_vars(css_text: str, tokens_css: str) -> str:
    """xhtml2pdf's CSS engine does not resolve CSS custom properties --
    verified empirically (var(--x) silently falls back to black/default
    instead of the real value). Substitute them with literal values before
    handing CSS to xhtml2pdf; the live browser preview keeps using the real
    var()-based stylesheet unmodified via a normal <link> tag -- this
    resolution only ever applies on the PDF-rendering path."""
    tokens = dict(re.findall(r"--([\w-]+)\s*:\s*([^;]+);", tokens_css))

    def repl(m):
        return tokens.get(m.group(1), m.group(0))

    return re.sub(r"var\(--([\w-]+)\)", repl, css_text)


def _voucher_context(payment_request_id: int) -> dict | None:
    """Builds the same voucher_* context keys new_request_form seeds with
    blanks -- used to render check_voucher.html with real data for the PDF."""
    pr = db.query_one(
        """
        SELECT pr.*, o.name AS org_name, pa.title AS program_area_title,
               v.display_name AS vendor_name, u.display_name AS submitter_name, u.email AS submitter_email
        FROM checkreq.payment_requests pr
        JOIN checkreq.organizations o ON o.id = pr.org_id
        JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
        LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
        JOIN checkreq.app_users u ON u.id = pr.submitter_user_id
        WHERE pr.id = %s
        """,
        (payment_request_id,),
    )
    if not pr:
        return None
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
    return {
        "voucher_org_name": pr["org_name"],
        "voucher_request_number": pr["request_number"],
        "voucher_date": pr["requested_pay_date"].strftime("%B %d, %Y") if pr["requested_pay_date"] else "—",
        "voucher_vendor": pr["vendor_name"] or "—",
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
    }


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

    vendor_row = db.query_one(
        "SELECT v.display_name FROM checkreq.payment_requests pr "
        "LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id WHERE pr.id = %s",
        (payment_request_id,),
    )
    vendor_name = vendor_row["display_name"] if vendor_row else None

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


def _next_request_number() -> str:
    today = date.today().strftime("%Y%m%d")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM checkreq.payment_requests WHERE request_number LIKE %s",
                (f"CR-{today}-%",),
            )
            n = cur.fetchone()["n"] + 1
    return f"CR-{today}-{n:03d}"


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
    vendor_id = int(form["vendor_id"]) if form.get("vendor_id") else None
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

    # Optional user attachments (not required -- see form). Read all bytes
    # now, while we still have the async UploadFile objects; everything
    # downstream (archival) works with plain bytes.
    uploaded_attachments: list[tuple[str, str, bytes]] = []
    for f in form.getlist("attachments"):
        if getattr(f, "filename", None):
            content = await f.read()
            if content:
                uploaded_attachments.append((f.filename, f.content_type or "application/octet-stream", content))

    chain = approval_engine.build_approval_chain(program_area_id, total_amount)
    chain_summary = approval_engine.describe_chain(chain)
    first_step = chain[0] if chain else None

    request_number = _next_request_number()
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO checkreq.payment_requests
                    (request_number, request_type, org_id, program_area_id, submitter_user_id,
                     vendor_id, amount, requested_pay_date, description, special_instructions,
                     status, current_approver_id, serial_group_current, approval_chain_summary)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (request_number, request_type, org_id, program_area_id, user["id"],
                 vendor_id, total_amount, requested_pay_date, description, special_instructions,
                 "Submitted",
                 first_step["approver_user_id"] if first_step else None,
                 first_step["serial_group"] if first_step else None,
                 chain_summary),
            )
            payment_request_id = cur.fetchone()["id"]

            for acct_id, amt, memo in gl_lines:
                cur.execute(
                    "INSERT INTO checkreq.payment_request_gl_lines "
                    "(payment_request_id, gl_account_id, amount, memo) VALUES (%s, %s, %s, %s)",
                    (payment_request_id, acct_id, amt, memo),
                )

            imp_id = request.session.get("impersonating_user_id")
            impersonated_by = _real_user(request)["id"] if imp_id else None
            cur.execute(
                "INSERT INTO checkreq.audit_log "
                "(payment_request_id, action_by_user_id, action_type, comment, new_status, impersonated_by_user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (payment_request_id, user["id"], "Submitted", chain_summary, "Submitted", impersonated_by),
            )

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


@app.get("/my-requests", response_class=HTMLResponse)
def my_requests(request: Request, submitted: str = "", archive_warning: str = ""):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    rows = db.query(
        """
        SELECT pr.request_number, pr.request_type, pr.amount, pr.status,
               pr.approval_chain_summary, pr.created_at, o.name AS org_name,
               pa.title AS program_area_title
        FROM checkreq.payment_requests pr
        JOIN checkreq.organizations o ON o.id = pr.org_id
        JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
        WHERE pr.submitter_user_id = %s
        ORDER BY pr.created_at DESC
        """,
        (user["id"],),
    )
    return _render(request, "my_requests.html", user, {
        "rows": rows, "submitted": submitted, "archive_warning": archive_warning,
    })


def render_check_voucher_pdf(payment_request_id: int) -> bytes:
    """Renders check_voucher.html with real data for one payment request and
    converts it to PDF bytes via xhtml2pdf. Shared by the on-demand
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
    resolved_css = _resolve_css_vars(voucher_css, tokens_css)

    html = f"""<html><head><meta charset="utf-8">
<style>{resolved_css}
body {{ margin: 0; padding: 20px; font-family: Helvetica, sans-serif; }}</style>
</head><body>{fragment}</body></html>"""

    buf = io.BytesIO()
    pisa.CreatePDF(html, dest=buf)
    buf.seek(0)
    return buf.read()


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
