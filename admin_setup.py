"""
admin_setup.py — Beacon Admin > Setup Tables module (PROTOTYPE, 2026-08-01;
Program Areas / Approval Rules / User Assignments added 2026-08-02).

Ports `Tools\\26-129 Check Request Setup Tables.xlsm` into Beacon itself. See
`Admin Module Plan.md` in this project's root for the full design, the
page-by-page mapping of the tabs NOT built here, and the open questions.

Built so far (real, working, wired to live checkreq Postgres):
  GET  /admin/setup                              — module index (all 8 tabs, status + row counts)
  GET  /admin/setup/gl-mapping                   — Program Area <-> GL Account map (workbook tab: ProgramAreaGLAccounts)
  POST /admin/setup/gl-mapping/save              — bulk save of edited rows (JSON); a row's
                                                    `_delete: true` flag deletes it here too
                                                    (no separate delete route -- 2026-08-02)
  POST /admin/setup/gl-mapping/add               — create one mapping (JSON)
  GET  /admin/setup/api/unmapped-gl-accounts     — dependent GL picker feed (Tom Select)
  GET  /admin/setup/organizations                — Organizations + GlobalApprovers (workbook tabs: Organizations, GlobalApprovers)
  POST /admin/setup/organizations/save-orgs      — per-entity Global Approval Threshold (JSON)
  POST /admin/setup/organizations/save-approvers — global approver rows (JSON); a row's
                                                    `_delete: true` flag deletes it here too

  Program Areas / Approval Rules / User-Program-Area Assignments -- three
  workbook tabs folded into ONE screen pair per Admin Module Plan.md's own
  recommendation ("They're separate tabs in the workbook only because a
  spreadsheet can't nest one table inside another... a single Program Area
  detail page: the area's own fields at the top, then 'Who can submit' and
  'Who approves' as two panels beneath"):
  GET  /admin/setup/program-areas                          — list (workbook tab: ProgramAreas)
  POST /admin/setup/program-areas/add                      — create one program area (JSON)
  GET  /admin/setup/program-areas/{id}                     — detail: the area's own fields,
                                                              Who Can Submit (UserProgramAreas),
                                                              Who Approves (ApprovalRules)
  POST /admin/setup/program-areas/{id}/update              — the area's own fields (Title,
                                                              Description, Sort Order, Active)
  POST /admin/setup/program-areas/{id}/submitters/grant    — grant one user submit access
                                                              (immediate, single-row -- same
                                                              convention as admin_users.py's
                                                              own grant-pa/revoke-pa)
  POST /admin/setup/program-areas/{id}/submitters/revoke   — revoke one user's submit access
  POST /admin/setup/program-areas/{id}/approval-rules/save — bulk save of Who Approves rows
                                                              (JSON); a row's `_delete: true`
                                                              flag deletes it, same batched
                                                              dirty-save pattern as GL Mapping
                                                              and Global Approvers above

ARCHITECTURE — why this talks to Postgres directly instead of calling
qbo-mcp-server's /api/checkreq/* endpoints (which the workbook uses):

Those endpoints exist because VBA has no Postgres driver — they are an HTTP
shim built FOR the workbook, not a shared service layer. Beacon already
holds a direct, pooled Cloud SQL connection to the very same `checkreq`
schema (db.py), and every one of main.py's ~40 routes reads and writes these
tables directly. Routing admin reads through a second Cloud Run service
would add a network hop, a cold-start failure mode (this project has already
been bitten once by a 30s cold-start timeout against 26-122), and an
admin-level X-API-Key whose `allowed_companies=["*"]` grant is strictly
weaker than the per-user `is_cfo` + session-entity gate applied here.

The decisive argument is correctness, not plumbing: qbo-mcp-server's
`save_program_area_gl_account()` resolves a mapping from two human-typed
STRINGS (program area title, GL account number) because a spreadsheet cell
is all VBA has. That string round-trip is what produced the 2026-07-25
trailing-zero incident — Excel silently turned "6680.070" into 6680.07 and
17 real rows failed a lookup that should have succeeded. A web form carries
the real integer FKs from the picker straight through, so that entire bug
class cannot occur here. Writes below therefore take `program_area_id` /
`gl_account_id`, and independently re-verify both belong to the caller's
session org before touching anything (row ids arrive from the client and are
never trusted).

Everything Beacon genuinely CANNOT do itself still goes through
qbo_mcp_client as before — the on-demand budget lookup on the GL mapping
screen reuses main.py's existing /api/budget-status route verbatim, since
only qbo-mcp-server holds the QBO OAuth tokens.
"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import art_completeness
import db
import rbac

router = APIRouter()

# main.py owns the identity/entity/render helpers; register() below injects
# them so this module never imports main (which imports this one).
_current_user = None
_current_org = None
_render = None


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


# ── The eight workbook tabs, and where each one stands ───────────────────────
# Drives the /admin/setup index. "built" tabs link to a real screen; the rest
# render as planned placeholders rather than dead links, matching how
# portal.html already handles its own not-yet-built module tiles.
SETUP_TABS = [
    {"key": "program_areas", "title": "Program Areas",
     "desc": "The grouping every GL account, approval rule and user assignment hangs off. "
             "Each area's own page also manages its approvers and submitters.",
     "url": "/admin/setup/program-areas", "built": True, "entity_scoped": True,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.program_areas WHERE org_id = %s"},
    {"key": "gl_mapping", "title": "Program Area / GL Account Map",
     "desc": "Which GL accounts a program area may code to, their display label, hierarchy and overspend buffer.",
     "url": "/admin/setup/gl-mapping", "built": True, "entity_scoped": True,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.program_area_gl_accounts pga "
                  "JOIN checkreq.program_areas pa ON pa.id = pga.program_area_id WHERE pa.org_id = %s"},
    {"key": "approval_rules", "title": "Approval Rules",
     "desc": "Per program area: approver, limit, serial group, must-approve threshold, backup. "
             "Manage from a program area's own detail page (below), or from a person's own "
             "page on User Management.",
     "url": "/admin/setup/program-areas", "built": True, "entity_scoped": True,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.approval_rules ar "
                  "JOIN checkreq.program_areas pa ON pa.id = ar.program_area_id WHERE pa.org_id = %s"},
    {"key": "user_program_areas", "title": "User / Program Area Assignments",
     "desc": "Who is allowed to submit against which program area. Manage from a program "
             "area's own detail page (below).",
     "url": "/admin/setup/program-areas", "built": True, "entity_scoped": True,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.user_program_areas upa "
                  "JOIN checkreq.program_areas pa ON pa.id = upa.program_area_id WHERE pa.org_id = %s"},
    # Added 2026-08-02 — Invoice Processing Intake Plan.md, "The ART /
    # Preapproved screen" + "ART completeness tracking" sections. Ports the
    # real CFM AP Recurring Bills spreadsheet's per-vendor ART settings, and
    # a manual on-demand version of the "did this period's bill actually
    # post to QBO" check (the nightly-automated version is deliberately not
    # built yet -- see art_completeness.py's own module docstring).
    {"key": "art_list", "title": "ART List (Recurring Bills)",
     "desc": "Vendors pre-approved for recurring payment: their expected amount/frequency, "
             "GL coding, and whether each period's bill has actually posted to QBO.",
     "url": "/admin/setup/art", "built": True, "entity_scoped": True,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.art_list WHERE org_id = %s"},
    {"key": "organizations", "title": "Entities & Global Approvers",
     "desc": "Per-entity global approval threshold, and who signs off above it.",
     "url": "/admin/setup/organizations", "built": True, "entity_scoped": False,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.organizations"},
    {"key": "gl_accounts", "title": "GL Accounts (reference)",
     "desc": "Chart of accounts, synced nightly from QBO. Read-only.",
     "url": "/admin/setup/gl-accounts", "built": True, "entity_scoped": True,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.gl_accounts WHERE org_id = %s"},
    {"key": "vendors", "title": "Vendors (reference)",
     "desc": "Vendor list, synced nightly from QBO. Read-only.",
     "url": "/admin/setup/vendors", "built": True, "entity_scoped": True,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.vendors WHERE org_id = %s"},
    # Added 2026-08-01, RBAC build (admin_users.py) -- live since
    # migrations/019_rbac.sql was applied and admin_users.py wired in.
    # Renamed "Users & Roles" -> "User Management" 2026-08-02 (Jay: it "has
    # identity, roles, program assignments, and ... approval rules for
    # them" -- a label change only, admin_users.py/its templates keep their
    # existing filenames).
    {"key": "users", "title": "User Management",
     "desc": "Who can sign in, and what each person may do in each entity.",
     "url": "/admin/setup/users", "built": True, "entity_scoped": False,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.app_users WHERE is_active"},
]


def _require_setup_admin(request: Request):
    """(user, None) when allowed, (None, response) when not — same shape as
    main.py's own _require_vendor_approver/_require_ap_reviewer.

    2026-08-16 fix: this used to check the legacy app_users.is_cfo boolean,
    which nothing has written to since the 2026-08-01 RBAC migration --
    rbac.grant_role()/revoke_role() only ever touch checkreq.user_roles.
    That meant anyone granted cfo/setup_admin through the modern Users &
    Roles screen was silently locked out of this entire Setup Tables module,
    while anyone with a legacy is_cfo=TRUE row kept standing access forever
    even after their role was later revoked through RBAC.

    Also entity-scoped (was org_id=None): unlike most of this app's other
    admin screens (Organizations/Global Approvers/Users & Roles/Access
    Requests/Vendor Approvals, all deliberately or necessarily cross-entity
    -- see admin_hub.py's own docstring), the DATA underneath Setup Tables
    (Program Areas, GL Mapping) is genuinely per-entity already -- every
    query in this file already filters by the current session org. The
    entry gate now matches that, so a setup_admin at Org A can't reach
    Org B's setup screens just by having the role somewhere. No selected
    entity means no access, explicitly -- never let org_id=None fall
    through to rbac.py's "check every org" meaning."""
    user = _current_user(request)
    if not user:
        return None, RedirectResponse("/login")
    org = _current_org(request)
    org_id = org["id"] if org else None
    if org_id is None or not rbac.user_has_role(user["id"], "setup_admin", org_id=org_id):
        return None, JSONResponse({"error": "Setup Admin access required"}, status_code=403)
    return user, None


def _sort_depth(sort_order: str | None) -> int:
    """Indentation depth from dot-notation SortOrder — literal dot count,
    identical to new_request.js's glAccountDepth() and to the ORDER BY both
    main.py and qbo-mcp-server already use. A malformed value renders flush
    left rather than erroring (same tolerance the SQL side applies)."""
    s = (sort_order or "").strip()
    if not s:
        return 0
    parts = s.split(".")
    if not all(p.isdigit() for p in parts):
        return 0
    return len(parts) - 1


def _money(value) -> float:
    return float(value or 0)


class _RowSavepoint:
    """Per-row savepoint for the bulk-save loops.

    Both loops report success/failure per row, which only holds if one bad
    row can't take the others down with it. Python-side validation errors
    (a non-numeric buffer, say) are raised before any SQL runs and are
    harmless, but a real database error — a constraint violation, a FK miss
    — aborts the whole transaction in psycopg, and every subsequent
    statement then fails with "current transaction is aborted". Without
    this, one such row would silently turn every later row's result into a
    bogus error. Rolling back to a savepoint releases just that row."""

    def __init__(self, cur, name: str):
        self.cur, self.name = cur, name

    def __enter__(self):
        self.cur.execute(f"SAVEPOINT {self.name}")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.cur.execute(f"RELEASE SAVEPOINT {self.name}")
        else:
            self.cur.execute(f"ROLLBACK TO SAVEPOINT {self.name}")
        return False  # never swallow — the caller's own except decides


# ── Module index ─────────────────────────────────────────────────────────────

@router.get("/admin/setup", response_class=HTMLResponse)
def setup_index(request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)

    tabs = []
    for t in SETUP_TABS:
        row = None
        if org or not t["entity_scoped"]:
            params = (org["id"],) if t["entity_scoped"] else ()
            row = db.query_one(t["count_sql"], params)
        tabs.append({**t, "count": (row or {}).get("n")})

    return _render(request, "admin_setup_index.html", user, {"tabs": tabs})


# ── Program Area / GL Account map ────────────────────────────────────────────

_GL_MAPPING_SQL = r"""
    SELECT pga.id, pga.program_area_id, pga.gl_account_id,
           pa.title AS program_area_title,
           ga.account_number, ga.account_name,
           pga.display_text,
           COALESCE(NULLIF(pga.display_text, ''), ga.account_name) AS effective_display_text,
           pga.allow_post, pga.sort_order, pga.overspend_buffer_amount
    FROM checkreq.program_area_gl_accounts pga
    JOIN checkreq.program_areas pa ON pa.id = pga.program_area_id
    JOIN checkreq.gl_accounts ga ON ga.id = pga.gl_account_id
    WHERE pa.org_id = %s
    ORDER BY pa.sort_order, pa.title,
             CASE WHEN pga.sort_order ~ '^[0-9]+(\.[0-9]+)*$' THEN 0 ELSE 1 END,
             CASE WHEN pga.sort_order ~ '^[0-9]+(\.[0-9]+)*$'
                  THEN string_to_array(pga.sort_order, '.')::int[] ELSE NULL END,
             ga.account_number
"""


def _gl_mapping_groups(org_id: int) -> list[dict]:
    """Rows grouped by program area, in exactly the order the check-request
    GL picker itself renders them (same ORDER BY as main.py's
    /api/gl-accounts and qbo-mcp-server's get_program_area_gl_accounts) —
    so what an admin sees here is literally what a submitter will see."""
    rows = db.query(_GL_MAPPING_SQL, (org_id,))
    groups: list[dict] = []
    by_area: dict[int, dict] = {}
    for r in rows:
        g = by_area.get(r["program_area_id"])
        if g is None:
            g = {"program_area_id": r["program_area_id"],
                 "title": r["program_area_title"], "rows": []}
            by_area[r["program_area_id"]] = g
            groups.append(g)
        g["rows"].append({
            **r,
            "depth": _sort_depth(r["sort_order"]),
            "overspend_buffer_amount": _money(r["overspend_buffer_amount"]),
        })
    return groups


@router.get("/admin/setup/gl-mapping", response_class=HTMLResponse)
def gl_mapping_page(request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org:
        return RedirectResponse("/portal")

    program_areas = db.query(
        "SELECT id, title FROM checkreq.program_areas "
        "WHERE org_id = %s AND is_active ORDER BY sort_order, title",
        (org["id"],),
    )
    return _render(request, "admin_setup_gl_mapping.html", user, {
        "groups": _gl_mapping_groups(org["id"]),
        "program_areas": program_areas,
    })


@router.get("/admin/setup/api/unmapped-gl-accounts")
def api_unmapped_gl_accounts(request: Request, program_area_id: int, q: str = ""):
    """Feed for the Add-mapping GL picker. Dependent on the selected program
    area in a way the spreadsheet's flat GLAccounts-column dropdown can't be:
    accounts ALREADY mapped to that area are excluded, so the silent
    ON CONFLICT DO NOTHING "unchanged" no-op the workbook reports on a
    duplicate simply can't be reached from the UI."""
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org:
        return JSONResponse({"error": "No entity selected"}, status_code=400)

    sql = """
        SELECT ga.id, ga.account_number, ga.account_name, ga.account_type
        FROM checkreq.gl_accounts ga
        WHERE ga.org_id = %s AND ga.is_active
          AND NOT EXISTS (
              SELECT 1 FROM checkreq.program_area_gl_accounts pga
              WHERE pga.gl_account_id = ga.id AND pga.program_area_id = %s
          )
    """
    params: tuple = (org["id"], program_area_id)
    if q:
        sql += " AND (ga.account_number ILIKE %s OR ga.account_name ILIKE %s)"
        params += (f"%{q}%", f"%{q}%")
    sql += " ORDER BY ga.account_number LIMIT 100"
    return db.query(sql, params)


@router.post("/admin/setup/gl-mapping/save")
async def gl_mapping_save(request: Request):
    """Bulk save of whichever rows the page marked dirty. Per-row results,
    not one aggregate message — this is the web equivalent of the workbook's
    hard-won per-row "Last Save Error" column (added 2026-07-25 after a
    single status cell hid 17 distinct real failures behind the last one),
    except the result lands on the row itself instead of in column K.

    2026-08-02 feedback batch, Item 8: a row can also carry `_delete: true`
    -- the "x" button no longer deletes immediately (Jay hit this directly:
    "I just deleted one that I didn't wanna delete"). It now only marks the
    row dirty client-side; the actual DELETE happens here, in the same
    batched, entity-ownership-checked, savepoint-isolated save as every
    other edit, only once Save Changes is clicked. The standalone
    `POST /admin/setup/gl-mapping/{row_id}/delete` route this replaced is
    removed entirely, not just unused, so that old immediate-delete
    behavior can never be hit again by accident."""
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org:
        return JSONResponse({"error": "No entity selected"}, status_code=400)

    body = await request.json()
    rows = body.get("rows") or []
    results = []

    with db.connect() as conn:
        with conn.cursor() as cur:
            for i, r in enumerate(rows):
                row_id = r.get("id")
                try:
                    with _RowSavepoint(cur, f"sp_{i}"):
                        # Row ids come from the client — re-verify ownership
                        # against the session entity before writing, never trust.
                        cur.execute(
                            "SELECT pga.id FROM checkreq.program_area_gl_accounts pga "
                            "JOIN checkreq.program_areas pa ON pa.id = pga.program_area_id "
                            "WHERE pga.id = %s AND pa.org_id = %s",
                            (row_id, org["id"]),
                        )
                        if not cur.fetchone():
                            raise ValueError("This mapping isn't part of the selected entity.")

                        if r.get("_delete"):
                            cur.execute(
                                "DELETE FROM checkreq.program_area_gl_accounts WHERE id = %s",
                                (row_id,),
                            )
                            results.append({"id": row_id, "ok": True, "deleted": True})
                            continue

                        sort_order = (r.get("sort_order") or "").strip() or None
                        display_text = (r.get("display_text") or "").strip() or None
                        try:
                            buffer_amount = float(r.get("overspend_buffer_amount") or 0)
                        except (TypeError, ValueError):
                            raise ValueError("Overspend buffer must be a number.")
                        if buffer_amount < 0:
                            raise ValueError("Overspend buffer can't be negative.")

                        cur.execute(
                            "UPDATE checkreq.program_area_gl_accounts "
                            "SET display_text = %s, allow_post = %s, sort_order = %s, "
                            "    overspend_buffer_amount = %s "
                            "WHERE id = %s",
                            (display_text, bool(r.get("allow_post")), sort_order,
                             buffer_amount, row_id),
                        )
                    results.append({"id": row_id, "ok": True})
                except Exception as exc:
                    results.append({"id": row_id, "ok": False, "error": str(exc)})

    saved = sum(1 for x in results if x["ok"])
    return {"saved": saved, "failed": len(results) - saved, "results": results}


@router.post("/admin/setup/gl-mapping/add")
async def gl_mapping_add(request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org:
        return JSONResponse({"error": "No entity selected"}, status_code=400)

    body = await request.json()
    try:
        program_area_id = int(body.get("program_area_id") or 0)
        gl_account_id = int(body.get("gl_account_id") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"error": "Pick a program area and a GL account."}, status_code=400)
    if not program_area_id or not gl_account_id:
        return JSONResponse({"error": "Pick a program area and a GL account."}, status_code=400)

    display_text = (body.get("display_text") or "").strip() or None
    sort_order = (body.get("sort_order") or "").strip() or None
    allow_post = bool(body.get("allow_post", True))
    try:
        buffer_amount = float(body.get("overspend_buffer_amount") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"error": "Overspend buffer must be a number."}, status_code=400)

    with db.connect() as conn:
        with conn.cursor() as cur:
            # Both FKs must belong to the session entity. Checked here rather
            # than relying on the FK alone: the FKs point at program_areas /
            # gl_accounts, neither of which constrains the pair to one org.
            cur.execute("SELECT 1 FROM checkreq.program_areas WHERE id = %s AND org_id = %s",
                        (program_area_id, org["id"]))
            if not cur.fetchone():
                return JSONResponse({"error": "That program area isn't part of the selected entity."},
                                    status_code=400)
            cur.execute("SELECT 1 FROM checkreq.gl_accounts WHERE id = %s AND org_id = %s",
                        (gl_account_id, org["id"]))
            if not cur.fetchone():
                return JSONResponse({"error": "That GL account isn't part of the selected entity."},
                                    status_code=400)

            cur.execute(
                "INSERT INTO checkreq.program_area_gl_accounts "
                "(program_area_id, gl_account_id, display_text, allow_post, sort_order, "
                " overspend_buffer_amount) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (program_area_id, gl_account_id) DO NOTHING RETURNING id",
                (program_area_id, gl_account_id, display_text, allow_post, sort_order,
                 buffer_amount),
            )
            created = cur.fetchone()

    if not created:
        # The workbook reports this as "unchanged", which reads as "nothing
        # happened" — say what actually happened instead.
        return JSONResponse(
            {"error": "That GL account is already mapped to this program area."},
            status_code=400,
        )
    return {"id": created["id"]}


# The standalone POST /admin/setup/gl-mapping/{row_id}/delete route (an
# immediate, un-confirmed-until-too-late real DELETE) was removed entirely
# 2026-08-02 -- Jay hit its exact failure mode live ("I just deleted one
# that I didn't wanna delete") and asked for delete to behave like every
# other edit: mark dirty, don't touch the database until Save Changes.
# That's now handled inside gl_mapping_save() above via a per-row
# `_delete: true` flag -- see its own docstring.


# ── Entities & Global Approvers ──────────────────────────────────────────────
# Deliberately NOT entity-scoped, matching the workbook: Organizations is the
# master list, and GlobalApprovers is org-wide with each row naming the entity
# it fires for. Filtering either by the session entity would hide exactly the
# rows an admin comes here to compare.

def _global_approver_rows() -> list[dict]:
    rows = db.query(
        """
        SELECT ga.id, u.email AS approver_email, ga.serial_group,
               bu.email AS backup_approver_email, ga.threshold_amount,
               ga.is_active, ga.org_id, o.code AS org_code
        FROM checkreq.global_approvers ga
        JOIN checkreq.app_users u ON u.id = ga.approver_user_id
        LEFT JOIN checkreq.app_users bu ON bu.id = ga.backup_approver_id
        LEFT JOIN checkreq.organizations o ON o.id = ga.org_id
        ORDER BY o.code NULLS FIRST, ga.serial_group, u.email
        """
    )
    for r in rows:
        r["threshold_amount"] = _money(r["threshold_amount"])
    return rows


@router.get("/admin/setup/organizations", response_class=HTMLResponse)
def organizations_page(request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err

    orgs = db.query(
        "SELECT id, code, name, is_active, global_approval_threshold "
        "FROM checkreq.organizations ORDER BY code"
    )
    for o in orgs:
        o["global_approval_threshold"] = _money(o["global_approval_threshold"])

    approvers = _global_approver_rows()
    # A row with no entity is never appended to any chain by
    # approval_engine.build_approval_chain() — surface that as a real warning
    # rather than leaving it to be inferred from a blank cell.
    unassigned = [a for a in approvers if not a["org_id"]]

    known_emails = db.query(
        "SELECT email FROM checkreq.app_users WHERE is_active ORDER BY email"
    )
    return _render(request, "admin_setup_organizations.html", user, {
        "orgs": orgs,
        "approvers": approvers,
        "unassigned_count": len(unassigned),
        "known_emails": [e["email"] for e in known_emails],
    })


@router.post("/admin/setup/organizations/save-orgs")
async def organizations_save_orgs(request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    body = await request.json()
    results = []
    with db.connect() as conn:
        with conn.cursor() as cur:
            for i, r in enumerate(body.get("rows") or []):
                row_id = r.get("id")
                try:
                    with _RowSavepoint(cur, f"sp_org_{i}"):
                        try:
                            threshold = float(r.get("global_approval_threshold") or 0)
                        except (TypeError, ValueError):
                            raise ValueError("Threshold must be a number.")
                        if threshold < 0:
                            raise ValueError("Threshold can't be negative.")
                        # code/name are reference data, same as the workbook —
                        # global_approval_threshold is the only writable field.
                        cur.execute(
                            "UPDATE checkreq.organizations SET global_approval_threshold = %s WHERE id = %s",
                            (threshold, row_id),
                        )
                    results.append({"id": row_id, "ok": True})
                except Exception as exc:
                    results.append({"id": row_id, "ok": False, "error": str(exc)})
    saved = sum(1 for x in results if x["ok"])
    return {"saved": saved, "failed": len(results) - saved, "results": results}


def _get_or_create_user(cur, email: str) -> int:
    """Mirrors qbo-mcp-server checkreq_api._get_or_create_user() exactly, so
    the two admin surfaces behave identically while both exist.

    Note this deliberately DOES insert an app_users row for an unrecognised
    email — unlike main.py's _complete_login(), which is INSERT-free on
    purpose. The distinction is intent: a login must never silently provision
    an account, whereas an admin typing an approver's address here is
    explicitly asking for that person to exist. Same behaviour the workbook
    has always had."""
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("Approver email is required.")
    cur.execute("SELECT id FROM checkreq.app_users WHERE LOWER(email) = %s", (email,))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur.execute(
        "INSERT INTO checkreq.app_users (email, display_name) VALUES (%s, %s) RETURNING id",
        (email, email.split("@")[0]),
    )
    return cur.fetchone()["id"]


@router.post("/admin/setup/organizations/save-approvers")
async def organizations_save_approvers(request: Request):
    """2026-08-02 feedback batch, Item 12.1: the same mark-for-removal-until-
    Save-Changes rule as gl_mapping_save -- a row's `_delete: true` deletes it
    here too, batched with everything else. Standalone
    POST /admin/setup/global-approvers/{row_id}/delete removed entirely."""
    user, err = _require_setup_admin(request)
    if err:
        return err
    body = await request.json()
    results = []

    with db.connect() as conn:
        with conn.cursor() as cur:
            for i, r in enumerate(body.get("rows") or []):
                row_id = r.get("id")  # None/blank => create
                new_id = None
                try:
                    with _RowSavepoint(cur, f"sp_appr_{i}"):
                        if r.get("_delete"):
                            if row_id:
                                cur.execute(
                                    "DELETE FROM checkreq.global_approvers WHERE id = %s",
                                    (row_id,),
                                )
                            results.append({"id": row_id, "ok": True, "deleted": True})
                            continue

                        approver_id = _get_or_create_user(cur, r.get("approver_email"))
                        backup_email = (r.get("backup_approver_email") or "").strip()
                        backup_id = _get_or_create_user(cur, backup_email) if backup_email else None

                        org_id = r.get("org_id")
                        org_id = int(org_id) if org_id not in (None, "", "null") else None
                        if org_id is not None:
                            cur.execute("SELECT 1 FROM checkreq.organizations WHERE id = %s", (org_id,))
                            if not cur.fetchone():
                                raise ValueError("Unknown entity.")

                        try:
                            serial_group = int(r.get("serial_group") or 1)
                        except (TypeError, ValueError):
                            raise ValueError("Serial group must be a whole number.")
                        if serial_group < 1:
                            raise ValueError("Serial group must be 1 or higher.")
                        is_active = bool(r.get("is_active", True))

                        # threshold_amount is legacy/unread (approval_engine.py
                        # uses organizations.global_approval_threshold since
                        # 2026-07-31) — preserved on update, defaulted on insert,
                        # never surfaced as an editable field on this screen.
                        if row_id:
                            cur.execute(
                                "UPDATE checkreq.global_approvers "
                                "SET approver_user_id = %s, serial_group = %s, backup_approver_id = %s, "
                                "    is_active = %s, org_id = %s WHERE id = %s",
                                (approver_id, serial_group, backup_id, is_active, org_id, row_id),
                            )
                        else:
                            cur.execute(
                                "INSERT INTO checkreq.global_approvers "
                                "(approver_user_id, serial_group, backup_approver_id, is_active, org_id) "
                                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                                (approver_id, serial_group, backup_id, is_active, org_id),
                            )
                            new_id = cur.fetchone()["id"]
                    results.append({"id": row_id, "new_id": new_id, "ok": True}
                                   if new_id else {"id": row_id, "ok": True})
                except Exception as exc:
                    results.append({"id": row_id, "ok": False, "error": str(exc)})

    saved = sum(1 for x in results if x["ok"])
    return {"saved": saved, "failed": len(results) - saved, "results": results}


# The standalone POST /admin/setup/global-approvers/{row_id}/delete route
# was removed 2026-08-02, same reasoning and same session as the GL Mapping
# delete route above -- a row's `_delete: true` flag now handles it inside
# organizations_save_approvers(), batched with every other edit.


# ── Program Areas / Approval Rules / User-Program-Area Assignments ──────────
# One screen pair for three workbook tabs, per Admin Module Plan.md's own
# recommendation: ProgramAreas is the list; each row's detail page carries
# the area's own fields plus "Who Can Submit" (UserProgramAreas) and "Who
# Approves" (ApprovalRules) as two panels beneath, since both answer a
# question ABOUT one program area and the workbook only kept them separate
# because a spreadsheet can't nest one table inside another.

_PROGRAM_AREA_LIST_SQL = """
    SELECT pa.id, pa.title, pa.description, pa.sort_order, pa.is_active,
           COUNT(DISTINCT pga.id) AS gl_count,
           COUNT(DISTINCT ar.id)  AS approver_count,
           COUNT(DISTINCT upa.id) AS submitter_count
    FROM checkreq.program_areas pa
    LEFT JOIN checkreq.program_area_gl_accounts pga ON pga.program_area_id = pa.id
    LEFT JOIN checkreq.approval_rules ar            ON ar.program_area_id = pa.id
    LEFT JOIN checkreq.user_program_areas upa        ON upa.program_area_id = pa.id
    WHERE pa.org_id = %s
    GROUP BY pa.id
    ORDER BY pa.sort_order, pa.title
"""

_APPROVAL_RULES_SQL = """
    SELECT ar.id, ar.approval_limit, ar.must_approve_flag, ar.must_approve_threshold,
           ar.serial_group, ar.is_active,
           u.email AS approver_email,
           bu.email AS backup_approver_email
    FROM checkreq.approval_rules ar
    JOIN checkreq.app_users u ON u.id = ar.approver_user_id
    LEFT JOIN checkreq.app_users bu ON bu.id = ar.backup_approver_id
    WHERE ar.program_area_id = %s
    ORDER BY ar.serial_group, u.email
"""


@router.get("/admin/setup/program-areas", response_class=HTMLResponse)
def program_areas_page(request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org:
        return RedirectResponse("/portal")

    rows = db.query(_PROGRAM_AREA_LIST_SQL, (org["id"],))
    return _render(request, "admin_setup_program_areas.html", user, {"rows": rows})


@router.post("/admin/setup/program-areas/add")
async def program_area_add(request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org:
        return JSONResponse({"error": "No entity selected"}, status_code=400)

    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        return JSONResponse({"error": "Title is required."}, status_code=400)
    description = (body.get("description") or "").strip() or None
    try:
        sort_order = int(body.get("sort_order") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"error": "Sort Order must be a whole number."}, status_code=400)
    is_active = bool(body.get("is_active", True))

    with db.connect() as conn:
        with conn.cursor() as cur:
            # program_areas has a real UNIQUE(org_id, title) constraint, but
            # checking first (rather than relying on the DB error alone)
            # gives a plain-language message instead of a raw psycopg
            # IntegrityError, matching gl_mapping_add's own precedent.
            cur.execute(
                "SELECT 1 FROM checkreq.program_areas WHERE org_id = %s AND LOWER(title) = LOWER(%s)",
                (org["id"], title),
            )
            if cur.fetchone():
                return JSONResponse(
                    {"error": "A program area with that title already exists for this entity."},
                    status_code=400,
                )
            cur.execute(
                "INSERT INTO checkreq.program_areas (org_id, title, description, sort_order, is_active) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (org["id"], title, description, sort_order, is_active),
            )
            new_id = cur.fetchone()["id"]

    return {"id": new_id}


def _program_area_for_org(program_area_id: int, org_id: int) -> dict | None:
    """Row-ownership check reused by every mutation below -- a program area
    id arrives from the client (a path segment here, same trust boundary as
    a row id in a JSON body) and is never assumed to belong to the session's
    current entity just because it was asked for."""
    return db.query_one(
        "SELECT id, org_id, title, description, sort_order, is_active "
        "FROM checkreq.program_areas WHERE id = %s AND org_id = %s",
        (program_area_id, org_id),
    )


@router.get("/admin/setup/program-areas/{program_area_id}", response_class=HTMLResponse)
def program_area_detail_page(program_area_id: int, request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org:
        return RedirectResponse("/portal")

    pa = _program_area_for_org(program_area_id, org["id"])
    if not pa:
        # Either a bad id, or it belongs to the OTHER entity and the header
        # switcher just hasn't been flipped to match yet -- either way, the
        # list page (scoped to whichever entity IS selected) is the correct
        # place to land, not a raw 404.
        return RedirectResponse(
            "/admin/setup/program-areas?error=That+program+area+isn%27t+part+of+the+selected+entity.",
            status_code=303,
        )

    submitters = db.query(
        """
        SELECT upa.id AS upa_id, u.id AS user_id, u.email, u.display_name
        FROM checkreq.user_program_areas upa
        JOIN checkreq.app_users u ON u.id = upa.user_id
        WHERE upa.program_area_id = %s
        ORDER BY u.email
        """,
        (program_area_id,),
    )
    already_submitter_ids = {s["user_id"] for s in submitters}
    # Cornerstone Served Parishes Plan.md Phase C, item 13: "only offer
    # people who already have access to the current entity" -- narrows
    # both the "Add a Submitter" picker and the "Who Approves" known-emails
    # datalist below to users who already hold a live role OR a
    # user_program_areas assignment at THIS org. UI-only, per decision 7 --
    # program_area_grant_submitter()/program_area_approval_rules_save() are
    # both unchanged and still honor a user_id/email submitted from outside
    # this list exactly as before (an approver at a brand-new entity still
    # needs to be addable before they have any other footprint there).
    available_users = rbac.users_with_org_access(org["id"])

    approval_rules = db.query(_APPROVAL_RULES_SQL, (program_area_id,))
    for a in approval_rules:
        a["approval_limit"] = _money(a["approval_limit"])
        a["must_approve_threshold"] = _money(a["must_approve_threshold"])

    known_emails = rbac.users_with_org_access(org["id"])

    return _render(request, "admin_setup_program_area_detail.html", user, {
        "pa": pa,
        "submitters": submitters,
        "already_submitter_ids": already_submitter_ids,
        "available_users": available_users,
        "approval_rules": approval_rules,
        "known_emails": [e["email"] for e in known_emails],
    })


@router.post("/admin/setup/program-areas/{program_area_id}/update")
async def program_area_update(program_area_id: int, request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org or not _program_area_for_org(program_area_id, org["id"]):
        return RedirectResponse("/admin/setup/program-areas", status_code=303)

    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        return RedirectResponse(
            f"/admin/setup/program-areas/{program_area_id}?error=Title+is+required.", status_code=303
        )
    description = (form.get("description") or "").strip() or None
    try:
        sort_order = int(form.get("sort_order") or 0)
    except (TypeError, ValueError):
        return RedirectResponse(
            f"/admin/setup/program-areas/{program_area_id}?error=Sort+Order+must+be+a+whole+number.",
            status_code=303,
        )
    is_active = form.get("is_active") == "on"

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.program_areas SET title = %s, description = %s, "
                "sort_order = %s, is_active = %s WHERE id = %s",
                (title, description, sort_order, is_active, program_area_id),
            )
    return RedirectResponse(f"/admin/setup/program-areas/{program_area_id}?saved=1", status_code=303)


# ── Who Can Submit (checkreq.user_program_areas) ─────────────────────────────
# Immediate single-row grant/revoke, deliberately NOT the batched dirty-save
# pattern -- same convention admin_users.py's own grant-pa/revoke-pa already
# uses for this identical table from the other direction (one user, many
# program areas, there; one program area, many users, here). This mirrors a
# "closest sibling" precedent that already exists in this codebase rather
# than inventing a new rule for a table that's really the same shape.

@router.post("/admin/setup/program-areas/{program_area_id}/submitters/grant")
async def program_area_grant_submitter(program_area_id: int, request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org or not _program_area_for_org(program_area_id, org["id"]):
        return RedirectResponse("/admin/setup/program-areas", status_code=303)

    form = await request.form()
    try:
        target_user_id = int(form.get("user_id") or 0)
    except (TypeError, ValueError):
        target_user_id = 0
    if not target_user_id:
        return RedirectResponse(
            f"/admin/setup/program-areas/{program_area_id}?error=Pick+a+user.", status_code=303
        )

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM checkreq.app_users WHERE id = %s", (target_user_id,))
            if not cur.fetchone():
                return RedirectResponse(
                    f"/admin/setup/program-areas/{program_area_id}?error=Unknown+user.", status_code=303
                )
            cur.execute(
                "INSERT INTO checkreq.user_program_areas (user_id, program_area_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (target_user_id, program_area_id),
            )
    return RedirectResponse(f"/admin/setup/program-areas/{program_area_id}?granted=1", status_code=303)


@router.post("/admin/setup/program-areas/{program_area_id}/submitters/revoke")
async def program_area_revoke_submitter(program_area_id: int, request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org or not _program_area_for_org(program_area_id, org["id"]):
        return RedirectResponse("/admin/setup/program-areas", status_code=303)

    form = await request.form()
    try:
        upa_id = int(form.get("upa_id") or 0)
    except (TypeError, ValueError):
        upa_id = 0

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM checkreq.user_program_areas WHERE id = %s AND program_area_id = %s",
                (upa_id, program_area_id),
            )
    return RedirectResponse(f"/admin/setup/program-areas/{program_area_id}?revoked=1", status_code=303)


# ── Who Approves (checkreq.approval_rules) ───────────────────────────────────
# The dense multi-field grid -- same batched dirty-save + `_delete: true` +
# _RowSavepoint + _get_or_create_user pattern as Global Approvers above,
# since this table is structurally almost identical (an approver, a backup,
# a serial group, an active flag) with two extra fields (approval_limit,
# must_approve_threshold/flag) that global_approvers doesn't need.

@router.post("/admin/setup/program-areas/{program_area_id}/approval-rules/save")
async def program_area_approval_rules_save(program_area_id: int, request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org:
        return JSONResponse({"error": "No entity selected"}, status_code=400)
    if not _program_area_for_org(program_area_id, org["id"]):
        return JSONResponse(
            {"error": "That program area isn't part of the selected entity."}, status_code=400
        )

    body = await request.json()
    rows = body.get("rows") or []
    results = []

    with db.connect() as conn:
        with conn.cursor() as cur:
            for i, r in enumerate(rows):
                row_id = r.get("id")
                new_id = None
                try:
                    with _RowSavepoint(cur, f"sp_ar_{i}"):
                        if row_id:
                            cur.execute(
                                "SELECT 1 FROM checkreq.approval_rules "
                                "WHERE id = %s AND program_area_id = %s",
                                (row_id, program_area_id),
                            )
                            if not cur.fetchone():
                                raise ValueError("This approval rule isn't part of this program area.")

                        if r.get("_delete"):
                            if row_id:
                                cur.execute(
                                    "DELETE FROM checkreq.approval_rules WHERE id = %s", (row_id,)
                                )
                            results.append({"id": row_id, "ok": True, "deleted": True})
                            continue

                        approver_id = _get_or_create_user(cur, r.get("approver_email"))
                        backup_email = (r.get("backup_approver_email") or "").strip()
                        backup_id = _get_or_create_user(cur, backup_email) if backup_email else None

                        try:
                            approval_limit = float(r.get("approval_limit") or 0)
                        except (TypeError, ValueError):
                            raise ValueError("Approval limit must be a number.")
                        if approval_limit < 0:
                            raise ValueError("Approval limit can't be negative.")

                        try:
                            must_approve_threshold = float(r.get("must_approve_threshold") or 0)
                        except (TypeError, ValueError):
                            raise ValueError("Must-approve threshold must be a number.")
                        if must_approve_threshold < 0:
                            raise ValueError("Must-approve threshold can't be negative.")

                        try:
                            serial_group = int(r.get("serial_group") or 1)
                        except (TypeError, ValueError):
                            raise ValueError("Serial group must be a whole number.")
                        if serial_group < 1:
                            raise ValueError("Serial group must be 1 or higher.")

                        must_approve_flag = bool(r.get("must_approve_flag"))
                        is_active = bool(r.get("is_active", True))

                        if row_id:
                            cur.execute(
                                "UPDATE checkreq.approval_rules "
                                "SET approver_user_id = %s, approval_limit = %s, must_approve_flag = %s, "
                                "    must_approve_threshold = %s, serial_group = %s, "
                                "    backup_approver_id = %s, is_active = %s "
                                "WHERE id = %s",
                                (approver_id, approval_limit, must_approve_flag, must_approve_threshold,
                                 serial_group, backup_id, is_active, row_id),
                            )
                        else:
                            cur.execute(
                                "INSERT INTO checkreq.approval_rules "
                                "(program_area_id, approver_user_id, approval_limit, must_approve_flag, "
                                " must_approve_threshold, serial_group, backup_approver_id, is_active) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                                (program_area_id, approver_id, approval_limit, must_approve_flag,
                                 must_approve_threshold, serial_group, backup_id, is_active),
                            )
                            new_id = cur.fetchone()["id"]
                    results.append({"id": row_id, "new_id": new_id, "ok": True}
                                   if new_id else {"id": row_id, "ok": True})
                except Exception as exc:
                    results.append({"id": row_id, "ok": False, "error": str(exc)})

    saved = sum(1 for x in results if x["ok"])
    return {"saved": saved, "failed": len(results) - saved, "results": results}


# ── ART List (Authorized Recurring Transactions) ─────────────────────────────
# Invoice Processing Intake Plan.md, "The ART / Preapproved screen" section --
# ports CFM AP Recurring Bills 2026.xlsx (the Services Team's real, hand-kept
# spreadsheet) into a real table. Two screens, matching the plan's own
# Tier 1 scope:
#   GET  /admin/setup/art                    -- list, grouped by group_label
#                                                (same collapsible-header
#                                                pattern as GL Mapping), a
#                                                dense grid of the fields
#                                                worth bulk-editing, with the
#                                                same batched dirty-save +
#                                                delete-as-dirty pattern
#   POST /admin/setup/art/save               -- batched save for the grid
#                                                above (group_label, art_type,
#                                                frequency, is_active,
#                                                grace_days; `_delete: true`
#                                                deletes the row, cascading
#                                                to its art_period_status
#                                                rows per the migration's own
#                                                ON DELETE CASCADE)
#   POST /admin/setup/art/add                -- create one entry (vendor +
#                                                a minimal starting field
#                                                set, matching every other
#                                                Add panel's own "small
#                                                subset now, full detail
#                                                later" convention)
#   GET  /admin/setup/art/api/vendors        -- Add-panel vendor picker feed
#                                                (org-scoped, excludes
#                                                vendors that already have an
#                                                ART entry for this org --
#                                                the real UNIQUE(vendor_id,
#                                                org_id) constraint)
#   GET  /admin/setup/art/{id}               -- detail: every real ART
#                                                field as an editable form
#                                                (identity-panel-at-top,
#                                                same shape as Program Area's
#                                                own detail page), plus the
#                                                Tier 2 completeness panel
#   POST /admin/setup/art/{id}/update        -- the detail form's single-POST
#                                                save (not batched -- one
#                                                record, same convention as
#                                                Program Area's own top form)
#   POST /admin/setup/art/{id}/delete        -- immediate, confirmed,
#                                                single-row delete from the
#                                                detail page. NOT the same
#                                                thing the 2026-08-02 standing
#                                                rule forbids -- that rule is
#                                                about a dense multi-row GRID
#                                                where an accidental click is
#                                                the real risk (Jay's actual
#                                                incident); a lone confirmed
#                                                action on one record's own
#                                                detail page is the same
#                                                shape as Cancel-a-Check-
#                                                Request's confirm()+POST,
#                                                already an established
#                                                pattern in this codebase.
#   GET  /admin/setup/art/api/gl-accounts    -- detail-page GL account picker
#                                                feed (org-scoped, no
#                                                exclusion -- unlike GL
#                                                Mapping's picker, an ART
#                                                entry's gl_account_id isn't
#                                                unique, so nothing to
#                                                exclude)
#   POST /admin/setup/art/{id}/check-completeness -- Tier 2: run the manual,
#                                                on-demand reconciliation for
#                                                ONE entry's current period
#                                                (art_completeness.check_one)
#   POST /admin/setup/art/check-all          -- Tier 2: same, for every
#                                                active entry in the current
#                                                entity (art_completeness.
#                                                check_org)
#   POST /admin/setup/art/{id}/periods/{period_id}/resolve -- mark one period
#                                                'manually_resolved' with a
#                                                note (a real, human-confirmed
#                                                exception -- e.g. a vendor
#                                                was cancelled mid-year --
#                                                per the plan's own design)

_ART_LIST_SQL = """
    SELECT al.id, al.group_label, al.art_type, al.frequency, al.is_active, al.grace_days,
           al.amount_check_mode, al.amount_exact, al.amount_min, al.amount_max, al.amount_notes,
           al.vendor_id, v.display_name AS vendor_display_name,
           al.gl_account_id, ga.account_number, ga.account_name, al.gl_account_name_override,
           lp.period_label AS latest_period_label, lp.status AS latest_status,
           lp.checked_at AS latest_checked_at
    FROM checkreq.art_list al
    JOIN checkreq.vendors v ON v.id = al.vendor_id
    LEFT JOIN checkreq.gl_accounts ga ON ga.id = al.gl_account_id
    LEFT JOIN LATERAL (
        SELECT period_label, status, checked_at
        FROM checkreq.art_period_status
        WHERE art_list_id = al.id
        ORDER BY checked_at DESC NULLS LAST, id DESC
        LIMIT 1
    ) lp ON TRUE
    WHERE al.org_id = %s
    ORDER BY COALESCE(al.group_label, '~'), v.display_name
"""


def _art_amount_display(row: dict) -> str:
    mode = row.get("amount_check_mode")
    if mode == "exact" and row.get("amount_exact") is not None:
        return f"${_money(row['amount_exact']):,.2f}"
    if mode == "range" and row.get("amount_min") is not None and row.get("amount_max") is not None:
        return f"${_money(row['amount_min']):,.2f}–${_money(row['amount_max']):,.2f}"
    return (row.get("amount_notes") or "").strip() or ("Seasonal" if mode == "seasonal" else "Varies")


def _art_gl_display(row: dict) -> str:
    if row.get("account_number"):
        return f"{row['account_number']} · {row.get('account_name') or ''}".strip(" ·")
    return row.get("gl_account_name_override") or "(multi-line)"


def _art_list_groups(org_id: int) -> list[dict]:
    rows = db.query(_ART_LIST_SQL, (org_id,))
    groups: list[dict] = []
    by_label: dict[str, dict] = {}
    for r in rows:
        label = r["group_label"] or "(Ungrouped)"
        g = by_label.get(label)
        if g is None:
            g = {"label": label, "rows": []}
            by_label[label] = g
            groups.append(g)
        g["rows"].append({
            **r,
            "amount_display": _art_amount_display(r),
            "gl_display": _art_gl_display(r),
        })
    return groups


@router.get("/admin/setup/art", response_class=HTMLResponse)
def art_list_page(request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org:
        return RedirectResponse("/portal")

    return _render(request, "admin_setup_art.html", user, {
        "groups": _art_list_groups(org["id"]),
    })


@router.get("/admin/setup/art/api/vendors")
def api_art_vendors(request: Request, q: str = ""):
    """Add-panel vendor picker feed -- org-scoped, excludes any vendor that
    already has an ART entry for this org (the real UNIQUE(vendor_id,
    org_id) constraint would just bounce a duplicate back as an error
    otherwise -- excluding it up front is the same "can't even pick a
    duplicate" precedent as GL Mapping's own unmapped-gl-accounts feed)."""
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org:
        return JSONResponse({"error": "No entity selected"}, status_code=400)

    sql = """
        SELECT v.id, v.display_name
        FROM checkreq.vendors v
        WHERE v.org_id = %s AND v.is_active
          AND NOT EXISTS (
              SELECT 1 FROM checkreq.art_list al
              WHERE al.vendor_id = v.id AND al.org_id = %s
          )
    """
    params: tuple = (org["id"], org["id"])
    if q:
        sql += " AND v.display_name ILIKE %s"
        params += (f"%{q}%",)
    sql += " ORDER BY v.display_name LIMIT 100"
    return db.query(sql, params)


@router.get("/admin/setup/art/api/gl-accounts")
def api_art_gl_accounts(request: Request, q: str = ""):
    """Detail-page GL account picker feed -- org-scoped, no exclusion
    (unlike GL Mapping's picker, an ART entry's gl_account_id has no
    uniqueness constraint to respect -- more than one ART entry can
    reasonably code to the same account)."""
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org:
        return JSONResponse({"error": "No entity selected"}, status_code=400)

    sql = "SELECT id, account_number, account_name FROM checkreq.gl_accounts WHERE org_id = %s AND is_active"
    params: tuple = (org["id"],)
    if q:
        sql += " AND (account_number ILIKE %s OR account_name ILIKE %s)"
        params += (f"%{q}%", f"%{q}%")
    sql += " ORDER BY account_number LIMIT 100"
    return db.query(sql, params)


@router.post("/admin/setup/art/add")
async def art_add(request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org:
        return JSONResponse({"error": "No entity selected"}, status_code=400)

    body = await request.json()
    try:
        vendor_id = int(body.get("vendor_id") or 0)
    except (TypeError, ValueError):
        vendor_id = 0
    if not vendor_id:
        return JSONResponse({"error": "Pick a vendor."}, status_code=400)

    group_label = (body.get("group_label") or "").strip() or None
    art_type = (body.get("art_type") or "").strip() or None
    is_active = bool(body.get("is_active", True))

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM checkreq.vendors WHERE id = %s AND org_id = %s",
                        (vendor_id, org["id"]))
            if not cur.fetchone():
                return JSONResponse({"error": "That vendor isn't part of the selected entity."},
                                    status_code=400)
            cur.execute(
                "INSERT INTO checkreq.art_list (vendor_id, org_id, group_label, art_type, "
                "is_active, created_by_user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (vendor_id, org_id) DO NOTHING RETURNING id",
                (vendor_id, org["id"], group_label, art_type, is_active, user["id"]),
            )
            created = cur.fetchone()

    if not created:
        return JSONResponse(
            {"error": "This vendor already has an ART entry for this entity."}, status_code=400
        )
    return {"id": created["id"]}


@router.post("/admin/setup/art/save")
async def art_save(request: Request):
    """Batched save for the list-page grid -- same _RowSavepoint +
    `_delete: true` pattern as gl_mapping_save()/organizations_save_
    approvers(). Only the grid's own editable subset (group_label,
    art_type, frequency, is_active, grace_days) is written here; every
    other real ART field is edited on the detail page's own single-POST
    form, not this grid -- there are simply too many fields (~20) to
    reasonably show inline per row."""
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org:
        return JSONResponse({"error": "No entity selected"}, status_code=400)

    body = await request.json()
    rows = body.get("rows") or []
    results = []

    with db.connect() as conn:
        with conn.cursor() as cur:
            for i, r in enumerate(rows):
                row_id = r.get("id")
                try:
                    with _RowSavepoint(cur, f"sp_art_{i}"):
                        cur.execute(
                            "SELECT 1 FROM checkreq.art_list WHERE id = %s AND org_id = %s",
                            (row_id, org["id"]),
                        )
                        if not cur.fetchone():
                            raise ValueError("This ART entry isn't part of the selected entity.")

                        if r.get("_delete"):
                            cur.execute("DELETE FROM checkreq.art_list WHERE id = %s", (row_id,))
                            results.append({"id": row_id, "ok": True, "deleted": True})
                            continue

                        group_label = (r.get("group_label") or "").strip() or None
                        art_type = (r.get("art_type") or "").strip() or None
                        frequency = (r.get("frequency") or "").strip() or None
                        try:
                            grace_days = int(r.get("grace_days") or 0)
                        except (TypeError, ValueError):
                            raise ValueError("Grace days must be a whole number.")
                        if grace_days < 0:
                            raise ValueError("Grace days can't be negative.")

                        cur.execute(
                            "UPDATE checkreq.art_list SET group_label = %s, art_type = %s, "
                            "frequency = %s, is_active = %s, grace_days = %s, "
                            "updated_by_user_id = %s, updated_at = NOW() WHERE id = %s",
                            (group_label, art_type, frequency, bool(r.get("is_active")),
                             grace_days, user["id"], row_id),
                        )
                    results.append({"id": row_id, "ok": True})
                except Exception as exc:
                    results.append({"id": row_id, "ok": False, "error": str(exc)})

    saved = sum(1 for x in results if x["ok"])
    return {"saved": saved, "failed": len(results) - saved, "results": results}


def _art_for_org(art_id: int, org_id: int) -> dict | None:
    return db.query_one(
        """
        SELECT al.*, v.display_name AS vendor_display_name, v.qbo_vendor_id,
               ga.account_number, ga.account_name,
               au.email AS approved_by_email
        FROM checkreq.art_list al
        JOIN checkreq.vendors v ON v.id = al.vendor_id
        LEFT JOIN checkreq.gl_accounts ga ON ga.id = al.gl_account_id
        LEFT JOIN checkreq.app_users au ON au.id = al.approved_by_user_id
        WHERE al.id = %s AND al.org_id = %s
        """,
        (art_id, org_id),
    )


@router.get("/admin/setup/art/{art_id}", response_class=HTMLResponse)
def art_detail_page(art_id: int, request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org:
        return RedirectResponse("/portal")

    art = _art_for_org(art_id, org["id"])
    if not art:
        return RedirectResponse(
            "/admin/setup/art?error=That+ART+entry+isn%27t+part+of+the+selected+entity.",
            status_code=303,
        )
    for f in ("amount_exact", "amount_min", "amount_max", "preapproval_max_amount"):
        art[f] = _money(art[f]) if art.get(f) is not None else None

    periods = db.query(
        "SELECT * FROM checkreq.art_period_status WHERE art_list_id = %s "
        "ORDER BY period_label DESC LIMIT 12",
        (art_id,),
    )
    known_emails = db.query("SELECT email FROM checkreq.app_users WHERE is_active ORDER BY email")

    return _render(request, "admin_setup_art_detail.html", user, {
        "art": art,
        "periods": periods,
        "known_emails": [e["email"] for e in known_emails],
    })


def _f(form, key, default=""):
    v = form.get(key)
    return v if v is not None else default


def _parse_money(form, key):
    v = (form.get(key) or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        raise ValueError(f"'{key}' must be a number.")


def _parse_date(form, key):
    v = (form.get(key) or "").strip()
    if not v:
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"'{key}' must be a valid date (YYYY-MM-DD).")


@router.post("/admin/setup/art/{art_id}/update")
async def art_update(art_id: int, request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org or not _art_for_org(art_id, org["id"]):
        return RedirectResponse("/admin/setup/art", status_code=303)

    form = await request.form()

    def back(msg: str):
        from urllib.parse import quote
        return RedirectResponse(f"/admin/setup/art/{art_id}?error={quote(msg)}", status_code=303)

    try:
        authorized_from = _parse_date(form, "authorized_from")
        authorized_through = _parse_date(form, "authorized_through")
        approved_at = _parse_date(form, "approved_at")
        amount_exact = _parse_money(form, "amount_exact")
        amount_min = _parse_money(form, "amount_min")
        amount_max = _parse_money(form, "amount_max")
        preapproval_max_amount = _parse_money(form, "preapproval_max_amount")
        try:
            grace_days = int(_f(form, "grace_days", "3") or 3)
        except ValueError:
            raise ValueError("Grace Days must be a whole number.")
        if grace_days < 0:
            raise ValueError("Grace Days can't be negative.")
    except ValueError as exc:
        return back(str(exc))

    amount_check_mode = _f(form, "amount_check_mode", "range")
    if amount_check_mode not in ("exact", "range", "seasonal", "variable"):
        return back("Invalid amount check mode.")
    preapproval_scope = _f(form, "preapproval_scope", "one_step_confirmation")
    if preapproval_scope not in ("full_skip", "dollar_cap", "one_step_confirmation"):
        return back("Invalid preapproval scope.")

    gl_account_id = form.get("gl_account_id") or None
    if gl_account_id:
        try:
            gl_account_id = int(gl_account_id)
        except ValueError:
            return back("Invalid GL account.")

    approved_by_email = (form.get("approved_by_email") or "").strip()

    with db.connect() as conn:
        with conn.cursor() as cur:
            if gl_account_id:
                cur.execute("SELECT 1 FROM checkreq.gl_accounts WHERE id = %s AND org_id = %s",
                            (gl_account_id, org["id"]))
                if not cur.fetchone():
                    return back("That GL account isn't part of the selected entity.")

            approved_by_user_id = None
            if approved_by_email:
                try:
                    approved_by_user_id = _get_or_create_user(cur, approved_by_email)
                except ValueError as exc:
                    return back(str(exc))

            cur.execute(
                """
                UPDATE checkreq.art_list SET
                    group_label = %s, art_type = %s, is_active = %s,
                    approved_by_user_id = %s, approved_at = %s,
                    authorized_from = %s, authorized_through = %s,
                    frequency = %s, expected_day_of_month = %s,
                    amount_check_mode = %s, amount_exact = %s, amount_min = %s, amount_max = %s,
                    amount_notes = %s,
                    invoice_setup = %s, invoice_source = %s, update_ap_bill = %s,
                    has_login_credential = %s, pay_method = %s,
                    gl_account_id = %s, gl_account_name_override = %s,
                    preapproval_scope = %s, preapproval_max_amount = %s,
                    is_monkey_see_monkey_do = %s, special_handling_notes = %s,
                    notes = %s, cfm_notes = %s, grace_days = %s,
                    updated_by_user_id = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (
                    (form.get("group_label") or "").strip() or None,
                    (form.get("art_type") or "").strip() or None,
                    form.get("is_active") == "on",
                    approved_by_user_id, approved_at,
                    authorized_from, authorized_through,
                    (form.get("frequency") or "").strip() or None,
                    (form.get("expected_day_of_month") or "").strip() or None,
                    amount_check_mode, amount_exact, amount_min, amount_max,
                    (form.get("amount_notes") or "").strip() or None,
                    (form.get("invoice_setup") or "").strip() or None,
                    (form.get("invoice_source") or "").strip() or None,
                    (form.get("update_ap_bill") or "").strip() or None,
                    (form.get("has_login_credential") or "").strip() or None,
                    (form.get("pay_method") or "").strip() or None,
                    gl_account_id,
                    (form.get("gl_account_name_override") or "").strip() or None,
                    preapproval_scope, preapproval_max_amount,
                    form.get("is_monkey_see_monkey_do") == "on",
                    (form.get("special_handling_notes") or "").strip() or None,
                    (form.get("notes") or "").strip() or None,
                    (form.get("cfm_notes") or "").strip() or None,
                    grace_days,
                    user["id"], art_id,
                ),
            )

    return RedirectResponse(f"/admin/setup/art/{art_id}?saved=1", status_code=303)


@router.post("/admin/setup/art/{art_id}/delete")
async def art_delete(art_id: int, request: Request):
    """Immediate, confirmed, single-row delete -- see this section's own
    header comment for why this is exempt from the 2026-08-02 delete-as-
    dirty rule (that rule targets a dense multi-row grid's accidental-click
    risk; this is one confirmed action on one record's own detail page,
    the same shape as Cancel-a-Check-Request)."""
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org or not _art_for_org(art_id, org["id"]):
        return RedirectResponse("/admin/setup/art", status_code=303)

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM checkreq.art_list WHERE id = %s", (art_id,))

    return RedirectResponse("/admin/setup/art?deleted=1", status_code=303)


# ── Tier 2: ART completeness tracking (manual, on-demand only) ──────────────
# See art_completeness.py's own module docstring for what this is and,
# explicitly, what it is NOT (the nightly automated job is a deliberate
# future step, not attempted here).

@router.post("/admin/setup/art/{art_id}/check-completeness")
def art_check_one(art_id: int, request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org or not _art_for_org(art_id, org["id"]):
        return JSONResponse({"error": "That ART entry isn't part of the selected entity."},
                            status_code=400)
    return art_completeness.check_one(art_id)


@router.post("/admin/setup/art/check-all")
def art_check_all(request: Request):
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org:
        return JSONResponse({"error": "No entity selected"}, status_code=400)
    return art_completeness.check_org(org["id"])


@router.post("/admin/setup/art/{art_id}/periods/{period_id}/resolve")
async def art_period_resolve(art_id: int, period_id: int, request: Request):
    """A real, human-confirmed exception (e.g. a vendor was cancelled
    mid-year, or the bill legitimately posted under a different vendor
    name the fuzzy matcher couldn't recognize) -- per the plan's own
    design, this is the only way a period's status becomes
    'manually_resolved', and check_one()'s own upsert deliberately never
    overwrites that status on a later automated re-check."""
    user, err = _require_setup_admin(request)
    if err:
        return err
    org = _current_org(request)
    if not org or not _art_for_org(art_id, org["id"]):
        return RedirectResponse("/admin/setup/art", status_code=303)

    form = await request.form()
    note = (form.get("resolved_note") or "").strip()

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM checkreq.art_period_status WHERE id = %s AND art_list_id = %s",
                (period_id, art_id),
            )
            if not cur.fetchone():
                return RedirectResponse(
                    f"/admin/setup/art/{art_id}?error=That+period+isn%27t+part+of+this+entry.",
                    status_code=303,
                )
            cur.execute(
                "UPDATE checkreq.art_period_status SET status = 'manually_resolved', "
                "resolved_by_user_id = %s, resolved_note = %s, checked_at = NOW() WHERE id = %s",
                (user["id"], note or None, period_id),
            )

    return RedirectResponse(f"/admin/setup/art/{art_id}?resolved=1", status_code=303)
