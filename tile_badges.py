"""
tile_badges.py — unread-count badges for /portal's module tiles
(2026-08-08, Jay: "put a number in the upper right hand of the box that
shows the number of unread items... if there are new items that have not
been seen yet, bold the number").

Design: each relevant tile gets {count, is_new}. `count` is always the
total open/pending item count visible to this user for that tile (so the
number itself never disappears just because you've looked at it — matches
every other queue/badge in this app). `is_new` is True only when at least
one of those open items has its own timestamp AFTER this user's last
recorded visit to that tile (checkreq.tile_badge_state) — a plain "did the
total count change" comparison would miss the case where one item resolved
and a different one appeared in the same window (net count unchanged, but
genuinely new content) and would also wrongly stay bold forever once
triggered. Visiting a tile's real page calls mark_viewed(), which is
main.py's/each module's job to call, not this module's — tile_badges.py
only computes and records, it renders nothing and owns no route.

New file per NFR-11 / the standing main.py rule — get_badges() is called
directly from main.py's _render(), the same pattern notifications.
get_unread_count() already established.
"""
from __future__ import annotations

import db
import rbac
import parish_roles


def _last_viewed(user_id: int, tile_key: str):
    row = db.query_one(
        "SELECT last_viewed_at FROM checkreq.tile_badge_state WHERE user_id = %s AND tile_key = %s",
        (user_id, tile_key),
    )
    return row["last_viewed_at"] if row else None


def mark_viewed(user_id: int, tile_key: str) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkreq.tile_badge_state (user_id, tile_key, last_viewed_at) "
                "VALUES (%s, %s, NOW()) "
                "ON CONFLICT (user_id, tile_key) DO UPDATE SET last_viewed_at = NOW()",
                (user_id, tile_key),
            )


def _badge(count: int, newest_at, last_viewed) -> dict | None:
    if count <= 0:
        return None
    is_new = bool(newest_at and (not last_viewed or newest_at > last_viewed))
    return {"count": count, "is_new": is_new}


def get_badges(user_id: int, org_id: int | None) -> dict[str, dict]:
    """Returns {tile_key: {"count", "is_new"}} for every tile with at least
    one open item this user can see -- a tile with nothing pending is
    simply absent from the dict (portal.html renders no badge at all)."""
    badges: dict[str, dict] = {}

    # Approval Queue -- this user's own pending approval_actions rows.
    row = db.query_one(
        "SELECT count(*) AS n, max(aa.created_at) AS newest "
        "FROM checkreq.approval_actions aa "
        "JOIN checkreq.payment_requests pr ON pr.id = aa.payment_request_id "
        "WHERE aa.approver_user_id = %s AND aa.status = 'pending' AND pr.org_id = %s",
        (user_id, org_id),
    )
    b = _badge(row["n"], row["newest"], _last_viewed(user_id, "approval_queue"))
    if b:
        badges["approval_queue"] = b

    # AP Review -- only meaningful for an actual AP reviewer (existence
    # check, cross-entity -- matches ap_review_list()'s own gate). The COUNT
    # itself is scoped to rbac.get_granted_org_ids() (2026-08-16, matching
    # ap_review_list()'s own fix) -- used to have no org filter at all,
    # which would have made this badge disagree with the screen the moment
    # a narrowly-granted parish-org approver exists.
    if rbac.user_has_role(user_id, "ap_reviewer", org_id=None):
        granted_org_ids = rbac.get_granted_org_ids(user_id, "ap_reviewer")
        row = db.query_one(
            "SELECT count(*) AS n, max(updated_at) AS newest "
            "FROM checkreq.payment_requests WHERE status = 'Approved' AND org_id = ANY(%s)",
            (granted_org_ids,),
        )
        b = _badge(row["n"], row["newest"], _last_viewed(user_id, "ap_review"))
        if b:
            badges["ap_review"] = b

    # Request Access -- only for a reviewer (beacon_admin or parish_admin
    # somewhere); scoped the same way parish_access.py's own queue is.
    is_beacon_admin = rbac.user_has_role(user_id, "beacon_admin", org_id=None)
    parish_admin_ids = parish_roles.get_parish_ids_with_role(user_id, "parish_admin")
    if is_beacon_admin or parish_admin_ids:
        scoped_ids = None if is_beacon_admin else parish_admin_ids
        row = db.query_one(
            "SELECT count(*) AS n, max(requested_at) AS newest "
            "FROM portal.parish_access_requests "
            "WHERE status = 'pending' AND (%s::int[] IS NULL OR parish_id = ANY(%s))",
            (scoped_ids, scoped_ids),
        )
        b = _badge(row["n"], row["newest"], _last_viewed(user_id, "request_parish_access"))
        if b:
            badges["request_parish_access"] = b

    # Parish Requests -- same reviewer scoping as admin_parish_requests_page.
    # Query duplicated from parish_requests.list_for_review() rather than
    # imported -- that module would need to import this one back for its
    # own mark_viewed() call, and this codebase's own established pattern
    # (parish_mode.py's docstring, admin_hub.py's _ADMIN_TASK_ROLE_KEYS) is
    # to accept a small duplication over a cross-module import cycle.
    if is_beacon_admin or parish_admin_ids:
        scoped_ids = None if is_beacon_admin else parish_admin_ids
        row = db.query_one(
            "SELECT count(*) AS n, max(created_at) AS newest FROM portal.parish_requests "
            "WHERE status != 'Closed' AND (%s::int[] IS NULL OR parish_id = ANY(%s))",
            (scoped_ids, scoped_ids),
        )
        b = _badge(row["n"], row["newest"], _last_viewed(user_id, "parish_requests_review"))
        if b:
            badges["parish_requests_review"] = b

    return badges
