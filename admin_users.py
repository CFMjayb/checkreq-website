"""
admin_users.py — Users & Roles admin screen (RBAC, Plan §6).

NOT YET WIRED IN — same status as access_requests.py (see that module's
docstring). Code-complete, not imported by main.py, and depends on
checkreq.roles / checkreq.user_roles which do not exist in production until
migrations/019_rbac.sql is applied.

Two pages, following Admin Module Plan.md's own precedent (a list + a
detail page, rather than one page trying to do both):

  GET  /admin/setup/users              — list: every app_users row, with
                                          role chips (not a checkbox matrix
                                          — Plan §6.2 explains why a matrix
                                          doesn't scale past a couple of
                                          entities), plus the "no roles" /
                                          "never signed in but is a required
                                          approver" / "deactivated" flags.
  POST /admin/setup/users/add          — provision a new, roleless user.
  GET  /admin/setup/users/{id}         — detail: identity, live role grants
                                          + revoke, program-area assignments
                                          (read-only here — that table's own
                                          UI is a separate, not-yet-built tab,
                                          Admin Module Plan.md).
  POST /admin/setup/users/{id}/update  — identity panel (display name,
                                          active toggle; email is editable
                                          too, since fixing a wrong address
                                          is a real, proven-necessary
                                          operation -- Plan §4.3's
                                          jboggs@/jay@ situation).
  POST /admin/setup/users/{id}/grant   — grant a role, one entity or "all
                                          active entities" (Plan §6.2).
  POST /admin/setup/users/{id}/revoke  — revoke a role. rbac.revoke_role
                                          enforces the last-admin and
                                          no-self-revoke guards; this route
                                          only translates its LastAdminError
                                          into a user-facing message.

Gated on beacon_admin (Plan §6.1: "a bookkeeper who maintains GL mappings
should not thereby be able to make themselves CFO") -- distinct from
setup_admin, which admin_setup.py's own screens use.

Kept as its own module rather than grown inside admin_setup.py (which is
already 609 lines) -- see feedback_modular_file_organization.md.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
import rbac

router = APIRouter()

_current_user = None
_render = None


def register(app, *, current_user, render) -> None:
    global _current_user, _render
    _current_user, _render = current_user, render
    app.include_router(router)


def _require_beacon_admin(request: Request):
    user = _current_user(request)
    if not user:
        return None, RedirectResponse("/login")
    if not rbac.user_has_role(user["id"], "beacon_admin", org_id=None):
        return None, JSONResponse({"error": "Beacon Admin access required"}, status_code=403)
    return user, None


def _user_list_rows() -> list[dict]:
    """One row per app_users row, with role/program-area/entity DETAIL (not
       just a count -- 2026-08-02 feedback batch, Item 13: "if you click on
       that and you show me what the program area is, that's great") plus
       the three inline flags described in Plan §6.2. A handful of extra
       queries per user is fine at this table's real size (tens of rows,
       not thousands)."""
    users = db.query(
        "SELECT id, email, display_name, first_name, is_active, last_login_at, last_login_provider "
        "FROM checkreq.app_users ORDER BY email"
    )
    for u in users:
        u["roles"] = rbac.get_roles_for_user(u["id"])
        u["has_any_role"] = len(u["roles"]) > 0
        u["program_areas"] = db.query(
            """
            SELECT pa.title AS program_area_title, o.code AS org_code
              FROM checkreq.user_program_areas upa
              JOIN checkreq.program_areas pa ON pa.id = upa.program_area_id
              JOIN checkreq.organizations o ON o.id = pa.org_id
             WHERE upa.user_id = %s
             ORDER BY o.code, pa.title
            """,
            (u["id"],),
        )
        u["pa_count"] = len(u["program_areas"])
        # Every entity this person has a live footprint in, role OR
        # program-area -- Item 13's "Entities" column.
        entity_codes = sorted({r["org_code"] for r in u["roles"]} |
                               {r["org_code"] for r in u["program_areas"]})
        u["entity_codes"] = entity_codes
        u["is_unreachable_approver"] = (u["last_login_at"] is None) and bool(db.query_one(
            """
            SELECT 1 FROM (
                SELECT approver_user_id AS uid FROM checkreq.global_approvers WHERE is_active
                UNION SELECT backup_approver_id FROM checkreq.global_approvers WHERE backup_approver_id IS NOT NULL
                UNION SELECT approver_user_id FROM checkreq.approval_rules WHERE is_active
                UNION SELECT backup_approver_id FROM checkreq.approval_rules WHERE backup_approver_id IS NOT NULL
            ) x WHERE x.uid = %s
            """,
            (u["id"],),
        ))
    return users


@router.get("/admin/setup/users", response_class=HTMLResponse)
def users_list_page(request: Request):
    user, err = _require_beacon_admin(request)
    if err:
        return err
    rows = _user_list_rows()
    unreachable = [r for r in rows if r["is_unreachable_approver"]]
    return _render(request, "admin_users_index.html", user, {
        "rows": rows,
        "unreachable_count": len(unreachable),
    })


@router.post("/admin/setup/users/add")
async def users_add(request: Request):
    """Step 1 of the guided setup flow (2026-08-02 feedback batch, Item
       13-continued). Still a plain roleless-account INSERT -- the "guided"
       part is the `?new=1` redirect below, which the detail page renders
       as an explicit "now grant a role" prompt rather than leaving that as
       an undiscoverable next step."""
    user, err = _require_beacon_admin(request)
    if err:
        return err
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    first_name = (form.get("first_name") or "").strip() or None
    display_name = (form.get("display_name") or "").strip() or None
    if not email:
        return JSONResponse({"error": "Email is required."}, status_code=400)

    existing = db.query_one("SELECT id FROM checkreq.app_users WHERE LOWER(email) = %s", (email,))
    if existing:
        return RedirectResponse(f"/admin/setup/users/{existing['id']}", status_code=303)

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkreq.app_users (email, display_name, first_name, is_active) "
                "VALUES (%s, %s, %s, TRUE) RETURNING id",
                (email, display_name or email.split("@")[0], first_name),
            )
            new_id = cur.fetchone()["id"]
    return RedirectResponse(f"/admin/setup/users/{new_id}?new=1", status_code=303)


def _guess_provider(email: str) -> str | None:
    """2026-08-02 feedback batch, Item 14.1: predict the sign-in provider
       from the email domain (same checkreq.identity_provider_domains table
       main.py's own /auth/route uses) so a never-signed-in user doesn't
       show a bare, unexplained "—". Duplicated rather than imported from
       main.py -- this module deliberately never imports main (main imports
       it), same reasoning as every other admin module's docstring."""
    domain = email.split("@")[-1].lower() if "@" in email else ""
    if not domain:
        return None
    row = db.query_one(
        "SELECT provider FROM checkreq.identity_provider_domains WHERE domain = %s AND is_active",
        (domain,),
    )
    return row["provider"] if row else None


@router.get("/admin/setup/users/{user_id}", response_class=HTMLResponse)
def user_detail_page(user_id: int, request: Request):
    user, err = _require_beacon_admin(request)
    if err:
        return err
    target = db.query_one("SELECT * FROM checkreq.app_users WHERE id = %s", (user_id,))
    if not target:
        return RedirectResponse("/admin/setup/users")

    roles = rbac.get_roles_for_user(user_id)
    program_areas = db.query(
        """
        SELECT upa.id AS upa_id, pa.id AS program_area_id,
               pa.title AS program_area_title, o.id AS org_id, o.code AS org_code
          FROM checkreq.user_program_areas upa
          JOIN checkreq.program_areas pa ON pa.id = upa.program_area_id
          JOIN checkreq.organizations o ON o.id = pa.org_id
         WHERE upa.user_id = %s
         ORDER BY o.code, pa.title
        """,
        (user_id,),
    )
    orgs = db.query("SELECT id, code, name FROM checkreq.organizations WHERE is_active ORDER BY name")

    # 2026-08-02 feedback batch, Item 14.2: which entities does this person
    # actually have SOMETHING in -- drives the dropdown's "multiple
    # entities" indicator (a plain count/list is enough; no separate flag
    # column needed since the template can just check membership).
    entities_with_access = sorted({r["org_id"] for r in roles} | {r["org_id"] for r in program_areas})

    # Grant-Program-Area picker: every active program area, grouped by
    # entity via <optgroup> so no dependent-dropdown JS/AJAX is needed --
    # program area counts here are small enough (dozens, not thousands) that
    # a single flat list grouped client-side-for-free by <optgroup> is
    # simpler and more robust than the GL Mapping page's async picker.
    all_program_areas = db.query(
        "SELECT pa.id, pa.title, o.code AS org_code "
        "FROM checkreq.program_areas pa JOIN checkreq.organizations o ON o.id = pa.org_id "
        "WHERE o.is_active ORDER BY o.code, pa.title"
    )
    already_granted_pa_ids = {p["program_area_id"] for p in program_areas}

    return _render(request, "admin_users_detail.html", user, {
        "target": target,
        "provider_guess": None if target["last_login_provider"] else _guess_provider(target["email"]),
        "roles": roles,
        "all_roles": rbac.all_roles(),
        "orgs": orgs,
        "entities_with_access": entities_with_access,
        "program_areas": program_areas,
        "all_program_areas": all_program_areas,
        "already_granted_pa_ids": already_granted_pa_ids,
    })


@router.post("/admin/setup/users/{user_id}/update")
async def user_update(user_id: int, request: Request):
    user, err = _require_beacon_admin(request)
    if err:
        return err
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    first_name = (form.get("first_name") or "").strip() or None
    display_name = (form.get("display_name") or "").strip() or None
    is_active = form.get("is_active") == "on"
    if not email:
        return JSONResponse({"error": "Email is required."}, status_code=400)

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.app_users SET email = %s, display_name = %s, first_name = %s, "
                "is_active = %s WHERE id = %s",
                (email, display_name, first_name, is_active, user_id),
            )
    return RedirectResponse(f"/admin/setup/users/{user_id}?saved=1", status_code=303)


