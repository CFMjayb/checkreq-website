"""
donor_batches.py -- Beacon Donor Management, Phase 2: gift batches, the lines in them, and the rules that close them.

Operations (each one function, each maps to a future tool): batch_list, batch_get, batch_open, batch_update_header,
batch_add_line, batch_update_line, batch_void_line, batch_close, batch_reopen, qbo_preview.

The rules this module enforces (handoff "Business rules", numbers match)
  1. A batch closes only when its actual amount and item count equal the expected figures and every line has a donor
     (or is a non-gift receipt). Correction batches are system-built and have no count sheet, so their expected
     figures are set to the actual ones when they close.
  2. A closed batch is never edited. Only an OPEN batch accepts, changes or voids a line. A line is voided, never deleted.
  3. batch_reopen is the one exception: Finance Supervisor only, a reason is required, who and why are logged, and the
     original entry is superseded (with a reversing entry if it had been posted).
  4. The person who closes a batch did not enter any of its lines, unless the parish has the single-person exception
     turned on, and then every such close is logged as an exception in batch_event.
  Also: every positive line must go to a fund that is still open at close; a gift counts as one item however many splits.

Who may do what (capabilities from donor_core.CAPS_BY_ROLE)
  batch.view   list batches and see a batch's totals          batch.open    open a batch, change its header
  batch.line   add, change and void lines of an OPEN batch    batch.close   close (Finance, Finance Supervisor)
  batch.reopen reopen a closed batch (Finance Supervisor)     giving.read   see the individual gifts of a CLOSED batch

Nothing here ever sends anything to QuickBooks. Closing builds and STORES the entry (donor_qbo_entry).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import db
import donor_funds as F
import donor_gifts as G
import donor_qbo_entry as Q
from donor_core import (
    Conflict, Ctx, InvalidInput, NotFound, PermissionDenied, check_enum, clean_text, diff_fields, log_change, need_giving,
    parse_date, to_bool, to_id, to_money, tx,
)

BATCH_KINDS = ("deposit", "non_deposit")
ZERO = Decimal("0.00")
LINE_LABELS = {"person_id": "Donor", "gift_date": "Gift date", "postmark_date": "Postmark date", "gift_type": "Gift type",
               "check_number": "Check number", "memo": "Memo", "goods_value": "Value of goods or services", "splits": "Funds",
               "fee_amount": "Processing fee", "fee_covered_by_donor": "Fee covered by donor", "in_kind_description": "In-kind description",
               "book_value": "Book value", "stock_shares": "Shares", "stock_symbol": "Stock symbol", "stock_value": "Value at receipt"}


# ── helpers ─────────────────────────────────────────────────────────────────────────────────────
def _need_view(ctx: Ctx) -> None:
    if not ctx.settings.get("giving_enabled"):
        raise PermissionDenied("Giving records are not turned on for this parish yet.")
    if not (ctx.can("batch.view") or ctx.can("totals.read") or G.can_read_gifts(ctx)):
        raise PermissionDenied("You do not have permission to see gift batches.")


def batch_row(c, ctx: Ctx, batch_id: int, *, lock: bool = False) -> dict:
    """The batch, only if it belongs to this parish. Anything else is 'not found'."""
    c.execute("SELECT * FROM donor.batch WHERE id = %s AND parish_id = %s" + (" FOR UPDATE" if lock else ""), (batch_id, ctx.parish_id))
    row = c.fetchone()
    if not row:
        raise NotFound("That batch was not found at this parish.")
    return row


def _event(c, ctx: Ctx, batch_id: int, kind: str, reason: str | None = None, detail: dict | None = None) -> None:
    from psycopg.types.json import Jsonb
    c.execute("INSERT INTO donor.batch_event (parish_id, batch_id, kind, user_id, reason, detail) VALUES (%s,%s,%s,%s,%s,%s)",
              (ctx.parish_id, batch_id, kind, ctx.user_id, reason, Jsonb(detail) if detail is not None else None))


_PARTICIPATION_EVENTS = ("update_line", "replace_line", "void_line", "header_change", "correction", "reopen")


def _participants(c, batch_id: int, lines: list[dict]) -> set:
    """Everyone who took part in this batch: who entered any line (voided or replaced ones too) plus whoever edited or voided
    a line, changed the count sheet, made a correction or reopened it. Opening the batch alone does not count."""
    people = {g["entered_by_user_id"] for g in lines}
    c.execute("SELECT DISTINCT user_id FROM donor.batch_event WHERE batch_id = %s AND kind = ANY(%s) AND user_id IS NOT NULL",
              (batch_id, list(_PARTICIPATION_EVENTS)))
    people |= {r["user_id"] for r in c.fetchall()}
    return people


def balance(c, batch: dict) -> dict:
    """Actual versus expected. Voided lines do not count. A reversing line is negative, so a correction batch nets."""
    c.execute("SELECT COALESCE(SUM(amount), 0) AS a, COUNT(*) AS n FROM donor.gift WHERE batch_id = %s AND status <> 'voided'", (batch["id"],))
    r = c.fetchone()
    actual, count = r["a"], r["n"]
    ea, ec = batch.get("expected_amount"), batch.get("expected_count")
    if batch["is_correction"]:
        balanced = count > 0
        ad, cd = None, None
    else:
        ad = None if ea is None else actual - ea
        cd = None if ec is None else count - ec
        balanced = ea is not None and ec is not None and ad == ZERO and cd == 0 and count > 0
    return {"actual_amount": actual, "actual_count": count, "expected_amount": ea, "expected_count": ec,
            "amount_diff": ad, "count_diff": cd, "balanced": balanced}


def new_batch(c, ctx: Ctx, *, kind: str, deposit_date: dt.date, is_correction: bool = False, default_fund_id: int | None = None,
              default_gift_type: str | None = None, expected_amount=None, expected_count=None, deposit_ref=None, cash_account=None,
              settles_to_diocese: bool = False, memo=None, source: str = "manual") -> dict:
    """Allocate the next batch number for this parish and insert an open batch. Shared with donor_corrections."""
    c.execute("INSERT INTO donor.parish_counter (parish_id, name, value) VALUES (%s, 'batch', 1) "
              "ON CONFLICT (parish_id, name) DO UPDATE SET value = donor.parish_counter.value + 1 RETURNING value", (ctx.parish_id,))
    number = c.fetchone()["value"]
    c.execute(
        "INSERT INTO donor.batch (parish_id, number, kind, is_correction, deposit_date, deposit_ref, cash_account, settles_to_diocese, source, "
        "default_fund_id, default_gift_type, expected_amount, expected_count, memo, opened_by_user_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (ctx.parish_id, number, kind, is_correction, deposit_date, deposit_ref, cash_account, settles_to_diocese, source, default_fund_id,
         default_gift_type or ("tax_deductible" if kind == "deposit" else "in_kind"), expected_amount, expected_count, memo, ctx.user_id))
    row = c.fetchone()
    _event(c, ctx, row["id"], "open", detail={"kind": kind, "correction": is_correction, "expected_amount": str(expected_amount) if expected_amount is not None else None,
                                                "expected_count": expected_count})
    return row


# ── Reads ───────────────────────────────────────────────────────────────────────────────────────
def batch_list(ctx: Ctx, *, status: str | None = None, limit: int = 100) -> list[dict]:
    _need_view(ctx)
    with tx() as c:
        c.execute(
            "SELECT b.*, COALESCE(a.amt, 0) AS actual_amount, COALESCE(a.n, 0) AS actual_count "
            "  FROM donor.batch b LEFT JOIN (SELECT batch_id, SUM(amount) AS amt, COUNT(*) AS n FROM donor.gift WHERE status <> 'voided' GROUP BY batch_id) a ON a.batch_id = b.id "
            " WHERE b.parish_id = %s" + (" AND b.status = %s" if status else "") + " ORDER BY b.status = 'open' DESC, b.deposit_date DESC, b.number DESC LIMIT %s",
            (ctx.parish_id, status, limit) if status else (ctx.parish_id, limit))
        return c.fetchall()


def _entry_funds(c, ctx: Ctx) -> dict:
    c.execute("SELECT * FROM donor.fund WHERE parish_id = %s", (ctx.parish_id,))
    return {f["id"]: f for f in c.fetchall()}


def _build(c, ctx: Ctx, batch: dict, lines: list[dict]) -> dict:
    return Q.build_entry(batch, [g for g in lines if g["status"] != "voided"], _entry_funds(c, ctx), ctx.settings)


def _close_problems(c, ctx: Ctx, batch: dict, lines: list[dict]) -> list[str]:
    """Every reason this batch cannot close right now, in plain words. Empty means it can."""
    out: list[str] = []
    live = [g for g in lines if g["status"] != "voided"]
    if batch["status"] != "open":
        out.append(f"This batch is {batch['status']}, not open.")
        return out
    if not live:
        out.append("The batch has no lines.")
    bal = balance(c, batch)
    if not batch["is_correction"]:
        if bal["expected_amount"] is None or bal["expected_count"] is None:
            out.append("Enter the expected total and item count from the count sheet.")
        else:
            if bal["amount_diff"] != ZERO:
                d = bal["amount_diff"]
                out.append(f"The lines total ${G.money(bal['actual_amount'])} but the count sheet says ${G.money(bal['expected_amount'])} "
                           f"({'over' if d > 0 else 'short'} by ${G.money(abs(d))}).")
            if bal["count_diff"] != 0:
                n = bal["count_diff"]
                out.append(f"There are {bal['actual_count']} items but the count sheet says {bal['expected_count']} "
                           f"({'over' if n > 0 else 'short'} by {abs(n)}).")
    for g in live:
        if g["person_id"] is None and g["gift_type"] != "non_gift_receipt":
            out.append(f"Line {g['id']} has no donor.")
    positive_funds = {s["fund_id"] for g in live for s in g["splits"] if s["amount"] > 0}
    if positive_funds:
        c.execute("SELECT name FROM donor.fund WHERE id = ANY(%s) AND NOT is_open ORDER BY name", (list(positive_funds),))
        for f in c.fetchall():
            out.append(f"The fund '{f['name']}' has been closed since these gifts were entered. Void those lines or ask Finance to reopen the fund.")
    # Anyone who took part counts: entered a line (even one later voided or replaced), edited or voided a line, changed the
    # count sheet, made a correction, or reopened the batch. The closer has to be someone else.
    enterers = _participants(c, batch["id"], lines)
    if ctx.user_id in enterers and not ctx.settings.get("allow_single_person_batch"):
        out.append("A different person has to close this batch: you took part in entering or changing it. "
                   "(A parish where one person does both can ask the diocese to allow it.)")
    return out


def batch_get(ctx: Ctx, batch_id: int) -> dict:
    """The batch for its screen: header, balance, the lines the viewer may see, the event log, the stored entry
    and a live preview of the entry, and why it cannot close yet. Finance roles see every line of a closed batch;
    people who only enter gifts see the lines of an OPEN batch and the totals of a closed one."""
    _need_view(ctx)
    with tx() as c:
        batch = batch_row(c, ctx, batch_id)
        bal = balance(c, batch)
        # A correction batch holds closed gifts being reversed, so only people who may read gifts see its lines. A clerk who only
        # enters gifts sees the lines of an ordinary OPEN batch and nothing else.
        can_lines = G.can_read_gifts(ctx) or (ctx.can("batch.line") and batch["status"] == "open" and not batch["is_correction"])
        lines = G.batch_lines(c, batch_id) if can_lines else None
        c.execute("SELECT * FROM donor.batch_event WHERE batch_id = %s ORDER BY id", (batch_id,))
        events = c.fetchall()
        if not can_lines:                      # event reasons and details are free text that can name a donor or a gift
            events = [{**e, "reason": None, "detail": None} for e in events]
        entry = None
        if batch["qbo_entry_id"] and (ctx.can("batch.close") or G.can_read_gifts(ctx)):
            c.execute("SELECT * FROM donor.qbo_entry WHERE id = %s", (batch["qbo_entry_id"],))
            entry = c.fetchone()
        preview = _build(c, ctx, batch, lines) if (lines is not None and batch["status"] == "open" and lines) else None
        problems = _close_problems(c, ctx, batch, lines if lines is not None else G.batch_lines(c, batch_id)) if (batch["status"] == "open" and ctx.can("batch.close")) else None
        return {"batch": batch, "balance": bal, "lines": lines, "events": events, "entry": entry, "preview": preview,
                "close_problems": problems, "can_close": problems == [] if problems is not None else False}


def qbo_preview(ctx: Ctx, batch_id: int) -> dict:
    """The journal entry this batch would post if it closed now. Built from the live lines. Never sent."""
    need_giving(ctx, "batch.line")
    with tx() as c:
        batch = batch_row(c, ctx, batch_id)
        return _build(c, ctx, batch, G.batch_lines(c, batch_id))


# ── Opening and the header ──────────────────────────────────────────────────────────────────────
def batch_open(ctx: Ctx, data: dict, *, cur=None) -> dict:
    need_giving(ctx, "batch.open", "You do not have permission to open a batch.")
    kind = check_enum(data.get("kind") or "deposit", BATCH_KINDS, field="batch kind", allow_blank=False)
    today = dt.date.today()
    dep_date = parse_date(data.get("deposit_date"), field="deposit date", allow_future=False) or today
    gtype = check_enum(data.get("default_gift_type") or ("tax_deductible" if kind == "deposit" else "in_kind"), G.GIFT_TYPES, field="gift type", allow_blank=False)
    if gtype not in (G.DEPOSIT_TYPES if kind == "deposit" else G.NON_DEPOSIT_TYPES):
        raise InvalidInput("That gift type does not fit this kind of batch.", "default_gift_type")
    if data.get("expected_amount") in (None, ""):
        raise InvalidInput("Enter the expected total from the count sheet.", "expected_amount")
    exp_amt = to_money(data.get("expected_amount"), field="expected total")
    if exp_amt <= ZERO:
        raise InvalidInput("The expected total has to be more than zero.", "expected_amount")
    try:
        exp_n = int(str(data.get("expected_count")).strip())
    except (TypeError, ValueError):
        raise InvalidInput("Enter the number of items from the count sheet.", "expected_count")
    if exp_n < 1:
        raise InvalidInput("The expected item count has to be at least 1.", "expected_count")
    with tx(cur) as c:
        fund_id = None
        if data.get("default_fund_id") not in (None, "", 0, "0"):
            f = F.fund_row(c, ctx, to_id(data["default_fund_id"], field="default_fund_id", label="a fund"))
            if not f["is_open"]:
                raise InvalidInput(f"The fund '{f['name']}' is closed.", "default_fund_id")
            fund_id = f["id"]
        row = new_batch(c, ctx, kind=kind, deposit_date=dep_date, default_fund_id=fund_id, default_gift_type=gtype, expected_amount=exp_amt,
                        expected_count=exp_n, deposit_ref=clean_text(data.get("deposit_ref"), field="bank deposit reference", max_len=60),
                        cash_account=clean_text(data.get("cash_account"), field="cash account", max_len=80),
                        settles_to_diocese=to_bool(data.get("settles_to_diocese"), field="settles to the diocese"),
                        memo=clean_text(data.get("memo"), field="memo", max_len=200))
        log_change(c, ctx, "batch", row["id"], None, None, f"Batch {row['number']}", kind="create", scope="parish")
        return {"id": row["id"], "number": row["number"]}


def batch_update_header(ctx: Ctx, batch_id: int, changes: dict, *, cur=None) -> dict:
    """Change an OPEN batch's header. The expected figures are the count sheet, so changing them is logged with the old
    and new values in the batch's event log."""
    need_giving(ctx, "batch.open", "You do not have permission to change a batch.")
    new: dict = {}
    if "deposit_date" in changes:
        d = parse_date(changes["deposit_date"], field="deposit date", allow_future=False)
        if d is None:
            raise InvalidInput("A batch needs a deposit date.", "deposit_date")
        new["deposit_date"] = d
    if "expected_amount" in changes:
        a = to_money(changes["expected_amount"], field="expected total")
        if a <= ZERO:
            raise InvalidInput("The expected total has to be more than zero.", "expected_amount")
        new["expected_amount"] = a
    if "expected_count" in changes:
        try:
            n = int(str(changes["expected_count"]).strip())
        except (TypeError, ValueError):
            raise InvalidInput("Enter the number of items from the count sheet.", "expected_count")
        if n < 1:
            raise InvalidInput("The expected item count has to be at least 1.", "expected_count")
        new["expected_count"] = n
    for f, lim in (("deposit_ref", 60), ("cash_account", 80), ("memo", 200)):
        if f in changes:
            new[f] = clean_text(changes[f], field=f.replace("_", " "), max_len=lim)
    if "settles_to_diocese" in changes:
        new["settles_to_diocese"] = to_bool(changes["settles_to_diocese"], field="settles to the diocese")
    if not new:
        return {"id": batch_id, "changed": []}
    with tx(cur) as c:
        old = batch_row(c, ctx, batch_id, lock=True)
        if old["status"] != "open":
            raise InvalidInput("Only an open batch can be changed. A closed batch is corrected through the gift, or reopened by the Finance Supervisor.")
        if old["is_correction"] and ({"expected_amount", "expected_count"} & set(new)):
            raise InvalidInput("A correction batch has no count sheet.")
        diffs = diff_fields(old, new)
        if not diffs:
            return {"id": batch_id, "changed": []}
        c.execute(f"UPDATE donor.batch SET {', '.join(f'{k} = %s' for k, _, _ in diffs)}, updated_at = NOW() WHERE id = %s",
                  (*[v for _, _, v in diffs], batch_id))
        for k, o, n in diffs:
            log_change(c, ctx, "batch", batch_id, k, o, n, scope="parish")
        _event(c, ctx, batch_id, "header_change", detail={k: {"old": None if o is None else str(o), "new": None if n is None else str(n)} for k, o, n in diffs})
        return {"id": batch_id, "changed": [k for k, _, _ in diffs]}


