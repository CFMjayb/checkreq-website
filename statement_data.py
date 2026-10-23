"""
statement_data.py -- Beacon Donor Management: reads what a giving statement needs, for statement_rules to work on. Reads only; every query
is scoped by the parish id the CALLER supplies (never a browser value), and explicit column lists keep memos, correction reasons and notes
out of anything it returns.

  person_gifts(c, parish_id, person_id, period_from, period_to)
        the person's own posted gifts at this parish in CLOSED or RECONCILED batches (the same join the member's Giving page uses), not
        voided, never a non-gift receipt, each with its splits. NO memo is selected. The period filter uses the postmark date when a gift
        has one, else the gift date, which is the statement-year rule.
  gift_years(c, parish_id, person_id)       the calendar years in which the person has at least one such gift (by the same rule)
  donor_record(c, parish_id, person_id)     who the statement is for: name parts, option, delivery, address, do-not-mail, spouse and their option
"""
from __future__ import annotations

import datetime as dt

GIFT_COLUMNS = ("g.id, g.gift_date, g.postmark_date, g.gift_type, g.status, g.check_number, g.goods_value, g.amount, g.reverses_gift_id, "
                "g.reclass_of_gift_id, g.replaces_gift_id, g.in_kind_description, g.stock_symbol, g.stock_shares")
_GIFT_FROM = (
    "  FROM donor.gift g "
    "  JOIN donor.batch b ON b.id = g.batch_id AND b.parish_id = g.parish_id AND b.status IN ('closed', 'reconciled') "
    " WHERE g.parish_id = %s AND g.person_id = %s AND g.status <> 'voided' AND g.gift_type <> 'non_gift_receipt' "
)
_EFFECTIVE_DATE = "COALESCE(g.postmark_date, g.gift_date)"


def person_gifts(c, parish_id: int, person_id: int, period_from: dt.date, period_to: dt.date) -> list[dict]:
    c.execute(f"SELECT {GIFT_COLUMNS}{_GIFT_FROM} AND {_EFFECTIVE_DATE} BETWEEN %s AND %s ORDER BY {_EFFECTIVE_DATE}, g.id",
              (parish_id, person_id, period_from, period_to))
    gifts = c.fetchall()
    if not gifts:
        return []
    ids = [g["id"] for g in gifts]
    c.execute("SELECT gs.gift_id, gs.fund_id, COALESCE(NULLIF(f.statement_name, ''), f.name) AS fund_name, gs.amount "
              "  FROM donor.gift_split gs JOIN donor.fund f ON f.id = gs.fund_id AND f.parish_id = %s "
              " WHERE gs.gift_id = ANY(%s) ORDER BY gs.id", (parish_id, ids))
    by_gift: dict = {}
    for s in c.fetchall():
        by_gift.setdefault(s["gift_id"], []).append({"fund_id": s["fund_id"], "fund_name": s["fund_name"], "amount": s["amount"]})
    for g in gifts:
        g["splits"] = by_gift.get(g["id"], [])
    return [g for g in gifts if g["splits"]]


def gift_years(c, parish_id: int, person_id: int) -> list[int]:
    c.execute(f"SELECT DISTINCT EXTRACT(YEAR FROM {_EFFECTIVE_DATE})::int AS y{_GIFT_FROM}ORDER BY y DESC", (parish_id, person_id))
    return [r["y"] for r in c.fetchall()]


def donor_record(c, parish_id: int, person_id: int) -> dict | None:
    """The facts statement_rules.decide and the letter need about one person at this parish, or None when the person is unknown here
    (no live connection and no gift)."""
    c.execute("SELECT id, record_type, org_name, title, first_name, middle_name, last_name, suffix, is_placeholder, do_not_mail, deceased_date, archived_at "
              "  FROM donor.person WHERE id = %s", (person_id,))
    p = c.fetchone()
    if not p:
        return None
    c.execute("SELECT statement_option, statement_delivery, envelope_number, kind FROM donor.parish_connection "
              " WHERE person_id = %s AND parish_id = %s AND archived_at IS NULL", (person_id, parish_id))
    conn = c.fetchone()
    if not conn:
        c.execute("SELECT 1 AS x FROM donor.gift WHERE parish_id = %s AND person_id = %s LIMIT 1", (parish_id, person_id))
        if not c.fetchone():
            return None
    c.execute("SELECT h.address1, h.address2, h.city, h.state, h.postal_code, h.mail_address1, h.mail_address2, h.mail_city, h.mail_state, h.mail_postal_code "
              "  FROM donor.household_member hm JOIN donor.household h ON h.id = hm.household_id "
              " WHERE hm.person_id = %s AND hm.left_at IS NULL AND h.archived_at IS NULL", (person_id,))
    household = c.fetchone()
    spouse = None
    c.execute("SELECT spouse_id FROM donor.spouse_link WHERE person_id = %s AND ended_at IS NULL", (person_id,))
    link = c.fetchone()
    if link:
        c.execute("SELECT statement_option FROM donor.parish_connection WHERE person_id = %s AND parish_id = %s AND archived_at IS NULL", (link["spouse_id"], parish_id))
        sc = c.fetchone()
        c.execute("SELECT record_type, org_name, first_name, middle_name, last_name, suffix FROM donor.person WHERE id = %s", (link["spouse_id"],))
        sp = c.fetchone()
        if sp:
            spouse = {"person_id": link["spouse_id"], "connected": sc is not None, "statement_option": (sc or {}).get("statement_option"), "person": sp}
    return {
        "person_id": person_id, "person": p, "household": household,
        "statement_option": (conn or {}).get("statement_option") or "individual",
        "delivery": (conn or {}).get("statement_delivery") or "print",
        "envelope_number": (conn or {}).get("envelope_number"),
        "do_not_mail": bool(p["do_not_mail"]), "is_placeholder": bool(p["is_placeholder"]),
        "is_archived": p["archived_at"] is not None, "is_deceased": p["deceased_date"] is not None,
        "spouse": spouse,
    }
