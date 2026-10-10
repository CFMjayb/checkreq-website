"""
payroll_totals.py -- the business logic for emailed (and other) period-total hours.

26-158 (DME Payroll Inbox Mining) + Beacon plan Part B, step 4. NO HTTP in this file: the service
API (payroll_api.py) and the hr_admin screens (payroll_screens.py) are thin layers over these
functions, so the same rules apply whichever door a number comes through.

THE HARD RULE: hours only. Nothing here reads, stores or returns a rate, salary, wage or any
amount of money (migrations 038, 047, 087 and 088 enforce it on the columns).

Every function takes the diocese `org_id` and checks that every parish, employee, period and
line it touches belongs to it. The API takes org_id from the API key, never from a parameter.

THE RULES (plan A5) in one place:
  * One line per employee, period and category.
  * Same hours as the line already holds: nothing changes (re-runs are safe).
  * Different hours on a needs_review or rejected line: the line takes the new figure and goes
    (back) to needs_review.
  * Different hours on a CONFIRMED line: the confirmed figure stays in force, the new one is
    stored as a proposal, and the line returns to needs_review until a reviewer decides.
  * Only open and future periods accept hours. A closed period refuses (a reviewer reopens it).
  * More than 300 hours on a line is refused. 50% above the person's recent average is flagged.
  * A source sentence is cut to 300 characters and refused if it holds personal data.
  * Rows are never deleted. A rejected line keeps its row.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import db

MAX_HOURS = Decimal("300")
QUOTE_MAX = 300
JUMP_FACTOR = Decimal("1.5")
SOURCES = ("email", "parish", "diocese", "standing", "register")
WRITABLE_PERIOD_STATUSES = ("open", "future")
PATTERNS = ("every_period", "most_periods", "two_period_batches", "standing_hours", "occasional", "none")
SERVICE_EMAIL = "payroll-inbox@system.invalid"
# Flags that mean "look at this one", never to be swept up by Accept all.
ATTENTION_FLAGS = ("jump", "not_hourly", "inactive_employee")


class PayrollError(Exception):
    """code: not_found | invalid | refused | pii"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- small helpers

_PII = [
    (re.compile(r"\b\d{3}[- ]\d{2}[- ]\d{4}\b"), "a Social Security number"),
    (re.compile(r"\b\d{8,17}\b"), "a long digit run (an account, routing or ID number)"),
    (re.compile(r"\b(?:routing|account|acct)\b[^.\n]{0,24}\d", re.I), "a bank routing or account number"),
    (re.compile(r"\b(?:born|dob|date of birth|birth ?date|birthday)\b", re.I), "a birth date"),
    (re.compile(r"\b(?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12]\d|3[01])[/-](?:19\d\d)\b"), "a date of birth"),
]


def pii_problem(text: str | None) -> str | None:
    """Why this text may not be stored, or None. (No SSN, bank numbers or birth dates.)"""
    if not text:
        return None
    for rx, what in _PII:
        if rx.search(text):
            return what
    return None


def clean_quote(text: str | None) -> str | None:
    """Trim, refuse personal data, cut to 300 characters."""
    if text is None:
        return None
    t = " ".join(str(text).split())
    if not t:
        return None
    why = pii_problem(t)
    if why:
        raise PayrollError("pii", f"The source sentence looks like it holds {why}; it was not stored.")
    return t[:QUOTE_MAX]


def parse_hours(value) -> Decimal:
    """Hours as a Decimal with two places, 0 to 300. Refuses NaN, infinity, negatives and over 300."""
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise PayrollError("invalid", "Hours must be a number.")
    if not d.is_finite():
        raise PayrollError("invalid", "Hours must be a number.")
    d = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if d < 0:
        raise PayrollError("invalid", "Hours cannot be negative. Corrections to a prior period are entered by hand.")
    if d > MAX_HOURS:
        raise PayrollError("refused", f"{d} hours is more than the 300 hours one line may hold.")
    return d


def _num(v) -> float | None:
    return None if v is None else float(v)


def service_user_id() -> int | None:
    r = db.query_one("SELECT id FROM checkreq.app_users WHERE email = %s", (SERVICE_EMAIL,))
    return r["id"] if r else None


def get_period(org_id: int, period_id: int) -> dict:
    p = db.query_one("SELECT * FROM portal.payroll_periods WHERE id = %s AND org_id = %s", (period_id, org_id))
    if not p:
        raise PayrollError("not_found", "That pay period was not found for this diocese.")
    return p


def _parish(org_id: int, code: str) -> dict:
    code = (code or "").strip()
    p = db.query_one("SELECT id, name, code FROM portal.parishes WHERE org_id = %s AND code = %s AND is_active",
                     (org_id, code))
    if not p:
        raise PayrollError("not_found", f"No active parish with code {code!r} in this diocese.")
    return p


def _category(org_id: int, key: str) -> dict:
    c = db.query_one("SELECT * FROM portal.timekeeping_categories WHERE org_id = %s AND key = %s AND is_active",
                     (org_id, (key or "").strip().lower()))
    if not c:
        raise PayrollError("not_found", f"No active category {key!r} for this diocese.")
    return c


def _staff(parish_id: int, employee_number: str) -> dict:
    s = db.query_one("SELECT * FROM portal.staff_roster WHERE parish_id = %s AND employee_number = %s "
                     "ORDER BY is_active DESC, id LIMIT 1", (parish_id, (employee_number or "").strip()))
    if not s:
        raise PayrollError("not_found", f"Employee number {employee_number!r} is not on this parish's roster.")
    return s


def _unfinalize(cur, period_id: int) -> None:
    cur.execute("UPDATE portal.payroll_periods SET hours_finalized_at = NULL, hours_finalized_by_user_id = NULL "
                "WHERE id = %s AND hours_finalized_at IS NOT NULL", (period_id,))


