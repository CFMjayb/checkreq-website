"""
donor_membership.py -- Beacon Donor Management: membership, canonical standing, sacramental records,
transfer letters. Everything here is PARISH-SCOPED: it is visible only to the parish that entered it,
even when the person is connected to other parishes (MS-10, PF-19).

Operations: status_code_list  status_code_save  membership_update  standing_review  adult_member_status
            sacrament_add  sacrament_list  sacrament_void  transfer_letter_update  transfer_letter_list

Retention (RT-01): sacramental registers are kept permanently, so a record is VOIDED with a reason,
never deleted. Canonical standing under Canon I.17 (communicant, communicant in good standing) rests on
worship and Communion, which Beacon does not track, so clergy set it by hand with the date last reviewed.
The wording of Canon I.17 is under revision (Requirements doc, MS-03): the four standing values are kept
in one list (donor_core.CANONICAL_STANDINGS) so they are easy to adjust.
"""
from __future__ import annotations

import re

import db
from donor_core import (
    Conflict, Ctx, InvalidInput, NotFound, age_on, check_enum, clean_text, diff_fields, log_change,
    need_people, new_batch_id, parse_date, to_id, tx, today,
    CANONICAL_STANDINGS, DIOCESAN_CATEGORIES, HOW_JOINED, LEAVING_CATEGORIES, MEMBER_CATEGORIES,
    REMOVAL_REASONS, SACRAMENT_KINDS, TRANSFER_DIRECTIONS, TRANSFER_STATUSES,
)
from donor_people import require_connection
from donor_roles import ensure_default_status_codes


# ── Member status codes (each parish edits its own list, each mapped to a fixed diocesan category) ──
def status_code_list(ctx: Ctx, include_inactive: bool = False) -> list[dict]:
    need_people(ctx, "membership.view")
    with tx() as c:
        ensure_default_status_codes(c, ctx.parish_id)
        c.execute("SELECT * FROM donor.member_status_code WHERE parish_id = %s"
                  + ("" if include_inactive else " AND is_active") + " ORDER BY sort_order, label", (ctx.parish_id,))
        return c.fetchall()


def status_code_save(ctx: Ctx, *, code_id: int | None = None, code: str | None = None, label: str | None = None,
                     diocesan_category: str | None = None, sort_order: int | None = None,
                     is_active: bool | None = None, cur=None) -> dict:
    need_people(ctx, "membership.edit")
    cat = check_enum(diocesan_category, DIOCESAN_CATEGORIES, field="diocesan category") if diocesan_category else None
    lbl = clean_text(label, field="label", max_len=60)
    with tx(cur) as c:
        if code_id is None:
            code_v = (code or "").strip().upper()
            if not re.fullmatch(r"[A-Z0-9_]{1,20}", code_v):
                raise InvalidInput("A code is 1 to 20 letters, numbers or underscores.", "code")
            if not lbl or not cat:
                raise InvalidInput("A new status code needs a label and a diocesan category.")
            c.execute("SELECT 1 AS x FROM donor.member_status_code WHERE parish_id = %s AND code = %s", (ctx.parish_id, code_v))
            if c.fetchone():
                raise Conflict("That code already exists at this parish.")
            c.execute("INSERT INTO donor.member_status_code (parish_id, code, label, diocesan_category, sort_order) "
                      "VALUES (%s,%s,%s,%s,%s) RETURNING id", (ctx.parish_id, code_v, lbl, cat, int(sort_order or 100)))
            cid = c.fetchone()["id"]
            log_change(c, ctx, "member_status_code", cid, None, None, code_v, kind="create", scope="parish")
            return {"id": cid, "created": True}
        c.execute("SELECT * FROM donor.member_status_code WHERE id = %s AND parish_id = %s FOR UPDATE", (code_id, ctx.parish_id))
        old = c.fetchone()
        if not old:
            raise NotFound("That status code was not found at this parish.")
        new: dict = {}
        if lbl:
            new["label"] = lbl
        if cat:
            new["diocesan_category"] = cat
        if sort_order is not None:
            new["sort_order"] = int(sort_order)
        if is_active is not None:
            new["is_active"] = bool(is_active)
        diffs = diff_fields(old, new)
        if diffs:
            c.execute(f"UPDATE donor.member_status_code SET {', '.join(f'{k} = %s' for k, _, _ in diffs)} WHERE id = %s",
                      (*[v for _, _, v in diffs], code_id))
            for k, o, n in diffs:
                log_change(c, ctx, "member_status_code", code_id, k, o, n, scope="parish")
        return {"id": code_id, "created": False, "changed": [k for k, _, _ in diffs]}


