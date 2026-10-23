"""
donor_portal_statements.py -- Beacon Donor Management: the PARISHIONER's own giving statements (Plan section 4d). Every function takes `ps`, the portal session as
donor_portal_login.current() read it: {person_id, parish_id, ...}. The person and the parish come from that row and from nowhere else; a year is the only thing
the browser chooses, and it has to be one of the person's own years.

  member_years(ps)            the years in which this person has posted gifts, newest first, each with its count and deductible total (like TouchPoint's list)
  member_statement(ps, year)  one year's statement of the person's OWN gifts, built with the parish's DEFAULT template. Individual only, even when the person's
                              option is "joint" (a joint statement stays a staff run until both spouses agree). Writes one audit line (who, which year, which
                              template version, the gift fingerprint, the total; never the contents or the donor's name). The PDF is NOT stored.

Closed doors, each with its plain reason: the parish has not switched member statements on; the parish profile is not confirmed; the person is set to "no
statement" (the parish office provides theirs on request); more than 10 statements in an hour (the builder renders one at a time). A person with no gifts in a
year gets "not found" for that year, as does a year that is not theirs.
"""
from __future__ import annotations

import datetime as dt

import statement_data as SD
import statement_profile as SP
import statement_render as RND
import statement_rules as SR
import statement_templates as TPL
from donor_core import DonorError, NotFound, PermissionDenied, tx
from psycopg.types.json import Jsonb

RATE_PER_HOUR = 10
OFF_MESSAGE = "Statements are not available online yet. Please use Get Help and the parish office will help."
NONE_MESSAGE = "The parish office provides your statement on request. Please use Get Help."
BUSY_MESSAGE = "Please try again in a minute."


class TooMany(DonorError):
    pass


def _on(ps: dict) -> bool:
    s = ps.get("settings") or {}
    return bool(s.get("giving_enabled") and s.get("member_statements_enabled"))


def member_years(ps: dict, *, today: dt.date | None = None) -> dict:
    """{years: [{year, count, total, to_date}], available, reason}. `available` is False (with a reason) when the parish has the area switched off or the person
    is set to no statement; the list is then empty, never an error."""
    today = today or dt.date.today()
    if not _on(ps):
        return {"years": [], "available": False, "reason": OFF_MESSAGE}
    pid, parish_id = int(ps["person_id"]), int(ps["parish_id"])
    settings = ps.get("settings") or {}
    out = []
    with tx() as c:
        donor = SD.donor_record(c, parish_id, pid)
        if not donor:
            return {"years": [], "available": True, "reason": None}
        if donor["statement_option"] == "none":
            return {"years": [], "available": False, "reason": NONE_MESSAGE}
        for y in SD.gift_years(c, parish_id, pid):
            p_from, p_to = SR.year_period(y, today)
            gifts = SD.person_gifts(c, parish_id, pid, p_from, p_to)
            st = SR.build_statement(gifts, p_from, p_to, include_nondeductible=bool(settings.get("statement_include_nondeductible")))
            if st["has_reportable"]:
                out.append({"year": y, "count": st["gift_count"], "total": st["deductible_total"], "to_date": y == today.year and today < dt.date(y, 12, 31)})
    return {"years": out, "available": True, "reason": None}


def _recent_count(c, parish_id: int, person_id: int) -> int:
    c.execute("SELECT COUNT(*) AS n FROM donor.stmt_event WHERE parish_id = %s AND person_id = %s AND kind = 'member_view' AND created_at > NOW() - INTERVAL '1 hour'", (parish_id, person_id))
    return c.fetchone()["n"]


def member_statement(ps: dict, year, *, today: dt.date | None = None, logo_loader=None) -> dict:
    """The sheet context (statement_render.sheet_context) for the person's own statement for `year`, plus the template version used. Raises PermissionDenied (a closed
    door, with its reason), NotFound (not their year / no gifts) or TooMany. Writes the audit line only when it will return a statement."""
    today = today or dt.date.today()
    try:
        yr = int(year)
    except (TypeError, ValueError):
        raise NotFound("That statement was not found.")
    if not _on(ps):
        raise PermissionDenied(OFF_MESSAGE)
    pid, parish_id = int(ps["person_id"]), int(ps["parish_id"])
    settings = ps.get("settings") or {}
    with tx() as c:
        ready, _gaps = SP.profile_ready(c, parish_id)
        if not ready:
            raise PermissionDenied(OFF_MESSAGE)
        donor = SD.donor_record(c, parish_id, pid)
        if not donor:
            raise NotFound("That statement was not found.")
        if donor["statement_option"] == "none":
            raise PermissionDenied(NONE_MESSAGE)
        if yr not in SD.gift_years(c, parish_id, pid):
            raise NotFound("That statement was not found.")
        if _recent_count(c, parish_id, pid) >= RATE_PER_HOUR:
            raise TooMany(BUSY_MESSAGE)
        p_from, p_to = SR.year_period(yr, today)
        gifts = SD.person_gifts(c, parish_id, pid, p_from, p_to)
        stmt = SR.build_statement(gifts, p_from, p_to, include_nondeductible=bool(settings.get("statement_include_nondeductible")),
                                  show_check_numbers=settings.get("statement_show_check_numbers") is not False)
        if not stmt["has_reportable"]:
            raise NotFound("That statement was not found.")
        tpl = TPL.ensure_default(parish_id, 0, cur=c)
        profile = SP._row(c, parish_id)
        c.execute("SELECT name FROM portal.parishes WHERE id = %s", (parish_id,))
        parish_name = (c.fetchone() or {}).get("name") or ""
        donor_out = {"person_id": pid, "name": SR.donor_name(donor["person"]), "address": SR.address_lines(donor["household"]), "envelope_number": None}
        wants_logo = (profile or {}).get("use_logo", True) and (tpl["parts"].get("letterhead") or {}).get("logo", True)
        sheet = RND.sheet_context(tpl["parts"], parish_name=parish_name, profile=profile, donor=donor_out, statement=stmt, period_from=p_from, period_to=p_to,
                                  recorded_as_of=today, today=today, settings=settings, logo_src=(logo_loader(parish_id) if (logo_loader and wants_logo) else None))
        c.execute("INSERT INTO donor.stmt_event (parish_id, person_id, kind, user_id, detail) VALUES (%s, %s, 'member_view', 0, %s)",
                  (parish_id, pid, Jsonb({"year": yr, "template_version_id": tpl.get("version_id"), "fingerprint": stmt["fingerprint"], "deductible_total": str(stmt["deductible_total"])})))
    return {"sheet": sheet, "statement": stmt, "year": yr, "template_version": tpl.get("version_no")}
