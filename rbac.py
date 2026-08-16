"""
rbac.py — role lookups for the checkreq schema.

The single read/write path for every permission decision in Beacon, per
`Role-Based Access Control Plan.md` (2026-08-01). A role is always held FOR
AN ENTITY (checkreq.user_roles.org_id is NOT NULL — see the plan's §2.2 for
why a nullable "global" sentinel was deliberately rejected, citing this same
project's own global_approvers.org_id incident). A caller that is
deliberately cross-entity passes org_id=None and gets "in any entity"
semantics — that choice is explicit at the call site, never a property of
the data.

Depends only on db.py (no schema-specific helpers live there — see
admin_setup.py's own module docstring for why business queries don't belong
in db.py). Safe for main.py, admin_setup.py, and approval_engine.py (if ever
needed) to all import with no circular-import risk.

NOTE: every function here queries checkreq.roles/checkreq.user_roles, which
do not exist until migrations/019_rbac.sql has been applied. This module is
written and reviewable now; nothing in Beacon imports or calls it yet.
"""
from __future__ import annotations

import db


def user_has_role(user_id: int, role_key: str, org_id: int | None = None) -> bool:
    """org_id given -> does this user hold this role FOR THAT ENTITY?
       org_id None  -> does this user hold it for ANY entity? (deliberately
                       cross-entity routes only -- /admin/all-requests,
                       /admin/ap-review, /admin/vendor-requests,
                       impersonation, and beacon_admin's own access-request
                       review queue)."""
    row = db.query_one(
        """
        SELECT 1
          FROM checkreq.user_roles ur
          JOIN checkreq.roles r ON r.key = ur.role_key AND r.is_active
         WHERE ur.user_id = %s AND ur.role_key = %s AND ur.revoked_at IS NULL
           AND (%s::int IS NULL OR ur.org_id = %s)
         LIMIT 1
        """,
        (user_id, role_key, org_id, org_id),
    )
    return row is not None


def user_has_any_role(user_id: int, role_keys: list[str] | None = None,
                       org_id: int | None = None) -> bool:
    """role_keys None -> holds ANY active role at all (the "is this person
       provisioned for anything" check -- this is what gates the roleless
       blank screen, Plan §9). Otherwise: holds any one of the given roles."""
    row = db.query_one(
        """
        SELECT 1
          FROM checkreq.user_roles ur
          JOIN checkreq.roles r ON r.key = ur.role_key AND r.is_active
         WHERE ur.user_id = %s AND ur.revoked_at IS NULL
           AND (%s::text[] IS NULL OR ur.role_key = ANY(%s))
           AND (%s::int IS NULL OR ur.org_id = %s)
         LIMIT 1
        """,
        (user_id, role_keys, role_keys, org_id, org_id),
    )
    return row is not None


def get_role_keys(user_id: int, org_id: int | None = None) -> set[str]:
    """Every role key this user holds (in org_id, or across all entities if
       org_id is None). ONE query -- what _render() should inject as `roles`
       so templates can do {% if 'cfo' in roles %} instead of N separate
       user_has_role calls (Plan §3.1)."""
    rows = db.query(
        """
        SELECT DISTINCT ur.role_key
          FROM checkreq.user_roles ur
          JOIN checkreq.roles r ON r.key = ur.role_key AND r.is_active
         WHERE ur.user_id = %s AND ur.revoked_at IS NULL
           AND (%s::int IS NULL OR ur.org_id = %s)
        """,
        (user_id, org_id, org_id),
    )
    return {r["role_key"] for r in rows}


def get_roles_for_user(user_id: int) -> list[dict]:
    """Jay's "give me all roles for x". Every (entity, role) pair this user
       holds, ordered for display."""
    return db.query(
        """
        SELECT ur.id AS user_role_id, ur.org_id, o.code AS org_code, o.name AS org_name,
               ur.role_key, r.label AS role_label, r.description AS role_description,
               ur.granted_at, g.email AS granted_by_email, ur.note
          FROM checkreq.user_roles ur
          JOIN checkreq.organizations o ON o.id = ur.org_id
          JOIN checkreq.roles r ON r.key = ur.role_key
          LEFT JOIN checkreq.app_users g ON g.id = ur.granted_by_user_id
         WHERE ur.user_id = %s AND ur.revoked_at IS NULL
         ORDER BY o.name, r.sort_order
        """,
        (user_id,),
    )


