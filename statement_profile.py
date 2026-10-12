"""
statement_profile.py -- Beacon Donor Management: what a giving statement says ABOUT THE PARISH, and the parish's statement settings.

A statement is a tax document, so it carries the parish's legal name, tax ID (EIN), address and a signer. Those live in one row per parish
(donor.parish_statement_profile) that a Finance person (or a Cornerstone person working for the parish) fills in and CONFIRMS. Until it is
confirmed nothing builds a statement (profile_ready says what is missing). Editing a confirmed profile takes the confirmation away: a
changed tax ID has to be looked at again.

First fill-in comes from what Beacon already holds, shown as a suggestion and never saved by itself: legal name and tax ID from the parish
record (portal.parishes), the church office address and email from its contacts, the signer from the clergy list. The parochial-report tax IDs
are NOT read here (the app's database role cannot read that table, and production does not have it); Cornerstone types or confirms the tax ID.

Operations: profile_get, profile_save (with confirm), profile_ready, settings_save.
Capabilities: statements.run reads; statements.templates changes. Both need giving turned on for the parish.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

from donor_core import Ctx, InvalidInput, clean_email, clean_text, log_change, need_giving, tx

PROFILE_FIELDS = ("legal_name", "ein", "address1", "address2", "city", "state", "postal_code", "signer_name", "signer_title", "contact_email")
REQUIRED_TO_CONFIRM = (("legal_name", "legal name"), ("ein", "tax ID (EIN)"), ("address1", "street address"), ("city", "city"), ("state", "state"),
                       ("postal_code", "ZIP code"), ("signer_name", "signer's name"), ("signer_title", "signer's title"))
SETTING_KEYS = ("statement_min_total", "statement_allow_no_address", "statement_include_nondeductible", "statement_show_check_numbers", "member_statements_enabled")
SIGNER_ROLE_ORDER = ("rector", "priest in charge", "priest-in-charge", "vicar", "interim rector", "interim priest in charge", "interim", "missioner")


def format_ein(value) -> str | None:
    """'12-3456789' for nine digits (any punctuation or spaces allowed), None for blank. Anything else is refused by the caller."""
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return None
    if len(digits) != 9:
        raise InvalidInput("A tax ID (EIN) has nine digits, like 12-3456789.", "ein")
    return f"{digits[:2]}-{digits[2:]}"


def _need_read(ctx: Ctx) -> None:
    need_giving(ctx, "statements.run", "You do not have permission to see the statement settings for this parish.")


def _row(c, parish_id: int) -> dict | None:
    c.execute("SELECT * FROM donor.parish_statement_profile WHERE parish_id = %s", (parish_id,))
    return c.fetchone()


def _office(contacts) -> dict:
    """The 'church_office' entry of a parish's contacts, whatever shape the JSON has ({'contacts': [...]} or a bare list)."""
    if isinstance(contacts, str):
        try:
            contacts = json.loads(contacts)
        except ValueError:
            return {}
    items = contacts.get("contacts") if isinstance(contacts, dict) else contacts
    for e in items or []:
        if isinstance(e, dict) and e.get("role") == "church_office":
            return e
    return {}


