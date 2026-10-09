"""
donor_funds.py -- Beacon Donor Management, Phase 2: funds and campaigns.

A fund is where a gift's money is meant to go (General Fund, Building Fund, a pass-through fund for a special
collection). It carries the QuickBooks accounts and class the journal entry uses. A campaign is a pledge period for
one fund ("2026 Annual Giving"), so the fund stays the same every year and only the campaign changes.

Operations (each one function, each maps to a future tool): fund_list, fund_create, fund_update, campaign_list,
campaign_create, campaign_update.

Rules enforced here
  * Every fund and campaign belongs to one parish. A fund id from another parish reads as "not found".
  * A fund is closed, never deleted. A closed fund takes no new gifts (donor_batches checks), and its old gifts stay.
  * Fund names are unique within a parish (ignoring case).
  * A campaign belongs to a fund that accepts pledges, and its period ends on or after it starts.
  * Creating and changing funds and campaigns needs funds.manage (Finance). Reading them needs any giving role.
"""
from __future__ import annotations

from donor_core import (
    Conflict, Ctx, InvalidInput, NotFound, PermissionDenied, check_enum, clean_text, diff_fields, log_change,
    need_giving, parse_date, to_bool, to_money, tx,
)

FUND_TEXT_FIELDS = {"name": 80, "statement_name": 120, "income_account": 80, "qbo_class": 80, "liability_account": 80}
FUND_BOOL_FIELDS = ("is_open", "accepts_pledges", "tax_deductible_default", "allows_recurring_end")
FUND_FIELDS = tuple(FUND_TEXT_FIELDS) + FUND_BOOL_FIELDS + ("donor_restriction", "sort_order")
DONOR_RESTRICTIONS = ("unrestricted", "donor_restricted")
FUND_LABELS = {
    "name": "Name", "statement_name": "Statement wording", "is_open": "Open", "accepts_pledges": "Accepts pledges",
    "tax_deductible_default": "Tax-deductible by default", "allows_recurring_end": "Allows a recurring end date",
    "income_account": "Income account", "qbo_class": "QuickBooks class", "liability_account": "Liability account",
    "donor_restriction": "Donor restriction", "sort_order": "Order",
}


def _can_read(ctx: Ctx) -> bool:
    return any(ctx.can(c) for c in ("batch.view", "funds.manage", "totals.read", "pledges.manage", "giving.read"))


def _need_read(ctx: Ctx) -> None:
    need_giving(ctx, "totals.read") if not _can_read(ctx) else need_giving(ctx, next(
        c for c in ("batch.view", "funds.manage", "totals.read", "pledges.manage", "giving.read") if ctx.can(c)))


def clean_fund_fields(data: dict, *, partial: bool) -> dict:
    """Validate the fund fields present in `data`. `partial` allows missing keys (an update)."""
    out: dict = {}
    for f, limit in FUND_TEXT_FIELDS.items():
        if f in data:
            out[f] = clean_text(data[f], field=FUND_LABELS[f].lower(), max_len=limit)
    for f in FUND_BOOL_FIELDS:
        if f in data:
            out[f] = to_bool(data[f], field=f)
    if "donor_restriction" in data:
        out["donor_restriction"] = check_enum(data["donor_restriction"], DONOR_RESTRICTIONS, field="donor restriction", allow_blank=False)
    if "sort_order" in data:
        try:
            out["sort_order"] = int(data["sort_order"]) if str(data["sort_order"]).strip() != "" else 100
        except (TypeError, ValueError):
            raise InvalidInput("Order must be a whole number.", "sort_order")
    if not partial and not out.get("name"):
        raise InvalidInput("A fund needs a name.", "name")
    if "name" in out and not out["name"]:
        raise InvalidInput("A fund needs a name.", "name")
    return out


def fund_row(c, ctx: Ctx, fund_id: int, *, lock: bool = False) -> dict:
    """The fund, only if it belongs to this parish. Anything else is 'not found'."""
    c.execute("SELECT * FROM donor.fund WHERE id = %s AND parish_id = %s" + (" FOR UPDATE" if lock else ""), (fund_id, ctx.parish_id))
    row = c.fetchone()
    if not row:
        raise NotFound("That fund was not found at this parish.")
    return row