def _log_edit(cur, total_id, old_hours, new_hours, old_status, new_status, user_id, source, note=None):
    cur.execute(
        "INSERT INTO portal.time_period_total_edits (total_id, old_hours, new_hours, old_review_status, "
        "new_review_status, edited_by_user_id, edit_source, note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (total_id, old_hours, new_hours, old_status, new_status, user_id, source, note))


def _recent_average(cur, staff_id: int, category_id: int, before: dt.date) -> Decimal | None:
    cur.execute(
        "SELECT avg(h) AS a FROM (SELECT t.hours AS h FROM portal.time_period_totals t "
        "JOIN portal.payroll_periods pp ON pp.id = t.period_id "
        "WHERE t.staff_id = %s AND t.category_id = %s AND t.review_status = 'confirmed' "
        "AND pp.period_start < %s ORDER BY pp.period_start DESC LIMIT 6) x", (staff_id, category_id, before))
    r = cur.fetchone()
    return None if not r or r["a"] is None else Decimal(r["a"])


def _flags_for(cur, staff: dict | None, category_id: int, hours: Decimal, period: dict, extra=()) -> list[str]:
    flags = list(extra)
    if staff is not None:
        if not staff.get("captures_hours"):
            flags.append("not_hourly")
        if not staff.get("is_active"):
            flags.append("inactive_employee")
        avg = _recent_average(cur, staff["id"], category_id, period["period_start"])
        if avg is not None and avg > 0 and hours > avg * JUMP_FACTOR:
            flags.append("jump")
    return sorted(set(flags))


# ------------------------------------------------------------------- B2: reads

def list_periods(org_id: int) -> list[dict]:
    rows = db.query(
        "SELECT id, label, period_start, period_end, submission_deadline, pay_date, status, "
        "hours_finalized_at FROM portal.payroll_periods WHERE org_id = %s ORDER BY period_start", (org_id,))
    return rows


def get_roster(org_id: int, parish_code: str | None = None) -> dict:
    """Roster rows (no pay) plus pending new hires. Optionally one parish."""
    args: list = [org_id]
    extra = ""
    if parish_code:
        extra = " AND p.code = %s"
        args.append(parish_code)
    staff = db.query(
        "SELECT p.code AS parish_code, s.id AS staff_id, s.employee_number, s.first_name, s.last_name, "
        "s.position, s.captures_hours, s.hours_basis, s.standing_hours, s.is_active, s.effective_date, "
        "s.inactive_as_of FROM portal.staff_roster s JOIN portal.parishes p ON p.id = s.parish_id "
        "WHERE p.org_id = %s" + extra + " ORDER BY p.code, s.last_name, s.first_name, s.id", args)
    pend = db.query(
        "SELECT p.code AS parish_code, c.id AS pending_change_id, c.proposed_employee_number AS employee_number, "
        "c.proposed_first_name AS first_name, c.proposed_last_name AS last_name, c.proposed_position AS position "
        "FROM portal.staff_roster_changes c JOIN portal.parishes p ON p.id = c.parish_id "
        "WHERE p.org_id = %s AND c.status = 'pending' AND c.change_type = 'add'" + extra +
        " ORDER BY p.code, c.id", args)
    for r in staff:
        for k in ("standing_hours",):
            r[k] = _num(r[k])
    return {"staff": staff, "pending_adds": pend}


def split_contacts(raw) -> tuple[list, dict | None]:
    """portal.parishes.contacts is the envelope {"contacts": [...]} (DME) or, on older rows, a bare
    list. Returns (entries, envelope) so a write can put things back in the shape it found."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if isinstance(raw, dict):
        entries = raw.get("contacts")
        return (list(entries) if isinstance(entries, list) else []), raw
    if isinstance(raw, list):
        return list(raw), None
    return [], {}


def join_contacts(entries: list, envelope: dict | None):
    if envelope is None:
        return entries
    out = dict(envelope)
    out["contacts"] = entries
    return out


def get_senders(org_id: int, parish_code: str | None = None) -> list[dict]:
    args: list = [org_id]
    extra = ""
    if parish_code:
        extra = " AND p.code = %s"
        args.append(parish_code)
    rows = db.query(
        "SELECT p.id AS parish_id, p.code, p.name, p.contacts, pr.pattern, pr.notes "
        "FROM portal.parishes p LEFT JOIN portal.parish_payroll_profile pr ON pr.parish_id = p.id "
        "WHERE p.org_id = %s AND p.is_active AND p.code IS NOT NULL" + extra + " ORDER BY p.code", args)
    out = []
    for r in rows:
        contacts, _env = split_contacts(r["contacts"])
        subs, recips = [], []
        for c in contacts:
            if not isinstance(c, dict):
                continue
            role = c.get("role")
            if role == "time_submitter":
                subs.append({k: c.get(k) for k in ("name", "email", "source", "first_seen", "last_seen")})
            elif role == "payroll":
                recips.append({k: c.get(k) for k in ("name", "email")})
        out.append({"parish_code": r["code"], "parish_name": r["name"], "pattern": r["pattern"],
                    "notes": r["notes"], "time_submitters": subs, "report_recipients": recips})
    return out


# ------------------------------------------------------------- B2: record hours

def record_period_hours(org_id: int, *, period_id: int, parish_code: str, category_key: str, hours,
                        source: str = "email", source_ref: str | None = None, quote: str | None = None,
                        confidence=None, employee_number: str | None = None,
                        pending_change_id: int | None = None, actor_user_id: int | None = None,
                        allow_closed: bool = False, status: str | None = None) -> dict:
    """One line. Applies the A5 rules. Returns {action, line_id, review_status, flags, hours,
    proposed_hours}. action is one of created | unchanged | updated | proposed. Raises PayrollError
    when the line is refused (closed period, unknown employee, over 300 hours, personal data...).

    allow_closed and status are for the history load only (register lines go in confirmed, into
    periods that are already closed)."""
    if source not in SOURCES:
        raise PayrollError("invalid", f"Unknown source {source!r}.")
    if (employee_number is None) == (pending_change_id is None):
        raise PayrollError("invalid", "Give either an employee number or a pending change id, not both and not neither.")
    h = parse_hours(hours)
    q = clean_quote(quote)
    conf = None
    if confidence is not None:
        try:
            conf = Decimal(str(confidence)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            raise PayrollError("invalid", "Confidence must be a number from 0 to 1.")
        if not (Decimal(0) <= conf <= Decimal(1)):
            raise PayrollError("invalid", "Confidence must be a number from 0 to 1.")
    ref = (source_ref or "").strip() or None
    period = get_period(org_id, period_id)
    if period["status"] not in WRITABLE_PERIOD_STATUSES and not allow_closed:
        raise PayrollError("refused", f"This pay period is {period['status']}: hours are not accepted. "
                                      "A reviewer must reopen it first.")
    parish = _parish(org_id, parish_code)
    cat = _category(org_id, category_key)
    staff = None
    if employee_number is not None:
        staff = _staff(parish["id"], employee_number)
    else:
        ch = db.query_one("SELECT id, parish_id, change_type, status FROM portal.staff_roster_changes WHERE id = %s",
                          (pending_change_id,))
        if not ch or ch["parish_id"] != parish["id"] or ch["change_type"] != "add" or ch["status"] != "pending":
            raise PayrollError("not_found", "That pending new hire was not found for this parish.")
    new_status = status or "needs_review"
    if new_status not in ("needs_review", "confirmed"):
        raise PayrollError("invalid", "status must be needs_review or confirmed.")
    actor = actor_user_id if actor_user_id is not None else service_user_id()

    with db.connect() as conn:
        with conn.cursor() as cur:
            subject_sql = "staff_id = %s" if staff else "pending_change_id = %s"
            subject_val = staff["id"] if staff else pending_change_id

            def lock_existing():
                cur.execute(f"SELECT * FROM portal.time_period_totals WHERE period_id = %s AND {subject_sql} "
                            "AND category_id = %s FOR UPDATE", (period_id, subject_val, cat["id"]))
                return cur.fetchone()

            cur_line = lock_existing()
            if cur_line is None:
                flags = _flags_for(cur, staff, cat["id"], h, period,
                                   extra=(["pending_hire"] if staff is None else []))
                cur.execute(
                    "INSERT INTO portal.time_period_totals (period_id, staff_id, pending_change_id, category_id, "
                    "hours, source, source_ref, source_quote, confidence, review_status, flags, "
                    "created_by_user_id, reviewed_by_user_id, reviewed_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING RETURNING id",
                    (period_id, staff["id"] if staff else None, pending_change_id, cat["id"], h, source, ref, q,
                     conf, new_status, flags, actor,
                     actor if new_status == "confirmed" else None,
                     dt.datetime.now(dt.timezone.utc) if new_status == "confirmed" else None))
                ins = cur.fetchone()
                if ins:
                    _log_edit(cur, ins["id"], None, h, None, new_status, actor, source,
                              "Recorded" + (f" ({ref})" if ref else ""))
                    _unfinalize(cur, period_id)
                    conn.commit()
                    return {"action": "created", "line_id": ins["id"], "review_status": new_status,
                            "flags": flags, "hours": float(h), "proposed_hours": None}
                cur_line = lock_existing()  # lost a race: fall through to the update rules
                if cur_line is None:
                    raise PayrollError("refused", "Could not record this line; try again.")

            line = cur_line
            old = Decimal(line["hours"])
            if h == old:
                # same figure: nothing changes. (A proposal equal to the confirmed figure is moot.)
                conn.commit()
                return {"action": "unchanged", "line_id": line["id"], "review_status": line["review_status"],
                        "flags": list(line["flags"] or []), "hours": float(old),
                        "proposed_hours": _num(line["proposed_hours"])}
            if line["review_status"] == "confirmed":
                if line["proposed_hours"] is not None and Decimal(line["proposed_hours"]) == h \
                        and (line["proposed_source_ref"] or None) == ref:
                    conn.commit()
                    return {"action": "unchanged", "line_id": line["id"], "review_status": line["review_status"],
                            "flags": list(line["flags"] or []), "hours": float(old),
                            "proposed_hours": float(h)}
                flags = sorted(set((line["flags"] or [])) | {"proposal"})
                cur.execute(
                    "UPDATE portal.time_period_totals SET proposed_hours = %s, proposed_source_ref = %s, "
                    "proposed_quote = %s, review_status = 'needs_review', flags = %s, updated_at = now() "
                    "WHERE id = %s", (h, ref, q, flags, line["id"]))
                _log_edit(cur, line["id"], old, old, "confirmed", "needs_review", actor, source,
                          f"A different figure ({h}) arrived for a confirmed line" + (f" ({ref})" if ref else ""))
                _unfinalize(cur, period_id)
                conn.commit()
                return {"action": "proposed", "line_id": line["id"], "review_status": "needs_review",
                        "flags": flags, "hours": float(old), "proposed_hours": float(h)}
            # needs_review or rejected: take the new figure, back to needs_review
            flags = _flags_for(cur, staff, cat["id"], h, period,
                               extra=(["pending_hire"] if staff is None else []))
            cur.execute(
                "UPDATE portal.time_period_totals SET hours = %s, source = %s, source_ref = %s, source_quote = %s, "
                "confidence = %s, review_status = %s, flags = %s, proposed_hours = NULL, proposed_source_ref = NULL, "
                "proposed_quote = NULL, reviewed_by_user_id = %s, reviewed_at = %s, updated_at = now() WHERE id = %s",
                (h, source, ref, q, conf, new_status, flags,
                 actor if new_status == "confirmed" else None,
                 dt.datetime.now(dt.timezone.utc) if new_status == "confirmed" else None, line["id"]))
            _log_edit(cur, line["id"], old, h, line["review_status"], new_status, actor, source,
                      "Updated by a later source" + (f" ({ref})" if ref else ""))
            _unfinalize(cur, period_id)
            conn.commit()
            return {"action": "updated", "line_id": line["id"], "review_status": new_status, "flags": flags,
                    "hours": float(h), "proposed_hours": None}


def record_register_hours(org_id: int, *, period_id: int, parish_code: str, employee_number: str,
                          category_key: str, hours, source_ref: str | None = None,
                          actor_user_id: int | None = None) -> dict:
    """One paid-register line (hours only) for the variance report. Allowed in a closed period
    (that is when the register exists). Overwrites the same line's previous figure."""
    h = parse_hours(hours)
    get_period(org_id, period_id)
    parish = _parish(org_id, parish_code)
    staff = _staff(parish["id"], employee_number)
    cat = db.query_one("SELECT * FROM portal.timekeeping_categories WHERE org_id = %s AND key = %s",
                       (org_id, (category_key or "").strip().lower()))
    if not cat:
        raise PayrollError("not_found", f"No category {category_key!r} for this diocese.")
    actor = actor_user_id if actor_user_id is not None else service_user_id()
    ref = (source_ref or "").strip() or None
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, hours FROM portal.time_register_hours WHERE period_id = %s AND staff_id = %s "
                        "AND category_id = %s FOR UPDATE", (period_id, staff["id"], cat["id"]))
            ex = cur.fetchone()
            if ex and Decimal(ex["hours"]) == h:
                conn.commit()
                return {"action": "unchanged"}
            cur.execute(
                "INSERT INTO portal.time_register_hours (period_id, staff_id, category_id, hours, source_ref, "
                "loaded_by_user_id) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (period_id, staff_id, category_id) "
                "DO UPDATE SET hours = EXCLUDED.hours, source_ref = EXCLUDED.source_ref, "
                "loaded_by_user_id = EXCLUDED.loaded_by_user_id, loaded_at = now()",
                (period_id, staff["id"], cat["id"], h, ref, actor))
            conn.commit()
            return {"action": "updated" if ex else "created"}


