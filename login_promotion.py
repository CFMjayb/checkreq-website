"""
login_promotion.py -- promote a parish login to an entity login, and take it back (26-129, Parish to Entity Promotion and User
Screens Plan.md, Step C; Jay, 2026-10-30).

A Cornerstone-served church is an entity of its own (portal.parishes.linked_org_id), but its people were first set up as PARISH
logins (checkreq.app_users.login_type = 'parish', roles in portal.parish_user_roles). A Beacon Admin presses "Promote to entity
login" on such a person's record. The person then holds all their roles at the entity (checkreq.user_roles), signs in to the entity
screens only, and holds no parish role: the either/or login rule (rbac.MixedLoginTypeError) stays as it is.

PROMOTE, in ONE transaction: login_type -> 'entity'. Entity Member is granted at the church's entity (it replaces Parish Member), plus
the entity-level version of every carry-over parish role the person holds (portal.parish_roles.entity_role_key) and any extra entity
role the admin ticked. Every live parish role at that church is retired (revoked, never deleted). A row in portal.login_promotions
records who did it, what was granted and what was retired.

RETURN TO PARISH ONLY (the undo), in ONE transaction: the entity roles granted by the promotion are revoked, login_type -> 'parish',
the retired parish roles are granted again (new rows, the old ones stay as history) and the promotion is marked reversed.

Nothing is silently lost: a parish role with no entity version is listed as "will not carry over" before anything happens, and a
role whose entity version is not switched on yet BLOCKS the promotion (Timekeeping and Parish Admin until step D of the plan). A person
who also holds parish roles at ANOTHER church blocks it too. The undo is blocked while the person holds any entity role the promotion did
not grant, any Program Area assignment or any approver role, so nothing is stranded.

build_plan / build_return_plan are pure (no database) and are what the confirmation screens and both actions run on.
"""
from __future__ import annotations

import json

import db
import parish_roles
import rbac

ENTITY_BASE_ROLE = rbac.ENTITY_BASE_ROLE          # 'entity_member'
PARISH_BASE_ROLE = parish_roles.PARISH_BASE_ROLE  # 'parish_member'


class PromotionBlocked(Exception):
    """The promotion (or the undo) cannot go ahead. .messages are plain sentences for the screen."""

    def __init__(self, messages):
        self.messages = [m for m in messages if m]
        super().__init__(" ".join(self.messages))


def _names(items: list[str]) -> str:
    return ", ".join(items)


# ---- who may do it ------------------------------------------------------------------------------------------------------------
def actor_may_manage(actor_id: int, org_ids) -> bool:
    """Beacon Admin at any of the given entities (D3: at the church's diocese or at its own linked entity)."""
    return any(rbac.user_has_role(actor_id, "beacon_admin", org_id=o) for o in {o for o in org_ids if o})


def actor_may_promote(actor_id: int, parish: dict) -> bool:
    return actor_may_manage(actor_id, [parish.get("org_id"), parish.get("linked_org_id")])


# ---- the plan for a promotion (pure) ------------------------------------------------------------------------------------------
def build_plan(*, login_type, parish, held_roles, parish_role_meta, entity_role_meta, other_parishes, open_promotion) -> dict:
    """What a promotion would do, and what stops it.

    held_roles        role keys the person holds live at this parish
    parish_role_meta  {key: {"label", "entity_role_key"}} for portal.parish_roles
    entity_role_meta  {key: {"label", "is_active", "parish_login"}} for checkreq.roles, in display order
    other_parishes    names of OTHER parishes where the person holds a live parish role
    open_promotion    an open portal.login_promotions row for this person and entity, or None
    """
    blockers: list[str] = []
    carry: list[dict] = []
    dropped: list[dict] = []
    replaced: list[dict] = []
    unavailable: list[dict] = []

    if login_type != "parish":
        blockers.append("Only a parish login can be promoted. This one is "
                        + ("already an entity login." if login_type == "entity" else "not classified as a parish login."))
    if not parish:
        blockers.append("That parish was not found.")
        return {"blockers": blockers, "carry": carry, "dropped": dropped, "replaced": replaced, "extras": [], "entity_grants": [],
                "held": []}
    if parish.get("linked_org_id") is None:
        blockers.append(f"{parish['name']} is not a Cornerstone-served church, so there is no entity of its own to promote into.")
    if not held_roles:
        blockers.append(f"This person holds no role at {parish['name']}.")
    if other_parishes:
        blockers.append("This person also holds parish roles at " + _names(sorted(other_parishes)) + ". An entity login cannot keep "
                        "parish roles, so take them off those churches first.")
    if open_promotion:
        blockers.append("This person has already been promoted into this entity. Use Return to parish only to undo that first.")

    for key in held_roles:
        meta = parish_role_meta.get(key) or {"label": key, "entity_role_key": None}
        if key == PARISH_BASE_ROLE:
            replaced.append({"role_key": key, "label": meta["label"]})
            continue
        ek = meta.get("entity_role_key")
        if not ek:
            dropped.append({"role_key": key, "label": meta["label"]})
            continue
        em = entity_role_meta.get(ek)
        if not em or not em.get("is_active"):
            unavailable.append({"role_key": key, "label": meta["label"]})
            continue
        carry.append({"parish_role_key": key, "parish_label": meta["label"], "entity_role_key": ek, "entity_label": em["label"]})
    for u in unavailable:
        blockers.append(f"{u['label']} cannot carry over yet: its entity-level role is not switched on. It is built in a later step of "
                        "the plan, so promote this person after that.")

    carried = {c["entity_role_key"] for c in carry}
    extras = [{"key": k, "label": m["label"]} for k, m in entity_role_meta.items()
              if m.get("is_active") and m.get("parish_login") and k not in carried and k != ENTITY_BASE_ROLE]
    return {
        "blockers": blockers, "carry": carry, "dropped": dropped, "replaced": replaced, "extras": extras,
        "entity_grants": [ENTITY_BASE_ROLE] + [c["entity_role_key"] for c in carry],
        "held": list(held_roles),
    }


