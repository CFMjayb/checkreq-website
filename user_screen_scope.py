"""
user_screen_scope.py -- which people, and which of their roles, the two lists on the User Management screen show
(26-129, 2026-10-30; Jay's feedback on the screen, Parish to Entity Promotion and User Screens Plan.md, Step A).

  Entity-Related Logins -- only people holding a role at the entity you are working in, and only their roles and
  Program Areas AT that entity (it used to list every role they hold at every entity).
  Parish-Related Logins -- only the parish logins of that entity's own church(es), and only their roles at those
  churches (it used to list every parish login in Beacon).

An entity's churches are the parishes it owns (portal.parishes.org_id: a diocese and its parishes) or is linked to
(portal.parishes.linked_org_id: a Cornerstone-served church's own entity, e.g. SJE for St Johns Episcopal Church).

The row functions are pure (rows in, new rows out, the inputs are never changed), so they are tested without a
database. Only parishes_for_entity / add_user_parishes touch it, and only to read.
"""
from __future__ import annotations

import db


def parishes_for_entity(org_id: int) -> list[dict]:
    """Every parish (church or unit, active or not) this entity owns or is linked to. All of them, on purpose: a
       login's roles at an inactive parish or a non-church unit must not make the person vanish from the screen."""
    return db.query(
        "SELECT id, name FROM portal.parishes WHERE org_id = %s OR linked_org_id = %s ORDER BY name",
        (org_id, org_id),
    )


def add_user_parishes(org_id: int) -> list[dict]:
    """The Add User form's Parish picker: the active churches this entity owns or is linked to. A served church
       (SJE) owns none itself, so the diocese-only list the form used before came up empty there."""
    return db.query(
        "SELECT id, name FROM portal.parishes WHERE (org_id = %s OR linked_org_id = %s) "
        "AND is_active AND coalesce(is_congregation, true) ORDER BY name",
        (org_id, org_id),
    )


def entity_label(org: dict | None) -> str:
    """How the entity is named in the section titles: its code in capitals (SJE), else its name."""
    if not org:
        return ""
    code = org.get("code")
    return code.upper() if code else (org.get("name") or "")


def parish_label(parishes: list[dict], org: dict | None) -> str:
    """The parish list's title suffix: the church's own name when the entity has exactly one (St Johns Episcopal
       Church), otherwise the entity (a diocese has many churches)."""
    if len(parishes) == 1:
        return parishes[0]["name"]
    return entity_label(org)


def scope_entity_rows(rows: list[dict], org_id: int) -> list[dict]:
    """People holding a live role at org_id, each shown with only their roles and Program Areas at org_id."""
    out = []
    for r in rows:
        roles = [x for x in r.get("roles", []) if x.get("org_id") == org_id]
        if not roles:
            continue
        areas = [a for a in r.get("program_areas", []) if a.get("org_id") == org_id]
        row = dict(r)
        row["roles"] = roles
        row["has_any_role"] = True
        row["program_areas"] = areas
        row["pa_count"] = len(areas)
        out.append(row)
    return out


def scope_parish_rows(rows: list[dict], parish_ids) -> list[dict]:
    """People holding a live parish role at one of parish_ids, each shown with only the roles held there."""
    ids = set(parish_ids or [])
    if not ids:
        return []
    out = []
    for r in rows:
        roles = [x for x in r.get("parish_roles", []) if x.get("parish_id") in ids]
        if not roles:
            continue
        row = dict(r)
        row["parish_roles"] = roles
        row["has_any_parish_role"] = True
        out.append(row)
    return out
