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

2026-08-16 addendum (Phase H, Cornerstone Served Parishes Plan.md item 20):
scheduling (publish_at/expires_at) and the admin console (this file's own
CRUD + admin_hub.py's card) were both already fully built as of the
original 2026-08-08 session -- confirmed by reading this file and its
template before touching anything, nothing to add for either. What was
genuinely missing, resolved by direct code reading (not guessed): the
existing target_parish_ids/target_roles columns only ever express 2 of the
plan's 4 scope levels ("specific-parishes", "all-parishes-in-diocese"),
and the delivery mechanism (list_for_session) only ever fires for a
session actively viewing SOME parish -- so neither "all-employees" nor
"served-entities" scoping was reachable at all. migrations/
039_announcements_target_scope.sql (NOT yet applied -- needs Jay's sign-off
per the standing schema-change rule) adds the real target_scope column;
every target_scope-aware query/write below fails over gracefully to the
pre-migration behavior (try/except around the new SQL shape) so this file
stays safely deployable before that migration lands, same graceful-
degradation convention notifications.py/cornerstone_mode.py already use.

Also new this pass: creating (or reactivating) an announcement that is
immediately live (publish_at <= now) now pushes a real notification into
the ALREADY-EXISTING notification bell (notifications.py, built
2026-08-02 -- a generic, page-agnostic bell shown on every page for every
signed-in user, not something built fresh here). This is deliberately a
one-shot push at creation/reactivation time only -- a future-dated
(scheduled) announcement will still correctly appear in the parish-facing
feed once its publish_at arrives (list_for_session's own time filter
already handles that), but this app has no background scheduler to revisit
it and push a bell notification retroactively at that moment. That's a
real, honest gap, not silently worked around by inventing a cron job that
doesn't exist anywhere else in this Cloud-Run-web-service codebase (unlike
26-124's Cloud Run Jobs, which do have one).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
import rbac
import registry
import parish_roles
import parish_mode
import notifications

_TARGET_SCOPES = ("all_employees", "served_entities", "specific_parishes", "all_parishes_in_diocese")

router = APIRouter()

_current_user = None
_current_org = None
_render = None


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


def _require_admin(request: Request):
    """2026-08-16 fix: was org_id=None (cross-diocese existence check) --
    a real gap flagged, not fixed, earlier the same day (see
    Cornerstone Served Parishes Plan.md's own "Open judgment call" note
    and this project's Decision 5: "beacon_admin sees every parish/entity
    within the diocese they're currently switched into... reaching a
    different diocese means switching entities first"). Unlike AP Review/
    Vendor Requests (a deliberate, Jay-confirmed cross-entity oversight
    view), there's no equivalent "must see every diocese's announcements
    at once" workflow here -- so this follows the SIMPLER, stricter rule
    Decision 5 already settled for parish-listing screens: scoped to the
    CURRENTLY SELECTED entity, not "any org this admin holds the role at
    somewhere." Switching entities (top nav) reaches a different diocese's
    announcements, same as Manage Parishes / the Parish Mode picker."""
    user = _current_user(request)
    if not user:
        return None, RedirectResponse("/login")
    org = _current_org(request)
    if not org or not rbac.user_has_any_role(user["id"], ["setup_admin", "beacon_admin"], org_id=org["id"]):
        return None, JSONResponse({"error": "Setup Admin or Beacon Admin access required"}, status_code=403)
    return user, None


# ── Data access ────────────────────────────────────────────────────────────

def list_all(org_id: int, include_inactive: bool = False) -> list[dict]:
    """2026-08-16 fix: was unscoped (every announcement across every
    diocese) -- now scoped to the current entity, matching _require_admin's
    own fix above."""
    sql = (
        "SELECT a.*, o.code AS org_code, o.name AS org_name, "
        "u.display_name AS created_by_name "
        "FROM portal.announcements a "
        "JOIN checkreq.organizations o ON o.id = a.org_id "
        "LEFT JOIN checkreq.app_users u ON u.id = a.created_by_user_id "
        "WHERE a.org_id = %s"
    )
    if not include_inactive:
        sql += " AND a.is_active"
    sql += " ORDER BY a.publish_at DESC"
    return db.query(sql, (org_id,))


def get_announcement(announcement_id: int) -> dict | None:
    return db.query_one("SELECT * FROM portal.announcements WHERE id = %s", (announcement_id,))


def create_announcement(org_id: int, title: str, body: str, target_parish_ids: list[int] | None,
                         target_roles: list[str] | None, publish_at: datetime, expires_at: datetime | None,
                         created_by_user_id: int, target_scope: str = "all_parishes_in_diocese") -> int:
    """target_scope is written best-effort -- if migrations/
    039_announcements_target_scope.sql hasn't been applied yet in this
    environment, the INSERT naming that column fails (undefined_column)
    and this falls back to the original pre-migration INSERT shape
    (identical to this function's 2026-08-08 form) rather than crashing the
    whole Create action. The scope selection is silently dropped in that
    case -- same "degrade, don't break" convention as notifications.py/
    cornerstone_mode.py -- until the migration lands."""
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO portal.announcements "
                    "(org_id, title, body, target_parish_ids, target_roles, target_scope, "
                    " publish_at, expires_at, created_by_user_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (org_id, title, body, target_parish_ids or None, target_roles or None,
                     target_scope, publish_at, expires_at, created_by_user_id),
                )
                return cur.fetchone()["id"]
    except Exception:
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
                         target_roles: list[str] | None, publish_at: datetime, expires_at: datetime | None,
                         target_scope: str = "all_parishes_in_diocese") -> None:
    """Same best-effort target_scope write as create_announcement -- see
    that function's own docstring."""
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE portal.announcements SET title=%s, body=%s, target_parish_ids=%s, "
                    "target_roles=%s, target_scope=%s, publish_at=%s, expires_at=%s, updated_at=NOW() WHERE id = %s",
                    (title, body, target_parish_ids or None, target_roles or None,
                     target_scope, publish_at, expires_at, announcement_id),
                )
    except Exception:
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
    currently viewing any parish (native or CFO preview).

    Note this is the PARISH feed only -- an 'all_employees'-scoped
    announcement deliberately never appears here (there is no parish for it
    to be "about"; it's delivered via the notification bell only, at
    creation/reactivation time -- see _notify_new_announcement below)."""
    parish, _ = parish_mode.effective_parish_mode(request, user)
    if not parish:
        return []
    role_keys = parish_roles.get_parish_role_keys(user["id"], parish["id"])
    parish_is_served = parish.get("linked_org_id") is not None
    try:
        rows = db.query(
            """
            SELECT id, title, body, publish_at, expires_at, target_roles, target_scope
              FROM portal.announcements
             WHERE org_id = %s AND is_active
               AND publish_at <= NOW()
               AND (expires_at IS NULL OR expires_at > NOW())
               AND target_scope != 'all_employees'
               AND (
                    target_scope = 'all_parishes_in_diocese'
                    OR (target_scope = 'specific_parishes'
                        AND %s = ANY(COALESCE(target_parish_ids, ARRAY[]::int[])))
                    OR (target_scope = 'served_entities' AND %s)
               )
             ORDER BY publish_at DESC
            """,
            (parish["org_id"], parish["id"], parish_is_served),
        )
    except Exception:
        # migrations/039_announcements_target_scope.sql not applied yet in
        # this environment -- fall back to the pre-target_scope behavior
        # (target_parish_ids only), same graceful-degradation convention as
        # cornerstone_mode.py/notifications.py.
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


# ── Notification-bell integration (2026-08-16, Phase H) ─────────────────────

def _is_effectively_live(row: dict) -> bool:
    """True if this row's publish_at is now or in the past -- the bell push
    only ever fires for an announcement that is immediately live at the
    moment it's created/reactivated (see module docstring for why a
    future-dated one isn't retroactively pushed)."""
    publish_at = row.get("publish_at")
    if publish_at is None:
        return True
    now = datetime.now(timezone.utc) if publish_at.tzinfo else datetime.utcnow()
    return publish_at <= now