# ------------------------------------------------------ B2: received / roster / senders

def mark_submission_received(org_id: int, *, period_id: int, parish_code: str, source_ref: str,
                             received_at: dt.datetime | None = None, channel: str = "email") -> dict:
    """Notes that an email arrived for this parish and period. Idempotent on the message id."""
    ref = (source_ref or "").strip()
    if not ref:
        raise PayrollError("invalid", "source_ref (the message id) is required.")
    if channel not in ("email", "diocese", "standing"):
        raise PayrollError("invalid", "channel must be email, diocese or standing.")
    period = get_period(org_id, period_id)
    if period["status"] not in WRITABLE_PERIOD_STATUSES:
        raise PayrollError("refused", f"This pay period is {period['status']}: receipts are not accepted.")
    parish = _parish(org_id, parish_code)
    when = received_at or dt.datetime.now(dt.timezone.utc)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO portal.time_email_receipts (period_id, parish_id, source_ref, received_at) "
                        "VALUES (%s,%s,%s,%s) ON CONFLICT (period_id, parish_id, source_ref) DO NOTHING RETURNING id",
                        (period_id, parish["id"], ref, when))
            new = cur.fetchone() is not None
            cur.execute("INSERT INTO portal.time_entry_submissions (period_id, parish_id) VALUES (%s,%s) "
                        "ON CONFLICT (period_id, parish_id) DO NOTHING", (period_id, parish["id"]))
            cur.execute("SELECT * FROM portal.time_entry_submissions WHERE period_id = %s AND parish_id = %s FOR UPDATE",
                        (period_id, parish["id"]))
            sub = cur.fetchone()
            sets, vals = [], []
            if sub["first_received_at"] is None or when < sub["first_received_at"]:
                sets.append("first_received_at = %s")
                vals.append(when)
            if sub["source_ref"] is None:
                sets.append("source_ref = %s")
                vals.append(ref)
            # A parish that submitted its own hours in Beacon stays channel 'parish'.
            if sub["status"] != "submitted" and sub["channel"] != channel:
                sets.append("channel = %s")
                vals.append(channel)
            if sets:
                cur.execute("UPDATE portal.time_entry_submissions SET " + ", ".join(sets) +
                            ", updated_at = now() WHERE id = %s", vals + [sub["id"]])
            conn.commit()
    return {"action": "created" if new else "unchanged", "submission_id": sub["id"]}


