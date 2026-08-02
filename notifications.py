"""
notifications.py — In-App Notifications (In-App Notifications Plan.md,
2026-07-31/2026-08-02). A bell icon + unread badge in base.html's header,
on every page, plus a rolling dropdown of the current user's most recent
notifications.

Per the plan's own "Still open" answers, followed literally rather than
re-decided:
  - The notification list is NOT scoped to whichever entity is currently
    selected in the header -- someone overseeing both EDOM and Claggett
    shouldn't be able to miss something just because the other entity
    happened to be selected. No org_id column exists on this table.
  - Only the CFO budget-overage notice (main.py's
    _send_budget_buffer_notice_email) populates this to start. Other
    event types are additive later -- notification_type is a free string
    for exactly this reason.
  - No separate "all notifications" history page in this pass -- the
    rolling ~20-item dropdown is the whole surface.

Schema: migrations/023_notifications.sql (checkreq.notifications).
create_notification()/get_unread_count()/get_recent() below all degrade
gracefully (fail open to a no-op / 0 / empty list, never raise) if that
migration somehow isn't applied yet in a given environment -- the same
graceful-degradation approach already proven for admin_users_detail.html's
first_name column and the GL Mapping/Global Approvers screens' missing
columns. A notification is always a secondary side-effect of some other
real action (an email send, a budget check) -- it must never be the thing
that breaks that action.

create_notification() is imported directly as a plain module-level
function (not injected via register()) since it needs to be callable from
main.py's own _send_budget_buffer_notice_email, which is defined and
called long before this module's own HTTP routes are wired in at the
bottom of main.py -- the same "plain import used both early and late"
shape as db.py/rbac.py, not the register()-injection pattern admin_setup.py/
admin_users.py/access_requests.py use (those need current_user/render
injected because their OWN routes need them; this module's
create_notification() needs neither). register() below only wires in the
two HTTP routes the bell's own dropdown JS calls, and takes just
current_user for that reason.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import db

router = APIRouter()

_current_user = None


def register(app, *, current_user) -> None:
    global _current_user
    _current_user = current_user
    app.include_router(router)


def create_notification(user_id: int, notification_type: str, message: str, link_url: str | None) -> None:
    """Fails open -- a notification-write hiccup must never break whatever
       real action (an email send, a submission) triggered it, and this
       silently no-ops if checkreq.notifications doesn't exist yet in a
       given environment rather than raising."""
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO checkreq.notifications "
                    "(user_id, notification_type, message, link_url) VALUES (%s, %s, %s, %s)",
                    (user_id, notification_type, message, link_url),
                )
    except Exception as exc:
        print(f"[notifications] create_notification failed for user {user_id}: {exc}")


def get_unread_count(user_id: int) -> int:
    """Computed at render time in main.py's _render() -- every page shows a
       live-as-of-this-page-load count, no push/websocket needed (per the
       plan: staff check periodically, this isn't a live chat)."""
    try:
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM checkreq.notifications WHERE user_id = %s AND read_at IS NULL",
            (user_id,),
        )
        return int(row["n"]) if row else 0
    except Exception:
        return 0


def get_recent(user_id: int, limit: int = 20) -> list[dict]:
    try:
        return db.query(
            "SELECT id, notification_type, message, link_url, created_at, read_at "
            "FROM checkreq.notifications WHERE user_id = %s "
            "ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        )
    except Exception:
        return []


@router.get("/api/notifications")
def api_notifications(request: Request):
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    rows = get_recent(user["id"])
    return JSONResponse({
        "notifications": [
            {
                "id": r["id"],
                "notification_type": r["notification_type"],
                "message": r["message"],
                "link_url": r["link_url"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "read": r["read_at"] is not None,
            }
            for r in rows
        ],
    })


@router.post("/api/notifications/{notification_id}/read")
async def api_notification_read(notification_id: int, request: Request):
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                # Scoped to user_id -- a notification can only ever be
                # marked read by the person it belongs to, same ownership
                # discipline as every other mutation in this codebase.
                cur.execute(
                    "UPDATE checkreq.notifications SET read_at = NOW() "
                    "WHERE id = %s AND user_id = %s AND read_at IS NULL",
                    (notification_id, user["id"]),
                )
    except Exception as exc:
        print(f"[notifications] mark-read failed for notification {notification_id}: {exc}")
        return JSONResponse({"ok": False}, status_code=500)
    return JSONResponse({"ok": True})