def fund_list(ctx: Ctx, *, include_closed: bool = True) -> list[dict]:
    _need_read(ctx)
    with tx() as c:
        c.execute("SELECT * FROM donor.fund WHERE parish_id = %s" + ("" if include_closed else " AND is_open") +
                  " ORDER BY is_open DESC, sort_order, LOWER(name)", (ctx.parish_id,))
        return c.fetchall()


def fund_create(ctx: Ctx, data: dict, *, cur=None) -> dict:
    need_giving(ctx, "funds.manage", "Only Finance can create funds.")
    f = clean_fund_fields(data, partial=False)
    f.setdefault("sort_order", 100)
    if f.get("liability_account") and f.get("income_account"):
        raise InvalidInput("A pass-through fund uses a liability account instead of an income account. Fill in one, not both.", "liability_account")
    with tx(cur) as c:
        c.execute("SELECT 1 AS x FROM donor.fund WHERE parish_id = %s AND LOWER(name) = LOWER(%s)", (ctx.parish_id, f["name"]))
        if c.fetchone():
            raise Conflict(f"This parish already has a fund called '{f['name']}'.")
        cols = ["parish_id", "created_by_user_id"] + list(f)
        c.execute(f"INSERT INTO donor.fund ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                  (ctx.parish_id, ctx.user_id, *f.values()))
        fid = c.fetchone()["id"]
        log_change(c, ctx, "fund", fid, None, None, f["name"], kind="create", scope="parish")
        return {"id": fid, "name": f["name"]}


def fund_update(ctx: Ctx, fund_id: int, changes: dict, *, cur=None) -> dict:
    need_giving(ctx, "funds.manage", "Only Finance can change funds.")
    f = clean_fund_fields(changes, partial=True)
    if not f:
        return {"id": fund_id, "changed": []}
    with tx(cur) as c:
        old = fund_row(c, ctx, fund_id, lock=True)
        new = dict(f)
        merged = {**old, **new}
        if merged.get("liability_account") and merged.get("income_account"):
            raise InvalidInput("A pass-through fund uses a liability account instead of an income account. Fill in one, not both.", "liability_account")
        if new.get("accepts_pledges") is False and old["accepts_pledges"]:
            c.execute("SELECT 1 AS x FROM donor.campaign WHERE fund_id = %s AND is_active", (fund_id,))
            if c.fetchone():
                raise Conflict("This fund has an active campaign. Close the campaign before the fund stops accepting pledges.")
        if "name" in new and new["name"].lower() != old["name"].lower():
            c.execute("SELECT 1 AS x FROM donor.fund WHERE parish_id = %s AND LOWER(name) = LOWER(%s) AND id <> %s",
                      (ctx.parish_id, new["name"], fund_id))
            if c.fetchone():
                raise Conflict(f"This parish already has a fund called '{new['name']}'.")
        diffs = diff_fields(old, new)
        if not diffs:
            return {"id": fund_id, "changed": []}
        c.execute(f"UPDATE donor.fund SET {', '.join(f'{k} = %s' for k, _, _ in diffs)}, updated_at = NOW() WHERE id = %s",
                  (*[v for _, _, v in diffs], fund_id))
        for k, o, n in diffs:
            log_change(c, ctx, "fund", fund_id, FUND_LABELS.get(k, k), o, n, scope="parish")
        return {"id": fund_id, "changed": [k for k, _, _ in diffs]}


# ── Campaigns ───────────────────────────────────────────────────────────────────────────────────
def campaign_row(c, ctx: Ctx, campaign_id: int, *, lock: bool = False) -> dict:
    c.execute("SELECT * FROM donor.campaign WHERE id = %s AND parish_id = %s" + (" FOR UPDATE" if lock else ""), (campaign_id, ctx.parish_id))
    row = c.fetchone()
    if not row:
        raise NotFound("That campaign was not found at this parish.")
    return row


def campaign_list(ctx: Ctx, *, include_inactive: bool = True) -> list[dict]:
    _need_read(ctx)
    with tx() as c:
        c.execute("SELECT ca.*, f.name AS fund_name FROM donor.campaign ca JOIN donor.fund f ON f.id = ca.fund_id "
                  "WHERE ca.parish_id = %s" + ("" if include_inactive else " AND ca.is_active") +
                  " ORDER BY ca.is_active DESC, ca.period_start DESC, LOWER(ca.name)", (ctx.parish_id,))
        return c.fetchall()


def campaign_create(ctx: Ctx, data: dict, *, cur=None) -> dict:
    need_giving(ctx, "funds.manage", "Only Finance can create campaigns.")
    name = clean_text(data.get("name"), field="campaign name", max_len=80)
    if not name:
        raise InvalidInput("A campaign needs a name.", "name")
    start = parse_date(data.get("period_start"), field="start date")
    end = parse_date(data.get("period_end"), field="end date")
    if start is None or end is None:
        raise InvalidInput("A campaign needs a start date and an end date.", "period_start")
    if end < start:
        raise InvalidInput("The end date cannot be before the start date.", "period_end")
    goal = None
    if str(data.get("goal_amount") or "").strip():
        goal = to_money(data.get("goal_amount"), field="goal")
    try:
        fund_id = int(data.get("fund_id"))
    except (TypeError, ValueError):
        raise InvalidInput("Pick the fund this campaign raises money for.", "fund_id")
    with tx(cur) as c:
        fund = fund_row(c, ctx, fund_id)
        if not fund["accepts_pledges"]:
            raise InvalidInput(f"'{fund['name']}' does not accept pledges yet. Turn on 'Accepts pledges' for that fund first.", "fund_id")
        c.execute("SELECT 1 AS x FROM donor.campaign WHERE parish_id = %s AND LOWER(name) = LOWER(%s)", (ctx.parish_id, name))
        if c.fetchone():
            raise Conflict(f"This parish already has a campaign called '{name}'.")
        c.execute("INSERT INTO donor.campaign (parish_id, fund_id, name, period_start, period_end, goal_amount, created_by_user_id) "
                  "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id", (ctx.parish_id, fund_id, name, start, end, goal, ctx.user_id))
        cid = c.fetchone()["id"]
        log_change(c, ctx, "campaign", cid, None, None, name, kind="create", scope="parish")
        return {"id": cid, "name": name}


def campaign_update(ctx: Ctx, campaign_id: int, changes: dict, *, cur=None) -> dict:
    need_giving(ctx, "funds.manage", "Only Finance can change campaigns.")
    new: dict = {}
    if "name" in changes:
        n = clean_text(changes["name"], field="campaign name", max_len=80)
        if not n:
            raise InvalidInput("A campaign needs a name.", "name")
        new["name"] = n
    if "period_start" in changes:
        new["period_start"] = parse_date(changes["period_start"], field="start date")
    if "period_end" in changes:
        new["period_end"] = parse_date(changes["period_end"], field="end date")
    if "goal_amount" in changes:
        new["goal_amount"] = to_money(changes["goal_amount"], field="goal") if str(changes["goal_amount"] or "").strip() else None
    if "is_active" in changes:
        new["is_active"] = to_bool(changes["is_active"], field="active")
    if not new:
        return {"id": campaign_id, "changed": []}
    with tx(cur) as c:
        old = campaign_row(c, ctx, campaign_id, lock=True)
        merged = {**old, **new}
        if merged["period_start"] is None or merged["period_end"] is None or merged["period_end"] < merged["period_start"]:
            raise InvalidInput("The end date cannot be before the start date.", "period_end")
        if "name" in new and new["name"].lower() != old["name"].lower():
            c.execute("SELECT 1 AS x FROM donor.campaign WHERE parish_id = %s AND LOWER(name) = LOWER(%s) AND id <> %s", (ctx.parish_id, new["name"], campaign_id))
            if c.fetchone():
                raise Conflict(f"This parish already has a campaign called '{new['name']}'.")
        diffs = diff_fields(old, new)
        if not diffs:
            return {"id": campaign_id, "changed": []}
        c.execute(f"UPDATE donor.campaign SET {', '.join(f'{k} = %s' for k, _, _ in diffs)}, updated_at = NOW() WHERE id = %s",
                  (*[v for _, _, v in diffs], campaign_id))
        for k, o, n in diffs:
            log_change(c, ctx, "campaign", campaign_id, k, o, n, scope="parish")
        return {"id": campaign_id, "changed": [k for k, _, _ in diffs]}
