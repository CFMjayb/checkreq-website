"""
donor_people.py -- Beacon Donor Management: people and their contact details.

One shared profile per person across every parish (PF-18). A parish reaches a person ONLY through its
own row in donor.parish_connection; a person with no connection here reads as "not found", never as
"forbidden", so the screens leak nothing about people at other parishes.

Operations (each maps 1:1 to a future MCP tool, NF-02):
  person_search  person_get  person_create  person_update  person_archive  person_restore
  contact_add  contact_update  contact_archive
  find_profile_matches  person_link_existing         (connect a person another parish already has)
  directory_list  people_export  placeholder_ensure

Rules enforced here (and pinned by Tools/test_donor_people.py):
  * every query that returns parish data filters on ctx.parish_id
  * minors (under 18) are left out of the directory and exports unless a role with minors.details asks
    for them, and their birth date and contacts are hidden from roles that do not need them (PF-16)
  * nothing is deleted: archiving ends THIS parish's connection, and the shared profile itself is
    archived only when no parish is connected any more (PF-09)
  * every write is logged to donor.change_log by log_change, inside the same transaction (NF-04)
"""
from __future__ import annotations

import csv
import io
import re

import db
from donor_core import (
    Conflict, Ctx, InvalidInput, MINOR_SQL, NotFound, PermissionDenied, age_on, check_enum, clean_email,
    clean_phone, clean_text, digits_of, diff_fields, is_minor, label, log_change, need_people, new_batch_id,
    parse_date, person_full_name, person_label, to_bool, tx, today,
    CONNECTION_KINDS, CONTACT_KINDS, CONTACT_SUBTYPES, GENDERS, GRADES, MARITAL_STATUSES, PLACEHOLDER_KINDS,
    RECORD_TYPES,
)

# Person columns a screen may set, and how each is cleaned.
_TEXT_FIELDS = {
    "title": 40, "first_name": 80, "middle_name": 80, "last_name": 100, "suffix": 20, "goes_by": 80,
    "former_name": 100, "org_name": 160, "alt_name": 100, "occupation": 100, "employer": 120, "school": 120,
}
_ENUM_FIELDS = {"gender": GENDERS, "marital_status": MARITAL_STATUSES, "grade": GRADES}
_DATE_FIELDS = ("birth_date", "wedding_date", "deceased_date")
_BOOL_FIELDS = ("in_directory", "hide_phone_in_directory", "do_not_mail", "do_not_call", "do_not_email")
PERSON_ONLY_FIELDS = ("first_name", "middle_name", "last_name", "suffix", "goes_by", "former_name", "title",
                      "gender", "marital_status", "birth_date", "wedding_date", "deceased_date",
                      "alt_name", "occupation", "employer", "school", "grade")
# A minor's school, grade, employer and occupation are hidden from a role that may not see a minor's details (like the
# birth date and contacts). See person_get.
MINOR_HIDDEN_FIELDS = ("occupation", "employer", "school", "grade")
BOOL_FIELDS = _BOOL_FIELDS
EDITABLE_FIELDS = tuple(_TEXT_FIELDS) + tuple(_ENUM_FIELDS) + _DATE_FIELDS + _BOOL_FIELDS
FIELD_LABELS = {
    "alt_name": "Alt name", "occupation": "Occupation", "employer": "Employer", "school": "School", "grade": "Grade",
    "title": "Title", "first_name": "First name", "middle_name": "Middle name", "last_name": "Last name",
    "suffix": "Suffix", "goes_by": "Goes by", "former_name": "Former name", "org_name": "Organization name",
    "gender": "Gender", "marital_status": "Marital status", "birth_date": "Birth date",
    "wedding_date": "Wedding date", "deceased_date": "Deceased date", "in_directory": "In directory",
    "hide_phone_in_directory": "Hide phones in directory", "do_not_mail": "Do not mail",
    "do_not_call": "Do not call", "do_not_email": "Do not email",
}


# ── Helpers ─────────────────────────────────────────────────────────────────────────────────────
def clean_person_fields(data: dict, record_type: str, *, creating: bool) -> dict:
    """Validate and normalize the person columns present in `data`. Unknown keys are ignored here (the
    callers decide what to allow). Organizations may not carry person-only fields."""
    out: dict = {}
    for k, max_len in _TEXT_FIELDS.items():
        if k in data:
            out[k] = clean_text(data[k], field=FIELD_LABELS[k], max_len=max_len)
    for k, allowed in _ENUM_FIELDS.items():
        if k in data:
            out[k] = check_enum(data[k], allowed, field=FIELD_LABELS[k])
    for k in _DATE_FIELDS:
        if k in data:
            out[k] = parse_date(data[k], field=FIELD_LABELS[k].lower(), allow_future=(k == "wedding_date"))
    for k in _BOOL_FIELDS:
        if k in data:
            out[k] = to_bool(data[k], field=FIELD_LABELS[k])
    if record_type == "organization":
        bad = [k for k in PERSON_ONLY_FIELDS if out.get(k) not in (None, "")]
        if bad:
            raise InvalidInput("An organization does not have: " + ", ".join(FIELD_LABELS[k] for k in bad) + ".")
    if creating:
        if record_type == "organization":
            if not out.get("org_name"):
                raise InvalidInput("An organization needs a name.", "org_name")
        elif not (out.get("first_name") or out.get("last_name")):
            raise InvalidInput("A person needs at least a first or last name.", "last_name")
    else:
        if record_type == "person" and "org_name" in out and out["org_name"]:
            raise InvalidInput("A person does not have an organization name.", "org_name")
    bd, dd = out.get("birth_date"), out.get("deceased_date")
    if bd and dd and dd < bd:
        raise InvalidInput("The deceased date cannot be before the birth date.", "deceased_date")
    return out