def propose_roster_change(org_id: int, *, parish_code: str, change_type: str, source_ref: str,
                          quote: str | None = None, employee_number: str | None = None,
                          first_name: str | None = None, last_name: str | None = None,
                          position: str | None = None, period_id: int | None = None,
                          captures_hours: bool | None = None, as_of: dt.date | None = None,
                          actor_user_id: int | None = None) -> dict:
    """One PENDING row in the same queue the parishes use (never applied directly). Idempotent on
    source_ref + change type + employee number + last name."""
    if change_type not in ("add", "edit", "deactivate", "reactivate"):
        raise PayrollError("invalid", "change_type must be add, edit, deactivate or reactivate.")
    ref = (source_ref or "").strip()
    if not ref:
        raise PayrollError("invalid", "source_ref (the message id) is required.")
    q = clean_quote(quote)
    parish = _parish(org_id, parish_code)
    emp = (employee_number or "").strip() or None
    first = (first_name or "").strip() or None
    last = (last_name or "").strip() or None
    staff_id = None
    if change_type == "add":
        if not (first and last):
            raise PayrollError("invalid", "A new hire needs a first and a last name.")
        if emp and db.query_one("SELECT 1 AS x FROM portal.staff_roster WHERE parish_id = %s AND employee_number = %s",
                                (parish["id"], emp)):
            raise PayrollError("refused", f"Employee number {emp} is already on this parish's roster.")
    else:
        if not emp:
            raise PayrollError("invalid", "An employee number is needed to change an existing employee.")
        staff_id = _staff(parish["id"], emp)["id"]
    if period_id is None:
        cur_p = db.query_one(
            "SELECT id FROM portal.payroll_periods WHERE org_id = %s AND status = 'open' "
            "ORDER BY (CURRENT_DATE BETWEEN period_start AND period_end) DESC, period_start DESC LIMIT 1", (org_id,))
        if not cur_p:
            raise PayrollError("refused", "There is no open pay period to attach this change to.")
        period_id = cur_p["id"]
    else:
        get_period(org_id, period_id)
    actor = actor_user_id if actor_user_id is not None else service_user_id()
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.staff_roster_changes (parish_id, payroll_period_id, staff_id, change_type, "
                "proposed_first_name, proposed_last_name, proposed_position, proposed_employee_number, "
                "proposed_captures_hours, as_of_date, created_by_user_id, submitted_by_user_id, submitted_at, "
                "source_ref, source_quote) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING id",
                (parish["id"], period_id, staff_id, change_type, first, last, (position or None), emp,
                 captures_hours, as_of, actor, actor, ref, q))
            r = cur.fetchone()
            if r:
                conn.commit()
                return {"action": "created", "change_id": r["id"]}
            cur.execute("SELECT id FROM portal.staff_roster_changes WHERE source_ref = %s AND change_type = %s "
                        "AND COALESCE(proposed_employee_number,'') = %s AND COALESCE(proposed_last_name,'') = %s",
                        (ref, change_type, emp or "", last or ""))
            ex = cur.fetchone()
            conn.commit()
            return {"action": "unchanged", "change_id": ex["id"] if ex else None}


