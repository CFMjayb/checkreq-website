"""
payroll_late.py -- the Late Email Queue: emailed hours that are HELD for a person instead of recorded (26-158 / 26-129, 2026-10-27).
Plan: 26-158 DME Payroll Inbox Mining\\Plan - Late Email Queue.md (approved by Jay 2026-10-27). Table: portal.time_late_email (migration 105).

THE HARD RULE: hours only. Nothing here reads, stores or returns a rate, salary, wage or any amount of money.

Why it exists: once a pay period's hours are marked Final the reader must stop changing them. An email with hours that arrives after Final
or after Close, one the reader cannot place with a church, or one that fits no pay period yet, is put here with the people and hours the
reader found. A person decides, one email at a time:
  record_item(mode='reopen')   Reopen and record: the period is no longer Final (a Closed one is reopened), the hours are recorded for review
  record_item(mode='carry')    Carry to next period: the same recording, into the next pay period
  record_item(mode='manual')   Pay by manual check (Jay: "follow the same process" as the tie-out): the hours are recorded in the pay period
                               they belong to with a note on each line, the paid register will not have them, the Variance report shows the
                               difference and the person marks it Manual check THERE. No tie-out decision is written here.
  dismiss()                    Dismiss as not hours (or, from the repeat check, as already entered)
  pick_church()                for "Church unknown": names the church, then the same buttons work
Every action does the repeat check again at the moment of the click (payroll_late_rules.line_state), records only lines that are not repeats,
and leaves a decided row decided: a later read of the same email never reopens it.

The alert (send_alert): one email per batch to the diocese's alert address about emails that arrived after Final. It names the department,
the sender, the subject and the time. It carries NO hours figures.
"""
from __future__ import annotations

import datetime as dt
import html as _html
import json
from decimal import Decimal

from psycopg import errors as pgerr

import app_settings
import beacon_address
import db
import email_client
import email_pdf
import payroll_late_rules as R
import payroll_totals as pt

MAX_LINES = 60
DEFAULT_ALERT_TO = {"DME": "accounting@episcopalmaine.org"}      # per diocese code; a setting overrides it
MODES = {"reopen": ("recorded", "Recorded in the pay period"),
         "carry": ("carried", "Carried to the next pay period"),
         "manual": ("manual_check", "Recorded to be paid by manual check")}
MANUAL_NOTE = "Late email: to be paid by manual check (mark it Manual check on the Variance report)"


# ---------------------------------------------------------------- putting an email in the queue

def _text(value, n: int) -> str | None:
    t = " ".join(str(value or "").split())
    if not t:
        return None
    if pt.pii_problem(t):
        return "(withheld: sensitive data)"[:n]
    return t[:n]


def _clean_lines(lines) -> list[dict]:
    if not isinstance(lines, list) or len(lines) > MAX_LINES:
        raise pt.PayrollError("invalid", f"lines must be a list of up to {MAX_LINES} entries.")
    out = []
    for ln in lines:
        if not isinstance(ln, dict):
            raise pt.PayrollError("invalid", "Every line must be an object.")
        who = " ".join(str(ln.get("employee") or "").split())[:80]
        if not who:
            continue                                  # a line with no name tells a person nothing
        hours = {}
        for k, v in (ln.get("hours") or {}).items():
            try:
                h = pt.parse_hours(v)
            except pt.PayrollError:
                continue                              # an unreadable figure is left out, the name and quote still show
            if h > 0:
                hours[str(k).strip().lower()[:30]] = str(h)
        try:
            quote = pt.clean_quote(ln.get("quote"))
        except pt.PayrollError:
            quote = None
        out.append({"employee": who, "parish_code": (str(ln.get("parish_code") or "").strip()[:10] or None),
                    "employee_number": (str(ln.get("employee_number") or "").strip()[:20] or None),
                    "hours": hours, "quote": quote})
    return out


