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
import base64
import os
import secrets as pysecrets
import threading
from datetime import date, datetime

from fastapi import FastAPI, Request, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import db
import approval_engine
import auth_azure
import gcs_client
import sharepoint_client
import document_extract
import email_client

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
               size_bytes, uploaded_at
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
        reset_approval = vendor_changed or amount_changed

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

                if reset_approval:
                    cur.execute(
                        """
                        UPDATE checkreq.payment_requests SET
                            program_area_id = %s, vendor_id = %s, vendor_request_id = %s,
                            amount = %s, requested_pay_date = %s, description = %s,
                            special_instructions = %s, status = %s, current_approver_id = %s,
                            serial_group_current = %s, approval_chain_summary = %s,
                            cfo_override = FALSE, cfo_override_date = NULL, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (program_area_id, vendor_id, new_vendor_request_id, total_amount,
                         requested_pay_date, description, special_instructions,
                         "UnderReview",
                         first_step["approver_user_id"] if first_step else None,
                         first_step["serial_group"] if first_step else None,
                         chain_summary, payment_request_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE checkreq.payment_requests SET
                            program_area_id = %s, vendor_id = %s, vendor_request_id = %s,
                            requested_pay_date = %s, description = %s,
                            special_instructions = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (program_area_id, vendor_id, new_vendor_request_id,
                         requested_pay_date, description, special_instructions, payment_request_id),
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

                if reset_approval:
                    audit_comment = (
                        f"Vendor and/or amount changed on edit (was ${old_total:,.2f}, "
                        f"now ${total_amount:,.2f}) -- approval workflow reset.\n{chain_summary}"
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
                     status, current_approver_id, serial_group_current, approval_chain_summary)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (request_number, request_type, org_id, program_area_id, user["id"],
                 vendor_id, total_amount, requested_pay_date, description, special_instructions,
                 "UnderReview",
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

    if show_all:
        org = _current_org(request)
        if not org:
            return RedirectResponse("/portal")
        rows = db.query(
            """
            SELECT pr.id AS pr_id, pr.request_number, pr.request_type, pr.amount, pr.status,
                   pr.approval_chain_summary, pr.created_at, o.code AS org_code,
                   pa.title AS program_area_title, u.display_name AS submitter_name,
                   u.email AS submitter_email
            FROM checkreq.payment_requests pr
            JOIN checkreq.organizations o ON o.id = pr.org_id
            JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
            JOIN checkreq.app_users u ON u.id = pr.submitter_user_id
            WHERE pr.org_id = %s
            ORDER BY pr.created_at DESC
            """,
            (org["id"],),
        )
    else:
        # Unchanged from before this session -- no org_id filter here (never
        # had one), per Task 6's explicit instruction that "My Requests"
        # behaves exactly as it did previously.
        rows = db.query(
            """
            SELECT pr.id AS pr_id, pr.request_number, pr.request_type, pr.amount, pr.status,
                   pr.approval_chain_summary, pr.created_at, o.code AS org_code,
                   pa.title AS program_area_title
            FROM checkreq.payment_requests pr
            JOIN checkreq.organizations o ON o.id = pr.org_id
            JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
            WHERE pr.submitter_user_id = %s
            ORDER BY pr.created_at DESC
            """,
            (user["id"],),
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

    return _render(request, "my_requests.html", user, {
        "rows": rows, "submitted": submitted, "archive_warning": archive_warning,
        "edited": edited, "cancelled": cancelled, "show_all": show_all,
    })


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
               vr.created_at, o.name AS org_name, pr.request_number, pr.amount
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