def load_plan(user_id: int, parish_id: int) -> dict:
    """The plan for promoting this person at this parish, read from the database, with the user, parish and entity added."""
    user = db.query_one("SELECT id, email, display_name, login_type, is_active FROM checkreq.app_users WHERE id = %s", (user_id,))
    parish = db.query_one(
        "SELECT p.id, p.name, p.org_id, p.linked_org_id, o.code AS entity_code, o.name AS entity_name "
        "FROM portal.parishes p LEFT JOIN checkreq.organizations o ON o.id = p.linked_org_id WHERE p.id = %s", (parish_id,))
    held = [r["role_key"] for r in db.query(
        "SELECT role_key FROM portal.parish_user_roles WHERE user_id = %s AND parish_id = %s AND revoked_at IS NULL "
        "ORDER BY role_key", (user_id, parish_id))] if user else []
    others = [r["name"] for r in db.query(
        "SELECT DISTINCT p.name FROM portal.parish_user_roles pur JOIN portal.parishes p ON p.id = pur.parish_id "
        "WHERE pur.user_id = %s AND pur.parish_id <> %s AND pur.revoked_at IS NULL", (user_id, parish_id))] if user else []
    pmeta = {r["key"]: {"label": r["label"], "entity_role_key": r["entity_role_key"]}
             for r in db.query("SELECT key, label, entity_role_key FROM portal.parish_roles")}
    emeta = {r["key"]: {"label": r["label"], "is_active": r["is_active"], "parish_login": r["parish_login"]}
             for r in db.query("SELECT key, label, is_active, parish_login FROM checkreq.roles ORDER BY sort_order, key")}
    open_promo = None
    if user and parish and parish.get("linked_org_id") is not None:
        open_promo = db.query_one(
            "SELECT id FROM portal.login_promotions WHERE user_id = %s AND org_id = %s AND reversed_at IS NULL",
            (user_id, parish["linked_org_id"]))
    plan = build_plan(login_type=user["login_type"] if user else None, parish=parish, held_roles=held, parish_role_meta=pmeta,
                      entity_role_meta=emeta, other_parishes=others, open_promotion=open_promo)
    if not user:
        plan["blockers"].insert(0, "That person was not found.")
    plan["user"], plan["parish"] = user, parish
    return plan


def promote_options(user_id: int, actor_id: int) -> list[dict]:
    """The parishes this person could be promoted out of (their church is Cornerstone-served) that the actor may promote at.
       Empty unless the person is a parish login."""
    row = db.query_one("SELECT login_type FROM checkreq.app_users WHERE id = %s", (user_id,))
    if not row or row["login_type"] != "parish":
        return []
    rows = db.query(
        "SELECT DISTINCT p.id AS parish_id, p.name AS parish_name, p.org_id, p.linked_org_id, o.code AS entity_code, o.name AS entity_name "
        "FROM portal.parish_user_roles pur JOIN portal.parishes p ON p.id = pur.parish_id "
        "JOIN checkreq.organizations o ON o.id = p.linked_org_id "
        "WHERE pur.user_id = %s AND pur.revoked_at IS NULL AND p.linked_org_id IS NOT NULL ORDER BY p.name", (user_id,))
    return [r for r in rows if actor_may_promote(actor_id, r)]


