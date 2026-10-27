"""
assignees.py -- who a checklist step can be assigned to (26-162, 2026-10-27; Jay: "For Claggett and EDOM, it can be anyone that has a
login from either domain @episcopalmaryland.org edom.org claggettcenter.org - the other entities are strictly their own entity logins").

  * EDOM and Claggett share one pool of diocesan staff: any active Beacon login whose e-mail is at one of SHARED_POOL_DOMAINS. Such a
    person may have no role at all in the entity (a supervisor, an IT person): they see their steps on the My steps page and nothing else.
    People who hold an HR role (HR Admin, Benefits Admin, Beacon Admin) at the entity are offered too, wherever their e-mail is, since they
    run the onboarding (Jay's own login is at cfmins.org).
  * Every other entity: only logins that already belong to THAT entity (a live role there, or a program-area assignment), the same
    people rbac.users_with_org_access() lists.

The list is explicit and short on purpose: a new entity or domain is added here, on Jay's word, never inferred from an e-mail.
The server checks it on every assignment (can_be_assigned): a step assigned to somebody puts the new hire's name on that person's
My steps page, so a posted user id outside the list is refused, unlike the picker lists elsewhere in Beacon, which are UI-only.
"""
from __future__ import annotations

import db
import rbac

SHARED_POOL_DOMAINS = ("episcopalmaryland.org", "edom.org", "claggettcenter.org")
SHARED_POOL_ENTITIES = ("edom", "claggett")          # entity codes, compared in lower case
HR_ROLES = ("hr_admin", "benefits_admin", "beacon_admin")


def _org_code(org_id: int) -> str:
    row = db.query_one("SELECT code FROM checkreq.organizations WHERE id = %s", (org_id,))
    return (row["code"] or "").lower() if row else ""


def uses_shared_pool(org_id: int) -> bool:
    return _org_code(org_id) in SHARED_POOL_ENTITIES


def people_for(org_id: int) -> list[dict]:
    """Every person a step at this entity may be assigned to: [{id, email, name}], by name."""
    if uses_shared_pool(org_id):
        rows = db.query(
            "SELECT DISTINCT u.id, u.email, coalesce(nullif(u.display_name, ''), u.email) AS name "
            "FROM checkreq.app_users u "
            "WHERE u.is_active AND ( "
            "  lower(split_part(u.email, '@', 2)) = ANY(%s) "
            "  OR EXISTS (SELECT 1 FROM checkreq.user_roles ur WHERE ur.user_id = u.id AND ur.org_id = %s "
            "             AND ur.revoked_at IS NULL AND ur.role_key = ANY(%s))) "
            "ORDER BY 3, 2", (list(SHARED_POOL_DOMAINS), org_id, list(HR_ROLES)))
        return [dict(r) for r in rows]
    return [{"id": r["id"], "email": r["email"], "name": r["display_name"] or r["email"]}
            for r in rbac.users_with_org_access(org_id)]


def can_be_assigned(user_id: int | None, org_id: int) -> bool:
    if not user_id:
        return False
    return any(p["id"] == user_id for p in people_for(org_id))