# ── Lines ───────────────────────────────────────────────────────────────────────────────────────
def _open_batch_for_lines(c, ctx: Ctx, batch_id: int) -> dict:
    batch = batch_row(c, ctx, batch_id, lock=True)
    if batch["status"] != "open":
        raise InvalidInput(f"This batch is {batch['status']}. Only an open batch accepts changes. "
                           "A closed gift is corrected with a reversal, a return or a reclass.")
    if batch["is_correction"]:
        raise InvalidInput("A correction batch is built from the original gift (reverse, return or reclass). It does not take typed-in lines.")
    return batch


def batch_add_line(ctx: Ctx, batch_id: int, data: dict, *, cur=None) -> dict:
    need_giving(ctx, "batch.line", "You do not have permission to enter gifts.")
    with tx(cur) as c:
        batch = _open_batch_for_lines(c, ctx, batch_id)
        line = G.normalize_line(c, ctx, batch, data)
        if not to_bool(data.get("confirm_duplicate"), field="confirm_duplicate"):
            G.check_duplicate(c, ctx, line)
        gid = G.insert_gift_row(c, ctx, batch_id, line, line["splits"])
        return {"id": gid, "amount": line["amount"], "batch_id": batch_id}


def _current_as_data(g: dict, splits: list[dict]) -> dict:
    d = {k: g[k] for k in ("person_id", "gift_date", "postmark_date", "gift_type", "check_number", "memo", "goods_value", "fee_amount",
                           "fee_covered_by_donor", "in_kind_description", "book_value", "stock_shares", "stock_symbol", "stock_value",
                           "source", "external_id")}
    d["splits"] = [{"fund_id": s["fund_id"], "amount": s["amount"]} for s in splits]
    return d