def require_connection(cur, ctx: Ctx, person_id: int, *, include_archived: bool = True, lock: bool = False) -> dict:
    """This parish's connection to the person, or NotFound. The ONLY door to a person's data."""
    cur.execute(
        "SELECT * FROM donor.parish_connection WHERE person_id = %s AND parish_id = %s"
        + ("" if include_archived else " AND archived_at IS NULL") + (" FOR UPDATE" if lock else ""),
        (person_id, ctx.parish_id))
    row = cur.fetchone()
    if not row:
        if not include_archived:
            cur.execute("SELECT 1 AS x FROM donor.parish_connection WHERE person_id = %s AND parish_id = %s",
                        (person_id, ctx.parish_id))
            if cur.fetchone():
                raise InvalidInput("That person is archived at this parish. Restore them before changing anything.")
        raise NotFound("That person was not found at this parish.")
    return row


def _person_row(cur, person_id: int) -> dict:
    cur.execute("SELECT * FROM donor.person WHERE id = %s", (person_id,))
    row = cur.fetchone()
    if not row:
        raise NotFound("That person was not found at this parish.")
    return row


def _household_position(cur, person_id: int) -> str | None:
    cur.execute("SELECT position FROM donor.household_member WHERE person_id = %s AND left_at IS NULL", (person_id,))
    r = cur.fetchone()
    return r["position"] if r else None


def person_is_minor(cur, p: dict) -> bool:
    return is_minor(p.get("birth_date"), household_position=_household_position(cur, p["id"]),
                    deceased_date=p.get("deceased_date"))


def _touch(cur, ctx: Ctx, person_id: int) -> None:
    cur.execute("UPDATE donor.person SET updated_at = NOW(), updated_by_user_id = %s, updated_by_parish_id = %s "
                "WHERE id = %s", (ctx.user_id, ctx.parish_id, person_id))


def _preferred_contacts(cur, person_id: int) -> dict:
    cur.execute("SELECT * FROM donor.person_contact WHERE person_id = %s AND archived_at IS NULL "
                "ORDER BY kind, is_preferred DESC, id", (person_id,))
    return {"all": cur.fetchall()}


# ── Search ──────────────────────────────────────────────────────────────────────────────────────
_SEARCH_SELECT = f"""
SELECT p.id, p.record_type, p.title, p.first_name, p.middle_name, p.last_name, p.suffix, p.goes_by,
       p.former_name, p.org_name, p.gender, p.marital_status, p.birth_date, p.deceased_date,
       p.is_placeholder, p.placeholder_kind, p.in_directory, p.hide_phone_in_directory,
       p.do_not_mail, p.do_not_call, p.do_not_email,
       pc.kind AS connection_kind, pc.envelope_number, pc.is_canonical, pc.archived_at AS connection_archived_at,
       pc.statement_option, pc.statement_delivery,
       hm.household_id, hm.position AS household_position, hm.is_primary_contact,
       h.name AS household_name, h.address1, h.address2, h.city, h.state, h.postal_code,
       h.mail_address1, h.mail_address2, h.mail_city, h.mail_state, h.mail_postal_code, h.home_phone,
       sc.code AS status_code, sc.label AS status_label, sc.diocesan_category,
       em.value AS email, ph.value AS phone,
       {MINOR_SQL} AS is_minor,
       COUNT(*) OVER() AS total_rows
  FROM donor.parish_connection pc
  JOIN donor.person p ON p.id = pc.person_id
  LEFT JOIN donor.household_member hm ON hm.person_id = p.id AND hm.left_at IS NULL
  LEFT JOIN donor.household h ON h.id = hm.household_id
  LEFT JOIN donor.membership m ON m.person_id = p.id AND m.parish_id = pc.parish_id
  LEFT JOIN donor.member_status_code sc ON sc.id = m.status_code_id
  LEFT JOIN LATERAL (SELECT c.value FROM donor.person_contact c
                      WHERE c.person_id = p.id AND c.kind = 'email' AND c.archived_at IS NULL
                      ORDER BY c.is_preferred DESC, c.id LIMIT 1) em ON TRUE
  LEFT JOIN LATERAL (SELECT c.value FROM donor.person_contact c
                      WHERE c.person_id = p.id AND c.kind = 'phone' AND c.archived_at IS NULL
                      ORDER BY c.is_preferred DESC, c.id LIMIT 1) ph ON TRUE
"""


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_rows(ctx: Ctx, q: str, *, member_status: str | None = None, gender: str | None = None,
                 marital_status: str | None = None, record_type: str | None = None,
                 include_archived: bool = False, directory_only: bool = False, include_minors: bool = True,
                 limit: int | None = 50, offset: int = 0) -> list[dict]:
    can_minor = ctx.can("minors.details")
    where = ["pc.parish_id = %s"]
    params: list = [ctx.parish_id]
    if not include_archived:
        where.append("pc.archived_at IS NULL")
        where.append("p.archived_at IS NULL")
    if not include_minors:
        where.append(f"NOT {MINOR_SQL}")
    if directory_only:
        where += ["pc.kind = 'member'", "p.in_directory", "NOT p.is_placeholder", "p.record_type = 'person'",
                  "p.deceased_date IS NULL",
                  "(sc.diocesan_category IS NULL OR sc.diocesan_category = 'active_member')"]
    if member_status:
        if not ctx.can("membership.view"):
            raise PermissionDenied("You cannot filter by member status.")
        where.append("(sc.code = %s OR sc.diocesan_category = %s)")
        params += [member_status, member_status]
    if gender:
        where.append("p.gender = %s")
        params.append(check_enum(gender, GENDERS, field="gender"))
    if marital_status:
        where.append("p.marital_status = %s")
        params.append(check_enum(marital_status, MARITAL_STATUSES, field="marital status"))
    if record_type:
        where.append("p.record_type = %s")
        params.append(check_enum(record_type, RECORD_TYPES, field="record type"))
    qs = (q or "").strip()
    # A whole phone number typed with spaces, "(410) 555-0142", is one search term, not two.
    tokens = [qs] if re.fullmatch(r"[\d()\-.+ ]{4,}", qs) else re.split(r"\s+", qs)
    for tok in tokens:
        if not tok:
            continue
        low = tok.lower()
        preds: list[str] = []
        pp: list = []
        # Contact-based matches never apply to a minor the searcher may not see details of.
        contact_guard = "(%s::bool OR NOT " + MINOR_SQL + ")"
        if "@" in low:
            preds.append("(EXISTS (SELECT 1 FROM donor.person_contact c WHERE c.person_id = p.id AND c.kind = 'email' "
                         "AND c.archived_at IS NULL AND LOWER(c.value) LIKE %s) AND " + contact_guard + ")")
            pp += ["%" + _like_escape(low) + "%", can_minor]
        else:
            like = _like_escape(low) + "%"
            preds.append("(LOWER(p.first_name) LIKE %s OR LOWER(p.last_name) LIKE %s OR LOWER(p.goes_by) LIKE %s "
                         "OR LOWER(p.former_name) LIKE %s OR LOWER(p.middle_name) LIKE %s OR LOWER(p.org_name) LIKE %s)")
            pp += [like, like, like, like, like, "%" + _like_escape(low) + "%"]
            if len(low) >= 3:
                preds.append("(h.address1 ILIKE %s OR h.city ILIKE %s OR h.postal_code LIKE %s)")
                pp += ["%" + _like_escape(tok) + "%", _like_escape(tok) + "%", _like_escape(tok) + "%"]
            d = digits_of(tok)
            if d and re.fullmatch(r"[\d()\-.+ ]+", tok):         # a number: envelope, person id, or phone digits
                preds.append("pc.envelope_number = %s")
                pp.append(tok)
                if len(d) <= 9 and d == tok:
                    preds.append("p.id = %s")
                    pp.append(int(d))
                if len(d) >= 4:
                    preds.append("(EXISTS (SELECT 1 FROM donor.person_contact c WHERE c.person_id = p.id "
                                 "AND c.kind = 'phone' AND c.archived_at IS NULL AND c.digits LIKE %s) AND "
                                 + contact_guard + ")")
                    pp += ["%" + d + "%", can_minor]
        where.append("(" + " OR ".join(preds) + ")")
        params += pp
    sql = _SEARCH_SELECT + " WHERE " + " AND ".join(where) + \
        " ORDER BY LOWER(COALESCE(p.last_name, p.org_name, '')), LOWER(COALESCE(p.first_name, '')), p.id"
    if limit is not None:
        sql += " LIMIT %s OFFSET %s"
        params += [limit, offset]
    rows = db.query(sql, tuple(params))
    out = []
    can_membership = ctx.can("membership.view")
    for r in rows:
        r["name"] = person_label(r)
        r["full_name"] = person_full_name(r)
        r["archived"] = r["connection_archived_at"] is not None
        if r["is_minor"] and not can_minor:
            r["email"] = r["phone"] = None
            r["birth_date"] = None
        if not can_membership:
            r["status_code"] = r["status_label"] = r["diocesan_category"] = None
        r["age"] = age_on(r["birth_date"]) if r["birth_date"] else None
        out.append(r)
    return out