# ── Membership ──────────────────────────────────────────────────────────────────────────────────
def _membership_row(c, person_id: int, parish_id: int, lock: bool = False) -> dict | None:
    c.execute("SELECT * FROM donor.membership WHERE person_id = %s AND parish_id = %s" + (" FOR UPDATE" if lock else ""),
              (person_id, parish_id))
    return c.fetchone()


def _ensure_membership(c, ctx: Ctx, person_id: int) -> dict:
    row = _membership_row(c, person_id, ctx.parish_id, lock=True)
    if row:
        return row
    c.execute("INSERT INTO donor.membership (person_id, parish_id, created_by_user_id, updated_by_user_id) "
              "VALUES (%s,%s,%s,%s) RETURNING id", (person_id, ctx.parish_id, ctx.user_id, ctx.user_id))
    log_change(c, ctx, "membership", c.fetchone()["id"], None, None, "Created", person_id=person_id, kind="create", scope="parish")
    return _membership_row(c, person_id, ctx.parish_id, lock=True)


def membership_update(ctx: Ctx, person_id: int, data: dict, *, cur=None) -> dict:
    """Set the member status, how and when they joined, and removal date and reason (MS-01, MS-04).
    A status in a member category makes the connection kind 'member'. A leaving category (transferred out,
    removed, deceased) clears the canonical flag, since the person is no longer a canonical member here."""
    need_people(ctx, "membership.edit")
    new: dict = {}
    if "how_joined" in data:
        new["how_joined"] = check_enum(data["how_joined"], HOW_JOINED, field="how joined")
    if "join_date" in data:
        new["join_date"] = parse_date(data["join_date"], field="join date", allow_future=False)
    if "removal_date" in data:
        new["removal_date"] = parse_date(data["removal_date"], field="removal date", allow_future=False)
    if "removal_reason" in data:
        new["removal_reason"] = check_enum(data["removal_reason"], REMOVAL_REASONS, field="removal reason")
    with tx(cur) as c:
        conn = require_connection(c, ctx, person_id, include_archived=False, lock=True)
        category = None
        if "status_code_id" in data or "status_code" in data:
            sid, scode = data.get("status_code_id"), (data.get("status_code") or "").strip().upper()
            if sid in (None, "") and not scode:
                new["status_code_id"] = None                      # clearing the status
            else:
                if sid not in (None, ""):
                    c.execute("SELECT * FROM donor.member_status_code WHERE id = %s AND parish_id = %s AND is_active",
                              (to_id(sid, field="status_code_id", label="a status"), ctx.parish_id))
                else:
                    c.execute("SELECT * FROM donor.member_status_code WHERE code = %s AND parish_id = %s AND is_active",
                              (scode, ctx.parish_id))
                sc = c.fetchone()
                if not sc:
                    raise InvalidInput("That member status is not one of this parish's codes.", "status_code")
                new["status_code_id"] = sc["id"]
                category = sc["diocesan_category"]
        old = _ensure_membership(c, ctx, person_id)
        merged_join, merged_rem = new.get("join_date", old["join_date"]), new.get("removal_date", old["removal_date"])
        if merged_join and merged_rem and merged_rem < merged_join:
            raise InvalidInput("The removal date cannot be before the join date.", "removal_date")
        diffs = diff_fields(old, new)
        batch = new_batch_id()
        if diffs:
            c.execute(f"UPDATE donor.membership SET {', '.join(f'{k} = %s' for k, _, _ in diffs)}, updated_at = NOW(), "
                      "updated_by_user_id = %s WHERE id = %s", (*[v for _, _, v in diffs], ctx.user_id, old["id"]))
            for k, o, n in diffs:
                log_change(c, ctx, "membership", old["id"], k, o, n, person_id=person_id, scope="parish", batch_id=batch)
        side: list[str] = []
        if category in MEMBER_CATEGORIES and conn["kind"] != "member":
            c.execute("UPDATE donor.parish_connection SET kind = 'member', updated_at = NOW() WHERE id = %s", (conn["id"],))
            log_change(c, ctx, "parish_connection", conn["id"], "kind", conn["kind"], "member", person_id=person_id,
                       scope="parish", batch_id=batch)
            side.append("kind")
        if category in LEAVING_CATEGORIES and conn["is_canonical"]:
            c.execute("UPDATE donor.parish_connection SET is_canonical = FALSE, updated_at = NOW() WHERE id = %s", (conn["id"],))
            log_change(c, ctx, "parish_connection", conn["id"], "is_canonical", "true", "false", person_id=person_id,
                       scope="parish", batch_id=batch)
            side.append("is_canonical")
        return {"person_id": person_id, "changed": [k for k, _, _ in diffs] + side}


