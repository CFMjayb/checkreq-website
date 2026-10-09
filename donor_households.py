"""
donor_households.py -- Beacon Donor Management: households, spouse links, parish connections.

Households are diocese-wide (shared profile), but a parish reaches one only through a member who is
connected to it, and sees only the members who are connected to it. Spouse is a direct link between two
people, never inferred (PF-05). The primary contact is chosen by staff, never calculated (PF-04).

Operations: household_create  household_update  household_add_member  household_move_member
            household_set_primary_contact  household_set_position  spouse_link_set  spouse_link_end
            household_relation_add  household_relation_archive  parish_connection_set
"""
from __future__ import annotations

import uuid

from donor_core import (
    Conflict, Ctx, InvalidInput, NotFound, check_enum, clean_phone, clean_text, diff_fields, log_change,
    need_people, new_batch_id, parse_date, tx, to_bool, HOUSEHOLD_POSITIONS, CONNECTION_KINDS,
    STATEMENT_DELIVERIES, STATEMENT_OPTIONS,
)
from donor_people import require_connection

_H_TEXT = {
    "name": 160, "salutation": 160, "directory_name": 160, "address1": 160, "address2": 160, "city": 80,
    "state": 40, "postal_code": 20, "country": 40, "mail_address1": 160, "mail_address2": 160, "mail_city": 80,
    "mail_state": 40, "mail_postal_code": 20,
}
HOUSEHOLD_FIELDS = tuple(_H_TEXT) + ("home_phone",)
HOUSEHOLD_LABELS = {
    "name": "Household name", "salutation": "Salutation", "directory_name": "Directory name",
    "address1": "Address", "address2": "Address line 2", "city": "City", "state": "State",
    "postal_code": "Zip", "country": "Country", "home_phone": "Home phone", "mail_address1": "Mailing address",
    "mail_address2": "Mailing address line 2", "mail_city": "Mailing city", "mail_state": "Mailing state",
    "mail_postal_code": "Mailing zip",
}


def _clean_household(data: dict) -> dict:
    out: dict = {}
    for k, n in _H_TEXT.items():
        if k in data:
            out[k] = clean_text(data[k], field=HOUSEHOLD_LABELS[k], max_len=n)
    if "home_phone" in data:
        out["home_phone"] = clean_phone(data["home_phone"], field="home phone")
    if out.get("country") is None and "country" in out:
        out["country"] = "US"
    return out


def _household_row(c, household_id: int) -> dict:
    c.execute("SELECT * FROM donor.household WHERE id = %s", (household_id,))
    row = c.fetchone()
    if not row:
        raise NotFound("That household was not found at this parish.")
    return row


def _accessible(c, ctx: Ctx, household_id: int) -> dict:
    """The household, if this parish may touch it: it has a current member connected here, or it is empty
    and this parish created it. Anything else reads as not found."""
    row = _household_row(c, household_id)
    c.execute("SELECT 1 AS x FROM donor.household_member hm JOIN donor.parish_connection pc "
              "ON pc.person_id = hm.person_id AND pc.parish_id = %s "
              "WHERE hm.household_id = %s AND hm.left_at IS NULL LIMIT 1", (ctx.parish_id, household_id))
    if c.fetchone():
        return row
    c.execute("SELECT 1 AS x FROM donor.household_member WHERE household_id = %s AND left_at IS NULL LIMIT 1", (household_id,))
    if not c.fetchone() and row["created_by_parish_id"] == ctx.parish_id:
        return row
    raise NotFound("That household was not found at this parish.")


def _current_membership(c, person_id: int) -> dict | None:
    c.execute("SELECT * FROM donor.household_member WHERE person_id = %s AND left_at IS NULL FOR UPDATE", (person_id,))
    return c.fetchone()


