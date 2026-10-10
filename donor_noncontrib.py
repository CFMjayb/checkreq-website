"""
donor_noncontrib.py -- Beacon Donor Management: non-contribution receipts need a GL account (Jay, 2026-10-10).

A parish takes in money that is not a gift: rent, a reimbursement, event tickets, money passed through to someone else. That money
cannot go to a fund, so a line of type "non-gift receipt" carries a GL ACCOUNT instead. This module owns the list of GL accounts a
parish allows for such lines and the reads that show them; donor_gifts validates a line against it, donor_qbo_entry credits the
account, and nothing here talks to QuickBooks.

Operations (each one function, each maps to a future tool): noncontribution_accounts_set, noncontribution_accounts_list,
uncoded_noncontributions, report_noncontributions, batch_noncontribution_summary.

Rules
  * The list belongs to one parish. An account id from another parish reads as "not found".
  * An account is turned off, never deleted. A line already coded to it keeps pointing at it.
  * At most one account is the parish's default, and the default has to be on.
  * Setting the list needs funds.manage (Finance). Reading it needs any giving role, because the entry screen needs it.
  * Non-contributions are never gifts: giving statements, tax receipts, pledge progress and giving totals leave them out (those
    queries say so), and they show here as their own section instead.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from donor_core import Ctx, InvalidInput, NotFound, PermissionDenied, clean_text, log_change, need_giving, tx

ZERO = Decimal("0.00")
MAX_ACCOUNTS = 60


def _can_read(ctx: Ctx) -> bool:
    return any(ctx.can(c) for c in ("batch.view", "batch.line", "funds.manage", "totals.read", "pledges.manage", "giving.read"))


def _need_read(ctx: Ctx) -> None:
    if not ctx.settings.get("giving_enabled"):
        raise PermissionDenied("Giving records are not turned on for this parish yet.")
    if not _can_read(ctx):
        raise PermissionDenied("You do not have permission to see the non-contribution accounts.")


def account_label(row: dict | None) -> str:
    """'4150 Rental income' for a row that has number and name, a plain note for a line with none."""
    if not row or not row.get("account_number"):
        return "GL account needed"
    return f"{row['account_number']} {row['account_name']}".strip()


# ── Reads ───────────────────────────────────────────────────────────────────────────────────────
def noncontribution_accounts_list(ctx: Ctx, *, include_inactive: bool = False) -> list[dict]:
    """The accounts this parish allows for non-contributions, the default first. Inactive ones only when asked for."""
    _need_read(ctx)
    with tx() as c:
        return accounts_for(c, ctx.parish_id, include_inactive=include_inactive)


def accounts_for(c, parish_id: int, *, include_inactive: bool = False) -> list[dict]:
    c.execute("SELECT * FROM donor.noncontribution_account WHERE parish_id = %s" + ("" if include_inactive else " AND is_active") +
              " ORDER BY is_active DESC, is_default DESC, account_number", (parish_id,))
    return c.fetchall()


def default_account(c, parish_id: int) -> dict | None:
    c.execute("SELECT * FROM donor.noncontribution_account WHERE parish_id = %s AND is_default AND is_active", (parish_id,))
    return c.fetchone()


def account_row(c, ctx: Ctx, account_id: int, *, lock: bool = False) -> dict:
    """The account, only if it belongs to this parish. Anything else is 'not found'."""
    c.execute("SELECT * FROM donor.noncontribution_account WHERE id = %s AND parish_id = %s" + (" FOR UPDATE" if lock else ""), (account_id, ctx.parish_id))
    row = c.fetchone()
    if not row:
        raise NotFound("That GL account was not found at this parish.")
    return row


def accounts_by_id(c, parish_id: int) -> dict:
    """Every account (on or off) of the parish by id, for the QuickBooks entry builder."""
    return {a["id"]: a for a in accounts_for(c, parish_id, include_inactive=True)}


# ── noncontribution_accounts_set ────────────────────────────────────────────────────────────────
def noncontribution_accounts_set(ctx: Ctx, accounts: list[dict], default_account_number: str | None = None, *, cur=None) -> dict:
    """Set the parish's list of GL accounts for non-contributions, and which one is the default.

    `accounts` is the WHOLE list the parish wants on: [{account_number, account_name, qbo_account_id (optional)}]. An account on the
    list is created, or updated and turned on. An account the parish already has that is not on the list is turned off (it is never
    deleted, lines already coded to it keep it). `default_account_number` names the default (one of the accounts on the list), or is
    blank for no default. Every change is logged. Returns counts."""
    need_giving(ctx, "funds.manage", "Only Finance can set the non-contribution accounts.")
    if len(accounts or []) > MAX_ACCOUNTS:
        raise InvalidInput(f"A parish can allow at most {MAX_ACCOUNTS} GL accounts for non-contributions.", "accounts")
    wanted: dict[str, dict] = {}
    for a in accounts or []:
        number = clean_text(a.get("account_number"), field="GL account number", max_len=40)
        name = clean_text(a.get("account_name"), field="GL account name", max_len=120)
        if not number and not name and not clean_text(a.get("qbo_account_id"), field="QuickBooks account id", max_len=40):
            continue                                              # a blank row on the form
        if not number:
            raise InvalidInput("Every GL account needs its account number.", "account_number")
        if not name:
            raise InvalidInput(f"GL account {number} needs a name, so the entry screen can show it.", "account_name")
        key = number.casefold()
        if key in wanted:
            raise InvalidInput(f"GL account {number} is listed twice.", "account_number")
        wanted[key] = {"account_number": number, "account_name": name, "qbo_account_id": clean_text(a.get("qbo_account_id"), field="QuickBooks account id", max_len=40)}
    default_key = None
    if default_account_number not in (None, ""):
        default_key = str(default_account_number).strip().casefold()
        if default_key not in wanted:
            raise InvalidInput("The default has to be one of the accounts on the list.", "default_account_number")
    created = updated = turned_off = 0
    with tx(cur) as c:
        c.execute("SELECT * FROM donor.noncontribution_account WHERE parish_id = %s ORDER BY id FOR UPDATE", (ctx.parish_id,))
        have = {r["account_number"].casefold(): r for r in c.fetchall()}
        old_default = next((r for r in have.values() if r["is_default"]), None)
        # Take the default off first when it is changing: only one row at a time may hold it (a partial unique index).
        if old_default and (default_key is None or old_default["account_number"].casefold() != default_key):
            c.execute("UPDATE donor.noncontribution_account SET is_default = FALSE, updated_at = NOW() WHERE id = %s", (old_default["id"],))
            log_change(c, ctx, "noncontribution_account", old_default["id"], "Default", True, False, kind="update", scope="parish")
        for key, r in have.items():
            if key not in wanted and r["is_active"]:
                c.execute("UPDATE donor.noncontribution_account SET is_active = FALSE, is_default = FALSE, updated_at = NOW() WHERE id = %s", (r["id"],))
                log_change(c, ctx, "noncontribution_account", r["id"], "On", True, False, kind="update", scope="parish")
                turned_off += 1
        for key, w in wanted.items():
            r = have.get(key)
            if r is None:
                c.execute("INSERT INTO donor.noncontribution_account (parish_id, qbo_account_id, account_number, account_name, created_by_user_id) "
                          "VALUES (%s,%s,%s,%s,%s) RETURNING id", (ctx.parish_id, w["qbo_account_id"], w["account_number"], w["account_name"], ctx.user_id))
                rid = c.fetchone()["id"]
                log_change(c, ctx, "noncontribution_account", rid, None, None, account_label(w), kind="create", scope="parish")
                created += 1
                continue
            sets, vals = [], []
            for field, label in (("account_name", "Name"), ("qbo_account_id", "QuickBooks account id")):
                if r[field] != w[field]:
                    sets.append(f"{field} = %s")
                    vals.append(w[field])
                    log_change(c, ctx, "noncontribution_account", r["id"], label, r[field], w[field], kind="update", scope="parish")
            if not r["is_active"]:
                sets.append("is_active = TRUE")
                log_change(c, ctx, "noncontribution_account", r["id"], "On", False, True, kind="update", scope="parish")
            if sets:
                c.execute(f"UPDATE donor.noncontribution_account SET {', '.join(sets)}, updated_at = NOW() WHERE id = %s", (*vals, r["id"]))
                updated += 1
        if default_key is not None:
            c.execute("SELECT id, is_default FROM donor.noncontribution_account WHERE parish_id = %s AND LOWER(account_number) = %s", (ctx.parish_id, default_key))
            target = c.fetchone()
            if not target["is_default"]:
                c.execute("UPDATE donor.noncontribution_account SET is_default = TRUE, updated_at = NOW() WHERE id = %s", (target["id"],))
                log_change(c, ctx, "noncontribution_account", target["id"], "Default", False, True, kind="update", scope="parish")
        return {"created": created, "updated": updated, "turned_off": turned_off, "accounts": len(wanted),
                "default": wanted[default_key]["account_number"] if default_key else None}


# ── What still needs a GL account, and the reports ──────────────────────────────────────────────
def uncoded_noncontributions(ctx: Ctx, *, limit: int = 200) -> dict:
    """Non-gift receipts that have no GL account yet. Today that can only be a line loaded from another system (the database refuses
    a new one without an account), and it is listed so the parish can supply the account before sign-off. Finance roles only.
    Returns {"count", "total", "rows": [{gift_id, batch_id, batch_number, gift_date, amount, memo, historical}]}. No donor is named."""
    need_giving(ctx, "giving.read", "Only finance roles can see individual receipts.")
    with tx() as c:
        c.execute(
            "SELECT g.id AS gift_id, b.id AS batch_id, b.number AS batch_number, g.gift_date, gs.amount, g.memo, b.is_historical AS historical "
            "  FROM donor.gift g JOIN donor.batch b ON b.id = g.batch_id JOIN donor.gift_split gs ON gs.gift_id = g.id "
            " WHERE g.parish_id = %s AND g.gift_type = 'non_gift_receipt' AND g.status <> 'voided' AND gs.gl_account_id IS NULL AND gs.fund_id IS NULL "
            " ORDER BY g.gift_date, g.id LIMIT %s", (ctx.parish_id, limit))
        rows = c.fetchall()
        c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(gs.amount), 0) AS total FROM donor.gift g JOIN donor.gift_split gs ON gs.gift_id = g.id "
            " WHERE g.parish_id = %s AND g.gift_type = 'non_gift_receipt' AND g.status <> 'voided' AND gs.gl_account_id IS NULL AND gs.fund_id IS NULL", (ctx.parish_id,))
        t = c.fetchone()
    return {"count": t["n"], "total": t["total"] or ZERO, "rows": rows}


def report_noncontributions(ctx: Ctx, start: dt.date, end: dt.date) -> list[dict]:
    """Non-contributions over closed batches, by GL account, by receipt date. A line with no account shows as 'GL account needed'.
    No donor is named, so the Finance View role may run it. These amounts are NOT giving: they are never in the giving totals."""
    if not ctx.settings.get("giving_enabled"):
        raise PermissionDenied("Giving records are not turned on for this parish yet.")
    if not (ctx.can("totals.read") or ctx.can("giving.read") or ctx.can("giving.read.diocese")):
        raise PermissionDenied("You do not have permission to see these totals.")
    if end < start:
        raise InvalidInput("The end date cannot be before the start date.")
    with tx() as c:
        c.execute(
            "SELECT a.id AS account_id, a.account_number, a.account_name, COALESCE(SUM(gs.amount), 0) AS total, COUNT(DISTINCT g.id) AS items "
            "  FROM donor.gift g JOIN donor.batch b ON b.id = g.batch_id AND b.status IN ('closed', 'reconciled') "
            "  JOIN donor.gift_split gs ON gs.gift_id = g.id LEFT JOIN donor.noncontribution_account a ON a.id = gs.gl_account_id "
            " WHERE g.parish_id = %s AND g.gift_type = 'non_gift_receipt' AND g.status <> 'voided' AND g.gift_date BETWEEN %s AND %s "
            " GROUP BY a.id, a.account_number, a.account_name ORDER BY a.account_number NULLS LAST", (ctx.parish_id, start, end))
        return c.fetchall()


def batch_noncontribution_summary(c, batch_id: int) -> dict:
    """One batch's non-contributions as their own section: items, total, and the total by account. Lines that were voided do not count.
    A batch's amounts include them (they are part of the deposit), so the balance bar is unchanged, but they are not gifts."""
    c.execute(
        "SELECT a.account_number, a.account_name, COALESCE(SUM(gs.amount), 0) AS total, COUNT(DISTINCT g.id) AS items "
        "  FROM donor.gift g JOIN donor.gift_split gs ON gs.gift_id = g.id LEFT JOIN donor.noncontribution_account a ON a.id = gs.gl_account_id "
        " WHERE g.batch_id = %s AND g.gift_type = 'non_gift_receipt' AND g.status <> 'voided' "
        " GROUP BY a.account_number, a.account_name ORDER BY a.account_number NULLS LAST", (batch_id,))
    by_account = c.fetchall()
    # A receipt split across two accounts is one item, so the items are counted over the gifts, not summed over the accounts.
    c.execute("SELECT COUNT(*) AS n FROM donor.gift WHERE batch_id = %s AND gift_type = 'non_gift_receipt' AND status <> 'voided'", (batch_id,))
    items = c.fetchone()["n"]
    return {"items": items, "total": sum((r["total"] for r in by_account), ZERO),
            "by_account": by_account, "uncoded": sum(r["items"] for r in by_account if not r["account_number"])}


def allowed_account(c, ctx: Ctx, account_id, *, require_active: bool = True) -> dict:
    """Validate one account id a line or a correction names: it has to be this parish's, and (for new money) on. Raises InvalidInput
    with a message a clerk can act on."""
    try:
        aid = int(account_id)
    except (TypeError, ValueError):
        raise InvalidInput("Pick the GL account for this non-gift receipt.", "gl_account_id")
    c.execute("SELECT * FROM donor.noncontribution_account WHERE id = %s AND parish_id = %s", (aid, ctx.parish_id))
    row = c.fetchone()
    if not row:
        raise NotFound("That GL account was not found at this parish.")
    if require_active and not row["is_active"]:
        raise InvalidInput(f"The GL account {account_label(row)} is turned off and takes no new receipts.", "gl_account_id")
    return row