def put_late(org_id: int, *, source_ref: str, reason: str, kind: str = "hours", period_id: int | None = None,
             parish_code: str | None = None, received_at=None, sender=None, subject=None, lines=None) -> dict:
    """Holds one email. One row per email and diocese: a waiting row is replaced by a later read, a decided row is never touched.
    Returns {action: created | updated | decided, id, reason}."""
    ref = (source_ref or "").strip()
    if not ref:
        raise pt.PayrollError("invalid", "source_ref (the message id) is required.")
    if reason not in R.REASONS:
        raise pt.PayrollError("invalid", f"reason must be one of {', '.join(R.REASONS)}.")
    if kind not in R.KINDS:
        raise pt.PayrollError("invalid", "kind must be hours or change.")
    if period_id is not None:
        pt.get_period(org_id, int(period_id))
    parish_id = pt._parish(org_id, parish_code)["id"] if parish_code else None
    clean = _clean_lines(lines or [])
    snd, sub = _text(sender, 120), _text(subject, 200)
    existing = db.query_one("SELECT id, status FROM portal.time_late_email WHERE org_id = %s AND source_ref = %s", (org_id, ref))
    with db.connect() as conn:
        with conn.cursor() as cur:
            if existing:
                if existing["status"] != "waiting":
                    return {"action": "decided", "id": existing["id"], "reason": reason}
                cur.execute("UPDATE portal.time_late_email SET period_id = %s, parish_id = %s, reason = %s, kind = %s, received_at = %s, "
                            "sender = %s, subject = %s, lines = %s::jsonb, updated_at = now() WHERE id = %s AND status = 'waiting'",
                            (period_id, parish_id, reason, kind, received_at, snd, sub, json.dumps(clean), existing["id"]))
                conn.commit()
                return {"action": "updated", "id": existing["id"], "reason": reason}
            cur.execute("INSERT INTO portal.time_late_email (org_id, period_id, parish_id, source_ref, reason, kind, received_at, sender, "
                        "subject, lines) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING id",
                        (org_id, period_id, parish_id, ref, reason, kind, received_at, snd, sub, json.dumps(clean)))
            new_id = cur.fetchone()["id"]
            conn.commit()
    return {"action": "created", "id": new_id, "reason": reason}


# ---------------------------------------------------------------- reading the queue (with the repeat check)

def next_period(org_id: int, period_id: int | None) -> dict | None:
    """The pay period a late email would be carried into: the next open or future one after its own, else the first open or future one."""
    if period_id:
        return db.query_one(
            "SELECT id, label, status FROM portal.payroll_periods WHERE org_id = %s AND status IN ('open', 'future') "
            "AND period_start > (SELECT period_start FROM portal.payroll_periods WHERE id = %s) ORDER BY period_start LIMIT 1",
            (org_id, period_id))
    return db.query_one("SELECT id, label, status FROM portal.payroll_periods WHERE org_id = %s AND status IN ('open', 'future') "
                        "ORDER BY period_start LIMIT 1", (org_id,))


def _annotate(org_id: int, item: dict, period_id: int | None) -> None:
    """Adds the repeat check to every line of one waiting item, against the given pay period."""
    all_same, n_new, n_same = True, 0, 0
    for ln in item["lines"]:
        code = ln.get("parish_code") or item.get("parish_code")
        cells, states = [], []
        for cat, h in (ln.get("hours") or {}).items():
            st = R.line_state(org_id, period_id, code, ln.get("employee_number"), cat, h)
            cells.append({"category": cat, "hours": h, **st})
            states.append(st["state"])
        if not cells:
            ln["state"] = "nohours"
        elif R.UNMATCHED in states:
            ln["state"] = R.UNMATCHED
        elif R.DIFFERENT in states:
            ln["state"] = R.DIFFERENT
        elif R.NEW in states:
            ln["state"] = R.NEW
        else:
            ln["state"] = R.SAME
        ln["cells"] = cells
        n_same += sum(1 for c in cells if c["state"] == R.SAME)
        n_new += sum(1 for c in cells if c["state"] != R.SAME)
        if ln["state"] != R.SAME:
            all_same = False
    item["all_repeats"] = bool(item["lines"]) and all_same
    item["repeat_cells"], item["open_cells"] = n_same, n_new