def get_users_with_role(role_key: str, org_id: int | None = None) -> list[dict]:
    """Jay's "who has this permission in Y entity". Replaces every
       `SELECT ... WHERE is_cfo = TRUE` in main.py -- and fixes them, since
       those currently return every entity's CFO regardless of whose request
       triggered them (Plan §1.2 / §5.4, findings #16/#17)."""
    return db.query(
        """
        SELECT DISTINCT u.id, u.email, u.display_name
          FROM checkreq.user_roles ur
          JOIN checkreq.app_users u ON u.id = ur.user_id AND u.is_active
          JOIN checkreq.roles r ON r.key = ur.role_key AND r.is_active
         WHERE ur.role_key = %s AND ur.revoked_at IS NULL
           AND (%s::int IS NULL OR ur.org_id = %s)
         ORDER BY u.display_name, u.email
        """,
        (role_key, org_id, org_id),
    )


def get_granted_org_ids(user_id: int, role_key: str) -> list[int]:
    """Every org_id where this user holds a LIVE grant of role_key --
    2026-08-16, the building block for "scope this list to only the orgs
    I'm actually authorized at" (AP Review / Vendor Requests / their badge
    counts). This is the fix for a real gap: those screens used to show
    every org's rows unconditionally, gated only by "does this user hold
    the role ANYWHERE" -- which was fine while only 4 large, CFM-trusted
    orgs existed, but would leak a parish-org's own AP data to every other
    entity the moment a small, narrowly-granted parish approver exists.
    Scoping the QUERY to this list (rather than the single current-session
    entity) preserves today's real workflow for someone granted the role at
    several entities (they still see everything they're authorized for, in
    one screen, no entity-switching) while a narrowly-granted holder only
    ever sees their own org(s). Returns [] if the user holds no live grant
    of this role anywhere -- callers should treat that as "show nothing",
    not "show everything" (never pass an empty list through as if it meant
    unscoped)."""
    rows = db.query(
        "SELECT org_id FROM checkreq.user_roles "
        "WHERE user_id = %s AND role_key = %s AND revoked_at IS NULL",
        (user_id, role_key),
    )
    return [r["org_id"] for r in rows]


def all_roles(include_inactive: bool = False) -> list[dict]:
    """The role picker's option list."""
    sql = "SELECT key, label, description, sort_order, is_active FROM checkreq.roles"
    if not include_inactive:
        sql += " WHERE is_active"
    sql += " ORDER BY sort_order"
    return db.query(sql)


# ── Writes. Only the Users & Roles screen and access-request approval call these. ──

def grant_role(user_id: int, org_id: int, role_key: str,
               granted_by_user_id: int | None, note: str | None = None) -> None:
    """Idempotent: a live grant already present is a no-op (the partial
       unique index makes a true duplicate impossible anyway, but check
       first so this never raises on a double-click). Re-granting a
       previously-revoked role INSERTs a new row -- the revoked one stays as
       history, never deleted or reactivated in place."""
    existing = db.query_one(
        "SELECT id FROM checkreq.user_roles "
        "WHERE user_id = %s AND org_id = %s AND role_key = %s AND revoked_at IS NULL",
        (user_id, org_id, role_key),
    )
    if existing:
        return
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkreq.user_roles "
                "(user_id, org_id, role_key, granted_by_user_id, note) "
                "VALUES (%s, %s, %s, %s, %s)",
                (user_id, org_id, role_key, granted_by_user_id, note),
            )


def grant_role_all_entities(user_id: int, role_key: str,
                             granted_by_user_id: int | None, note: str | None = None) -> int:
    """The Users & Roles screen's "All entities" checkbox (Plan §6.2) --
       writes one row per active organization, exactly how a genuinely
       cross-entity role (cfo, setup_admin, beacon_admin) gets granted
       system-wide without a NULL org_id sentinel (Plan §2.2). Returns the
       count of orgs granted (idempotent per-org via grant_role)."""
    orgs = db.query("SELECT id FROM checkreq.organizations WHERE is_active")
    for o in orgs:
        grant_role(user_id, o["id"], role_key, granted_by_user_id, note)
    return len(orgs)


class LastAdminError(Exception):
    """Raised by revoke_role when a revoke would leave zero live
       beacon_admin holders system-wide, or when an admin tries to revoke
       their own beacon_admin grant (Plan §6.3). Deliberately not a bare
       ValueError -- callers should catch this specifically and show a
       clear message, not a generic error."""