def standing_review(ctx: Ctx, person_id: int, standing: str, reviewed_on=None, *, cur=None) -> dict:
    """Clergy set canonical standing by hand and record the date last reviewed (MS-03)."""
    need_people(ctx, "standing.review", "Only clergy can set canonical standing.")
    standing = check_enum(standing, CANONICAL_STANDINGS, field="standing", allow_blank=False)
    when = parse_date(reviewed_on, field="review date", allow_future=False) or today()
    with tx(cur) as c:
        require_connection(c, ctx, person_id, include_archived=False)
        old = _ensure_membership(c, ctx, person_id)
        c.execute("UPDATE donor.membership SET canonical_standing = %s, standing_reviewed_on = %s, "
                  "standing_reviewed_by_user_id = %s, updated_at = NOW(), updated_by_user_id = %s WHERE id = %s",
                  (standing, when, ctx.user_id, ctx.user_id, old["id"]))
        batch = new_batch_id()
        if old["canonical_standing"] != standing:
            log_change(c, ctx, "membership", old["id"], "canonical_standing", old["canonical_standing"], standing,
                       person_id=person_id, scope="parish", batch_id=batch)
        log_change(c, ctx, "membership", old["id"], "standing_reviewed_on", old["standing_reviewed_on"], when,
                   person_id=person_id, scope="parish", batch_id=batch)
        return {"person_id": person_id, "standing": standing, "reviewed_on": when}


def adult_member_status(ctx: Ctx, person_id: int) -> dict:
    """'Adult member' is CALCULATED (MS-03): 16 or older, or confirmed or received. Only this parish's own
    sacramental records count, because another parish's are never visible here."""
    need_people(ctx, "membership.view")
    with tx() as c:
        require_connection(c, ctx, person_id)
        c.execute("SELECT birth_date FROM donor.person WHERE id = %s", (person_id,))
        bd = c.fetchone()["birth_date"]
        a = age_on(bd)
        if a is not None and a >= 16:
            return {"adult": True, "reason": "age 16 or older"}
        c.execute("SELECT kind FROM donor.sacramental_event WHERE person_id = %s AND parish_id = %s AND voided_at IS NULL "
                  "AND kind IN ('confirmation', 'reception')", (person_id, ctx.parish_id))
        k = c.fetchone()
        if k:
            return {"adult": True, "reason": "confirmed" if k["kind"] == "confirmation" else "received"}
        return {"adult": False, "reason": None if a is not None else "birth date not recorded"}


# ── Sacramental records (entering parish only, permanent) ───────────────────────────────────────
def sacrament_add(ctx: Ctx, person_id: int, kind: str, event_date=None, place: str | None = None,
                  officiant_name: str | None = None, register_ref: str | None = None, notes: str | None = None,
                  related_person_id: int | None = None, date_approximate: bool = False, *, cur=None) -> dict:
    need_people(ctx, "sacrament.edit")
    kind = check_enum(kind, SACRAMENT_KINDS, field="sacrament", allow_blank=False)
    when = parse_date(event_date, field="date", allow_future=False)
    with tx(cur) as c:
        require_connection(c, ctx, person_id, include_archived=False)
        if related_person_id is not None:
            if kind != "marriage":
                raise InvalidInput("Only a marriage names a second person.")
            require_connection(c, ctx, related_person_id, include_archived=False)
        c.execute("INSERT INTO donor.sacramental_event (person_id, parish_id, kind, event_date, date_approximate, place, "
                  "officiant_name, register_ref, related_person_id, notes, entered_by_user_id) "
                  "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                  (person_id, ctx.parish_id, kind, when, bool(date_approximate), clean_text(place, field="place", max_len=200),
                   clean_text(officiant_name, field="officiant", max_len=160), clean_text(register_ref, field="register reference", max_len=80),
                   related_person_id, clean_text(notes, field="notes", max_len=2000), ctx.user_id))
        eid = c.fetchone()["id"]
        log_change(c, ctx, "sacramental_event", eid, kind, None, when, person_id=person_id, kind="create", scope="parish")
        return {"id": eid}


def sacrament_list(ctx: Ctx, person_id: int, include_voided: bool = False) -> list[dict]:
    """Only THIS parish's records. A record another parish entered is not returned, ever."""
    need_people(ctx, "sacrament.view")
    with tx() as c:
        require_connection(c, ctx, person_id)
        c.execute("SELECT e.*, rp.first_name AS related_first, rp.last_name AS related_last FROM donor.sacramental_event e "
                  "LEFT JOIN donor.person rp ON rp.id = e.related_person_id "
                  "WHERE e.person_id = %s AND e.parish_id = %s" + ("" if include_voided else " AND e.voided_at IS NULL")
                  + " ORDER BY e.event_date NULLS LAST, e.id", (person_id, ctx.parish_id))
        return c.fetchall()