def batch_update_line(ctx: Ctx, gift_id: int, changes: dict, *, cur=None) -> dict:
    need_giving(ctx, "batch.line", "You do not have permission to change gifts.")
    with tx(cur) as c:
        c.execute("SELECT * FROM donor.gift WHERE id = %s AND parish_id = %s FOR UPDATE", (gift_id, ctx.parish_id))
        g = c.fetchone()
        if not g:
            raise NotFound("That gift was not found at this parish.")
        batch = _open_batch_for_lines(c, ctx, g["batch_id"])
        if g["status"] != "recorded":
            raise InvalidInput(f"This line is {g['status']} and cannot be changed.")
        if g["reverses_gift_id"] or g["reclass_of_gift_id"]:
            raise InvalidInput("This line is part of a correction and cannot be edited.")
        G.attach_splits(c, [g])
        merged = _current_as_data(g, g["splits"])
        if "splits" in changes:
            merged["splits"] = changes["splits"]
        elif "fund_id" in changes or "amount" in changes:
            merged["splits"] = [{"fund_id": changes.get("fund_id", g["splits"][0]["fund_id"] if len(g["splits"]) == 1 else None),
                                 "amount": changes.get("amount", g["amount"] if len(g["splits"]) == 1 else None)}]
        for k, v in changes.items():
            if k in merged and k != "splits":
                merged[k] = v
        line = G.normalize_line(c, ctx, batch, merged)
        diffs = []
        for k in ("person_id", "gift_date", "postmark_date", "gift_type", "check_number", "memo", "goods_value", "fee_amount", "fee_covered_by_donor",
                  "in_kind_description", "book_value", "stock_shares", "stock_symbol", "stock_value"):
            if g[k] != line[k]:
                diffs.append((k, g[k], line[k]))
        old_split = sorted((s["fund_id"], s["amount"]) for s in g["splits"])
        new_split = sorted((s["fund_id"], s["amount"]) for s in line["splits"])
        splits_changed = old_split != new_split
        if not diffs and not splits_changed:
            return {"id": gift_id, "changed": []}
        if any(k in ("check_number", "person_id") for k, _, _ in diffs) or splits_changed:
            if not to_bool(changes.get("confirm_duplicate"), field="confirm_duplicate"):
                G.check_duplicate(c, ctx, line, ignore_gift_id=gift_id)
        if splits_changed:
            # The application role has no DELETE privilege, so a line's splits are never removed. A change to which
            # funds the money goes to (or how much) VOIDS this line with a reason and enters a replacement line, so
            # both stay on the record and the batch arithmetic only sees the live one.
            if g["external_id"]:
                raise InvalidInput("A gift that came from an import cannot have its amount or funds changed here. Void it and match the import again.")
            c.execute("UPDATE donor.gift SET status = 'voided', void_reason = 'Replaced by an edit', voided_at = NOW(), "
                      "voided_by_user_id = %s, updated_at = NOW() WHERE id = %s", (ctx.user_id, gift_id))
            new_id = G.insert_gift_row(c, ctx, g["batch_id"], line, line["splits"], kind="update")
            log_change(c, ctx, "gift", gift_id, LINE_LABELS["splits"], ", ".join(f"{f}:{G.money(a)}" for f, a in old_split),
                       ", ".join(f"{f}:{G.money(a)}" for f, a in new_split), person_id=line["person_id"], scope="parish",
                       reason=f"replaced by line {new_id}")
            _event(c, ctx, g["batch_id"], "replace_line", detail={"gift_id": gift_id, "new_gift_id": new_id})
            return {"id": new_id, "replaced_gift_id": gift_id, "changed": [k for k, _, _ in diffs] + ["splits"]}
        c.execute(f"UPDATE donor.gift SET {', '.join(f'{k} = %s' for k, _, _ in diffs)}, updated_at = NOW() WHERE id = %s",
                  (*[v for _, _, v in diffs], gift_id))
        for k, o, n in diffs:
            log_change(c, ctx, "gift", gift_id, LINE_LABELS.get(k, k), o, n, person_id=line["person_id"], scope="parish")
        _event(c, ctx, g["batch_id"], "update_line", detail={"gift_id": gift_id})
        return {"id": gift_id, "replaced_gift_id": None, "changed": [k for k, _, _ in diffs]}


