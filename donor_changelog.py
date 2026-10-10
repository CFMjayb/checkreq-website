"""
donor_changelog.py -- Beacon Donor Management: read the change history and undo ONE change (PF-12).

What a parish may see of the history of a person:
  * profile changes (scope 'profile'): every one, from every connected parish, each naming the parish that
    made it ("a change here also shows at the other parishes"). Who made it is shown by name.
  * parish changes (scope 'parish'): only this parish's own.
A change to a note is logged as "note added", never with its text.

Undo (change_undo) restores the old value of ONE field from ONE change. It refuses if the value has been
changed again since ("changed since"), if it was already undone, or if that kind of change is not undoable
(creates, archives, links and gifts are corrected by their own operations). The undo is itself logged, so
the history never loses anything.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import db
from donor_core import (
    MINOR_SQL, Conflict, Ctx, InvalidInput, NotFound, PermissionDenied, diff_fields, digits_of, log_change, need_people,
    parse_date, ser, tx,
)
from donor_people import FIELD_LABELS, require_connection

# table -> (key column, {field: type}, capability needed, parish-scoped?)
_P_TYPES = {}
for _k in ("title", "first_name", "middle_name", "last_name", "suffix", "goes_by", "former_name", "org_name",
           "gender", "marital_status", "alt_name", "occupation", "employer", "school", "grade"):
    _P_TYPES[_k] = "text"
for _k in ("birth_date", "wedding_date", "deceased_date"):
    _P_TYPES[_k] = "date"
for _k in ("in_directory", "hide_phone_in_directory", "do_not_mail", "do_not_call", "do_not_email"):
    _P_TYPES[_k] = "bool"

UNDOABLE = {
    "person": ("id", _P_TYPES, "people.edit", False),
    "household": ("id", {k: "text" for k in (
        "name", "salutation", "directory_name", "address1", "address2", "city", "state", "postal_code", "country",
        "home_phone", "mail_address1", "mail_address2", "mail_city", "mail_state", "mail_postal_code")},
        "people.edit", False),
    "person_contact": ("id", {"value": "text"}, "people.edit", False),
    "membership": ("id", {"how_joined": "text", "join_date": "date", "removal_date": "date", "removal_reason": "text"},
                   "membership.edit", True),
    "parish_connection": ("id", {"envelope_number": "text", "statement_option": "text", "statement_delivery": "text"},
                          "people.edit", True),
}

_TABLE_LABELS = {"person": "Profile", "household": "Household", "person_contact": "Contact", "membership": "Membership",
                 "parish_connection": "Connection", "sacramental_event": "Sacrament", "transfer_letter": "Transfer letter",
                 "note": "Note", "task": "Task", "household_member": "Household", "spouse_link": "Spouse",
                 "role_grant": "Role", "parish_settings": "Settings", "member_status_code": "Status code",
                 "household_relation": "Related household"}


def _typed(kind: str, text: str | None):
    if text is None:
        return None
    if kind == "date":
        return parse_date(text)
    if kind == "bool":
        return text == "true"
    return text


def _field_label(row: dict) -> str:
    f = row.get("field")
    if row["table_name"] == "person_contact" and f == "value":
        return ("Email" if row.get("contact_kind") == "email" else "Phone") + (
            f" ({row['contact_subtype']})" if row.get("contact_subtype") and row.get("contact_kind") == "phone" else "")
    if not f:
        return _TABLE_LABELS.get(row["table_name"], row["table_name"])
    return FIELD_LABELS.get(f, f.replace("_", " ").capitalize())


# Which capability a history row needs before the viewer may see it. The log holds every kind of change tied to a
# person (gifts, pledges, notes, sacraments, ...), and its old/new text carries real content, so the System tab must
# not show a row the viewer could not open on its own screen. A table not listed here is hidden (fail closed).
_OPEN_TABLES = {"person", "household", "person_contact", "parish_connection", "household_member", "spouse_link", "household_relation"}
_ROW_CAP = {
    "gift": "giving.read", "gift_split": "giving.read", "batch": "giving.read", "pledge": "giving.read", "soft_credit": "giving.read",
    "pledge_request": "giving.read",
    "fund": "giving.read", "campaign": "giving.read", "noncontribution_account": "giving.read",
    "membership": "membership.view", "member_status_code": "membership.view",
    "sacramental_event": "sacrament.view", "transfer_letter": "sacrament.view",
    "note": "notes.clergy",          # a clergy-only note must not be hinted at; the note list itself is filtered by visibility
    "task": "notes.staff",
    "role_grant": "roles.manage", "parish_settings": "roles.manage", "parishioner_login": "roles.manage",
}


def _may_see_change(ctx: Ctx, row: dict, minor_hidden: bool) -> bool:
    t = row["table_name"]
    if t in _OPEN_TABLES:
        if minor_hidden and (t == "person_contact" or (t == "person" and row.get("field") in (
                "birth_date", "wedding_date", "occupation", "employer", "school", "grade"))):
            return False                  # a minor's birth date, contact values, school and grade are for minors.details holders only
        return True
    cap = _ROW_CAP.get(t)
    return bool(cap and ctx.can(cap))


def changes_for_person(ctx: Ctx, person_id: int, limit: int = 100) -> list[dict]:
    """The history screen's rows, newest first, with who and which parish. Profile changes from every
    connected parish, parish changes from this parish only. A row appears only if the viewer's role could see
    the underlying record (a gift row needs giving.read, a note row notes.clergy, and so on)."""
    need_people(ctx, "people.view")
    limit = max(1, min(int(limit), 500))
    with tx() as c:
        require_connection(c, ctx, person_id)
        c.execute(f"SELECT {MINOR_SQL} AS m FROM donor.person p LEFT JOIN donor.household_member hm "
                  "ON hm.person_id = p.id AND hm.left_at IS NULL WHERE p.id = %s", (person_id,))
        mrow = c.fetchone()
        minor_hidden = bool(mrow and mrow["m"]) and not ctx.can("minors.details")
        c.execute("SELECT household_id FROM donor.household_member WHERE person_id = %s AND left_at IS NULL", (person_id,))
        h = c.fetchone()
        hid = h["household_id"] if h else None
        c.execute(
            "SELECT cl.*, pc.kind AS contact_kind, pc.subtype AS contact_subtype FROM donor.change_log cl "
            "LEFT JOIN donor.person_contact pc ON cl.table_name = 'person_contact' AND pc.id = cl.row_id "
            "WHERE (cl.person_id = %s OR cl.person_id IN (SELECT merged_person_id FROM donor.merge_history "
            "WHERE survivor_person_id = %s) OR (cl.table_name = 'household' AND cl.row_id = %s)) "
            "AND (cl.scope = 'profile' OR cl.parish_id = %s) "
            "ORDER BY cl.created_at DESC, cl.id DESC LIMIT %s", (person_id, person_id, hid, ctx.parish_id, limit))
        rows = [r for r in c.fetchall() if _may_see_change(ctx, r, minor_hidden)]
    user_ids = sorted({r["user_id"] for r in rows if r["user_id"] is not None})
    parish_ids = sorted({r["parish_id"] for r in rows if r["parish_id"] is not None})
    names: dict = {}
    pnames: dict = {}
    if user_ids:
        for u in db.query("SELECT id, COALESCE(display_name, email) AS n FROM checkreq.app_users WHERE id = ANY(%s)", (user_ids,)):
            names[u["id"]] = u["n"]
    if parish_ids:
        for p in db.query("SELECT id, name FROM portal.parishes WHERE id = ANY(%s)", (parish_ids,)):
            pnames[p["id"]] = p["name"]
    out = []
    for r in rows:
        r["who"] = names.get(r["user_id"]) or (f"User #{r['user_id']}" if r["user_id"] else "System")
        if r["user_id"] == 0 and (r.get("reason") or "").startswith("Parishioner self-service"):
            r["who"] = "The parishioner (self-service)"      # a change the person made themselves on their own screen (user 0, no roles)
        r["parish_label"] = "This parish" if r["parish_id"] == ctx.parish_id else (
            pnames.get(r["parish_id"]) or (f"Parish #{r['parish_id']}" if r["parish_id"] else ""))
        r["field_label"] = _field_label(r)
        spec = UNDOABLE.get(r["table_name"])
        r["undoable"] = bool(
            spec and r["kind"] == "update" and r["undone_at"] is None and r["field"] in spec[1]
            and ctx.can(spec[2]) and (not spec[3] or r["parish_id"] == ctx.parish_id))
        out.append(r)
    return out


def change_undo(ctx: Ctx, change_id: int, *, cur=None) -> dict:
    """Undo one field of one earlier change. Conflict if the value has changed since."""
    need_people(ctx, "people.view")
    with tx(cur) as c:
        c.execute("SELECT * FROM donor.change_log WHERE id = %s FOR UPDATE", (change_id,))
        ch = c.fetchone()
        if not ch:
            raise NotFound("That change was not found.")
        spec = UNDOABLE.get(ch["table_name"])
        if not spec or ch["kind"] != "update" or ch["field"] not in spec[1]:
            raise InvalidInput("That kind of change cannot be undone with one click.")
        key_col, types, cap, parish_scoped = spec
        ctx.require(cap)
        if ch["scope"] == "parish" or parish_scoped:
            if ch["parish_id"] != ctx.parish_id:
                raise NotFound("That change was not found.")
        # The change must be about someone this parish is connected to (or a household reaching one of them).
        pid = ch["person_id"]
        if ch["table_name"] == "household":
            c.execute("SELECT 1 AS x FROM donor.household_member hm JOIN donor.parish_connection pc ON pc.person_id = hm.person_id "
                      "AND pc.parish_id = %s WHERE hm.household_id = %s AND hm.left_at IS NULL LIMIT 1", (ctx.parish_id, ch["row_id"]))
            if not c.fetchone():
                raise NotFound("That change was not found.")
        else:
            if pid is None:
                raise NotFound("That change was not found.")
            require_connection(c, ctx, pid, include_archived=False)
        if ch["undone_at"] is not None:
            raise Conflict("That change was already undone.")
        table, field = ch["table_name"], ch["field"]
        c.execute(f"SELECT {field} AS v FROM donor.{table} WHERE {key_col} = %s FOR UPDATE", (ch["row_id"],))
        row = c.fetchone()
        if not row:
            raise NotFound("The record this change belongs to no longer exists.")
        if ser(row["v"]) != ch["new_value"]:
            raise Conflict("This was changed again after that edit, so it cannot be undone from here. "
                           "Edit it directly instead.")
        restore = _typed(types[field], ch["old_value"])
        if types[field] == "text" and table == "person" and restore is None:
            # restoring a blank must not leave a person with no name at all
            c.execute("SELECT first_name, last_name, org_name, record_type FROM donor.person WHERE id = %s", (ch["row_id"],))
            p = c.fetchone()
            merged = {**p, field: None}
            if p["record_type"] == "person" and not (merged["first_name"] or merged["last_name"]):
                raise InvalidInput("That would leave the person without a name.")
            if p["record_type"] == "organization" and not merged["org_name"]:
                raise InvalidInput("That would leave the organization without a name.")
        if table == "person_contact":
            c.execute("SELECT kind FROM donor.person_contact WHERE id = %s", (ch["row_id"],))
            kind = c.fetchone()["kind"]
            c.execute("SELECT 1 AS x FROM donor.person_contact WHERE person_id = %s AND kind = %s AND archived_at IS NULL "
                      "AND id <> %s AND LOWER(value) = LOWER(%s)", (pid, kind, ch["row_id"], restore))
            if c.fetchone():
                raise Conflict("That value is already on this person, so the change cannot be undone.")
            c.execute("UPDATE donor.person_contact SET value = %s, digits = %s WHERE id = %s",
                      (restore, digits_of(restore) if kind == "phone" else "", ch["row_id"]))
        else:
            extra = ""
            if table in ("person", "household"):
                extra = ", updated_at = NOW()"
            c.execute(f"UPDATE donor.{table} SET {field} = %s{extra} WHERE {key_col} = %s", (restore, ch["row_id"]))
        c.execute("UPDATE donor.change_log SET undone_at = NOW(), undone_by_user_id = %s WHERE id = %s", (ctx.user_id, change_id))
        log_change(c, ctx, table, ch["row_id"], field, ch["new_value"], ch["old_value"], person_id=pid, kind="undo",
                   scope=ch["scope"], undo_of_id=change_id)
        return {"id": change_id, "restored": ch["old_value"]}
