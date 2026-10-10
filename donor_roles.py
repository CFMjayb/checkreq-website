"""
donor_roles.py -- Beacon Donor Management: who may do what, and which parishes have it turned on.

The donor roles live in donor.role / donor.role_grant (NOT in portal.parish_roles: tonight's rule is that
no existing table is written). The existing Parish Admin role is READ from portal.parish_user_roles and
maps to "edit people and households, manage donor roles" (Requirements doc, role table).

  build_ctx(user, parish)      the Ctx every service function takes (reads, never writes)
  role_grant / role_revoke     parish-scoped donor roles; rule 5: nobody grants themselves a finance role, EXCEPT a
                               Beacon Admin or Setup Admin at the parish's diocese (Jay, 2026-10-09), noted as self-assigned
  diocesan_finance_grant       diocese-scoped role, granted only by that diocese's Beacon Admin
  role_roster                  the User Access panel's list
  settings_get / settings_update   activation ("turn Donor Management on for this parish") and options
  ensure_default_status_codes  each parish starts with a short list mapped to the diocesan categories

Two small lookups (user_exists, user_at_parish) read checkreq.app_users and portal.parish_user_roles.
They are module-level functions on purpose, so a test can use synthetic user ids without writing to
those tables.
"""
from __future__ import annotations

import psycopg.errors

import db
import org_time
import parish_roles
from donor_core import (
    Ctx, Conflict, InvalidInput, NotFound, PermissionDenied, FINANCE_ROLES, SECOND_PERSON_ROLES, caps_for, log_change, tx,
    to_bool, clean_text,
)

# The Setup > Users screen groups the role boxes in columns, like TouchPoint's role form. Parish Admin is not a donor role:
# it is given under User Access and only shown (never changed) here. diocesan_finance is shown to a diocese's Beacon Admin only.
ROLE_GROUPS = (
    ("People", ("clergy", "membership_editor")),
    ("Giving", ("gift_entry", "finance", "finance_supervisor", "finance_view_detail", "finance_view")),
)
USER_ROW_CHOICES = (10, 25, 50, 100, 200)
USER_SORTS = ("name", "email", "last", "roles")

DEFAULT_STATUS_CODES = (
    ("MEMBER", "Member", "active_member", 10),
    ("INACTIVE", "Inactive member", "inactive_member", 20),
    ("VISITOR", "Visitor", "non_member", 30),
    ("FRIEND", "Friend of the parish", "non_member", 40),
    ("TRANSFERRED", "Transferred out", "transferred_out", 50),
    ("REMOVED", "Removed", "removed", 60),
    ("DECEASED", "Deceased", "deceased", 70),
)

SETTING_DEFAULTS = {
    "people_enabled": False, "giving_enabled": False, "qbo_company_key": None, "default_cash_account": None,
    "processing_fee_account": None, "due_from_diocese_account": None, "allow_single_person_batch": False,
    "qbo_posting_enabled": False,
    # added by migration 077 (giving). Read tolerantly below so Phase 1 works before 077 is applied.
    "investment_account": None, "in_kind_account": None, "default_class": None,
    # added by migration 082 (Parishioner Self-Service): the diocese's switch for the parishioner's own screen (/my). Off until turned on.
    "portal_enabled": False,
    # added by migration 083: the parish's member sign-in link name (/my/<portal_slug>), made from its name when the portal is switched on.
    "portal_slug": None,
}
# Which settings need which capability to change.
_DIOCESAN_SETTINGS = {"people_enabled", "giving_enabled", "allow_single_person_batch", "qbo_posting_enabled", "portal_enabled", "portal_slug"}
_ACCOUNT_SETTINGS = {"qbo_company_key", "default_cash_account", "processing_fee_account", "due_from_diocese_account",
                     "investment_account", "in_kind_account", "default_class"}
_BOOL_SETTINGS = {"people_enabled", "giving_enabled", "allow_single_person_batch", "qbo_posting_enabled", "portal_enabled"}


# ── Read-only lookups into existing tables (test seams) ─────────────────────────────────────────
def user_exists(user_id: int) -> bool:
    return db.query_one("SELECT 1 AS x FROM checkreq.app_users WHERE id = %s AND is_active", (user_id,)) is not None


def user_at_parish(user_id: int, parish_id: int) -> bool:
    """Does this person already have a Beacon login role at this parish (a portal role, or a donor role)?"""
    if parish_roles.user_has_any_parish_role(user_id, None, parish_id):
        return True
    return db.query_one(
        "SELECT 1 AS x FROM donor.role_grant WHERE user_id = %s AND parish_id = %s AND revoked_at IS NULL LIMIT 1",
        (user_id, parish_id)) is not None


