"""
donor_pledge_requests.py -- Beacon Donor Management: a parishioner asks to pledge, to change a pledge, or to cancel one.

A parishioner never creates, changes or cancels a pledge. They send a REQUEST, and Finance answers it. Approving a request runs the
EXISTING pledge services (donor_pledges.pledge_create / pledge_update / pledge_cancel), so every pledge rule (one active pledge per
person or spouse per campaign, dates inside the campaign, a fund that accepts pledges, a joint pledge only between linked spouses)
lives in exactly one place and a request can never get around one of them.

Parishioner side (everything takes `ps`, the session row: the person and the parish come from it, never from the browser):
  portal_pledges     their pledges (their own and any joint pledge they are part of), the campaigns open for online pledging, and
                     their requests with the answers
  request_create     kind new / change / cancel, with every id the browser sent checked against the session person and parish

Finance side (a Ctx with pledges.manage):
  waiting_requests / recently_answered / waiting_count
  request_approve    runs the pledge service in the SAME transaction and records the resulting pledge id on the request
  request_decline    needs a reason, keeps the request

Rules (pinned by Tools/test_donor_portal.py): one waiting request per person per campaign. Amount more than zero, whole cents, never
more than PLEDGE_MAX, and above PLEDGE_CONFIRM_ABOVE only when the parishioner ticks the confirm box. The campaign must be active,
accept pledges, be opened for online pledging (a switch per campaign, off until Finance turns it on) and not be over. A joint pledge
only with the spouse linked on the profile (the partner comes from the profile, a posted id is only compared with it). An active pledge
offers change or cancel instead of a new one. Nobody answers their own request (a login whose email is on the requesting person).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import db
import donor_pledges as PL
import donor_portal as PP
import donor_roles
from donor_core import (
    Conflict, Ctx, InvalidInput, NotFound, PermissionDenied, check_enum, clean_text, label, log_change, need_giving, parse_date,
    person_label, to_id, to_money, tx,
)

KINDS = ("new", "change", "cancel")
PLEDGE_CONFIRM_ABOVE = Decimal("25000.00")      # a pledge above this needs the confirm box
PLEDGE_MAX = Decimal("1000000.00")              # above this nothing is accepted online (a mistyped extra zero, or a conversation to have)
ZERO = Decimal("0.00")


# ── the parishioner's side ──────────────────────────────────────────────────────────────────────
def _name(row: dict | None) -> str:
    if not row:
        return ""
    first = (row.get("goes_by") or row.get("first_name") or "").strip()
    return " ".join(x for x in (first, (row.get("last_name") or "").strip()) if x)


def spouse_of(c, person_id: int, parish_id: int) -> dict | None:
    """The person's current spouse when that spouse is also connected to this parish (so a joint pledge is possible), else None.
    Comes from the profile, never from the browser."""
    c.execute("SELECT sl.spouse_id AS id, p.first_name, p.last_name, p.goes_by FROM donor.spouse_link sl "
              "JOIN donor.person p ON p.id = sl.spouse_id AND p.archived_at IS NULL AND p.record_type = 'person' "
              "JOIN donor.parish_connection pc ON pc.person_id = sl.spouse_id AND pc.parish_id = %s AND pc.archived_at IS NULL "
              "WHERE sl.person_id = %s AND sl.ended_at IS NULL", (parish_id, person_id))
    row = c.fetchone()
    if row:
        row["name"] = _name(row)
    return row


def portal_pledges(ps: dict, *, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    pid, parish_id = ps["person_id"], ps["parish_id"]
    a = PP.actor(parish_id)
    with tx() as c:
        spouse = spouse_of(c, pid, parish_id)
        c.execute("SELECT p.*, ca.name AS campaign_name, ca.is_active AS campaign_active, ca.online_pledging, ca.period_end AS campaign_end, f.name AS fund_name "
                  "FROM donor.pledge p JOIN donor.campaign ca ON ca.id = p.campaign_id JOIN donor.fund f ON f.id = ca.fund_id "
                  "WHERE p.parish_id = %s AND p.status = 'active' AND (p.person_id = %s OR p.joint_with_person_id = %s) "
                  "ORDER BY ca.period_end DESC, p.id DESC LIMIT 20", (parish_id, pid, pid))
        pledges = c.fetchall()
        c.execute("SELECT campaign_id FROM donor.pledge_request WHERE parish_id = %s AND person_id = %s AND status = 'waiting'", (parish_id, pid))
        waiting = {r["campaign_id"] for r in c.fetchall()}
        out = []
        for p in pledges:
            c.execute("SELECT * FROM donor.campaign WHERE id = %s", (p["campaign_id"],))
            camp = c.fetchone()
            f = PL._fulfillment(c, a, p, camp, today)
            other_id = p["joint_with_person_id"] if p["person_id"] == pid else p["person_id"]
            joint_name = None
            if p["joint_with_person_id"] is not None:
                c.execute("SELECT first_name, last_name, goes_by FROM donor.person WHERE id = %s", (other_id,))
                joint_name = _name(c.fetchone())
            out.append({
                "id": p["id"], "campaign_id": p["campaign_id"], "campaign_name": p["campaign_name"], "fund_name": p["fund_name"],
                "amount": p["amount"], "frequency": p["frequency"], "start_date": p["start_date"], "end_date": p["end_date"],
                "joint_with": joint_name, "given": f["given"], "balance": f["balance"], "percent": f["percent"],
                "pace_status": f["pace_status"],
                "open": bool(p["campaign_active"]) and bool(p["online_pledging"]) and p["campaign_end"] >= today,
                "waiting": p["campaign_id"] in waiting})
        c.execute(
            "SELECT ca.id, ca.name, ca.period_start, ca.period_end, f.name AS fund_name FROM donor.campaign ca "
            "JOIN donor.fund f ON f.id = ca.fund_id AND f.parish_id = ca.parish_id "
            "WHERE ca.parish_id = %s AND ca.is_active AND ca.online_pledging AND f.accepts_pledges AND ca.period_end >= %s "
            "AND NOT EXISTS (SELECT 1 FROM donor.pledge p WHERE p.campaign_id = ca.id AND p.parish_id = ca.parish_id AND p.status = 'active' "
            "AND (p.person_id = %s OR p.joint_with_person_id = %s)) ORDER BY ca.period_start DESC, ca.id", (parish_id, today, pid, pid))
        open_campaigns = c.fetchall()
        for oc in open_campaigns:
            oc["waiting"] = oc["id"] in waiting
        c.execute("SELECT r.id, r.kind, r.amount, r.frequency, r.status, r.decision_reason, r.created_at, r.decided_at, ca.name AS campaign_name "
                  "FROM donor.pledge_request r JOIN donor.campaign ca ON ca.id = r.campaign_id WHERE r.parish_id = %s AND r.person_id = %s "
                  "ORDER BY r.id DESC LIMIT 20", (parish_id, pid))
        requests = c.fetchall()
    return {"pledges": out, "open_campaigns": open_campaigns, "requests": requests, "spouse": spouse,
            "confirm_above": PLEDGE_CONFIRM_ABOVE}


def _check_amount(form, field: str = "amount") -> Decimal:
    amount = to_money(form.get(field), field="amount")
    if amount <= ZERO:
        raise InvalidInput("A pledge has to be more than zero.", "amount")
    if amount > PLEDGE_MAX:
        raise InvalidInput("That is more than can be pledged online. Please use Get Help and the parish office will help.", "amount")
    if amount > PLEDGE_CONFIRM_ABOVE and form.get("confirm_large") is None:
        raise InvalidInput(f"That is a large pledge. Please tick the box to confirm you mean ${amount:,.2f}.", "amount",
                           details={"needs_confirm": True})
    return amount


def _one_waiting_only(c, parish_id: int, person_id: int, campaign_id: int) -> None:
    c.execute("SELECT 1 AS x FROM donor.pledge_request WHERE parish_id = %s AND person_id = %s AND campaign_id = %s AND status = 'waiting'",
              (parish_id, person_id, campaign_id))
    if c.fetchone():
        raise Conflict("You already have a request waiting for this campaign. Finance will answer it, and then you can send another.")


def request_create(ps: dict, form, *, today: dt.date | None = None) -> dict:
    """File a request. `form` has get(): kind, and per kind campaign_id (new) or pledge_id (change, cancel), amount, frequency, dates,
    joint_with_person_id, note, confirm_large. Raises DonorError subclasses with a message safe to show. NotFound for any id that is
    not the signed-in person's own to ask about (a forged id looks exactly like a missing one)."""
    today = today or dt.date.today()
    pid, parish_id = ps["person_id"], ps["parish_id"]
    st = donor_roles.settings_get(parish_id)
    if not (st.get("portal_enabled") and st.get("giving_enabled")):
        raise PermissionDenied("Pledges are not available online for this parish yet.")
    kind = check_enum(form.get("kind"), KINDS, field="request", allow_blank=False)
    a = PP.actor(parish_id)
    note = clean_text(form.get("note"), field="note", max_len=300)
    with tx() as c:
        if kind == "new":
            cid = to_id(form.get("campaign_id"), field="campaign_id", label="a campaign")
            c.execute("SELECT ca.id, ca.name, ca.is_active, ca.online_pledging, ca.period_start, ca.period_end, f.accepts_pledges "
                      "FROM donor.campaign ca JOIN donor.fund f ON f.id = ca.fund_id AND f.parish_id = ca.parish_id "
                      "WHERE ca.id = %s AND ca.parish_id = %s", (cid, parish_id))
            camp = c.fetchone()
            if not camp or not (camp["is_active"] and camp["online_pledging"] and camp["accepts_pledges"] and camp["period_end"] >= today):
                raise NotFound("That campaign is not open for pledges here.")
            amount = _check_amount(form)
            freq = check_enum(form.get("frequency") or "monthly", PL.FREQUENCIES, field="frequency", allow_blank=False)
            start = parse_date(form.get("start_date"), field="start date") or camp["period_start"]
            end = parse_date(form.get("end_date"), field="end date") or camp["period_end"]
            if end < start:
                raise InvalidInput("The end date cannot be before the start date.", "end_date")
            if start < camp["period_start"] or end > camp["period_end"]:
                raise InvalidInput(f"A pledge has to fall inside its campaign ({camp['period_start']:%m/%d/%Y} to {camp['period_end']:%m/%d/%Y}).", "start_date")
            joint = None
            joint_raw = form.get("joint_with_person_id")
            if joint_raw not in (None, "", "0"):
                jid = to_id(joint_raw, field="joint_with_person_id", label="your spouse")
                sp = spouse_of(c, pid, parish_id)
                if not sp or sp["id"] != jid:
                    raise NotFound("That person was not found.")           # the partner is the profile's spouse or nobody
                joint = jid
            who = [pid] + ([joint] if joint else [])
            c.execute("SELECT 1 AS x FROM donor.pledge WHERE parish_id = %s AND campaign_id = %s AND status = 'active' "
                      "AND (person_id = ANY(%s) OR joint_with_person_id = ANY(%s)) LIMIT 1", (parish_id, cid, who, who))
            if c.fetchone():
                raise Conflict("You already have a pledge for this campaign. Use \"Ask to change\" on it instead.")
            _one_waiting_only(c, parish_id, pid, cid)
            c.execute("INSERT INTO donor.pledge_request (parish_id, person_id, campaign_id, kind, amount, frequency, start_date, end_date, "
                      "joint_with_person_id, note) VALUES (%s,%s,%s,'new',%s,%s,%s,%s,%s,%s) RETURNING id",
                      (parish_id, pid, cid, amount, freq, start, end, joint, note))
            rid = c.fetchone()["id"]
            log_change(c, a, "pledge_request", rid, None, None, f"new pledge request ${amount:,.2f} {freq.replace('_', ' ')} to {camp['name']}",
                       person_id=pid, kind="create", scope="parish", reason=PP.SELF_REASON)
            return {"id": rid, "kind": kind}

        # change or cancel: the pledge must be the signed-in person's own (theirs, or a joint pledge they are part of)
        plid = to_id(form.get("pledge_id"), field="pledge_id", label="a pledge")
        c.execute("SELECT p.id, p.campaign_id, p.amount, p.frequency, ca.is_active, ca.online_pledging, ca.period_end, ca.name AS campaign_name "
                  "FROM donor.pledge p JOIN donor.campaign ca ON ca.id = p.campaign_id "
                  "WHERE p.id = %s AND p.parish_id = %s AND p.status = 'active' AND (p.person_id = %s OR p.joint_with_person_id = %s)",
                  (plid, parish_id, pid, pid))
        pl = c.fetchone()
        if not pl:
            raise NotFound("That pledge was not found.")
        if not (pl["is_active"] and pl["online_pledging"]) or pl["period_end"] < today:
            raise InvalidInput("This pledge cannot be changed here. Please use Get Help and the parish office will help.", "pledge_id")
        _one_waiting_only(c, parish_id, pid, pl["campaign_id"])
        if kind == "change":
            amount = _check_amount(form)
            freq = check_enum(form.get("frequency") or pl["frequency"], PL.FREQUENCIES, field="frequency", allow_blank=False)
            if amount == pl["amount"] and freq == pl["frequency"]:
                raise InvalidInput("That is the same as your pledge now. Change the amount or how often, then send it.", "amount")
            c.execute("INSERT INTO donor.pledge_request (parish_id, person_id, campaign_id, kind, pledge_id, amount, frequency, note) "
                      "VALUES (%s,%s,%s,'change',%s,%s,%s,%s) RETURNING id", (parish_id, pid, pl["campaign_id"], plid, amount, freq, note))
            what = f"change request to ${amount:,.2f} {freq.replace('_', ' ')} for {pl['campaign_name']}"
        else:
            c.execute("INSERT INTO donor.pledge_request (parish_id, person_id, campaign_id, kind, pledge_id, note) "
                      "VALUES (%s,%s,%s,'cancel',%s,%s) RETURNING id", (parish_id, pid, pl["campaign_id"], plid, note))
            what = f"cancel request for {pl['campaign_name']}"
        rid = c.fetchone()["id"]
        log_change(c, a, "pledge_request", rid, None, None, what, person_id=pid, kind="create", scope="parish", reason=PP.SELF_REASON)
        return {"id": rid, "kind": kind}


