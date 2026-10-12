"""
statement_rules.py -- Beacon Donor Management: the RULES of a giving (tax) statement. Pure: no database, no web, no clock of its own.
Staff and members get identical results because they both call this (Plan "Donor Management - Giving Statements Plan", section 4b).

What a statement shows, in one place:
  * Only gifts the caller passes in. The data reader (statement_data) passes posted gifts in CLOSED or RECONCILED batches, never an open
    batch, never a non-gift receipt; this module drops anything voided or a non-gift receipt again, so a caller mistake cannot leak one.
  * Reversals, returns and reclassifications are stored as new NEGATIVE rows pointing at the original gift, with the original's date.
    A correction pair is dropped from the LIST (the original and its reversing row; the original and the negative half of a reclass),
    and the totals are the same either way, because the pair sums to zero. A reclassified gift therefore shows only under its final
    fund. A reversing row whose original was not passed in is KEPT (as a negative line) so money is never silently hidden, and the
    statement carries a warning.
  * Which year: the POSTMARK date when the gift has one (a December-postmarked check deposited in January counts in December), else the
    gift date (Jay, decision 6).
  * Tax-deductible gifts are listed by date and fund with a total. Goods or services given back (goods_value) reduce the deductible
    part; the statement shows the value given. Non-cash gifts (in kind) are listed by DESCRIPTION only, never a dollar value; stock by
    symbol and shares only. Non-deductible gifts are left off unless the parish turns the section on. Staff memos never appear.
  * A fingerprint of exactly the gifts printed lets a later check tell that a statement is out of date ("changed since issued").

Skips (decide): a statement is not built for a placeholder, a person set to "no statement", a person with nothing to report, a total
under the parish's minimum, a person with no mailing address who gets print (unless the parish allows it), or a person marked
do-not-mail who gets print. Joint statements need BOTH spouses set to joint and a current spouse link; otherwise each is individual and the
plan warns. Every skip comes with its plain reason.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0.00")
CENT = Decimal("0.01")

SECTION_DEDUCTIBLE = "tax_deductible"
IN_PERIOD_TYPES = ("tax_deductible", "non_deductible", "in_kind", "stock")


def money(v) -> Decimal:
    return Decimal(str(v if v is not None else 0)).quantize(CENT, ROUND_HALF_UP)


def effective_date(g: dict) -> dt.date:
    """The date that decides the statement year: the postmark when there is one, else the gift date."""
    return g.get("postmark_date") or g["gift_date"]


def in_period(g: dict, period_from: dt.date, period_to: dt.date) -> bool:
    return period_from <= effective_date(g) <= period_to


def year_period(year: int, today: dt.date | None = None) -> tuple[dt.date, dt.date]:
    """Calendar year. For the current year the period ends today ("through [today]")."""
    start, end = dt.date(year, 1, 1), dt.date(year, 12, 31)
    if today is not None and today.year == year and today < end:
        end = today
    return start, end


# ── Names and addresses ─────────────────────────────────────────────────────────────────────────
def donor_name(p: dict) -> str:
    """The name printed on a statement. An organization prints its name. A person prints first, middle, last, suffix, no title
    (TouchPoint's "No Titles On Statements"). Missing parts are left out, never printed as 'None'."""
    if (p.get("record_type") or "person") == "organization":
        return (p.get("org_name") or "").strip()
    parts = [p.get("first_name"), p.get("middle_name"), p.get("last_name")]
    name = " ".join(x.strip() for x in parts if x and x.strip())
    if p.get("suffix") and p["suffix"].strip():
        name = f"{name}, {p['suffix'].strip()}" if name else p["suffix"].strip()
    return name


def address_lines(h: dict | None) -> list[str]:
    """The household's MAILING address when it has one, else its street address. [] when there is none (street line empty)."""
    if not h:
        return []
    if (h.get("mail_address1") or "").strip():
        a1, a2, city, st, zp = (h.get("mail_address1"), h.get("mail_address2"), h.get("mail_city"), h.get("mail_state"), h.get("mail_postal_code"))
    else:
        a1, a2, city, st, zp = (h.get("address1"), h.get("address2"), h.get("city"), h.get("state"), h.get("postal_code"))
    if not (a1 or "").strip():
        return []
    last = " ".join(x for x in (", ".join(x.strip() for x in (city, st) if x and x.strip()), (zp or "").strip()) if x)
    return [x.strip() for x in (a1, a2, last) if x and x.strip()]


# ── Which gifts count, and the corrections netted ───────────────────────────────────────────────
def counted_gifts(gifts: list[dict]) -> tuple[list[dict], list[str]]:
    """The gifts that stand after voids are gone and each correction pair is cancelled. Returns (gifts, warnings)."""
    live = [g for g in gifts if g.get("status") != "voided" and g.get("gift_type") != "non_gift_receipt"]
    by_id = {g["id"]: g for g in live}
    drop: set = set()
    warnings: list[str] = []
    for g in live:
        if money(g["amount"]) >= ZERO:
            continue
        original = g.get("reverses_gift_id") or g.get("reclass_of_gift_id")
        if original is not None and original in by_id:
            drop.add(g["id"])
            drop.add(original)
        else:
            warnings.append("A correction is on this donor's record without its original gift in the same period; it is shown as a negative line.")
    return [g for g in live if g["id"] not in drop], sorted(set(warnings))


def _allocate(total: Decimal, amounts: list[Decimal]) -> list[Decimal]:
    """Split `total` across the amounts in proportion, to the cent; the last share takes the rounding remainder."""
    whole = sum(amounts, ZERO)
    if total <= ZERO or whole <= ZERO:
        return [ZERO for _ in amounts]
    out, used = [], ZERO
    for i, a in enumerate(amounts):
        share = (total - used) if i == len(amounts) - 1 else (total * a / whole).quantize(CENT, ROUND_HALF_UP)
        out.append(share)
        used += share
    return out


def fingerprint(printed: list[dict]) -> str:
    """A hash of exactly the gifts a statement prints (id, date, type, check number, goods value, each split). Any later change to
    one of them, or a gift added or removed, changes it."""
    rows = []
    for g in sorted(printed, key=lambda x: x["id"]):
        rows.append([g["id"], effective_date(g).isoformat(), g.get("gift_type"), g.get("check_number") or "", str(money(g.get("goods_value") or 0)),
                     g.get("in_kind_description") or "", g.get("stock_symbol") or "", str(g.get("stock_shares") or ""),
                     [[s.get("fund_id"), str(money(s["amount"]))] for s in sorted(g["splits"], key=lambda s: (s.get("fund_id") or 0, str(s["amount"])))]])
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode("utf-8")).hexdigest()