# ── Catalog and settings ────────────────────────────────────────────────────────────────────────
def role_catalog(max_phase: int = 9) -> list[dict]:
    return db.query("SELECT * FROM donor.role WHERE is_active AND phase <= %s ORDER BY sort_order", (max_phase,))


def settings_get(parish_id: int) -> dict:
    """The parish's settings, with defaults filled in. A parish with no row yet reads as everything off."""
    row = db.query_one("SELECT * FROM donor.parish_settings WHERE parish_id = %s", (parish_id,))
    out = dict(SETTING_DEFAULTS)
    out["parish_id"] = parish_id
    if row:
        for k in SETTING_DEFAULTS:
            out[k] = row.get(k, out[k])          # a column that migration 077 adds may not exist yet
    return out


def ensure_default_status_codes(cur, parish_id: int) -> int:
    """Create the default member status codes if this parish has none yet. Returns how many were created."""
    cur.execute("SELECT COUNT(*) AS n FROM donor.member_status_code WHERE parish_id = %s", (parish_id,))
    if cur.fetchone()["n"]:
        return 0
    for code, lbl, cat, order in DEFAULT_STATUS_CODES:
        cur.execute(
            "INSERT INTO donor.member_status_code (parish_id, code, label, diocesan_category, sort_order) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (parish_id, code) DO NOTHING",
            (parish_id, code, lbl, cat, order))
    return len(DEFAULT_STATUS_CODES)


ACTIVATION_SWITCHES = ("people_enabled", "giving_enabled", "allow_single_person_batch", "qbo_posting_enabled", "portal_enabled")
ACTIVATION_ACCOUNTS = ("qbo_company_key", "default_cash_account", "processing_fee_account", "due_from_diocese_account",
                       "investment_account", "in_kind_account", "default_class")


def activation_changes(form) -> dict:
    """The settings a 'Turn on and accounts' form posted, as settings_update wants them. One source for both places the form lives (the Portal
    pop-up on Manage Parishes and the accounts card on Settings). A switch is only read when its hidden `has_<name>` marker came with it (an
    unchecked box sends nothing), and a blank link name keeps the published one (a blank box must never erase a link)."""
    changes: dict = {}
    for k in ACTIVATION_SWITCHES:
        if f"has_{k}" in form:
            changes[k] = form.get(k) is not None
    for k in ACTIVATION_ACCOUNTS:
        if k in form:
            changes[k] = form.get(k)
    if (form.get("portal_slug") or "").strip():
        changes["portal_slug"] = form.get("portal_slug")
    return changes


def settings_update(ctx: Ctx, changes: dict, *, cur=None) -> dict:
    """Change this parish's settings. Turning Donor Management on or off, the single-person-batch exception
    and QBO posting are diocesan decisions (capability parish.activate). The QBO account names are set by
    Finance or by the diocese."""
    unknown = set(changes) - set(SETTING_DEFAULTS)
    if unknown:
        raise InvalidInput(f"Unknown setting: {', '.join(sorted(unknown))}.")
    clean: dict = {}
    for k, v in changes.items():
        if k in _DIOCESAN_SETTINGS:
            ctx.require("parish.activate", "Only the diocese can change that setting.")
        else:
            if not (ctx.can("funds.manage") or ctx.can("parish.activate")):
                raise PermissionDenied("Only Finance or the diocese can change the QBO account settings.")
        if k == "portal_slug":
            import donor_portal_login as PL
            try:
                clean[k] = PL.normalize_slug(v)
            except ValueError as e:
                raise InvalidInput(str(e), "portal_slug")
            continue
        clean[k] = to_bool(v, field=k) if k in _BOOL_SETTINGS else clean_text(v, field=k, max_len=120)
    if not clean:
        return settings_get(ctx.parish_id)
    with tx(cur) as c:
        c.execute("INSERT INTO donor.parish_settings (parish_id) VALUES (%s) ON CONFLICT (parish_id) DO NOTHING",
                  (ctx.parish_id,))
        c.execute("SELECT * FROM donor.parish_settings WHERE parish_id = %s FOR UPDATE", (ctx.parish_id,))
        old = c.fetchone()
        if clean.get("portal_enabled") and not old.get("portal_slug") and "portal_slug" not in clean and "portal_slug" in old:
            import donor_portal_login as PL
            clean["portal_slug"] = PL.default_slug(ctx.parish_id)           # the link is made from the parish name when the portal goes on
        sets, params = [], []
        for k, v in clean.items():
            if old.get(k) != v:
                sets.append(f"{k} = %s")
                params.append(v)
                log_change(c, ctx, "parish_settings", ctx.parish_id, k, old[k], v, scope="parish")
        if sets:
            sets.append("updated_by_user_id = %s")
            params.append(ctx.user_id)
            sets.append("updated_at = NOW()")
            try:
                c.execute(f"UPDATE donor.parish_settings SET {', '.join(sets)} WHERE parish_id = %s",
                          (*params, ctx.parish_id))
            except psycopg.errors.UniqueViolation:
                raise InvalidInput("That link name is already used by another parish. Pick a different one.", "portal_slug")
        if clean.get("people_enabled"):
            ensure_default_status_codes(c, ctx.parish_id)
    return settings_get(ctx.parish_id)