@router.post("/admin/setup/users/{user_id}/grant-pa")
async def user_grant_program_area(user_id: int, request: Request):
    """2026-08-02 feedback batch, Item 14.4: this screen could only ever
       VIEW program-area assignments -- a real gap Jay asked to close, not
       just a display tweak. Immediate single-row action, no dirty/batch
       tracking, same convention this screen's own Roles Grant/Revoke
       already uses (unlike the dense multi-field GL Mapping/Global
       Approvers grids, which needed batching)."""
    user, err = _require_beacon_admin(request)
    if err:
        return err
    form = await request.form()
    try:
        program_area_id = int(form.get("program_area_id") or 0)
    except (TypeError, ValueError):
        program_area_id = 0
    if not program_area_id:
        return RedirectResponse(f"/admin/setup/users/{user_id}?error=Pick+a+program+area.", status_code=303)

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM checkreq.program_areas WHERE id = %s", (program_area_id,))
            if not cur.fetchone():
                return RedirectResponse(f"/admin/setup/users/{user_id}?error=Unknown+program+area.", status_code=303)
            cur.execute(
                "INSERT INTO checkreq.user_program_areas (user_id, program_area_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, program_area_id),
            )
    return RedirectResponse(f"/admin/setup/users/{user_id}?pa_granted=1", status_code=303)


