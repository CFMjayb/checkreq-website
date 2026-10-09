"""
donor_qbo_entry.py -- Beacon Donor Management, Phase 2: the QuickBooks journal entry for a batch.

BUILT, SHOWN AND STORED. NEVER SENT. Posting to QuickBooks is switched off in three independent ways and stays off
until someone deliberately changes all of them:
  1. POSTING_AVAILABLE below is False, and no poster function exists in this module or anywhere that imports it
  2. donor.parish_settings.qbo_posting_enabled is false by default and only the diocese can change it
  3. post_entry() raises PostingDisabled unconditionally while POSTING_AVAILABLE is False

The builder is a set of PURE functions (no database, no network), so every shape of entry is unit-tested.

Shapes (the handoff's "QBO posting" section)
  deposit batch      Dr the batch's cash account for the net of the batch
                     Dr the processing-fee expense account for fees the parish pays (processor deposits)
                     Cr each fund's income account (or liability account for a pass-through fund) at gross, with the
                       fund's QuickBooks class
  non-deposit batch  Dr the investment account for stock at value received, Dr the in-kind account for in-kind gifts at
                     book value, Cr each fund as above
  reversal / return  the same lines with the signs flipped (a reversing gift carries negative splits)
  reclass            Dr the old fund's income account, Cr the new fund's income account, no cash line
  settles to diocese Dr the "due from the diocese" account instead of cash (an open question for Jay, built as a flag)
  reopen             a REVERSING entry (every line's debit and credit swapped), only when the original was posted

Lines carry the fund name as the description. A donor's name never appears in an entry. Amounts are Decimal; the stored
JSON holds them as two-decimal strings, and entry_payload() converts to the numbers qbo-mcp-server's
post_journal_entry takes ({txn_date, doc_number, private_note, lines:[{acct_num, debit, credit, class_name, description}]}).
Finding for Jay: that endpoint takes no class on a line today, so a small server extension is needed before the first real post.
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

from donor_core import DonorError

POSTING_AVAILABLE = False          # never flipped by the application. See the module docstring.
ZERO = Decimal("0.00")


class PostingDisabled(DonorError):
    pass


def q(v) -> Decimal:
    return Decimal(v).quantize(Decimal("0.01"))


def _line(acct, debit, credit, cls, desc) -> dict:
    return {"acct_num": acct, "debit": format(q(debit), "f"), "credit": format(q(credit), "f"), "class_name": cls, "description": desc}


def build_entry(batch: dict, gifts: list[dict], funds: dict, settings: dict, *, today: dt.date | None = None) -> dict:
    """Pure. `gifts` are the batch's non-voided gifts, each with `splits` [{fund_id, amount}] (negative for a reversal),
    `gift_type`, `fee_amount`, `fee_covered_by_donor`, `reclass_of_gift_id`, `reverses_gift_id`. `funds` maps
    fund_id -> fund row. Returns {kind, txn_date, doc_number, private_note, lines, total_debit, total_credit, problems,
    status}. Raises DonorError if the lines do not balance (which would be a bug here, never bad data)."""
    today = today or dt.date.today()
    problems: list[str] = []
    live = [g for g in gifts if g.get("status") != "voided"]
    kind = "deposit" if batch["kind"] == "deposit" else "non_deposit"
    if batch.get("is_correction"):
        kind = "reclass" if live and all(g.get("reclass_of_gift_id") for g in live) else "correction"

    signed: dict[tuple, Decimal] = {}          # (account, class, description) -> debit positive, credit negative

    def add(acct, cls, desc, amount):
        key = (acct, cls, desc)
        signed[key] = signed.get(key, ZERO) + Decimal(amount)

    cash_acct = batch.get("cash_account") or settings.get("default_cash_account")
    if batch.get("settles_to_diocese"):
        cash_acct = settings.get("due_from_diocese_account")
        cash_desc = "Due from the diocese"
    else:
        cash_desc = f"Batch {batch['number']} deposit"
    cash_total = stock_total = in_kind_total = fee_total = ZERO
    fund_totals: dict[int, Decimal] = {}
    for g in live:
        amount = sum((Decimal(s["amount"]) for s in g["splits"]), ZERO)
        for s in g["splits"]:
            fund_totals[s["fund_id"]] = fund_totals.get(s["fund_id"], ZERO) + Decimal(s["amount"])
        if g["gift_type"] == "stock":
            stock_total += amount
        elif g["gift_type"] == "in_kind":
            in_kind_total += amount
        else:
            fee = Decimal(g.get("fee_amount") or 0)
            if fee and not g.get("fee_covered_by_donor"):
                fee_total += fee
                cash_total += amount - fee
            else:
                cash_total += amount        # no fee, or the donor covered it (it never reaches the books)
    if cash_total != ZERO:
        add(cash_acct, None, cash_desc, cash_total)
        if not cash_acct:
            problems.append("The cash account is not set. Set a default cash account in Settings or on the batch."
                            if not batch.get("settles_to_diocese") else "The 'due from the diocese' account is not set in Settings.")
    if fee_total != ZERO:
        fee_acct = settings.get("processing_fee_account")
        add(fee_acct, None, "Processing fees", fee_total)
        if not fee_acct:
            problems.append("The processing-fee expense account is not set in Settings.")
    if stock_total != ZERO:
        inv = settings.get("investment_account")
        add(inv, None, "Stock received", stock_total)
        if not inv:
            problems.append("The investment account for stock is not set in Settings.")
    if in_kind_total != ZERO:
        ik = settings.get("in_kind_account")
        add(ik, None, "In-kind gifts", in_kind_total)
        if not ik:
            problems.append("The in-kind gift account is not set in Settings.")
    for fund_id, total in fund_totals.items():
        f = funds.get(fund_id) or {}
        acct = f.get("liability_account") or f.get("income_account")
        cls = f.get("qbo_class") or settings.get("default_class")
        if not acct:
            problems.append(f"The fund '{f.get('name', fund_id)}' has no income account.")
        add(acct, cls, f.get("name") or f"Fund {fund_id}", -total)

    lines = []
    for (acct, cls, desc), amt in sorted(signed.items(), key=lambda kv: (kv[1] < 0, str(kv[0][0] or ""), kv[0][2])):
        if amt == ZERO:
            continue
        lines.append(_line(acct, amt if amt > 0 else ZERO, -amt if amt < 0 else ZERO, cls, desc))
    debit = sum((Decimal(l["debit"]) for l in lines), ZERO)
    credit = sum((Decimal(l["credit"]) for l in lines), ZERO)
    if debit != credit:
        raise DonorError(f"The journal entry for batch {batch['number']} does not balance (debits {debit}, credits {credit}).")
    doc = f"BAT-{int(batch['number']):05d}"
    note = f"Beacon batch {batch['number']}, {len(live)} item{'s' if len(live) != 1 else ''}, dated {batch['deposit_date']}"
    if batch.get("is_correction"):
        note += ", corrections"
    return {"kind": kind, "txn_date": batch["deposit_date"], "doc_number": doc, "private_note": note, "lines": lines,
            "total_debit": debit, "total_credit": credit, "problems": problems,
            "status": "incomplete" if problems else "built"}


def reversal_of(entry: dict, *, doc_suffix: str = "-R") -> dict:
    """Pure. The reversing entry for a stored entry: every line's debit and credit swapped."""
    lines = [{**l, "debit": l["credit"], "credit": l["debit"]} for l in entry["lines"]]
    return {"kind": "reversal", "txn_date": entry["txn_date"], "doc_number": entry["doc_number"] + doc_suffix,
            "private_note": "Reversal of " + entry["doc_number"] + (" (batch reopened)"), "lines": lines,
            "total_debit": entry["total_credit"], "total_credit": entry["total_debit"], "problems": [], "status": "built"}


