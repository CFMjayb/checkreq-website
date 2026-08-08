"""
announcements.py — Parish Portal S4: diocese announcements, targeted +
dated (Parish Portal Plan.md Section 4/9.2, PP-201-204).

New file per NFR-11 / the standing main.py rule. Register()-injection
pattern, same as every other Parish Portal module.

Jay: "announcements posted can be tagged for target user groups (per RBAC),
and dated for when they appear." target_parish_ids/target_roles are both
nullable Postgres arrays -- NULL or an empty array both mean "no
restriction" (every parish in the org / every role), never a separate
sentinel. The parish-facing feed (list_for_session, called from
GET /api/announcements/mine) filters purely server-side, deriving the
viewer's parish from parish_mode.effective_parish_mode() -- never trusts a
client-supplied parish_id, same discipline as every other parish-scoped
route in this codebase.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
import rbac
import registry
import parish_roles
import parish_mode

router = APIRouter()

_current_user = None
_render = None


def register(app, *, current_user, render) -> None:
    global _current_user, _render
    _current_user, _render = current_user, render
    app.include_router(router)


def _require_admin(request: Request):
    user = _current_user(request)
    if not user:
        return None, RedirectResponse("/login")
    if not rbac.user_has_any_role(user["id"], ["setup_admin", "beacon_admin"], org_id=None):
        return None, JSONResponse({"error": "Setup Admin or Beacon Admin access required"}, status_code=403)
    return user, None


# ── Data access ────────────────────────────────────────────────────────────

def list_all(include_inactive: bool = False) -> list[dict]:
    sql = (
        "SELECT a.*, o.code AS org_code, o.name AS org_name, "
        "u.display_name AS created_by_name "
        "FROM portal.announcements a "
        "JOIN checkreq.organizations o ON o.id = a.org_id "
        "LEFT JOIN checkreq.app_users u ON u.id = a.created_by_user_id"
    )
    if not include_inactive:
        sql += " WHERE a.is_active"
    sql += " ORDER BY a.publish_at DESC"
    return db.query(sql)


def get_announcement(announcement_id: int) -> dict | None:
    return db.query_one("SELECT * FROM portal.announcements WHERE id = %s", (announcement_id,))


def create_announcement(org_id: int, title: str, body: str, target_parish_ids: list[int] | None,
                         target_roles: list[str] | None, publish_at: datetime, expires_at: datetime | None,
                         created_by_user_id: int) -> int:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.announcements "
                "(org_id, title, body, target_parish_ids, target_roles, publish_at, expires_at, created_by_user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (org_id, title, body, target_parish_ids or None, target_roles or None,
                 publish_at, expires_at, created_by_user_id),
            )
            return cur.fetchone()["id"]


def update_announcement(announcement_id: int, title: str, body: str, target_parish_ids: list[int] | None,
                         target_roles: list[str] | None, publish_at: datetime, expires_at: datetime | None) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.announcements SET title=%s, body=%s, target_parish_ids=%s, "
                "target_roles=%s, publish_at=%s, expires_at=%s, updated_at=NOW() WHERE id = %s",
                (title, body, target_parish_ids or None, target_roles or None,
                 publish_at, expires_at, announcement_id),
            )


def set_active(announcement_id: int, is_active: bool) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.announcements SET is_active = %s, updated_at = NOW() WHERE id = %s",
                (is_active, announcement_id),
            )


def list_for_session(request: Request, user: dict) -> list[dict]:
    """The parish-facing feed. Derives the viewer's parish itself (never
    trusts a client-supplied id) -- returns [] if the session isn't
    currently viewing any parish (native or CFO preview)."""
    parish, _ = parish_mode.effective_parish_mode(request, user)
    if not parish:
        return []
    role_keys = parish_roles.get_parish_role_keys(user["id"], parish["id"])
    rows = db.query(
        """
        SELECT id, title, body, publish_at, expires_at, target_roles
          FROM portal.announcements
         WHERE org_id = %s AND is_active
           AND publish_at <= NOW()
           AND (expires_at IS NULL OR expires_at > NOW())
           AND (target_parish_ids IS NULL OR cardinality(target_parish_ids) = 0
                OR %s = ANY(target_parish_ids))
         ORDER BY publish_at DESC
        """,
        (parish["org_id"], parish["id"]),
    )
    out = []
    for r in rows:
        troles = r.get("target_roles")
        if not troles or (role_keys & set(troles)):
            out.append(r)
    return out


# ── Parish-facing API ─────────────────────────────────────────────────────

@router.get("/api/announcements/mine")
def announcements_mine(request: Request):
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "login required"}, status_code=401)
    rows = list_for_session(request, user)
    return JSONResponse({"announcements": [
        {
            "id": r["id"], "title": r["title"], "body": r["body"],
            "publish_at": r["publish_at"].isoformat() if r.get("publish_at") else None,
        }
        for r in rows
    ]})


# ── Diocesan admin CRUD ────────────────────────────────────────────────────

def _parse_form_lists(form) -> tuple[list[int], list[str]]:
    parish_ids = [int(v) for v in form.getlist("target_parish_ids") if str(v).strip().isdigit()]
    role_keys = [v for v in form.getlist("target_roles") if v]
    return parish_ids, role_keys


@router.get("/admin/announcements", response_class=HTMLResponse)
def admin_announcements_page(request: Request):
    user, err = _require_admin(request)
    if err:
        return err
    return _render(request, "admin_announcements.html", user, {
        "rows": list_all(include_inactive=True),
        "orgs": db.query("SELECT id, code, name FROM checkreq.organizations WHERE is_active ORDER BY name"),
        "parishes": registry.list_all_parishes(),
        "roles": parish_roles.all_parish_roles(),
        "editing": None,
    })


@router.get("/admin/announcements/{announcement_id}/edit", response_class=HTMLResponse)
def admin_announcements_edit_page(announcement_id: int, request: Request):
    user, err = _require_admin(request)
    if err:
        return err
    editing = get_announcement(announcement_id)
    return _render(request, "admin_announcements.html", user, {
        "rows": list_all(include_inactive=True),
        "orgs": db.query("SELECT id, code, name FROM checkreq.organizations WHERE is_active ORDER BY name"),
        "parishes": registry.list_all_parishes(),
        "roles": parish_roles.all_parish_roles(),
        "editing": editing,
    })


@router.post("/admin/announcements")
async def admin_announcements_create(request: Request):
    user, err = _require_admin(request)
    if err:
        return err
    form = await request.form()
    org_id = int(form.get("org_id") or 0)
    title = (form.get("title") or "").strip()
    body = (form.get("body") or "").strip()
    publish_at = form.get("publish_at") or None
    expires_at = form.get("expires_at") or None
    target_parish_ids, target_roles = _parse_form_lists(form)
    if not (org_id and title and body):
        return RedirectResponse("/admin/announcements?error=1", status_code=303)
    create_announcement(
        org_id, title, body, target_parish_ids, target_roles,
        publish_at or datetime.utcnow(), expires_at, user["id"],
    )
    return RedirectResponse("/admin/announcements?created=1", status_code=303)


@router.post("/admin/announcements/{announcement_id}")
async def admin_announcements_update(announcement_id: int, request: Request):
    user, err = _require_admin(request)
    if err:
        return err
    form = await request.form()
    title = (form.get("title") or "").strip()
    body = (form.get("body") or "").strip()
    publish_at = form.get("publish_at") or datetime.utcnow()
    expires_at = form.get("expires_at") or None
    target_parish_ids, target_roles = _parse_form_lists(form)
    if title and body:
        update_announcement(announcement_id, title, body, target_parish_ids, target_roles, publish_at, expires_at)
    return RedirectResponse("/admin/announcements?updated=1", status_code=303)


@router.post("/admin/announcements/{announcement_id}/deactivate")
def admin_announcements_deactivate(announcement_id: int, request: Request):
    user, err = _require_admin(request)
    if err:
        return err
    set_active(announcement_id, False)
    return RedirectResponse("/admin/announcements?deactivated=1", status_code=303)


@router.post("/admin/announcements/{announcement_id}/reactivate")
def admin_announcements_reactivate(announcement_id: int, request: Request):
    user, err = _require_admin(request)
    if err:
        return err
    set_active(announcement_id, True)
    return RedirectResponse("/admin/announcements?reactivated=1", status_code=303)