_EMAIL_RX = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def add_learned_sender(org_id: int, *, parish_code: str, email: str, name: str | None = None,
                       source: str = "payroll inbox (learned)") -> dict:
    """Adds or refreshes one time submitter (role time_submitter) on a parish. Keyed on
    (parish, email address). Other contacts, including the payroll report recipients, are untouched."""
    em = (email or "").strip().lower()
    if not _EMAIL_RX.match(em):
        raise PayrollError("invalid", "That does not look like one email address.")
    if source not in ("payroll inbox (learned)", "manual"):
        raise PayrollError("invalid", "source must be 'payroll inbox (learned)' or 'manual'.")
    parish = _parish(org_id, parish_code)
    today = dt.date.today().isoformat()
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT contacts FROM portal.parishes WHERE id = %s FOR UPDATE", (parish["id"],))
            contacts, envelope = split_contacts(cur.fetchone()["contacts"])
            action = "created"
            for c in contacts:
                if isinstance(c, dict) and c.get("role") == "time_submitter" \
                        and (c.get("email") or "").strip().lower() == em:
                    c["last_seen"] = today
                    if name and not c.get("name"):
                        c["name"] = name
                    action = "refreshed"
                    break
            else:
                contacts.append({"role": "time_submitter", "name": (name or "").strip() or None, "email": em,
                                 "source": source, "first_seen": today, "last_seen": today})
            cur.execute("UPDATE portal.parishes SET contacts = %s::jsonb, updated_at = now() WHERE id = %s",
                        (json.dumps(join_contacts(contacts, envelope)), parish["id"]))
            conn.commit()
    return {"action": action}


def remove_sender(org_id: int, *, parish_code: str, email: str) -> dict:
    """Takes a time submitter off a parish (hr_admin screen only). Other contacts untouched."""
    em = (email or "").strip().lower()
    parish = _parish(org_id, parish_code)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT contacts FROM portal.parishes WHERE id = %s FOR UPDATE", (parish["id"],))
            contacts, envelope = split_contacts(cur.fetchone()["contacts"])
            keep = [c for c in contacts if not (isinstance(c, dict) and c.get("role") == "time_submitter"
                                                and (c.get("email") or "").strip().lower() == em)]
            removed = len(contacts) - len(keep)
            if removed:
                cur.execute("UPDATE portal.parishes SET contacts = %s::jsonb, updated_at = now() WHERE id = %s",
                            (json.dumps(join_contacts(keep, envelope)), parish["id"]))
            conn.commit()
    return {"removed": removed}


def set_profile(org_id: int, *, parish_id: int, pattern: str, notes: str | None, user_id: int) -> None:
    if pattern not in PATTERNS:
        raise PayrollError("invalid", "Unknown pattern.")
    p = db.query_one("SELECT id FROM portal.parishes WHERE id = %s AND org_id = %s", (parish_id, org_id))
    if not p:
        raise PayrollError("not_found", "That parish was not found for this diocese.")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.parish_payroll_profile (parish_id, pattern, notes, updated_by_user_id) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (parish_id) DO UPDATE SET pattern = EXCLUDED.pattern, "
                "notes = EXCLUDED.notes, updated_by_user_id = EXCLUDED.updated_by_user_id, updated_at = now()",
                (parish_id, pattern, (notes or "").strip()[:500] or None, user_id))
            conn.commit()


# -------------------------------------------- roster-change hooks (called by the review queue)

def on_roster_add_approved(change_id: int, staff_id: int) -> int:
    """A pending new hire became a real roster row: move the hours recorded for them onto it."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT t.id, t.period_id FROM portal.time_period_totals t WHERE t.pending_change_id = %s",
                        (change_id,))
            rows = cur.fetchall()
            for r in rows:
                cur.execute(
                    "UPDATE portal.time_period_totals SET staff_id = %s, pending_change_id = NULL, "
                    "flags = array_remove(flags, 'pending_hire'), updated_at = now() WHERE id = %s",
                    (staff_id, r["id"]))
                _log_edit(cur, r["id"], None, None, None, None, None, "diocese",
                          "Moved onto the roster row when the new hire was approved")
            conn.commit()
            return len(rows)


def on_roster_add_rejected(change_id: int, reviewer_user_id: int | None) -> int:
    """A pending new hire was rejected: the hours recorded for them are rejected too (rows are kept)."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, hours, review_status FROM portal.time_period_totals WHERE pending_change_id = %s "
                        "AND review_status <> 'rejected'", (change_id,))
            rows = cur.fetchall()
            for r in rows:
                cur.execute("UPDATE portal.time_period_totals SET review_status = 'rejected', "
                            "reviewed_by_user_id = %s, reviewed_at = now(), updated_at = now() WHERE id = %s",
                            (reviewer_user_id, r["id"]))
                _log_edit(cur, r["id"], r["hours"], r["hours"], r["review_status"], "rejected", reviewer_user_id,
                          "diocese", "The new hire was rejected")
            conn.commit()
            return len(rows)


# ------------------------------------------------------------------ review actions

def _line_for_review(cur, org_id: int, line_id: int) -> tuple[dict, dict]:
    cur.execute("SELECT t.*, pp.org_id, pp.status AS period_status, pp.period_start FROM portal.time_period_totals t "
                "JOIN portal.payroll_periods pp ON pp.id = t.period_id WHERE t.id = %s FOR UPDATE OF t", (line_id,))
    line = cur.fetchone()
    if not line or line["org_id"] != org_id:
        raise PayrollError("not_found", "That line was not found for this diocese.")
    if line["period_status"] not in WRITABLE_PERIOD_STATUSES:
        raise PayrollError("refused", f"This pay period is {line['period_status']}. Reopen it before changing its hours.")
    return line, line