def person_search(ctx: Ctx, q: str = "", *, member_status: str | None = None, gender: str | None = None,
                  marital_status: str | None = None, record_type: str | None = None,
                  include_archived: bool = False, limit: int = 50, offset: int = 0) -> dict:
    """Quick search (SR-01): name, e-mail, phone, address, envelope number or id, with filters. Only people
    connected to THIS parish are ever returned. Returns {rows, total}."""
    need_people(ctx, "people.view")
    limit = max(1, min(int(limit), 500))
    rows = _search_rows(ctx, q, member_status=member_status, gender=gender, marital_status=marital_status,
                        record_type=record_type, include_archived=include_archived, limit=limit,
                        offset=max(0, int(offset)))
    total = rows[0]["total_rows"] if rows else 0
    for r in rows:
        r.pop("total_rows", None)
    return {"rows": rows, "total": total}


# ── Read one person ─────────────────────────────────────────────────────────────────────────────
def person_get(ctx: Ctx, person_id: int) -> dict:
    """The person as THIS parish may see them: the shared profile (minors redacted for roles that do not
    need the details), contacts, household (only members connected to this parish), spouse (only if
    connected here), this parish's connection and membership, and the names of the other parishes the
    person is connected to (kind only: nothing of theirs is shown)."""
    need_people(ctx, "people.view")
    with tx() as c:
        conn = require_connection(c, ctx, person_id)
        p = _person_row(c, person_id)
        pos = _household_position(c, person_id)
        minor = is_minor(p["birth_date"], household_position=pos, deceased_date=p["deceased_date"])
        redact = minor and not ctx.can("minors.details")

        c.execute("SELECT * FROM donor.person_contact WHERE person_id = %s AND archived_at IS NULL "
                  "ORDER BY kind, is_preferred DESC, id", (person_id,))
        contacts = [] if redact else c.fetchall()

        household = None
        c.execute("SELECT hm.*, h.* , hm.id AS member_row_id FROM donor.household_member hm "
                  "JOIN donor.household h ON h.id = hm.household_id WHERE hm.person_id = %s AND hm.left_at IS NULL",
                  (person_id,))
        hrow = c.fetchone()
        if hrow:
            hid = hrow["household_id"]
            c.execute(
                "SELECT p2.id, p2.record_type, p2.first_name, p2.middle_name, p2.last_name, p2.suffix, p2.goes_by, "
                "p2.org_name, p2.birth_date, p2.deceased_date, p2.is_placeholder, hm2.position, hm2.is_primary_contact, "
                "pc2.kind AS connection_kind, sl.spouse_id "
                "FROM donor.household_member hm2 JOIN donor.person p2 ON p2.id = hm2.person_id "
                "JOIN donor.parish_connection pc2 ON pc2.person_id = p2.id AND pc2.parish_id = %s "
                "LEFT JOIN donor.spouse_link sl ON sl.person_id = p2.id AND sl.ended_at IS NULL "
                "WHERE hm2.household_id = %s AND hm2.left_at IS NULL ORDER BY hm2.is_primary_contact DESC, "
                "CASE hm2.position WHEN 'primary_adult' THEN 0 WHEN 'secondary_adult' THEN 1 ELSE 2 END, p2.id",
                (ctx.parish_id, hid))
            members = c.fetchall()
            for m in members:
                m["name"] = person_full_name(m)
                m["age"] = age_on(m["birth_date"]) if m["birth_date"] else None
                m["is_minor"] = is_minor(m["birth_date"], household_position=m["position"], deceased_date=m["deceased_date"])
                if m["is_minor"] and not ctx.can("minors.details"):
                    m["age"] = None
            c.execute("SELECT r.id, r.description, r.related_household_id, h2.name AS related_name "
                      "FROM donor.household_relation r JOIN donor.household h2 ON h2.id = r.related_household_id "
                      "WHERE r.household_id = %s AND r.archived_at IS NULL AND EXISTS (SELECT 1 FROM donor.household_member x "
                      "JOIN donor.parish_connection pcx ON pcx.person_id = x.person_id AND pcx.parish_id = %s "
                      "WHERE x.household_id = r.related_household_id AND x.left_at IS NULL)", (hid, ctx.parish_id))
            relations = c.fetchall()
            household = {k: hrow[k] for k in (
                "household_id", "name", "salutation", "directory_name", "address1", "address2", "city", "state",
                "postal_code", "country", "home_phone", "mail_address1", "mail_address2", "mail_city", "mail_state",
                "mail_postal_code")}
            household["position"] = hrow["position"]
            household["is_primary_contact"] = hrow["is_primary_contact"]
            household["members"] = members
            household["relations"] = relations

        c.execute("SELECT sl.spouse_id, sl.married_on FROM donor.spouse_link sl WHERE sl.person_id = %s AND sl.ended_at IS NULL",
                  (person_id,))
        sp = c.fetchone()
        spouse = None
        if sp:
            c.execute("SELECT 1 AS x FROM donor.parish_connection WHERE person_id = %s AND parish_id = %s",
                      (sp["spouse_id"], ctx.parish_id))
            if c.fetchone():
                sprow = _person_row(c, sp["spouse_id"])
                spouse = {"id": sprow["id"], "name": person_full_name(sprow), "married_on": sp["married_on"], "visible": True}
            else:
                spouse = {"id": None, "name": None, "married_on": sp["married_on"], "visible": False}

        c.execute("SELECT pc.parish_id, pc.kind, pc.connected_at FROM donor.parish_connection pc "
                  "WHERE pc.person_id = %s AND pc.parish_id <> %s AND pc.archived_at IS NULL ORDER BY pc.connected_at",
                  (person_id, ctx.parish_id))
        others = c.fetchall()

        membership = None
        if ctx.can("membership.view"):
            c.execute("SELECT m.*, sc.code AS status_code, sc.label AS status_label, sc.diocesan_category "
                      "FROM donor.membership m LEFT JOIN donor.member_status_code sc ON sc.id = m.status_code_id "
                      "WHERE m.person_id = %s AND m.parish_id = %s", (person_id, ctx.parish_id))
            membership = c.fetchone()

    person = {k: p[k] for k in p}
    if redact:
        person["birth_date"] = None
        person["wedding_date"] = None
        for k in MINOR_HIDDEN_FIELDS:
            person[k] = None
    age = age_on(person["birth_date"]) if person.get("birth_date") else None
    person["name"] = person_label(p)
    person["full_name"] = person_full_name(p)
    return {
        "person": person, "age": age, "is_minor": minor, "redacted": redact, "contacts": contacts,
        "household": household, "spouse": spouse, "connection": conn, "other_connections": others,
        "membership": membership, "archived": conn["archived_at"] is not None,
        "household_position": pos,
    }


