"""
donor_pledges.py -- Beacon Donor Management, Phase 2: pledges, soft credits, fulfillment and campaign responses.

Operations: pledge_create, pledge_update, pledge_cancel, pledge_list, soft_credit_add, soft_credit_list,
pledge_fulfillment, current_pledge_summary, campaign_responses.

Fulfillment (rule 9). A pledge is fulfilled automatically by gifts, nothing is keyed against it:
  * counted: gifts to the campaign's fund, dated inside the CAMPAIGN period, in CLOSED batches, from the pledger and, on a
    joint pledge, from the spouse. A reversing gift is a negative row in a closed batch, so it cancels the original with
    no special case. A returned or reversed original keeps counting (its reversal subtracts it), so the net is exact.
  * not counted: non-gift receipts, voided lines, anything still in an open batch. A processor fee the donor chose to cover
    is never part of a gift's amount (donor_gifts), so it cannot be counted.
  * soft credits (a donor-advised fund's gift credited to the person who advised it) count toward the pledge, for gifts that
    are still recorded, but they are NOT part of that person's own giving: they are reported separately and never added to a
    deductible total.
Pace compares what has been given with the straight-line share of the pledge that should have arrived by today, over the
pledge's own start and end dates: paid in full, on track, or behind.

Who may use it: pledges.manage (Finance) manages pledges and soft credits. giving.read (Finance, Finance Supervisor) reads them.
Gift Entry and the people roles never see a pledge.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_HALF_UP

from donor_core import (
    Conflict, Ctx, InvalidInput, NotFound, PermissionDenied, check_enum, clean_text, diff_fields, log_change, need_giving,
    parse_date, person_label, to_id, to_money, tx,
)
from donor_gifts import can_read_gifts, money
from donor_people import require_connection

FREQUENCIES = ("one_time", "weekly", "monthly", "quarterly", "annual")
ZERO = Decimal("0.00")


def _need_manage(ctx: Ctx) -> None:
    need_giving(ctx, "pledges.manage", "Only Finance can manage pledges.")


def _need_read(ctx: Ctx) -> None:
    if not ctx.settings.get("giving_enabled"):
        raise PermissionDenied("Giving records are not turned on for this parish yet.")
    if not (ctx.can("pledges.manage") or can_read_gifts(ctx)):
        raise PermissionDenied("Only finance roles can see pledges.")


def _quant(v) -> Decimal:
    return Decimal(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _spouses(c, a: int, b: int) -> bool:
    c.execute("SELECT 1 AS x FROM donor.spouse_link WHERE person_id = %s AND spouse_id = %s AND ended_at IS NULL", (a, b))
    return c.fetchone() is not None


# ── Create, change, cancel ──────────────────────────────────────────────────────────────────────
def pledge_create(ctx: Ctx, data: dict, *, cur=None) -> dict:
    _need_manage(ctx)
    try:
        person_id = int(data.get("person_id"))
    except (TypeError, ValueError):
        raise InvalidInput("Pick the person who is pledging.", "person_id")
    joint = data.get("joint_with_person_id")
    joint = to_id(joint, field="joint_with_person_id", label="the spouse") if joint not in (None, "", 0, "0") else None
    try:
        campaign_id = int(data.get("campaign_id"))
    except (TypeError, ValueError):
        raise InvalidInput("Pick the campaign.", "campaign_id")
    amount = to_money(data.get("amount"), field="pledge amount")
    if amount <= ZERO:
        raise InvalidInput("A pledge has to be more than zero.", "amount")
    freq = check_enum(data.get("frequency") or "monthly", FREQUENCIES, field="frequency", allow_blank=False)
    with tx(cur) as c:
        c.execute("SELECT ca.*, f.accepts_pledges, f.name AS fund_name FROM donor.campaign ca JOIN donor.fund f ON f.id = ca.fund_id "
                  "WHERE ca.id = %s AND ca.parish_id = %s", (campaign_id, ctx.parish_id))
        camp = c.fetchone()
        if not camp:
            raise NotFound("That campaign was not found at this parish.")
        if not camp["is_active"]:
            raise InvalidInput("That campaign is closed.", "campaign_id")
        if not camp["accepts_pledges"]:
            raise InvalidInput(f"The fund '{camp['fund_name']}' does not accept pledges.", "campaign_id")
        start = parse_date(data.get("start_date"), field="start date") or camp["period_start"]
        end = parse_date(data.get("end_date"), field="end date") or camp["period_end"]
        if end < start:
            raise InvalidInput("The end date cannot be before the start date.", "end_date")
        if start < camp["period_start"] or end > camp["period_end"]:
            raise InvalidInput(f"A pledge has to fall inside its campaign ({camp['period_start']:%m/%d/%Y} to {camp['period_end']:%m/%d/%Y}).", "start_date")
        require_connection(c, ctx, person_id, include_archived=False)
        if joint is not None:
            if joint == person_id:
                raise InvalidInput("A joint pledge needs two different people.", "joint_with_person_id")
            require_connection(c, ctx, joint, include_archived=False)
            if not _spouses(c, person_id, joint):
                raise InvalidInput("A joint pledge needs the two people to be linked as spouses first.", "joint_with_person_id")
        who = [p for p in (person_id, joint) if p is not None]
        c.execute("SELECT id FROM donor.pledge WHERE parish_id = %s AND campaign_id = %s AND status = 'active' "
                  "AND (person_id = ANY(%s) OR joint_with_person_id = ANY(%s)) LIMIT 1", (ctx.parish_id, campaign_id, who, who))
        if c.fetchone():
            raise Conflict("There is already an active pledge for this campaign from this person or their spouse. Change that one instead.")
        c.execute("INSERT INTO donor.pledge (parish_id, person_id, joint_with_person_id, campaign_id, amount, frequency, start_date, end_date, notes, created_by_user_id) "
                  "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                  (ctx.parish_id, person_id, joint, campaign_id, amount, freq, start, end, clean_text(data.get("notes"), field="notes", max_len=300), ctx.user_id))
        pid = c.fetchone()["id"]
        log_change(c, ctx, "pledge", pid, None, None, f"${money(amount)} {freq.replace('_', ' ')} to {camp['name']}", person_id=person_id, kind="create", scope="parish")
        return {"id": pid}


def _pledge_row(c, ctx: Ctx, pledge_id: int, *, lock: bool = False) -> dict:
    c.execute("SELECT * FROM donor.pledge WHERE id = %s AND parish_id = %s" + (" FOR UPDATE" if lock else ""), (pledge_id, ctx.parish_id))
    row = c.fetchone()
    if not row:
        raise NotFound("That pledge was not found at this parish.")
    return row


def pledge_update(ctx: Ctx, pledge_id: int, changes: dict, *, cur=None) -> dict:
    _need_manage(ctx)
    new: dict = {}
    if "amount" in changes:
        a = to_money(changes["amount"], field="pledge amount")
        if a <= ZERO:
            raise InvalidInput("A pledge has to be more than zero.", "amount")
        new["amount"] = a
    if "frequency" in changes:
        new["frequency"] = check_enum(changes["frequency"], FREQUENCIES, field="frequency", allow_blank=False)
    if "start_date" in changes:
        new["start_date"] = parse_date(changes["start_date"], field="start date")
    if "end_date" in changes:
        new["end_date"] = parse_date(changes["end_date"], field="end date")
    if "notes" in changes:
        new["notes"] = clean_text(changes["notes"], field="notes", max_len=300)
    if not new:
        return {"id": pledge_id, "changed": []}
    with tx(cur) as c:
        old = _pledge_row(c, ctx, pledge_id, lock=True)
        if old["status"] != "active":
            raise InvalidInput("A cancelled pledge cannot be changed.")
        merged = {**old, **new}
        if merged["start_date"] is None or merged["end_date"] is None or merged["end_date"] < merged["start_date"]:
            raise InvalidInput("The end date cannot be before the start date.", "end_date")
        c.execute("SELECT period_start, period_end FROM donor.campaign WHERE id = %s", (old["campaign_id"],))
        camp = c.fetchone()
        if merged["start_date"] < camp["period_start"] or merged["end_date"] > camp["period_end"]:
            raise InvalidInput("A pledge has to fall inside its campaign's dates.", "start_date")
        diffs = diff_fields(old, new)
        if not diffs:
            return {"id": pledge_id, "changed": []}
        c.execute(f"UPDATE donor.pledge SET {', '.join(f'{k} = %s' for k, _, _ in diffs)}, updated_at = NOW() WHERE id = %s",
                  (*[v for _, _, v in diffs], pledge_id))
        for k, o, n in diffs:
            log_change(c, ctx, "pledge", pledge_id, k, o, n, person_id=old["person_id"], scope="parish")
        return {"id": pledge_id, "changed": [k for k, _, _ in diffs]}


def pledge_cancel(ctx: Ctx, pledge_id: int, reason: str, *, cur=None) -> dict:
    """A pledge is cancelled, never deleted. The gifts already given stay, and no longer count toward anything."""
    _need_manage(ctx)
    why = clean_text(reason, field="reason", max_len=200)
    if not why:
        raise InvalidInput("Say why the pledge is being cancelled.", "reason")
    with tx(cur) as c:
        old = _pledge_row(c, ctx, pledge_id, lock=True)
        if old["status"] != "active":
            raise InvalidInput("That pledge is already cancelled.")
        c.execute("UPDATE donor.pledge SET status = 'cancelled', cancelled_at = NOW(), cancelled_by_user_id = %s, updated_at = NOW() WHERE id = %s", (ctx.user_id, pledge_id))
        log_change(c, ctx, "pledge", pledge_id, "Status", "active", "cancelled", person_id=old["person_id"], kind="cancel", scope="parish", reason=why)
        return {"id": pledge_id}


# ── Soft credits ────────────────────────────────────────────────────────────────────────────────
def soft_credit_add(ctx: Ctx, gift_id: int, person_id: int, note: str | None = None, amount=None, *, cur=None) -> dict:
    """Credit a gift to another person (a donor-advised fund's gift credited to the person who advised it). It counts toward
    that person's pledge but is never part of their own giving or deductible total."""
    _need_manage(ctx)
    with tx(cur) as c:
        c.execute("SELECT * FROM donor.gift WHERE id = %s AND parish_id = %s FOR UPDATE", (gift_id, ctx.parish_id))
        g = c.fetchone()
        if not g:
            raise NotFound("That gift was not found at this parish.")
        if g["status"] != "recorded" or g["amount"] <= ZERO:
            raise InvalidInput("Only a recorded gift can be soft-credited.")
        require_connection(c, ctx, int(person_id), include_archived=False)
        if g["person_id"] == int(person_id):
            raise InvalidInput("A gift cannot be soft-credited to the person who gave it.", "person_id")
        amt = to_money(amount, field="amount") if amount not in (None, "") else g["amount"]
        if amt <= ZERO or amt > g["amount"]:
            raise InvalidInput(f"A soft credit has to be more than zero and no more than the gift (${money(g['amount'])}).", "amount")
        c.execute("SELECT 1 AS x FROM donor.soft_credit WHERE gift_id = %s AND person_id = %s", (gift_id, int(person_id)))
        if c.fetchone():
            raise Conflict("That person is already credited for this gift.")
        c.execute("INSERT INTO donor.soft_credit (parish_id, gift_id, person_id, amount, note, created_by_user_id) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                  (ctx.parish_id, gift_id, int(person_id), amt, clean_text(note, field="note", max_len=200), ctx.user_id))
        sid = c.fetchone()["id"]
        log_change(c, ctx, "soft_credit", sid, None, None, f"${money(amt)} credited for gift {gift_id}", person_id=int(person_id), kind="create", scope="parish")
        return {"id": sid}


def soft_credit_list(ctx: Ctx, person_id: int) -> list[dict]:
    _need_read(ctx)
    with tx() as c:
        require_connection(c, ctx, person_id, include_archived=True)
        c.execute("SELECT sc.*, g.gift_date FROM donor.soft_credit sc JOIN donor.gift g ON g.id = sc.gift_id WHERE sc.parish_id = %s AND sc.person_id = %s ORDER BY g.gift_date DESC",
                  (ctx.parish_id, person_id))
        return c.fetchall()


# ── Fulfillment and pace ────────────────────────────────────────────────────────────────────────
def _given(c, ctx: Ctx, camp: dict, people: list[int]) -> tuple[Decimal, Decimal]:
    c.execute(
        "SELECT COALESCE(SUM(gs.amount), 0) AS s FROM donor.gift g JOIN donor.batch b ON b.id = g.batch_id AND b.status IN ('closed', 'reconciled') "
        "JOIN donor.gift_split gs ON gs.gift_id = g.id WHERE g.parish_id = %s AND g.status <> 'voided' AND gs.fund_id = %s AND g.person_id = ANY(%s) "
        "AND g.gift_date BETWEEN %s AND %s AND g.gift_type <> 'non_gift_receipt'",
        (ctx.parish_id, camp["fund_id"], people, camp["period_start"], camp["period_end"]))
    direct = c.fetchone()["s"]
    # One amount per GIFT, however many of the pledger's people it is credited to, and never a gift the pledger (or the spouse on
    # a joint pledge) gave themselves: that already counted above, so crediting it again would double it.
    c.execute(
        "SELECT COALESCE(SUM(per_gift), 0) AS s FROM ("
        "SELECT LEAST(SUM(sc.amount), (SELECT COALESCE(SUM(x.amount), 0) FROM donor.gift_split x WHERE x.gift_id = g.id AND x.fund_id = %s)) AS per_gift "
        "FROM donor.soft_credit sc JOIN donor.gift g ON g.id = sc.gift_id JOIN donor.batch b ON b.id = g.batch_id AND b.status IN ('closed', 'reconciled') "
        "WHERE sc.parish_id = %s AND sc.person_id = ANY(%s) AND g.status = 'recorded' AND g.gift_date BETWEEN %s AND %s AND g.gift_type <> 'non_gift_receipt' "
        "AND (g.person_id IS NULL OR g.person_id <> ALL(%s)) GROUP BY g.id) t",
        (camp["fund_id"], ctx.parish_id, people, camp["period_start"], camp["period_end"], people))
    soft = c.fetchone()["s"]
    return direct, soft


def pace(amount: Decimal, given: Decimal, start: dt.date, end: dt.date, today: dt.date) -> dict:
    """Pure. paid_in_full when given >= amount, else on_track when given >= the straight-line share due by `today`, else behind."""
    amount, given = _quant(amount), _quant(given)
    total_days = (end - start).days + 1
    elapsed = 0 if today < start else min(total_days, (today - start).days + 1)
    expected = _quant(amount * elapsed / total_days)
    if given >= amount:
        status = "paid_in_full"
    elif given >= expected:
        status = "on_track"
    else:
        status = "behind"
    pct = (given / amount * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if amount else Decimal("0.0")
    return {"pace_status": status, "expected_to_date": expected, "percent": pct, "balance": max(amount - given, ZERO)}


def _fulfillment(c, ctx: Ctx, pledge: dict, camp: dict, today: dt.date) -> dict:
    people = [p for p in (pledge["person_id"], pledge["joint_with_person_id"]) if p is not None]
    direct, soft = _given(c, ctx, camp, people)
    given = direct + soft
    out = {"given": given, "given_direct": direct, "given_soft": soft}
    out.update(pace(pledge["amount"], given, pledge["start_date"], pledge["end_date"], today))
    return out


def pledge_fulfillment(ctx: Ctx, pledge_id: int, *, today: dt.date | None = None) -> dict:
    _need_read(ctx)
    today = today or dt.date.today()
    with tx() as c:
        p = _pledge_row(c, ctx, pledge_id)
        c.execute("SELECT * FROM donor.campaign WHERE id = %s", (p["campaign_id"],))
        return {**_fulfillment(c, ctx, p, c.fetchone(), today), "pledge_id": pledge_id, "amount": p["amount"]}


def _names(c, ids: list[int]) -> dict:
    if not ids:
        return {}
    c.execute("SELECT id, record_type, first_name, last_name, goes_by, org_name FROM donor.person WHERE id = ANY(%s)", (ids,))
    return {r["id"]: person_label(r) for r in c.fetchall()}


def pledge_list(ctx: Ctx, campaign_id: int, *, include_cancelled: bool = False, today: dt.date | None = None) -> dict:
    """The campaign's pledges with fulfillment and pace, plus the campaign totals."""
    _need_read(ctx)
    today = today or dt.date.today()
    with tx() as c:
        c.execute("SELECT ca.*, f.name AS fund_name FROM donor.campaign ca JOIN donor.fund f ON f.id = ca.fund_id WHERE ca.id = %s AND ca.parish_id = %s", (campaign_id, ctx.parish_id))
        camp = c.fetchone()
        if not camp:
            raise NotFound("That campaign was not found at this parish.")
        c.execute("SELECT * FROM donor.pledge WHERE campaign_id = %s AND parish_id = %s" + ("" if include_cancelled else " AND status = 'active'") +
                  " ORDER BY id", (campaign_id, ctx.parish_id))
        pledges = c.fetchall()
        names = _names(c, [x for p in pledges for x in (p["person_id"], p["joint_with_person_id"]) if x])
        total_pledged = total_given = ZERO
        for p in pledges:
            p["name"] = names.get(p["person_id"], "")
            p["joint_name"] = names.get(p["joint_with_person_id"]) if p["joint_with_person_id"] else None
            p.update(_fulfillment(c, ctx, p, camp, today))
            if p["status"] == "active":
                total_pledged += p["amount"]
                total_given += p["given"]
        return {"campaign": camp, "pledges": pledges, "total_pledged": total_pledged, "total_given": total_given,
                "counts": {s: sum(1 for p in pledges if p["status"] == "active" and p["pace_status"] == s) for s in ("paid_in_full", "on_track", "behind")}}


def current_pledge_summary(ctx: Ctx, person_id: int, *, today: dt.date | None = None) -> dict | None:
    """The person's (or couple's) active pledge in the campaign whose period contains today, else the most recent one.
    For the Giving tab. None when they have no active pledge."""
    _need_read(ctx)
    today = today or dt.date.today()
    with tx() as c:
        c.execute("SELECT p.*, ca.name AS campaign_name FROM donor.pledge p JOIN donor.campaign ca ON ca.id = p.campaign_id "
                  "WHERE p.parish_id = %s AND p.status = 'active' AND (p.person_id = %s OR p.joint_with_person_id = %s) "
                  "ORDER BY (%s BETWEEN ca.period_start AND ca.period_end) DESC, ca.period_end DESC LIMIT 1", (ctx.parish_id, person_id, person_id, today))
        p = c.fetchone()
        if not p:
            return None
        c.execute("SELECT * FROM donor.campaign WHERE id = %s", (p["campaign_id"],))
        f = _fulfillment(c, ctx, p, c.fetchone(), today)
        other = p["joint_with_person_id"] if p["person_id"] == person_id else p["person_id"]
        joint_name = _names(c, [other]).get(other) if p["joint_with_person_id"] else None
        return {"campaign_name": p["campaign_name"], "amount": p["amount"], "percent": f["percent"], "balance": f["balance"],
                "status": f["pace_status"], "given": f["given"], "joint_with": joint_name, "pledge_id": p["id"]}


def campaign_responses(ctx: Ctx, campaign_id: int, prior_campaign_id: int | None = None, *, today: dt.date | None = None) -> dict:
    """Who has pledged to this campaign and how it compares with the prior campaign, plus who pledged before and has not
    answered yet. Every person is named once (a joint pledge is one row under the pledger)."""
    _need_read(ctx)
    with tx() as c:
        c.execute("SELECT * FROM donor.campaign WHERE id = %s AND parish_id = %s", (campaign_id, ctx.parish_id))
        camp = c.fetchone()
        if not camp:
            raise NotFound("That campaign was not found at this parish.")
        prior = None
        if prior_campaign_id:
            c.execute("SELECT * FROM donor.campaign WHERE id = %s AND parish_id = %s", (prior_campaign_id, ctx.parish_id))
            prior = c.fetchone()
            if not prior:
                raise NotFound("The prior campaign was not found at this parish.")

        def pledges_for(cid):
            c.execute("SELECT * FROM donor.pledge WHERE campaign_id = %s AND parish_id = %s AND status = 'active'", (cid, ctx.parish_id))
            return c.fetchall()
        now = pledges_for(campaign_id)
        before = pledges_for(prior["id"]) if prior else []

        def keys(p):
            return {p["person_id"]} | ({p["joint_with_person_id"]} if p["joint_with_person_id"] else set())
        names = _names(c, [x for p in now + before for x in keys(p)])
        rows, matched_prior = [], set()
        for p in now:
            pr = next((b for b in before if keys(b) & keys(p)), None)
            if pr:
                matched_prior.add(pr["id"])
            prior_amt = pr["amount"] if pr else None
            change = (p["amount"] - prior_amt) if pr else None
            rows.append({"person_id": p["person_id"], "name": names.get(p["person_id"], ""), "amount": p["amount"], "prior_amount": prior_amt, "change": change,
                         "change_pct": (change / prior_amt * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if pr and prior_amt else None,
                         "state": "new" if prior and not pr else "pledged"})
        no_response = [{"person_id": b["person_id"], "name": names.get(b["person_id"], ""), "prior_amount": b["amount"]} for b in before if b["id"] not in matched_prior]
        return {"campaign": camp, "prior_campaign": prior, "responses": rows, "no_response": no_response,
                "total": sum((r["amount"] for r in rows), ZERO), "prior_total": sum((b["amount"] for b in before), ZERO)}