def review_line(org_id: int, line_id: int, action: str, user_id: int, hours=None, note: str | None = None) -> dict:
    """An hr_admin's decision on one line. action: accept | reject | reopen | edit | accept_proposal |
    dismiss_proposal | use_total | use_grid. Every change writes a time_period_total_edits row."""
    note = (note or "").strip()[:300] or None
    with db.connect() as conn:
        with conn.cursor() as cur:
            line, _ = _line_for_review(cur, org_id, line_id)
            old_h, old_s = Decimal(line["hours"]), line["review_status"]
            flags = list(line["flags"] or [])
            sets: dict = {}
            new_h, new_s = old_h, old_s

            def need_staff():
                if line["staff_id"] is None:
                    raise PayrollError("refused", "This line belongs to a new hire who is not on the roster yet. "
                                                  "Approve the roster change first.")

            if action == "accept":
                need_staff()
                if line["proposed_hours"] is not None:
                    raise PayrollError("refused", "A different figure is waiting on this line: accept it or dismiss it.")
                new_s = "confirmed"
            elif action == "reject":
                new_s = "rejected"
            elif action == "reopen":
                if old_s != "rejected":
                    raise PayrollError("refused", "Only a rejected line can be reopened.")
                new_s = "needs_review"
            elif action == "edit":
                need_staff()
                new_h = parse_hours(hours)
                new_s = "confirmed"
                sets.update(source="diocese", proposed_hours=None, proposed_source_ref=None, proposed_quote=None)
                flags = [f for f in flags if f != "proposal"]
            elif action == "accept_proposal":
                need_staff()
                if line["proposed_hours"] is None:
                    raise PayrollError("refused", "There is no waiting figure on this line.")
                new_h = Decimal(line["proposed_hours"])
                new_s = "confirmed"
                sets.update(source_ref=line["proposed_source_ref"], source_quote=line["proposed_quote"],
                            proposed_hours=None, proposed_source_ref=None, proposed_quote=None)
                flags = [f for f in flags if f != "proposal"]
            elif action == "dismiss_proposal":
                if line["proposed_hours"] is None:
                    raise PayrollError("refused", "There is no waiting figure on this line.")
                new_s = "confirmed"
                sets.update(proposed_hours=None, proposed_source_ref=None, proposed_quote=None)
                flags = [f for f in flags if f != "proposal"]
            elif action == "use_total":
                need_staff()
                if line["proposed_hours"] is not None:
                    raise PayrollError("refused", "A different figure is waiting on this line: accept it or dismiss it.")
                new_s = "confirmed"
                flags = sorted(set(flags) | {"resolved_total"})
            elif action == "use_grid":
                new_s = "rejected"
                note = note or "Chose the parish's daily-grid hours instead"
            else:
                raise PayrollError("invalid", f"Unknown action {action!r}.")

            sets.update(hours=new_h, review_status=new_s, flags=sorted(set(flags)))
            if new_s == "confirmed":
                sets.update(reviewed_by_user_id=user_id, reviewed_at=dt.datetime.now(dt.timezone.utc))
            elif new_s in ("rejected",):
                sets.update(reviewed_by_user_id=user_id, reviewed_at=dt.datetime.now(dt.timezone.utc))
            else:
                sets.update(reviewed_by_user_id=None, reviewed_at=None)
            cols = ", ".join(f"{k} = %s" for k in sets)
            cur.execute(f"UPDATE portal.time_period_totals SET {cols}, updated_at = now() WHERE id = %s",
                        list(sets.values()) + [line_id])
            _log_edit(cur, line_id, old_h, new_h, old_s, new_s, user_id, "diocese", note or action.replace("_", " "))
            _unfinalize(cur, line["period_id"])
            conn.commit()
    return {"line_id": line_id, "review_status": new_s, "hours": float(new_h)}


def accept_all(org_id: int, period_id: int, parish_id: int, user_id: int) -> dict:
    """Confirms every plain needs_review line of one parish. Skips lines that need a person to look
    at them: a waiting proposal, a conflict with the daily grid, a pending new hire, or a flag
    (jump, not hourly, inactive)."""
    period = get_period(org_id, period_id)
    if period["status"] not in WRITABLE_PERIOD_STATUSES:
        raise PayrollError("refused", f"This pay period is {period['status']}. Reopen it before changing its hours.")
    rows = db.query(
        "SELECT t.id, t.flags, t.proposed_hours, t.staff_id, "
        "EXISTS (SELECT 1 FROM portal.time_entries te WHERE te.period_id = t.period_id AND te.staff_id = t.staff_id "
        "        AND te.category_id = t.category_id AND te.hours > 0) AS has_grid "
        "FROM portal.time_period_totals t JOIN portal.staff_roster sr ON sr.id = t.staff_id "
        "WHERE t.period_id = %s AND sr.parish_id = %s AND t.review_status = 'needs_review'", (period_id, parish_id))
    done = skipped = 0
    for r in rows:
        fl = set(r["flags"] or [])
        if r["proposed_hours"] is not None or r["staff_id"] is None or fl & set(ATTENTION_FLAGS) \
                or (r["has_grid"] and "resolved_total" not in fl):
            skipped += 1
            continue
        review_line(org_id, r["id"], "accept", user_id, note="Accept all")
        done += 1
    return {"accepted": done, "skipped": skipped}


def carry_forward_standing(org_id: int, period_id: int, user_id: int) -> dict:
    """For every active employee whose hours_basis is standing, records their standing_hours as a
    source=standing line in the Regular category. The line is confirmed on the spot when the
    employee's last three periods' Regular totals were confirmed at the same figure, otherwise it
    waits for review."""
    period = get_period(org_id, period_id)
    if period["status"] not in WRITABLE_PERIOD_STATUSES:
        raise PayrollError("refused", f"This pay period is {period['status']}.")
    svc = service_user_id()
    people = db.query(
        "SELECT s.id, s.employee_number, s.standing_hours, p.code FROM portal.staff_roster s "
        "JOIN portal.parishes p ON p.id = s.parish_id WHERE p.org_id = %s AND s.is_active "
        "AND s.hours_basis = 'standing' AND s.standing_hours IS NOT NULL AND s.employee_number IS NOT NULL", (org_id,))
    created = unchanged = confirmed = 0
    cat = _category(org_id, "regular")
    for s in people:
        prior = db.query(
            "SELECT t.hours FROM portal.time_period_totals t JOIN portal.payroll_periods pp ON pp.id = t.period_id "
            "WHERE t.staff_id = %s AND t.category_id = %s AND t.review_status = 'confirmed' AND pp.org_id = %s "
            "AND pp.period_start < %s ORDER BY pp.period_start DESC LIMIT 3",
            (s["id"], cat["id"], org_id, period["period_start"]))
        steady = len(prior) == 3 and all(Decimal(x["hours"]) == Decimal(s["standing_hours"]) for x in prior)
        r = record_period_hours(org_id, period_id=period_id, parish_code=s["code"], category_key="regular",
                                hours=s["standing_hours"], source="standing", source_ref=None,
                                quote="Standing hours carried forward", employee_number=s["employee_number"],
                                actor_user_id=svc if steady else user_id,
                                status="confirmed" if steady else None)
        if r["action"] == "unchanged":
            unchanged += 1
        else:
            created += 1
            if r["review_status"] == "confirmed":
                confirmed += 1
    return {"recorded": created, "unchanged": unchanged, "confirmed_automatically": confirmed}


