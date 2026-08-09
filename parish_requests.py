"""
parish_requests.py — Parish Portal 9.7 (General requests) + 9.9 (Feedback),
2026-08-08. New file per NFR-11 / the standing main.py rule.

Jay's explicit direction: a SEPARATE, simpler, parish-scoped flow from the
existing general checkreq.app_feedback / feedback_chat.py conversational
intake (entity-wide, untouched by this build). One table
(portal.parish_requests) with a `kind` tag ('feedback' | 'general_request')
covers both PRD sections -- proportionate for what a small parish office
needs, not a full ticketing system.

Review queue mirrors parish_access.py's exact dual-reviewer pattern: a
beacon_admin (any org) sees and can act on every parish's requests; a
parish_admin sees and can act on ONLY the parish(es) they administer. This
module deliberately re-derives that small check locally rather than
importing parish_access.py (the accepted small-duplication pattern already
used across parish_mode.py/admin_hub.py/access_requests.py for their own
local `_require_*` guards).
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
import rbac
import parish_roles
import parish_mode
import tile_badges

router = APIRouter()

_current_user = None
_render = None


def register(app, *, current_user, render) -> None:
    global _current_user, _render
    _current_user, _render = current_user, render
    app.include_router(router)


def _is_beacon_admin(user: dict) -> bool:
    return rbac.user_has_role(user["id"], "beacon_admin", org_id=None)


def _require_reviewer(request: Request):
    user = _current_user(request)
    if not user:
        return None, RedirectResponse("/login")
    if _is_beacon_admin(user):
        return user, None
    if parish_roles.get_parish_ids_with_role(user["id"], "parish_admin"):
        return user, None
    return None, JSONResponse({"error": "Beacon Admin or Parish Admin access required"}, status_code=403)


# ── Data access ────────────────────────────────────────────────────────────

def create_request(parish_id: int, user_id: int, kind: str, subject: str | None, message: str) -> int:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.parish_requests (parish_id, user_id, kind, subject, message) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (parish_id, user_id, kind, subject, message),
            )
            return cur.fetchone()["id"]


def list_mine(user_id: int) -> list[dict]:
    """Every request this user has ever submitted, across every parish they
    belong to -- a shared bookkeeper covering more than one parish (a real,
    named case in Parish Portal Plan.md Section 1) should see all of their
    own history in one place, not have to switch parishes to find an old
    submission."""
    return db.query(
        """
        SELECT pr.*, p.name AS parish_name
          FROM portal.parish_requests pr
          JOIN portal.parishes p ON p.id = pr.parish_id
         WHERE pr.user_id = %s
         ORDER BY pr.created_at DESC
        """,
        (user_id,),
    )


def get_request(request_id: int) -> dict | None:
    return db.query_one("SELECT * FROM portal.parish_requests WHERE id = %s", (request_id,))


def list_for_review(parish_ids: list[int] | None, include_closed: bool = False) -> list[dict]:
    sql = (
        "SELECT pr.*, p.name AS parish_name, p.org_id, o.code AS org_code, "
        "u.display_name AS submitter_name, u.email AS submitter_email "
        "FROM portal.parish_requests pr "
        "JOIN portal.parishes p ON p.id = pr.parish_id "
        "JOIN checkreq.organizations o ON o.id = p.org_id "
        "JOIN checkreq.app_users u ON u.id = pr.user_id "
        "WHERE (%s::int[] IS NULL OR pr.parish_id = ANY(%s))"
    )
    if not include_closed:
        sql += " AND pr.status != 'Closed'"
    sql += " ORDER BY pr.created_at"
    return db.query(sql, (parish_ids, parish_ids))


def respond(request_id: int, reviewer_user_id: int, status: str, review_note: str | None) -> None:
    if status not in ("Open", "Reviewed", "Closed"):
        status = "Reviewed"
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.parish_requests SET status = %s, reviewed_by_user_id = %s, "
                "reviewed_at = NOW(), review_note = %s WHERE id = %s",
                (status, reviewer_user_id, review_note, request_id),
            )


# ── Parish-facing routes ──────────────────────────────────────────────────

@router.get("/parish-requests", response_class=HTMLResponse)
def parish_requests_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    parish, _ = parish_mode.effective_parish_mode(request, user)
    if not parish:
        return RedirectResponse("/parish-view")
    return _render(request, "parish_requests.html", user, {
        "parish": parish, "mine": list_mine(user["id"]),
    })


@router.post("/parish-requests")
async def parish_requests_submit(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    parish, _ = parish_mode.effective_parish_mode(request, user)
    if not parish:
        return RedirectResponse("/parish-view")
    form = await request.form()
    kind = (form.get("kind") or "").strip()
    subject = (form.get("subject") or "").strip() or None
    message = (form.get("message") or "").strip()
    if kind not in ("feedback", "general_request") or not message:
        return RedirectResponse("/parish-requests?error=1", status_code=303)
    create_request(parish["id"], user["id"], kind, subject, message)
    return RedirectResponse("/parish-requests?submitted=1", status_code=303)


# ── Diocesan review queue ──────────────────────────────────────────────────

@router.get("/admin/parish-requests", response_class=HTMLResponse)
def admin_parish_requests_page(request: Request, view: str = "open"):
    user, err = _require_reviewer(request)
    if err:
        return err
    tile_badges.mark_viewed(user["id"], "parish_requests_review")
    is_admin = _is_beacon_admin(user)
    scoped_ids = None if is_admin else parish_roles.get_parish_ids_with_role(user["id"], "parish_admin")
    rows = list_for_review(scoped_ids, include_closed=(view == "all"))
    return _render(request, "admin_parish_requests.html", user, {
        "rows": rows, "view": view, "is_beacon_admin_reviewer": is_admin,
    })


@router.post("/admin/parish-requests/{request_id}/respond")
async def admin_parish_requests_respond(request_id: int, request: Request):
    user, err = _require_reviewer(request)
    if err:
        return err
    req = get_request(request_id)
    if not req:
        return RedirectResponse("/admin/parish-requests")
    if not _is_beacon_admin(user) and not parish_roles.user_has_parish_role(user["id"], "parish_admin", req["parish_id"]):
        return JSONResponse({"error": "You don't administer this request's parish."}, status_code=403)
    form = await request.form()
    status = (form.get("status") or "Reviewed").strip()
    note = (form.get("review_note") or "").strip() or None
    respond(request_id, user["id"], status, note)
    return RedirectResponse("/admin/parish-requests?responded=1", status_code=303)