def batch_void_line(ctx: Ctx, gift_id: int, reason: str, *, cur=None) -> dict:
    """Void a line of an OPEN batch. The row stays (status voided, with who and why). Voiding the reversing line of a
    correction puts the original gift back to recorded, and voiding one half of a reclass voids the other half."""
    need_giving(ctx, "batch.line", "You do not have permission to change gifts.")
    why = clean_text(reason, field="reason", max_len=200)
    if not why:
        raise InvalidInput("Say why the line is being voided.", "reason")
    with tx(cur) as c:
        c.execute("SELECT * FROM donor.gift WHERE id = %s AND parish_id = %s FOR UPDATE", (gift_id, ctx.parish_id))
        g = c.fetchone()
        if not g:
            raise NotFound("That gift was not found at this parish.")
        c.execute("SELECT * FROM donor.batch WHERE id = %s FOR UPDATE", (g["batch_id"],))
        batch = c.fetchone()
        if batch["status"] != "open":
            raise InvalidInput(f"This batch is {batch['status']}. A closed gift is corrected with a reversal, a return or a reclass.")
        if batch["is_correction"]:
            need_giving(ctx, "gift.correct", "Only Finance can void a line of a correction batch.")
        if g["status"] != "recorded":
            raise InvalidInput(f"This line is already {g['status']}.")
        targets = [g]
        if g["reclass_of_gift_id"]:
            c.execute("SELECT * FROM donor.gift WHERE batch_id = %s AND reclass_of_gift_id = %s AND status = 'recorded' AND id <> %s",
                      (g["batch_id"], g["reclass_of_gift_id"], g["id"]))
            targets += c.fetchall()
        if g["reverses_gift_id"]:           # a replacement gift entered with the reversal goes with it
            c.execute("SELECT * FROM donor.gift WHERE batch_id = %s AND replaces_gift_id = %s AND status = 'recorded'", (g["batch_id"], g["reverses_gift_id"]))
            targets += c.fetchall()
        voided = []
        for t in targets:
            c.execute("UPDATE donor.gift SET status = 'voided', void_reason = %s, voided_at = NOW(), voided_by_user_id = %s, updated_at = NOW() WHERE id = %s",
                      (why, ctx.user_id, t["id"]))
            log_change(c, ctx, "gift", t["id"], "Status", "recorded", "voided", person_id=t["person_id"], kind="void", scope="parish", reason=why)
            voided.append(t["id"])
            if t["reverses_gift_id"]:
                c.execute("UPDATE donor.gift SET status = 'recorded', correction_reason = NULL, corrected_at = NULL, corrected_by_user_id = NULL, "
                          "updated_at = NOW() WHERE id = %s AND status IN ('reversed', 'returned')", (t["reverses_gift_id"],))
                log_change(c, ctx, "gift", t["reverses_gift_id"], "Status", "reversed", "recorded", person_id=t["person_id"], kind="restore",
                           scope="parish", reason="The reversing line was voided")
        _event(c, ctx, g["batch_id"], "void_line", reason=why, detail={"gift_ids": voided})
        return {"voided": voided, "batch_id": g["batch_id"]}