# ── Create ──────────────────────────────────────────────────────────────────────────────────────
def _insert_contacts(c, ctx: Ctx, person_id: int, contacts: list[dict]) -> None:
    by_kind: dict[str, list[dict]] = {}
    for item in contacts:
        kind = check_enum(item.get("kind"), CONTACT_KINDS, field="contact type", allow_blank=False)
        value = clean_email(item.get("value")) if kind == "email" else clean_phone(item.get("value"))
        if value is None:
            continue
        by_kind.setdefault(kind, []).append({
            "kind": kind, "value": value, "subtype": check_enum(item.get("subtype") or "other", CONTACT_SUBTYPES,
                                                                field="contact subtype"),
            "preferred": to_bool(item.get("is_preferred"))})
    for kind, items in by_kind.items():
        seen: set = set()
        uniq = []
        for it in items:
            key = it["value"].lower()
            if key not in seen:
                seen.add(key)
                uniq.append(it)
        flagged = [i for i in uniq if i["preferred"]]
        if len(flagged) > 1:
            raise InvalidInput(f"Only one preferred {kind} is allowed.")
        if not flagged:
            uniq[0]["preferred"] = True
        for it in uniq:
            c.execute("INSERT INTO donor.person_contact (person_id, kind, subtype, value, digits, is_preferred, created_by_user_id) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                      (person_id, kind, it["subtype"], it["value"], digits_of(it["value"]) if kind == "phone" else "",
                       it["preferred"], ctx.user_id))