def list_items(org_id: int, *, decided: bool = False) -> list[dict]:
    """The queue, oldest first. Waiting rows carry the repeat check on every line, the PDF address and what each button may do."""
    cond = "l.status <> 'waiting'" if decided else "l.status = 'waiting'"
    order = "l.handled_at DESC NULLS LAST, l.id DESC" if decided else "l.received_at NULLS LAST, l.id"
    try:
        rows = db.query(
            "SELECT l.*, pp.label AS period_label, pp.status AS period_status, pp.hours_finalized_at, p.code AS parish_code, "
            "p.name AS parish_name, COALESCE(p.short_name, p.name) AS dept_name, tp.label AS target_label, "
            "u.display_name AS handled_by_name FROM portal.time_late_email l "
            "LEFT JOIN portal.payroll_periods pp ON pp.id = l.period_id LEFT JOIN portal.parishes p ON p.id = l.parish_id "
            "LEFT JOIN portal.payroll_periods tp ON tp.id = l.target_period_id "
            "LEFT JOIN checkreq.app_users u ON u.id = l.handled_by_user_id "
            f"WHERE l.org_id = %s AND {cond} ORDER BY {order}", (org_id,))
    except pgerr.UndefinedTable:
        return []
    for r in rows:
        r["lines"] = r["lines"] if isinstance(r["lines"], list) else json.loads(r["lines"] or "[]")
        r["pdf_url"] = email_pdf.url_for(org_id, r["source_ref"])
        r["reason_label"] = R.REASON_LABEL.get(r["reason"], r["reason"])
        r["status_label"] = R.STATUS_LABEL.get(r["status"], r["status"])
        if decided:
            continue
        nxt = next_period(org_id, r["period_id"])
        r["next_period"] = nxt
        r["needs_church"] = r["parish_id"] is None and any(not ln.get("parish_code") for ln in r["lines"])
        r["can_reopen"] = bool(r["period_id"]) and r["kind"] == "hours"
        _annotate(org_id, r, r["period_id"])
    return rows


# ---------------------------------------------------------------- the actions

def _load_waiting(org_id: int, item_id: int) -> dict:
    row = db.query_one("SELECT l.*, p.code AS parish_code FROM portal.time_late_email l "
                       "LEFT JOIN portal.parishes p ON p.id = l.parish_id WHERE l.id = %s AND l.org_id = %s", (item_id, org_id))
    if not row:
        raise pt.PayrollError("not_found", "That late email was not found.")
    if row["status"] != "waiting":
        raise pt.PayrollError("refused", "That late email has already been decided.")
    row["lines"] = row["lines"] if isinstance(row["lines"], list) else json.loads(row["lines"] or "[]")
    return row


def _finish(org_id: int, item_id: int, user_id: int, status: str, note: str | None, *, target_period_id=None, dismiss_reason=None) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE portal.time_late_email SET status = %s, target_period_id = %s, dismiss_reason = %s, handled_by_user_id = %s, "
                        "handled_at = now(), handled_note = %s, updated_at = now() WHERE id = %s AND org_id = %s AND status = 'waiting'",
                        (status, target_period_id, dismiss_reason, user_id, (note or "")[:300] or None, item_id, org_id))
            conn.commit()


def _make_ready(org_id: int, period_id: int, user_id: int) -> None:
    """Gets a pay period able to take hours again: a Closed one is reopened (as Time Review's Set status does, and its Actualized stamp
    cleared because the hours are about to change), and a Final one is no longer Final."""
    p = pt.get_period(org_id, period_id)
    if p["status"] == "closed":
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE portal.payroll_periods SET status = 'open', processed_by_user_id = NULL, processed_at = NULL, "
                            "actualized_at = NULL, actualized_by_user_id = NULL, actualized_automatically = FALSE, updated_at = now() "
                            "WHERE id = %s AND org_id = %s", (period_id, org_id))
                conn.commit()
    if p.get("hours_finalized_at"):
        pt.unfinalize_period(org_id, period_id)


def _plan(org_id: int, item: dict, target_id: int) -> dict:
    """What recording this item into the target period would do, repeat check included: {record: [...], repeats: n, unplaced: [...]}."""
    record, unplaced, repeats = [], [], 0
    for ln in item["lines"]:
        code = ln.get("parish_code") or item.get("parish_code")
        hours = ln.get("hours") or {}
        if ln.get("employee_number"):
            if not code:
                raise pt.PayrollError("refused", "Pick the church first: a person in this email has no church yet.")
            for cat, h in hours.items():
                if R.line_state(org_id, target_id, code, ln["employee_number"], cat, h)["state"] == R.SAME:
                    repeats += 1
                else:
                    record.append({"line": ln, "category": cat, "hours": h, "parish_code": code})
        elif ln.get("employee") and (hours or ln.get("quote")):
            if not code:
                raise pt.PayrollError("refused", "Pick the church first: a person in this email has no church yet.")
            unplaced.append({"parish_code": code, "employee": ln["employee"], "reason": "no_match" if hours else "no_figure",
                             "hours": {k: float(v) for k, v in hours.items()} or None, "quote": ln.get("quote")})
    return {"record": record, "repeats": repeats, "unplaced": unplaced}