# ── Closing and reopening ───────────────────────────────────────────────────────────────────────
def batch_close(ctx: Ctx, batch_id: int, *, cur=None) -> dict:
    """Close an open batch if, and only if, every rule is met (see the module docstring). Builds and STORES the QuickBooks
    entry. Sends nothing anywhere. Raises InvalidInput listing every reason when it cannot close."""
    need_giving(ctx, "batch.close", "Only Finance can close a batch.")
    with tx(cur) as c:
        batch = batch_row(c, ctx, batch_id, lock=True)
        lines = G.batch_lines(c, batch_id)
        problems = _close_problems(c, ctx, batch, lines)
        if problems:
            raise InvalidInput("This batch cannot be closed yet. " + " ".join(problems))
        live = [g for g in lines if g["status"] != "voided"]
        enterers = _participants(c, batch_id, lines)
        exception = ctx.user_id in enterers          # only reachable when the parish has the single-person exception on
        bal = balance(c, batch)
        exp_amt, exp_n = (bal["actual_amount"], bal["actual_count"]) if batch["is_correction"] else (batch["expected_amount"], batch["expected_count"])
        entry = _build(c, ctx, batch, lines)
        entry_id = Q.store_entry(c, ctx.parish_id, batch_id, entry, ctx.user_id, company_key=ctx.settings.get("qbo_company_key"))
        c.execute("UPDATE donor.batch SET status = 'closed', closed_by_user_id = %s, closed_at = NOW(), closed_under_exception = %s, "
                  "expected_amount = %s, expected_count = %s, qbo_entry_id = %s, updated_at = NOW() WHERE id = %s",
                  (ctx.user_id, exception, exp_amt, exp_n, entry_id, batch_id))
        log_change(c, ctx, "batch", batch_id, "Status", "open", "closed", kind="close", scope="parish")
        detail = {"items": len(live), "amount": str(bal["actual_amount"]), "entry_id": entry_id, "entry_status": entry["status"]}
        _event(c, ctx, batch_id, "close", detail=detail)
        if exception:
            _event(c, ctx, batch_id, "close_exception", reason="Closed by a person who also entered lines (single-person exception is on)", detail=detail)
        return {"id": batch_id, "number": batch["number"], "entry_id": entry_id, "entry_status": entry["status"], "under_exception": exception,
                "entry_problems": entry["problems"]}