def local_duplicates(cur, ctx: Ctx, fields: dict) -> list[dict]:
    """Likely duplicates ALREADY AT THIS PARISH: same first and last name and a matching (or unknown) birth date,
    or the same organization name."""
    if fields.get("org_name"):
        cur.execute("SELECT p.id, p.org_name, p.first_name, p.last_name, p.record_type FROM donor.person p "
                    "JOIN donor.parish_connection pc ON pc.person_id = p.id AND pc.parish_id = %s "
                    "WHERE p.record_type = 'organization' AND LOWER(p.org_name) = LOWER(%s)",
                    (ctx.parish_id, fields["org_name"]))
        return cur.fetchall()
    if not (fields.get("first_name") and fields.get("last_name")):
        return []
    cur.execute("SELECT p.id, p.org_name, p.first_name, p.last_name, p.record_type, p.birth_date FROM donor.person p "
                "JOIN donor.parish_connection pc ON pc.person_id = p.id AND pc.parish_id = %s "
                "WHERE p.record_type = 'person' AND LOWER(p.first_name) = LOWER(%s) AND LOWER(p.last_name) = LOWER(%s) "
                "AND (%s::date IS NULL OR p.birth_date IS NULL OR p.birth_date = %s::date)",
                (ctx.parish_id, fields["first_name"], fields["last_name"], fields.get("birth_date"), fields.get("birth_date")))
    return cur.fetchall()


def person_create(ctx: Ctx, data: dict, *, record_type: str = "person", connection_kind: str | None = None,
                  contacts: list[dict] | None = None, allow_duplicate: bool = False, cur=None) -> dict:
    """Create a person (or an organization) and connect them to THIS parish. A Gift Entry or Finance
    user may create a giver; membership editors and clergy may also create members. If someone with the
    same name already exists at this parish a Conflict is raised (with .details listing them) unless the
    caller confirms it is not a duplicate."""
    need_people(ctx, "people.create")
    record_type = check_enum(record_type, RECORD_TYPES, field="record type", allow_blank=False)
    kind = check_enum(connection_kind or ("member" if ctx.can("membership.edit") else "giver"),
                      CONNECTION_KINDS, field="connection", allow_blank=False)
    if kind == "member":
        ctx.require("membership.edit", "Only clergy and membership editors can add a member.")
    fields = clean_person_fields(data, record_type, creating=True)
    with tx(cur) as c:
        dups = local_duplicates(c, ctx, fields)
        if dups and not allow_duplicate:
            raise Conflict("Someone with that name is already at this parish.", details=[
                {"id": d["id"], "name": person_label(d)} for d in dups])
        cols = ["record_type", "created_by_user_id", "created_by_parish_id", "updated_by_user_id", "updated_by_parish_id"]
        vals: list = [record_type, ctx.user_id, ctx.parish_id, ctx.user_id, ctx.parish_id]
        for k, v in fields.items():
            cols.append(k)
            vals.append(v)
        c.execute(f"INSERT INTO donor.person ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id", vals)
        pid = c.fetchone()["id"]
        c.execute("INSERT INTO donor.parish_connection (person_id, parish_id, kind, created_by_user_id) "
                  "VALUES (%s,%s,%s,%s)", (pid, ctx.parish_id, kind, ctx.user_id))
        if contacts:
            _insert_contacts(c, ctx, pid, contacts)
        log_change(c, ctx, "person", pid, None, None, "Created", person_id=pid, kind="create")
        return {"id": pid, "person": _person_row(c, pid)}


# ── Update ──────────────────────────────────────────────────────────────────────────────────────
def person_update(ctx: Ctx, person_id: int, changes: dict, reason: str | None = None, *, cur=None) -> dict:
    """Edit the shared profile. Every changed field is logged (who, which parish, old, new) so the change is
    visible at every connected parish and can be undone one field at a time."""
    need_people(ctx, "people.edit")
    with tx(cur) as c:
        require_connection(c, ctx, person_id, include_archived=False)
        old = _person_row(c, person_id)
        fields = clean_person_fields({k: v for k, v in changes.items() if k in EDITABLE_FIELDS}, old["record_type"],
                                     creating=False)
        # Blank names on a person are only a problem if nothing is left.
        merged = {**old, **fields}
        if old["record_type"] == "person" and not (merged.get("first_name") or merged.get("last_name")):
            raise InvalidInput("A person needs at least a first or last name.", "last_name")
        if old["record_type"] == "organization" and not merged.get("org_name"):
            raise InvalidInput("An organization needs a name.", "org_name")
        bd, dd = merged.get("birth_date"), merged.get("deceased_date")
        if bd and dd and dd < bd:
            raise InvalidInput("The deceased date cannot be before the birth date.", "deceased_date")
        diffs = diff_fields(old, fields)
        if not diffs:
            return {"id": person_id, "changed": []}
        # School, grade, employer and occupation of a person under 18 are shown only to a role that may see a minor's
        # details, so only that role may change them (a crafted request cannot get around the hidden box).
        if any(k in MINOR_HIDDEN_FIELDS for k, _, _ in diffs) and not ctx.can("minors.details") and person_is_minor(c, old):
            raise PermissionDenied("Details about a person under 18 can be changed only by a role that may see them.")
        batch = new_batch_id()
        sets = ", ".join(f"{k} = %s" for k, _, _ in diffs)
        c.execute(f"UPDATE donor.person SET {sets}, updated_at = NOW(), updated_by_user_id = %s, updated_by_parish_id = %s "
                  "WHERE id = %s", (*[v for _, _, v in diffs], ctx.user_id, ctx.parish_id, person_id))
        for k, o, n in diffs:
            log_change(c, ctx, "person", person_id, k, o, n, person_id=person_id, batch_id=batch, reason=reason)
        return {"id": person_id, "changed": [k for k, _, _ in diffs]}