def _add_member_row(c, ctx: Ctx, household_id: int, person_id: int, position: str, primary: bool) -> int:
    if primary and position == "child":
        raise InvalidInput("A child cannot be the primary contact.")
    if primary:
        c.execute("SELECT person_id FROM donor.household_member WHERE household_id = %s AND is_primary_contact "
                  "AND left_at IS NULL", (household_id,))
        for old in c.fetchall():
            c.execute("UPDATE donor.household_member SET is_primary_contact = FALSE WHERE household_id = %s "
                      "AND person_id = %s AND left_at IS NULL", (household_id, old["person_id"]))
            log_change(c, ctx, "household_member", household_id, "primary_contact", f"person {old['person_id']}",
                       f"person {person_id}", person_id=old["person_id"])
    c.execute("INSERT INTO donor.household_member (household_id, person_id, position, is_primary_contact, created_by_user_id) "
              "VALUES (%s,%s,%s,%s,%s) RETURNING id", (household_id, person_id, position, primary, ctx.user_id))
    mid = c.fetchone()["id"]
    log_change(c, ctx, "household_member", household_id, "member", None, f"person {person_id} ({position})",
               person_id=person_id, kind="move")
    return mid


def household_create(ctx: Ctx, data: dict, members: list[dict] | None = None, *, cur=None) -> dict:
    """Create a household, optionally with members: [{person_id, position, is_primary_contact}]. Every member
    must be connected to this parish and not already be in another household (use household_move_member)."""
    need_people(ctx, "people.edit")
    fields = _clean_household(data)
    members = members or []
    with tx(cur) as c:
        for m in members:
            require_connection(c, ctx, m["person_id"], include_archived=False)
            if _current_membership(c, m["person_id"]):
                raise Conflict("That person is already in a household. Move them instead.")
        if not fields.get("name") and members:
            c.execute("SELECT last_name FROM donor.person WHERE id = %s", (members[0]["person_id"],))
            ln = (c.fetchone() or {}).get("last_name")
            if ln:
                fields["name"] = f"The {ln} Household"
        cols = ["created_by_user_id", "created_by_parish_id", "updated_by_user_id", "updated_by_parish_id"]
        vals: list = [ctx.user_id, ctx.parish_id, ctx.user_id, ctx.parish_id]
        for k, v in fields.items():
            cols.append(k)
            vals.append(v)
        c.execute(f"INSERT INTO donor.household ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id", vals)
        hid = c.fetchone()["id"]
        log_change(c, ctx, "household", hid, None, None, "Created", kind="create")
        primaries = [m for m in members if to_bool(m.get("is_primary_contact"))]
        if len(primaries) > 1:
            raise InvalidInput("Only one member can be the primary contact.")
        for m in members:
            pos = check_enum(m.get("position") or "primary_adult", HOUSEHOLD_POSITIONS, field="household position", allow_blank=False)
            _add_member_row(c, ctx, hid, m["person_id"], pos, to_bool(m.get("is_primary_contact")))
        return {"id": hid}


def household_update(ctx: Ctx, household_id: int, changes: dict, *, cur=None) -> dict:
    need_people(ctx, "people.edit")
    fields = _clean_household({k: v for k, v in changes.items() if k in HOUSEHOLD_FIELDS})
    with tx(cur) as c:
        old = _accessible(c, ctx, household_id)
        diffs = diff_fields(old, fields)
        if not diffs:
            return {"id": household_id, "changed": []}
        batch = new_batch_id()
        c.execute(f"UPDATE donor.household SET {', '.join(f'{k} = %s' for k, _, _ in diffs)}, updated_at = NOW(), "
                  "updated_by_user_id = %s, updated_by_parish_id = %s WHERE id = %s",
                  (*[v for _, _, v in diffs], ctx.user_id, ctx.parish_id, household_id))
        for k, o, n in diffs:
            log_change(c, ctx, "household", household_id, k, o, n, batch_id=batch)
        return {"id": household_id, "changed": [k for k, _, _ in diffs]}


