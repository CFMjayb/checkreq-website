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

Originally NO "last admin" guard here, unlike rbac.revoke_role's beacon_admin
protection — a parish losing its only parish_admin isn't the same class of
outage as losing the diocese's only beacon_admin: diocesan staff (still
holding beacon_admin) can always intervene at any parish. **Revisited
2026-09-13**: once the new "User Access" screen (parish_access.py) made
revoking a parish_admin grant one click away for parish admins and Diocesan
Employees (parish_mode_user) alike, not just beacon_admin, Jay asked for the
same class of guard anyway -- see LastParishAdminError/revoke_parish_role
below. Scoped PER PARISH (not system-wide) since the original reasoning
still holds; this only prevents an accidental self-inflicted gap, not a
structural one a beacon_admin couldn't fix.

Depends only on db.py, same one-way-dependency discipline as rbac.py.
"""
from __future__ import annotations

import db
import rbac

# Baseline "you belong to this parish" role -- the parish-side mirror of
# rbac.ENTITY_BASE_ROLE, 2026-09-15. admin_users.users_add grants this
# immediately for a new Parish login (at the parish picked on the Add User
# form), the same "baseline now, more later" shape as the entity side.
PARISH_BASE_ROLE = "parish_member"


def get_parish_org_id(parish_id: int) -> int | None:
    """The diocese (checkreq.organizations id) that owns this parish --
    portal.parishes.org_id. None if the parish doesn't exist."""
    row = db.query_one("SELECT org_id FROM portal.parishes WHERE id = %s", (parish_id,))
    return row["org_id"] if row else None


def holds_role_at_parish_org(user_id: int, role_key: str, parish_org_id: int | None) -> bool:
    """Does this user hold role_key AT the diocese that owns a parish?

    2026-10-05 (cross-diocese fix): every "can this person manage/review/edit
    that parish" check used to ask rbac.user_has_role(..., org_id=None) --
    "holds it at ANY entity" -- so a Beacon Admin or Parish-Mode user of one
    diocese could act on another diocese's parishes. The parish's own
    portal.parishes.org_id is the entity the role has to be held at. A None
    org_id returns False here on purpose: rbac.user_has_role reads None as
    "any entity", and a parish row with no diocese must never fall through
    to that meaning."""
    if parish_org_id is None:
        return False
    return rbac.user_has_role(user_id, role_key, org_id=parish_org_id)


def is_parish_manager(user_id: int, parish_id: int) -> bool:
    """Beacon Admin OR parish_mode_user (a Diocesan Employee granted Parish
    Mode) held AT THIS PARISH'S OWN DIOCESE, OR THIS SPECIFIC parish's own
    Parish Admin -- the "User Access" screen's authorization rule
    (parish_access.py, 2026-09-13, Jay's explicit widen-beyond-just-
    beacon_admin/parish_admin decision). 2026-10-05: the two diocesan roles
    are now checked at the parish's own org, not "any entity" (see
    holds_role_at_parish_org). Lives here (not in parish_access.py
    or parish_mode.py) so BOTH of those modules can call it with no circular
    import -- parish_mode.py already imports this module, and
    parish_access.py already imports parish_mode.py, so parish_mode.py
    importing parish_access.py back would be circular."""
    org_id = get_parish_org_id(parish_id)
    if holds_role_at_parish_org(user_id, "beacon_admin", org_id):
        return True
    if holds_role_at_parish_org(user_id, "parish_mode_user", org_id):
        return True
    return user_has_parish_role(user_id, "parish_admin", parish_id)


def is_parish_reviewer(user_id: int, parish_id: int) -> bool:
    """May this person approve/reject a self-service request for THIS parish
    (parish_access.py, parish_requests.py)? Beacon Admin at the parish's own
    diocese, or this specific parish's own Parish Admin. parish_mode_user is
    a manager (is_parish_manager) but not a reviewer -- unchanged from
    before the 2026-10-05 org scoping."""
    if holds_role_at_parish_org(user_id, "beacon_admin", get_parish_org_id(parish_id)):
        return True
    return user_has_parish_role(user_id, "parish_admin", parish_id)