# ── Archive / restore (never delete) ────────────────────────────────────────────────────────────
def person_archive(ctx: Ctx, person_id: int, reason: str | None = None, *, cur=None) -> dict:
    """Archive THIS parish's connection to the person: they drop out of search and mailings here but their
    records (including gifts, and the right to receive a statement) stay (PF-09, RT-01). The shared profile
    is archived only when no parish is connected any more."""
    need_people(ctx, "people.edit")
    with tx(cur) as c:
        conn = require_connection(c, ctx, person_id, lock=True)
        if conn["archived_at"]:
            return {"id": person_id, "archived": True, "profile_archived": False, "changed": False}
        c.execute("UPDATE donor.parish_connection SET archived_at = NOW(), archived_by_user_id = %s, is_canonical = FALSE, "
                  "updated_at = NOW() WHERE id = %s", (ctx.user_id, conn["id"]))
        log_change(c, ctx, "parish_connection", conn["id"], "archived", None, "true", person_id=person_id,
                   kind="archive", scope="parish", reason=reason)
        c.execute("SELECT COUNT(*) AS n FROM donor.parish_connection WHERE person_id = %s AND archived_at IS NULL", (person_id,))
        profile_archived = c.fetchone()["n"] == 0
        if profile_archived:
            c.execute("UPDATE donor.person SET archived_at = NOW(), archived_by_user_id = %s, archive_reason = %s, "
                      "updated_at = NOW() WHERE id = %s AND archived_at IS NULL", (ctx.user_id, reason, person_id))
            log_change(c, ctx, "person", person_id, "archived", None, "true", person_id=person_id, kind="archive", reason=reason)
        return {"id": person_id, "archived": True, "profile_archived": profile_archived, "changed": True}


def person_restore(ctx: Ctx, person_id: int, *, cur=None) -> dict:
    need_people(ctx, "people.edit")
    with tx(cur) as c:
        conn = require_connection(c, ctx, person_id, lock=True)
        if not conn["archived_at"]:
            return {"id": person_id, "archived": False, "changed": False}
        c.execute("UPDATE donor.parish_connection SET archived_at = NULL, archived_by_user_id = NULL, updated_at = NOW() "
                  "WHERE id = %s", (conn["id"],))
        c.execute("UPDATE donor.person SET archived_at = NULL, archived_by_user_id = NULL, archive_reason = NULL, "
                  "updated_at = NOW() WHERE id = %s AND archived_at IS NOT NULL", (person_id,))
        log_change(c, ctx, "parish_connection", conn["id"], "archived", "true", None, person_id=person_id,
                   kind="restore", scope="parish")
        return {"id": person_id, "archived": False, "changed": True}


# ── Contacts ────────────────────────────────────────────────────────────────────────────────────
def _contact_value(kind: str, value) -> str:
    v = clean_email(value) if kind == "email" else clean_phone(value)
    if v is None:
        raise InvalidInput("Enter an email address or phone number.", "value")
    return v


