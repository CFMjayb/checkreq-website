"""
donor_personal.py -- Beacon Donor Management: the Personal tab's ONE save.

The person screen is read as a set of compact fields and, after one Edit button, the SAME fields become editable
in place (like the Ministry Connect people screen). So there is one form and one Save, and this module is its
service: it takes everything on the form and applies it in a single transaction, through the existing services,
which keep every rule (who may edit what, what is logged, what is refused):

    profile        donor_people.person_update            (name, dates, gender, privacy check boxes)
    contacts       donor_people.contact_update/_archive/_add   (change a value or kind, make preferred, remove, add)
    household      donor_households.household_update/_create   (address, phone, mailing address)
    this parish    donor_households.parish_connection_set      (connection, envelope, statement, canonical)

Nothing is written unless every part is valid: a refusal anywhere rolls the whole save back and the caller shows
the reason. Only values that really changed are handed on, so saving an unchanged screen writes and logs nothing and
a role that may not make members (see parish_connection_set) is never refused for a value it did not change.

Form field names (what templates/donor_tab_personal.html posts)
    profile fields as donor_people.EDITABLE_FIELDS, plus has_privacy=1 (so an unticked box means "no")
    contact_ids (repeated) and, per id: c_<id>_value, c_<id>_subtype, c_<id>_remove; preferred_email, preferred_phone
    new_kind / new_value / new_subtype (parallel lists; a row with no value is ignored)
    hh_<household field> for each of donor_households.HOUSEHOLD_FIELDS
    cn_kind, cn_envelope_number, cn_statement_option, cn_statement_delivery, cn_has_canonical + cn_is_canonical
"""
from __future__ import annotations

import donor_households as H
import donor_people as P
from donor_core import Ctx, InvalidInput, NotFound, tx

_CONN_KEYS = ("kind", "envelope_number", "statement_option", "statement_delivery")


def _blank(v) -> bool:
    return v is None or str(v).strip() == ""


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def personal_save(ctx: Ctx, person_id: int, form) -> dict:
    """Apply the whole Personal tab. `form` is a mapping with get(), getlist() and `in` (a Starlette FormData or a
    test double). Returns {"changed": [what changed, in words]}; an empty list means nothing needed saving."""
    changed: list[str] = []
    with tx() as c:
        P.require_connection(c, ctx, person_id, include_archived=False)

        # 1. The shared profile.
        changes: dict = {}
        for f in P.EDITABLE_FIELDS:
            if f in P.BOOL_FIELDS:
                if "has_privacy" in form:
                    changes[f] = form.get(f) is not None            # an unticked box is simply absent from the post
            elif f in form:
                changes[f] = form.get(f)
        r = P.person_update(ctx, person_id, changes, cur=c)
        changed += [P.FIELD_LABELS.get(k, k) for k in r["changed"]]

        # 2. Contacts. Every posted id must be one of THIS person's live contacts (a crafted id never reaches anyone else).
        c.execute("SELECT id, kind, subtype, value, is_preferred FROM donor.person_contact "
                  "WHERE person_id = %s AND archived_at IS NULL", (person_id,))
        mine = {row["id"]: row for row in c.fetchall()}
        posted_ids = [_int(x) for x in form.getlist("contact_ids")]
        if any(i is None or i not in mine for i in posted_ids):
            raise NotFound("That contact detail was not found.")
        removing = {i for i in posted_ids if f"c_{i}_remove" in form}
        for i in posted_ids:
            if i in removing:
                continue
            row = mine[i]
            value, subtype = form.get(f"c_{i}_value"), form.get(f"c_{i}_subtype")
            kw: dict = {}
            if not _blank(value) and str(value).strip() != row["value"]:
                kw["value"] = value
            if row["kind"] == "phone" and not _blank(subtype) and subtype != row["subtype"]:
                kw["subtype"] = subtype
            if kw:
                P.contact_update(ctx, i, cur=c, **kw)
                changed.append("Email" if row["kind"] == "email" else "Phone")
        for kind in ("email", "phone"):
            pref = _int(form.get(f"preferred_{kind}"))
            if pref is not None and pref in mine and pref not in removing and mine[pref]["kind"] == kind and not mine[pref]["is_preferred"]:
                P.contact_update(ctx, pref, is_preferred=True, cur=c)
                changed.append(f"Preferred {kind}")
        for i in sorted(removing):
            P.contact_archive(ctx, i, cur=c)
            changed.append("Removed " + mine[i]["kind"])
        kinds, values, subtypes = form.getlist("new_kind"), form.getlist("new_value"), form.getlist("new_subtype")
        for n, value in enumerate(values):
            if _blank(value):
                continue
            kind = kinds[n] if n < len(kinds) else "email"
            subtype = subtypes[n] if n < len(subtypes) else "other"
            P.contact_add(ctx, person_id, kind, value, subtype if kind == "phone" else "other", False, cur=c)
            changed.append("Added " + (kind or "contact"))

        # 3. The household (address, phones). A person with no household gets one only if something was typed.
        hh_in = {f: form.get("hh_" + f) for f in H.HOUSEHOLD_FIELDS if ("hh_" + f) in form}
        if hh_in:
            c.execute("SELECT household_id FROM donor.household_member WHERE person_id = %s AND left_at IS NULL", (person_id,))
            row = c.fetchone()
            if row:
                r = H.household_update(ctx, row["household_id"], hh_in, cur=c)
                changed += [H.HOUSEHOLD_LABELS.get(k, k) for k in r["changed"]]
            elif any(not _blank(v) for v in hh_in.values()):
                H.household_create(ctx, hh_in, [{"person_id": person_id, "position": "primary_adult", "is_primary_contact": True}], cur=c)
                changed.append("Household created")

        # 4. This parish's connection: only what really changed (see the module note).
        c.execute("SELECT * FROM donor.parish_connection WHERE person_id = %s AND parish_id = %s", (person_id, ctx.parish_id))
        conn = c.fetchone()
        cn: dict = {}
        for k in _CONN_KEYS:
            v = form.get("cn_" + k)
            if ("cn_" + k) in form and v is not None:
                if k == "envelope_number":
                    if (v.strip() or None) != (conn[k] or None):
                        cn[k] = v
                elif v != conn[k]:
                    cn[k] = v
        if "cn_has_canonical" in form:
            want = form.get("cn_is_canonical") is not None
            if want != bool(conn["is_canonical"]):
                cn["is_canonical"] = want
        if cn:
            r = H.parish_connection_set(ctx, person_id, cn, cur=c)
            changed += [H._CONN_LABELS.get(k, k) for k in r["changed"]]
    if not isinstance(changed, list):          # pragma: no cover
        raise InvalidInput("Could not save.")
    return {"changed": changed}