def open_promotions(user_id: int, actor_id: int) -> list[dict]:
    """Promotions of this person not yet undone, that the actor may undo."""
    rows = db.query(
        "SELECT lp.id, lp.promoted_at, lp.org_id, p.org_id AS parish_org_id, p.name AS parish_name, o.code AS entity_code, "
        "g.email AS promoted_by_email "
        "FROM portal.login_promotions lp JOIN portal.parishes p ON p.id = lp.parish_id "
        "JOIN checkreq.organizations o ON o.id = lp.org_id LEFT JOIN checkreq.app_users g ON g.id = lp.promoted_by_user_id "
        "WHERE lp.user_id = %s AND lp.reversed_at IS NULL ORDER BY lp.promoted_at DESC", (user_id,))
    return [r for r in rows if actor_may_manage(actor_id, [r["org_id"], r["parish_org_id"]])]


# ---- promote ------------------------------------------------------------------------------------------------------------------
def promote(user_id: int, parish_id: int, actor_id: int, extra_keys=()) -> int:
    """Promote, in one transaction. Returns the new portal.login_promotions id. Raises PromotionBlocked and changes nothing when the plan
       has a blocker (it is rebuilt from the database here, never trusted from the screen)."""
    plan = load_plan(user_id, parish_id)
    if plan["blockers"]:
        raise PromotionBlocked(plan["blockers"])
    allowed = {e["key"] for e in plan["extras"]}
    extra_keys = [k for k in dict.fromkeys(extra_keys) if k]
    bad = [k for k in extra_keys if k not in allowed]
    if bad:
        raise PromotionBlocked([f"{_names(bad)} cannot be granted as an extra role here."])

    parish = plan["parish"]
    org_id = parish["linked_org_id"]
    grants = plan["entity_grants"] + extra_keys
    labels = {r["key"]: r["label"] for r in db.query("SELECT key, label FROM checkreq.roles")}
    granted_rows = [{"role_key": k, "label": labels.get(k, k)} for k in grants]
    pmeta = {r["key"]: r["label"] for r in db.query("SELECT key, label FROM portal.parish_roles")}
    retired_rows = [{"role_key": k, "label": pmeta.get(k, k)} for k in plan["held"]]
    note = f"Promoted from the parish login at {parish['name']}"

    with db.shared_transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE checkreq.app_users SET login_type = 'entity' WHERE id = %s AND login_type = 'parish'", (user_id,))
            if cur.rowcount != 1:
                raise PromotionBlocked(["This login changed while you were looking at it. Reload the page and try again."])
            for role_key in grants:
                cur.execute("SELECT 1 FROM checkreq.user_roles WHERE user_id = %s AND org_id = %s AND role_key = %s "
                            "AND revoked_at IS NULL", (user_id, org_id, role_key))
                if not cur.fetchone():
                    cur.execute("INSERT INTO checkreq.user_roles (user_id, org_id, role_key, granted_by_user_id, note) "
                                "VALUES (%s, %s, %s, %s, %s)", (user_id, org_id, role_key, actor_id, note))
            cur.execute("UPDATE portal.parish_user_roles SET revoked_at = NOW(), revoked_by_user_id = %s, note = COALESCE(%s, note) "
                        "WHERE user_id = %s AND parish_id = %s AND revoked_at IS NULL", (actor_id, note, user_id, parish_id))
            cur.execute("INSERT INTO portal.login_promotions (user_id, parish_id, org_id, promoted_by_user_id, "
                        "entity_roles_granted, parish_roles_retired) VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb) RETURNING id",
                        (user_id, parish_id, org_id, actor_id, json.dumps(granted_rows), json.dumps(retired_rows)))
            return cur.fetchone()["id"]


# ---- return to parish only (the undo) -----------------------------------------------------------------------------------------
def build_return_plan(*, promotion, login_type, live_entity_roles, program_areas, approver_rules) -> dict:
    """What undoing a promotion would do, and what stops it.

    promotion         the portal.login_promotions row (org_id, entity_roles_granted, parish_roles_retired, reversed_at)
    live_entity_roles every live entity role the person holds, as {"role_key", "org_id", "label", "org_code"}
    program_areas     the person's Program Area assignments, as {"title", "org_code"}
    approver_rules    how many live approval rules name the person as approver or backup
    """
    blockers: list[str] = []
    if not promotion:
        return {"blockers": ["That promotion was not found."], "revoke": [], "restore": [], "other_roles": []}
    if promotion.get("reversed_at"):
        blockers.append("That promotion was already undone.")
    if login_type != "entity":
        blockers.append("This person is not an entity login, so there is nothing to return.")
    granted = {g["role_key"] for g in (promotion.get("entity_roles_granted") or [])}
    own, other = [], []
    for r in live_entity_roles:
        (own if r["org_id"] == promotion["org_id"] and r["role_key"] in granted else other).append(r)
    if other:
        blockers.append("This person now holds other entity roles: " + _names(sorted(f"{r['label']} ({r['org_code']})" for r in other))
                        + ". Revoke them first, or keep this person as an entity login.")
    if program_areas:
        blockers.append("This person has Program Area assignments: " + _names(sorted(f"{a['title']} ({a['org_code']})" for a in program_areas))
                        + ". Remove them first.")
    if approver_rules:
        blockers.append(f"This person is a named approver or backup on {approver_rules} approval rule(s). Point those rules at someone "
                        "else first, so no request is left without an approver.")
    return {
        "blockers": blockers,
        "revoke": [{"role_key": r["role_key"], "label": r["label"]} for r in own],
        "restore": list(promotion.get("parish_roles_retired") or []),
        "other_roles": other,
    }