def contact_add(ctx: Ctx, person_id: int, kind: str, value, subtype: str = "other", is_preferred: bool = False,
                *, cur=None) -> dict:
    need_people(ctx, "people.edit")
    kind = check_enum(kind, CONTACT_KINDS, field="contact type", allow_blank=False)
    subtype = check_enum(subtype or "other", CONTACT_SUBTYPES, field="contact subtype", allow_blank=False)
    value = _contact_value(kind, value)
    with tx(cur) as c:
        require_connection(c, ctx, person_id, include_archived=False)
        c.execute("SELECT id FROM donor.person_contact WHERE person_id = %s AND kind = %s AND LOWER(value) = LOWER(%s) "
                  "AND archived_at IS NULL", (person_id, kind, value))
        if c.fetchone():
            raise Conflict(f"That {kind} is already on this person.")
        c.execute("SELECT COUNT(*) AS n FROM donor.person_contact WHERE person_id = %s AND kind = %s AND archived_at IS NULL",
                  (person_id, kind))
        first_of_kind = c.fetchone()["n"] == 0
        preferred = bool(is_preferred) or first_of_kind
        if preferred and not first_of_kind:
            c.execute("UPDATE donor.person_contact SET is_preferred = FALSE WHERE person_id = %s AND kind = %s "
                      "AND is_preferred AND archived_at IS NULL RETURNING id, value", (person_id, kind))
            for old in c.fetchall():
                log_change(c, ctx, "person_contact", old["id"], "preferred", "true", "false", person_id=person_id)
        c.execute("INSERT INTO donor.person_contact (person_id, kind, subtype, value, digits, is_preferred, created_by_user_id) "
                  "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                  (person_id, kind, subtype, value, digits_of(value) if kind == "phone" else "", preferred, ctx.user_id))
        cid = c.fetchone()["id"]
        log_change(c, ctx, "person_contact", cid, "value", None, value, person_id=person_id, kind="create")
        _touch(c, ctx, person_id)
        return {"id": cid, "preferred": preferred}


def _contact_for_update(c, ctx: Ctx, contact_id: int) -> dict:
    c.execute("SELECT * FROM donor.person_contact WHERE id = %s AND archived_at IS NULL FOR UPDATE", (contact_id,))
    row = c.fetchone()
    if not row:
        raise NotFound("That contact detail was not found.")
    require_connection(c, ctx, row["person_id"], include_archived=False)      # no connection -> not found
    return row


def contact_update(ctx: Ctx, contact_id: int, *, value=None, subtype: str | None = None,
                   is_preferred: bool | None = None, cur=None) -> dict:
    need_people(ctx, "people.edit")
    with tx(cur) as c:
        row = _contact_for_update(c, ctx, contact_id)
        pid = row["person_id"]
        changed = []
        if value is not None and str(value).strip():
            new_value = _contact_value(row["kind"], value)
            if new_value.lower() != row["value"].lower() or new_value != row["value"]:
                c.execute("SELECT 1 AS x FROM donor.person_contact WHERE person_id = %s AND kind = %s AND "
                          "LOWER(value) = LOWER(%s) AND archived_at IS NULL AND id <> %s",
                          (pid, row["kind"], new_value, contact_id))
                if c.fetchone():
                    raise Conflict(f"That {row['kind']} is already on this person.")
                c.execute("UPDATE donor.person_contact SET value = %s, digits = %s WHERE id = %s",
                          (new_value, digits_of(new_value) if row["kind"] == "phone" else "", contact_id))
                log_change(c, ctx, "person_contact", contact_id, "value", row["value"], new_value, person_id=pid)
                changed.append("value")
        if subtype is not None:
            st = check_enum(subtype, CONTACT_SUBTYPES, field="contact subtype", allow_blank=False)
            if st != row["subtype"]:
                c.execute("UPDATE donor.person_contact SET subtype = %s WHERE id = %s", (st, contact_id))
                log_change(c, ctx, "person_contact", contact_id, "subtype", row["subtype"], st, person_id=pid)
                changed.append("subtype")
        if is_preferred is True and not row["is_preferred"]:
            c.execute("UPDATE donor.person_contact SET is_preferred = FALSE WHERE person_id = %s AND kind = %s AND is_preferred "
                      "AND archived_at IS NULL", (pid, row["kind"]))
            c.execute("UPDATE donor.person_contact SET is_preferred = TRUE WHERE id = %s", (contact_id,))
            log_change(c, ctx, "person_contact", contact_id, "preferred", "false", "true", person_id=pid)
            changed.append("preferred")
        elif is_preferred is False and row["is_preferred"]:
            raise InvalidInput("Pick a different preferred contact instead of clearing this one.")
        if changed:
            _touch(c, ctx, pid)
        return {"id": contact_id, "changed": changed}


def contact_archive(ctx: Ctx, contact_id: int, *, cur=None) -> dict:
    need_people(ctx, "people.edit")
    with tx(cur) as c:
        row = _contact_for_update(c, ctx, contact_id)
        pid = row["person_id"]
        c.execute("UPDATE donor.person_contact SET archived_at = NOW(), archived_by_user_id = %s, is_preferred = FALSE "
                  "WHERE id = %s", (ctx.user_id, contact_id))
        log_change(c, ctx, "person_contact", contact_id, "archived", None, "true", person_id=pid, kind="archive")
        if row["is_preferred"]:
            c.execute("SELECT id FROM donor.person_contact WHERE person_id = %s AND kind = %s AND archived_at IS NULL "
                      "ORDER BY id LIMIT 1", (pid, row["kind"]))
            nxt = c.fetchone()
            if nxt:
                c.execute("UPDATE donor.person_contact SET is_preferred = TRUE WHERE id = %s", (nxt["id"],))
                log_change(c, ctx, "person_contact", nxt["id"], "preferred", "false", "true", person_id=pid)
        _touch(c, ctx, pid)
        return {"id": contact_id, "archived": True}


# ── Linking a person another parish already has (one shared profile) ────────────────────────────
def find_profile_matches(ctx: Ctx, *, email: str | None = None, first_name: str | None = None,
                         last_name: str | None = None, birth_date=None) -> list[dict]:
    """Profiles OUTSIDE this parish that match what the caller already knows: an exact e-mail address, or an
    exact first name + last name + birth date. Only a masked preview comes back (first name, last initial,
    birth year) -- enough to recognize the person, not enough to learn anything else. Linking is done by
    person_link_existing, which re-checks the same proof."""
    need_people(ctx, "people.create")
    email = clean_email(email) if email else None
    fn, ln = clean_text(first_name, field="first name"), clean_text(last_name, field="last name")
    bd = parse_date(birth_date, field="birth date", allow_future=False)
    clauses, params = [], []
    if email:
        clauses.append("EXISTS (SELECT 1 FROM donor.person_contact c WHERE c.person_id = p.id AND c.kind = 'email' "
                       "AND c.archived_at IS NULL AND LOWER(c.value) = %s)")
        params.append(email)
    if fn and ln and bd:
        clauses.append("(LOWER(p.first_name) = LOWER(%s) AND LOWER(p.last_name) = LOWER(%s) AND p.birth_date = %s)")
        params += [fn, ln, bd]
    if not clauses:
        return []
    rows = db.query(
        "SELECT p.id, p.record_type, p.first_name, p.last_name, p.org_name, p.birth_date FROM donor.person p "
        "WHERE p.archived_at IS NULL AND NOT p.is_placeholder AND (" + " OR ".join(clauses) + ") "
        "AND NOT EXISTS (SELECT 1 FROM donor.parish_connection pc WHERE pc.person_id = p.id AND pc.parish_id = %s) "
        "ORDER BY p.id LIMIT 5", (*params, ctx.parish_id))
    out = []
    for r in rows:
        if r["record_type"] == "organization":
            preview = (r["org_name"] or "")[:1] + "***"
        else:
            preview = f"{(r['first_name'] or '')[:1]}*** {(r['last_name'] or '')[:1]}***"
        # The birth year is shown only when the caller typed the birth date themselves (they already know it). With an e-mail
        # alone it would tell them the birth year of whoever owns that address at another parish.
        out.append({"person_id": r["id"], "preview": preview.strip(),
                    "birth_year": r["birth_date"].year if (bd and r["birth_date"]) else None})
    return out


def person_link_existing(ctx: Ctx, person_id: int, proof: dict, connection_kind: str = "giver", *, cur=None) -> dict:
    """Connect THIS parish to a profile another parish already has, so the person keeps one profile. The
    caller must supply proof (the same e-mail, or name + birth date) which is checked again here: a bare
    person id is never enough. The new connection is never canonical, and none of the other parish's
    membership, sacraments, gifts, pledges or notes come with it."""
    need_people(ctx, "people.create")
    kind = check_enum(connection_kind, CONNECTION_KINDS, field="connection", allow_blank=False)
    if kind == "member":
        ctx.require("membership.edit", "Only clergy and membership editors can add a member.")
    matches = find_profile_matches(ctx, email=proof.get("email"), first_name=proof.get("first_name"),
                                   last_name=proof.get("last_name"), birth_date=proof.get("birth_date"))
    if person_id not in {m["person_id"] for m in matches}:
        raise NotFound("That profile could not be matched with what you entered.")
    with tx(cur) as c:
        c.execute("INSERT INTO donor.parish_connection (person_id, parish_id, kind, created_by_user_id) "
                  "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id", (person_id, ctx.parish_id, kind, ctx.user_id))
        row = c.fetchone()
        if not row:
            raise Conflict("This person is already connected to this parish.")
        c.execute("UPDATE donor.person SET archived_at = NULL, archived_by_user_id = NULL, archive_reason = NULL "
                  "WHERE id = %s AND archived_at IS NOT NULL", (person_id,))
        log_change(c, ctx, "parish_connection", row["id"], None, None, kind, person_id=person_id, kind="link", scope="parish")
        return {"id": person_id, "connection_id": row["id"]}


# ── Placeholder donors ──────────────────────────────────────────────────────────────────────────
_PLACEHOLDER_NAMES = {"open_plate": "Open Plate", "anonymous": "Anonymous Contributor"}


def placeholder_ensure(ctx: Ctx, kind: str, *, cur=None) -> int:
    """The parish's own placeholder donor ("Open Plate" for loose cash, "Anonymous Contributor"). Created the
    first time it is needed. Placeholders are left out of the directory and donor counts and never get
    statements (BT-06)."""
    need_people(ctx, "people.create")
    kind = check_enum(kind, PLACEHOLDER_KINDS, field="placeholder", allow_blank=False)
    with tx(cur) as c:
        c.execute("SELECT p.id FROM donor.person p JOIN donor.parish_connection pc ON pc.person_id = p.id "
                  "AND pc.parish_id = %s WHERE p.is_placeholder AND p.placeholder_kind = %s ORDER BY p.id LIMIT 1",
                  (ctx.parish_id, kind))
        row = c.fetchone()
        if row:
            return row["id"]
        c.execute("INSERT INTO donor.person (record_type, last_name, is_placeholder, placeholder_kind, in_directory, "
                  "created_by_user_id, created_by_parish_id, updated_by_user_id, updated_by_parish_id) "
                  "VALUES ('person', %s, TRUE, %s, FALSE, %s, %s, %s, %s) RETURNING id",
                  (_PLACEHOLDER_NAMES[kind], kind, ctx.user_id, ctx.parish_id, ctx.user_id, ctx.parish_id))
        pid = c.fetchone()["id"]
        c.execute("INSERT INTO donor.parish_connection (person_id, parish_id, kind, statement_option, created_by_user_id) "
                  "VALUES (%s,%s,'giver','none',%s)", (pid, ctx.parish_id, ctx.user_id))
        log_change(c, ctx, "person", pid, None, None, "Created", person_id=pid, kind="create")
        return pid


# ── Directory and export ────────────────────────────────────────────────────────────────────────
def _need_minor_access(ctx: Ctx, include_minors: bool) -> None:
    if include_minors and not ctx.can("minors.details"):
        raise PermissionDenied("Only clergy, membership editors and parish admins can include people under 18.")


def directory_list(ctx: Ctx, *, include_minors: bool = False) -> list[dict]:
    """The parish directory: active members who are in the directory, not deceased, not placeholders, and (by
    default) not under 18. Phone numbers are left out when the person asked for that."""
    need_people(ctx, "people.view")
    _need_minor_access(ctx, include_minors)
    rows = _search_rows(ctx, "", directory_only=True, include_minors=include_minors, limit=None)
    for r in rows:
        r.pop("total_rows", None)
        if r["hide_phone_in_directory"]:
            r["phone"] = None
            r["home_phone"] = None
    return rows


EXPORT_COLUMNS = (
    ("id", "Person ID"), ("title", "Title"), ("first_name", "First name"), ("middle_name", "Middle name"),
    ("last_name", "Last name"), ("suffix", "Suffix"), ("goes_by", "Goes by"), ("org_name", "Organization"),
    ("household_name", "Household"), ("address1", "Address 1"), ("address2", "Address 2"), ("city", "City"),
    ("state", "State"), ("postal_code", "Zip"), ("email", "Email"), ("phone", "Phone"),
    ("connection_kind", "Connection"), ("status_label", "Member status"), ("envelope_number", "Envelope"),
    ("do_not_mail", "Do not mail"), ("do_not_call", "Do not call"), ("do_not_email", "Do not email"),
)


def people_export(ctx: Ctx, *, kind: str = "roll", include_minors: bool = False) -> str:
    """CSV text. kind='roll' lists everyone connected here (live connections), kind='directory' applies the
    directory rules. People under 18 are left out unless a role with minors.details asks for them (PF-16)."""
    need_people(ctx, "people.view")
    _need_minor_access(ctx, include_minors)
    if kind not in ("roll", "directory"):
        raise InvalidInput("Export kind must be 'roll' or 'directory'.")
    rows = _search_rows(ctx, "", directory_only=(kind == "directory"), include_minors=include_minors, limit=None)
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow([h for _, h in EXPORT_COLUMNS])
    for r in rows:
        if kind == "directory" and r["hide_phone_in_directory"]:
            r["phone"] = None
        w.writerow([csv_safe("Yes" if r[k] is True else "No" if r[k] is False else ("" if r.get(k) is None else r.get(k)))
                    for k, _ in EXPORT_COLUMNS])
    return out.getvalue()


def csv_safe(v):
    """A text cell that starts with = + - @ (or a tab or carriage return) is read by Excel as a formula. Names and
    notes are typed by people, so such a cell is prefixed with an apostrophe and stays plain text."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v