# ── Finance's side ──────────────────────────────────────────────────────────────────────────────
def _need_manage(ctx: Ctx) -> None:
    need_giving(ctx, "pledges.manage", "Only Finance can answer pledge requests.")


def waiting_count(parish_id: int) -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM donor.pledge_request WHERE parish_id = %s AND status = 'waiting'", (parish_id,))
    return row["n"] if row else 0


def waiting_requests(ctx: Ctx) -> list[dict]:
    _need_manage(ctx)
    with tx() as c:
        c.execute(
            "SELECT r.id, r.kind, r.person_id, r.campaign_id, r.pledge_id, r.amount, r.frequency, r.start_date, r.end_date, "
            "r.joint_with_person_id, r.note, r.created_at, ca.name AS campaign_name, ca.fund_id, ca.period_start, ca.period_end, "
            "cur.amount AS current_amount, cur.frequency AS current_frequency, "
            "p.first_name, p.last_name, p.goes_by, p.org_name, p.record_type "
            "FROM donor.pledge_request r JOIN donor.campaign ca ON ca.id = r.campaign_id JOIN donor.person p ON p.id = r.person_id "
            "LEFT JOIN donor.pledge cur ON cur.id = r.pledge_id WHERE r.parish_id = %s AND r.status = 'waiting' ORDER BY r.created_at, r.id",
            (ctx.parish_id,))
        rows = c.fetchall()
        for r in rows:
            r["name"] = person_label(r)
            r["joint_name"] = None
            if r["joint_with_person_id"]:
                c.execute("SELECT first_name, last_name, goes_by, org_name, record_type FROM donor.person WHERE id = %s", (r["joint_with_person_id"],))
                r["joint_name"] = person_label(c.fetchone())
            c.execute("SELECT p2.amount, ca2.name FROM donor.pledge p2 JOIN donor.campaign ca2 ON ca2.id = p2.campaign_id "
                      "WHERE p2.parish_id = %s AND p2.person_id = %s AND ca2.fund_id = %s AND p2.campaign_id <> %s AND p2.status = 'active' "
                      "ORDER BY ca2.period_end DESC LIMIT 1", (ctx.parish_id, r["person_id"], r["fund_id"], r["campaign_id"]))
            prior = c.fetchone()
            r["prior"] = {"amount": prior["amount"], "campaign": prior["name"]} if prior else None
    return rows