def build_statement(gifts: list[dict], period_from: dt.date, period_to: dt.date, *, include_nondeductible: bool = False,
                    show_check_numbers: bool = True) -> dict:
    """Everything a statement prints for the gifts of ONE statement subject (one person, or a joint pair whose gifts carry a
    `donor_label`). Each gift dict: id, gift_date, postmark_date, gift_type, status, check_number, goods_value, amount, reverses_gift_id,
    reclass_of_gift_id, in_kind_description, stock_symbol, stock_shares, splits [{fund_id, fund_name, amount}], donor_label (optional).
    Staff memos are never an input."""
    inside = [g for g in gifts if in_period(g, period_from, period_to)]
    counted, warnings = counted_gifts(inside)
    deductible: list[dict] = []
    nondeductible: list[dict] = []
    in_kind: list[dict] = []
    stock: list[dict] = []
    printed: list[dict] = []
    by_fund: dict[str, Decimal] = {}
    nd_by_fund: dict[str, Decimal] = {}
    goods_total = ZERO
    for g in sorted(counted, key=lambda x: (effective_date(x), x["id"])):
        kind = g["gift_type"]
        splits = g["splits"]
        when = effective_date(g)
        label = g.get("donor_label") or ""
        if kind == "tax_deductible":
            amounts = [money(s["amount"]) for s in splits]
            goods = money(g.get("goods_value") or 0)
            goods_each = _allocate(goods, amounts) if goods > ZERO and sum(amounts, ZERO) > ZERO else [ZERO for _ in amounts]
            printed.append(g)
            for i, s in enumerate(splits):
                amt, gv = amounts[i], goods_each[i]
                ded = amt - gv
                by_fund[s["fund_name"]] = by_fund.get(s["fund_name"], ZERO) + ded
                deductible.append({"date": when, "fund_name": s["fund_name"], "amount": amt, "goods_value": gv, "deductible": ded,
                                   "check_number": (g.get("check_number") or "") if show_check_numbers and i == 0 else "", "donor_label": label, "gift_id": g["id"]})
            goods_total += goods
            if goods > ZERO:
                warnings.append("A gift has goods or services given back; the statement shows the value given and the deductible part.")
        elif kind == "non_deductible":
            if include_nondeductible:
                printed.append(g)
                for s in splits:
                    amt = money(s["amount"])
                    nd_by_fund[s["fund_name"]] = nd_by_fund.get(s["fund_name"], ZERO) + amt
                    nondeductible.append({"date": when, "fund_name": s["fund_name"], "amount": amt, "donor_label": label, "gift_id": g["id"]})
        elif kind == "in_kind":
            printed.append(g)                # described only: never a dollar value, whatever the record holds
            in_kind.append({"date": when, "fund_name": splits[0]["fund_name"] if splits else "", "description": (g.get("in_kind_description") or "Non-cash gift").strip(),
                            "donor_label": label, "gift_id": g["id"]})
        elif kind == "stock":
            printed.append(g)                # symbol and shares only
            stock.append({"date": when, "fund_name": splits[0]["fund_name"] if splits else "", "symbol": (g.get("stock_symbol") or "").strip(),
                          "shares": g.get("stock_shares"), "donor_label": label, "gift_id": g["id"]})
    deductible_total = sum((r["deductible"] for r in deductible), ZERO)
    return {
        "period_from": period_from, "period_to": period_to,
        "gifts": deductible, "by_fund": [{"fund_name": k, "amount": v} for k, v in sorted(by_fund.items())],
        "deductible_total": deductible_total, "goods_total": goods_total,
        "nondeductible": nondeductible, "nondeductible_by_fund": [{"fund_name": k, "amount": v} for k, v in sorted(nd_by_fund.items())],
        "nondeductible_total": sum((r["amount"] for r in nondeductible), ZERO),
        "in_kind": in_kind, "stock": stock,
        "gift_count": len({r["gift_id"] for r in deductible}) + len(in_kind) + len(stock),
        "has_reportable": bool(deductible or in_kind or stock),
        "fingerprint": fingerprint(printed), "warnings": sorted(set(warnings)),
        "show_check_numbers": show_check_numbers,
    }