def sacrament_void(ctx: Ctx, event_id: int, reason: str, *, cur=None) -> dict:
    """Void a record entered in error. Registers are permanent, so it is kept (hidden by default) with who and why."""
    need_people(ctx, "sacrament.edit")
    why = clean_text(reason, field="reason", max_len=300)
    if not why:
        raise InvalidInput("Say why the record is being voided.", "reason")
    with tx(cur) as c:
        c.execute("SELECT * FROM donor.sacramental_event WHERE id = %s AND parish_id = %s FOR UPDATE", (event_id, ctx.parish_id))
        row = c.fetchone()
        if not row:
            raise NotFound("That record was not found at this parish.")
        if row["voided_at"]:
            return {"id": event_id, "voided": True, "changed": False}
        c.execute("UPDATE donor.sacramental_event SET voided_at = NOW(), voided_by_user_id = %s, void_reason = %s WHERE id = %s",
                  (ctx.user_id, why, event_id))
        log_change(c, ctx, "sacramental_event", event_id, row["kind"], "recorded", "voided", person_id=row["person_id"],
                   kind="void", scope="parish", reason=why)
        return {"id": event_id, "voided": True, "changed": True}


# ── Transfer letters ────────────────────────────────────────────────────────────────────────────
def transfer_letter_update(ctx: Ctx, person_id: int, data: dict, letter_id: int | None = None, *, cur=None) -> dict:
    """Create (letter_id None) or update a transfer letter: direction, the other parish (as text), requested,
    issued and received dates, status. Dates must be in order."""
    need_people(ctx, "transfer.edit")
    new: dict = {}
    if "direction" in data:
        new["direction"] = check_enum(data["direction"], TRANSFER_DIRECTIONS, field="direction", allow_blank=False)
    if "status" in data:
        new["status"] = check_enum(data["status"], TRANSFER_STATUSES, field="status", allow_blank=False)
    for k in ("requested_on", "issued_on", "received_on"):
        if k in data:
            new[k] = parse_date(data[k], field=k.replace("_", " "), allow_future=False)
    for k, n in (("other_parish", 160), ("notes", 1000)):
        if k in data:
            new[k] = clean_text(data[k], field=k.replace("_", " "), max_len=n)
    with tx(cur) as c:
        require_connection(c, ctx, person_id, include_archived=False)
        if letter_id is None:
            if "direction" not in new:
                raise InvalidInput("Say whether the letter is incoming or outgoing.", "direction")
            vals = {"status": "requested", **new}
            _check_letter_dates(vals)
            cols = ["person_id", "parish_id", "created_by_user_id", "updated_by_user_id"] + list(vals)
            c.execute(f"INSERT INTO donor.transfer_letter ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                      [person_id, ctx.parish_id, ctx.user_id, ctx.user_id, *vals.values()])
            lid = c.fetchone()["id"]
            log_change(c, ctx, "transfer_letter", lid, None, None, vals.get("direction"), person_id=person_id,
                       kind="create", scope="parish")
            return {"id": lid, "created": True}
        c.execute("SELECT * FROM donor.transfer_letter WHERE id = %s AND person_id = %s AND parish_id = %s FOR UPDATE",
                  (letter_id, person_id, ctx.parish_id))
        old = c.fetchone()
        if not old:
            raise NotFound("That transfer letter was not found at this parish.")
        _check_letter_dates({**old, **new})
        diffs = diff_fields(old, new)
        if diffs:
            c.execute(f"UPDATE donor.transfer_letter SET {', '.join(f'{k} = %s' for k, _, _ in diffs)}, updated_at = NOW(), "
                      "updated_by_user_id = %s WHERE id = %s", (*[v for _, _, v in diffs], ctx.user_id, letter_id))
            batch = new_batch_id()
            for k, o, n in diffs:
                log_change(c, ctx, "transfer_letter", letter_id, k, o, n, person_id=person_id, scope="parish", batch_id=batch)
        return {"id": letter_id, "created": False, "changed": [k for k, _, _ in diffs]}


def _check_letter_dates(v: dict) -> None:
    req, iss, rec = v.get("requested_on"), v.get("issued_on"), v.get("received_on")
    if req and iss and iss < req:
        raise InvalidInput("The letter cannot be issued before it was requested.", "issued_on")
    if iss and rec and rec < iss:
        raise InvalidInput("The letter cannot be received before it was issued.", "received_on")
    if req and rec and rec < req:
        raise InvalidInput("The letter cannot be received before it was requested.", "received_on")


def transfer_letter_list(ctx: Ctx, person_id: int) -> list[dict]:
    need_people(ctx, "membership.view")
    with tx() as c:
        require_connection(c, ctx, person_id)
        c.execute("SELECT * FROM donor.transfer_letter WHERE person_id = %s AND parish_id = %s ORDER BY id", (person_id, ctx.parish_id))
        return c.fetchall()