def get_reviewable_parish_ids(user_id: int) -> list[int]:
    """Every parish whose requests this person may review: every parish of a
    diocese where they hold Beacon Admin, plus every parish they are Parish
    Admin of. Always a real list -- [] means "nothing", never "everything"
    (list_pending_parish_access_requests reads None as unscoped, so no caller
    may pass None for a reviewer)."""
    org_ids = rbac.get_granted_org_ids(user_id, "beacon_admin")
    rows = db.query(
        """
        SELECT p.id AS parish_id FROM portal.parishes p WHERE p.org_id = ANY(%s::int[])
        UNION
        SELECT pur.parish_id
          FROM portal.parish_user_roles pur
          JOIN portal.parish_roles pr ON pr.key = pur.role_key AND pr.is_active
         WHERE pur.user_id = %s AND pur.role_key = 'parish_admin' AND pur.revoked_at IS NULL
        """,
        (org_ids, user_id),
    )
    return [r["parish_id"] for r in rows]


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
       mirrors rbac.get_roles_for_user's shape.

       linked_org_id (2026-08-16, admin_users_detail.html's "Cornerstone
       Entity Roles" gating) is non-NULL exactly when this specific parish
       is Cornerstone-served -- Jay: "If a user is at All Saints in
       Frederick, that is NOT a Cornerstone served parish and the user
       would only have parish roles showing." Selected here so the
       template can check it without a second query."""
    return db.query(
        """
        SELECT pur.id AS parish_user_role_id, pur.parish_id, p.name AS parish_name,
               p.org_id, p.linked_org_id, o.code AS org_code,
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


def get_users_at_parish(parish_id: int) -> list[dict]:
    """Every LIVE (user, role) grant at this ONE parish -- the "User Access"
    screen's roster (parish_access.py, 2026-09-13): "add the ability to see
    all users registered for this parish." One row per grant (a person
    holding 2 roles there appears twice, same convention as every other
    role listing in this codebase) -- ordered by role sort_order then name,
    matching get_parish_roles_for_user's own ordering convention."""
    return db.query(
        """
        SELECT pur.id AS parish_user_role_id, pur.user_id, u.email, u.display_name,
               pur.role_key, pr.label AS role_label, pr.sort_order, pur.granted_at,
               g.email AS granted_by_email, pur.note
          FROM portal.parish_user_roles pur
          JOIN checkreq.app_users u ON u.id = pur.user_id
          JOIN portal.parish_roles pr ON pr.key = pur.role_key
          LEFT JOIN checkreq.app_users g ON g.id = pur.granted_by_user_id
         WHERE pur.parish_id = %s AND pur.revoked_at IS NULL
         ORDER BY pr.sort_order, u.display_name, u.email
        """,
        (parish_id,),
    )


def group_parish_roster(rows: list[dict], format_dt=None) -> list[dict]:
    """Collapse get_users_at_parish()'s one-row-per-grant list into ONE entry
    per person (Jay, 2026-10-05: "a user should only be listed once and all
    the roles for that user should be able to be viewed").

    The baseline PARISH_BASE_ROLE grant is NOT listed as a role: it is the
    "you can sign in to this parish" role everyone gets, not a permission
    anyone should read or revoke one-by-one (Jay: people were revoking it
    without realizing it was the default). It is reported separately as
    ``has_base_role``; a person who holds ONLY the baseline simply has an
    empty ``roles`` list. Each listed role keeps the fields the Revoke form
    needs. ``format_dt`` (optional) formats granted_at for display.

    Pure function -- no database access -- so it is unit-testable on its own.
    Entries come back sorted by display name then email."""
    people: dict[int, dict] = {}
    for r in rows:
        person = people.get(r["user_id"])
        if person is None:
            person = {
                "user_id": r["user_id"],
                "email": r["email"],
                "display_name": r["display_name"],
                "roles": [],
                "has_base_role": False,
            }
            people[r["user_id"]] = person
        if r["role_key"] == PARISH_BASE_ROLE:
            person["has_base_role"] = True
            continue
        person["roles"].append({
            "role_key": r["role_key"],
            "role_label": r["role_label"],
            "granted_at": r["granted_at"],
            "granted_at_display": format_dt(r["granted_at"]) if format_dt else r["granted_at"],
            "granted_by_email": r.get("granted_by_email"),
            "sort_order": r.get("sort_order", 0),
        })
    out = list(people.values())
    for p in out:
        p["roles"].sort(key=lambda x: (x["sort_order"], x["role_label"]))
        p["role_summary"] = ", ".join(x["role_label"] for x in p["roles"])
    out.sort(key=lambda p: ((p["display_name"] or p["email"] or "").lower(), (p["email"] or "").lower()))
    return out


def get_or_create_user_for_grant(email: str, display_name: str | None = None) -> tuple[int, bool]:
    """Resolve an email to a checkreq.app_users id for the "User Access"
    screen's direct-grant form (2026-09-13, Jay: "create the account +
    grant together... silently, so they can attempt a log in with their
    email address"). Mirrors admin_users.py's users_add() exactly: an
    existing row (any email, any active state) is returned completely
    untouched -- no field updated, no reactivation attempted here -- only a
    genuinely new email gets a fresh, roleless, no-password row (the normal
    first-sign-in-via-emailed-code path picks it up from there, same as
    every other silently-provisioned account in this app). Returns
    (user_id, was_created)."""
    email = email.strip().lower()
    existing = db.query_one("SELECT id FROM checkreq.app_users WHERE LOWER(email) = %s", (email,))
    if existing:
        return existing["id"], False
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkreq.app_users (email, display_name, is_active, login_type) "
                "VALUES (%s, %s, TRUE, 'parish') RETURNING id",
                (email, display_name or email.split("@")[0]),
            )
            return cur.fetchone()["id"], True


