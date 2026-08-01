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
    """One row per app_users row, with role chips and the three inline
       flags described in Plan §6.2. Two extra queries (approver-somewhere,
       program-area count) per user is fine at this table's real size
       (tens of rows, not thousands)."""
    users = db.query(
        "SELECT id, email, display_name, is_active, last_login_at, last_login_provider "
        "FROM checkreq.app_users ORDER BY email"
    )
    for u in users:
        u["roles"] = rbac.get_roles_for_user(u["id"])
        u["has_any_role"] = len(u["roles"]) > 0
        u["pa_count"] = db.query_one(
            "SELECT COUNT(*) AS n FROM checkreq.user_program_areas WHERE user_id = %s", (u["id"],)
        )["n"]
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
    user, err = _require_beacon_admin(request)
    if err:
        return err
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    display_name = (form.get("display_name") or "").strip() or None
    if not email:
        return JSONResponse({"error": "Email is required."}, status_code=400)

    existing = db.query_one("SELECT id FROM checkreq.app_users WHERE LOWER(email) = %s", (email,))
    if existing:
        return RedirectResponse(f"/admin/setup/users/{existing['id']}", status_code=303)

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkreq.app_users (email, display_name, is_active) "
                "VALUES (%s, %s, TRUE) RETURNING id",
                (email, display_name or email.split("@")[0]),
            )
            new_id = cur.fetchone()["id"]
    return RedirectResponse(f"/admin/setup/users/{new_id}", status_code=303)


@router.get("/admin/setup/users/{user_id}", response_class=HTMLResponse)
def user_detail_page(user_id: int, request: Request):
    user, err = _require_beacon_admin(request)
    if err:
        return err
    target = db.query_one("SELECT * FROM checkreq.app_users WHERE id = %s", (user_id,))
    if not target:
        return RedirectResponse("/admin/setup/users")

    program_areas = db.query(
        """
        SELECT pa.title AS program_area_title, o.code AS org_code
          FROM checkreq.user_program_areas upa
          JOIN checkreq.program_areas pa ON pa.id = upa.program_area_id
          JOIN checkreq.organizations o ON o.id = pa.org_id
         WHERE upa.user_id = %s
         ORDER BY o.code, pa.title
        """,
        (user_id,),
    )
    return _render(request, "admin_users_detail.html", user, {
        "target": target,
        "roles": rbac.get_roles_for_user(user_id),
        "all_roles": rbac.all_roles(),
        "orgs": db.query("SELECT id, code, name FROM checkreq.organizations WHERE is_active ORDER BY name"),
        "program_areas": program_areas,
    })


@router.post("/admin/setup/users/{user_id}/update")
async def user_update(user_id: int, request: Request):
    user, err = _require_beacon_admin(request)
    if err:
        return err
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    display_name = (form.get("display_name") or "").strip() or None
    is_active = form.get("is_active") == "on"
    if not email:
        return JSONResponse({"error": "Email is required."}, status_code=400)

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.app_users SET email = %s, display_name = %s, is_active = %s WHERE id = %s",
                (email, display_name, is_active, user_id),
            )
    return RedirectResponse(f"/admin/setup/users/{user_id}?saved=1", status_code=303)


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
