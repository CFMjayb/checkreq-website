"""
donor_portal.py -- Beacon Donor Management: what a signed-in parishioner sees and may change about THEMSELVES.

The parishioner screen is the person screen cut down to one person (Plan, Revision 2). Every function here takes `ps`, the portal
session as donor_portal_login.current() read it from its database row: {person_id, parish_id, ...}. The person and the parish come
from that row and from nowhere else; a function never takes a person id or a parish id from the browser.

READ (explicit column lists only, never SELECT * into a template):
  personal_view      the person's own details, their live emails and phones, their household's address (shown read only)
  contributions      their own gifts at THIS parish: posted gifts in CLOSED batches only, never an open batch, never anything
                     staff wrote (memo, correction reasons, notes). A reversal or a return shows as the correction it is. Soft credits
                     and a spouse's gifts are never in the list.
WRITE (the only things a parishioner may change directly, each one logged to donor.change_log):
  personal_save      goes-by name, occupation, employer, school, grade, the five privacy flags, and their own emails and phones
                     (add, change, make preferred, remove). Name, gender, marital status, birth and wedding dates, household and
                     address are staff-owned and shown read only.
  message_create     the Get Help form: a message to the parish office (a staff-visible record, see messages_waiting)

Every change is written by an ACTOR Ctx with user_id 0, no roles and no capabilities (actor()), reason "Parishioner self-service".
A staff Ctx is never reused and no staff service is called: the staff services check capabilities this actor does not have, and
nothing here gives it any.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from donor_core import (
    Conflict, Ctx, GRADES, CONTACT_SUBTYPES, InvalidInput, NotFound, check_enum, clean_email, clean_phone, clean_text, digits_of,
    diff_fields, initials, label, log_change, make_ctx, need_people, new_batch_id, person_label, tx,
)
from donor_people import FIELD_LABELS

SELF_REASON = "Parishioner self-service"
ACTOR_LABEL = "Parishioner (self-service)"

# What a parishioner may change directly. Everything else on the screen is read only.
SELF_TEXT_FIELDS = {"goes_by": 80, "occupation": 100, "employer": 120, "school": 120}
SELF_BOOL_FIELDS = ("in_directory", "hide_phone_in_directory", "do_not_mail", "do_not_call", "do_not_email")
SELF_EDITABLE = tuple(SELF_TEXT_FIELDS) + ("grade",) + SELF_BOOL_FIELDS     # the field names the shared person screen leaves editable for a parishioner
MAX_CONTACTS_PER_KIND = 6
EMAIL_TAKEN = "That email address can't be added here. Please use Get Help and the parish office will help."
SIGNIN_EMAIL_LOCKED = "That is the email address your sign-in code goes to, so it can't be changed or removed here. Please use Get Help and the parish office will help."

PERSON_COLUMNS = ("title", "first_name", "middle_name", "last_name", "suffix", "goes_by", "alt_name", "former_name", "gender",
                  "marital_status", "birth_date", "wedding_date", "occupation", "employer", "school", "grade", "in_directory",
                  "hide_phone_in_directory", "do_not_mail", "do_not_call", "do_not_email", "created_at")
HOUSEHOLD_COLUMNS = ("address1", "address2", "city", "state", "postal_code", "country", "home_phone", "mail_address1",
                     "mail_address2", "mail_city", "mail_state", "mail_postal_code")


def actor(parish_id: int) -> Ctx:
    """The Ctx a self-service change is logged under: user 0, no roles, no capabilities."""
    return make_ctx(0, parish_id, (), user_label=ACTOR_LABEL)


# ── Read: the person's own details ──────────────────────────────────────────────────────────────
def personal_view(ps: dict) -> dict:
    pid, parish_id = ps["person_id"], ps["parish_id"]
    with tx() as c:
        c.execute(f"SELECT {', '.join(PERSON_COLUMNS)} FROM donor.person WHERE id = %s", (pid,))
        person = c.fetchone()
        if not person:
            raise NotFound("Your record was not found.")
        c.execute("SELECT id, kind, subtype, value, is_preferred FROM donor.person_contact WHERE person_id = %s AND archived_at IS NULL "
                  "ORDER BY kind, is_preferred DESC, id", (pid,))
        contacts = c.fetchall()
        c.execute(f"SELECT {', '.join('h.' + k for k in HOUSEHOLD_COLUMNS)} FROM donor.household_member hm "
                  "JOIN donor.household h ON h.id = hm.household_id WHERE hm.person_id = %s AND hm.left_at IS NULL", (pid,))
        household = c.fetchone()
        c.execute("SELECT kind, envelope_number FROM donor.parish_connection WHERE person_id = %s AND parish_id = %s AND archived_at IS NULL", (pid, parish_id))
        conn = c.fetchone() or {}
        c.execute("SELECT login_email FROM donor.parishioner_login WHERE person_id = %s AND parish_id = %s AND is_enabled", (pid, parish_id))
        lg = c.fetchone()
    return {"p": person, "contacts": contacts, "household": household, "envelope": conn.get("envelope_number"), "kind": conn.get("kind"),
            "signin_email": lg["login_email"] if lg else None}


def self_context(ps: dict) -> dict:
    """The context the parishioner's pages give the SHARED person screen (templates/donor_person.html and its tabs). It has the same keys
    donor_routes_people gives a staff page (g, p, title_name, initials, ...), built from personal_view's explicit column lists, so the
    shared templates draw the same layout for both; everything staff-only is simply absent (no household members, no other parishes, no
    spouse, no membership, no notes), and the parishioner's own Ctx is the actor, which holds no capability, so every staff action
    stays hidden. See the template header of donor_person.html for the switches."""
    v = personal_view(ps)
    person = dict(v["p"])
    person.update({"full_name": " ".join(x for x in (person.get("title"), person.get("first_name"), person.get("middle_name"),
                                                      person.get("last_name"), person.get("suffix")) if x),
                   "record_type": "person", "is_placeholder": False, "deceased_date": None, "org_name": None, "updated_at": None})
    hh = dict(v["household"]) if v["household"] else None
    if hh is not None:
        hh["members"] = []                                         # a parishioner is never shown the other people in the household
    g = {"person": person, "redacted": False, "archived": False, "age": None, "is_minor": False, "household": hh, "household_position": None,
         "connection": {"kind": v["kind"] or "member", "envelope_number": v["envelope"], "is_canonical": False, "connected_at": None},
         "membership": None, "contacts": v["contacts"], "spouse": None, "other_connections": []}
    return {"g": g, "p": person, "title_name": person["full_name"] or "You", "initials": initials(person), "signin_email": v["signin_email"],
            "layout": "donor_portal_base.html", "self_mode": True, "self_editable": SELF_EDITABLE, "ctx": actor(ps["parish_id"]),
            "parish": {"name": ps["parish_name"]}, "pid": None, "other_names": {}}


# ── Write: the person's own details ─────────────────────────────────────────────────────────────
def _email_taken_by_other(c, person_id: int, email_lower: str) -> bool:
    """Is this address an active email on someone else (other than the person's own current spouse)? Adding it would put two
    unrelated people on one address, and the sign-in rule would then refuse to sign either of them in."""
    c.execute("SELECT DISTINCT person_id FROM donor.person_contact WHERE kind = 'email' AND archived_at IS NULL "
              "AND LOWER(value) = %s AND person_id <> %s", (email_lower, person_id))
    others = {r["person_id"] for r in c.fetchall()}
    if not others:
        return False
    c.execute("SELECT spouse_id FROM donor.spouse_link WHERE person_id = %s AND ended_at IS NULL", (person_id,))
    sp = c.fetchone()
    return bool(others - ({sp["spouse_id"]} if sp else set()))


def _clean_contact(kind: str, value) -> str:
    v = clean_email(value, field="email") if kind == "email" else clean_phone(value, field="phone")
    if v is None:
        raise InvalidInput("Enter an email address or phone number.", "value")
    return v


def _blank(v) -> bool:
    return v is None or str(v).strip() == ""


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def personal_save(ps: dict, form) -> dict:
    """Apply the whole Personal tab in one transaction. `form` has get(), getlist() and `in` (a Starlette FormData). Only the fields
    in SELF_TEXT_FIELDS, grade, the five privacy flags and the person's OWN contacts are read: anything else posted is ignored.
    Returns {"changed": [what changed, in words]}. Raises (and rolls everything back) if anything is refused."""
    pid, parish_id = ps["person_id"], ps["parish_id"]
    a = actor(parish_id)
    changed: list[str] = []
    with tx() as c:
        c.execute("SELECT * FROM donor.person WHERE id = %s FOR UPDATE", (pid,))
        old = c.fetchone()
        if not old:
            raise NotFound("Your record was not found.")

        # 1. Their own profile fields.
        new: dict = {}
        for k, limit in SELF_TEXT_FIELDS.items():
            if k in form:
                new[k] = clean_text(form.get(k), field=FIELD_LABELS[k], max_len=limit)
        if "grade" in form:
            new["grade"] = check_enum(form.get("grade"), GRADES, field=FIELD_LABELS["grade"])
        if "has_privacy" in form:
            for k in SELF_BOOL_FIELDS:
                new[k] = form.get(k) is not None                      # an unticked box is simply absent from the post
        diffs = diff_fields(old, new)
        touched = False
        if diffs:
            sets = ", ".join(f"{k} = %s" for k, _, _ in diffs)
            c.execute(f"UPDATE donor.person SET {sets} WHERE id = %s", (*[v for _, _, v in diffs], pid))
            batch = new_batch_id()
            for k, o, n in diffs:
                log_change(c, a, "person", pid, k, o, n, person_id=pid, batch_id=batch, reason=SELF_REASON)
            changed += [FIELD_LABELS.get(k, k) for k, _, _ in diffs]
            touched = True

        # 2. Their own emails and phones. Every posted id must be one of THIS person's live contacts: a crafted id never reaches anyone else.
        c.execute("SELECT id, kind, subtype, value, is_preferred FROM donor.person_contact WHERE person_id = %s AND archived_at IS NULL", (pid,))
        mine = {row["id"]: row for row in c.fetchall()}
        before_pref = {k: next((i for i, r in mine.items() if r["kind"] == k and r["is_preferred"]), None) for k in ("email", "phone")}
        posted_ids = [_int(x) for x in form.getlist("contact_ids")]
        if any(i is None or i not in mine for i in posted_ids):
            raise NotFound("That contact detail was not found.")
        removing = {i for i in posted_ids if f"c_{i}_remove" in form}
        # The email their sign-in code goes to (the parishioner login staff turned on) is changed or removed only by staff: a member who
        # changed it in a session would lock themselves out, and a stranger at an unlocked screen could redirect the code.
        c.execute("SELECT login_email FROM donor.parishioner_login WHERE person_id = %s AND parish_id = %s AND is_enabled", (pid, parish_id))
        lrow = c.fetchone()
        locked = {i for i, r in mine.items() if lrow and r["kind"] == "email" and r["value"].lower() == lrow["login_email"]}
        if removing & locked:
            raise InvalidInput(SIGNIN_EMAIL_LOCKED, "email")
        for i in posted_ids:
            if i in removing:
                continue
            row = mine[i]
            value, subtype = form.get(f"c_{i}_value"), form.get(f"c_{i}_subtype")
            if not _blank(value) and str(value).strip() != row["value"]:     # only a value that really was edited (an untouched box is not "cleaned")
                nv = _clean_contact(row["kind"], value)
                if nv != row["value"]:
                    if i in locked:
                        raise InvalidInput(SIGNIN_EMAIL_LOCKED, "email")
                    c.execute("SELECT 1 AS x FROM donor.person_contact WHERE person_id = %s AND kind = %s AND LOWER(value) = LOWER(%s) "
                              "AND archived_at IS NULL AND id <> %s", (pid, row["kind"], nv, i))
                    if c.fetchone():
                        raise Conflict(f"That {row['kind']} is already on your record.")
                    if row["kind"] == "email" and nv.lower() != row["value"].lower() and _email_taken_by_other(c, pid, nv.lower()):
                        raise InvalidInput(EMAIL_TAKEN, "email")
                    c.execute("UPDATE donor.person_contact SET value = %s, digits = %s WHERE id = %s",
                              (nv, digits_of(nv) if row["kind"] == "phone" else "", i))
                    log_change(c, a, "person_contact", i, "value", row["value"], nv, person_id=pid, reason=SELF_REASON)
                    changed.append("Email" if row["kind"] == "email" else "Phone")
                    touched = True
            if row["kind"] == "phone" and not _blank(subtype):
                st = check_enum(subtype, CONTACT_SUBTYPES, field="phone kind", allow_blank=False)
                if st != row["subtype"]:
                    c.execute("UPDATE donor.person_contact SET subtype = %s WHERE id = %s", (st, i))
                    log_change(c, a, "person_contact", i, "subtype", row["subtype"], st, person_id=pid, reason=SELF_REASON)
                    changed.append("Phone kind")
                    touched = True
        for i in sorted(removing):
            c.execute("UPDATE donor.person_contact SET archived_at = NOW(), archived_by_user_id = 0, is_preferred = FALSE WHERE id = %s", (i,))
            log_change(c, a, "person_contact", i, "archived", None, "true", person_id=pid, kind="archive", reason=SELF_REASON)
            changed.append("Removed " + mine[i]["kind"])
            touched = True
        kinds, values, subtypes = form.getlist("new_kind"), form.getlist("new_value"), form.getlist("new_subtype")
        for n, value in enumerate(values):
            if _blank(value):
                continue
            kind = check_enum(kinds[n] if n < len(kinds) else "email", ("email", "phone"), field="contact type", allow_blank=False)
            val = _clean_contact(kind, value)
            subtype = check_enum((subtypes[n] if n < len(subtypes) else "") or "other", CONTACT_SUBTYPES, field="phone kind", allow_blank=False) if kind == "phone" else "other"
            c.execute("SELECT 1 AS x FROM donor.person_contact WHERE person_id = %s AND kind = %s AND LOWER(value) = LOWER(%s) AND archived_at IS NULL", (pid, kind, val))
            if c.fetchone():
                raise Conflict(f"That {kind} is already on your record.")
            if kind == "email" and _email_taken_by_other(c, pid, val.lower()):
                raise InvalidInput(EMAIL_TAKEN, "email")
            c.execute("SELECT COUNT(*) AS n FROM donor.person_contact WHERE person_id = %s AND kind = %s AND archived_at IS NULL", (pid, kind))
            if c.fetchone()["n"] >= MAX_CONTACTS_PER_KIND:
                raise InvalidInput(f"You can keep up to {MAX_CONTACTS_PER_KIND} {kind}s on file. Remove one first, or use Get Help.", "value")
            c.execute("INSERT INTO donor.person_contact (person_id, kind, subtype, value, digits, is_preferred, created_by_user_id) "
                      "VALUES (%s,%s,%s,%s,%s,FALSE,0) RETURNING id", (pid, kind, subtype, val, digits_of(val) if kind == "phone" else ""))
            log_change(c, a, "person_contact", c.fetchone()["id"], "value", None, val, person_id=pid, kind="create", reason=SELF_REASON)
            changed.append("Added " + kind)
            touched = True
        # Preferred: the radio they chose (when it is theirs and still live), then every kind with contacts has exactly one.
        for kind in ("email", "phone"):
            want = _int(form.get(f"preferred_{kind}"))
            if want is not None and want in mine and want not in removing and mine[want]["kind"] == kind:
                c.execute("UPDATE donor.person_contact SET is_preferred = FALSE WHERE person_id = %s AND kind = %s AND is_preferred AND archived_at IS NULL", (pid, kind))
                c.execute("UPDATE donor.person_contact SET is_preferred = TRUE WHERE id = %s", (want,))
            c.execute("SELECT id, is_preferred FROM donor.person_contact WHERE person_id = %s AND kind = %s AND archived_at IS NULL ORDER BY id", (pid, kind))
            rows = c.fetchall()
            if rows and not any(r["is_preferred"] for r in rows):
                c.execute("UPDATE donor.person_contact SET is_preferred = TRUE WHERE id = %s", (rows[0]["id"],))
                rows[0]["is_preferred"] = True
            after = next((r["id"] for r in rows if r["is_preferred"]), None)
            if after != before_pref[kind] and after is not None:
                if before_pref[kind] is not None and before_pref[kind] not in removing:
                    log_change(c, a, "person_contact", before_pref[kind], "preferred", "true", "false", person_id=pid, reason=SELF_REASON)
                log_change(c, a, "person_contact", after, "preferred", "false", "true", person_id=pid, reason=SELF_REASON)
                if f"Preferred {kind}" not in changed and not any(x.startswith("Added") for x in changed):
                    changed.append(f"Preferred {kind}")
                touched = True
        # They keep at least one email address: it is how they sign in. Taking the last one away is refused (and rolled back).
        c.execute("SELECT COUNT(*) AS n FROM donor.person_contact WHERE person_id = %s AND kind = 'email' AND archived_at IS NULL", (pid,))
        if c.fetchone()["n"] == 0 and mine and any(r["kind"] == "email" for r in mine.values()):
            raise InvalidInput("Keep at least one email address on file: it is how you sign in. To remove your last one, use Get Help.", "email")
        if touched:
            c.execute("UPDATE donor.person SET updated_at = NOW(), updated_by_user_id = 0, updated_by_parish_id = %s WHERE id = %s", (parish_id, pid))
    return {"changed": changed}


# ── Read: their own giving ──────────────────────────────────────────────────────────────────────
_GIFT_JOIN = (
    "  FROM donor.gift g "
    "  JOIN donor.batch b ON b.id = g.batch_id AND b.parish_id = g.parish_id AND b.status IN ('closed', 'reconciled') "
    "  JOIN donor.gift_split gs ON gs.gift_id = g.id "
    "  JOIN donor.fund f ON f.id = gs.fund_id AND f.parish_id = g.parish_id "
    " WHERE g.parish_id = %s AND g.person_id = %s AND g.status <> 'voided' AND g.gift_type <> 'non_gift_receipt' "
)


def contributions(ps: dict, year: int | None = None, *, today: dt.date | None = None) -> dict:
    """The person's OWN gifts at this parish, newest first: posted gifts in closed (or reconciled) batches. `year` None means every
    year. Reversals and returns are listed as the corrections they are (a negative row, and the original marked). No memo, no
    correction reason, no gift id leaves this function."""
    today = today or dt.date.today()
    pid, parish_id = ps["person_id"], ps["parish_id"]
    flt, extra = ("", ()) if year is None else (" AND EXTRACT(YEAR FROM g.gift_date) = %s", (int(year),))
    with tx() as c:
        c.execute("SELECT DISTINCT EXTRACT(YEAR FROM g.gift_date)::int AS y" + _GIFT_JOIN + "ORDER BY y DESC", (parish_id, pid))
        years = [r["y"] for r in c.fetchall()]
        c.execute(
            "SELECT g.gift_date, g.gift_type, g.status, g.check_number, "
            "       (g.reverses_gift_id IS NOT NULL OR g.reclass_of_gift_id IS NOT NULL OR g.replaces_gift_id IS NOT NULL) AS is_correction, "
            "       COALESCE(NULLIF(f.statement_name, ''), f.name) AS fund_name, gs.amount" + _GIFT_JOIN + flt +
            " ORDER BY g.gift_date DESC, g.id DESC, gs.id LIMIT 500", (parish_id, pid, *extra))
        rows = c.fetchall()
        c.execute("SELECT COALESCE(NULLIF(f.statement_name, ''), f.name) AS fund_name, SUM(gs.amount) AS total" + _GIFT_JOIN + flt +
                  " GROUP BY 1 ORDER BY 1", (parish_id, pid, *extra))
        by_fund = c.fetchall()
        c.execute("SELECT envelope_number FROM donor.parish_connection WHERE person_id = %s AND parish_id = %s AND archived_at IS NULL", (pid, parish_id))
        env = (c.fetchone() or {}).get("envelope_number")
    for r in rows:
        r["note"] = "Correction" if r.pop("is_correction") else ""
        if r["status"] in ("reversed", "returned"):
            r["note"] = label(r["status"])
    total = sum((Decimal(r["total"]) for r in by_fund), Decimal("0.00"))
    return {"year": year, "years": years, "rows": rows, "by_fund": by_fund, "total": total, "envelope": env, "truncated": len(rows) >= 500}


def giving_view(ps: dict, year: int | None = None, *, today: dt.date | None = None) -> dict:
    """contributions() in the shape the SHARED person screen's Giving tab (templates/donor_tab_giving.html) reads for staff: year_total,
    deductible_total, last, rows, soft_credits and no pledge, plus the parishioner-only parts (the year picker's years, the summary by
    fund, an envelope number, the truncated flag). Still only this person's own posted gifts in closed batches: nothing is added to
    what contributions() already limits (no soft credit, no spouse's gift, no batch number or id)."""
    c = contributions(ps, year, today=today)
    rows = c["rows"]
    deductible = sum((Decimal(r["amount"]) for r in rows if r["gift_type"] == "tax_deductible"), Decimal("0.00"))
    last = next(({"gift_date": r["gift_date"], "amount": r["amount"]} for r in rows if r["status"] not in ("reversed", "returned") and Decimal(r["amount"]) > 0), None)
    return {"year": c["year"], "years": c["years"], "year_total": c["total"], "deductible_total": deductible, "joint_with": None, "pledge": None,
            "last": last, "rows": rows, "soft_credits": [], "by_fund": c["by_fund"], "envelope": c["envelope"], "truncated": c["truncated"]}


# ── Get Help: a message to the parish office ────────────────────────────────────────────────────
MAX_MESSAGES_PER_HOUR = 5


def message_create(ps: dict, subject, body) -> dict:
    text = clean_text(body, field="message", max_len=2000)
    if not text:
        raise InvalidInput("Write your message first.", "body")
    subj = clean_text(subject, field="subject", max_len=120)
    with tx() as c:
        c.execute("SELECT COUNT(*) AS n FROM donor.parishioner_message WHERE person_id = %s AND parish_id = %s AND created_at > NOW() - INTERVAL '1 hour'",
                  (ps["person_id"], ps["parish_id"]))
        if c.fetchone()["n"] >= MAX_MESSAGES_PER_HOUR:
            raise InvalidInput("You have sent several messages in the last hour. Please wait a little, or call the parish office.", "body")
        c.execute("INSERT INTO donor.parishioner_message (parish_id, person_id, subject, body) VALUES (%s,%s,%s,%s) RETURNING id",
                  (ps["parish_id"], ps["person_id"], subj, text))
        return {"id": c.fetchone()["id"]}


def messages_for_person(ps: dict, limit: int = 10) -> list[dict]:
    with tx() as c:
        c.execute("SELECT subject, status, created_at FROM donor.parishioner_message WHERE person_id = %s AND parish_id = %s "
                  "ORDER BY created_at DESC, id DESC LIMIT %s", (ps["person_id"], ps["parish_id"], limit))
        return c.fetchall()


# ── Staff side: messages waiting at this parish ─────────────────────────────────────────────────
def messages_waiting(ctx: Ctx, limit: int = 50) -> list[dict]:
    """New messages from parishioners at THIS parish, oldest first. People editors (Parish Admin, clergy, membership editor)."""
    need_people(ctx, "people.edit", "Only people editors can read messages from parishioners.")
    with tx() as c:
        c.execute("SELECT m.id, m.person_id, m.subject, m.body, m.created_at, p.first_name, p.last_name, p.goes_by, p.org_name, p.record_type "
                  "FROM donor.parishioner_message m JOIN donor.person p ON p.id = m.person_id "
                  "WHERE m.parish_id = %s AND m.status = 'new' ORDER BY m.created_at, m.id LIMIT %s", (ctx.parish_id, limit))
        rows = c.fetchall()
    for r in rows:
        r["name"] = person_label(r)
    return rows


def messages_waiting_count(parish_id: int) -> int:
    import db
    row = db.query_one("SELECT COUNT(*) AS n FROM donor.parishioner_message WHERE parish_id = %s AND status = 'new'", (parish_id,))
    return row["n"] if row else 0


def message_done(ctx: Ctx, message_id: int, *, cur=None) -> dict:
    need_people(ctx, "people.edit", "Only people editors can answer messages from parishioners.")
    with tx(cur) as c:
        c.execute("UPDATE donor.parishioner_message SET status = 'done', handled_by_user_id = %s, handled_at = NOW() "
                  "WHERE id = %s AND parish_id = %s AND status = 'new' RETURNING id", (ctx.user_id, message_id, ctx.parish_id))
        if not c.fetchone():
            raise NotFound("That message was not found, or it is already marked done.")
        return {"id": message_id}
