"""
parish_roles.py — role lookups for portal.parish_user_roles (Parish Portal
S3 — Parish Portal Plan.md Section 2/5).

Mirrors rbac.py's exact shape, scoped to parish_id (portal.parishes)
instead of org_id (checkreq.organizations) — a deliberately SEPARATE grant
system, not a nullable parish_id column on checkreq.user_roles. See
migrations/024_parish_roles.sql's header comment for why (the plan itself
flagged this as a design question; confirmed with Jay 2026-08-08 against
the same nullable-scope-column ambiguity checkreq.user_roles.org_id already
rejected once, citing the global_approvers.org_id incident).

No "last admin" guard here, unlike rbac.revoke_role's beacon_admin
protection — a parish losing its only parish_admin isn't the same class of
outage as losing the diocese's only beacon_admin: diocesan staff (still
holding beacon_admin) can always intervene at any parish. Worth revisiting
if that assumption ever proves wrong in practice.

Depends only on db.py, same one-way-dependency discipline as rbac.py.
"""
from __future__ import annotations

import db


def user_has_parish_role(user_id: int, role_key: str, parish_id: int | None = None) -> bool:
    """parish_id given -> does this user hold this role FOR THAT PARISH?
       parish_id None  -> for ANY parish? (deliberately cross-parish routes
                          only, mirroring rbac.user_has_role's org_id=None
                          convention)."""
    row = db.query_one(
        """
        SELECT 1
          FROM portal.parish_user_roles pur
          JOIN portal.parish_roles pr ON pr.key = pur.role_key AND pr.is_active
         WHERE pur.user_id = %s AND pur.role_key = %s AND pur.revoked_at IS NULL
           AND (%s::int IS NULL OR pur.parish_id = %s)
         LIMIT 1
        """,
        (user_id, role_key, parish_id, parish_id),
    )
    return row is not None


def user_has_any_parish_role(user_id: int, role_keys: list[str] | None = None,
                              parish_id: int | None = None) -> bool:
    row = db.query_one(
        """
        SELECT 1
          FROM portal.parish_user_roles pur
          JOIN portal.parish_roles pr ON pr.key = pur.role_key AND pr.is_active
         WHERE pur.user_id = %s AND pur.revoked_at IS NULL
           AND (%s::text[] IS NULL OR pur.role_key = ANY(%s))
           AND (%s::int IS NULL OR pur.parish_id = %s)
         LIMIT 1
        """,
        (user_id, role_keys, role_keys, parish_id, parish_id),
    )
    return row is not None


def get_parish_role_keys(user_id: int, parish_id: int | None = None) -> set[str]:
    rows = db.query(
        """
        SELECT DISTINCT pur.role_key
          FROM portal.parish_user_roles pur
          JOIN portal.parish_roles pr ON pr.key = pur.role_key AND pr.is_active
         WHERE pur.user_id = %s AND pur.revoked_at IS NULL
           AND (%s::int IS NULL OR pur.parish_id = %s)
        """,
        (user_id, parish_id, parish_id),
    )
    return {r["role_key"] for r in rows}


def get_parish_roles_for_user(user_id: int) -> list[dict]:
    """Every (parish, role) pair this user holds, ordered for display --
       mirrors rbac.get_roles_for_user's shape."""
    return db.query(
        """
        SELECT pur.id AS parish_user_role_id, pur.parish_id, p.name AS parish_name,
               p.org_id, o.code AS org_code,
               pur.role_key, pr.label AS role_label, pr.description AS role_description,
               pur.granted_at, g.email AS granted_by_email, pur.note
          FROM portal.parish_user_roles pur
          JOIN portal.parishes p ON p.id = pur.parish_id
          JOIN checkreq.organizations o ON o.id = p.org_id
          JOIN portal.parish_roles pr ON pr.key = pur.role_key
          LEFT JOIN checkreq.app_users g ON g.id = pur.granted_by_user_id
         WHERE pur.user_id = %s AND pur.revoked_at IS NULL
         ORDER BY p.name, pr.sort_order
        """,
        (user_id,),
    )


def get_users_with_parish_role(role_key: str, parish_id: int | None = None) -> list[dict]:
    return db.query(
        """
        SELECT DISTINCT u.id, u.email, u.display_name
          FROM portal.parish_user_roles pur
          JOIN checkreq.app_users u ON u.id = pur.user_id AND u.is_active
          JOIN portal.parish_roles pr ON pr.key = pur.role_key AND pr.is_active
         WHERE pur.role_key = %s AND pur.revoked_at IS NULL
           AND (%s::int IS NULL OR pur.parish_id = %s)
         ORDER BY u.display_name, u.email
        """,
        (role_key, parish_id, parish_id),
    )