def record_item(org_id: int, item_id: int, user_id: int, mode: str) -> dict:
    """Reopen and record / Carry to next period / Pay by manual check. Records only lines that are not repeats."""
    if mode not in MODES:
        raise pt.PayrollError("invalid", "mode must be reopen, carry or manual.")
    item = _load_waiting(org_id, item_id)
    if item["kind"] != "hours":
        raise pt.PayrollError("refused", "Only an email with hours can be recorded. Dismiss this one or pick the church.")
    if mode == "carry":
        nxt = next_period(org_id, item["period_id"])
        if not nxt:
            raise pt.PayrollError("refused", "There is no next pay period to carry these hours into yet.")
        target_id = nxt["id"]
    else:
        if not item["period_id"]:
            raise pt.PayrollError("refused", "No pay period fits this email yet. Carry it to the next period, or dismiss it.")
        target_id = item["period_id"]
    plan = _plan(org_id, item, target_id)
    if not plan["record"] and not plan["unplaced"]:
        raise pt.PayrollError("refused", "Every line of this email is already entered, so nothing is left to record. "
                                         "Use Dismiss as already entered.")
    _make_ready(org_id, target_id, user_id)
    done, errors = 0, []
    for r in plan["record"]:
        ln = r["line"]
        try:
            out = pt.record_period_hours(org_id, period_id=target_id, parish_code=r["parish_code"], category_key=r["category"],
                                         hours=r["hours"], source="email", source_ref=item["source_ref"], quote=ln.get("quote"),
                                         employee_number=ln["employee_number"], actor_user_id=user_id)
            done += 1
            if mode == "manual" and out.get("line_id"):
                with db.connect() as conn:
                    with conn.cursor() as cur:
                        st = out.get("review_status") or "needs_review"
                        pt._log_edit(cur, out["line_id"], Decimal(r["hours"]), Decimal(r["hours"]), st, st, user_id, "diocese", MANUAL_NOTE)
                        conn.commit()
        except pt.PayrollError as e:
            errors.append(f"{ln['employee']}: {e.message}")
    if plan["unplaced"]:
        pt.replace_unentered(org_id, period_id=target_id, source_ref=item["source_ref"], items=plan["unplaced"])
    if not done and not plan["unplaced"]:
        raise pt.PayrollError("refused", "Nothing could be recorded: " + "; ".join(errors)[:260])
    status, label = MODES[mode]
    note = label + (f"; {plan['repeats']} line(s) already entered were skipped" if plan["repeats"] else "") \
        + (f"; {len(plan['unplaced'])} person(s) went to the not-entered list" if plan["unplaced"] else "") \
        + (f"; refused: {'; '.join(errors)}" if errors else "")
    _finish(org_id, item_id, user_id, status, note, target_period_id=target_id)
    return {"status": status, "recorded": done, "repeats_skipped": plan["repeats"], "not_entered_list": len(plan["unplaced"]),
            "errors": errors, "target_period_id": target_id}


def dismiss(org_id: int, item_id: int, user_id: int, *, reason: str = "not_hours", note: str | None = None) -> dict:
    if reason not in ("not_hours", "repeat"):
        raise pt.PayrollError("invalid", "reason must be not_hours or repeat.")
    _load_waiting(org_id, item_id)
    _finish(org_id, item_id, user_id, "dismissed", note or ("Already entered" if reason == "repeat" else "Not hours"), dismiss_reason=reason)
    return {"status": "dismissed", "reason": reason}


def dismiss_repeats(org_id: int, user_id: int) -> dict:
    """One button for every waiting email whose every line is already entered."""
    n = 0
    for it in list_items(org_id):
        if it["kind"] == "hours" and it["all_repeats"]:
            try:
                dismiss(org_id, it["id"], user_id, reason="repeat")
                n += 1
            except pt.PayrollError:
                pass
    return {"dismissed": n}