def get_parish_ids_with_role(user_id: int, role_key: str) -> list[int]:
    """Every parish_id this user holds role_key for, live -- the scoping
    query behind a Parish Admin's own (not diocese-wide) access-request
    queue (parish_access.py's _require_parish_reviewer). Not the same as
    get_parish_role_keys(), which returns role KEYS for one parish (or all)
    -- this returns PARISH IDS for one role, across every parish."""
    rows = db.query(
        """
        SELECT DISTINCT pur.parish_id
          FROM portal.parish_user_roles pur
          JOIN portal.parish_roles pr ON pr.key = pur.role_key AND pr.is_active
         WHERE pur.user_id = %s AND pur.role_key = %s AND pur.revoked_at IS NULL
        """,
        (user_id, role_key),
    )
    return [r["parish_id"] for r in rows]


def all_parish_roles(include_inactive: bool = False) -> list[dict]:
    sql = "SELECT key, label, description, sort_order, is_active FROM portal.parish_roles"
    if not include_inactive:
        sql += " WHERE is_active"
    sql += " ORDER BY sort_order"
    return db.query(sql)


# ── Writes ────────────────────────────────────────────────────────────────

def grant_parish_role(user_id: int, parish_id: int, role_key: str,
                      granted_by_user_id: int | None, note: str | None = None) -> None:
    """Idempotent, same discipline as rbac.grant_role.

       2026-09-15: enforces the Entity/Parish login split from the other
       side of rbac.grant_role's identical guard -- raises
       rbac.MixedLoginTypeError if this login is already classified
       'entity'. A not-yet-classified login (login_type NULL) claims
       'parish' as a side effect, mirroring rbac.grant_role's own
       claim-on-first-grant behavior."""
    current_type = rbac.get_login_type(user_id)
    if current_type == "entity":
        raise rbac.MixedLoginTypeError(
            "This login is classified as an Entity login -- a login must be "
            "either an Entity login or a Parish login, not both."
        )
    if current_type is None:
        rbac.claim_login_type(user_id, "parish")

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


class LastParishAdminError(Exception):
    """Raised by revoke_parish_role when a revoke would leave zero live
       parish_admin holders for THAT SPECIFIC parish, or when someone tries
       to revoke their own parish_admin grant there. Added 2026-09-13, per
       Jay's direct call, once the new "User Access" screen (parish_access.py)
       made revoking a click away for a much wider audience than before --
       the module docstring's original "no last-admin guard" reasoning
       ("diocesan staff can always intervene") is still true and is exactly
       why this guard is scoped to ONE parish, not system-wide the way
       rbac.LastAdminError's beacon_admin guard is: a beacon_admin can still
       always grant a fresh parish_admin at any parish with zero live
       holders, this just stops that gap from being created by an ordinary
       revoke click with no warning."""


def revoke_parish_role(user_id: int, parish_id: int, role_key: str,
                       revoked_by_user_id: int, note: str | None = None) -> None:
    """UPDATE ... SET revoked_at = NOW() ... -- never DELETEs. Guards only
       for parish_admin specifically (2026-09-13, see LastParishAdminError
       above) -- every other parish role is unguarded, matching the module
       docstring's original reasoning."""
    if role_key == "parish_admin":
        if user_id == revoked_by_user_id:
            raise LastParishAdminError("You can't remove your own Parish Admin role at this parish.")
        remaining = db.query_one(
            "SELECT COUNT(*) AS n FROM portal.parish_user_roles "
            "WHERE parish_id = %s AND role_key = 'parish_admin' AND revoked_at IS NULL AND user_id != %s",
            (parish_id, user_id),
        )
        if not remaining or remaining["n"] == 0:
            raise LastParishAdminError(
                "This is the last Parish Admin grant for this parish -- revoking it "
                "would leave nobody able to manage its users. Grant Parish Admin to "
                "someone else at this parish first."
            )

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.parish_user_roles SET revoked_at = NOW(), "
                "revoked_by_user_id = %s, note = COALESCE(%s, note) "
                "WHERE user_id = %s AND parish_id = %s AND role_key = %s AND revoked_at IS NULL",
                (revoked_by_user_id, note, user_id, parish_id, role_key),
            )


