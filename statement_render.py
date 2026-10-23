"""
statement_render.py -- Beacon Donor Management: turns a template's parts plus a statement's data into what the ONE statement layout
(templates/statement_sheet.html) needs. Pure: no database, no web. The layout is shared by the staff preview, the PDF builder and the member's own
statement, so there is never a staff copy and a member copy.

  merge_values(...)    the value of every allowed merge field ({{donor_name}}, {{year}}, ...) for this statement
  sheet_context(...)   the template's wording cleaned AGAIN and filled with those values (each value escaped), the section switches and the look,
                       together with the statement itself, in the shape statement_sheet.html reads

The wording is cleaned here as well as when it is saved, so a row written some other way still cannot put markup in a PDF.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import statement_text as TXT


def _date(v) -> str:
    return f"{v.month}/{v.day}/{v.year}" if isinstance(v, (dt.date, dt.datetime)) else (v or "")


def _money(v) -> str:
    d = Decimal(str(v if v is not None else 0))
    return ("-" if d < 0 else "") + "${:,.2f}".format(abs(d))


def merge_values(*, parish_name: str, profile: dict | None, donor: dict, statement: dict, period_from, period_to, recorded_as_of, today) -> dict:
    p = profile or {}
    addr = ", ".join(x for x in (p.get("address1"), p.get("address2"), " ".join(y for y in (", ".join(z for z in (p.get("city"), p.get("state")) if z), p.get("postal_code")) if y)) if x)
    return {
        "parish_name": parish_name or p.get("legal_name") or "", "parish_legal_name": p.get("legal_name") or parish_name or "the church", "ein": p.get("ein") or "",
        "parish_address": addr, "year": period_to.year, "period": f"{_date(period_from)} - {_date(period_to)}",
        "donor_name": donor.get("name") or "", "donor_address": ", ".join(donor.get("address") or []), "today": _date(today),
        "signer_name": p.get("signer_name") or "", "signer_title": p.get("signer_title") or "", "contact_email": p.get("contact_email") or "",
        "deductible_total": _money(statement.get("deductible_total")), "gift_count": statement.get("gift_count", 0),
        "envelope_number": "", "recorded_as_of": _date(recorded_as_of),
    }


def sheet_context(parts: dict, *, parish_name: str, profile: dict | None, donor: dict, statement: dict, period_from, period_to, recorded_as_of, today,
                  settings: dict | None = None, logo_src: str | None = None) -> dict:
    """Everything statement_sheet.html reads beyond the formatters. `settings` is the parish's statement settings (check numbers)."""
    settings = settings or {}
    values = merge_values(parish_name=parish_name, profile=profile, donor=donor, statement=statement, period_from=period_from, period_to=period_to,
                          recorded_as_of=recorded_as_of, today=today)
    html = {}
    for k in ("notice", "opening", "closing", "signer", "questions"):
        try:
            html[k] = TXT.fill(TXT.clean_rich(parts.get(k)), values)
        except TXT.TextError:
            html[k] = ""
    if not values.get("contact_email"):
        html["questions"] = ""            # "please contact ." must never print: no address on the profile means no questions line
    if not values.get("signer_name"):
        html["signer"] = ""               # a signature block with no name is left off until the profile has one
    sec = parts.get("sections") or {}
    look = parts.get("look") or {}
    show_checks = bool(sec.get("check_numbers", True)) and settings.get("statement_show_check_numbers") is not False
    return {
        "profile": profile, "donor": donor, "statement": dict(statement, show_check_numbers=show_checks), "period_from": period_from, "period_to": period_to,
        "recorded_as_of": recorded_as_of, "words": html, "show_summary": bool(sec.get("summary_by_fund", True)), "show_list": bool(sec.get("gift_list", True)),
        "accent": look.get("accent") if look.get("accent") in ("green", "navy", "burgundy", "gray") else "green",
        "size": look.get("size") if look.get("size") in ("small", "normal") else "normal",
        "logo_src": logo_src if (parts.get("letterhead") or {}).get("logo", True) and (profile is None or profile.get("use_logo", True)) else None,
    }