def entry_payload(entry: dict) -> dict:
    """The dict qbo-mcp-server's post_journal_entry takes. For display and tests tonight. Nothing calls the server."""
    lines = entry["lines"] if not isinstance(entry["lines"], str) else json.loads(entry["lines"])
    return {"txn_date": str(entry["txn_date"]), "doc_number": entry["doc_number"], "private_note": entry["private_note"],
            "lines": [{"acct_num": l["acct_num"], "debit": float(l["debit"]), "credit": float(l["credit"]),
                       "class_name": l["class_name"], "description": l["description"]} for l in lines]}


def post_entry(*_args, **_kwargs):
    """There is no poster. This exists so a caller that tries gets a clear refusal instead of a silent no-op."""
    raise PostingDisabled("Posting to QuickBooks is switched off. The entry is built and stored but is never sent.")


def store_entry(c, parish_id: int, batch_id: int, entry: dict, user_id: int, *, reverses_entry_id: int | None = None,
                company_key: str | None = None) -> int:
    from psycopg.types.json import Jsonb
    c.execute(
        "INSERT INTO donor.qbo_entry (parish_id, batch_id, kind, txn_date, doc_number, private_note, lines, total_debit, total_credit, "
        "problems, status, reverses_entry_id, qbo_company_key, created_by_user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (parish_id, batch_id, entry["kind"], entry["txn_date"], entry["doc_number"], entry["private_note"], Jsonb(entry["lines"]),
         entry["total_debit"], entry["total_credit"], Jsonb(entry["problems"]), entry["status"], reverses_entry_id, company_key, user_id))
    return c.fetchone()["id"]