def _resolve_notification_user_ids(row: dict) -> list[int]:
    """Every active user who should get a bell notification for this
    announcement, right now. Reuses already-proven building blocks from
    this codebase rather than a new one-off audience query:
      - 'all_employees'   -> rbac.users_with_org_access() (the same "who
        already has a real reason to be in this org" set the Program Area
        submitter/approver pickers already use).
      - the 3 parish-audience scopes -> portal.parish_user_roles, filtered
        by parish set (specific list / served-only / every parish in the
        org) and, if target_roles is set, by role_key too."""
    scope = row.get("target_scope") or "all_parishes_in_diocese"
    org_id = row["org_id"]
    target_roles = row.get("target_roles") or None

    if scope == "all_employees":
        return [u["id"] for u in rbac.users_with_org_access(org_id)]

    params: list = [org_id]
    if scope == "specific_parishes":
        parish_ids = row.get("target_parish_ids") or []
        if not parish_ids:
            return []
        parish_filter_sql = "p.id = ANY(%s)"
        params.append(parish_ids)
    elif scope == "served_entities":
        parish_filter_sql = "p.linked_org_id IS NOT NULL"
    else:  # all_parishes_in_diocese
        parish_filter_sql = "TRUE"

    role_filter_sql = "TRUE"
    if target_roles:
        role_filter_sql = "pur.role_key = ANY(%s)"
        params.append(target_roles)

    sql = (
        "SELECT DISTINCT pur.user_id AS id "
        "FROM portal.parish_user_roles pur "
        "JOIN portal.parishes p ON p.id = pur.parish_id "
        "JOIN portal.parish_roles pr ON pr.key = pur.role_key AND pr.is_active "
        "JOIN checkreq.app_users u ON u.id = pur.user_id AND u.is_active "
        f"WHERE pur.revoked_at IS NULL AND p.org_id = %s AND {parish_filter_sql} AND {role_filter_sql}"
    )
    return [r["id"] for r in db.query(sql, tuple(params))]