def recently_answered(ctx: Ctx, limit: int = 10) -> list[dict]:
    _need_manage(ctx)
    with tx() as c:
        c.execute("SELECT r.id, r.kind, r.status, r.amount, r.frequency, r.decided_at, r.decision_reason, r.resulting_pledge_id, "
                  "ca.name AS campaign_name, p.first_name, p.last_name, p.goes_by, p.org_name, p.record_type, r.person_id "
                  "FROM donor.pledge_request r JOIN donor.campaign ca ON ca.id = r.campaign_id JOIN donor.person p ON p.id = r.person_id "
                  "WHERE r.parish_id = %s AND r.status <> 'waiting' ORDER BY r.decided_at DESC, r.id DESC LIMIT %s", (ctx.parish_id, limit))
        rows = c.fetchall()
    for r in rows:
        r["name"] = person_label(r)
    return rows


def _lock_waiting(c, ctx: Ctx, request_id: int, approver_email: str | None) -> dict:
    c.execute("SELECT * FROM donor.pledge_request WHERE id = %s AND parish_id = %s FOR UPDATE", (request_id, ctx.parish_id))
    req = c.fetchone()
    if not req:
        raise NotFound("That request was not found at this parish.")
    if req["status"] != "waiting":
        raise Conflict("That request has already been answered.")
    email = (approver_email or "").strip().lower()
    if email:
        c.execute("SELECT 1 AS x FROM donor.person_contact WHERE person_id = ANY(%s) AND kind = 'email' AND archived_at IS NULL AND LOWER(value) = %s",
                  ([x for x in (req["person_id"], req["joint_with_person_id"]) if x], email))
        if c.fetchone():
            raise PermissionDenied("Someone else has to answer your own request.")
    return req


