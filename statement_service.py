"""
statement_service.py -- Beacon Donor Management: giving statements, the staff-side service that puts statement_data (what the database
holds) and statement_rules (what a statement shows) together for one person.

  find_donors(ctx, q)                         people at THIS parish who have posted gifts, matching a name, for the preview picker
  years_for_person(ctx, person_id)            the years in which that person has gifts
  preview_for_person(ctx, person_id, year)    the statement as it would print, the decision (build or the plain reason not), the parish
                                              profile and whether it is confirmed. Builds nothing, stores nothing, sends nothing.

Capability: statements.run, with giving turned on. The parish comes from the Ctx; a person id from the browser is only ever a key looked up
INSIDE this parish (a person with no connection and no gift here reads as "not found").
"""
from __future__ import annotations

import datetime as dt

import statement_data as SD
import statement_profile as SP
import statement_render as RND
import statement_rules as SR
import statement_templates as TPL
from donor_core import Ctx, InvalidInput, NotFound, need_giving, tx

MAX_FIND = 25


def _need(ctx: Ctx) -> None:
    need_giving(ctx, "statements.run", "You do not have permission to run statements for this parish.")


def find_donors(ctx: Ctx, q: str) -> list[dict]:
    _need(ctx)
    text = (q or "").strip()
    if len(text) < 2:
        return []
    like = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    like_any = "%" + like
    with tx() as c:
        c.execute(
            "SELECT p.id, p.record_type, p.org_name, p.first_name, p.middle_name, p.last_name, p.suffix, COUNT(DISTINCT g.id) AS gifts "
            "  FROM donor.person p JOIN donor.gift g ON g.person_id = p.id AND g.parish_id = %s AND g.status <> 'voided' AND g.gift_type <> 'non_gift_receipt' "
            "  JOIN donor.batch b ON b.id = g.batch_id AND b.parish_id = g.parish_id AND b.status IN ('closed', 'reconciled') "
            " WHERE p.is_placeholder = FALSE AND (p.last_name ILIKE %s OR p.first_name ILIKE %s OR p.org_name ILIKE %s) "
            " GROUP BY p.id ORDER BY LOWER(COALESCE(p.last_name, p.org_name, '')), LOWER(COALESCE(p.first_name, '')) LIMIT %s",
            (ctx.parish_id, like, like, like_any, MAX_FIND))
        return [{"person_id": r["id"], "name": SR.donor_name(r), "gifts": r["gifts"]} for r in c.fetchall()]


def years_for_person(ctx: Ctx, person_id: int) -> list[int]:
    _need(ctx)
    with tx() as c:
        return SD.gift_years(c, ctx.parish_id, int(person_id))


def preview_for_person(ctx: Ctx, person_id, year, *, today: dt.date | None = None, template_id: int | None = None) -> dict:
    _need(ctx)
    try:
        pid, yr = int(person_id), int(year)
    except (TypeError, ValueError):
        raise InvalidInput("Pick a person and a year.", "person_id")
    if not (1990 <= yr <= 2100):
        raise InvalidInput("That year is not one statements can be built for.", "year")
    today = today or dt.date.today()
    period_from, period_to = SR.year_period(yr, today)
    with tx() as c:
        donor = SD.donor_record(c, ctx.parish_id, pid)
        if not donor:
            raise NotFound("That person was not found at this parish.")
        gifts = SD.person_gifts(c, ctx.parish_id, pid, period_from, period_to)
        years = SD.gift_years(c, ctx.parish_id, pid)
        profile = SP._row(c, ctx.parish_id)
        ready, gaps = SP.profile_ready(c, ctx.parish_id)
        c.execute("SELECT MAX(entered_at) AS last_entered FROM donor.gift WHERE parish_id = %s", (ctx.parish_id,))
        last_entered = (c.fetchone() or {}).get("last_entered")
        c.execute("SELECT COUNT(*) AS n FROM donor.batch WHERE parish_id = %s AND status IN ('open', 'pending')", (ctx.parish_id,))
        open_batches = c.fetchone()["n"]
        tpl = TPL.resolve(c, ctx.parish_id, template_id)
        c.execute("SELECT logo_gcs_path FROM portal.parishes WHERE id = %s", (ctx.parish_id,))
        has_logo = bool((c.fetchone() or {}).get("logo_gcs_path"))
    settings = ctx.settings
    stmt = SR.build_statement(gifts, period_from, period_to,
                              include_nondeductible=bool(settings.get("statement_include_nondeductible")),
                              show_check_numbers=settings.get("statement_show_check_numbers") is not False)
    p = donor["person"]
    donor_facts = {
        "person_id": pid, "statement_option": donor["statement_option"], "delivery": donor["delivery"], "do_not_mail": donor["do_not_mail"],
        "is_placeholder": donor["is_placeholder"], "has_address": bool(SR.address_lines(donor["household"])), "spouse": donor["spouse"],
    }
    decision = SR.decide(donor_facts, stmt, settings, bulk=False)
    joint = SR.joint_role(donor_facts)
    if joint["warnings"]:
        decision["warnings"] = list(decision["warnings"]) + joint["warnings"]
    if donor["is_deceased"]:
        decision["warnings"].append("This person is recorded as deceased: the statement covers gifts they made while alive.")
    if donor["is_archived"]:
        decision["warnings"].append("This person's record is archived.")
    donor_out = {"person_id": pid, "name": SR.donor_name(p), "address": SR.address_lines(donor["household"]), "envelope_number": donor["envelope_number"],
                 "statement_option": donor["statement_option"], "delivery": donor["delivery"]}
    sheet = RND.sheet_context(tpl["parts"], parish_name=ctx.parish_name, profile=profile, donor=donor_out, statement=stmt, period_from=period_from, period_to=period_to,
                              recorded_as_of=today, today=today, settings=settings, logo_src=f"/parish-logo/{ctx.parish_id}" if has_logo else None)
    return {
        "year": yr, "years": years, "period_from": period_from, "period_to": period_to,
        "donor": donor_out, "sheet": sheet, "template": tpl["template"], "template_version": tpl["version_no"], "template_source": tpl["source"],
        "statement": stmt, "decision": decision,
        "profile": profile, "profile_ready": ready, "profile_gaps": gaps,
        "recorded_as_of": today, "open_batches": open_batches, "last_entered": last_entered,
    }