def remove_user_from_parish(user_id: int, parish_id: int, removed_by_user_id: int,
                            note: str | None = None) -> int:
    """Take a person off this parish entirely: revoke EVERY live role they hold
    here, including the baseline PARISH_BASE_ROLE (Jay, 2026-10-05). This is the
    one place the baseline role is ever revoked from the User Access screen --
    the per-role Revoke buttons refuse it, so "remove this person" is a separate,
    deliberate, confirmed action rather than something a role-by-role revoke can
    do by accident.

    Same last-Parish-Admin protection as revoke_parish_role: removing someone who
    holds Parish Admin here is refused if it is their own grant or would leave
    the parish with no Parish Admin. Revokes (never DELETEs); returns how many
    grants were revoked."""
    holds_admin = db.query_one(
        "SELECT 1 AS ok FROM portal.parish_user_roles "
        "WHERE user_id = %s AND parish_id = %s AND role_key = 'parish_admin' AND revoked_at IS NULL",
        (user_id, parish_id),
    )
    if holds_admin:
        if user_id == removed_by_user_id:
            raise LastParishAdminError("You can't remove yourself from a parish where you are its Parish Admin.")
        remaining = db.query_one(
            "SELECT COUNT(*) AS n FROM portal.parish_user_roles "
            "WHERE parish_id = %s AND role_key = 'parish_admin' AND revoked_at IS NULL AND user_id != %s",
            (parish_id, user_id),
        )
        if not remaining or remaining["n"] == 0:
            raise LastParishAdminError(
                "This person is the last Parish Admin for this parish -- removing them "
                "would leave nobody able to manage its users. Grant Parish Admin to "
                "someone else at this parish first."
            )
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.parish_user_roles SET revoked_at = NOW(), "
                "revoked_by_user_id = %s, note = COALESCE(%s, note) "
                "WHERE user_id = %s AND parish_id = %s AND revoked_at IS NULL",
                (removed_by_user_id, note, user_id, parish_id),
            )
            return cur.rowcount


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
        SELECT par.id, par.parish_id, p.name AS parish_name, p.org_id,
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


def list_pending_parish_access_requests(parish_ids: list[int] | None = None) -> list[dict]:
    """Reviewed by beacon_admin OR, as of 2026-08-08 (Jay: "The Parish Admin
    will have to grant access to someone who requests it"), a Parish Admin.
    parish_ids is always the reviewer's own reach, from
    get_reviewable_parish_ids (the parishes of the dioceses where they hold
    Beacon Admin, plus the parishes they administer). None means no
    restriction at all -- since 2026-10-05 no caller passes it, because even
    a Beacon Admin only reaches their own dioceses' parishes."""
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
           AND (%s::int[] IS NULL OR par.parish_id = ANY(%s::int[]))
         ORDER BY par.requested_at
        """,
        (parish_ids, parish_ids),
    )


def get_parish_access_request(request_id: int) -> dict | None:
    """One request row by id, parish_id included -- the lookup
    parish_access.py's approve/reject routes need BEFORE authorizing a
    non-beacon_admin Parish Admin reviewer (must confirm the request's own
    parish_id matches one they actually administer, not just that they
    administer some parish somewhere)."""
    return db.query_one(
        "SELECT id, user_id, parish_id, requested_role_key, status FROM portal.parish_access_requests WHERE id = %s",
        (request_id,),
    )


def approve_parish_access_request(request_id: int, reviewer_user_id: int, review_note: str | None = None) -> None:
    req = db.query_one(
        "SELECT * FROM portal.parish_access_requests WHERE id = %s AND status = 'Pending'",
        (request_id,),
    )
    if not req:
        raise ValueError("That request is no longer pending.")
    try:
        grant_parish_role(req["user_id"], req["parish_id"], req["requested_role_key"],
                          granted_by_user_id=reviewer_user_id,
                          note=f"Approved parish access request #{request_id}")
    except rbac.MixedLoginTypeError as exc:
        # Translated to this function's own existing ValueError contract
        # (2026-09-15), same reasoning as rbac.approve_access_request's
        # identical translation -- every caller's pre-existing
        # `except ValueError` handling covers this with no route changes.
        raise ValueError(str(exc)) from exc
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
