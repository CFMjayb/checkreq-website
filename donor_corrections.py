"""
donor_corrections.py -- Beacon Donor Management, Phase 2: correcting a CLOSED gift.

A closed batch is never edited (rule 2). To correct a gift in one, Finance (capability gift.correct) uses one of:
  gift_reverse   an entry error. The original is marked 'reversed', a reversing gift (negative splits) is added, and a
                 corrected replacement gift can be added with it.
  gift_return    an NSF check or ACH reject. Same mechanics, the original is marked 'returned'.
  gift_reclass   money moved between funds with no cash. Two new gifts in the correction batch: a negative one that takes
                 the money out of the old funds and a positive one that puts it in the new funds. The original stays
                 'recorded', so the donor's total does not change, only where it sits.

Every correction row goes into an OPEN correction batch dated the day of the correction (one open correction batch per
parish, day and kind; it is created when needed). The correction batch has no count sheet. It closes like any batch, so
the person who made the corrections cannot close it unless the parish has the single-person exception (rule 4), and that
close is what builds the entry (a reversal's lines are the original's with the signs flipped, a reclass has no cash line).
That is how 'Finance Supervisor approves reversals' is delivered: maker and checker are different people.

A correction row keeps the ORIGINAL gift's date, so the statement year and pledge period of the gift and of its reversal
are the same and they cancel exactly. Voiding a reversing line in the open correction batch puts the original back to
'recorded' (see donor_batches.batch_void_line).

Only a gift line (a positive amount, status recorded, in a closed batch, with no live correction against it) can be
corrected. To fix a reclass, correct the positive line it created.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import donor_batches as B
import donor_gifts as G
import donor_noncontrib as NC
from donor_core import (
    Conflict, Ctx, InvalidInput, NotFound, clean_text, log_change, need_giving, to_money, tx,
)

ZERO = Decimal("0.00")
_COPY = ("person_id", "gift_date", "postmark_date", "gift_type", "check_number", "memo", "goods_value", "in_kind_description")


def _reason(reason) -> str:
    why = clean_text(reason, field="reason", max_len=300)
    if not why:
        raise InvalidInput("Say why the gift is being corrected. The reason is kept with your name.", "reason")
    return why


def _target(c, ctx: Ctx, gift_id: int) -> tuple[dict, dict]:
    c.execute("SELECT * FROM donor.gift WHERE id = %s AND parish_id = %s FOR UPDATE", (gift_id, ctx.parish_id))
    g = c.fetchone()
    if not g:
        raise NotFound("That gift was not found at this parish.")
    c.execute("SELECT * FROM donor.batch WHERE id = %s", (g["batch_id"],))
    batch = c.fetchone()
    if batch["status"] not in ("closed", "reconciled"):
        raise InvalidInput("This gift is still in an open batch. Void the line there instead of correcting it.")
    if g["amount"] <= ZERO:
        raise InvalidInput("This line is itself part of a correction. Correct the original gift or its replacement instead.")
    if g["status"] != "recorded":
        raise InvalidInput(f"This gift is already {g['status']}.")
    c.execute("SELECT 1 AS x FROM donor.gift WHERE (reverses_gift_id = %s OR reclass_of_gift_id = %s) AND status <> 'voided' LIMIT 1", (gift_id, gift_id))
    if c.fetchone():
        raise Conflict("This gift already has a correction. If it was reclassified, correct the reclassified line instead.")
    G.attach_splits(c, [g])
    return g, batch


def _correction_batch(c, ctx: Ctx, kind: str) -> dict:
    today = dt.date.today()
    c.execute("SELECT * FROM donor.batch WHERE parish_id = %s AND is_correction AND status = 'open' AND kind = %s AND deposit_date = %s "
              "ORDER BY id LIMIT 1 FOR UPDATE", (ctx.parish_id, kind, today))
    row = c.fetchone()
    if row:
        return row
    return B.new_batch(c, ctx, kind=kind, deposit_date=today, is_correction=True, source="correction",
                       default_gift_type="tax_deductible" if kind == "deposit" else "in_kind", memo="Corrections")


def _fields_from(g: dict, sign: int) -> dict:
    f = {k: g[k] for k in _COPY}
    f.update({"fee_amount": ZERO, "fee_covered_by_donor": False, "source": "correction", "external_id": None,
              "book_value": None if g["book_value"] is None else sign * g["book_value"],
              "stock_shares": None if g["stock_shares"] is None else sign * g["stock_shares"],
              "stock_symbol": g["stock_symbol"],
              "stock_value": None if g["stock_value"] is None else sign * g["stock_value"]})
    return f


def _mark_and_reverse(ctx: Ctx, gift_id: int, reason, new_status: str, replacement: dict | None, cur) -> dict:
    need_giving(ctx, "gift.correct", "Only Finance can correct a gift.")
    why = _reason(reason)
    with tx(cur) as c:
        g, batch = _target(c, ctx, gift_id)
        cb = _correction_batch(c, ctx, batch["kind"])
        neg = [{"fund_id": s["fund_id"], "gl_account_id": s.get("gl_account_id"), "amount": -Decimal(s["amount"]), "fund_name": s["fund_name"],
                "gl_label": s.get("gl_label")} for s in g["splits"]]
        rid = G.insert_gift_row(c, ctx, cb["id"], _fields_from(g, -1), neg, reverses_gift_id=gift_id, correction_reason=why,
                                kind="reverse" if new_status == "reversed" else "return")
        c.execute("UPDATE donor.gift SET status = %s, correction_reason = %s, corrected_at = NOW(), corrected_by_user_id = %s, updated_at = NOW() WHERE id = %s",
                  (new_status, why, ctx.user_id, gift_id))
        log_change(c, ctx, "gift", gift_id, "Status", "recorded", new_status, person_id=g["person_id"],
                   kind="reverse" if new_status == "reversed" else "return", scope="parish", reason=why)
        out = {"reversal_gift_id": rid, "replacement_gift_id": None, "batch_id": cb["id"], "batch_number": cb["number"]}
        if replacement:
            base = {"person_id": g["person_id"], "gift_date": g["gift_date"], "gift_type": g["gift_type"], "check_number": g["check_number"],
                    "memo": g["memo"], "in_kind_description": g["in_kind_description"]}
            line = G.normalize_line(c, ctx, cb, {**base, **replacement, "source": "correction"})
            out["replacement_gift_id"] = G.insert_gift_row(c, ctx, cb["id"], line, line["splits"], replaces_gift_id=gift_id,
                                                           correction_reason=why, kind="create")
        B._event(c, ctx, cb["id"], "correction", reason=why, detail={"original_gift_id": gift_id, "action": new_status, "reversal_gift_id": rid,
                                                                     "replacement_gift_id": out["replacement_gift_id"]})
        return out


def gift_reverse(ctx: Ctx, gift_id: int, reason: str, replacement: dict | None = None, *, cur=None) -> dict:
    """An entry error. `replacement`, if given, is a corrected gift ({splits or fund_id+amount, and any field to change})."""
    return _mark_and_reverse(ctx, gift_id, reason, "reversed", replacement, cur)


def gift_return(ctx: Ctx, gift_id: int, reason: str, *, cur=None) -> dict:
    """A check that bounced or an ACH debit that was rejected. No replacement: the money did not arrive."""
    return _mark_and_reverse(ctx, gift_id, reason, "returned", None, cur)


def gift_reclass(ctx: Ctx, gift_id: int, new_splits: list[dict], reason: str, *, cur=None) -> dict:
    """Move a closed gift's money to other funds. No cash moves. The new splits must add up to the gift exactly."""
    need_giving(ctx, "gift.correct", "Only Finance can correct a gift.")
    why = _reason(reason)
    with tx(cur) as c:
        g, batch = _target(c, ctx, gift_id)
        cb = _correction_batch(c, ctx, batch["kind"])
        checked, seen = [], set()
        is_nc = g["gift_type"] == "non_gift_receipt"
        for s in new_splits or []:
            if is_nc:
                # A non-gift receipt moves between GL accounts, never into a fund. A fund that came along (the form always has one) is ignored.
                try:
                    aid = int(s.get("gl_account_id"))
                except (TypeError, ValueError):
                    raise InvalidInput("Pick the GL account for every amount.", "gl_account_id")
                if aid in seen:
                    raise InvalidInput("List each GL account once.", "gl_account_id")
                seen.add(aid)
                amt = to_money(s.get("amount"), field="amount")
                if amt <= ZERO:
                    raise InvalidInput("Every amount must be more than zero.", "amount")
                acct = NC.allowed_account(c, ctx, aid)
                checked.append({"fund_id": None, "gl_account_id": aid, "amount": amt, "fund_name": None, "gl_label": NC.account_label(acct)})
                continue
            if s.get("gl_account_id") not in (None, "") and s.get("fund_id") in (None, ""):
                raise InvalidInput("Only a non-gift receipt is coded to a GL account. Pick a fund.", "fund_id")
            try:
                fid = int(s.get("fund_id"))
            except (TypeError, ValueError):
                raise InvalidInput("Pick a fund for every amount.", "fund_id")
            if fid in seen:
                raise InvalidInput("List each fund once.", "fund_id")
            seen.add(fid)
            amt = to_money(s.get("amount"), field="amount")
            if amt <= ZERO:
                raise InvalidInput("Every amount must be more than zero.", "amount")
            c.execute("SELECT id, name, is_open FROM donor.fund WHERE id = %s AND parish_id = %s", (fid, ctx.parish_id))
            f = c.fetchone()
            if not f:
                raise NotFound("That fund was not found at this parish.")
            if not f["is_open"]:
                raise InvalidInput(f"The fund '{f['name']}' is closed and takes no new money.", "fund_id")
            checked.append({"fund_id": fid, "gl_account_id": None, "amount": amt, "fund_name": f["name"], "gl_label": None})
        if not checked:
            raise InvalidInput("Say which GL accounts the money should go to." if is_nc else "Say which funds the money should go to.", "gl_account_id" if is_nc else "fund_id")
        total = sum((s["amount"] for s in checked), ZERO)
        if total != g["amount"]:
            raise InvalidInput(f"The new funds add up to ${G.money(total)} but the gift is ${G.money(g['amount'])}. A reclass moves the whole gift.", "amount")
        key = lambda x: (x["fund_id"] or 0, x.get("gl_account_id") or 0, x["amount"])
        if sorted(map(key, checked)) == sorted(map(key, g["splits"])):
            raise InvalidInput("Those are the GL accounts the receipt is already in." if is_nc else "Those are the funds the gift is already in.",
                               "gl_account_id" if is_nc else "fund_id")
        out_splits = [{"fund_id": s["fund_id"], "gl_account_id": s.get("gl_account_id"), "amount": -Decimal(s["amount"]), "fund_name": s["fund_name"],
                       "gl_label": s.get("gl_label")} for s in g["splits"]]
        out_id = G.insert_gift_row(c, ctx, cb["id"], _fields_from(g, -1), out_splits, reclass_of_gift_id=gift_id, correction_reason=why, kind="reclass")
        in_id = G.insert_gift_row(c, ctx, cb["id"], _fields_from(g, 1), checked, reclass_of_gift_id=gift_id, correction_reason=why, kind="reclass")
        log_change(c, ctx, "gift", gift_id, "GL accounts" if is_nc else "Funds", ", ".join(f"{s['target_name']}:{G.money(s['amount'])}" for s in g["splits"]),
                   ", ".join(f"{s['fund_name'] or s['gl_label']}:{G.money(s['amount'])}" for s in checked), person_id=g["person_id"], kind="reclass", scope="parish", reason=why)
        B._event(c, ctx, cb["id"], "correction", reason=why, detail={"original_gift_id": gift_id, "action": "reclass", "out_gift_id": out_id, "in_gift_id": in_id})
        return {"out_gift_id": out_id, "in_gift_id": in_id, "batch_id": cb["id"], "batch_number": cb["number"]}