# ── Roles ───────────────────────────────────────────────────────────────────────────────────────
def live_donor_roles(user_id: int, parish_id: int, org_id: int | None) -> set[str]:
    rows = db.query(
        "SELECT role_key FROM donor.role_grant WHERE user_id = %s AND revoked_at IS NULL "
        "AND (parish_id = %s OR (org_id IS NOT NULL AND org_id = %s))",
        (user_id, parish_id, org_id))
    return {r["role_key"] for r in rows}


def effective_roles(user_id: int, parish_id: int, org_id: int | None) -> set[str]:
    roles = live_donor_roles(user_id, parish_id, org_id)
    if parish_roles.user_has_parish_role(user_id, "parish_admin", parish_id):
        roles.add("parish_admin")
    return roles


def build_ctx(user: dict, parish: dict) -> Ctx:
    """The Ctx for this signed-in person at this parish. `parish` is the dict
    parish_mode.effective_parish_mode returns (never a client-supplied id).

    A Beacon Admin or a Setup Admin held AT THE PARISH'S OWN DIOCESE may give themselves any role (Jay, 2026-10-09), so they also
    manage roles here (roles.manage). Only the Beacon Admin may activate the parish or give Diocesan Finance."""
    uid, pid, org_id = user["id"], parish["id"], parish.get("org_id")
    roles = effective_roles(uid, pid, org_id)
    is_dioc_admin = parish_roles.holds_role_at_parish_org(uid, "beacon_admin", org_id)
    is_setup_admin = parish_roles.holds_role_at_parish_org(uid, "setup_admin", org_id)
    may_self = is_dioc_admin or is_setup_admin
    can_activate = is_dioc_admin or parish_roles.holds_role_at_parish_org(uid, "parish_mode_user", org_id)
    manager = may_self or parish_roles.is_parish_manager(uid, pid)
    return Ctx(
        user_id=uid, user_label=user.get("display_name") or user.get("email") or f"User #{uid}",
        parish_id=pid, parish_name=parish.get("name") or "", org_id=org_id, roles=frozenset(roles),
        caps=caps_for(roles, manager, can_activate), is_diocesan_admin=is_dioc_admin, may_self_assign=may_self,
        settings=settings_get(pid),
    )


def _self_assigning(ctx: Ctx, user_id: int) -> bool:
    """True when the person is giving a role to THEMSELVES and is a Beacon Admin or Setup Admin at this parish's diocese: the one
    case where the 'nobody gives themselves a finance or Clergy role' and 'must already have a login here' rules are waived."""
    return bool(ctx.may_self_assign) and user_id == ctx.user_id


def _self_label(ctx: Ctx) -> str:
    return "Self-assigned by Beacon Admin" if ctx.is_diocesan_admin else "Self-assigned by Setup Admin"