def all_parish_roles(include_inactive: bool = False) -> list[dict]:
    sql = "SELECT key, label, description, sort_order, is_active FROM portal.parish_roles"
    if not include_inactive:
        sql += " WHERE is_active"
    sql += " ORDER BY sort_order"
    return db.query(sql)


# ── Writes ────────────────────────────────────────────────────────────────

def grant_parish_role(user_id: int, parish_id: int, role_key: str,
                      granted_by_user_id: int | None, note: str | None = None) -> None:
    """Idempotent, same discipline as rbac.grant_role."""
    existing = db.query_one(
        "SELECT id FROM portal.parish_user_roles "
        "WHERE user_id = %s AND parish_id = %s AND role_key = %s AND revoked_at IS NULL",
        (user_id, parish_id, role_key),
    )
    if existing:
        return
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.parish_user_roles "
                "(user_id, parish_id, role_key, granted_by_user_id, note) "
                "VALUES (%s, %s, %s, %s, %s)",
                (user_id, parish_id, role_key, granted_by_user_id, note),
            )


def revoke_parish_role(user_id: int, parish_id: int, role_key: str,
                       revoked_by_user_id: int, note: str | None = None) -> None:
    """UPDATE ... SET revoked_at = NOW() ... -- never DELETEs. No last-admin
       guard here -- see module docstring for why that's a deliberate
       difference from rbac.revoke_role."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.parish_user_roles SET revoked_at = NOW(), "
                "revoked_by_user_id = %s, note = COALESCE(%s, note) "
                "WHERE user_id = %s AND parish_id = %s AND role_key = %s AND revoked_at IS NULL",
                (revoked_by_user_id, note, user_id, parish_id, role_key),
            )


# ── Self-service access requests ────────────────────────────────────────────

def create_parish_access_request(user_id: int, parish_id: int, requested_role_key: str,
                                  note: str | None = None) -> int:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.parish_access_requests "
                "(user_id, parish_id, requested_role_key, note) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (user_id, parish_id, requested_role_key, note),
            )
            return cur.fetchone()["id"]


def get_pending_parish_access_request(user_id: int) -> dict | None:
    return db.query_one(
        """
        SELECT par.id, par.parish_id, p.name AS parish_name,
               par.requested_role_key, pr.label AS role_label, par.note, par.requested_at
          FROM portal.parish_access_requests par
          JOIN portal.parishes p ON p.id = par.parish_id
          JOIN portal.parish_roles pr ON pr.key = par.requested_role_key
         WHERE par.user_id = %s AND par.status = 'Pending'
         ORDER BY par.requested_at DESC
         LIMIT 1
        """,
        (user_id,),
    )


def list_pending_parish_access_requests() -> list[dict]:
    """Reviewed by beacon_admin (diocese-wide) -- see module docstring."""
    return db.query(
        """
        SELECT par.id, par.user_id, u.email, u.display_name,
               par.parish_id, p.name AS parish_name, p.org_id, o.code AS org_code,
               par.requested_role_key, pr.label AS role_label, pr.description AS role_description,
               par.note, par.requested_at
          FROM portal.parish_access_requests par
          JOIN checkreq.app_users u ON u.id = par.user_id
          JOIN portal.parishes p ON p.id = par.parish_id
          JOIN checkreq.organizations o ON o.id = p.org_id
          JOIN portal.parish_roles pr ON pr.key = par.requested_role_key
         WHERE par.status = 'Pending'
         ORDER BY par.requested_at
        """
    )


def approve_parish_access_request(request_id: int, reviewer_user_id: int, review_note: str | None = None) -> None:
    req = db.query_one(
        "SELECT * FROM portal.parish_access_requests WHERE id = %s AND status = 'Pending'",
        (request_id,),
    )
    if not req:
        raise ValueError("That request is no longer pending.")
    grant_parish_role(req["user_id"], req["parish_id"], req["requested_role_key"],
                      granted_by_user_id=reviewer_user_id,
                      note=f"Approved parish access request #{request_id}")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.parish_access_requests SET status = 'Approved', "
                "reviewed_by_user_id = %s, reviewed_at = NOW(), review_note = %s "
                "WHERE id = %s",
                (reviewer_user_id, review_note, request_id),
            )


def reject_parish_access_request(request_id: int, reviewer_user_id: int, review_note: str | None = None) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.parish_access_requests SET status = 'Rejected', "
                "reviewed_by_user_id = %s, reviewed_at = NOW(), review_note = %s "
                "WHERE id = %s AND status = 'Pending'",
                (reviewer_user_id, review_note, request_id),
            )
