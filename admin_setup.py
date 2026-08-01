"""
admin_setup.py — Beacon Admin > Setup Tables module (PROTOTYPE, 2026-08-01).

Ports `Tools\\26-129 Check Request Setup Tables.xlsm` into Beacon itself. See
`Admin Module Plan.md` in this project's root for the full design, the
page-by-page mapping of the tabs NOT built here, and the open questions.

Built so far (real, working, wired to live checkreq Postgres):
  GET  /admin/setup                              — module index (all 8 tabs, status + row counts)
  GET  /admin/setup/gl-mapping                   — Program Area <-> GL Account map (workbook tab: ProgramAreaGLAccounts)
  POST /admin/setup/gl-mapping/save              — bulk save of edited rows (JSON)
  POST /admin/setup/gl-mapping/add               — create one mapping (JSON)
  POST /admin/setup/gl-mapping/{row_id}/delete   — delete one mapping
  GET  /admin/setup/api/unmapped-gl-accounts     — dependent GL picker feed (Tom Select)
  GET  /admin/setup/organizations                — Organizations + GlobalApprovers (workbook tabs: Organizations, GlobalApprovers)
  POST /admin/setup/organizations/save-orgs      — per-entity Global Approval Threshold (JSON)
  POST /admin/setup/organizations/save-approvers — global approver rows (JSON)
  POST /admin/setup/global-approvers/{row_id}/delete

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

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db

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
     "desc": "The grouping every GL account, approval rule and user assignment hangs off.",
     "url": None, "built": False, "entity_scoped": True,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.program_areas WHERE org_id = %s"},
    {"key": "gl_mapping", "title": "Program Area / GL Account Map",
     "desc": "Which GL accounts a program area may code to, their display label, hierarchy and overspend buffer.",
     "url": "/admin/setup/gl-mapping", "built": True, "entity_scoped": True,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.program_area_gl_accounts pga "
                  "JOIN checkreq.program_areas pa ON pa.id = pga.program_area_id WHERE pa.org_id = %s"},
    {"key": "approval_rules", "title": "Approval Rules",
     "desc": "Per program area: approver, limit, serial group, must-approve threshold, backup.",
     "url": None, "built": False, "entity_scoped": True,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.approval_rules ar "
                  "JOIN checkreq.program_areas pa ON pa.id = ar.program_area_id WHERE pa.org_id = %s"},
    {"key": "user_program_areas", "title": "User / Program Area Assignments",
     "desc": "Who is allowed to submit against which program area.",
     "url": None, "built": False, "entity_scoped": True,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.user_program_areas upa "
                  "JOIN checkreq.program_areas pa ON pa.id = upa.program_area_id WHERE pa.org_id = %s"},
    {"key": "organizations", "title": "Entities & Global Approvers",
     "desc": "Per-entity global approval threshold, and who signs off above it.",
     "url": "/admin/setup/organizations", "built": True, "entity_scoped": False,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.organizations"},
    {"key": "gl_accounts", "title": "GL Accounts (reference)",
     "desc": "Chart of accounts, synced nightly from QBO. Read-only.",
     "url": None, "built": False, "entity_scoped": True,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.gl_accounts WHERE org_id = %s"},
    {"key": "vendors", "title": "Vendors (reference)",
     "desc": "Vendor list, synced nightly from QBO. Read-only.",
     "url": None, "built": False, "entity_scoped": True,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.vendors WHERE org_id = %s"},
    # Added 2026-08-01, RBAC build (admin_users.py) -- live since
    # migrations/019_rbac.sql was applied and admin_users.py wired in.
    {"key": "users", "title": "Users & Roles",
     "desc": "Who can sign in, and what each person may do in each entity.",
     "url": "/admin/setup/users", "built": True, "entity_scoped": False,
     "count_sql": "SELECT COUNT(*) AS n FROM checkreq.app_users WHERE is_active"},
]


def _require_cfo(request: Request):
    """(user, None) when allowed, (None, response) when not — same shape and
    same gate as main.py's own _require_vendor_approver/_require_ap_reviewer,
    and the same is_cfo check /admin/all-requests and /admin/feedback use."""
    user = _current_user(request)
    if not user:
        return None, RedirectResponse("/login")
    if not user.get("is_cfo"):
        return None, JSONResponse({"error": "CFO access required"}, status_code=403)
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
    user, err = _require_cfo(request)
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
    user, err = _require_cfo(request)
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
    user, err = _require_cfo(request)
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
    except the result lands on the row itself instead of in column K."""
    user, err = _require_cfo(request)
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
    user, err = _require_cfo(request)
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


@router.post("/admin/setup/gl-mapping/{row_id}/delete")
def gl_mapping_delete(row_id: int, request: Request):
    """Removing a mapping only withdraws the account from that program
    area's future picker — posted requests reference gl_account_id directly
    and are untouched. The workbook has no delete at all (clearing the cells
    is a silent no-op, since Save only ever iterates rows that are present)."""
    user, err = _require_cfo(request)
    if err:
        return err
    org = _current_org(request)
    if not org:
        return RedirectResponse("/portal")

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM checkreq.program_area_gl_accounts pga "
                "USING checkreq.program_areas pa "
                "WHERE pa.id = pga.program_area_id AND pga.id = %s AND pa.org_id = %s",
                (row_id, org["id"]),
            )
    return RedirectResponse("/admin/setup/gl-mapping?deleted=1", status_code=303)


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
    user, err = _require_cfo(request)
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
    user, err = _require_cfo(request)
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
    user, err = _require_cfo(request)
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


@router.post("/admin/setup/global-approvers/{row_id}/delete")
def global_approver_delete(row_id: int, request: Request):
    user, err = _require_cfo(request)
    if err:
        return err
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM checkreq.global_approvers WHERE id = %s", (row_id,))
    return RedirectResponse("/admin/setup/organizations?deleted=1", status_code=303)