def role_grant(ctx: Ctx, user_id: int, role_key: str, note: str | None = None, *, cur=None) -> dict:
    """Give a donor role to someone at THIS parish. Rules: caller needs roles.manage. Nobody grants
    themselves a finance role (rule 5 / RL-03: a Parish Admin who needs one gets it from a second Parish
    Admin or from the diocese). Repeating a grant that is already live changes nothing."""
    ctx.require("roles.manage", "Only a Parish Admin or the diocese can give roles.")
    role = db.query_one("SELECT * FROM donor.role WHERE key = %s AND is_active", (role_key,))
    if not role:
        raise NotFound("That role does not exist.")
    if role["scope"] != "parish":
        raise InvalidInput("That role is given by the diocese, not by a parish.")
    self_ok = _self_assigning(ctx, user_id)
    if role["is_finance"] and user_id == ctx.user_id and not self_ok:
        raise PermissionDenied("No one can give themselves a finance role. Ask a second Parish Admin or the diocese.")
    if role_key in SECOND_PERSON_ROLES and user_id == ctx.user_id and not self_ok:
        raise PermissionDenied(f"No one can give themselves the {role['label']} role. Ask a second Parish Admin or the diocese.")
    if not user_exists(user_id):
        raise NotFound("That person does not have a Beacon login.")
    if not self_ok and not user_at_parish(user_id, ctx.parish_id):
        raise InvalidInput("That person has no Beacon login role at this parish yet. Add them under User Access first.")
    note = clean_text(note, field="note")
    if self_ok:                                              # every self-assignment says so, in the grant and in the change log
        note = f"{note} · {_self_label(ctx)}" if note else _self_label(ctx)
    with tx(cur) as c:
        c.execute("SELECT id FROM donor.role_grant WHERE role_key = %s AND user_id = %s AND parish_id = %s "
                  "AND revoked_at IS NULL", (role_key, user_id, ctx.parish_id))
        existing = c.fetchone()
        if existing:
            return {"id": existing["id"], "created": False}
        c.execute(
            "INSERT INTO donor.role_grant (role_key, user_id, parish_id, granted_by_user_id, note) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (role_key, user_id, ctx.parish_id, ctx.user_id, clean_text(note, field="note", max_len=200)))
        gid = c.fetchone()["id"]
        log_change(c, ctx, "role_grant", gid, role_key, None, f"user {user_id}", kind="grant", scope="parish",
                   reason=_self_label(ctx) if self_ok else None)
        return {"id": gid, "created": True}


def role_revoke(ctx: Ctx, user_id: int, role_key: str, reason: str | None = None, *, cur=None) -> bool:
    ctx.require("roles.manage", "Only a Parish Admin or the diocese can take roles away.")
    with tx(cur) as c:
        c.execute(
            "UPDATE donor.role_grant SET revoked_at = NOW(), revoked_by_user_id = %s, revoke_reason = %s "
            "WHERE role_key = %s AND user_id = %s AND parish_id = %s AND revoked_at IS NULL RETURNING id",
            (ctx.user_id, clean_text(reason, field="reason"), role_key, user_id, ctx.parish_id))
        row = c.fetchone()
        if not row:
            return False
        log_change(c, ctx, "role_grant", row["id"], role_key, f"user {user_id}", None, kind="revoke",
                   scope="parish", reason=reason)
        return True


def diocesan_finance_grant(ctx: Ctx, user_id: int, *, cur=None) -> dict:
    """The diocese-level audit role. Only that diocese's Beacon Admin may give it (a Setup Admin may not, not even to themselves),
    and not to themselves either unless they may give themselves any role, which a Beacon Admin may (Jay, 2026-10-09)."""
    if not ctx.is_diocesan_admin or ctx.org_id is None:
        raise PermissionDenied("Only the diocese's Beacon Admin can give Diocesan Finance.")
    self_ok = _self_assigning(ctx, user_id)
    if user_id == ctx.user_id and not self_ok:
        raise PermissionDenied("No one can give themselves a finance role.")
    if not user_exists(user_id):
        raise NotFound("That person does not have a Beacon login.")
    with tx(cur) as c:
        c.execute("SELECT id FROM donor.role_grant WHERE role_key = 'diocesan_finance' AND user_id = %s AND org_id = %s "
                  "AND revoked_at IS NULL", (user_id, ctx.org_id))
        existing = c.fetchone()
        if existing:
            return {"id": existing["id"], "created": False}
        c.execute("INSERT INTO donor.role_grant (role_key, user_id, org_id, granted_by_user_id, note) "
                  "VALUES ('diocesan_finance', %s, %s, %s, %s) RETURNING id",
                  (user_id, ctx.org_id, ctx.user_id, _self_label(ctx) if self_ok else None))
        gid = c.fetchone()["id"]
        log_change(c, ctx, "role_grant", gid, "diocesan_finance", None, f"user {user_id}", kind="grant", scope="parish",
                   reason=_self_label(ctx) if self_ok else None)
        return {"id": gid, "created": True}


def assignable_users(ctx: Ctx) -> list[dict]:
    """People a task can be given to: everyone with a Beacon login role at this parish (names only). Needs
    notes.staff, the capability that writes tasks."""
    ctx.require("notes.staff")
    return db.query(
        "SELECT u.id, COALESCE(u.display_name, u.email) AS name FROM checkreq.app_users u WHERE u.is_active AND u.id IN ("
        " SELECT user_id FROM portal.parish_user_roles WHERE parish_id = %s AND revoked_at IS NULL"
        " UNION SELECT user_id FROM donor.role_grant WHERE parish_id = %s AND revoked_at IS NULL"
        ") ORDER BY 2", (ctx.parish_id, ctx.parish_id))


def role_roster(ctx: Ctx) -> list[dict]:
    """People with a Beacon login at this parish, with the donor roles each holds. Needs roles.manage."""
    ctx.require("roles.manage")
    users = db.query(
        "SELECT u.id, u.email, u.display_name FROM checkreq.app_users u WHERE u.is_active AND u.id IN ("
        " SELECT user_id FROM portal.parish_user_roles WHERE parish_id = %s AND revoked_at IS NULL"
        " UNION SELECT user_id FROM donor.role_grant WHERE parish_id = %s AND revoked_at IS NULL"
        ") ORDER BY COALESCE(u.display_name, u.email)", (ctx.parish_id, ctx.parish_id))
    grants = db.query("SELECT user_id, role_key FROM donor.role_grant WHERE parish_id = %s AND revoked_at IS NULL",
                      (ctx.parish_id,))
    by_user: dict[int, list[str]] = {}
    for g in grants:
        by_user.setdefault(g["user_id"], []).append(g["role_key"])
    for u in users:
        u["donor_roles"] = sorted(by_user.get(u["id"], []))
        u["is_parish_admin"] = parish_roles.user_has_parish_role(u["id"], "parish_admin", ctx.parish_id)
    return users


# ── Who can sign in as this person, and what donor roles they hold (shown on the person's profile) ──
def users_by_email(emails) -> list[dict]:
    """Active Beacon logins whose email is one of `emails` (compared without regard to case). A module-level function on
    purpose, like user_exists, so a test can stand in for the read of checkreq.app_users."""
    wanted = sorted({str(e).strip().lower() for e in (emails or []) if e and str(e).strip()})
    if not wanted:
        return []
    return db.query("SELECT id, email, display_name, last_login_at FROM checkreq.app_users WHERE is_active AND LOWER(email) = ANY(%s) ORDER BY id", (wanted,))


def user_labels(user_ids) -> dict:
    """{user id: display name or email} for people shown in a history line. Same test seam as users_by_email."""
    ids = sorted({int(i) for i in (user_ids or []) if i is not None})
    if not ids:
        return {}
    return {r["id"]: (r["display_name"] or r["email"]) for r in
            db.query("SELECT id, email, display_name FROM checkreq.app_users WHERE id = ANY(%s)", (ids,))}


def roles_for_person(ctx: Ctx, person_id: int) -> list[dict]:
    """The Beacon logins that belong to this person (an active login whose email is one of the person's live email addresses)
    AND hold a role at THIS parish, with each donor role they hold here, when it was given, by whom and why. Needs roles.manage
    (a Parish Admin or the diocese): the page that shows it is the one place Jay asked role grants to be visible. A login with
    no role at this parish is left out, so the profile never reveals access somewhere else. Empty when nothing matches."""
    ctx.require("roles.manage", "Only a Parish Admin or the diocese can see who holds which role.")
    if not db.query_one("SELECT 1 AS x FROM donor.parish_connection WHERE person_id = %s AND parish_id = %s", (person_id, ctx.parish_id)):
        raise NotFound("That person was not found at this parish.")
    emails = [r["value"] for r in db.query("SELECT value FROM donor.person_contact WHERE person_id = %s AND kind = 'email' AND archived_at IS NULL", (person_id,))]
    out = []
    zone = org_time.zone_name_for_org(ctx.org_id)
    for u in users_by_email(emails):
        if not user_at_parish(u["id"], ctx.parish_id):
            continue
        grants = db.query(
            "SELECT g.role_key, r.label, g.granted_at, g.granted_by_user_id, g.note FROM donor.role_grant g JOIN donor.role r ON r.key = g.role_key "
            "WHERE g.user_id = %s AND g.revoked_at IS NULL AND (g.parish_id = %s OR (g.org_id IS NOT NULL AND g.org_id = %s)) ORDER BY r.sort_order",
            (u["id"], ctx.parish_id, ctx.org_id))
        last = u.get("last_login_at")
        out.append({"user_id": u["id"], "name": u["display_name"] or u["email"], "email": u["email"],
                    "last_sign_in": org_time.format_local(last, zone) if last else None,
                    "is_parish_admin": parish_roles.user_has_parish_role(u["id"], "parish_admin", ctx.parish_id), "roles": grants})
    names = user_labels([g["granted_by_user_id"] for u in out for g in u["roles"]])
    for u in out:
        for g in u["roles"]:
            g["granted_by_name"] = names.get(g["granted_by_user_id"]) or ""
    return out


# ── The Users screen (Setup > Users), laid out like TouchPoint's Users list ─────────────────────
# Who appears: everyone with a Beacon login role at THIS parish (a portal role such as Parish Admin or Member, or a donor
# role), the same people role_roster lists. Roles are given and taken away on the edit page, all in one Save.
_ROSTER_SQL = ("u.id IN (SELECT user_id FROM portal.parish_user_roles WHERE parish_id = %s AND revoked_at IS NULL"
               " UNION SELECT user_id FROM donor.role_grant WHERE parish_id = %s AND revoked_at IS NULL)")


def _like(s: str) -> str:
    return "%" + s.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# Four reads of checkreq.app_users and portal.parish_user_roles. Module-level functions on purpose, like user_exists and
# user_at_parish, so a test can stand in for them with synthetic logins (the donor tests never write outside `donor`).
def roster_users(parish_id: int, *, q: str = "", within=None, idle=None) -> list[dict]:
    """Active logins with a role at this parish (a portal role or a donor role): id, email, display_name, last_login_at.
    Filtered by name or email text, by 'signed in within N days' and by 'no sign-in for N days'."""
    return db.query(
        "SELECT u.id, u.email, u.display_name, u.last_login_at FROM checkreq.app_users u WHERE u.is_active AND " + _ROSTER_SQL
        + " AND (%s = '' OR LOWER(COALESCE(u.display_name, '')) LIKE %s OR LOWER(u.email) LIKE %s)"
        " AND (%s::int IS NULL OR u.last_login_at >= NOW() - make_interval(days => %s))"
        " AND (%s::int IS NULL OR u.last_login_at IS NULL OR u.last_login_at < NOW() - make_interval(days => %s))",
        (parish_id, parish_id, q, _like(q), _like(q), within, within, idle, idle))


def parish_admin_ids(parish_id: int, user_ids) -> set:
    """Which of these logins are Parish Admin at this parish."""
    return {r["user_id"] for r in db.query(
        "SELECT pur.user_id FROM portal.parish_user_roles pur JOIN portal.parish_roles pr ON pr.key = pur.role_key AND pr.is_active "
        "WHERE pur.parish_id = %s AND pur.role_key = 'parish_admin' AND pur.revoked_at IS NULL AND pur.user_id = ANY(%s)",
        (parish_id, list(user_ids)))}


def login_row(user_id: int) -> dict | None:
    return db.query_one("SELECT id, email, display_name, last_login_at FROM checkreq.app_users WHERE id = %s AND is_active", (user_id,))


def roster_names(parish_id: int, exclude_user_id: int) -> list[dict]:
    """{id, name} of every other login at this parish, for the 'copy roles from' list."""
    return db.query(
        "SELECT u2.id, COALESCE(u2.display_name, u2.email) AS name FROM checkreq.app_users u2 WHERE u2.is_active AND u2.id <> %s AND "
        + _ROSTER_SQL.replace("u.id", "u2.id") + " ORDER BY 2", (exclude_user_id, parish_id, parish_id))


def _days(v, label: str):
    if v in (None, ""):
        return None
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise InvalidInput(f"'{label}' has to be a whole number of days.")
    if n < 0 or n > 36500:
        raise InvalidInput(f"'{label}' has to be a number of days from 0 to 36500.")
    return n


def _person_links(ctx: Ctx, emails) -> dict:
    """{lowercase email: person id} for the people at THIS parish who have that email. Only for a role that may open people."""
    if not ctx.can("people.view"):
        return {}
    wanted = sorted({str(e).strip().lower() for e in emails if e})
    if not wanted:
        return {}
    rows = db.query(
        "SELECT DISTINCT ON (LOWER(c.value)) LOWER(c.value) AS email, c.person_id FROM donor.person_contact c "
        "JOIN donor.parish_connection pc ON pc.person_id = c.person_id AND pc.parish_id = %s AND pc.archived_at IS NULL "
        "WHERE c.kind = 'email' AND c.archived_at IS NULL AND LOWER(c.value) = ANY(%s) "
        "ORDER BY LOWER(c.value), c.is_preferred DESC, c.person_id", (ctx.parish_id, wanted))
    return {r["email"]: r["person_id"] for r in rows}


def users_list(ctx: Ctx, *, q: str = "", role: str = "", within_days=None, idle_days=None, sort: str = "name",
               direction: str = "asc", page=1, rows=25) -> dict:
    """The Users list: filter by name or email, by role, by 'signed in within N days' and 'no sign-in for N days'; sort; page.
    Needs roles.manage. Returns {rows, total, page, pages, per_page, role_options}."""
    ctx.require("roles.manage", "Only a Parish Admin or the diocese can see who holds which role.")
    q = (q or "").strip()[:80]
    catalog = db.query("SELECT key, label, scope, sort_order FROM donor.role WHERE is_active ORDER BY sort_order")
    order = {r["key"]: r["sort_order"] for r in catalog}
    labels = {r["key"]: r["label"] for r in catalog}
    role = (role or "").strip()
    if role and role not in order and role not in ("parish_admin", "none"):
        raise InvalidInput("That role does not exist.")
    within, idle = _days(within_days, "Last sign in within"), _days(idle_days, "No sign ins for")
    sort = sort if sort in USER_SORTS else "name"
    descending = str(direction).lower() == "desc"
    pid, oid = ctx.parish_id, ctx.org_id
    users = roster_users(pid, q=q, within=within, idle=idle)
    ids = [u["id"] for u in users]
    grants = db.query(
        "SELECT user_id, role_key FROM donor.role_grant WHERE user_id = ANY(%s) AND revoked_at IS NULL "
        "AND (parish_id = %s OR (org_id IS NOT NULL AND org_id = %s))", (ids, pid, oid)) if ids else []
    admins = parish_admin_ids(pid, ids) if ids else set()
    held: dict[int, list[str]] = {}
    for g in grants:
        held.setdefault(g["user_id"], []).append(g["role_key"])
    links = _person_links(ctx, [u["email"] for u in users])
    zone = org_time.zone_name_for_org(oid)
    out = []
    for u in users:
        keys = sorted(held.get(u["id"], []), key=lambda k: order.get(k, 999))
        is_admin = u["id"] in admins
        if role == "parish_admin" and not is_admin:
            continue
        if role == "none" and keys:
            continue
        if role not in ("", "parish_admin", "none") and role not in keys:
            continue
        out.append({
            "id": u["id"], "name": u["display_name"] or u["email"], "email": u["email"], "last_login_at": u["last_login_at"],
            "last_sign_in": org_time.format_local(u["last_login_at"], zone) if u["last_login_at"] else None,
            "is_parish_admin": is_admin, "donor_roles": [{"key": k, "label": labels.get(k, k)} for k in keys],
            "person_id": links.get(u["email"].strip().lower()),
        })
    keyfn = {
        "name": lambda r: r["name"].lower(), "email": lambda r: r["email"].lower(),
        "last": lambda r: (r["last_login_at"] is not None, r["last_login_at"].timestamp() if r["last_login_at"] else 0.0),
        "roles": lambda r: (len(r["donor_roles"]) + (1 if r["is_parish_admin"] else 0), r["name"].lower()),
    }[sort]
    out.sort(key=keyfn, reverse=descending)
    total = len(out)
    if str(rows).lower() in ("all", "0"):
        per_page = max(total, 1)
    else:
        try:
            per_page = int(rows)
        except (TypeError, ValueError):
            per_page = 25
        per_page = per_page if per_page in USER_ROW_CHOICES else 25
    pages = max(1, -(-total // per_page))
    try:
        page_no = min(max(1, int(page)), pages)
    except (TypeError, ValueError):
        page_no = 1
    chunk = out[(page_no - 1) * per_page: page_no * per_page]
    return {"rows": chunk, "total": total, "page": page_no, "pages": pages, "per_page": per_page,
            "role_options": [{"key": r["key"], "label": r["label"]} for r in catalog if r["scope"] == "parish"]}


def user_detail(ctx: Ctx, user_id: int) -> dict:
    """Everything the edit page needs for one login at this parish: who they are, the role boxes in groups (each with its label,
    description, whether it is ticked, and whether this person may change it), who else could be copied from, and when each
    role was given. Needs roles.manage. A login with no role at this parish reads as not found."""
    ctx.require("roles.manage", "Only a Parish Admin or the diocese can see who holds which role.")
    u = login_row(user_id)
    self_ok = _self_assigning(ctx, user_id)
    if not u or not (self_ok or user_at_parish(user_id, ctx.parish_id)):
        raise NotFound("That login was not found at this parish.")
    catalog = {r["key"]: r for r in db.query("SELECT * FROM donor.role WHERE is_active ORDER BY sort_order")}
    mine = db.query(
        "SELECT g.role_key, g.granted_at, g.granted_by_user_id, g.note, g.org_id FROM donor.role_grant g WHERE g.user_id = %s "
        "AND g.revoked_at IS NULL AND (g.parish_id = %s OR (g.org_id IS NOT NULL AND g.org_id = %s))", (user_id, ctx.parish_id, ctx.org_id))
    held = {g["role_key"]: g for g in mine}
    names = user_labels([g["granted_by_user_id"] for g in mine])
    zone = org_time.zone_name_for_org(ctx.org_id)
    phase_max = 2 if ctx.settings.get("giving_enabled") else 1
    is_self = user_id == ctx.user_id

    def item(key: str) -> dict | None:
        r = catalog.get(key)
        if not r:
            return None
        have = key in held
        if r["phase"] > phase_max and not have:
            return None                      # a role of a part that is not switched on is not offered, but is always shown once held
        locked, why = False, ""
        if not have and is_self and (r["is_finance"] or key in SECOND_PERSON_ROLES) and not self_ok:
            locked, why = True, "No one can give themselves this role. Ask a second Parish Admin or the diocese."
        if r["scope"] == "diocese" and not (ctx.is_diocesan_admin and ctx.org_id is not None):
            locked, why = True, "Only the diocese's Beacon Admin can give this role."
        g = held.get(key)
        return {"key": key, "label": r["label"], "description": r["description"], "is_finance": bool(r["is_finance"]),
                "checked": have, "locked": locked, "why": why,
                "given": (org_time.format_local(g["granted_at"], zone) + (" by " + names[g["granted_by_user_id"]] if names.get(g["granted_by_user_id"]) else "")
                          + (" · " + g["note"] if g["note"] else "")) if g else ""}

    groups = []
    for title, keys in ROLE_GROUPS:
        items = [i for i in (item(k) for k in keys) if i]
        if items:
            groups.append({"title": title, "items": items})
    if ctx.is_diocesan_admin or "diocesan_finance" in held:
        di = item("diocesan_finance")
        if di:
            groups.append({"title": "Diocese", "items": [di]})
    roster = roster_names(ctx.parish_id, user_id)
    other = db.query("SELECT user_id, role_key FROM donor.role_grant WHERE parish_id = %s AND revoked_at IS NULL", (ctx.parish_id,))
    by_user: dict[int, list[str]] = {}
    for g in other:
        by_user.setdefault(g["user_id"], []).append(g["role_key"])
    copy_from = [{"id": r["id"], "name": r["name"], "roles": sorted(by_user.get(r["id"], []))} for r in roster]
    return {
        "id": u["id"], "name": u["display_name"] or u["email"], "email": u["email"], "is_self": is_self, "self_assign": self_ok,
        "last_sign_in": org_time.format_local(u["last_login_at"], zone) if u["last_login_at"] else None,
        "is_parish_admin": parish_roles.user_has_parish_role(user_id, "parish_admin", ctx.parish_id),
        "person_id": _person_links(ctx, [u["email"]]).get(u["email"].strip().lower()),
        "groups": groups, "copy_from": copy_from,
    }


def roles_set(ctx: Ctx, user_id: int, wanted, note: str | None = None, *, cur=None) -> dict:
    """Make this login's donor roles at THIS parish exactly `wanted` (a list of role keys), in ONE transaction: every role that
    is ticked and not held is given, every role held and not ticked is taken away, or nothing changes at all. Same rules as
    role_grant: roles.manage; nobody gives themselves a finance or second-person role (except a Beacon Admin or Setup Admin at the
    diocese, who may give themselves any role); diocesan roles only by the diocese's Beacon Admin. Returns {"added": [labels], "removed": [labels]}. Taking a role away needs no second person."""
    ctx.require("roles.manage", "Only a Parish Admin or the diocese can give or take away roles.")
    wanted = {str(k).strip() for k in (wanted or []) if k and str(k).strip()}
    catalog = {r["key"]: r for r in db.query("SELECT * FROM donor.role WHERE is_active")}
    if wanted - set(catalog):
        raise NotFound("That role does not exist.")
    if not user_exists(user_id):
        raise NotFound("That person does not have a Beacon login.")
    if not (_self_assigning(ctx, user_id) or user_at_parish(user_id, ctx.parish_id)):
        raise InvalidInput("That person has no Beacon login role at this parish yet. Add them under User Access first.")
    note = clean_text(note, field="note", max_len=120)
    with tx(cur) as c:
        c.execute("SELECT id, role_key FROM donor.role_grant WHERE user_id = %s AND parish_id = %s AND revoked_at IS NULL", (user_id, ctx.parish_id))
        have_parish = {r["role_key"]: r["id"] for r in c.fetchall()}
        have_org: dict = {}
        if ctx.org_id is not None:
            c.execute("SELECT id, role_key FROM donor.role_grant WHERE user_id = %s AND org_id = %s AND revoked_at IS NULL",
                      (user_id, ctx.org_id))
            have_org = {r["role_key"]: r["id"] for r in c.fetchall()}
        added, removed = [], []
        for key in sorted(wanted, key=lambda k: catalog[k]["sort_order"]):
            role = catalog[key]
            if role["scope"] == "diocese":
                if key in have_org:
                    continue
                dr = diocesan_finance_grant(ctx, user_id, cur=c)          # its own rules: the diocese's Beacon Admin, never to oneself
                if dr["created"]:
                    added.append(role["label"])
            else:
                if key in have_parish:
                    continue
                role_grant(ctx, user_id, key, note, cur=c)               # its own rules: no self-granted finance or clergy role
                added.append(role["label"])
        for key, gid in have_parish.items():
            if key not in wanted:
                role_revoke(ctx, user_id, key, note, cur=c)
                removed.append(catalog[key]["label"] if key in catalog else key)
        for key, gid in have_org.items():
            if key not in wanted:
                if not (ctx.is_diocesan_admin and ctx.org_id is not None):
                    raise PermissionDenied("Only the diocese's Beacon Admin can take away a diocesan role.")
                c.execute("UPDATE donor.role_grant SET revoked_at = NOW(), revoked_by_user_id = %s, revoke_reason = %s WHERE id = %s AND revoked_at IS NULL",
                          (ctx.user_id, note, gid))
                log_change(c, ctx, "role_grant", gid, key, f"user {user_id}", None, kind="revoke", scope="parish", reason=note)
                removed.append(catalog[key]["label"] if key in catalog else key)
    return {"added": added, "removed": removed}