def household_add_member(ctx: Ctx, household_id: int, person_id: int, position: str = "primary_adult",
                         is_primary_contact: bool = False, *, cur=None) -> dict:
    need_people(ctx, "people.edit")
    position = check_enum(position, HOUSEHOLD_POSITIONS, field="household position", allow_blank=False)
    with tx(cur) as c:
        _accessible(c, ctx, household_id)
        require_connection(c, ctx, person_id, include_archived=False)
        if _current_membership(c, person_id):
            raise Conflict("That person is already in a household. Move them instead.")
        mid = _add_member_row(c, ctx, household_id, person_id, position, to_bool(is_primary_contact))
        return {"id": mid, "household_id": household_id}


def household_move_member(ctx: Ctx, person_id: int, to_household_id: int | None = None,
                          position: str = "primary_adult", is_primary_contact: bool = False, *, cur=None) -> dict:
    """Move a person to another household, or to a brand-new one when to_household_id is None (PF-07).
    Their giving history stays with them (gifts point at the person, not the household). If they were the
    old household's primary contact, that household is left with none until staff choose another. A household
    left with no members is archived, never deleted."""
    need_people(ctx, "people.edit")
    position = check_enum(position, HOUSEHOLD_POSITIONS, field="household position", allow_blank=False)
    with tx(cur) as c:
        require_connection(c, ctx, person_id, include_archived=False)
        cur_m = _current_membership(c, person_id)
        if to_household_id is not None:
            _accessible(c, ctx, to_household_id)
            if cur_m and cur_m["household_id"] == to_household_id:
                raise InvalidInput("That person is already in that household.")
        if cur_m:
            c.execute("UPDATE donor.household_member SET left_at = NOW(), is_primary_contact = FALSE WHERE id = %s",
                      (cur_m["id"],))
            c.execute("SELECT COUNT(*) AS n FROM donor.household_member WHERE household_id = %s AND left_at IS NULL",
                      (cur_m["household_id"],))
            if c.fetchone()["n"] == 0:
                c.execute("UPDATE donor.household SET archived_at = NOW() WHERE id = %s", (cur_m["household_id"],))
        if to_household_id is None:
            c.execute("SELECT last_name FROM donor.person WHERE id = %s", (person_id,))
            ln = (c.fetchone() or {}).get("last_name")
            c.execute("INSERT INTO donor.household (name, created_by_user_id, created_by_parish_id, updated_by_user_id, "
                      "updated_by_parish_id) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                      (f"The {ln} Household" if ln else None, ctx.user_id, ctx.parish_id, ctx.user_id, ctx.parish_id))
            to_household_id = c.fetchone()["id"]
            log_change(c, ctx, "household", to_household_id, None, None, "Created", kind="create")
        _add_member_row(c, ctx, to_household_id, person_id, position, to_bool(is_primary_contact))
        return {"person_id": person_id, "household_id": to_household_id}


def household_set_primary_contact(ctx: Ctx, household_id: int, person_id: int, *, cur=None) -> dict:
    need_people(ctx, "people.edit")
    with tx(cur) as c:
        _accessible(c, ctx, household_id)
        c.execute("SELECT * FROM donor.household_member WHERE household_id = %s AND person_id = %s AND left_at IS NULL",
                  (household_id, person_id))
        m = c.fetchone()
        if not m:
            raise NotFound("That person is not in this household.")
        require_connection(c, ctx, person_id, include_archived=False)
        if m["position"] == "child":
            raise InvalidInput("A child cannot be the primary contact.")
        if m["is_primary_contact"]:
            return {"household_id": household_id, "person_id": person_id, "changed": False}
        c.execute("SELECT person_id FROM donor.household_member WHERE household_id = %s AND is_primary_contact "
                  "AND left_at IS NULL", (household_id,))
        old = c.fetchone()
        c.execute("UPDATE donor.household_member SET is_primary_contact = FALSE WHERE household_id = %s "
                  "AND is_primary_contact AND left_at IS NULL", (household_id,))
        c.execute("UPDATE donor.household_member SET is_primary_contact = TRUE WHERE id = %s", (m["id"],))
        log_change(c, ctx, "household_member", household_id, "primary_contact",
                   f"person {old['person_id']}" if old else None, f"person {person_id}", person_id=person_id)
        return {"household_id": household_id, "person_id": person_id, "changed": True}


def household_set_position(ctx: Ctx, person_id: int, position: str, *, cur=None) -> dict:
    need_people(ctx, "people.edit")
    position = check_enum(position, HOUSEHOLD_POSITIONS, field="household position", allow_blank=False)
    with tx(cur) as c:
        require_connection(c, ctx, person_id, include_archived=False)
        m = _current_membership(c, person_id)
        if not m:
            raise NotFound("That person is not in a household.")
        if m["position"] == position:
            return {"person_id": person_id, "changed": False}
        if position == "child" and m["is_primary_contact"]:
            raise InvalidInput("Choose another primary contact before making this person a child.")
        c.execute("UPDATE donor.household_member SET position = %s WHERE id = %s", (position, m["id"]))
        log_change(c, ctx, "household_member", m["household_id"], "position", m["position"], position, person_id=person_id)
        return {"person_id": person_id, "changed": True}


# ── Spouse (a direct link, one row per side) ────────────────────────────────────────────────────
def _set_person_field(c, ctx: Ctx, person_id: int, field: str, value, batch) -> None:
    c.execute(f"SELECT {field} FROM donor.person WHERE id = %s FOR UPDATE", (person_id,))
    old = c.fetchone()[field]
    if old == value:
        return
    c.execute(f"UPDATE donor.person SET {field} = %s, updated_at = NOW(), updated_by_user_id = %s, "
              "updated_by_parish_id = %s WHERE id = %s", (value, ctx.user_id, ctx.parish_id, person_id))
    log_change(c, ctx, "person", person_id, field, old, value, person_id=person_id, batch_id=batch)


def spouse_link_set(ctx: Ctx, person_id: int, spouse_id: int, married_on=None, *, cur=None) -> dict:
    """Link two people as spouses. Both must be connected to this parish and neither may already have a
    current spouse. Sets both to married (and the wedding date if one is given and none is on file)."""
    need_people(ctx, "people.edit")
    if person_id == spouse_id:
        raise InvalidInput("A person cannot be their own spouse.")
    married = parse_date(married_on, field="marriage date", allow_future=False)
    with tx(cur) as c:
        for pid in (person_id, spouse_id):
            require_connection(c, ctx, pid, include_archived=False)
            c.execute("SELECT record_type FROM donor.person WHERE id = %s", (pid,))
            if c.fetchone()["record_type"] != "person":
                raise InvalidInput("An organization cannot have a spouse.")
        c.execute("SELECT person_id FROM donor.spouse_link WHERE person_id = ANY(%s) AND ended_at IS NULL FOR UPDATE",
                  ([person_id, spouse_id],))
        if c.fetchall():
            raise Conflict("One of these people already has a spouse. End that link first.")
        key = str(uuid.uuid4())
        for a, b in ((person_id, spouse_id), (spouse_id, person_id)):
            c.execute("INSERT INTO donor.spouse_link (link_key, person_id, spouse_id, married_on, created_by_user_id) "
                      "VALUES (%s,%s,%s,%s,%s)", (key, a, b, married, ctx.user_id))
        log_change(c, ctx, "spouse_link", person_id, "spouse", None, f"person {spouse_id}", person_id=person_id, kind="link")
        log_change(c, ctx, "spouse_link", spouse_id, "spouse", None, f"person {person_id}", person_id=spouse_id, kind="link")
        batch = new_batch_id()
        for pid in (person_id, spouse_id):
            _set_person_field(c, ctx, pid, "marital_status", "married", batch)
            if married:
                c.execute("SELECT wedding_date FROM donor.person WHERE id = %s", (pid,))
                if c.fetchone()["wedding_date"] is None:
                    _set_person_field(c, ctx, pid, "wedding_date", married, batch)
        return {"link_key": key}


def spouse_link_end(ctx: Ctx, person_id: int, reason: str | None = None, *, cur=None) -> dict:
    """End the current spouse link (both sides). A reason of widowed, divorced or separated also sets both
    people's marital status."""
    need_people(ctx, "people.edit")
    reason_key = (reason or "").strip().lower() or None
    with tx(cur) as c:
        require_connection(c, ctx, person_id, include_archived=False)
        c.execute("SELECT * FROM donor.spouse_link WHERE person_id = %s AND ended_at IS NULL FOR UPDATE", (person_id,))
        mine = c.fetchone()
        if not mine:
            raise NotFound("That person has no spouse link.")
        c.execute("UPDATE donor.spouse_link SET ended_at = NOW(), ended_by_user_id = %s, ended_reason = %s "
                  "WHERE link_key = %s AND ended_at IS NULL", (ctx.user_id, reason_key, mine["link_key"]))
        log_change(c, ctx, "spouse_link", person_id, "spouse", f"person {mine['spouse_id']}", None, person_id=person_id,
                   kind="unlink", reason=reason_key)
        log_change(c, ctx, "spouse_link", mine["spouse_id"], "spouse", f"person {person_id}", None,
                   person_id=mine["spouse_id"], kind="unlink", reason=reason_key)
        if reason_key in ("widowed", "divorced", "separated"):
            batch = new_batch_id()
            for pid in (person_id, mine["spouse_id"]):
                _set_person_field(c, ctx, pid, "marital_status", reason_key, batch)
        return {"ended": True}


# ── Related households ──────────────────────────────────────────────────────────────────────────
def household_relation_add(ctx: Ctx, household_id: int, related_household_id: int, description: str, *, cur=None) -> dict:
    need_people(ctx, "people.edit")
    desc = clean_text(description, field="description", max_len=200)
    if not desc:
        raise InvalidInput("Describe how the households are related.", "description")
    if household_id == related_household_id:
        raise InvalidInput("A household cannot be related to itself.")
    with tx(cur) as c:
        _accessible(c, ctx, household_id)
        _accessible(c, ctx, related_household_id)
        c.execute("INSERT INTO donor.household_relation (household_id, related_household_id, description, created_by_user_id) "
                  "VALUES (%s,%s,%s,%s) RETURNING id", (household_id, related_household_id, desc, ctx.user_id))
        rid = c.fetchone()["id"]
        log_change(c, ctx, "household_relation", rid, "description", None, desc, kind="create")
        return {"id": rid}


def household_relation_archive(ctx: Ctx, relation_id: int, *, cur=None) -> dict:
    need_people(ctx, "people.edit")
    with tx(cur) as c:
        c.execute("SELECT * FROM donor.household_relation WHERE id = %s AND archived_at IS NULL FOR UPDATE", (relation_id,))
        r = c.fetchone()
        if not r:
            raise NotFound("That relationship was not found.")
        _accessible(c, ctx, r["household_id"])
        c.execute("UPDATE donor.household_relation SET archived_at = NOW() WHERE id = %s", (relation_id,))
        log_change(c, ctx, "household_relation", relation_id, "archived", None, "true", kind="archive")
        return {"id": relation_id, "archived": True}


# ── This parish's connection to a person ────────────────────────────────────────────────────────
_CONN_LABELS = {"kind": "Connection", "envelope_number": "Envelope number", "statement_option": "Statement",
                "statement_delivery": "Statement delivery", "is_canonical": "Canonical member"}


def parish_connection_set(ctx: Ctx, person_id: int, changes: dict, *, cur=None) -> dict:
    """Change THIS parish's connection to the person: kind (member, giver, visitor), envelope number,
    statement option and delivery, and whether this is their canonical parish. A person is a canonical
    member of one parish at a time (MS-09). Setting kind to member, or canonical, needs the membership
    role. Joint statements need a spouse link."""
    need_people(ctx, "people.edit")
    keys = set(changes) & set(_CONN_LABELS)
    if not keys:
        return {"person_id": person_id, "changed": []}
    new: dict = {}
    if "kind" in keys:
        new["kind"] = check_enum(changes["kind"], CONNECTION_KINDS, field="connection", allow_blank=False)
    if "envelope_number" in keys:
        new["envelope_number"] = clean_text(changes["envelope_number"], field="envelope number", max_len=20)
    if "statement_option" in keys:
        new["statement_option"] = check_enum(changes["statement_option"], STATEMENT_OPTIONS, field="statement", allow_blank=False)
    if "statement_delivery" in keys:
        new["statement_delivery"] = check_enum(changes["statement_delivery"], STATEMENT_DELIVERIES,
                                                field="statement delivery", allow_blank=False)
    if "is_canonical" in keys:
        new["is_canonical"] = to_bool(changes["is_canonical"], field="canonical")
    with tx(cur) as c:
        conn = require_connection(c, ctx, person_id, include_archived=False, lock=True)
        if new.get("kind") == "member" or new.get("is_canonical") is True:
            ctx.require("membership.edit", "Only clergy and membership editors can make someone a member.")
        if new.get("is_canonical") is True:
            new["kind"] = "member"
            c.execute("SELECT 1 AS x FROM donor.parish_connection WHERE person_id = %s AND is_canonical "
                      "AND archived_at IS NULL AND id <> %s", (person_id, conn["id"]))
            if c.fetchone():
                raise Conflict("This person is already a canonical member of another parish. They have to transfer "
                               "first, or the diocese can help.")
        if new.get("kind") and new["kind"] != "member" and "is_canonical" not in new and conn["is_canonical"]:
            new["is_canonical"] = False
        if new.get("statement_option") == "joint":
            c.execute("SELECT 1 AS x FROM donor.spouse_link WHERE person_id = %s AND ended_at IS NULL", (person_id,))
            if not c.fetchone():
                raise InvalidInput("A joint statement needs a spouse link on this person.", "statement_option")
        diffs = diff_fields(conn, new)
        if not diffs:
            return {"person_id": person_id, "changed": []}
        c.execute(f"UPDATE donor.parish_connection SET {', '.join(f'{k} = %s' for k, _, _ in diffs)}, updated_at = NOW() "
                  "WHERE id = %s", (*[v for _, _, v in diffs], conn["id"]))
        batch = new_batch_id()
        for k, o, n in diffs:
            log_change(c, ctx, "parish_connection", conn["id"], k, o, n, person_id=person_id, scope="parish", batch_id=batch)
        return {"person_id": person_id, "changed": [k for k, _, _ in diffs]}


def envelope_lookup(ctx: Ctx, envelope_number: str) -> list[dict]:
    """People at THIS parish with that envelope number, the household's primary contact first (a family
    shares an envelope, so the answer can be several people)."""
    need_people(ctx, "people.view")
    env = clean_text(envelope_number, field="envelope number", max_len=20)
    if not env:
        return []
    import db
    rows = db.query(
        "SELECT p.id, p.first_name, p.last_name, p.org_name, p.record_type, hm.is_primary_contact, hm.position "
        "FROM donor.parish_connection pc JOIN donor.person p ON p.id = pc.person_id "
        "LEFT JOIN donor.household_member hm ON hm.person_id = p.id AND hm.left_at IS NULL "
        "WHERE pc.parish_id = %s AND pc.envelope_number = %s AND pc.archived_at IS NULL "
        "ORDER BY hm.is_primary_contact DESC NULLS LAST, p.id", (ctx.parish_id, env))
    from donor_core import person_label
    return [{"id": r["id"], "name": person_label(r), "is_primary_contact": bool(r["is_primary_contact"])} for r in rows]