# ── Who gets a statement, and why not ───────────────────────────────────────────────────────────
def joint_role(donor: dict) -> dict:
    """How this person's statement is addressed. `donor` carries statement_option and a `spouse` dict (person_id, statement_option,
    connected) or None. Returns {role: individual | joint_lead | joint_follower, partner_id, warnings}. Joint needs BOTH set to joint
    and a current spouse link (the spouse connected to this parish); the lower person id leads, the other is skipped as included."""
    opt = donor.get("statement_option") or "individual"
    sp = donor.get("spouse")
    if opt != "joint":
        return {"role": "individual", "partner_id": None, "warnings": []}
    if not sp:
        return {"role": "individual", "partner_id": None, "warnings": ["Set to a joint statement but has no current spouse link: an individual statement is used."]}
    if not sp.get("connected", True) or (sp.get("statement_option") or "individual") != "joint":
        return {"role": "individual", "partner_id": None, "warnings": ["Set to a joint statement but the spouse is not set to joint at this parish: an individual statement is used."]}
    lead = donor["person_id"] < sp["person_id"]
    return {"role": "joint_lead" if lead else "joint_follower", "partner_id": sp["person_id"], "warnings": []}


def decide(donor: dict, statement: dict, settings: dict, *, bulk: bool = True) -> dict:
    """Whether to build `statement` for `donor`, and the plain reason when not. `bulk` is a staff run over many donors; a staff
    one-person preview or build (bulk False) ignores the choices a donor made about delivery and no-address, so staff can always
    produce one. settings: statement_min_total, statement_allow_no_address.
    Returns {build, reason, warnings}."""
    warnings = list(statement.get("warnings") or [])
    if donor.get("is_placeholder"):
        return {"build": False, "reason": "placeholder or anonymous donor", "warnings": warnings}
    if bulk and (donor.get("statement_option") or "individual") == "none":
        return {"build": False, "reason": "set to no statement", "warnings": warnings}
    if not statement.get("has_reportable"):
        return {"build": False, "reason": "no gifts to report in the period", "warnings": warnings}
    minimum = money(settings.get("statement_min_total") or 0)
    if bulk and minimum > ZERO and statement["deductible_total"] < minimum and not statement.get("in_kind") and not statement.get("stock"):
        return {"build": False, "reason": f"under the parish minimum of ${minimum:,.2f}", "warnings": warnings}
    if bulk and donor.get("delivery", "print") == "print":
        if donor.get("do_not_mail"):
            return {"build": False, "reason": "marked do not mail (not printed)", "warnings": warnings}
        if not donor.get("has_address") and not settings.get("statement_allow_no_address"):
            return {"build": False, "reason": "no mailing address", "warnings": warnings}
    if not donor.get("has_address"):
        warnings.append("No mailing address is on file.")
    return {"build": True, "reason": None, "warnings": warnings}