def _notify_new_announcement(row: dict) -> None:
    """Best-effort bell push -- never raises, never blocks the Create/
    Reactivate action it's called from (same fail-open discipline as
    notifications.create_notification itself, which this wraps)."""
    if not row or not row.get("is_active", True) or not _is_effectively_live(row):
        return
    try:
        link = None if row.get("target_scope") == "all_employees" else "/parish-view"
        for user_id in _resolve_notification_user_ids(row):
            notifications.create_notification(user_id, "announcement", row["title"], link)
    except Exception as exc:
        print(f"[announcements] bell notify failed for announcement {row.get('id')}: {exc}")


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


def _parse_target_scope(form) -> str:
    """Whitelisted against _TARGET_SCOPES -- an unrecognized/blank value
    (e.g. the form field not present at all, on an environment where the
    template hasn't been redeployed yet) falls back to the column's own
    default, never a raw client-supplied string."""
    val = (form.get("target_scope") or "").strip()
    return val if val in _TARGET_SCOPES else "all_parishes_in_diocese"


@router.get("/admin/announcements", response_class=HTMLResponse)
def admin_announcements_page(request: Request):
    user, err = _require_admin(request)
    if err:
        return err
    org = _current_org(request)
    return _render(request, "admin_announcements.html", user, {
        "rows": list_all(org["id"], include_inactive=True),
        "current_org": org,
        "parishes": registry.list_parishes(org["id"]),
        "roles": parish_roles.all_parish_roles(),
        "editing": None,
    })