def pick_church(org_id: int, item_id: int, parish_id: int, user_id: int) -> dict:
    """Names the church of a 'Church unknown' email. A pay period that still takes hours sends the people to that church's not-entered
    list at once. A Final or Closed one keeps the email waiting for the four buttons, with its reason updated."""
    item = _load_waiting(org_id, item_id)
    parish = db.query_one("SELECT id, code, name FROM portal.parishes WHERE id = %s AND org_id = %s AND is_active", (parish_id, org_id))
    if not parish or not parish["code"]:
        raise pt.PayrollError("not_found", "That department was not found.")
    for ln in item["lines"]:
        ln["parish_code"] = ln.get("parish_code") or parish["code"]
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE portal.time_late_email SET parish_id = %s, lines = %s::jsonb, updated_at = now() WHERE id = %s",
                        (parish["id"], json.dumps(item["lines"]), item_id))
            conn.commit()
    if not item["period_id"]:
        return {"church": parish["code"], "moved": False}
    per = pt.get_period(org_id, item["period_id"])
    if per["status"] in pt.WRITABLE_PERIOD_STATUSES and not per.get("hours_finalized_at"):
        people = [{"parish_code": ln["parish_code"], "employee": ln["employee"], "reason": "no_match" if ln.get("hours") else "no_figure",
                   "hours": {k: float(v) for k, v in (ln.get("hours") or {}).items()} or None, "quote": ln.get("quote")}
                  for ln in item["lines"] if ln.get("employee")]
        pt.replace_unentered(org_id, period_id=item["period_id"], source_ref=item["source_ref"], items=people)
        _finish(org_id, item_id, user_id, "recorded", f"Church picked: {parish['code']} {parish['name']}. The people are on that "
                                                      "department's not-entered list.", target_period_id=item["period_id"])
        return {"church": parish["code"], "moved": True}
    new_reason = "period_closed" if per["status"] == "closed" else "period_final"
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE portal.time_late_email SET reason = %s, updated_at = now() WHERE id = %s", (new_reason, item_id))
            conn.commit()
    return {"church": parish["code"], "moved": False, "reason": new_reason}


# ---------------------------------------------------------------- the alert

def alert_address(org_id: int) -> str | None:
    got = (app_settings.get_setting(f"payroll_late_alert_to:{org_id}") or "").strip()
    if got:
        return got
    org = db.query_one("SELECT code FROM checkreq.organizations WHERE id = %s", (org_id,))
    return DEFAULT_ALERT_TO.get((org or {}).get("code") or "")


def _when_text(v) -> str:
    if not v:
        return "time unknown"
    try:
        from zoneinfo import ZoneInfo
        return v.astimezone(ZoneInfo("America/New_York")).strftime("%m/%d/%y %I:%M %p ET")
    except Exception:
        return str(v)[:16]


def send_alert(org_id: int) -> dict:
    """One email about every waiting 'After Final' email that has not been alerted yet. No hours figures. A failed send leaves them
    un-alerted so the next run tries again. Returns {sent: n, to}."""
    try:
        rows = db.query("SELECT id FROM portal.time_late_email WHERE org_id = %s AND reason = 'period_final' AND status = 'waiting' "
                        "AND alerted_at IS NULL ORDER BY received_at NULLS LAST, id", (org_id,))
    except pgerr.UndefinedTable:
        return {"sent": 0, "why": "the queue table is not there yet"}
    if not rows:
        return {"sent": 0}
    to = alert_address(org_id)
    if not to:
        return {"sent": 0, "why": "no alert address is set for this diocese"}
    ids = {r["id"] for r in rows}
    items = [i for i in list_items(org_id) if i["id"] in ids]
    host = beacon_address.beacon_address(org_id) if email_client._BEACON_ENV == "prod" else "beacondev.cfmins.org"
    link = f"https://{host}/admin/timekeeping/late"
    n = len(items)
    subject = f"Payroll hours arrived after Final: {n} email{'s' if n != 1 else ''} to review"
    lines_text, lines_html = [], []
    for i in items:
        dept = f"{i['parish_code']} {i['dept_name']}" if i.get("parish_code") else "Church unknown"
        repeat = " (looks like a repeat of hours already entered)" if i.get("all_repeats") else ""
        row = f"{_when_text(i['received_at'])} - {dept} - from {i['sender'] or 'unknown sender'} - \"{i['subject'] or '(no subject)'}\"{repeat}"
        lines_text.append("- " + row)
        lines_html.append("<li>" + _html.escape(row) + "</li>")
    intro = (f"{n} payroll email{'s' if n != 1 else ''} with hours arrived after the pay period's hours were marked Final. "
             "Nothing was recorded. Decide each one in the Late emails queue:")
    text = intro + "\n\n" + "\n".join(lines_text) + f"\n\n{link}\n"
    body_html = f"<p>{_html.escape(intro)}</p><ul>{''.join(lines_html)}</ul><p><a href=\"{_html.escape(link)}\">Open the Late emails queue</a></p>"
    result = email_client.send_email(to=to, subject=subject, body_html=body_html, body_text=text, sender="", timeout=30)
    if result.get("status") != "sent":
        return {"sent": 0, "to": to, "error": str(result.get("error") or result.get("status") or "no answer from the email server")[:200]}
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE portal.time_late_email SET alerted_at = now() WHERE id = ANY(%s)", (list(ids),))
            conn.commit()
    return {"sent": n, "to": to}