def revoke_role(user_id: int, org_id: int, role_key: str,
                revoked_by_user_id: int, note: str | None = None) -> None:
    """UPDATE ... SET revoked_at = NOW() ... -- never DELETEs, matching this
       schema's settled soft-revoke philosophy (Plan §2.1).

       Both guards live here, not in the route, so no future caller can
       bypass them (Plan §6.3):
         1. Last-admin guard -- refuse any revoke that would leave zero live
            beacon_admin grants system-wide.
         2. No self-revoke of beacon_admin -- an admin may drop any other
            role from themselves, but not the one that lets them undo it."""
    if role_key == "beacon_admin":
        if user_id == revoked_by_user_id:
            raise LastAdminError("You can't remove your own Beacon Admin role.")
        remaining = db.query_one(
            "SELECT COUNT(*) AS n FROM checkreq.user_roles "
            "WHERE role_key = 'beacon_admin' AND revoked_at IS NULL AND user_id != %s",
            (user_id,),
        )
        if not remaining or remaining["n"] == 0:
            raise LastAdminError(
                "This is the last Beacon Admin grant system-wide -- revoking it "
                "would leave nobody able to manage roles. Grant Beacon Admin to "
                "someone else first."
            )

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.user_roles SET revoked_at = NOW(), "
                "revoked_by_user_id = %s, note = COALESCE(%s, note) "
                "WHERE user_id = %s AND org_id = %s AND role_key = %s AND revoked_at IS NULL",
                (revoked_by_user_id, note, user_id, org_id, role_key),
            )


# ── Self-service access requests (Plan §9) ──────────────────────────────────

def create_access_request(user_id: int, org_id: int, requested_role_key: str,
                           note: str | None = None) -> int:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkreq.access_requests "
                "(user_id, org_id, requested_role_key, note) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (user_id, org_id, requested_role_key, note),
            )
            return cur.fetchone()["id"]


def get_pending_access_request(user_id: int) -> dict | None:
    """The requester's own most recent pending request, if any -- so
       reloading the blank screen shows "pending" status instead of the form
       again (Plan §9, point 4)."""
    return db.query_one(
        """
        SELECT ar.id, ar.org_id, o.code AS org_code, o.name AS org_name,
               ar.requested_role_key, r.label AS role_label, ar.note, ar.requested_at
          FROM checkreq.access_requests ar
          JOIN checkreq.organizations o ON o.id = ar.org_id
          JOIN checkreq.roles r ON r.key = ar.requested_role_key
         WHERE ar.user_id = %s AND ar.status = 'Pending'
         ORDER BY ar.requested_at DESC
         LIMIT 1
        """,
        (user_id,),
    )


def list_pending_access_requests() -> list[dict]:
    """The beacon_admin review queue."""
    return db.query(
        """
        SELECT ar.id, ar.user_id, u.email, u.display_name,
               ar.org_id, o.code AS org_code, o.name AS org_name,
               ar.requested_role_key, r.label AS role_label, r.description AS role_description,
               ar.note, ar.requested_at
          FROM checkreq.access_requests ar
          JOIN checkreq.app_users u ON u.id = ar.user_id
          JOIN checkreq.organizations o ON o.id = ar.org_id
          JOIN checkreq.roles r ON r.key = ar.requested_role_key
         WHERE ar.status = 'Pending'
         ORDER BY ar.requested_at
        """
    )


def approve_access_request(request_id: int, reviewer_user_id: int, review_note: str | None = None) -> None:
    req = db.query_one(
        "SELECT * FROM checkreq.access_requests WHERE id = %s AND status = 'Pending'",
        (request_id,),
    )
    if not req:
        raise ValueError("That request is no longer pending.")
    grant_role(req["user_id"], req["org_id"], req["requested_role_key"],
               granted_by_user_id=reviewer_user_id,
               note=f"Approved access request #{request_id}")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.access_requests SET status = 'Approved', "
                "reviewed_by_user_id = %s, reviewed_at = NOW(), review_note = %s "
                "WHERE id = %s",
                (reviewer_user_id, review_note, request_id),
            )


def reject_access_request(request_id: int, reviewer_user_id: int, review_note: str | None = None) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.access_requests SET status = 'Rejected', "
                "reviewed_by_user_id = %s, reviewed_at = NOW(), review_note = %s "
                "WHERE id = %s AND status = 'Pending'",
                (reviewer_user_id, review_note, request_id),
            )