def batch_reopen(ctx: Ctx, batch_id: int, reason: str, *, cur=None) -> dict:
    """The one exception to 'a closed batch is never edited'. Finance Supervisor only. A reason is required and is logged
    with who. The stored entry is superseded, and if it had been posted a reversing entry is stored for it."""
    need_giving(ctx, "batch.reopen", "Only a Finance Supervisor can reopen a closed batch.")
    why = clean_text(reason, field="reason", max_len=300)
    if not why:
        raise InvalidInput("Say why the batch is being reopened. The reason is kept with your name.", "reason")
    with tx(cur) as c:
        batch = batch_row(c, ctx, batch_id, lock=True)
        if batch["status"] == "reconciled":
            raise InvalidInput("This batch has been matched to a bank statement and cannot be reopened.")
        if batch["status"] != "closed":
            raise InvalidInput(f"Only a closed batch can be reopened. This one is {batch['status']}.")
        c.execute("SELECT 1 AS x FROM donor.gift orig JOIN donor.gift corr ON (corr.reverses_gift_id = orig.id OR corr.reclass_of_gift_id = orig.id) "
                  "AND corr.status <> 'voided' WHERE orig.batch_id = %s LIMIT 1", (batch_id,))
        if c.fetchone():
            raise Conflict("Gifts in this batch have already been corrected. Reopening it would make the corrections add up wrongly. "
                           "Void the correction lines first.")
        reversal_id = None
        if batch["qbo_entry_id"]:
            c.execute("SELECT * FROM donor.qbo_entry WHERE id = %s FOR UPDATE", (batch["qbo_entry_id"],))
            old = c.fetchone()
            if old and old["status"] == "posted":
                rev = Q.reversal_of(old)
                reversal_id = Q.store_entry(c, ctx.parish_id, batch_id, rev, ctx.user_id, reverses_entry_id=old["id"], company_key=old["qbo_company_key"])
            if old:
                c.execute("UPDATE donor.qbo_entry SET status = 'superseded' WHERE id = %s", (old["id"],))
        c.execute("UPDATE donor.batch SET status = 'open', closed_by_user_id = NULL, closed_at = NULL, closed_under_exception = FALSE, "
                  "qbo_entry_id = NULL, reopened_count = reopened_count + 1, updated_at = NOW() WHERE id = %s", (batch_id,))
        log_change(c, ctx, "batch", batch_id, "Status", "closed", "open", kind="reopen", scope="parish", reason=why)
        _event(c, ctx, batch_id, "reopen", reason=why, detail={"reversing_entry_id": reversal_id, "reopened_count": batch["reopened_count"] + 1})
        return {"id": batch_id, "reversing_entry_id": reversal_id}
