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
_complete_login() gate called by every provider's callback route — this is
the structural fix for the exact class of bug that created Jay's own blank
jboggs@episcopalmaryland.org duplicate profile on 2026-07-17/18.

EXTRACTED 2026-08-07 (Jay: "the login programming should be a separate
program file, shouldn't it?" — main.py was already flagged in Parish Portal
Plan.md Section 1 as "too big, do not feed it"): every /login, /auth/*, and
/logout route, plus _complete_login() and its helpers, now live in
auth_routes.py — this file only does `app.include_router(
auth_routes.create_router(templates))`. Same session added the third and
fourth identity flavors there (Multi-Provider Authentication Plan Addendum
2026-08-06): an emailed one-time code (auth_code.py) and an opt-in
Beacon-managed password (auth_password.py) — the universal fallback for
anyone whose email domain isn't Microsoft or Google, e.g. parish volunteers
on Outlook.com/iCloud/small-custom-domain addresses with no IT department.
Login page is now a single Front-style screen (Jay's explicit reference)
showing every option at once — password, email-code, Microsoft, Google — no
domain-based redirect first; identity_provider_domains stays in the schema
but no longer gates which buttons render. **Standing rule from this same
session: no new functionality goes into this file without Jay's explicit
consent — propose the module split first.**

RECONCILED 2026-08-07 (same session): this extraction was originally built
against a working-folder copy of main.py that had fallen ~5 days and several
features behind the actual deployed `main` branch (Program Areas/Approval
Rules admin, ART completeness, in-app notifications, conversational feedback
intake, full-screen support, Invoice Intake Tier 3). Root cause, per Jay: a
session on his home desktop coded those features directly into the
C:/GITRepos/checkreq-website checkout instead of the OneDrive working
folder -- not an OneDrive sync lag, there was simply nothing in the working
folder for OneDrive to sync. Re-applied the same auth extraction directly
onto the real current main.py rather than overwriting it with the stale
copy — confirmed via diff that the login/auth block itself was
byte-identical between the stale base and the current file, so nothing about
those other features was touched or at risk.
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
import rbac
import parish_roles
import parish_mode
import parish_documents
import cornerstone_documents
import parish_info
import parish_finance
import tile_badges
import announcements
import parish_requests
import parish_org_admin
import cornerstone_mode
# Needed by _render()'s synthetic "timekeeping_reviewer" gate (2026-08-17) --
# main.py had never imported this directly before; admin_hub.py was the only
# consumer. A missing import here is a runtime NameError, not a compile error,
# so it is worth stating why it exists.
import org_features
import timekeeping
import timekeeping_activation
import timekeeping_roster
import timekeeping_employees
import timekeeping_entries
import timekeeping_review
import timekeeping_status
import approval_engine
import auth_routes
import gcs_client
import sharepoint_client
import document_extract
import email_client
import qbo_mcp_client
import app_settings
import notifications
import art_preapproval

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

# Dev/Prod Split Plan.md (2026-07-31): distinguishes today's only real
# environment (checkreq-website-dev) from the future production service
# (checkreq-website, BEACON_ENV=prod set explicitly at deploy time). Defaults
# to "dev" when unset -- the safe direction, since a misconfigured deploy
# then still shows every dev safeguard (the QBO-posting confirmation, the
# red banner) rather than silently behaving like production with none of
# them. See email_client.py's _apply_test_mode() for the matching prod-only
# Test Mode hard lock, and the post_to_qbo route below for the QBO-posting
# confirmation this flag gates.
BEACON_ENV = os.environ.get("BEACON_ENV", "dev")

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
templates.env.globals["BEACON_ENV"] = BEACON_ENV

if os.environ.get("ENABLE_DEV_AUTH_BYPASS") == "1":
    print("*** DEV AUTH BYPASS ENABLED — LOCAL ONLY — /dev/auth-as/{email} is live ***")

# Every /login, /auth/*, and /logout route lives in auth_routes.py (pulled
# out of this file 2026-08-07 -- see that module's docstring). Thin wiring
# only, per the standing rule against feeding new functionality into
# main.py directly.
app.include_router(auth_routes.create_router(templates))


@app.get("/how-it-works", response_class=HTMLResponse)
def how_it_works_page(request: Request):
    """Rendered replacement for the old docx-only "About Beacon" footer link
    (2026-08-02) -- rebuilt from Beacon - Overview.docx's own content so
    staff get a page that actually renders in a browser, with the docx still
    linked at the bottom for anyone who wants a printable copy. Public, no
    login required -- matches the footer link's own placement outside the
    `{% if user %}` header block in base.html, and login.html itself follows
    the identical no-`user`-in-context render pattern just below."""
    return templates.TemplateResponse(request, "how_it_works.html", {})


# Portal module tiles. A plain list is enough for this scope (6 tiles) --
# revisit as a real "modules" config table if this grows much past that.
# "gate" (AP Review Workflow Plan.md, 2026-07-26): None = visible to every
# logged-in user; "ap_reviewer" = only shown when the current user has
# is_ap_reviewer=TRUE (checked in portal.html, since MODULES itself is a
# module-level constant shared by every request, not per-user).
MODULES = [
    {"key": "check_request", "title": "Check Request", "desc": "Submit a classic check request.", "url": "/new-request", "enabled": True, "gate": None},
    {"key": "my_requests", "title": "My Requests", "desc": "Track requests you've submitted.", "url": "/my-requests", "enabled": True, "gate": None},
    # Flipped enabled 2026-08-02 (Invoice Processing Intake Plan.md, Tier 3) --
    # was a disabled "Coming Soon" placeholder. Same generic `m.gate in
    # entity_roles` mechanism every other gated tile already uses -- the
    # invoice_intake_submitter role is granted via the existing Users &
    # Roles "grant a role" UI, same as pre_approved_submitter, no new admin
    # screen needed.
    {"key": "invoice_payment", "title": "Invoice for Payment", "desc": "Upload and code an invoice.", "url": "/invoice-intake", "enabled": True, "gate": "invoice_intake_submitter"},
    # Replaced 2026-08-02 (feedback batch, Item 1) -- was a disabled "Vendor
    # Requests" placeholder; this is now the single consolidated entry point
    # for every admin-ish screen, gated on a synthetic pseudo-role
    # (_render() adds "administrative_tasks" to entity_roles whenever the
    # real role set intersects ADMIN_TASK_ROLE_KEYS) so this tile uses the
    # exact same generic `m.gate in entity_roles` check as every other tile
    # -- no template special-casing needed.
    {"key": "administrative_tasks", "title": "Administrative Tasks", "desc": "Setup tables, user roles, vendor approvals, and system administration.", "url": "/admin", "enabled": True, "gate": "administrative_tasks"},
    # Flipped enabled 2026-07-26 (was a disabled "Coming Soon" placeholder) --
    # AP Review Workflow Plan.md Section 2a: visible to every logged-in user,
    # same as My Requests -- an empty queue is a harmless empty state, not
    # worth its own permission gate.
    {"key": "approval_queue", "title": "Approval Queue", "desc": "Review requests awaiting your approval.", "url": "/my-approvals", "enabled": True, "gate": None},
    {"key": "ap_review", "title": "AP Review", "desc": "Final review and QBO posting for fully-approved requests.", "url": "/admin/ap-review", "enabled": True, "gate": "ap_reviewer"},
    # Parish Portal S3 (2026-08-08), consolidated same day per Jay's direct
    # feedback ("Request Access replaces Request Parish Access and Parish
    # Access Requests -- the sub-screen should do all of this, depending on
    # your RBAC"): ONE tile, gate=None so a parish volunteer with zero
    # checkreq.roles grants can still reach it -- the page itself
    # (parish_access.py) shows the submit form to everyone and additionally
    # shows the review queue inline for anyone who qualifies as a reviewer
    # (beacon_admin, or parish_admin at the relevant parish). The old
    # separate "Parish Access Requests" tile/gate is gone; /admin/
    # parish-access-requests now just redirects here.
    {"key": "request_parish_access", "title": "Request Access", "desc": "Ask for access to a parish, or review pending requests if you're a reviewer.", "url": "/parish-access-request", "enabled": True, "gate": None},
    # Parish Portal S4+S5 (2026-08-08): same "parish_reviewer" synthetic
    # pseudo-role as the tile above -- a pure Parish Admin (no
    # checkreq.roles grant at all) needs to reach this without the
    # Administrative Tasks hub's own top-level gate blocking them.
    {"key": "parish_requests_review", "title": "Parish Requests", "desc": "Review parish feedback and general requests.", "url": "/admin/parish-requests", "enabled": True, "gate": "parish_reviewer"},
    # 2026-08-17, Jay: "I think there needs to be a main menu items on the
    # Diocese for Time Review and Time Status, and appear according to the
    # personal RBAC." Moved OUT of the Administrative Tasks HR group (which
    # keeps only the four configuration cards: HR Activation, Employees,
    # Payroll Periods, Time Categories) because these two are day-to-day
    # operational queues, not setup.
    #
    # Gated on the synthetic "timekeeping_reviewer" pseudo-role injected in
    # _render() below -- the same trick administrative_tasks/parish_reviewer
    # already use. It has to be synthetic because a tile's `gate` is a single
    # role key, while the real condition is TWO things at once: the diocese
    # has the timekeeping feature flag on AND the person holds hr_admin or
    # beacon_admin there. Encoding that in one place keeps the tile check as
    # the same generic `m.gate in entity_roles` as every other tile.
    #
    # Deliberately absent from CORNERSTONE_MODULE_KEYS: these are diocese-wide
    # review screens and their routes reject a Cornerstone-Mode-selected entity
    # outright. This is also NOT the Parish Mode timekeeping grid -- Jay:
    # "This is different from Parish Mode Timekeeping."
    {"key": "time_review", "title": "Time Review", "desc": "Approve roster changes and see submitted hours awaiting review.", "url": "/admin/timekeeping/review", "enabled": True, "gate": "timekeeping_reviewer"},
    {"key": "time_status", "title": "Time Status", "desc": "Track each parish's submissions, open or close a period, enter missing hours, and export.", "url": "/admin/timekeeping/status", "enabled": True, "gate": "timekeeping_reviewer"},
]

# The union of roles that unlock the "Administrative Tasks" tile/hub (2026-08-02
# feedback batch, Item 1) -- deliberately excludes ap_reviewer, which keeps its
# own separate portal tile and was never part of the consolidated 8.
ADMIN_TASK_ROLE_KEYS = {"cfo", "setup_admin", "beacon_admin", "vendor_approver", "hr_admin"}

# Cornerstone Served Parishes Phase B (2026-08-16) -- the tile subset shown
# on /portal when the current entity is a served parish-org, per Jay's own
# enumeration (Cornerstone Served Parishes Plan.md, Phase B). Deliberately
# a small, explicit set rather than "everything except X" -- easiest to
# extend correctly if the parish-side menu (still unscoped, "we'll walk
# through those together") ends up needing more CFM-staff-side tiles too.
# Extended same day, live test: Jay's full enumeration also named "AP
# Review" -- already a real MODULES tile, just missed from the first cut.
CORNERSTONE_MODULE_KEYS = {
    "check_request", "my_requests", "invoice_payment", "ap_review",
    "approval_queue", "administrative_tasks", "parish_requests_review",
}

# 2026-08-16, same live test: "Document Library, and Resources would show
# [under Cornerstone Mode] -- correct?" These are NOT part of the shared
# MODULES list above (they're parish-scoped concepts -- showing them on the
# plain, no-specific-parish diocesan dashboard wouldn't make sense) --
# Cornerstone-Mode-only, appended in the /portal route below. Reuse the
# existing parish-facing routes (parish_documents.py), bridged there to
# resolve the CURRENT served parish-org's own linked parish when no Parish
# Mode preview is active (see parish_documents._parish_context).
CORNERSTONE_ONLY_MODULES = [
    {"key": "parish_document_library", "title": "Document Library",
     "desc": "View and manage this parish's documents.", "url": "/parish-documents",
     "enabled": True, "gate": None},
    {"key": "parish_resource_library", "title": "Resources",
     "desc": "Diocese-wide reference library.", "url": "/resource-library",
     "enabled": True, "gate": None},
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
    # RBAC (2026-08-01): was `real["is_cfo"]`. Same cross-entity semantics
    # (org_id=None) -- an EDOM-only CFO could impersonate before RBAC too.
    if imp_id and rbac.user_has_role(real["id"], "cfo", org_id=None):
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
    Selects every column (not a fixed subset) since callers across this
    app -- the attachment-archival flow (sp_hostname/sp_site_path/
    sp_library_folder), parish_documents.py's DioNet AND Cornerstone areas
    (sp_parish_hostname/sp_parish_site_path/sp_parish_library_folder/
    sp_resource_library_folder), cornerstone_mode.py, etc. -- each need a
    different, growing slice of this row. A narrower hand-picked column
    list here silently KeyErrors the moment a caller reads a column that
    list doesn't happen to include (real incident, 2026-08-16:
    admin_parish_documents_page crashed on org["sp_parish_hostname"] since
    this SELECT never listed it)."""
    org_id = request.session.get("current_org_id")
    if not org_id:
        return None
    return db.query_one(
        "SELECT * FROM checkreq.organizations WHERE id = %s AND is_active",
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


def _roles(request: Request, user: dict | None, org_id: int | None) -> set[str]:
    """Per-request-cached role set (Role-Based Access Control Plan.md §3.1)
    -- resolved once and stashed on request.state, since _render() and
    several guards can all ask for the same (user, org) pair within one
    request. Cache key includes org_id so `roles` (current org) and
    `real_roles` (cross-entity, org_id=None) never collide."""
    if not user:
        return set()
    key = f"_roles_{user['id']}_{org_id}"
    if not hasattr(request.state, key):
        setattr(request.state, key, rbac.get_role_keys(user["id"], org_id))
    return getattr(request.state, key)


def _render(request: Request, template: str, user: dict, extra: dict | None = None):
    """Renders a template that extends base.html, always including the
    header's user/entity-switcher context so every page shows it
    consistently -- not just /portal. Also always includes real_user +
    impersonating, since the impersonation banner/nav-link must gate on the
    REAL identity, not whichever identity `user` currently resolves to.

    RBAC (2026-08-01, revised 2026-08-16): also injects `roles` (an alias of
    `entity_roles`, kept for template back-compat), `entity_roles` (the
    CURRENT/impersonated identity's role keys, scoped to the current
    entity), `real_roles` (the REAL identity's, cross-entity -- used only
    for things inherently about the real session itself, e.g. the
    impersonation banner's own re-check), and `effective_roles` (the
    CURRENTLY EFFECTIVE identity's, cross-entity -- the impersonated
    persona's own roles while impersonating, same as real_roles otherwise;
    matches base.html's Parish/Cornerstone Mode toggle and parish_mode.py's
    `_require_effective_cfo`, changed 2026-08-16 per Jay's direct feedback
    that the real admin's own permissions shouldn't "leak into" what an
    impersonated view shows).

    2026-08-16: `entity_roles` replaces the old `any_org_roles` (was
    cross-entity, "holds this role ANYWHERE"). Jay's explicit direction:
    every role check becomes strictly entity-scoped, no exceptions --
    including nav-tile visibility, not just data access. This also closes a
    real bug the old design had baked in: `org_id = org["id"] if org else
    None` fed straight into a role-check function where `org_id=None` means
    "check every org" -- so whenever no entity happened to be selected
    (never set at login; cleared on impersonation start/stop), nav
    visibility silently reverted to the exact cross-entity behavior this
    fix exists to remove. Never let that happen again: no selected entity
    means no roles, full stop -- explicitly, not by accident of a None
    falling through."""
    real = _real_user(request)
    org = _current_org(request)
    org_id = org["id"] if org else None
    entity_roles = set() if org_id is None else _roles(request, user, org_id)
    # 2026-08-02 feedback batch, Item 1: synthetic pseudo-role so the
    # Administrative Tasks tile uses the same generic `m.gate in
    # entity_roles` check every other portal tile already uses, instead of
    # a one-off template special-case.
    if entity_roles & ADMIN_TASK_ROLE_KEYS:
        entity_roles = entity_roles | {"administrative_tasks"}
    # Parish Portal S3 (2026-08-08): same synthetic-pseudo-role trick as
    # administrative_tasks above, so the Parish Access Requests tile uses
    # the identical generic `m.gate in entity_roles` check -- a beacon_admin
    # AT THE CURRENT ENTITY (2026-08-16: was "anywhere") OR anyone holding
    # parish_admin for at least one parish (a separate grant system,
    # portal.parish_user_roles, entirely independent of checkreq.roles/
    # entity_roles -- deliberately NOT entity-scoped here, since a parish
    # doesn't belong to "the current entity" the same way a checkreq role
    # does) both count as a reviewer.
    if "beacon_admin" in entity_roles or parish_roles.get_parish_ids_with_role(user["id"], "parish_admin"):
        entity_roles = entity_roles | {"parish_reviewer"}
    # 2026-08-17: synthetic gate for the two new diocese Time Review / Time
    # Status tiles. BOTH conditions must hold, which is why this can't be a
    # plain role gate: the current entity must have Timekeeping turned on
    # (checkreq.org_features) AND the person must hold hr_admin or
    # beacon_admin there. org_features.is_enabled() returns False for a NULL
    # org and for a served parish-org (the flag lives on the diocese row), so
    # this also keeps the tiles out of Cornerstone Mode without a second check.
    if org_id is not None and entity_roles & {"hr_admin", "beacon_admin"}             and org_features.is_enabled(org_id, "timekeeping"):
        entity_roles = entity_roles | {"timekeeping_reviewer"}
    _parish_view, _parish_view_is_preview = parish_mode.effective_parish_mode(request, user)
    # Cornerstone Served Parishes Phase B (2026-08-16): True whenever the
    # currently-selected entity is a served parish-org -- drives the
    # dark-blue theme + user-menu indicator in base.html. No separate
    # session state to track (see cornerstone_mode.py's own docstring) --
    # this is just a live check against whichever org is currently selected.
    cornerstone_context = bool(org and cornerstone_mode.is_cornerstone_org(org["id"]))
    ctx = {
        "user": user,
        "real_user": real,
        "cornerstone_context": cornerstone_context,
        "impersonating": bool(request.session.get("impersonating_user_id"))
                          and bool(real and rbac.user_has_role(real["id"], "cfo", org_id=None)),
        "current_org": org,
        # 2026-08-16 fix: used to list every active org regardless of the
        # signed-in user's actual access (confirmed live: Caroline Bomgardner
        # saw all 4 entities though she should only reach EDOM). Filtered to
        # orgs _user_has_org_access() actually grants -- checkreq.roles OR
        # checkreq.user_program_areas, no exceptions, matching the same
        # explicit-grant-required rule select_entity now enforces. Same-day
        # follow-up (Jay, live test): also excludes Cornerstone-served
        # parish-orgs -- "when I am in Diocese Mode, I should only see the
        # Dioceses in the Entity picker" -- see _accessible_diocese_orgs().
        "all_orgs": _accessible_diocese_orgs(user["id"]),
        "roles": entity_roles,  # back-compat alias, same value as entity_roles
        "entity_roles": entity_roles,
        "real_roles": _roles(request, real, None),
        # 2026-08-16, Jay: "aren't you trying to impersonate ALL of that
        # user? How can you diagnose security/permission issues when you
        # are impersonating a user if your permissions are mixed into it?"
        # -- the CURRENTLY EFFECTIVE identity's cross-entity roles: the
        # impersonated persona's own roles while impersonating, or the same
        # as real_roles otherwise (user == real when not impersonating).
        # Distinct from real_roles, which is deliberately ALWAYS the real
        # admin (used only for things that are inherently about the real
        # session itself, e.g. the impersonation banner's own fail-closed
        # re-check above -- not for "what can this identity do" UI).
        "effective_roles": _roles(request, user, None),
        # In-App Notifications (2026-08-02): live-as-of-this-page-load
        # unread count for the header bell -- no push/websocket, just
        # recomputed on every render, per the plan's own design.
        "unread_notification_count": notifications.get_unread_count(user["id"]),
        # Parish Mode (S4, 2026-08-08) -- fail-closed live re-check and
        # native-vs-preview logic all live in parish_mode.py itself; this
        # is just the thin wiring every template needs, both for the
        # persistent Exit banner (only on a CFO's explicit preview) and for
        # the dark-red body.parish-mode theme (applies either way).
        "parish_view": _parish_view,
        "parish_mode_preview": _parish_view_is_preview,
        # Portal tile badges (2026-08-08) -- computed on every render, same
        # as unread_notification_count above; a tile with nothing open
        # simply has no key in this dict (portal.html renders no badge).
        "tile_badges": tile_badges.get_badges(user["id"], org_id),
    }
    if extra:
        ctx.update(extra)
    return templates.TemplateResponse(request, template, ctx)


@app.get("/health")
def health():
    return {"status": "ok"}




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

    # RBAC (2026-08-01, Plan §9): a user who authenticated successfully but
    # holds no live role anywhere, and has no user_program_areas assignment
    # either, does not get an (empty) portal -- they get the blank
    # Request Access screen instead. Extended 2026-08-08 (Parish Portal S3):
    # a pure Parish Admin/parish_member/etc -- someone with a real
    # portal.parish_user_roles grant but ZERO checkreq.roles grants, e.g. a
    # parish volunteer with no diocesan footprint at all -- is NOT roleless;
    # they get the real portal (My Requests/Approval Queue will be
    # empty-but-harmless for them, same as any other low-permission user,
    # but Request/Review Parish Access must be reachable).
    if (not rbac.user_has_any_role(user["id"])
            and not db.query_one("SELECT 1 FROM checkreq.user_program_areas WHERE user_id = %s", (user["id"],))
            and not parish_roles.get_parish_role_keys(user["id"])):
        return RedirectResponse("/access-request")

    # Parish Portal S4 correction (2026-08-08, Jay, first real test login):
    # a user whose ONLY roles are parish-scoped has no diocesan "entity" to
    # work in at all -- send them straight to their own parish's page
    # instead of the diocesan portal (entity picker and all). Also covers a
    # CFO's explicit /admin/parish-mode preview. parish_mode.effective_
    # parish_mode() returning (None, False) for a native multi-parish user
    # with no choice made yet is STILL a reason to redirect here --
    # /parish-view itself renders their own small picker in that case.
    parish, _ = parish_mode.effective_parish_mode(request, user)
    if parish or (not rbac.user_has_any_role(user["id"]) and parish_roles.get_parish_role_keys(user["id"])):
        return RedirectResponse("/parish-view")

    # Cornerstone Served Parishes Phase B (2026-08-16): a curated tile list
    # when the currently-selected entity is a served parish-org, per Jay's
    # own enumeration (Check Request/My Requests/Invoice for Payment/
    # Approval Queue/Administrative Tasks/Parish Requests -- Feedback is
    # already an always-visible nav link, not a tile; "Documents" is
    # deliberately not included yet, flagged as needing its own design pass
    # since it bridges two different data models, portal.parishes-level
    # docs vs. this checkreq.organizations-level AP entity).
    org = _current_org(request)
    # 2026-08-16, Jay: "when a user logs in and only has one entity assigned
    # to them, you can skip past the entity chooser and go straight to that
    # entity." Only ever auto-picks among real dioceses (never a
    # Cornerstone-served parish-org -- those are reached exclusively via the
    # Cornerstone Mode picker, never assumed).
    if not org:
        dioceses = _accessible_diocese_orgs(user["id"])
        if len(dioceses) == 1:
            return RedirectResponse(f"/select-entity/{dioceses[0]['id']}", status_code=303)

    modules = MODULES
    if org and cornerstone_mode.is_cornerstone_org(org["id"]):
        modules = [m for m in MODULES if m["key"] in CORNERSTONE_MODULE_KEYS] + CORNERSTONE_ONLY_MODULES

    return _render(request, "portal.html", user, {"modules": modules})


def _user_has_org_access(user_id: int, org_id: int) -> bool:
    """Does this user have ANY real reason to be in this org's session
    context -- a live checkreq.roles grant there, or a checkreq.
    user_program_areas assignment to one of its program areas. No
    exceptions for beacon_admin or any other role -- per Jay's explicit
    2026-08-16 direction (Cornerstone Served Parishes Plan.md, decision 5's
    follow-up question), an org is an org: access always requires an
    explicit grant AT that org, never inherited from a role held
    elsewhere. 2026-08-16 fix: `/select-entity/{org_id}` used to have NO
    check at all beyond "does this org exist" -- any signed-in user could
    URL-switch into any org regardless of the entity-switcher dropdown
    being filtered (see the all_orgs fix in `_render()`'s context)."""
    if rbac.get_role_keys(user_id, org_id):
        return True
    row = db.query_one(
        "SELECT 1 FROM checkreq.user_program_areas upa "
        "JOIN checkreq.program_areas pa ON pa.id = upa.program_area_id "
        "WHERE upa.user_id = %s AND pa.org_id = %s LIMIT 1",
        (user_id, org_id),
    )
    return row is not None


def _accessible_diocese_orgs(user_id: int) -> list[dict]:
    """Every top-level diocese/entity this user can access -- deliberately
    EXCLUDES Cornerstone-served parish-orgs. Jay, 2026-08-16, live testing:
    "when I am in Diocese Mode, I should only see the Dioceses in the Entity
    picker. Right now you show them all, even parishes that are Cornerstone
    served" -- a served parish-org is a real checkreq.organizations row (so
    _user_has_org_access() alone can't tell it apart from a diocese), but
    it's meant to be reached ONLY through the Cornerstone Mode picker, never
    the plain entity switcher -- otherwise the two pickers show overlapping,
    indistinguishable lists (EDOM/Claggett next to a dozen parish codes)."""
    return [o for o in db.query(
        "SELECT id, code, name FROM checkreq.organizations WHERE is_active ORDER BY name"
    ) if _user_has_org_access(user_id, o["id"]) and not cornerstone_mode.is_cornerstone_org(o["id"])]


@app.get("/select-entity/{org_id}")
def select_entity(org_id: int, request: Request, next: str = "/portal"):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    org = db.query_one(
        "SELECT id FROM checkreq.organizations WHERE id = %s AND is_active", (org_id,)
    )
    if not org or not _user_has_org_access(user["id"], org_id):
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
    # RBAC (2026-08-01): cross-entity by design, same as today -- an
    # EDOM-only CFO could impersonate before RBAC too (is_cfo was global).
    if not rbac.user_has_role(real["id"], "cfo", org_id=None):
        return JSONResponse({"error": "CFO access required"}, status_code=403)

    users = db.query(
        "SELECT id, email, display_name FROM checkreq.app_users "
        "WHERE is_active AND id != %s ORDER BY display_name",
        (real["id"],),
    )
    for u in users:
        u["roles"] = rbac.get_roles_for_user(u["id"])
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
    if not rbac.user_has_role(real["id"], "cfo", org_id=None):
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


@app.get("/new-request-easy", response_class=HTMLResponse)
def new_request_easy_form(request: Request):
    """Phase D (Cornerstone Served Parishes Plan.md): an alternate, simpler
    New Request layout for a brand-new check_request submission only -- no
    edit_data support (editing an existing request still uses the classic
    /requests/{request_number}/edit page). Posts to the same /new-request
    route as the classic form; new_request_submit's own ui_variant=easy
    branch (see that route) is the only difference in what happens after
    submit."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    org = _current_org(request)
    if not org:
        return RedirectResponse("/portal")
    return _render(request, "new_request_easy.html", user, {"today": date.today().isoformat()})


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
    # Invoice Intake's Draft queue is a shared team inbox -- any authorized
    # Invoice Intake staff member may open and finish coding a Draft, not
    # just whoever originally uploaded it. Matches the identical widened
    # check in new_request_submit's POST handler (never trust that this
    # GET route's own check is the only gate -- see that route's comment).
    if pr["request_type"] == "invoice_payment" and pr["status"] == "Draft":
        if not rbac.user_has_role(user["id"], "invoice_intake_submitter", pr["org_id"]):
            return JSONResponse({"error": "Not authorized to code Invoice Intake requests"}, status_code=403)
    elif pr["submitter_user_id"] != user["id"]:
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
        "request_type": pr["request_type"],
        "status": pr["status"],
        "program_area_id": pr["program_area_id"],
        "requested_pay_date": pr["requested_pay_date"].isoformat() if pr["requested_pay_date"] else None,
        "description": pr["description"] or "",
        "special_instructions": pr["special_instructions"] or "",
        "gl_lines": [
            {"gl_account_id": g["gl_account_id"], "amount": float(g["amount"]), "memo": g["memo"] or "",
             "account_number": g["account_number"], "account_name": g["account_name"]}
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

    if rbac.user_has_role(user["id"], "cfo", org_id):
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


def _user_can_submit_for(user: dict, program_area_id: int, org_id: int) -> bool:
    """Access-control gate: CFO bypasses; everyone else needs an explicit
    checkreq.user_program_areas assignment. No silent fallback — an
    unassigned user gets a clear rejection, not quiet access.

    RBAC (2026-08-01): org_id is now required -- was `user["is_cfo"]` with
    no entity scoping at all, meaning an EDOM-only CFO silently bypassed
    Claggett's program-area scoping too. Every real caller already has
    org_id in hand (see Role-Based Access Control Plan.md §5.3 #13)."""
    if rbac.user_has_role(user["id"], "cfo", org_id):
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


def _send_budget_buffer_notice_email(request_number: str, org_name: str, org_id: int, details: list[str]) -> None:
    """Tier 2 (over budget, within the account's allowed buffer) -- FYI
    only, no action needed, sent to every CFO of THIS request's own entity
    (Jay's plan: "the CFO is notified... no approval needed"). Fails soft,
    matching every other notification in this app -- one CFO's bad email
    address must never crash the submission that already succeeded by the
    time this runs.

    RBAC (2026-08-01): was `SELECT ... WHERE is_cfo = TRUE`, no org filter --
    a real bug (Role-Based Access Control Plan.md §1.2/§5.4 #16): a Claggett
    tier-2 overage was emailing EDOM's CFO too. org_id scopes this to the
    request's own entity."""
    cfos = rbac.get_users_with_role("cfo", org_id)
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
    # In-App Notifications Plan.md: "whatever code path fires the CFO
    # budget-overage email also inserts one notifications row per is_cfo
    # user at the same time -- one shared helper function, not two
    # separate places to keep in sync." This function IS that shared call
    # site (both the new-submission and edit-triggered-reset branches of
    # new_request_submit call it) -- the notification is created right
    # alongside the email, not from a second, parallel code path.
    notif_message = (
        f"{request_number} ({org_name}) was submitted over budget, within the "
        f"account's allowed buffer. No action needed."
    )
    notif_link = f"/requests/{request_number}/view"
    for c in cfos:
        try:
            email_client.send_email(
                to=c["email"], subject=subject, body_html=body_html, body_text=body_text,
                sender=W9_SENDER_EMAIL,
            )
        except Exception as exc:
            print(f"[budget-buffer-notice] failed for {c['email']}: {exc}")
        notifications.create_notification(c["id"], "budget_overage", notif_message, notif_link)


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
               COALESCE(pa.title, 'All Program Areas') AS program_area_title,
               u.display_name AS submitter_name, u.email AS submitter_email,
               v.display_name AS vendor_display_name,
               vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
               vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
               vr.dba_name AS vr_dba_name
        FROM checkreq.payment_requests pr
        JOIN checkreq.organizations o ON o.id = pr.org_id
        LEFT JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
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


def _cfo_approver_rows(exclude_user_id: int, org_id: int) -> list[dict]:
    """Every CFO of THIS entity except the given one -- shared by the
    self-payment chain and the tier-3 budget-overage CFO step. Excluding the
    acting submitter even if they happen to hold the cfo role is deliberate
    in both call sites: letting someone approve a step whose entire purpose
    is a check on their OWN submission would defeat the point of the rule; a
    genuinely separate person must act.

    RBAC (2026-08-01): was `SELECT ... WHERE is_cfo = TRUE`, no org filter --
    a real bug (Role-Based Access Control Plan.md §1.2/§5.4 #17): a Claggett
    self-payment was appending EDOM's CFO to a real approval chain. org_id
    scopes this to the request's own entity -- the highest-impact single fix
    in that plan."""
    return [u for u in rbac.get_users_with_role("cfo", org_id) if u["id"] != exclude_user_id]


def _self_payment_cfo_chain(submitter_user_id: int, org_id: int) -> list[dict]:
    """Self-payment always requires CFO approval, any ONE of them, regardless
    of amount or the submitter's own authorization -- bypasses the normal
    Program-Area chain and the entity global-approver step entirely (Jay:
    "that always requires the CFO's approval, regardless of their
    authorization level or the dollar amount")."""
    return [
        {"serial_group": 1, "approver_user_id": u["id"], "approver_email": u["email"],
         "approver_name": u["display_name"], "backup_approver_id": None,
         "any_one_suffices": True}
        for u in _cfo_approver_rows(submitter_user_id, org_id)
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
    # RBAC (2026-08-01): cross-entity for now, matching today's behavior
    # exactly (is_ap_reviewer was a flat global flag) -- Role-Based Access
    # Control Plan.md §8 q2 flags entity-scoping this as a real, deliberately
    # deferred follow-up once Claggett has its own AP clerk.
    if not rbac.user_has_role(user["id"], "ap_reviewer", org_id=None):
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
def api_gl_accounts(org_id: int, request: Request, program_area_id: int | None = None, q: str = ""):
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
    dot-notation ordering, same malformed-sort_order safety guard.

    program_area_id is now OPTIONAL (2026-08-02, Invoice Processing Intake
    Plan.md Tier 3) -- "Program Area defaults to All" for Invoice Intake
    means there's no program_area_gl_accounts mapping row to join against
    at all when none is chosen. Falls back to the plain active chart of
    accounts, unfiltered/uncurated, in raw account-number order -- this is
    a real, deliberate degradation (no display_text override, no curated
    sort_order) versus the curated per-program-area view, not a bug; the
    Check Request form always supplies a real program_area_id and is
    completely unaffected by this branch."""
    if not _current_user(request):
        return JSONResponse({"error": "Not signed in"}, status_code=401)

    if program_area_id is None:
        base_sql = "SELECT id, account_number, account_name, NULL AS sort_order " \
                    "FROM checkreq.gl_accounts WHERE org_id = %s AND is_active"
        order_sql = " ORDER BY account_number LIMIT 50"
        if q:
            return db.query(
                base_sql + " AND (account_number ILIKE %s OR account_name ILIKE %s) " + order_sql,
                (org_id, f"%{q}%", f"%{q}%"),
            )
        return db.query(base_sql + order_sql, (org_id,))

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


@app.get("/api/vendor-preapproval-status")
def api_vendor_preapproval_status(request: Request, vendor_id: int = 0):
    """Live-preview endpoint for the Invoice Intake coding screen's ART/
    Monkey-See-Monkey-Do status banner -- same "just exposes what
    new_request_submit already computes" pattern as
    /api/approval-chain-preview and /api/budget-status. Returns
    {"has_art": false} when there's nothing to show (no vendor selected, or
    no active ART entry) rather than an error, matching this codebase's
    established soft-error convention for live-preview endpoints."""
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not signed in"}, status_code=401)
    org = _current_org(request)
    if not org:
        return {"has_art": False}
    if not vendor_id:
        return {"has_art": False}

    status = art_preapproval.vendor_preapproval_status(vendor_id, org["id"])
    if not status:
        return {"has_art": False}
    return {"has_art": True, **status}


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
        SELECT pr.*, o.name AS org_name, COALESCE(pa.title, 'All Program Areas') AS program_area_title,
               v.display_name AS vendor_name,
               vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
               vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
               vr.dba_name AS vr_dba_name, vr.status AS vr_status,
               u.display_name AS submitter_name, u.email AS submitter_email
        FROM checkreq.payment_requests pr
        JOIN checkreq.organizations o ON o.id = pr.org_id
        LEFT JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
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
        "voucher_pre_approved": bool(pr.get("pre_approved")),
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


def _archive_one_file(org: dict, payment_request_id: int, request_number: str, source: str,
                       filename: str, content_type: str, data: bytes, user_id: int) -> dict:
    """One more archived file for an already-existing request -- shared by
    add_attachment() (the Edit page's "Add" button, one call per file) and
    Invoice Intake (Tier 3, 2026-08-02: one call for the raw invoice at
    intake, one for the generated voucher PDF at finalize -- two separate
    archival moments over one request's life, which _archive_attachments()
    doesn't support, since it always assumes exactly one archival moment
    and starts its own index at 1).

    Extracted from add_attachment's own per-file logic (2026-07-26), which
    already solved the real indexing problem: the next archived-filename
    index must be unique across every attachment EVER created for this
    request (not just currently-active ones), since a removed row's index
    could otherwise be reused and silently overwrite that row's still-
    intact SharePoint file (upload_bytes() overwrites unconditionally on a
    filename collision, by design).

    Raises on any failure (GCS or SharePoint) -- caller decides whether
    that's fatal or a soft, per-file error to collect, matching
    add_attachment's own existing behavior. Returns the inserted
    checkreq.payment_request_attachments row's own fields as a dict."""
    if not (org.get("sp_hostname") and org.get("sp_site_path") and org.get("sp_library_folder")):
        raise RuntimeError(
            f"No SharePoint archive location configured for {org['name']} -- "
            f"set sp_hostname/sp_site_path/sp_library_folder in checkreq.organizations."
        )

    pr = db.query_one(
        "SELECT requested_pay_date, amount FROM checkreq.payment_requests WHERE id = %s",
        (payment_request_id,),
    )
    vendor_name = _vendor_display_name_for_request(payment_request_id)
    pay_date_str = pr["requested_pay_date"].isoformat() if pr["requested_pay_date"] else None
    total_amount = float(pr["amount"])

    next_index = db.query_one(
        "SELECT COUNT(*) AS c FROM checkreq.payment_request_attachments WHERE payment_request_id = %s",
        (payment_request_id,),
    )["c"] + 1

    ext = _guess_extension(filename, content_type)
    archived_filename = _compute_archived_filename(
        org["code"], pay_date_str, vendor_name, total_amount, request_number, next_index, ext,
    )
    blob_path = f"{request_number}/{archived_filename}"
    gcs_client.upload_bytes(ATTACHMENTS_BUCKET, blob_path, data, content_type)

    token = sharepoint_client.get_access_token()
    site_id = sharepoint_client.get_site_id(token, org["sp_hostname"], org["sp_site_path"])
    try:
        sp_result = sharepoint_client.upload_bytes(
            token, site_id, org["sp_library_folder"], archived_filename, data, content_type,
        )
    except Exception:
        gcs_client.delete_blob(ATTACHMENTS_BUCKET, blob_path)
        raise

    row = {
        "source": source,
        "original_filename": filename,
        "archived_filename": archived_filename,
        "content_type": content_type,
        "size_bytes": len(data),
        "gcs_bucket": ATTACHMENTS_BUCKET,
        "gcs_blob_path": blob_path,
        "sp_file_path": f"{org['sp_library_folder'].strip('/')}/{archived_filename}",
        "sp_web_url": sp_result.get("webUrl"),
    }
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO checkreq.payment_request_attachments
                    (payment_request_id, source, original_filename, archived_filename,
                     content_type, size_bytes, gcs_bucket, gcs_blob_path, sp_file_path,
                     sp_web_url, uploaded_by_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (payment_request_id, row["source"], row["original_filename"], row["archived_filename"],
                 row["content_type"], row["size_bytes"], row["gcs_bucket"], row["gcs_blob_path"],
                 row["sp_file_path"], row["sp_web_url"], user_id),
            )
            row["id"] = cur.fetchone()["id"]
    return row


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
    request_type = form.get("request_type", "check_request")

    # Invoice Processing Intake Plan.md (Tier 3, 2026-08-02): "Program Area
    # defaults to All" -- an Invoice Intake coder, unlike a Check Request
    # submitter, isn't required to pick one. A blank selection is honored as
    # None ("All"); a chosen one is still checked the normal way. The Check
    # Request path below is byte-for-byte what this route always did --
    # program_area_id stays required and _user_can_submit_for-gated.
    if request_type == "invoice_payment":
        raw_pa = (form.get("program_area_id") or "").strip()
        program_area_id = int(raw_pa) if raw_pa else None
        if program_area_id is not None and not _user_can_submit_for(user, program_area_id, org_id):
            return JSONResponse(
                {"error": "You are not assigned to this program area. "
                          "Ask an admin to add you in checkreq.user_program_areas, "
                          "or use Request Access to ask for it."},
                status_code=403,
            )
    else:
        program_area_id = int(form["program_area_id"])
        if not _user_can_submit_for(user, program_area_id, org_id):
            return JSONResponse(
                {"error": "You are not assigned to this program area. "
                          "Ask an admin to add you in checkreq.user_program_areas, "
                          "or use Request Access to ask for it."},
                status_code=403,
            )

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
        # Invoice Intake's Draft queue is a shared team inbox, not a
        # personal submission -- any authorized Invoice Intake staff member
        # may finish coding a Draft, not just whoever originally uploaded
        # it. Every other request (Check Requests, and an invoice_payment
        # row once it's past Draft) keeps the existing submitter-only rule.
        if existing_pr["request_type"] == "invoice_payment" and existing_pr["status"] == "Draft":
            if not rbac.user_has_role(user["id"], "invoice_intake_submitter", existing_pr["org_id"]):
                return JSONResponse({"error": "Not authorized to code Invoice Intake requests."}, status_code=403)
        elif existing_pr["submitter_user_id"] != user["id"]:
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

    # -- SAVE AS DRAFT (2026-08-17) ------------------------------------------
    # Jay: "add a feature that allows a Check Request to be saved as a Draft
    # (like you do for Invoice upload)."
    #
    # Placed HERE, before every submit-time validation below, on purpose. A
    # draft is unfinished work, so it must not be held to the rules that exist
    # to make a SUBMISSION sound: vendor required, pay date required, a
    # positive amount, the tier-3 budget block. Putting this branch after those
    # checks would mean you could only save a draft that was already complete
    # enough to submit, which defeats the point.
    #
    # It therefore skips, deliberately, every one of:
    #   - the "select a vendor" / "Pay Date is required" 400s
    #   - _evaluate_gl_line_budgets (a draft is not being submitted, so it is
    #     neither blocked nor flagged and nothing hits budget_overage_log)
    #   - build_approval_chain / _materialize_approval_actions (no approver
    #     should see or be notified about a draft)
    #   - _archive_attachments (the archived PDF is a point-in-time SUBMISSION
    #     snapshot; archiving on every draft save would litter SharePoint with
    #     versions of an unfinished request)
    #
    # No migration was needed: payment_requests.status already DEFAULTS to
    # 'Draft' and has no CHECK constraint, and program_area_id / vendor_id /
    # requested_pay_date / description are all nullable -- verified against the
    # live schema before building. Invoice Intake has written 'Draft' rows since
    # 2026-08-02, and this function's own DRAFT FINALIZE branch below is keyed
    # on status alone rather than request_type, so a check_request draft
    # finalizes through that same already-proven code with no change to it.
    if form.get("save_as_draft") == "1":
        # Only an unsaved request or an existing DRAFT may be saved as a draft.
        # This guard is the important one: pulling an UnderReview row back to
        # Draft would orphan its approval_actions and silently withdraw it from
        # approvers' queues.
        if existing_pr and existing_pr["status"] != "Draft":
            return JSONResponse(
                {"error": "This request is already " + str(existing_pr["status"])
                          + " and cannot be returned to draft. Edit and resubmit it instead."},
                status_code=400,
            )

        d_program_area_id = int(form["program_area_id"]) if form.get("program_area_id") else None
        if d_program_area_id is not None and not _user_can_submit_for(user, d_program_area_id, org_id):
            # Authorization still applies to a draft -- it is a real row in a
            # real entity, and this is the same gate the submit path uses. Only
            # the COMPLETENESS rules are relaxed here, never access.
            return JSONResponse(
                {"error": "You don't have access to that program area."}, status_code=403)

        d_using_new_vendor = form.get("using_new_vendor") == "1"
        d_vendor_id = (int(form["vendor_id"])
                       if (not d_using_new_vendor and form.get("vendor_id")) else None)
        d_pay_date = form.get("requested_pay_date") or None
        d_description = form.get("description", "")
        d_special = form.get("special_instructions", "")

        # Tolerant GL parse: a blank or half-typed row is skipped rather than
        # rejected, which is the whole point of a draft.
        d_gl_lines = []
        d_accounts = form.getlist("gl_account_id")
        d_amounts = form.getlist("gl_amount")
        d_memos = form.getlist("gl_memo")
        for idx, acct in enumerate(d_accounts):
            if not (acct or "").strip():
                continue
            try:
                amt = round(float((d_amounts[idx] or "0").strip() or 0), 2)
            except (ValueError, IndexError):
                amt = 0.0
            d_gl_lines.append((int(acct), amt,
                               d_memos[idx] if idx < len(d_memos) else ""))
        if d_gl_lines:
            d_total = round(sum(a for _, a, _ in d_gl_lines), 2)
        else:
            try:
                d_total = round(float((form.get("ask_my_accountant_amount") or "0").strip() or 0), 2)
            except ValueError:
                d_total = 0.0

        with db.connect() as conn:
            with conn.cursor() as cur:
                if existing_pr:
                    d_request_number = existing_pr["request_number"]
                    d_pr_id = existing_pr["id"]
                    cur.execute(
                        "UPDATE checkreq.payment_requests SET program_area_id = %s, "
                        "  vendor_id = %s, amount = %s, requested_pay_date = %s, "
                        "  description = %s, special_instructions = %s, updated_at = NOW() "
                        "WHERE id = %s AND org_id = %s",
                        (d_program_area_id, d_vendor_id, d_total, d_pay_date,
                         d_description, d_special, d_pr_id, org_id))
                else:
                    d_request_number = _next_request_number("check_request")
                    cur.execute(
                        "INSERT INTO checkreq.payment_requests "
                        "(request_number, request_type, org_id, program_area_id, "
                        " submitter_user_id, vendor_id, amount, requested_pay_date, "
                        " description, special_instructions, status) "
                        "VALUES (%s, 'check_request', %s, %s, %s, %s, %s, %s, %s, %s, 'Draft') "
                        "RETURNING id",
                        (d_request_number, org_id, d_program_area_id, user["id"],
                         d_vendor_id, d_total, d_pay_date, d_description, d_special))
                    d_pr_id = cur.fetchone()["id"]

                # Replace coding lines wholesale, matching the edit path's own
                # settled approach -- simpler than diffing, and a draft's lines
                # carry no downstream state.
                cur.execute("DELETE FROM checkreq.payment_request_gl_lines "
                            "WHERE payment_request_id = %s", (d_pr_id,))
                for acct_id, amt, memo in d_gl_lines:
                    cur.execute(
                        "INSERT INTO checkreq.payment_request_gl_lines "
                        "(payment_request_id, gl_account_id, amount, memo) "
                        "VALUES (%s, %s, %s, %s)", (d_pr_id, acct_id, amt, memo))

                cur.execute(
                    "INSERT INTO checkreq.audit_log "
                    "(payment_request_id, action_by_user_id, action_type, new_status, comment) "
                    "VALUES (%s, %s, 'Draft Saved', 'Draft', %s)",
                    (d_pr_id, user["id"],
                     "Saved as draft -- not submitted, no approvers notified."))
            conn.commit()

        # A draft cannot carry an in-progress NEW vendor: checkreq.vendor_requests
        # is CHECK-constrained to pending_approval/approved/rejected/posted_to_qbo
        # with no draft state, and the Vendor Approvals queue lists every row
        # regardless of status -- creating one here would put an unfinished
        # request in front of a vendor approver. Reported in the banner rather
        # than dropped silently, which would be a data-loss bug.
        extra = "&new_vendor_dropped=1" if d_using_new_vendor else ""
        return RedirectResponse(
            "/my-requests?draft_saved=" + d_request_number + extra, status_code=303)

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

    # Ask My Accountant (2026-08-16, per Jay's revision of the original
    # "Ask My Accountant" thread-log design): the submitter still picks a
    # Program Area, but may skip GL Coding entirely if they don't know it --
    # AP assigns the actual GL line(s) later, via the AP Review screen's own
    # "Needs GL Coding" section (see _assign_gl_coding below), same
    # role/screen Jay specified, not a new one. No schema change needed:
    # checkreq.payment_request_gl_lines has no "at least one row" DB
    # constraint, and payment_requests.amount is its own required field
    # independent of GL lines -- confirmed directly against the schema
    # before building this.
    ask_my_accountant = form.get("ask_my_accountant") == "1"
    if ask_my_accountant:
        gl_lines: list[tuple[int, float, str]] = []
        raw_amount = (form.get("ask_my_accountant_amount") or "").strip()
        try:
            total_amount = round(float(raw_amount), 2)
        except ValueError:
            total_amount = 0.0
        if total_amount <= 0:
            return JSONResponse(
                {"error": "Please enter the amount to be paid."},
                status_code=400,
            )
    else:
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

    # Pre-Approved Submission Designation (Pre-Approved Submission Plan.md,
    # 2026-08-01): "certain submitters are allowed to designate that the
    # approvals for this CR have already been obtained and are on the
    # uploaded document(s)" -- only trusted server-side, never from the
    # client checkbox alone, same posture as every other gate in this app.
    pre_approved_requested = form.get("pre_approved") == "1"
    pre_approved = pre_approved_requested and rbac.user_has_role(
        user["id"], "pre_approved_submitter", org_id=None
    )
    if pre_approved:
        has_attachment = bool(uploaded_attachments) or (
            existing_pr is not None and bool(_active_attachments(existing_pr["id"]))
        )
        if not has_attachment:
            return JSONResponse(
                {"error": "Pre-approval documentation is required -- attach at least one file "
                          "showing the approval before submitting this way."},
                status_code=400,
            )

    # Approval Workflow Corrections (Jay, 2026-07-31): a self-payment --
    # this vendor row is linked to the submitter's own login -- always
    # requires CFO approval, bypassing the normal Program-Area chain and
    # the entity global-approver step entirely, regardless of amount or the
    # submitter's own authorization. Only meaningful for an EXISTING vendor
    # (using_new_vendor has no linked_user_id concept -- see
    # _is_self_payment's own docstring).
    is_self_payment = (not using_new_vendor) and _is_self_payment(vendor_id, user["id"])
    # Invoice Processing Intake Plan.md (Tier 3): an active ART (Authorized
    # Recurring Transactions) entry for this vendor -- per Jay's direct
    # answer this session, an ART-preapproved vendor ALWAYS skips the chain
    # entirely and lands straight on AP Review, no confirmation step of any
    # kind (art_list.preapproval_scope is left unused for this reason).
    vendor_art = (not using_new_vendor) and art_preapproval.vendor_preapproval_status(
        vendor_id, org_id, total_amount
    )
    if ask_my_accountant:
        # Top precedence, deliberate: matches the original design's intent
        # (thread log, 2026-08-15) that GL-coding-skip defers ANY chain
        # computation -- including self-payment/pre-approved/ART -- until
        # AP has assigned real GL line(s). Budget enforcement is also
        # necessarily skipped until then (nothing to check against yet);
        # _assign_gl_coding() re-derives all of this once coding is done.
        chain = []
        chain_summary = "Ask My Accountant -- awaiting GL coding from AP before the approval chain starts."
    elif is_self_payment:
        chain = _self_payment_cfo_chain(user["id"], org_id)
        chain_summary = (
            "Self-payment -- requires CFO approval (any one), regardless of amount "
            "or the submitter's own authorization."
            if chain else
            "Self-payment -- no CFO configured to approve this. Needs setup."
        )
        # Pre-Approved Submission Designation: confirmed with Jay -- self-
        # payment protection is NOT bypassable by this designation, so the
        # attestation is only noted alongside it, never replaces it.
        if pre_approved:
            chain_summary = (
                "Submitter designates this request as previously approved outside Beacon; "
                "see attached documentation. Self-payment rules still require independent "
                "CFO approval below.\n" + chain_summary
            )
    elif pre_approved:
        # Skips the ordinary Program-Area/Global-Approver chain entirely --
        # the tier-3 budget block below still applies on top of this if
        # triggered (also confirmed non-bypassable).
        chain = []
        chain_summary = ("Submitter designates this request as previously approved outside "
                          "Beacon; see attached documentation.")
    elif vendor_art:
        # Precedence, deliberate: self-payment and an explicit human
        # pre-approval attestation both outrank a vendor-level ART setting
        # -- self-payment is a conflict-of-interest control that should
        # never be overridable by a convenience designation on the vendor.
        chain = []
        chain_summary = (
            f"ART Preapproved ({vendor_art['vendor_display_name']}) -- skips the approval "
            f"chain, goes straight to AP Review."
        )
        if vendor_art["amount_flag"]:
            chain_summary += f"\nNote for AP: {vendor_art['amount_flag']}"
    else:
        # Invoice Intake's "Program Area defaults to All" is about browsing/
        # coding GL accounts, not approval routing -- without an ART/self-
        # payment/pre-approved override, SOME program area is required here,
        # or this request would silently reach an empty chain (which reads
        # as "nothing required," not "not yet decided") and skip approval
        # by accident.
        if program_area_id is None:
            return JSONResponse(
                {"error": "Please select a Program Area before submitting -- this vendor "
                          "isn't ART Preapproved, so a program area is needed to route this "
                          "for approval."},
                status_code=400,
            )
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
            for u in _cfo_approver_rows(user["id"], org_id)
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

    # Pre-Approved Submission Designation: reaches 'Approved' immediately
    # ONLY when the designation actually left nothing else to satisfy (no
    # self-payment override, no tier-3 budget group) -- otherwise this
    # follows the normal UnderReview path like any other chain. Gated on
    # pre_approved specifically, not on "chain is empty" in general, so this
    # doesn't change behavior for the (separate, pre-existing) case of a
    # program area with no approval_rules configured at all.
    initial_status = (
        "AwaitingCoding" if ask_my_accountant else
        "Approved" if ((pre_approved or vendor_art) and not chain) else
        "UnderReview"
    )

    imp_id = request.session.get("impersonating_user_id")
    impersonated_by = _real_user(request)["id"] if imp_id else None

    if existing_pr:
        if existing_pr["status"] == "Draft":
            # ── DRAFT FINALIZE branch (Invoice Intake, Tier 3, 2026-08-02) ──
            # A Draft's first real submission -- nothing to "reset" here,
            # unlike the EDIT branch below, since no approval chain has ever
            # been computed for this row yet. Always returns, so the
            # existing EDIT branch immediately below never runs for a Draft.
            request_number = existing_pr["request_number"]
            payment_request_id = existing_pr["id"]
            old_vendor_request_id = existing_pr["vendor_request_id"]

            with db.connect() as conn:
                with conn.cursor() as cur:
                    # Vendor bookkeeping -- identical shape to the EDIT
                    # branch's own handling below (a payment_request has at
                    # most one vendor_request row; update it in place if one
                    # already exists rather than creating a duplicate).
                    new_vendor_request_id = old_vendor_request_id
                    if using_new_vendor:
                        requires_w9 = total_amount > VENDOR_W9_AMOUNT_THRESHOLD
                        if old_vendor_request_id:
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
                        new_vendor_request_id = None

                    cur.execute(
                        """
                        UPDATE checkreq.payment_requests SET
                            program_area_id = %s, vendor_id = %s, vendor_request_id = %s,
                            amount = %s, requested_pay_date = %s, description = %s,
                            special_instructions = %s, status = %s, current_approver_id = %s,
                            serial_group_current = %s, approval_chain_summary = %s,
                            overspend_flagged = %s, overspend_detail = %s, budget_checked_at = NOW(),
                            pre_approved = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (program_area_id, vendor_id, new_vendor_request_id, total_amount,
                         requested_pay_date, description, special_instructions,
                         initial_status, first_display_approver, first_serial_group,
                         chain_summary, overspend_flagged, overspend_detail, pre_approved,
                         payment_request_id),
                    )

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

                    # Nothing was ever pending for this row -- no supersede
                    # call needed, unlike the EDIT branch's reset case.
                    _materialize_approval_actions(cur, payment_request_id, chain)

                    cur.execute(
                        "INSERT INTO checkreq.audit_log "
                        "(payment_request_id, action_by_user_id, action_type, comment, "
                        " previous_status, new_status, impersonated_by_user_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (payment_request_id, user["id"], "Submitted", chain_summary,
                         "Draft", initial_status, impersonated_by),
                    )

            if budget_result["buffer_notice"]:
                _send_budget_buffer_notice_email(
                    request_number, org["name"], org["id"],
                    [e["detail"] for e in budget_result["buffer_notice"]],
                )
            notify_group = first_serial_group
            if not is_self_payment:
                advanced = _maybe_auto_approve_self(
                    payment_request_id, first_serial_group, user["id"], _client_ip(request), request,
                )
                if advanced is not None:
                    notify_group = advanced
            _notify_approvers_for_group(payment_request_id, notify_group, request)

            # Archive ONLY the generated voucher PDF -- the real invoice was
            # already archived (source='user_upload') back at intake time.
            archive_warning = None
            try:
                _archive_one_file(
                    org, payment_request_id, request_number, "generated_pdf",
                    f"{request_number}.pdf", "application/pdf",
                    render_check_voucher_pdf(payment_request_id), user["id"],
                )
            except Exception as exc:
                archive_warning = str(exc)

            redirect_url = f"/my-requests?submitted={request_number}"
            if archive_warning:
                redirect_url += f"&archive_warning={quote(archive_warning)}"
            return RedirectResponse(redirect_url, status_code=303)

        # ── EDIT branch (Check Requests, and an invoice_payment row once
        # it's past Draft) -- unchanged below; unreachable for a Draft
        # since the branch above always returns ─────────────────────────
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
                            pre_approved = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (program_area_id, vendor_id, new_vendor_request_id, total_amount,
                         requested_pay_date, description, special_instructions,
                         initial_status,
                         first_display_approver, first_serial_group,
                         chain_summary, overspend_flagged, overspend_detail, pre_approved, payment_request_id),
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
                         existing_pr["status"], initial_status, impersonated_by),
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
                request_number, org["name"], org["id"], [e["detail"] for e in budget_result["buffer_notice"]],
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
                     overspend_flagged, overspend_detail, pre_approved, budget_checked_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                RETURNING id
                """,
                (request_number, request_type, org_id, program_area_id, user["id"],
                 vendor_id, total_amount, requested_pay_date, description, special_instructions,
                 initial_status,
                 first_display_approver, first_serial_group,
                 chain_summary, overspend_flagged, overspend_detail, pre_approved),
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
                (payment_request_id, user["id"], "Submitted", chain_summary, initial_status, impersonated_by),
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
            request_number, org["name"], org["id"], [e["detail"] for e in budget_result["buffer_notice"]],
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

    # Phase D (Easy View): lands on the request's own detail page instead
    # of My Requests -- Easy View's whole point is a simpler on-ramp, and
    # seeing exactly what was just submitted is a more useful confirmation
    # than a list row. The classic form's redirect is unchanged.
    if form.get("ui_variant") == "easy":
        redirect_url = f"/requests/{request_number}/view?submitted=1"
    else:
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
        rbac.user_has_role(user["id"], "cfo", pr["org_id"])
        or pr["submitter_user_id"] == user["id"]
        or _user_can_submit_for(user, pr["program_area_id"], pr["org_id"])
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

    # Archival itself is now shared with Invoice Intake via _archive_one_file()
    # (2026-08-02 extraction) -- it handles the vendor-name/pay-date lookup,
    # the never-reuse-a-removed-row's-index computation, and the GCS+
    # SharePoint upload pipeline, one call per file.
    imp_id = request.session.get("impersonating_user_id")
    impersonated_by = _real_user(request)["id"] if imp_id else None

    errors = []
    for f in files:
        content = await f.read()
        if not content:
            continue
        try:
            content_type = f.content_type or "application/octet-stream"
            _archive_one_file(org, pr["id"], request_number, "user_upload",
                               f.filename, content_type, content, user["id"])
            with db.connect() as conn:
                with conn.cursor() as cur:
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


@app.get("/invoice-intake", response_class=HTMLResponse)
def invoice_intake_queue(request: Request, add_error: str = ""):
    """Invoice Processing Intake Plan.md (Tier 3, 2026-08-02) -- the shared
    team queue for invoices uploaded outside the manual Check Request form.
    Gated on the invoice_intake_submitter role (a plain RBAC grant, same
    mechanism as pre_approved_submitter -- no new admin UI needed)."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    org = _current_org(request)
    if not org:
        return RedirectResponse("/portal")
    if not rbac.user_has_role(user["id"], "invoice_intake_submitter", org["id"]):
        return HTMLResponse("Not authorized -- ask an admin to grant you the Invoice Intake role.", status_code=403)

    drafts = db.query(
        """
        SELECT pr.request_number, pr.created_at,
               COALESCE(v.display_name, vr.company_name, vr.first_name || ' ' || vr.last_name,
                        pr.invoice_extracted_vendor, '—') AS vendor_name,
               COALESCE(pr.invoice_extracted_amount, pr.amount) AS amount
        FROM checkreq.payment_requests pr
        LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
        LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
        WHERE pr.org_id = %s AND pr.request_type = 'invoice_payment' AND pr.status = 'Draft'
        ORDER BY pr.created_at DESC
        """,
        (org["id"],),
    )
    return _render(request, "invoice_intake_queue.html", user, {"drafts": drafts, "add_error": add_error})


@app.post("/invoice-intake")
async def invoice_intake_upload(request: Request, file: UploadFile):
    """Uploads one invoice, extracts what we can, best-effort vendor match,
    checks ART/Monkey-See-Monkey-Do, creates the Draft row, archives the
    raw file -- then hands off to the SAME edit/finalize screen a Check
    Request uses (GET/POST /requests/{request_number}/edit), rather than a
    second coding UI. Vendor recognition is deliberately the first real
    step here, before anything else, per the plan's own framing."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    org = _current_org(request)
    if not org:
        return RedirectResponse("/portal")
    if not rbac.user_has_role(user["id"], "invoice_intake_submitter", org["id"]):
        return HTMLResponse("Not authorized -- ask an admin to grant you the Invoice Intake role.", status_code=403)

    content = await file.read()
    if len(content) > _EXTRACT_MAX_BYTES:
        return RedirectResponse(
            f"/invoice-intake?add_error={quote('File is too large (max 10MB).')}", status_code=303,
        )
    mime_type = file.content_type or ""
    if mime_type not in _EXTRACT_ALLOWED_TYPES:
        return RedirectResponse(
            f"/invoice-intake?add_error={quote('Unsupported file type -- use a PDF or JPG/PNG/GIF/WebP image.')}",
            status_code=303,
        )

    try:
        result = await asyncio.to_thread(document_extract.extract_fields, content, mime_type)
    except Exception as exc:
        print(f"[invoice-intake] extraction {type(exc).__name__}: {exc}")
        result = {}

    vendor_name = result.get("vendor_name")
    vendor_id = None
    if vendor_name:
        match = db.query_one(
            "SELECT id FROM checkreq.vendors WHERE org_id = %s AND is_active AND display_name ILIKE %s "
            "ORDER BY display_name LIMIT 1",
            (org["id"], f"%{vendor_name}%"),
        )
        if match:
            vendor_id = match["id"]

    extracted_amount = result.get("amount")

    request_number = _next_request_number("invoice_payment")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO checkreq.payment_requests
                    (request_number, request_type, org_id, program_area_id, submitter_user_id,
                     vendor_id, amount, requested_pay_date, description, status,
                     source_channel, invoice_extracted_vendor, invoice_extracted_amount)
                VALUES (%s, 'invoice_payment', %s, NULL, %s, %s, %s, %s, %s, 'Draft',
                        'manual_upload', %s, %s)
                RETURNING id
                """,
                (request_number, org["id"], user["id"], vendor_id,
                 extracted_amount or 0, date.today(), result.get("description") or "",
                 vendor_name, extracted_amount),
            )
            payment_request_id = cur.fetchone()["id"]

    # Monkey-See-Monkey-Do pre-fill: only meaningful once a real vendor is
    # matched AND that vendor's ART entry designates it -- reads real QBO
    # Bill/Purchase history (Jay's direct correction, 2026-08-02), not
    # Beacon's own GL-line history, which would be empty for a brand-new
    # intake flow. Best-effort: any failure here just means no pre-fill,
    # never blocks the Draft from being created.
    if vendor_id:
        art = art_preapproval.vendor_preapproval_status(vendor_id, org["id"])
        if art and art["is_monkey_see_monkey_do"]:
            try:
                _prefill_msmd_gl_lines(org, payment_request_id, vendor_id, extracted_amount)
            except Exception as exc:
                print(f"[invoice-intake] MSMD pre-fill failed for vendor {vendor_id}: {exc}")

    # Archive the raw uploaded invoice now -- the generated voucher PDF is
    # archived separately, at finalize (see the Draft-finalize branch of
    # new_request_submit).
    try:
        _archive_one_file(org, payment_request_id, request_number, "user_upload",
                           file.filename or "invoice", mime_type, content, user["id"])
    except Exception as exc:
        print(f"[invoice-intake] archival failed for {request_number}: {exc}")

    return RedirectResponse(f"/requests/{request_number}/edit?intake=1", status_code=303)


def _prefill_msmd_gl_lines(org: dict, payment_request_id: int, vendor_id: int,
                            new_total: float | None) -> None:
    """Fetches the vendor's most recent real QBO Bill/Purchase and
    proportionally replays its GL-line split onto this brand-new Draft --
    the split PATTERN repeats even though the dollar amounts don't. Never
    silently final -- every pre-filled line stays fully editable, and the
    coding screen marks them "auto-filled from last month's coding -- please
    review" (see invoice_intake=1 handling in new_request.html)."""
    vendor = db.query_one("SELECT qbo_vendor_id FROM checkreq.vendors WHERE id = %s", (vendor_id,))
    if not vendor or not vendor.get("qbo_vendor_id"):
        return
    result, err = qbo_mcp_client.get_vendor_last_bill((org.get("code") or "").lower(), vendor["qbo_vendor_id"])
    if err or not result or not result.get("found") or not result.get("bill"):
        return
    bill = result["bill"]
    lines = bill.get("lines") or []
    prior_total = sum(abs(float(ln["amount"])) for ln in lines)
    if not lines or prior_total <= 0:
        return
    replay_total = float(new_total) if new_total else prior_total

    with db.connect() as conn:
        with conn.cursor() as cur:
            for ln in lines:
                acct = db.query_one(
                    "SELECT id FROM checkreq.gl_accounts WHERE org_id = %s AND account_number = %s",
                    (org["id"], ln["acct_num"]),
                )
                if not acct:
                    continue  # this GL account isn't mapped in Beacon -- skip rather than guess
                fraction = abs(float(ln["amount"])) / prior_total
                amount = round(replay_total * fraction, 2)
                cur.execute(
                    "INSERT INTO checkreq.payment_request_gl_lines "
                    "(payment_request_id, gl_account_id, amount, memo) VALUES (%s, %s, %s, %s)",
                    (payment_request_id, acct["id"], amount,
                     f"Auto-filled from last month's coding ({bill['txn_date']}) -- please review."),
                )


@app.get("/my-requests", response_class=HTMLResponse)
def my_requests(request: Request, submitted: str = "", archive_warning: str = "", edited: str = "",
                cancelled: str = "", view: str = "mine"):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

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

    # Task 6 (2026-07-26): CFO-only "My Requests" vs "All Requests" toggle.
    # "All Requests" deliberately stays scoped to the currently selected
    # entity (org_id = current_org_id), NOT a global cross-entity view --
    # Jay's explicit call. A non-CFO passing ?view=all gets silently treated
    # as "mine" rather than an error -- same "no silent broadening of access"
    # posture as _user_can_submit_for, just expressed as a quiet fallback
    # since this is a read-only list view, not a submission.
    # RBAC (2026-08-01): entity-scoped, matching where this view already
    # scopes everything else -- Role-Based Access Control Plan.md §5.7.
    show_all = (view == "all") and rbac.user_has_role(user["id"], "cfo", org["id"])

    if show_all:
        rows = db.query(
            """
            SELECT pr.id AS pr_id, pr.request_number, pr.request_type, pr.amount, pr.status,
                   pr.approval_chain_summary, pr.created_at, pr.requested_pay_date, o.code AS org_code,
                   COALESCE(pa.title, 'All Program Areas') AS program_area_title, u.display_name AS submitter_name,
                   u.email AS submitter_email,
                   v.display_name AS vendor_display_name,
                   vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
                   vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
                   vr.dba_name AS vr_dba_name
            FROM checkreq.payment_requests pr
            JOIN checkreq.organizations o ON o.id = pr.org_id
            LEFT JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
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
                   COALESCE(pa.title, 'All Program Areas') AS program_area_title,
                   v.display_name AS vendor_display_name,
                   vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
                   vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
                   vr.dba_name AS vr_dba_name
            FROM checkreq.payment_requests pr
            JOIN checkreq.organizations o ON o.id = pr.org_id
            LEFT JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
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
    tile_badges.mark_viewed(user["id"], "approval_queue")

    # Jay, 2026-07-29: "how can I see items already taken an action on?"
    # Once approved/rejected, a request drops off the pending queue entirely
    # -- this branch shows the requester's OWN past actions instead, sourced
    # straight from approval_actions (the real per-approver record), not the
    # pending-queue query below.
    if view == "history":
        history_rows = db.query(
            """
            SELECT pr.request_number, pr.request_type, pr.amount, o.code AS org_code,
                   COALESCE(pa.title, 'All Program Areas') AS program_area_title,
                   v.display_name AS vendor_display_name,
                   vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
                   vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
                   vr.dba_name AS vr_dba_name,
                   aa.status AS action_status, aa.acted_at, aa.comment
            FROM checkreq.approval_actions aa
            JOIN checkreq.payment_requests pr ON pr.id = aa.payment_request_id
            JOIN checkreq.organizations o ON o.id = pr.org_id
            LEFT JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
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
    # RBAC (2026-08-01): entity-scoped -- Role-Based Access Control Plan.md §5.7.
    show_all = (view == "all") and rbac.user_has_role(user["id"], "cfo", org["id"])

    if show_all:
        rows = db.query(
            """
            SELECT DISTINCT pr.id AS pr_id, pr.request_number, pr.request_type, pr.amount,
                   pr.status, pr.approval_chain_summary, pr.created_at, pr.requested_pay_date,
                   o.code AS org_code,
                   COALESCE(pa.title, 'All Program Areas') AS program_area_title, u.display_name AS submitter_name,
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
            LEFT JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
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
                   COALESCE(pa.title, 'All Program Areas') AS program_area_title,
                   v.display_name AS vendor_display_name,
                   vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
                   vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
                   vr.dba_name AS vr_dba_name
            FROM checkreq.payment_requests pr
            JOIN checkreq.approval_actions aa
              ON aa.payment_request_id = pr.id AND aa.serial_group = pr.serial_group_current
             AND aa.approver_user_id = %s AND aa.status = 'pending'
            JOIN checkreq.organizations o ON o.id = pr.org_id
            LEFT JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
            LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
            LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
            WHERE pr.status = 'UnderReview' AND pr.org_id = %s
            ORDER BY pr.created_at
            """,
            (user["id"], org["id"]),
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

    # Ask My Accountant (2026-08-16): a new section on this SAME daily
    # digest, per Jay's explicit answer in the thread log ("can be part of
    # the daily mail, not an individual email") -- notifies each submitter
    # whose request(s) had GL coding assigned by AP in roughly the last day
    # (matching this digest's own once-daily cadence) and moved into the
    # real approval chain. Sourced from audit_log's own "GL Coding Assigned"
    # entries -- no new tracking table needed.
    coded = db.query(
        """
        SELECT pr.submitter_user_id, u.email AS submitter_email, u.display_name AS submitter_name,
               pr.request_number, pr.amount, pr.approval_chain_summary
        FROM checkreq.audit_log al
        JOIN checkreq.payment_requests pr ON pr.id = al.payment_request_id
        JOIN checkreq.app_users u ON u.id = pr.submitter_user_id
        WHERE al.action_type = 'GL Coding Assigned' AND al.action_date > NOW() - INTERVAL '1 day'
        ORDER BY u.email, pr.request_number
        """
    )
    coding_notified = 0
    by_submitter: dict[int, list[dict]] = {}
    for r in coded:
        by_submitter.setdefault(r["submitter_user_id"], []).append(r)
    for submitter_id, items in by_submitter.items():
        lines = "\n".join(f"- {i['request_number']} (${i['amount']:.2f}): {i['approval_chain_summary']}" for i in items)
        body_text = (
            f"AP has finished assigning GL coding to the following request(s) you submitted with "
            f"\"Ask My Accountant\" -- they're now in the approval chain:\n\n{lines}"
        )
        try:
            result = email_client.send_email(
                to=items[0]["submitter_email"],
                subject="Beacon: GL coding assigned -- your request(s) are now in review",
                body_text=body_text,
            )
            if result.get("status") == "sent":
                coding_notified += 1
            else:
                errors.append({"submitter": items[0]["submitter_email"], "error": result.get("error")})
        except Exception as exc:
            errors.append({"submitter": items[0]["submitter_email"], "error": str(exc)})

    return JSONResponse({
        "approvers_notified": sent, "skipped_empty": skipped_empty,
        "gl_coding_submitters_notified": coding_notified, "errors": errors,
    })


# ── Feedback (Task 10, UI/UX batch, 2026-07-26; rebuilt into a real
# conversation, 2026-08-02, see feedback_chat.py) ────────────────────────────
# GET/POST /feedback and the whole conversational flow now live in
# feedback_chat.py (register()'d near the bottom of this file, same
# injection pattern as admin_setup.py/access_requests.py) -- kept out of
# this already-~4,700-line file per the project's modular-file-organization
# standard, and because the Claude API call logic is a genuinely distinct
# concern from everything else in here.


# ── Administrative: system-wide request log (Jay, 2026-07-29) ───────────────
# "some sort of administratives pill on the main menu for people that have
# administrative access. They're gonna be able to review all the logs, like
# all the CRs and where they are, what has happened to them." Distinct from
# My Requests' own "All Requests" toggle -- that one stays scoped to the
# session's selected entity by Jay's own earlier explicit call (Task 6,
# 2026-07-26); this view is deliberately NOT entity-scoped, showing every
# request across every organization and every status in one place.

@app.get("/admin/all-requests", response_class=HTMLResponse)
def admin_all_requests(request: Request, entity: str = "", vendor: str = "",
                        submitted_by: str = "", status: str = ""):
    """2026-08-02 feedback batch, Item 7: renamed "Administrative" -> "All
    Requests" in the template; added entity/vendor/submitted-by/status
    filters, all confirmed necessary for this screen to be effective at
    real scale. Filters are plain query params (GET, bookmarkable/
    shareable) rather than a POST+session state -- matches this screen's
    existing read-only, no-state-to-preserve character."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    # RBAC (2026-08-01): cross-entity by design -- the genuinely cross-entity
    # audit view Jay asked for. See Role-Based Access Control Plan.md §2.2.
    if not rbac.user_has_role(user["id"], "cfo", org_id=None):
        return JSONResponse({"error": "CFO access required"}, status_code=403)

    where = []
    params: list = []
    if entity:
        where.append("o.code = %s")
        params.append(entity)
    if status:
        where.append("pr.status = %s")
        params.append(status)
    if vendor:
        where.append(
            "(v.display_name ILIKE %s OR vr.company_name ILIKE %s OR vr.dba_name ILIKE %s "
            " OR vr.first_name ILIKE %s OR vr.last_name ILIKE %s)"
        )
        like = f"%{vendor}%"
        params.extend([like, like, like, like, like])
    if submitted_by:
        where.append("(u.display_name ILIKE %s OR u.email ILIKE %s)")
        like = f"%{submitted_by}%"
        params.extend([like, like])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    rows = db.query(
        f"""
        SELECT pr.id AS pr_id, pr.request_number, pr.request_type, pr.amount, pr.status,
               pr.created_at, pr.updated_at, o.code AS org_code,
               COALESCE(pa.title, 'All Program Areas') AS program_area_title, u.display_name AS submitter_name,
               u.email AS submitter_email,
               v.display_name AS vendor_display_name,
               vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
               vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
               vr.dba_name AS vr_dba_name
        FROM checkreq.payment_requests pr
        JOIN checkreq.organizations o ON o.id = pr.org_id
        LEFT JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
        JOIN checkreq.app_users u ON u.id = pr.submitter_user_id
        LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
        LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
        {where_sql}
        ORDER BY pr.created_at DESC
        """,
        tuple(params),
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

    all_orgs_list = db.query("SELECT code, name FROM checkreq.organizations WHERE is_active ORDER BY name")
    all_statuses = ["UnderReview", "Approved", "Posted to QBO", "Rejected", "Returned by AP", "Cancelled"]
    return _render(request, "admin_all_requests.html", user, {
        "rows": rows,
        "filter_entity": entity, "filter_vendor": vendor,
        "filter_submitted_by": submitted_by, "filter_status": status,
        "all_orgs_list": all_orgs_list, "all_statuses": all_statuses,
    })


@app.get("/admin/feedback", response_class=HTMLResponse)
def feedback_list(request: Request):
    """CFO-only listing -- the task's own stated "nice-to-have," not the
    core ask (which is just collecting feedback). No edit/resolve workflow;
    read-only, matching the deliberately-simple scope of this feature."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    # RBAC (2026-08-01): cross-entity -- feedback is app-wide, not per entity.
    if not rbac.user_has_role(user["id"], "cfo", org_id=None):
        return JSONResponse({"error": "CFO access required"}, status_code=403)

    rows = db.query(
        """
        SELECT f.id, f.comment, f.created_at, o.code AS org_code,
               u.display_name AS submitter_name, u.email AS submitter_email,
               c.id AS conversation_id
        FROM checkreq.app_feedback f
        LEFT JOIN checkreq.organizations o ON o.id = f.org_id
        JOIN checkreq.app_users u ON u.id = f.submitted_by_user_id
        LEFT JOIN checkreq.feedback_conversations c ON c.feedback_id = f.id
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
    # RBAC (2026-08-01): setup_admin, not cfo -- this is an app-wide setting,
    # not a CFO-specific power, per Role-Based Access Control Plan.md §5.1 #6.
    if not rbac.user_has_role(user["id"], "setup_admin", org_id=None):
        return JSONResponse({"error": "Setup Administrator access required"}, status_code=403)

    enabled = app_settings.get_setting("email_test_mode", "false") == "true"
    address = app_settings.get_setting("email_test_mode_address", "") or ""
    return _render(request, "admin_test_mode.html", user, {"enabled": enabled, "address": address, "saved": saved})


@app.post("/admin/test-mode")
async def test_mode_save(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not rbac.user_has_role(user["id"], "setup_admin", org_id=None):
        return JSONResponse({"error": "Setup Administrator access required"}, status_code=403)

    form = await request.form()
    enabled = form.get("enabled") == "1"
    address = (form.get("address") or "").strip()

    # Dev/Prod Split Plan.md (2026-07-31), Decision 5: Test Mode is a
    # real code-level lock in production, not just a policy -- refuse to
    # even SAVE an "enabled" toggle here, not only at send time
    # (_apply_test_mode() has its own independent lock too -- defense in
    # depth, since this route and that function are two different places a
    # future change could otherwise drift apart).
    if enabled and BEACON_ENV == "prod":
        return _render(
            request, "admin_test_mode.html", user,
            {"enabled": False, "address": address,
             "error": "Test Mode cannot be enabled in production."},
        )

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
# role, deliberately NOT folded into is_cfo or GlobalApprovers.
#
# 2026-08-16: the gate below stays a plain existence check ("holds
# vendor_approver somewhere") so someone granted it at several entities
# keeps a real, current one-screen workflow -- but vendor_requests_list's
# own query now filters to rbac.get_granted_org_ids(), the actual set of
# orgs THIS user holds vendor_approver at (previously: every org's pending
# requests, unconditionally, "same blanket-access shape as is_cfo's own
# bypass" -- a real leak once a narrowly-granted parish-org approver can
# exist). Also added the optional `entity=` filter every sibling cross-org
# admin screen (All Requests/AP Review/Access Requests) already has --
# this was the one screen in that family that never got one.

def _require_vendor_approver(request: Request):
    """Returns (user, None) if allowed, or (None, error_response) if not --
    callers do `user, err = _require_vendor_approver(request); if err: return err`."""
    user = _current_user(request)
    if not user:
        return None, RedirectResponse("/login")
    if not rbac.user_has_role(user["id"], "vendor_approver", org_id=None):
        return None, JSONResponse({"error": "Vendor-approver access required"}, status_code=403)
    return user, None


@app.get("/admin/vendor-requests", response_class=HTMLResponse)
def vendor_requests_list(request: Request, email_warning: str = "", entity: str = ""):
    user, err = _require_vendor_approver(request)
    if err:
        return err

    granted_org_ids = rbac.get_granted_org_ids(user["id"], "vendor_approver")
    all_orgs_list = db.query("SELECT code, name FROM checkreq.organizations WHERE is_active ORDER BY name")

    where_sql = "WHERE vr.org_id = ANY(%s)"
    params: tuple = (granted_org_ids,)
    if entity:
        where_sql += " AND o.code = %s"
        params = (granted_org_ids, entity)

    rows = db.query(
        f"""
        SELECT vr.id, vr.entity_type, vr.first_name, vr.last_name, vr.company_name,
               vr.dba_name, vr.contact_name, vr.contact_email, vr.requires_w9,
               vr.w9_email_sent_at, vr.w9_received, vr.status, vr.rejected_reason,
               vr.created_at, o.name AS org_name, o.code AS org_code, pr.request_number, pr.amount
        FROM checkreq.vendor_requests vr
        JOIN checkreq.organizations o ON o.id = vr.org_id
        JOIN checkreq.payment_requests pr ON pr.id = vr.payment_request_id
        {where_sql}
        ORDER BY (vr.status = 'pending_approval') DESC, vr.created_at DESC
        """,
        params,
    )
    for r in rows:
        r["display_name"] = _vendor_request_display_name(r)

    return _render(request, "vendor_requests.html", user, {
        "rows": rows, "email_warning": email_warning,
        "all_orgs_list": all_orgs_list, "filter_entity": entity,
    })


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
                    post_error: str = "", email_warning: str = "", view: str = "pending",
                    entity: str = ""):
    """2026-08-02 feedback batch, Item 6: Jay assumed this screen was
    already scoped to a single entity and asked to drop its redundant
    Entity column -- checked the actual query and it is NOT: `_require_
    ap_reviewer` is deliberately cross-entity (org_id=None, "an AP reviewer
    sees every org's fully-chain-approved requests, org-wide") and neither
    the pending nor completed query below filters by org_id at all. The
    correct fix per the STANDING rule (Item 5, not Item 6) is an entity
    FILTER, keeping the column -- not removing it, which would make rows
    from different entities indistinguishable on a screen that genuinely
    spans all of them.

    2026-08-16, tightened further (critical-review finding): the gate above
    still deliberately stays a plain existence check ("holds ap_reviewer
    somewhere") so someone granted it at several entities keeps their real,
    current one-screen workflow -- but the two queries below now ALSO
    filter to `rbac.get_granted_org_ids()`, the actual set of orgs THIS
    user holds ap_reviewer at. Before this, ANY ap_reviewer anywhere saw
    EVERY org's pending/completed requests unconditionally -- a real leak
    once a narrowly-granted parish-org approver can exist. The optional
    `entity=` filter above still works as a convenience layered on top of
    that set, same as before."""
    user, err = _require_ap_reviewer(request)
    if err:
        return err
    tile_badges.mark_viewed(user["id"], "ap_review")

    granted_org_ids = rbac.get_granted_org_ids(user["id"], "ap_reviewer")
    all_orgs_list = db.query("SELECT code, name FROM checkreq.organizations WHERE is_active ORDER BY name")

    # Jay, 2026-07-29: "some sort of need in the AP review to also have a
    # completed tab as well." A request leaves this queue the moment it's
    # posted (status flips to 'Posted to QBO') with no way to look back at
    # it from here -- same gap as My Approvals' own history request.
    if view == "completed":
        where_sql = "WHERE pr.status = 'Posted to QBO' AND pr.org_id = ANY(%s)"
        params: tuple = (granted_org_ids,)
        if entity:
            where_sql += " AND o.code = %s"
            params = (granted_org_ids, entity)
        completed_rows = db.query(
            f"""
            SELECT pr.request_number, pr.request_type, pr.amount, pr.qbo_bill_id,
                   pr.qbo_bill_url, pr.updated_at, o.code AS org_code,
                   COALESCE(pa.title, 'All Program Areas') AS program_area_title,
                   v.display_name AS vendor_display_name,
                   vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
                   vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
                   vr.dba_name AS vr_dba_name
            FROM checkreq.payment_requests pr
            JOIN checkreq.organizations o ON o.id = pr.org_id
            LEFT JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
            LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
            LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
            {where_sql}
            ORDER BY pr.updated_at DESC
            """,
            params,
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
        return _render(request, "ap_review_completed.html", user, {
            "rows": completed_rows, "all_orgs_list": all_orgs_list, "filter_entity": entity,
        })

    pending_where = "WHERE pr.status = 'Approved' AND pr.org_id = ANY(%s)"
    pending_params: tuple = (granted_org_ids,)
    if entity:
        pending_where += " AND o.code = %s"
        pending_params = (granted_org_ids, entity)

    rows = db.query(
        f"""
        SELECT pr.id AS pr_id, pr.request_number, pr.request_type, pr.amount,
               pr.approval_chain_summary, pr.created_at, pr.vendor_request_id,
               pr.overspend_flagged, pr.overspend_detail,
               o.code AS org_code, o.name AS org_name,
               COALESCE(pa.title, 'All Program Areas') AS program_area_title, u.display_name AS submitter_name,
               u.email AS submitter_email,
               v.display_name AS vendor_display_name,
               vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
               vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
               vr.dba_name AS vr_dba_name, vr.status AS vr_status,
               vr.requires_w9 AS vr_requires_w9, vr.w9_received AS vr_w9_received
        FROM checkreq.payment_requests pr
        JOIN checkreq.organizations o ON o.id = pr.org_id
        LEFT JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
        JOIN checkreq.app_users u ON u.id = pr.submitter_user_id
        LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
        LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
        {pending_where}
        ORDER BY pr.created_at
        """,
        pending_params,
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

    # Ask My Accountant (2026-08-16): requests waiting on AP to assign GL
    # coding before the approval chain can even start -- same screen/role
    # as the rest of AP Review, per Jay's explicit answer ("the same people
    # who already do AP Review... add a filter, not a new role"), scoped to
    # the same granted_org_ids as the pending queue above.
    coding_where = "WHERE pr.status = 'AwaitingCoding' AND pr.org_id = ANY(%s)"
    coding_params: tuple = (granted_org_ids,)
    if entity:
        coding_where += " AND o.code = %s"
        coding_params = (granted_org_ids, entity)
    coding_rows = db.query(
        f"""
        SELECT pr.id AS pr_id, pr.request_number, pr.request_type, pr.amount,
               pr.created_at, o.code AS org_code,
               COALESCE(pa.title, 'All Program Areas') AS program_area_title,
               u.display_name AS submitter_name, u.email AS submitter_email,
               v.display_name AS vendor_display_name,
               vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
               vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
               vr.dba_name AS vr_dba_name
        FROM checkreq.payment_requests pr
        JOIN checkreq.organizations o ON o.id = pr.org_id
        LEFT JOIN checkreq.program_areas pa ON pa.id = pr.program_area_id
        JOIN checkreq.app_users u ON u.id = pr.submitter_user_id
        LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
        LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
        {coding_where}
        ORDER BY pr.created_at
        """,
        coding_params,
    )
    for r in coding_rows:
        if r.get("vendor_display_name"):
            r["vendor_name"] = r["vendor_display_name"]
        elif r.get("vr_entity_type"):
            r["vendor_name"] = _vendor_request_row_display_name(
                r["vr_entity_type"], r["vr_company_name"], r["vr_dba_name"],
                r["vr_first_name"], r["vr_last_name"],
            )
        else:
            r["vendor_name"] = "—"

    return _render(request, "ap_review.html", user, {
        "rows": rows, "coding_rows": coding_rows, "posted": posted, "returned": returned,
        "post_error": post_error, "email_warning": email_warning,
        "all_orgs_list": all_orgs_list, "filter_entity": entity,
    })


@app.get("/requests/{request_number}/gl-account-options")
def gl_account_options_for_request(request_number: str, request: Request):
    """Tom Select's async data source for the GL Coding picker on the
    Needs-Coding row below -- reuses the exact same /api/gl-accounts data
    the New Request form's own picker already calls, just resolved from the
    request's own org instead of the session's current one (an AP reviewer
    coding a request may not have that entity selected)."""
    user, err = _require_ap_reviewer(request)
    if err:
        return err
    pr = db.query_one(
        "SELECT org_id FROM checkreq.payment_requests WHERE request_number = %s",
        (request_number,),
    )
    if not pr:
        return JSONResponse({"error": "Request not found"}, status_code=404)
    return api_gl_accounts(pr["org_id"], request, q=request.query_params.get("q", ""))


@app.post("/requests/{request_number}/assign-gl-coding")
async def assign_gl_coding(request_number: str, request: Request):
    """Ask My Accountant, Stage 2: AP assigns the real GL line(s) to a
    request that skipped GL Coding at submission -- the moment the approval
    chain actually starts (approval_engine.build_approval_chain(), reused
    completely unchanged, per the original thread-log design). Deliberately
    does NOT re-derive self-payment/pre-approved/ART special-casing --
    those are submission-time bypasses for a normal request; an Ask My
    Accountant submitter didn't opt into any of them, so the plain,
    ordinary chain is what's expected here. Budget IS evaluated now (there
    was nothing to check before real GL lines existed), same tier-3
    CFO-group append the normal submission path already does."""
    user, err = _require_ap_reviewer(request)
    if err:
        return err

    pr = db.query_one(
        "SELECT * FROM checkreq.payment_requests WHERE request_number = %s",
        (request_number,),
    )
    if not pr:
        return JSONResponse({"error": "Request not found"}, status_code=404)
    if pr["status"] != "AwaitingCoding":
        return JSONResponse({"error": "This request is not awaiting GL coding."}, status_code=400)
    if pr["org_id"] not in rbac.get_granted_org_ids(user["id"], "ap_reviewer"):
        return JSONResponse({"error": "Not authorized for this entity."}, status_code=403)

    form = await request.form()
    gl_account_ids = form.getlist("gl_account_id")
    gl_amounts = form.getlist("gl_amount")
    gl_memos = form.getlist("gl_memo")
    gl_lines = [
        (int(a), float(amt), memo)
        for a, amt, memo in zip(gl_account_ids, gl_amounts, gl_memos)
        if a and amt
    ]
    if not gl_lines:
        return JSONResponse({"error": "Add at least one GL line."}, status_code=400)
    coded_total = round(sum(amt for _, amt, _ in gl_lines), 2)
    if abs(coded_total - float(pr["amount"])) > 0.01:
        return JSONResponse(
            {"error": f"GL lines must total ${pr['amount']:.2f} (the request's original amount) -- got ${coded_total:.2f}."},
            status_code=400,
        )

    org = db.query_one("SELECT id, code, name FROM checkreq.organizations WHERE id = %s", (pr["org_id"],))
    budget_result = _evaluate_gl_line_budgets(org, pr["program_area_id"], gl_lines)
    overspend_flagged = bool(budget_result["buffer_notice"] or budget_result["cfo_required"])
    overspend_detail = "\n".join(
        e["detail"] for e in budget_result["buffer_notice"] + budget_result["cfo_required"]
    ) or None

    chain = approval_engine.build_approval_chain(pr["program_area_id"], pr["org_id"], float(pr["amount"]))
    chain_summary = approval_engine.describe_chain(chain)
    if budget_result["cfo_required"]:
        next_group = (max((c["serial_group"] for c in chain), default=0)) + 1
        cfo_budget_group = [
            {"serial_group": next_group, "approver_user_id": u2["id"], "approver_email": u2["email"],
             "approver_name": u2["display_name"], "backup_approver_id": None, "any_one_suffices": True}
            for u2 in _cfo_approver_rows(user["id"], pr["org_id"])
        ]
        chain = chain + cfo_budget_group
        chain_summary += "\n" + (
            f"Group {next_group}: CFO approval required -- over budget beyond the account's allowed buffer."
            if cfo_budget_group else
            "Over budget beyond buffer -- no CFO configured to approve. Needs setup."
        )
    first_step = chain[0] if chain else None
    first_display_approver = _serial_group_display_approver(chain, first_step["serial_group"] if first_step else None)

    with db.connect() as conn:
        with conn.cursor() as cur:
            for acct_id, amt, memo in gl_lines:
                cur.execute(
                    "INSERT INTO checkreq.payment_request_gl_lines "
                    "(payment_request_id, gl_account_id, amount, memo) VALUES (%s, %s, %s, %s)",
                    (pr["id"], acct_id, amt, memo),
                )
            for tier, entries in (("buffer_notice", budget_result["buffer_notice"]),
                                   ("cfo_required", budget_result["cfo_required"])):
                for e in entries:
                    cur.execute(
                        "INSERT INTO checkreq.budget_overage_log "
                        "(payment_request_id, gl_account_id, tier, annual_budget, projected_spend, buffer_amount) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (pr["id"], e["gl_account_id"], tier, e["annual_budget"], e["projected"], e["buffer_amount"]),
                    )
            cur.execute(
                """
                UPDATE checkreq.payment_requests SET
                    status = 'UnderReview', current_approver_id = %s, serial_group_current = %s,
                    approval_chain_summary = %s, overspend_flagged = %s, overspend_detail = %s,
                    budget_checked_at = NOW(), updated_at = NOW()
                WHERE id = %s
                """,
                (first_display_approver, first_step["serial_group"] if first_step else None,
                 chain_summary, overspend_flagged, overspend_detail, pr["id"]),
            )
            _materialize_approval_actions(cur, pr["id"], chain)
            cur.execute(
                "INSERT INTO checkreq.audit_log "
                "(payment_request_id, action_by_user_id, action_type, comment, previous_status, new_status) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (pr["id"], user["id"], "GL Coding Assigned", chain_summary, "AwaitingCoding", "UnderReview"),
            )

    return RedirectResponse("/admin/ap-review?coded=1", status_code=303)


@app.post("/requests/{request_number}/post-to-qbo")
async def post_to_qbo(request_number: str, request: Request):
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

    # Dev/Prod Split Plan.md (2026-07-31), Decision 4: dev is allowed to post
    # real Bills/Vendors to the real, live production QuickBooks company --
    # there is no QBO sandbox to test against safely -- but only behind an
    # explicit, hard-to-miss confirmation naming the real company, never a
    # bare click. ap_review.html's confirm() dialog is the real UX; this is
    # the server-side backstop, so a replayed/direct POST that never showed
    # the dialog is refused rather than silently honored.
    if BEACON_ENV == "dev":
        form = await request.form()
        if form.get("dev_confirmed") != "1":
            return RedirectResponse(
                f"/admin/ap-review?post_error="
                f"{quote(request_number + ': dev-environment confirmation required before posting to the real, live QuickBooks.')}",
                status_code=303,
            )

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
        "SELECT id, submitter_user_id, program_area_id, org_id FROM checkreq.payment_requests WHERE request_number = %s",
        (request_number,),
    )
    if not pr:
        return JSONResponse({"error": "Request not found"}, status_code=404)

    allowed = (
        rbac.user_has_role(user["id"], "cfo", pr["org_id"])
        or pr["submitter_user_id"] == user["id"]
        or _user_can_submit_for(user, pr["program_area_id"], pr["org_id"])
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
        "SELECT id, submitter_user_id, program_area_id, status, org_id FROM checkreq.payment_requests "
        "WHERE request_number = %s",
        (request_number,),
    )
    if not pr:
        return JSONResponse({"error": "Request not found"}, status_code=404)

    is_approver = db.query_one(
        "SELECT 1 FROM checkreq.approval_actions WHERE payment_request_id = %s AND approver_user_id = %s",
        (pr["id"], user["id"]),
    )
    # RBAC (2026-08-01, tightened 2026-08-16): all four role checks scoped
    # to this request's own org. ap_reviewer/vendor_approver used to be
    # cross-entity (org_id=None) here -- a real, previously-unnoticed leak
    # that let any ap_reviewer/vendor_approver at ANY org read any OTHER
    # entity's voucher and attachment list. Matches request_pdf's own
    # (already-correct) scoping, and Jay's direction that every role check
    # is entity-scoped, no exceptions.
    allowed = (
        rbac.user_has_role(user["id"], "cfo", pr["org_id"])
        or pr["submitter_user_id"] == user["id"]
        or _user_can_submit_for(user, pr["program_area_id"], pr["org_id"])
        or bool(is_approver)
        or rbac.user_has_role(user["id"], "ap_reviewer", pr["org_id"])
        or rbac.user_has_role(user["id"], "vendor_approver", pr["org_id"])
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


# ── Admin > Setup Tables (prototype, 2026-08-01) ─────────────────────────────
# The Excel Setup Tables workbook ported into Beacon -- see admin_setup.py's
# own docstring and Admin Module Plan.md. Kept in its own module rather than
# appended here: this file is already ~4,700 lines, the new screens share no
# helpers with anything above, and a self-contained module is far easier to
# review, back out, or hand to a future session.
#
# register() (rather than a plain `app.include_router` at import time) is
# what keeps the dependency one-way -- admin_setup.py never imports main, it
# receives the three helpers it needs. Called last, after every helper it
# takes has actually been defined.
import admin_setup  # noqa: E402  (deliberately last -- see comment above)

admin_setup.register(
    app,
    current_user=_current_user,
    current_org=_current_org,
    render=_render,
)

# Cornerstone Served Parishes Plan.md Phase J (item 16) -- two view-only
# reference screens (GL Accounts, Vendors), same register()-injection
# pattern and same entity-scoped setup_admin gate as admin_setup.py above.
import gl_vendors_reference

gl_vendors_reference.register(
    app,
    current_user=_current_user,
    current_org=_current_org,
    render=_render,
)

# RBAC (2026-08-01, Role-Based Access Control Plan.md §9/§6). Same
# register()-injection pattern as admin_setup.py -- each module owns one
# concern (self-service access requests vs. the Users & Roles admin screen)
# and stays under ~250 lines instead of growing this file further.
#
# Migration 019_rbac.sql applied to production 2026-08-01; Stages 2-6
# (rbac.py, route guards, row-level checks, program-area bypass, recipient
# queries, identity path) verified against real data via run_dev.py before
# wiring these two modules in -- see Role-Based Access Control Plan.md §7.
import access_requests
import admin_users
import admin_hub
import parish_access
import account

access_requests.register(app, current_user=_current_user, render=_render)
admin_users.register(app, current_user=_current_user, current_org=_current_org, render=_render)
admin_hub.register(app, current_user=_current_user, current_org=_current_org, render=_render)
# Parish Portal S3 (Parish Portal Plan.md Section 2/5) -- same register()
# pattern as the three lines above, thin wiring only.
parish_access.register(app, current_user=_current_user, render=_render)
# account.py (2026-08-08) -- closes the "set_password() has no UI caller" gap.
account.register(app, current_user=_current_user, render=_render)
# Parish Portal S4, "Diocese Mode / Parish Mode" (2026-08-08) -- same
# register() pattern, thin wiring only.
parish_mode.register(app, current_user=_current_user, current_org=_current_org, render=_render)
# Cornerstone Served Parishes Phase A (Cornerstone Served Parishes Plan.md) --
# same register() pattern as everything else, thin wiring only.
parish_org_admin.register(app, current_user=_current_user, current_org=_current_org, render=_render)
cornerstone_mode.register(app, current_user=_current_user, current_org=_current_org, render=_render)
# Parish Portal S4+S5 (2026-08-08): announcements, document archive/library,
# and parish feedback/general-requests -- three more new modules, same thin
# register() wiring, no logic added here.
parish_documents.register(app, current_user=_current_user, current_org=_current_org, render=_render)
cornerstone_documents.register(app, current_user=_current_user, current_org=_current_org)
announcements.register(app, current_user=_current_user, current_org=_current_org, render=_render)
parish_requests.register(app, current_user=_current_user, render=_render)
# 2026-08-08 feedback batch: live Databank contact info, replacing the plain
# Diocese/City/Served Tier/Status card on parish_view.html.
parish_info.register(app, current_user=_current_user, render=_render)
# Parish Portal Plan.md S6/S7 addendum (2026-08-16): SMA status/statement,
# Middendorf loan progress, AP payments processed, Ask the Business Office,
# SMA direct-debit enrollment. Thin wiring only -- all logic lives in
# parish_finance.py itself.
parish_finance.register(app, current_user=_current_user, render=_render)

# Timekeeping HR Roster Review Plan.md (2026-08-16), finished and wired live
# same day it was gated on the new org_features "timekeeping" flag (any
# parish under a diocese with the flag on, not just Cornerstone-served --
# see timekeeping.py's own docstring) -- timekeeping_roster.register()/
# timekeeping_entries.register() take ONLY `render` (no current_user/
# current_org kwarg -- they resolve the parish context internally via
# timekeeping.timekeeping_context()); passing either extra kwarg to those
# two raises TypeError at import time.
timekeeping.register(app, current_user=_current_user, current_org=_current_org, render=_render)
# HR Activation -- the parish-level half of the two-level Timekeeping gate
# (2026-08-17). Same current_user/current_org/render signature as
# timekeeping.py itself, since its screen is diocese-scoped the same way.
timekeeping_activation.register(app, current_user=_current_user, current_org=_current_org, render=_render)
# Diocese-side HR employee management (2026-08-17). Same signature as its
# timekeeping_* siblings; its own _require_hr_admin() does the gating.
timekeeping_employees.register(app, current_user=_current_user, current_org=_current_org, render=_render)
timekeeping_roster.register(app, render=_render)
timekeeping_entries.register(app, render=_render)
timekeeping_review.register(app, current_user=_current_user, current_org=_current_org, render=_render)
# Diocese status board / "enter missing data" backfill / Excel export
# (2026-08-16, per Jay's direct request) -- new sibling module, same
# current_user/current_org/render signature as timekeeping.py/
# timekeeping_review.py (it needs _current_org() to resolve the diocese,
# unlike the two parish-context-only modules above).
timekeeping_status.register(app, current_user=_current_user, current_org=_current_org, render=_render)

# In-App Notifications (2026-08-02, In-App Notifications Plan.md).
# create_notification()/get_unread_count() are already in use above (this
# module was imported near the top, alongside db/rbac) -- this call only
# wires in the two HTTP routes the bell's own dropdown JS calls.
notifications.register(app, current_user=_current_user)

# Conversational Feedback intake (2026-08-02) -- see feedback_chat.py's own
# docstring for the full design/hard-boundary rationale. Registered last
# among the feature modules for the same reason as the others: it needs
# _current_user/_current_org/_render already defined above.
import feedback_chat

feedback_chat.register(app, current_user=_current_user, current_org=_current_org, render=_render)
