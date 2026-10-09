"""
donor_gifts.py -- Beacon Donor Management, Phase 2: gift lines (the rules for one line, and the one place a gift
row is written), what a donor has given, and giving totals by fund.

Used by donor_batches (entering lines), donor_corrections (reversals) and donor_pledges (fulfillment). It never
closes or opens a batch and never talks to QuickBooks.

What a line has to be
  * A gift type that fits its batch: a deposit batch takes tax-deductible gifts, non-deductible gifts and non-gift
    receipts. A non-deposit batch takes in-kind gifts and stock.
  * A donor connected to THIS parish, unless it is a non-gift receipt (a refund or a reimbursement has no donor).
  * One or more splits, each to an OPEN fund at this parish, each a positive amount. The gift's amount is their sum
    and a gift counts as one item however many splits it has.
  * In-kind: a description and a book value equal to the amount. Stock: shares, a symbol, and a value at receipt
    equal to the amount. Goods or services provided to the donor (a dinner, a book) are only allowed on a
    tax-deductible gift and cannot exceed it. A processor fee is only allowed on a deposit-batch gift.

A reversing gift is written by donor_corrections through insert_gift_row, which (unlike entry) accepts negative
splits. Nothing here deletes anything.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import db
from donor_core import (
    Conflict, Ctx, InvalidInput, NotFound, PermissionDenied, check_enum, clean_text, log_change, need_giving,
    parse_date, person_label, to_bool, to_money, tx,
)
from donor_people import require_connection

GIFT_TYPES = ("tax_deductible", "non_deductible", "non_gift_receipt", "in_kind", "stock")
DEPOSIT_TYPES = ("tax_deductible", "non_deductible", "non_gift_receipt")
NON_DEPOSIT_TYPES = ("in_kind", "stock")
MAX_SPLITS = 12
ZERO = Decimal("0.00")


def can_read_gifts(ctx: Ctx) -> bool:
    """Individual gifts are for the finance roles (and the diocese's audit role), never for everyone with a people role."""
    return ctx.can("giving.read") or ctx.can("giving.read.diocese")


def need_read_gifts(ctx: Ctx) -> None:
    if not ctx.settings.get("giving_enabled"):
        raise PermissionDenied("Giving records are not turned on for this parish yet.")
    if not can_read_gifts(ctx):
        raise PermissionDenied("Only finance roles can see individual gifts.")


def money(v) -> str:
    return format(Decimal(v).quantize(Decimal("0.01")), "f")


# ── Validation of one entered line ──────────────────────────────────────────────────────────────
def _splits_from(data: dict) -> list[dict]:
    raw = data.get("splits")
    if raw is None:
        if data.get("fund_id") in (None, "") and data.get("amount") in (None, ""):
            return []
        raw = [{"fund_id": data.get("fund_id"), "amount": data.get("amount")}]
    out = []
    for s in raw:
        if s.get("fund_id") in (None, "") and s.get("amount") in (None, ""):
            continue                                       # a blank split row on the form
        out.append(s)
    return out


def normalize_line(c, ctx: Ctx, batch: dict, data: dict) -> dict:
    """Validate an entered line and return the clean fields plus its splits. Raises InvalidInput with a message a
    clerk can act on. Does not write anything."""
    gift_type = check_enum(data.get("gift_type") or batch.get("default_gift_type"), GIFT_TYPES, field="gift type", allow_blank=False)
    allowed = DEPOSIT_TYPES if batch["kind"] == "deposit" else NON_DEPOSIT_TYPES
    if gift_type not in allowed:
        where = "a deposit batch" if batch["kind"] == "deposit" else "a non-deposit batch"
        raise InvalidInput(f"A {gift_type.replace('_', ' ')} gift cannot go in {where}. "
                           + ("Use a non-deposit batch for stock and in-kind gifts." if batch["kind"] == "deposit"
                              else "A non-deposit batch takes stock and in-kind gifts only."), "gift_type")
    # donor
    person_id = data.get("person_id")
    if person_id in (None, "", 0, "0"):
        person_id = None
    if person_id is None and gift_type != "non_gift_receipt":
        raise InvalidInput("Pick the donor for this gift. Only a non-gift receipt can be entered without one.", "person_id")
    if person_id is not None:
        try:
            person_id = int(person_id)
        except (TypeError, ValueError):
            raise InvalidInput("Pick the donor for this gift.", "person_id")
        require_connection(c, ctx, person_id, include_archived=False)
    # dates
    gift_date = parse_date(data.get("gift_date"), field="gift date", allow_future=False) or batch["deposit_date"]
    postmark = parse_date(data.get("postmark_date"), field="postmark date", allow_future=False)
    if postmark and postmark > gift_date:
        raise InvalidInput("The postmark date cannot be after the date the gift was received.", "postmark_date")
    check_number = clean_text(data.get("check_number"), field="check number", max_len=30)
    memo = clean_text(data.get("memo"), field="memo", max_len=200)
    # splits
    raw_splits = _splits_from(data)
    default_fund = batch.get("default_fund_id")
    if default_fund and len(raw_splits) == 1 and raw_splits[0].get("fund_id") in (None, ""):
        raw_splits = [{**raw_splits[0], "fund_id": default_fund}]     # one amount, no fund picked: the batch's default fund
    if not raw_splits:
        raise InvalidInput("Enter an amount and pick a fund.", "amount")
    if len(raw_splits) > MAX_SPLITS:
        raise InvalidInput(f"A gift can be split across at most {MAX_SPLITS} funds.", "splits")
    splits, seen = [], set()
    for s in raw_splits:
        try:
            fid = int(s.get("fund_id"))
        except (TypeError, ValueError):
            raise InvalidInput("Pick a fund for every amount.", "fund_id")
        if fid in seen:
            raise InvalidInput("List each fund once. Add the amounts together instead.", "fund_id")
        seen.add(fid)
        amt = to_money(s.get("amount"), field="amount")
        if amt <= ZERO:
            raise InvalidInput("Every amount must be more than zero.", "amount")
        c.execute("SELECT id, name, is_open FROM donor.fund WHERE id = %s AND parish_id = %s", (fid, ctx.parish_id))
        f = c.fetchone()
        if not f:
            raise NotFound("That fund was not found at this parish.")
        if not f["is_open"]:
            raise InvalidInput(f"The fund '{f['name']}' is closed and takes no new gifts.", "fund_id")
        splits.append({"fund_id": fid, "amount": amt, "fund_name": f["name"]})
    total = sum((s["amount"] for s in splits), ZERO)
    # goods and services, fees
    goods = to_money(data.get("goods_value") or "0", field="value of goods or services")
    if goods > ZERO and gift_type != "tax_deductible":
        raise InvalidInput("Goods or services are only recorded on a tax-deductible gift.", "goods_value")
    if goods > total:
        raise InvalidInput("The value of goods or services cannot be more than the gift.", "goods_value")
    fee = to_money(data.get("fee_amount") or "0", field="processing fee")
    covered = to_bool(data.get("fee_covered_by_donor"), field="fee covered by donor")
    if fee > ZERO and batch["kind"] != "deposit":
        raise InvalidInput("A processing fee only applies to a deposit batch.", "fee_amount")
    if fee > total:
        raise InvalidInput("The processing fee cannot be more than the gift.", "fee_amount")
    # in-kind and stock
    desc = book = shares = symbol = svalue = None
    if gift_type == "in_kind":
        desc = clean_text(data.get("in_kind_description"), field="description", max_len=300)
        if not desc:
            raise InvalidInput("Describe the in-kind gift. Beacon records the description, never a value to the donor.", "in_kind_description")
        book = to_money(data.get("book_value") if data.get("book_value") not in (None, "") else total, field="book value")
        if book != total:
            raise InvalidInput("The book value has to equal the amount of the gift.", "book_value")
    elif gift_type == "stock":
        try:
            shares = Decimal(str(data.get("stock_shares")).replace(",", "").strip())
        except Exception:
            raise InvalidInput("Enter the number of shares.", "stock_shares")
        if shares <= 0:
            raise InvalidInput("The number of shares has to be more than zero.", "stock_shares")
        symbol = (clean_text(data.get("stock_symbol"), field="stock symbol", max_len=10) or "").upper()
        if not re.fullmatch(r"[A-Z0-9.\-]{1,10}", symbol or ""):
            raise InvalidInput("Enter the stock's ticker symbol, for example ABC.", "stock_symbol")
        svalue = to_money(data.get("stock_value") if data.get("stock_value") not in (None, "") else total, field="value at receipt")
        if svalue != total:
            raise InvalidInput("The value at receipt has to equal the amount of the gift.", "stock_value")
    source = clean_text(data.get("source"), field="source", max_len=30) or "manual"
    external_id = clean_text(data.get("external_id"), field="external id", max_len=80)
    return {
        "person_id": person_id, "gift_date": gift_date, "postmark_date": postmark, "gift_type": gift_type,
        "check_number": check_number, "memo": memo, "goods_value": goods, "amount": total, "splits": splits,
        "fee_amount": fee, "fee_covered_by_donor": covered, "in_kind_description": desc, "book_value": book,
        "stock_shares": shares, "stock_symbol": symbol or None, "stock_value": svalue, "source": source,
        "external_id": external_id,
    }


def check_duplicate(c, ctx: Ctx, line: dict, *, ignore_gift_id: int | None = None) -> None:
    """The classic entry mistake: the same check keyed twice. Same donor, check number and amount at this parish."""
    if not line.get("check_number") or line.get("person_id") is None:
        return
    c.execute("SELECT g.id, b.number FROM donor.gift g JOIN donor.batch b ON b.id = g.batch_id "
              "WHERE g.parish_id = %s AND g.person_id = %s AND g.check_number = %s AND g.amount = %s "
              "AND g.status <> 'voided' AND g.reverses_gift_id IS NULL AND g.id <> COALESCE(%s, -1) LIMIT 1",
              (ctx.parish_id, line["person_id"], line["check_number"], line["amount"], ignore_gift_id))
    row = c.fetchone()
    if row:
        raise Conflict(f"Check {line['check_number']} for ${money(line['amount'])} from this donor is already recorded in batch {row['number']}. "
                       "If it really is a second check, tick 'This is a different gift' and save again.")


# ── The one writer of a gift row ────────────────────────────────────────────────────────────────
_GIFT_COLS = ("person_id", "gift_date", "postmark_date", "gift_type", "check_number", "memo", "goods_value", "amount",
              "fee_amount", "fee_covered_by_donor", "in_kind_description", "book_value", "stock_shares", "stock_symbol",
              "stock_value", "source", "external_id")


def insert_gift_row(c, ctx: Ctx, batch_id: int, fields: dict, splits: list[dict], *, reverses_gift_id: int | None = None,
                    reclass_of_gift_id: int | None = None, replaces_gift_id: int | None = None,
                    correction_reason: str | None = None, kind: str = "create") -> int:
    """Insert one gift and its splits and log it. Splits may be negative here (a reversing gift). The caller has
    already validated and locked the batch. amount is recomputed from the splits so the two cannot disagree."""
    total = sum((Decimal(s["amount"]) for s in splits), ZERO)
    if not splits or total == ZERO:
        raise InvalidInput("A gift needs at least one split with an amount.")
    cols = {k: fields.get(k) for k in _GIFT_COLS}
    cols["amount"] = total
    cols.setdefault("goods_value", ZERO)
    if cols["goods_value"] is None:
        cols["goods_value"] = ZERO
    if cols["fee_amount"] is None:
        cols["fee_amount"] = ZERO
    if cols["fee_covered_by_donor"] is None:
        cols["fee_covered_by_donor"] = False
    c.execute(
        f"INSERT INTO donor.gift (parish_id, batch_id, entered_by_user_id, reverses_gift_id, reclass_of_gift_id, replaces_gift_id, correction_reason, "
        f"{', '.join(_GIFT_COLS)}) VALUES (%s,%s,%s,%s,%s,%s,%s,{', '.join(['%s'] * len(_GIFT_COLS))}) RETURNING id",
        (ctx.parish_id, batch_id, ctx.user_id, reverses_gift_id, reclass_of_gift_id, replaces_gift_id, correction_reason, *[cols[k] for k in _GIFT_COLS]))
    gid = c.fetchone()["id"]
    for s in splits:
        c.execute("INSERT INTO donor.gift_split (gift_id, fund_id, amount) VALUES (%s,%s,%s)", (gid, s["fund_id"], Decimal(s["amount"]).quantize(Decimal("0.01"))))
    names = ", ".join(s.get("fund_name") or f"fund {s['fund_id']}" for s in splits)
    log_change(c, ctx, "gift", gid, None, None, f"${money(total)} to {names}", person_id=fields.get("person_id"), kind=kind,
               scope="parish", reason=correction_reason)
    return gid


# ── Reads ───────────────────────────────────────────────────────────────────────────────────────
def attach_splits(c, gifts: list[dict]) -> list[dict]:
    if not gifts:
        return gifts
    c.execute("SELECT gs.gift_id, gs.fund_id, gs.amount, f.name AS fund_name FROM donor.gift_split gs JOIN donor.fund f ON f.id = gs.fund_id "
              "WHERE gs.gift_id = ANY(%s) ORDER BY gs.id", ([g["id"] for g in gifts],))
    by: dict[int, list[dict]] = {}
    for s in c.fetchall():
        by.setdefault(s["gift_id"], []).append(s)
    for g in gifts:
        g["splits"] = by.get(g["id"], [])
    return gifts


def batch_lines(c, batch_id: int, *, include_voided: bool = True) -> list[dict]:
    """Every line in a batch, oldest first, with its splits and the donor's name. The caller has already decided
    the viewer may see them."""
    c.execute("SELECT g.*, p.record_type, p.first_name, p.last_name, p.goes_by, p.org_name, p.is_placeholder "
              "FROM donor.gift g LEFT JOIN donor.person p ON p.id = g.person_id WHERE g.batch_id = %s"
              + ("" if include_voided else " AND g.status <> 'voided'") + " ORDER BY g.id", (batch_id,))
    gifts = c.fetchall()
    for g in gifts:
        g["donor_name"] = person_label(g) if g["person_id"] else "No donor (non-gift receipt)"
    return attach_splits(c, gifts)


def person_giving_summary(ctx: Ctx, person_id: int, *, year: int | None = None, today: dt.date | None = None) -> dict:
    """What this person has given at THIS parish, for the Giving tab. Closed batches only: a line in an open batch is
    not yet a recorded gift. Reversals are included as negative rows so the list shows what really happened, and a
    reversed gift's original row stays visible. Finance roles only."""
    need_read_gifts(ctx)
    today = today or dt.date.today()
    year = year or today.year
    with tx() as c:
        require_connection(c, ctx, person_id, include_archived=True)
        c.execute(
            "SELECT g.id AS gift_id, g.gift_date, g.gift_type, g.status, g.check_number, g.goods_value, g.amount AS gift_amount, "
            "       b.id AS batch_id, b.number AS batch_number, f.name AS fund_name, gs.amount AS amount "
            "  FROM donor.gift g JOIN donor.batch b ON b.id = g.batch_id AND b.status IN ('closed', 'reconciled') "
            "  JOIN donor.gift_split gs ON gs.gift_id = g.id JOIN donor.fund f ON f.id = gs.fund_id "
            " WHERE g.parish_id = %s AND g.person_id = %s AND g.status <> 'voided' "
            " ORDER BY g.gift_date DESC, g.id DESC, gs.id LIMIT 300", (ctx.parish_id, person_id))
        rows = c.fetchall()
        # Per gift (not per split). A reversal row carries the original's goods value, so the deductible amount
        # of a gift and its reversal cancel exactly: (X - G) + (-X + G) = 0.
        c.execute(
            "SELECT COALESCE(SUM(g.amount) FILTER (WHERE g.gift_type <> 'non_gift_receipt'), 0) AS given, "
            "       COALESCE(SUM(CASE WHEN g.amount >= 0 THEN g.amount - g.goods_value ELSE g.amount + g.goods_value END) "
            "                FILTER (WHERE g.gift_type IN ('tax_deductible', 'in_kind', 'stock')), 0) AS deductible "
            "  FROM donor.gift g JOIN donor.batch b ON b.id = g.batch_id AND b.status IN ('closed', 'reconciled') "
            " WHERE g.parish_id = %s AND g.person_id = %s AND g.status <> 'voided' AND EXTRACT(YEAR FROM g.gift_date) = %s",
            (ctx.parish_id, person_id, year))
        tot = c.fetchone()
        c.execute(
            "SELECT sc.amount, g.gift_date, f.name AS fund_name, p.first_name, p.last_name, p.org_name, p.record_type "
            "  FROM donor.soft_credit sc JOIN donor.gift g ON g.id = sc.gift_id JOIN donor.batch b ON b.id = g.batch_id AND b.status IN ('closed','reconciled') "
            "  JOIN donor.gift_split gs ON gs.gift_id = g.id JOIN donor.fund f ON f.id = gs.fund_id LEFT JOIN donor.person p ON p.id = g.person_id "
            " WHERE sc.parish_id = %s AND sc.person_id = %s AND g.status <> 'voided' ORDER BY g.gift_date DESC LIMIT 50", (ctx.parish_id, person_id))
        soft = c.fetchall()
        last = None
        for r in rows:
            if r["gift_amount"] > 0:
                last = {"amount": r["gift_amount"], "gift_date": r["gift_date"], "batch_number": r["batch_number"]}
                break
    try:
        import donor_pledges
        pledge = donor_pledges.current_pledge_summary(ctx, person_id, today=today)
    except ImportError:
        pledge = None
    given = tot["given"] or ZERO
    deductible = tot["deductible"] or ZERO
    return {"year": year, "year_total": given, "deductible_total": deductible, "last": last, "rows": rows,
            "pledge": pledge, "joint_with": (pledge or {}).get("joint_with"), "soft_credits": soft}


def report_totals_by_fund(ctx: Ctx, start: dt.date, end: dt.date) -> list[dict]:
    """Totals by fund over closed batches, by gift date. No donor is named, so the Finance View role may run it."""
    if not ctx.settings.get("giving_enabled"):
        raise PermissionDenied("Giving records are not turned on for this parish yet.")
    if not (ctx.can("totals.read") or can_read_gifts(ctx)):
        raise PermissionDenied("You do not have permission to see giving totals.")
    if end < start:
        raise InvalidInput("The end date cannot be before the start date.")
    with tx() as c:
        c.execute(
            "SELECT f.id AS fund_id, f.name AS fund_name, COALESCE(SUM(gs.amount), 0) AS total, COUNT(DISTINCT g.id) AS items "
            "  FROM donor.gift g JOIN donor.batch b ON b.id = g.batch_id AND b.status IN ('closed', 'reconciled') "
            "  JOIN donor.gift_split gs ON gs.gift_id = g.id JOIN donor.fund f ON f.id = gs.fund_id "
            " WHERE g.parish_id = %s AND g.status <> 'voided' AND g.gift_date BETWEEN %s AND %s "
            " GROUP BY f.id, f.name ORDER BY f.name", (ctx.parish_id, start, end))
        return c.fetchall()