def prefill(c, parish_id: int) -> dict:
    """A suggested profile from what the system already holds. Nothing is written. Each value is None when nothing is on file."""
    c.execute("SELECT name, legal_name, ein, contacts, logo_gcs_path FROM portal.parishes WHERE id = %s", (parish_id,))
    p = c.fetchone() or {}
    office = _office(p.get("contacts"))
    c.execute("SELECT first_name, middle_name, last_name, suffix, role FROM portal.congregation_cache WHERE parish_id = %s AND role_category = 'clergy' ORDER BY id", (parish_id,))
    clergy = c.fetchall()
    signer = None
    for want in SIGNER_ROLE_ORDER:
        signer = next((r for r in clergy if want in (r["role"] or "").lower()), None)
        if signer:
            break
    signer_name = None
    if signer:
        signer_name = " ".join(x.strip() for x in (signer["first_name"], signer["middle_name"], signer["last_name"]) if x and x.strip())
        if signer.get("suffix") and signer["suffix"].strip():
            signer_name += ", " + signer["suffix"].strip()
    ein = None
    try:
        ein = format_ein(p.get("ein"))
    except InvalidInput:
        ein = None                       # a stored value that is not nine digits is left for a person to type
    return {
        "legal_name": (p.get("legal_name") or "").strip() or (p.get("name") or "").strip() or None,
        "ein": ein, "ein_source": "parish_record" if ein else None,
        "address1": (office.get("address") or "").strip() or None, "address2": (office.get("address2") or "").strip() or None,
        "city": (office.get("city") or "").strip() or None, "state": (office.get("state") or "").strip() or None,
        "postal_code": (office.get("zip") or "").strip() or None,
        "signer_name": signer_name or None, "signer_title": (signer["role"].strip() if signer and signer.get("role") else None),
        "contact_email": (office.get("email") or "").strip() or None,
        "has_logo": bool(p.get("logo_gcs_path")),
    }


def missing_items(profile: dict | None) -> list[str]:
    return [label for key, label in REQUIRED_TO_CONFIRM if not (profile or {}).get(key)]


def profile_get(ctx: Ctx) -> dict:
    """{profile (the saved row or None), suggestion (what the system holds), confirmed, missing, has_logo, settings}."""
    _need_read(ctx)
    with tx() as c:
        row = _row(c, ctx.parish_id)
        sug = prefill(c, ctx.parish_id)
    return {"profile": row, "suggestion": sug, "confirmed": bool(row and row.get("confirmed_at")), "missing": missing_items(row),
            "has_logo": sug["has_logo"], "settings": {k: ctx.settings.get(k) for k in SETTING_KEYS}}


def profile_ready(c, parish_id: int) -> tuple[bool, list[str]]:
    """(True, []) when the parish's profile is saved, complete and confirmed; else (False, what is missing or 'confirmation')."""
    row = _row(c, parish_id)
    if not row:
        return False, ["the parish statement profile"]
    gaps = missing_items(row)
    if not row.get("confirmed_at"):
        gaps.append("Finance's confirmation of the profile")
    return (not gaps), gaps