@router.post("/admin/setup/users/{user_id}/revoke-pa")
async def user_revoke_program_area(user_id: int, request: Request):
    user, err = _require_beacon_admin(request)
    if err:
        return err
    form = await request.form()
    try:
        upa_id = int(form.get("upa_id") or 0)
    except (TypeError, ValueError):
        upa_id = 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM checkreq.user_program_areas WHERE id = %s AND user_id = %s",
                (upa_id, user_id),
            )
    return RedirectResponse(f"/admin/setup/users/{user_id}?pa_revoked=1", status_code=303)


@router.post("/admin/setup/users/{user_id}/grant")
async def user_grant_role(user_id: int, request: Request):
    user, err = _require_beacon_admin(request)
    if err:
        return err
    form = await request.form()
    role_key = (form.get("role_key") or "").strip()
    org_id_raw = (form.get("org_id") or "").strip()
    note = (form.get("note") or "").strip() or None

    if not role_key:
        return RedirectResponse(f"/admin/setup/users/{user_id}?error=Pick+a+role.", status_code=303)

    if org_id_raw == "all":
        rbac.grant_role_all_entities(user_id, role_key, user["id"], note)
    else:
        try:
            org_id = int(org_id_raw)
        except (TypeError, ValueError):
            return RedirectResponse(f"/admin/setup/users/{user_id}?error=Pick+an+entity.", status_code=303)
        rbac.grant_role(user_id, org_id, role_key, user["id"], note)

    return RedirectResponse(f"/admin/setup/users/{user_id}?granted=1", status_code=303)


@router.post("/admin/setup/users/{user_id}/revoke")
async def user_revoke_role(user_id: int, request: Request):
    user, err = _require_beacon_admin(request)
    if err:
        return err
    form = await request.form()
    role_key = (form.get("role_key") or "").strip()
    try:
        org_id = int(form.get("org_id") or 0)
    except (TypeError, ValueError):
        return RedirectResponse(f"/admin/setup/users/{user_id}?error=Bad+request.", status_code=303)

    try:
        rbac.revoke_role(user_id, org_id, role_key, user["id"])
    except rbac.LastAdminError as exc:
        return RedirectResponse(f"/admin/setup/users/{user_id}?error={exc}", status_code=303)

    return RedirectResponse(f"/admin/setup/users/{user_id}?revoked=1", status_code=303)