# ------------------------------------------------------------- B3: the period picture

def _period_rows(org_id: int, period_id: int) -> list[dict]:
    """One row per employee (or pending hire) and category for the period, merging the parish's
    daily grid with the period-total lines. See the module docstring for the rules. Each row carries
    counted_hours (None when nothing is in force yet) and `state`: ok | needs_review | proposal |
    conflict | pending_hire | not_hourly."""
    grid = db.query(
        "SELECT te.staff_id, te.category_id, SUM(te.hours) AS h FROM portal.time_entries te "
        "JOIN portal.staff_roster sr ON sr.id = te.staff_id JOIN portal.parishes p ON p.id = sr.parish_id "
        "WHERE te.period_id = %s AND p.org_id = %s GROUP BY te.staff_id, te.category_id HAVING SUM(te.hours) > 0",
        (period_id, org_id))
    gmap = {(g["staff_id"], g["category_id"]): Decimal(g["h"]) for g in grid}
    lines = db.query(
        "SELECT t.*, c.key AS cat_key, c.label AS cat_label, c.sort_order AS cat_sort, "
        "sr.employee_number, sr.first_name, sr.last_name, sr.captures_hours, sr.parish_id AS staff_parish_id, "
        "rc.proposed_first_name AS pend_first, rc.proposed_last_name AS pend_last, "
        "rc.proposed_employee_number AS pend_emp, rc.parish_id AS pend_parish_id "
        "FROM portal.time_period_totals t JOIN portal.timekeeping_categories c ON c.id = t.category_id "
        "LEFT JOIN portal.staff_roster sr ON sr.id = t.staff_id "
        "LEFT JOIN portal.staff_roster_changes rc ON rc.id = t.pending_change_id "
        "WHERE t.period_id = %s", (period_id,))
    cats = {c["id"]: c for c in db.query(
        "SELECT id, key, label, sort_order FROM portal.timekeeping_categories WHERE org_id = %s", (org_id,))}
    parishes = {p["id"]: p for p in db.query(
        "SELECT id, code, name FROM portal.parishes WHERE org_id = %s", (org_id,))}
    people = {s["id"]: s for s in db.query(
        "SELECT s.id, s.parish_id, s.employee_number, s.first_name, s.last_name, s.captures_hours "
        "FROM portal.staff_roster s JOIN portal.parishes p ON p.id = s.parish_id WHERE p.org_id = %s", (org_id,))}
    out: list[dict] = []
    seen_grid: set = set()
    for t in lines:
        parish_id = t["staff_parish_id"] if t["staff_id"] else t["pend_parish_id"]
        if parish_id not in parishes:
            continue
        key = (t["staff_id"], t["category_id"])
        g = gmap.get(key) if t["staff_id"] else None
        flags = list(t["flags"] or [])
        counted, state = None, "needs_review"
        if g is not None:
            seen_grid.add(key)  # the grid hours are accounted for by this line, never listed twice
        if t["review_status"] == "rejected":
            if g is not None:
                counted, state = g, "ok"
            else:
                continue  # a rejected line with no grid hours counts for nothing and shows nowhere
        elif g is not None and "resolved_total" not in flags:
            counted, state = None, "conflict"
        elif t["staff_id"] is None:
            state = "pending_hire"
        elif t["proposed_hours"] is not None:
            counted, state = Decimal(t["hours"]), "proposal"
        elif t["review_status"] == "confirmed":
            counted, state = Decimal(t["hours"]), "ok"
        out.append({
            "line_id": t["id"], "staff_id": t["staff_id"], "pending_change_id": t["pending_change_id"],
            "parish_id": parish_id, "parish_code": parishes[parish_id]["code"], "parish_name": parishes[parish_id]["name"],
            "employee_number": t["employee_number"] if t["staff_id"] else t["pend_emp"],
            "last_name": t["last_name"] if t["staff_id"] else t["pend_last"],
            "first_name": t["first_name"] if t["staff_id"] else t["pend_first"],
            "category_id": t["category_id"], "category_key": t["cat_key"], "category_label": t["cat_label"],
            "category_sort": t["cat_sort"], "line_hours": Decimal(t["hours"]), "grid_hours": g,
            "counted_hours": counted, "source": t["source"], "source_ref": t["source_ref"],
            "quote": t["source_quote"], "review_status": t["review_status"], "flags": flags,
            "proposed_hours": None if t["proposed_hours"] is None else Decimal(t["proposed_hours"]),
            "proposed_ref": t["proposed_source_ref"], "proposed_quote": t["proposed_quote"],
            "state": state, "captures_hours": bool(t["captures_hours"]) if t["staff_id"] else True,
        })
    # daily-grid-only rows (no period-total line at all, or only a rejected one already added above)
    for (staff_id, cat_id), g in gmap.items():
        if (staff_id, cat_id) in seen_grid:
            continue
        s = people.get(staff_id)
        if not s or cat_id not in cats:
            continue
        par = parishes[s["parish_id"]]
        out.append({
            "line_id": None, "staff_id": staff_id, "pending_change_id": None, "parish_id": par["id"],
            "parish_code": par["code"], "parish_name": par["name"], "employee_number": s["employee_number"],
            "last_name": s["last_name"], "first_name": s["first_name"], "category_id": cat_id,
            "category_key": cats[cat_id]["key"], "category_label": cats[cat_id]["label"],
            "category_sort": cats[cat_id]["sort_order"], "line_hours": None, "grid_hours": g,
            "counted_hours": g, "source": "parish", "source_ref": None, "quote": None,
            "review_status": "entered", "flags": [], "proposed_hours": None, "proposed_ref": None,
            "proposed_quote": None, "state": "ok", "captures_hours": bool(s["captures_hours"]),
        })
    out.sort(key=lambda r: (r["parish_name"] or "", r["last_name"] or "", r["first_name"] or "",
                            r["category_sort"] or 0))
    return out


def period_rows(org_id: int, period_id: int) -> list[dict]:
    get_period(org_id, period_id)
    return _period_rows(org_id, period_id)