def profile_save(ctx: Ctx, fields: dict, *, confirm: bool = False, use_logo: bool | None = None) -> dict:
    """Save the parish's statement profile; with confirm=True also confirm it (every required item has to be filled in). Saving with
    a change to any field takes an existing confirmation away. Needs statements.templates. Logged field by field."""
    need_giving(ctx, "statements.templates", "Only Finance can change the parish statement profile.")
    clean: dict = {}
    for k in PROFILE_FIELDS:
        if k == "ein":
            clean[k] = format_ein(fields.get(k))
        elif k == "contact_email":
            clean[k] = clean_email(fields.get(k), field="contact email") if (fields.get(k) or "").strip() else None
        else:
            clean[k] = clean_text(fields.get(k), field=k.replace("_", " "), max_len=160)
    gaps = [label for key, label in REQUIRED_TO_CONFIRM if not clean.get(key)]
    if confirm and gaps:
        raise InvalidInput("Fill in " + ", ".join(gaps) + " before confirming the profile.", "profile")
    with tx() as c:
        c.execute("INSERT INTO donor.parish_statement_profile (parish_id, updated_by_user_id) VALUES (%s, %s) ON CONFLICT (parish_id) DO NOTHING", (ctx.parish_id, ctx.user_id))
        c.execute("SELECT * FROM donor.parish_statement_profile WHERE parish_id = %s FOR UPDATE", (ctx.parish_id,))
        old = c.fetchone()
        sug = prefill(c, ctx.parish_id)
        new_logo = old["use_logo"] if use_logo is None else bool(use_logo)
        changed = [k for k in PROFILE_FIELDS if old.get(k) != clean[k]] + (["use_logo"] if old["use_logo"] != new_logo else [])
        source = None
        if clean["ein"]:
            if clean["ein"] == sug.get("ein"):
                source = "parish_record"
            else:
                source = old.get("ein_source") if (old.get("ein") == clean["ein"] and old.get("ein_source")) else "typed"
        sets = {k: clean[k] for k in PROFILE_FIELDS}
        sets["ein_source"], sets["use_logo"] = source, new_logo
        set_sql = [f"{k} = %s" for k in sets]
        args = list(sets.values())
        was_confirmed = old.get("confirmed_at") is not None
        lost = (not confirm) and bool(changed) and was_confirmed
        if confirm:
            set_sql += ["confirmed_by_user_id = %s", "confirmed_at = NOW()"]
            args.append(ctx.user_id)
        elif lost:
            set_sql += ["confirmed_by_user_id = NULL", "confirmed_at = NULL"]       # one statement, so the confirmed-by check never sees half of it
        c.execute(f"UPDATE donor.parish_statement_profile SET {', '.join(set_sql)}, updated_by_user_id = %s, updated_at = NOW() WHERE parish_id = %s",
                  (*args, ctx.user_id, ctx.parish_id))
        for k in changed:
            log_change(c, ctx, "parish_statement_profile", ctx.parish_id, k, old.get(k), new_logo if k == "use_logo" else clean.get(k), kind="update", scope="parish")
        if confirm:
            log_change(c, ctx, "parish_statement_profile", ctx.parish_id, "confirmed", None, "confirmed", kind="update", scope="parish")
        elif lost:
            log_change(c, ctx, "parish_statement_profile", ctx.parish_id, "confirmed", "confirmed", None, kind="update", scope="parish")
        c.execute("SELECT * FROM donor.parish_statement_profile WHERE parish_id = %s", (ctx.parish_id,))
        row = c.fetchone()
    return {"profile": row, "confirmed": row.get("confirmed_at") is not None, "missing": missing_items(row), "changed": changed, "confirmation_removed": lost}


def settings_save(ctx: Ctx, values: dict) -> dict:
    """The parish's statement rules: the minimum yearly total, whether a donor with no address still gets one, whether the not-deductible
    section prints, whether check numbers print. Needs statements.templates. Logged."""
    need_giving(ctx, "statements.templates", "Only Finance can change the statement settings.")
    new: dict = {}
    if "statement_min_total" in values:
        raw = str(values["statement_min_total"] or "0").replace("$", "").replace(",", "").strip() or "0"
        try:
            amount = Decimal(raw).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            raise InvalidInput("The minimum has to be an amount, like 25 or 0.", "statement_min_total")
        if amount < 0 or amount > 1_000_000:
            raise InvalidInput("The minimum has to be between 0 and 1,000,000.", "statement_min_total")
        new["statement_min_total"] = amount
    for k in ("statement_allow_no_address", "statement_include_nondeductible", "statement_show_check_numbers", "member_statements_enabled"):
        if k in values:
            new[k] = bool(values[k])
    changed = []
    with tx() as c:
        c.execute("INSERT INTO donor.parish_settings (parish_id) VALUES (%s) ON CONFLICT (parish_id) DO NOTHING", (ctx.parish_id,))
        c.execute("SELECT * FROM donor.parish_settings WHERE parish_id = %s FOR UPDATE", (ctx.parish_id,))
        old = c.fetchone()
        for k, v in new.items():
            if old[k] != v:
                changed.append(k)
        if changed:
            sets = ", ".join(f"{k} = %s" for k in changed)
            c.execute(f"UPDATE donor.parish_settings SET {sets}, updated_by_user_id = %s, updated_at = NOW() WHERE parish_id = %s",
                      (*[new[k] for k in changed], ctx.user_id, ctx.parish_id))
            for k in changed:
                log_change(c, ctx, "parish_settings", ctx.parish_id, k, old[k], new[k], kind="update", scope="parish")
    return {"changed": changed}