def request_approve(ctx: Ctx, request_id: int, data: dict | None = None, approver_email: str | None = None, *, cur=None) -> dict:
    """Approve: run the existing pledge service with the requested values (Finance may correct the amount, how often and the dates
    first) in the same transaction, then record the answer and the pledge id on the request. Any refusal from the pledge service
    (a duplicate, a closed campaign, a fund that no longer accepts pledges, dates outside the campaign, a spouse no longer linked)
    leaves the request waiting and tells Finance why."""
    _need_manage(ctx)
    data = data or {}
    note = clean_text(data.get("decision_note"), field="note", max_len=200)
    with tx(cur) as c:
        req = _lock_waiting(c, ctx, request_id, approver_email)
        why = note
        if req["kind"] == "new":
            amount = to_money(data.get("amount"), field="pledge amount") if str(data.get("amount") or "").strip() else req["amount"]
            if amount != req["amount"]:
                why = (why + " " if why else "") + f"(Finance changed the amount from ${req['amount']:,.2f} to ${amount:,.2f}.)"
            pn = f"Online request #{request_id}" + (f": {req['note']}" if req["note"] else "")
            res = PL.pledge_create(ctx, {
                "person_id": req["person_id"], "joint_with_person_id": req["joint_with_person_id"], "campaign_id": req["campaign_id"],
                "amount": amount, "frequency": (data.get("frequency") or req["frequency"]),
                "start_date": (data.get("start_date") or req["start_date"]), "end_date": (data.get("end_date") or req["end_date"]),
                "notes": pn[:300]}, cur=c)
            pledge_id = res["id"]
        elif req["kind"] == "change":
            amount = to_money(data.get("amount"), field="pledge amount") if str(data.get("amount") or "").strip() else req["amount"]
            if amount != req["amount"]:
                why = (why + " " if why else "") + f"(Finance changed the amount from ${req['amount']:,.2f} to ${amount:,.2f}.)"
            PL.pledge_update(ctx, req["pledge_id"], {"amount": amount, "frequency": (data.get("frequency") or req["frequency"])}, cur=c)
            pledge_id = req["pledge_id"]
        else:
            PL.pledge_cancel(ctx, req["pledge_id"], note or f"Cancelled at the parishioner's request (online request #{request_id})", cur=c)
            pledge_id = req["pledge_id"]
        c.execute("UPDATE donor.pledge_request SET status = 'approved', decided_by_user_id = %s, decided_at = NOW(), decision_reason = %s, "
                  "resulting_pledge_id = %s WHERE id = %s", (ctx.user_id, (why or None) and why[:300], pledge_id, request_id))
        log_change(c, ctx, "pledge_request", request_id, "status", "waiting", "approved", person_id=req["person_id"], scope="parish", reason=why)
        return {"id": request_id, "pledge_id": pledge_id, "kind": req["kind"]}


def request_decline(ctx: Ctx, request_id: int, reason, approver_email: str | None = None, *, cur=None) -> dict:
    _need_manage(ctx)
    why = clean_text(reason, field="reason", max_len=200)
    if not why:
        raise InvalidInput("Say why the request is declined: the parishioner will see it.", "reason")
    with tx(cur) as c:
        req = _lock_waiting(c, ctx, request_id, approver_email)
        c.execute("UPDATE donor.pledge_request SET status = 'declined', decided_by_user_id = %s, decided_at = NOW(), decision_reason = %s WHERE id = %s",
                  (ctx.user_id, why, request_id))
        log_change(c, ctx, "pledge_request", request_id, "status", "waiting", "declined", person_id=req["person_id"], scope="parish", reason=why)
        return {"id": request_id}