def load_return_plan(promotion_id: int) -> dict:
    promo = db.query_one(
        "SELECT lp.*, p.name AS parish_name, p.org_id AS parish_org_id, o.code AS entity_code, o.name AS entity_name "
        "FROM portal.login_promotions lp JOIN portal.parishes p ON p.id = lp.parish_id "
        "JOIN checkreq.organizations o ON o.id = lp.org_id WHERE lp.id = %s", (promotion_id,))
    if not promo:
        plan = build_return_plan(promotion=None, login_type=None, live_entity_roles=[], program_areas=[], approver_rules=0)
        plan["promotion"], plan["user"] = None, None
        return plan
    uid = promo["user_id"]
    user = db.query_one("SELECT id, email, display_name, login_type FROM checkreq.app_users WHERE id = %s", (uid,))
    live = db.query(
        "SELECT ur.role_key, ur.org_id, r.label, o.code AS org_code FROM checkreq.user_roles ur "
        "JOIN checkreq.roles r ON r.key = ur.role_key JOIN checkreq.organizations o ON o.id = ur.org_id "
        "WHERE ur.user_id = %s AND ur.revoked_at IS NULL", (uid,))
    areas = db.query(
        "SELECT pa.title, o.code AS org_code FROM checkreq.user_program_areas upa "
        "JOIN checkreq.program_areas pa ON pa.id = upa.program_area_id JOIN checkreq.organizations o ON o.id = pa.org_id "
        "WHERE upa.user_id = %s", (uid,))
    rules = db.query_one(
        "SELECT (SELECT count(*) FROM checkreq.approval_rules WHERE is_active AND (approver_user_id = %s OR backup_approver_id = %s)) "
        "+ (SELECT count(*) FROM checkreq.global_approvers WHERE is_active AND (approver_user_id = %s OR backup_approver_id = %s)) AS n",
        (uid, uid, uid, uid))
    plan = build_return_plan(promotion=promo, login_type=user["login_type"] if user else None, live_entity_roles=live,
                             program_areas=areas, approver_rules=int(rules["n"]) if rules else 0)
    plan["promotion"], plan["user"] = promo, user
    return plan


def return_to_parish(promotion_id: int, actor_id: int) -> None:
    """Undo a promotion, in one transaction. Raises PromotionBlocked and changes nothing when the plan has a blocker."""
    plan = load_return_plan(promotion_id)
    if plan["blockers"]:
        raise PromotionBlocked(plan["blockers"])
    promo = plan["promotion"]
    uid, org_id, parish_id = promo["user_id"], promo["org_id"], promo["parish_id"]
    granted = [g["role_key"] for g in (promo["entity_roles_granted"] or [])]
    note = f"Returned to parish only (undoing the promotion into {promo['entity_code']})"

    with db.shared_transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE checkreq.user_roles SET revoked_at = NOW(), revoked_by_user_id = %s, note = COALESCE(%s, note) "
                        "WHERE user_id = %s AND org_id = %s AND role_key = ANY(%s) AND revoked_at IS NULL",
                        (actor_id, note, uid, org_id, granted))
            cur.execute("UPDATE checkreq.app_users SET login_type = 'parish' WHERE id = %s AND login_type = 'entity'", (uid,))
            if cur.rowcount != 1:
                raise PromotionBlocked(["This login changed while you were looking at it. Reload the page and try again."])
            for r in (promo["parish_roles_retired"] or []):
                cur.execute("SELECT 1 FROM portal.parish_user_roles WHERE user_id = %s AND parish_id = %s AND role_key = %s "
                            "AND revoked_at IS NULL", (uid, parish_id, r["role_key"]))
                if not cur.fetchone():
                    cur.execute("INSERT INTO portal.parish_user_roles (user_id, parish_id, role_key, granted_by_user_id, note) "
                                "VALUES (%s, %s, %s, %s, %s)", (uid, parish_id, r["role_key"], actor_id, note))
            cur.execute("UPDATE portal.login_promotions SET reversed_at = NOW(), reversed_by_user_id = %s "
                        "WHERE id = %s AND reversed_at IS NULL", (actor_id, promotion_id))
            if cur.rowcount != 1:
                raise PromotionBlocked(["That promotion was already undone."])