UNRESOLVED_STATES = ("needs_review", "proposal", "conflict", "pending_hire")


def unresolved(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["state"] in UNRESOLVED_STATES]


def finalize_period(org_id: int, period_id: int, user_id: int) -> dict:
    """Marks the hours Final for Checkwriters. Refused while any line still needs a decision."""
    get_period(org_id, period_id)
    left = unresolved(_period_rows(org_id, period_id))
    if left:
        raise PayrollError("refused", f"{len(left)} line(s) still need a decision, so the hours cannot be marked Final.")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE portal.payroll_periods SET hours_finalized_at = now(), hours_finalized_by_user_id = %s, "
                        "updated_at = now() WHERE id = %s", (user_id, period_id))
            conn.commit()
    return {"final": True}


def unfinalize_period(org_id: int, period_id: int) -> None:
    get_period(org_id, period_id)
    with db.connect() as conn:
        with conn.cursor() as cur:
            _unfinalize(cur, period_id)
            conn.commit()


def board_extras(org_id: int, period_id: int) -> dict[int, dict]:
    """Per parish id: pattern, channel, emails received (count and first time), lines needing
    review, conflicts and waiting proposals. For the Time Status board."""
    get_period(org_id, period_id)
    out: dict[int, dict] = {}

    def slot(pid):
        return out.setdefault(pid, {"pattern": None, "channel": None, "emails": 0, "first_received_at": None,
                                    "needs_review": 0, "conflicts": 0, "proposals": 0, "pending_hires": 0})

    for r in db.query("SELECT p.id, pr.pattern FROM portal.parishes p JOIN portal.parish_payroll_profile pr "
                      "ON pr.parish_id = p.id WHERE p.org_id = %s", (org_id,)):
        slot(r["id"])["pattern"] = r["pattern"]
    for r in db.query("SELECT s.parish_id, s.channel, s.first_received_at FROM portal.time_entry_submissions s "
                      "JOIN portal.parishes p ON p.id = s.parish_id WHERE s.period_id = %s AND p.org_id = %s",
                      (period_id, org_id)):
        d = slot(r["parish_id"])
        d["channel"], d["first_received_at"] = r["channel"], r["first_received_at"]
    for r in db.query("SELECT e.parish_id, count(*) AS n, min(e.received_at) AS first FROM portal.time_email_receipts e "
                      "JOIN portal.parishes p ON p.id = e.parish_id WHERE e.period_id = %s AND p.org_id = %s "
                      "GROUP BY e.parish_id", (period_id, org_id)):
        d = slot(r["parish_id"])
        d["emails"], d["first_received_at"] = r["n"], r["first"] or d["first_received_at"]
    for r in _period_rows(org_id, period_id):
        d = slot(r["parish_id"])
        if r["state"] == "needs_review":
            d["needs_review"] += 1
        elif r["state"] == "conflict":
            d["conflicts"] += 1
        elif r["state"] == "proposal":
            d["proposals"] += 1
        elif r["state"] == "pending_hire":
            d["pending_hires"] += 1
    return out


def message_link(source_ref: str | None) -> str | None:
    """Where to read the email: a Gmail search for the message id (the mailbox is signed in by staff)."""
    if not source_ref:
        return None
    from urllib.parse import quote
    ref = source_ref.strip().strip("<>")
    return "https://mail.google.com/mail/u/0/#search/rfc822msgid:" + quote(ref, safe="")


def parish_view(org_id: int, parish_id: int, period_id: int) -> list[dict]:
    """What the diocese recorded for one parish and period (the parish's own read-only view).
    Only confirmed or waiting figures and who they are: no quotes from other parishes, no pay."""
    get_period(org_id, period_id)
    return [r for r in _period_rows(org_id, period_id) if r["parish_id"] == parish_id and r["line_id"] is not None]


def variance(org_id: int, period_id: int) -> dict:
    """Beacon's counted hours against the paid register's hours, per employee and category, for
    employees who report hours. Hours only."""
    get_period(org_id, period_id)
    beacon: dict = {}
    for r in _period_rows(org_id, period_id):
        if r["counted_hours"] is not None and r["staff_id"] is not None and r["captures_hours"]:
            beacon[(r["staff_id"], r["category_id"])] = r
    reg = db.query(
        "SELECT rh.staff_id, rh.category_id, rh.hours, c.label, c.sort_order, sr.employee_number, sr.first_name, "
        "sr.last_name, sr.captures_hours, p.code AS parish_code, p.name AS parish_name "
        "FROM portal.time_register_hours rh JOIN portal.staff_roster sr ON sr.id = rh.staff_id "
        "JOIN portal.parishes p ON p.id = sr.parish_id JOIN portal.timekeeping_categories c ON c.id = rh.category_id "
        "WHERE rh.period_id = %s AND p.org_id = %s", (period_id, org_id))
    rmap = {(x["staff_id"], x["category_id"]): x for x in reg if x["captures_hours"]}
    rows = []
    for key in sorted(set(beacon) | set(rmap), key=lambda k: (
            (beacon.get(k) or {}).get("parish_name") or (rmap.get(k) or {}).get("parish_name") or "",
            (beacon.get(k) or {}).get("last_name") or (rmap.get(k) or {}).get("last_name") or "")):
        b, g = beacon.get(key), rmap.get(key)
        bh = None if b is None else b["counted_hours"]
        gh = None if g is None else Decimal(g["hours"])
        if b is not None and g is not None:
            kind = "match" if abs(bh - gh) < Decimal("0.005") else "differs"
        elif g is not None:
            kind = "paid_no_beacon_hours"
        else:
            kind = "beacon_hours_not_paid"
        src = b or g
        rows.append({"kind": kind, "parish_code": src["parish_code"], "parish_name": src["parish_name"],
                     "employee_number": src["employee_number"], "last_name": src["last_name"],
                     "first_name": src["first_name"],
                     "category_label": src["category_label"] if b else src["label"],
                     "beacon_hours": None if bh is None else float(bh),
                     "register_hours": None if gh is None else float(gh),
                     "difference": None if (bh is None or gh is None) else float(bh - gh)})
    counts = {k: sum(1 for r in rows if r["kind"] == k)
              for k in ("match", "differs", "paid_no_beacon_hours", "beacon_hours_not_paid")}
    return {"rows": rows, "counts": counts, "register_loaded": bool(reg)}