@router.get("/admin/announcements/{announcement_id}/edit", response_class=HTMLResponse)
def admin_announcements_edit_page(announcement_id: int, request: Request):
    user, err = _require_admin(request)
    if err:
        return err
    org = _current_org(request)
    editing = get_announcement(announcement_id)
    # 2026-08-16: an announcement belonging to a DIFFERENT entity than the
    # one currently selected must not be editable here -- same "tile/route
    # must match" discipline as every other record-level check fixed
    # earlier today (this is a record-level check, scoped to the record's
    # own org, not the session's current selection -- but since editing
    # requires this exact page's own org-scoped parish/role pickers to make
    # sense, we require the two to match here rather than silently
    # resolving the "wrong" org's picker options).
    if not editing or editing["org_id"] != org["id"]:
        return RedirectResponse("/admin/announcements")
    return _render(request, "admin_announcements.html", user, {
        "rows": list_all(org["id"], include_inactive=True),
        "current_org": org,
        "parishes": registry.list_parishes(org["id"]),
        "roles": parish_roles.all_parish_roles(),
        "editing": editing,
    })


@router.post("/admin/announcements")
async def admin_announcements_create(request: Request):
    user, err = _require_admin(request)
    if err:
        return err
    org = _current_org(request)
    form = await request.form()
    title = (form.get("title") or "").strip()
    body = (form.get("body") or "").strip()
    publish_at = form.get("publish_at") or None
    expires_at = form.get("expires_at") or None
    target_parish_ids, target_roles = _parse_form_lists(form)
    target_scope = _parse_target_scope(form)
    if not (title and body):
        return RedirectResponse("/admin/announcements?error=1", status_code=303)
    new_id = create_announcement(
        org["id"], title, body, target_parish_ids, target_roles,
        publish_at or datetime.utcnow(), expires_at, user["id"], target_scope,
    )
    _notify_new_announcement(get_announcement(new_id))
    return RedirectResponse("/admin/announcements?created=1", status_code=303)


@router.post("/admin/announcements/{announcement_id}")
async def admin_announcements_update(announcement_id: int, request: Request):
    user, err = _require_admin(request)
    if err:
        return err
    org = _current_org(request)
    existing = get_announcement(announcement_id)
    if not existing or existing["org_id"] != org["id"]:
        return RedirectResponse("/admin/announcements")
    form = await request.form()
    title = (form.get("title") or "").strip()
    body = (form.get("body") or "").strip()
    publish_at = form.get("publish_at") or datetime.utcnow()
    expires_at = form.get("expires_at") or None
    target_parish_ids, target_roles = _parse_form_lists(form)
    target_scope = _parse_target_scope(form)
    if title and body:
        # Deliberately does NOT push a bell notification on a plain edit --
        # only Create and Reactivate do (see those routes' own comments).
        # A typo fix or retargeting tweak on an already-live announcement
        # re-notifying everyone on every save would be noisy, not helpful;
        # if Jay wants an explicit "re-notify" action later, that's a small,
        # separate addition (a dedicated button calling the same
        # _notify_new_announcement helper), not default edit behavior.
        update_announcement(announcement_id, title, body, target_parish_ids, target_roles,
                             publish_at, expires_at, target_scope)
    return RedirectResponse("/admin/announcements?updated=1", status_code=303)


@router.post("/admin/announcements/{announcement_id}/deactivate")
def admin_announcements_deactivate(announcement_id: int, request: Request):
    user, err = _require_admin(request)
    if err:
        return err
    org = _current_org(request)
    existing = get_announcement(announcement_id)
    if not existing or existing["org_id"] != org["id"]:
        return RedirectResponse("/admin/announcements")
    set_active(announcement_id, False)
    return RedirectResponse("/admin/announcements?deactivated=1", status_code=303)


@router.post("/admin/announcements/{announcement_id}/reactivate")
def admin_announcements_reactivate(announcement_id: int, request: Request):
    user, err = _require_admin(request)
    if err:
        return err
    org = _current_org(request)
    existing = get_announcement(announcement_id)
    if not existing or existing["org_id"] != org["id"]:
        return RedirectResponse("/admin/announcements")
    set_active(announcement_id, True)
    # Reactivating is a deliberate re-publish action (unlike a plain edit,
    # see admin_announcements_update's own comment) -- push the bell same
    # as a fresh Create.
    _notify_new_announcement(get_announcement(announcement_id))
    return RedirectResponse("/admin/announcements?reactivated=1", status_code=303)
