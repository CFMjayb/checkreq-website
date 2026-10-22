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
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import category_order
import db
import payroll_decisions as pdec

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


AUTO_ACCEPT_SETTING = "payroll_auto_accept_confidence"   # checkreq.app_settings; "0", "off" or blank turns it off
AUTO_ACCEPT_DEFAULT = Decimal("0.80")
AUTO_ACCEPT_TOLERANCE = Decimal("0.25")                  # within 25% of the person's recent confirmed average


def auto_accept_threshold() -> Decimal | None:
    """The confidence needed to confirm an emailed line on arrival, or None when the feature is off."""
    try:
        import app_settings
        raw = (app_settings.get_setting(AUTO_ACCEPT_SETTING, str(AUTO_ACCEPT_DEFAULT)) or "").strip().lower()
    except Exception:
        raw = str(AUTO_ACCEPT_DEFAULT)
    if raw in ("", "0", "off", "false", "no"):
        return None
    try:
        t = Decimal(raw)
    except (InvalidOperation, ValueError):
        return AUTO_ACCEPT_DEFAULT
    return t if Decimal(0) < t <= Decimal(1) else AUTO_ACCEPT_DEFAULT


def _auto_accept(cur, staff, category_id: int, hours: Decimal, period: dict, conf, flags: list[str]) -> str | None:
    """A short reason (for the edit log) when this NEW emailed line may be confirmed on arrival, else None.
    All of: confidence at the setting or above; a roster person; no attention flag; a recent confirmed average
    exists (so there is something to compare with); the hours are within 25% of it."""
    if staff is None or conf is None or set(flags) & set(ATTENTION_FLAGS) or "pending_hire" in flags:
        return None
    need = auto_accept_threshold()
    if need is None or Decimal(conf) < need:
        return None
    avg = _recent_average(cur, staff["id"], category_id, period["period_start"])
    if avg is None or avg <= 0 or abs(hours - avg) > avg * AUTO_ACCEPT_TOLERANCE:
        return None
    return f"Accepted automatically: confidence {Decimal(conf):.2f}, within 25% of the usual {avg:.2f}"


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
    # Hours carried into this period from the previous one ("paid next period", payroll_decisions.py) are part of this line:
    # an EMAILED figure is what the parish reported for THIS period, so it is stored on top of them, never over them. A figure a
    # person types on a screen (source diocese or parish) is the total they want and is taken as it is.
    if staff is not None and source == "email":
        carry = pdec.carried_in(period_id, staff["id"], cat["id"])
        if carry > 0:
            h = h + carry
            if h > MAX_HOURS:
                raise PayrollError("refused", f"{h} hours (with {carry} carried in from the previous pay period) is more than "
                                              "the 300 hours one line may hold.")
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
                auto_note = None
                if new_status == "needs_review" and status is None and source == "email":
                    auto_note = _auto_accept(cur, staff, cat["id"], h, period, conf, flags)
                    if auto_note:
                        new_status, flags = "confirmed", sorted(set(flags) | {"auto_accepted"})
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
                              "Recorded" + (f" ({ref})" if ref else "") + (f". {auto_note}" if auto_note else ""))
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
            auto_note = None
            if new_status == "needs_review" and status is None and source == "email":
                auto_note = _auto_accept(cur, staff, cat["id"], h, period, conf, flags)
                if auto_note:
                    new_status, flags = "confirmed", sorted(set(flags) | {"auto_accepted"})
            cur.execute(
                "UPDATE portal.time_period_totals SET hours = %s, source = %s, source_ref = %s, source_quote = %s, "
                "confidence = %s, review_status = %s, flags = %s, proposed_hours = NULL, proposed_source_ref = NULL, "
                "proposed_quote = NULL, reviewed_by_user_id = %s, reviewed_at = %s, updated_at = now() WHERE id = %s",
                (h, source, ref, q, conf, new_status, flags,
                 actor if new_status == "confirmed" else None,
                 dt.datetime.now(dt.timezone.utc) if new_status == "confirmed" else None, line["id"]))
            _log_edit(cur, line["id"], old, h, line["review_status"], new_status, actor, source,
                      "Updated by a later source" + (f" ({ref})" if ref else "") + (f". {auto_note}" if auto_note else ""))
            _unfinalize(cur, period_id)
            conn.commit()
            return {"action": "updated", "line_id": line["id"], "review_status": new_status, "flags": flags,
                    "hours": float(h), "proposed_hours": None}


def record_history_line(cur, *, period_id: int, staff_id: int, category_id: int, hours: Decimal,
                        source_ref: str | None, flags: list[str], actor_user_id: int | None) -> str:
    """The history load (26-158 step 5): one paid-register line, confirmed, into a period that is
    already closed. Works on the caller's cursor so the loader can run against dev or production with
    its own connection. Idempotent: the same hours and flags change nothing. It never overwrites a
    line that came from anywhere but the register (a live email line wins). Returns created |
    unchanged | updated | skipped_other_source."""
    cur.execute("SELECT * FROM portal.time_period_totals WHERE period_id = %s AND staff_id = %s AND category_id = %s "
                "FOR UPDATE", (period_id, staff_id, category_id))
    ex = cur.fetchone()
    flags = sorted(set(flags))
    now = dt.datetime.now(dt.timezone.utc)
    if ex is None:
        cur.execute(
            "INSERT INTO portal.time_period_totals (period_id, staff_id, category_id, hours, source, source_ref, "
            "review_status, flags, created_by_user_id, reviewed_by_user_id, reviewed_at) "
            "VALUES (%s,%s,%s,%s,'register',%s,'confirmed',%s,%s,%s,%s) RETURNING id",
            (period_id, staff_id, category_id, hours, source_ref, flags, actor_user_id, actor_user_id, now))
        _log_edit(cur, cur.fetchone()["id"], None, hours, None, "confirmed", actor_user_id, "register",
                  "History load from the labor distribution")
        return "created"
    if ex["source"] != "register":
        return "skipped_other_source"
    if Decimal(ex["hours"]) == hours and sorted(ex["flags"] or []) == flags and ex["review_status"] == "confirmed":
        return "unchanged"
    cur.execute("UPDATE portal.time_period_totals SET hours = %s, source_ref = %s, review_status = 'confirmed', "
                "flags = %s, updated_at = now() WHERE id = %s", (hours, source_ref, flags, ex["id"]))
    _log_edit(cur, ex["id"], Decimal(ex["hours"]), hours, ex["review_status"], "confirmed", actor_user_id, "register",
              "History load re-run")
    return "updated"


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


# ------------------------------------------------- people an email named that were not entered

UNENTERED_REASONS = ("no_figure", "no_match", "ambiguous", "low_confidence", "refused", "other")
UNENTERED_REASON_LABELS = {
    "no_figure": "No exact hours in the email (only a limit or a rule)",
    "no_match": "No one on the roster matches this name",
    "ambiguous": "More than one employee fits this name",
    "low_confidence": "The reader was not sure of this line",
    "refused": "Beacon refused this line",
    "other": "Not entered",
}


def _clean_item_hours(raw) -> dict | None:
    """{category_key: hours} with each hours a number from 0 up to 300, or None when there are none."""
    if not isinstance(raw, dict):
        return None
    out = {}
    for k, v in raw.items():
        if v in (None, "", 0, 0.0):
            continue
        out[str(k).strip().lower()[:30]] = float(parse_hours(v))
    return out or None


def _hours_label(hours: dict | None, cats: dict[str, str]) -> str:
    if not hours:
        return ""
    return ", ".join(f"{h:g} {cats.get(k, k).lower()}" for k, h in hours.items())


def replace_unentered(org_id: int, *, period_id: int, source_ref: str, items: list[dict]) -> dict:
    """The CURRENT list of people one email named that the reader could not enter. Idempotent: the same
    list again changes nothing. Each item: parish_code, employee (the name as written), reason (one of
    UNENTERED_REASONS), hours ({category: hours} as read, optional), employee_number (the reader's best
    guess, optional), quote (the sentence, optional). An open item not in the list any more (the reader
    entered the person on a later read) is marked cleared. An item a person dismissed stays dismissed. A
    new or re-opened item takes the period off Final."""
    ref = (source_ref or "").strip()
    if not ref:
        raise PayrollError("invalid", "source_ref is required.")
    get_period(org_id, period_id)
    clean: dict[tuple[int, str], dict] = {}
    for it in items or []:
        parish = _parish(org_id, str(it.get("parish_code") or ""))
        name = " ".join(str(it.get("employee") or "").split())[:80]
        if not name:
            raise PayrollError("invalid", "Every item needs the employee's name as written.")
        reason = it.get("reason") if it.get("reason") in UNENTERED_REASONS else "other"
        staff_id = None
        if it.get("employee_number") not in (None, ""):
            s = db.query_one("SELECT id FROM portal.staff_roster WHERE parish_id = %s AND employee_number = %s "
                             "ORDER BY is_active DESC, id LIMIT 1", (parish["id"], str(it["employee_number"]).strip()))
            staff_id = s["id"] if s else None
        # A name that matches ONE INACTIVE roster person at this parish (a rehire, or someone wrongly inactive): suggest her
        # and queue a Reactivate request in Roster Review (the reader only knows the active roster).
        inactive = inactive_roster_match(parish["id"], name) if staff_id is None else None
        if inactive:
            staff_id = inactive["id"]
        clean[(parish["id"], name)] = {"reason": reason, "hours": _clean_item_hours(it.get("hours")),
                                       "staff_id": staff_id, "quote": clean_quote(it.get("quote")),
                                       "inactive": inactive, "parish_code": str(it.get("parish_code") or "")}
    opened = cleared = kept = 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT u.id, u.parish_id, u.employee_text, u.status FROM portal.time_email_unentered u "
                        "JOIN portal.parishes p ON p.id = u.parish_id "
                        "WHERE u.period_id = %s AND u.source_ref = %s AND p.org_id = %s FOR UPDATE",
                        (period_id, ref, org_id))
            have = {(r["parish_id"], r["employee_text"]): r for r in cur.fetchall()}
            for key, v in clean.items():
                row = have.get(key)
                hj = None if v["hours"] is None else json.dumps(v["hours"])
                if row is None:
                    cur.execute("INSERT INTO portal.time_email_unentered (period_id, parish_id, source_ref, employee_text, "
                                "reason, hours, suggested_staff_id, quote) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s)",
                                (period_id, key[0], ref, key[1], v["reason"], hj, v["staff_id"], v["quote"]))
                    opened += 1
                elif row["status"] == "dismissed":
                    kept += 1
                elif row["status"] == "cleared":
                    # a person already entered it from the list, or the reader did: leave it settled
                    kept += 1
                else:
                    cur.execute("UPDATE portal.time_email_unentered SET reason = %s, hours = %s::jsonb, "
                                "suggested_staff_id = %s, quote = %s, updated_at = now() WHERE id = %s",
                                (v["reason"], hj, v["staff_id"], v["quote"], row["id"]))
            for key, row in have.items():
                if key not in clean and row["status"] == "open":
                    cur.execute("UPDATE portal.time_email_unentered SET status = 'cleared', handled_at = now(), "
                                "handled_note = %s, updated_at = now() WHERE id = %s",
                                ("The reader entered this person or no longer lists them.", row["id"]))
                    cleared += 1
            if opened:
                _unfinalize(cur, period_id)
            conn.commit()
    queued = 0
    for v in clean.values():
        m = v.get("inactive")
        if not m or not m.get("employee_number"):
            continue
        if db.query_one("SELECT 1 AS x FROM portal.staff_roster_changes WHERE staff_id = %s AND change_type = 'reactivate' "
                        "AND status = 'pending'", (m["id"],)):
            continue  # already waiting in Roster Review
        try:
            r = propose_roster_change(org_id, parish_code=v["parish_code"], change_type="reactivate", source_ref=ref,
                                      quote=v["quote"], employee_number=m["employee_number"], first_name=m["first_name"],
                                      last_name=m["last_name"], period_id=period_id)
            if r.get("action") == "created":
                queued += 1
        except PayrollError:
            pass
    return {"listed": len(clean), "opened": opened, "cleared": cleared, "settled_kept": kept, "reactivations_queued": queued}


def list_unentered(org_id: int, period_id: int, parish_id: int | None = None, status: str = "open") -> list[dict]:
    """The people the emails named that nobody has entered yet (or, with status, the dismissed or cleared)."""
    get_period(org_id, period_id)
    args: list = [period_id, org_id, status]
    extra = ""
    if parish_id is not None:
        extra = " AND u.parish_id = %s"
        args.append(parish_id)
    rows = db.query(
        "SELECT u.id, u.period_id, u.parish_id, p.code AS parish_code, p.name AS parish_name, "
        "COALESCE(p.short_name, p.name) AS dept_name, u.source_ref, u.employee_text, u.reason, u.hours, "
        "u.suggested_staff_id, u.quote, u.status, u.created_at, sr.first_name AS sug_first, sr.last_name AS sug_last, "
        "sr.is_active AS sug_active, sr.employee_number AS sug_emp "
        "FROM portal.time_email_unentered u JOIN portal.parishes p ON p.id = u.parish_id "
        "LEFT JOIN portal.staff_roster sr ON sr.id = u.suggested_staff_id "
        "WHERE u.period_id = %s AND p.org_id = %s AND u.status = %s" + extra +
        " ORDER BY p.code, u.employee_text, u.id", args)
    cats = {c["key"]: c["label"] for c in db.query(
        "SELECT key, label FROM portal.timekeeping_categories WHERE org_id = %s", (org_id,))}
    for r in rows:
        r["reason_label"] = UNENTERED_REASON_LABELS.get(r["reason"], r["reason"])
        r["hours_label"] = _hours_label(r["hours"], cats)
        r["hours_single"] = next(iter(r["hours"].values())) if r["hours"] and len(r["hours"]) == 1 else None
        # the suggested person is on the roster but inactive: say so, and the page offers Reactivate
        r["inactive_match"] = bool(r["suggested_staff_id"]) and r.get("sug_active") is False
        if r["inactive_match"]:
            r["reason_label"] = "On the roster but inactive. Reactivate her and these hours are entered."
    return rows


def count_unentered(org_id: int, period_id: int) -> dict[int, int]:
    """Open items per parish id for one pay period."""
    return {r["parish_id"]: r["n"] for r in db.query(
        "SELECT u.parish_id, count(*) AS n FROM portal.time_email_unentered u JOIN portal.parishes p ON p.id = u.parish_id "
        "WHERE u.period_id = %s AND p.org_id = %s AND u.status = 'open' GROUP BY u.parish_id", (period_id, org_id))}


def _settle_unentered(item_id: int, org_id: int, user_id: int, status: str, note: str | None) -> dict:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT u.* FROM portal.time_email_unentered u JOIN portal.parishes p ON p.id = u.parish_id "
                        "WHERE u.id = %s AND p.org_id = %s FOR UPDATE", (item_id, org_id))
            row = cur.fetchone()
            if not row:
                raise PayrollError("not_found", "That item was not found.")
            if row["status"] != "open":
                raise PayrollError("refused", "That item is not waiting any more.")
            cur.execute("UPDATE portal.time_email_unentered SET status = %s, handled_by_user_id = %s, "
                        "handled_at = now(), handled_note = %s, updated_at = now() WHERE id = %s",
                        (status, user_id, (note or "").strip()[:300] or None, item_id))
            conn.commit()
    return row


def dismiss_unentered(org_id: int, item_id: int, user_id: int, note: str | None = None) -> dict:
    """A reviewer says this person needs nothing (for example the hours were entered by hand on the row,
    or the person is not paid hourly). Who and when are kept."""
    _settle_unentered(item_id, org_id, user_id, "dismissed", note)
    return {"dismissed": True}


def assign_unentered(org_id: int, item_id: int, staff_id: int, user_id: int, hours=None, confirm: bool = True) -> dict:
    """A reviewer picks the right employee for a name the reader could not place and the hours are entered for
    that person, as email lines tied to the same email (so the PDF links to them), confirmed by the reviewer.
    The hours are the ones the reader read; when it read none, `hours` (regular) must be given."""
    row = db.query_one("SELECT u.* FROM portal.time_email_unentered u JOIN portal.parishes p ON p.id = u.parish_id "
                       "WHERE u.id = %s AND p.org_id = %s", (item_id, org_id))
    if not row:
        raise PayrollError("not_found", "That item was not found.")
    if row["status"] != "open":
        raise PayrollError("refused", "That item is not waiting any more.")
    staff = db.query_one("SELECT s.*, p.code AS parish_code FROM portal.staff_roster s JOIN portal.parishes p ON p.id = s.parish_id "
                         "WHERE s.id = %s AND p.org_id = %s", (staff_id, org_id))
    if not staff or not staff.get("employee_number"):
        raise PayrollError("not_found", "Choose an employee from the list.")
    if staff["parish_id"] != row["parish_id"]:
        raise PayrollError("refused", "That employee is not on this parish's roster.")
    figures = _clean_item_hours(row["hours"]) or {}
    if hours not in (None, "") and len(figures) <= 1:
        figures = {(next(iter(figures)) if figures else "regular"): float(parse_hours(hours))}
    if not figures:
        raise PayrollError("invalid", "Type the hours for this person first: the email gave no exact figure.")
    period = get_period(org_id, row["period_id"])
    who = db.query_one("SELECT COALESCE(display_name, email) AS n FROM checkreq.app_users WHERE id = %s", (user_id,))
    note = (f"Name chosen by {(who or {}).get('n') or 'a reviewer'} from the email" if confirm else
            f"Entered when {(who or {}).get('n') or 'a reviewer'} reactivated her. The hours wait for review")
    placed = []
    for cat, h in figures.items():
        res = record_period_hours(org_id, period_id=period["id"], parish_code=staff["parish_code"], category_key=cat, hours=h,
                                  source="email", source_ref=row["source_ref"],
                                  quote=(f"(written as {row['employee_text']}) " + (row["quote"] or ""))[:300],
                                  confidence=1 if confirm else 0.5, employee_number=staff["employee_number"],
                                  actor_user_id=user_id, status="confirmed" if confirm else None)
        placed.append({"category": cat, "hours": h, "action": res.get("action")})
    _settle_unentered(item_id, org_id, user_id, "cleared", note)
    return {"entered": placed, "staff_id": staff["id"]}


# Everyday nicknames (Tony = Anthony): each set is ONE first name. Same list as the payroll email reader's match.py
# (26-158 payroll_inbox/logic/match.py) -- keep the two in step.
_NICK_SETS = [
    {"anthony", "tony", "antony"}, {"timothy", "tim", "timmy"}, {"william", "will", "bill", "billy", "willy", "liam"},
    {"robert", "rob", "bob", "bobby", "robbie", "bert"}, {"richard", "rick", "ricky", "dick", "rich", "richie"},
    {"james", "jim", "jimmy", "jamie"}, {"john", "jack", "johnny", "jon"}, {"michael", "mike", "mick", "mickey"},
    {"thomas", "tom", "tommy"}, {"charles", "charlie", "chuck", "chas"}, {"edward", "ed", "eddie", "ted", "teddy", "ned"},
    {"joseph", "joe", "joey"}, {"daniel", "dan", "danny"}, {"david", "dave", "davy"}, {"donald", "don", "donnie"},
    {"stephen", "steven", "steve", "stevie"}, {"matthew", "matt"}, {"nicholas", "nick", "nicky"}, {"patrick", "pat", "paddy"},
    {"peter", "pete"}, {"samuel", "sam", "sammy"}, {"benjamin", "ben", "benny"}, {"alexander", "alex", "sandy"},
    {"andrew", "andy", "drew"}, {"christopher", "chris", "kit"}, {"jonathan", "jon", "jonny"}, {"gregory", "greg"},
    {"kenneth", "ken", "kenny"}, {"lawrence", "larry", "laurence"}, {"ronald", "ron", "ronnie"}, {"walter", "walt", "wally"},
    {"raymond", "ray"}, {"frederick", "fred", "freddy", "freddie"}, {"gerald", "gerry", "jerry"}, {"harold", "harry", "hal"},
    {"henry", "hank", "harry"}, {"albert", "al", "bert"}, {"arthur", "art", "artie"}, {"leonard", "leo", "len", "lenny"},
    {"margaret", "peg", "peggy", "maggie", "meg", "marge", "margie", "greta"}, {"elizabeth", "liz", "beth", "betty", "lizzie", "libby", "eliza", "betsy"},
    {"katherine", "catherine", "kathryn", "kate", "katie", "kathy", "cathy", "kitty", "kay"}, {"jennifer", "jen", "jenny", "jenn"},
    {"patricia", "pat", "patty", "trish", "tricia"}, {"barbara", "barb", "barbie", "babs"}, {"susan", "sue", "susie", "suzy", "suzanne"},
    {"deborah", "debra", "deb", "debbie"}, {"rebecca", "becky", "becca"}, {"jessica", "jess", "jessie"}, {"victoria", "vicky", "vicki", "tori"},
    {"christine", "christina", "chris", "chrissy", "tina"}, {"dorothy", "dot", "dottie", "dolly"}, {"edith", "edie", "edy"},
    {"abraham", "abe", "bram"}, {"cynthia", "cindy"}, {"judith", "judy", "jude"}, {"pamela", "pam"}, {"theresa", "teresa", "terry", "tess"},
    {"virginia", "ginny", "ginger"}, {"eleanor", "ellie", "nell", "nellie"}, {"mary", "molly", "polly"}, {"nancy", "nan"},
    {"frances", "fran", "frankie"}, {"franklin", "frank", "frankie"}, {"phillip", "philip", "phil"}, {"vincent", "vince", "vinny"},
    {"timothea", "tim"}, {"alan", "allen", "allan", "al"}, {"douglas", "doug"}, {"russell", "russ"}, {"theodore", "ted", "teddy", "theo"},
]
_NICK = {}
for _i, _set in enumerate(_NICK_SETS):
    for _n in _set:
        _NICK.setdefault(_n, set()).add(_i)


def _first_names_fit(a, b) -> bool:
    """Two first names that are the same person's: the first word equal (Mary / Mary Frances, Abraham / Abraham (Bram)),
    one a prefix of the other from three letters up (Tim / Timothy), or two forms of one name (Tony / Anthony)."""
    ta = re.findall(r"[a-z0-9']+", (a or "").lower().replace("'", ""))
    tb = re.findall(r"[a-z0-9']+", (b or "").lower().replace("'", ""))
    if not ta or not tb:
        return False
    x, y = ta[0], tb[0]
    return (x == y or (len(x) >= 3 and len(y) >= 3 and (x.startswith(y) or y.startswith(x)))
            or bool(_NICK.get(x, set()) & _NICK.get(y, set())))


def _name_parts(text) -> tuple[str, str] | None:
    """(first, last) of a name as written: 'Elizabeth Boucher' or 'Boucher, Elizabeth'. None when it is one word."""
    t = " ".join(str(text or "").split())
    if not t:
        return None
    if "," in t:
        last, first = [x.strip() for x in t.split(",", 1)]
    else:
        w = t.split()
        if len(w) < 2:
            return None
        first, last = w[0], w[-1]
    return (first, last) if first and last else None


def inactive_roster_match(parish_id: int, name) -> dict | None:
    """The ONE inactive roster person at this parish a written name is about (same surname, a first name that fits), or None
    when there is none, more than one, or an ACTIVE person fits too (the reader's own match already covers active people)."""
    parts = _name_parts(name)
    if not parts:
        return None
    first, last = parts
    rows = db.query("SELECT id, first_name, last_name, employee_number, is_active FROM portal.staff_roster "
                    "WHERE parish_id = %s AND lower(last_name) = lower(%s)", (parish_id, last))
    fits = [r for r in rows if _first_names_fit(first, r["first_name"])]
    if any(r["is_active"] for r in fits):
        return None
    inactive = [r for r in fits if not r["is_active"]]
    return inactive[0] if len(inactive) == 1 else None


def on_staff_reactivated(staff_id: int, user_id: int | None) -> dict:
    """A roster person was reactivated (Roster Review, the Employees screen, a rehire): answer her waiting Reactivate request
    and enter the hours of every open 'named, not entered' item that is about her, as email lines tied to the same email.
    Those lines WAIT FOR REVIEW (a reviewer still looks at the number). An item with no exact figure stays listed."""
    s = db.query_one("SELECT s.id, s.parish_id, s.first_name, s.last_name, s.is_active, p.org_id FROM portal.staff_roster s "
                     "JOIN portal.parishes p ON p.id = s.parish_id WHERE s.id = %s", (staff_id,))
    if not s or not s["is_active"]:
        return {"entered": 0, "items": 0}
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE portal.staff_roster_changes SET status = 'approved', reviewed_by_user_id = %s, reviewed_at = now(), "
                        "review_note = 'She was reactivated.' WHERE staff_id = %s AND change_type = 'reactivate' "
                        "AND status = 'pending'", (user_id, staff_id))
            conn.commit()
    entered = items = 0
    for it in db.query("SELECT u.id, u.employee_text, u.suggested_staff_id FROM portal.time_email_unentered u "
                       "JOIN portal.payroll_periods pp ON pp.id = u.period_id "
                       "WHERE u.parish_id = %s AND u.status = 'open' AND pp.status IN ('open', 'future') AND pp.org_id = %s",
                       (s["parish_id"], s["org_id"])):
        mine = it["suggested_staff_id"] == staff_id
        if not mine and it["suggested_staff_id"] is None:
            parts = _name_parts(it["employee_text"])
            mine = bool(parts) and parts[1].lower() == (s["last_name"] or "").strip().lower() and _first_names_fit(parts[0], s["first_name"])
        if not mine:
            continue
        try:
            res = assign_unentered(s["org_id"], it["id"], staff_id, user_id, confirm=False)
        except PayrollError:
            continue  # no exact figure in the email: it stays listed for a reviewer to finish
        entered += len(res["entered"])
        items += 1
    return {"entered": entered, "items": items}


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
        # Never ask for someone who is already on the roster (active or not) or already asked for: an email about a
        # person's paperwork is not a new hire when she is already in the system, and a second email about the same
        # hire must not queue a second request.
        for r in db.query("SELECT id, first_name, employee_number, is_active FROM portal.staff_roster "
                          "WHERE parish_id = %s AND lower(last_name) = lower(%s) ORDER BY is_active DESC, id",
                          (parish["id"], last)):
            if _first_names_fit(first, r["first_name"]):
                return {"action": "already_on_roster", "change_id": None, "staff_id": r["id"],
                        "employee_number": r["employee_number"], "is_active": bool(r["is_active"])}
        for r in db.query("SELECT id, proposed_first_name FROM portal.staff_roster_changes WHERE parish_id = %s "
                          "AND change_type = 'add' AND status = 'pending' AND lower(proposed_last_name) = lower(%s) "
                          "ORDER BY id", (parish["id"], last)):
            if _first_names_fit(first, r["proposed_first_name"]):
                return {"action": "unchanged", "change_id": r["id"], "duplicate_of_pending": True}
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


def retract_line(org_id: int, line_id: int, user_id: int, source: str, note: str | None = None) -> dict:
    """A parish takes back a period total IT typed itself, before the diocese has looked at it (the entry
    screen uses this when a typed total is replaced by daily hours). Only a line from the same source that is
    still waiting for review; anything else is refused. The line becomes rejected, never deleted."""
    if source not in SOURCES:
        raise PayrollError("invalid", f"Unknown source {source!r}.")
    with db.connect() as conn:
        with conn.cursor() as cur:
            line, _ = _line_for_review(cur, org_id, line_id)
            if line["source"] != source or line["review_status"] != "needs_review":
                raise PayrollError("refused", "Only a total you entered yourself, before the diocese reviews it, "
                                              "can be taken back.")
            h = Decimal(line["hours"])
            cur.execute("UPDATE portal.time_period_totals SET review_status = 'rejected', reviewed_by_user_id = %s, "
                        "reviewed_at = now(), updated_at = now() WHERE id = %s", (user_id, line_id))
            _log_edit(cur, line_id, h, h, "needs_review", "rejected", user_id, source, (note or "Taken back")[:300])
            _unfinalize(cur, line["period_id"])
            conn.commit()
    return {"line_id": line_id, "review_status": "rejected"}


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


# --------------------------------------------- the comments, notes and requests in each email (Payroll notes page)

NOTE_KIND_LABELS = {
    "note": "Note", "new_hire": "New hire", "termination": "Termination", "pay_change": "Pay change", "address_change": "Change of details",
    "other": "To key by hand", "question": "Question", "admin": "Administrative",
}
NOTES_PER_EMAIL = 40


def note_kind_label(kind: str) -> str:
    return NOTE_KIND_LABELS.get(kind) or (kind or "other").replace("_", " ").capitalize()


def replace_email_notes(org_id: int, *, period_id: int, source_ref: str, subject: str | None = None, sender: str | None = None,
                        received_at=None, items: list | None = None) -> dict:
    """Stores ONE email's CURRENT comments, notes and requests (the rows for that email are replaced as a set, so reading the
    email again never doubles them and an empty list clears them). Each item: {parish_code, kind, employee, text, result}.
    Personal data is never stored: such a text becomes a plain notice that points at the email."""
    get_period(org_id, period_id)
    ref = (source_ref or "").strip()
    if not ref:
        raise PayrollError("invalid", "source_ref (the message id) is required.")
    items = items or []
    if not isinstance(items, list) or len(items) > NOTES_PER_EMAIL:
        raise PayrollError("invalid", f"items must be a list of up to {NOTES_PER_EMAIL} entries.")
    when = None
    if received_at:
        try:
            when = dt.datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
        except ValueError:
            when = None

    def clip(v, n):
        t = " ".join(str(v or "").split())
        return t[:n] or None

    rows = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise PayrollError("invalid", "Every item must be an object.")
        text = clip(it.get("text"), 600)
        if not text:
            continue
        if pii_problem(text):
            text = "[Not stored: it looks like personal data. Read it in the email.]"
        kind = re.sub(r"[^a-z_]", "", str(it.get("kind") or "other").lower())[:30] or "other"
        parish_id = None
        code = (it.get("parish_code") or "").strip()
        if code:
            pr = db.query_one("SELECT id FROM portal.parishes WHERE org_id = %s AND code = %s", (org_id, code))
            parish_id = pr["id"] if pr else None
        res = clip(it.get("result"), 200)
        if res and pii_problem(res):
            res = None
        emp = clip(it.get("employee"), 80)
        if emp and pii_problem(emp):
            emp = None
        rows.append((period_id, parish_id, ref, i, kind, emp, text, res, clip(subject, 200), clip(sender, 120), when))
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM portal.time_email_notes WHERE period_id = %s AND source_ref = %s", (period_id, ref))
            for r in rows:
                cur.execute("INSERT INTO portal.time_email_notes (period_id, parish_id, source_ref, seq, kind, employee_text, note_text, "
                            "result_text, email_subject, email_sender, received_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", r)
            conn.commit()
    return {"stored": len(rows)}


def list_email_notes(org_id: int, period_id: int) -> list[dict]:
    """Every stored note of the period for this diocese, by department then email then order in the email."""
    get_period(org_id, period_id)
    rows = db.query(
        "SELECT n.*, p.code AS parish_code, COALESCE(p.short_name, p.name) AS parish_name, p.name AS parish_full_name "
        "FROM portal.time_email_notes n LEFT JOIN portal.parishes p ON p.id = n.parish_id "
        "WHERE n.period_id = %s ORDER BY p.code NULLS LAST, n.received_at NULLS LAST, n.source_ref, n.seq", (period_id,))
    for r in rows:
        r["kind_label"] = note_kind_label(r["kind"])
    return rows


def _person_for_review(org_id: int, parish_id: int, staff_id: int) -> dict:
    st = db.query_one(
        "SELECT s.*, p.code AS parish_code FROM portal.staff_roster s JOIN portal.parishes p ON p.id = s.parish_id "
        "WHERE s.id = %s AND s.parish_id = %s AND p.org_id = %s", (staff_id, parish_id, org_id))
    if not st:
        raise PayrollError("not_found", "That employee was not found for this parish.")
    return st


def review_person(org_id: int, period_id: int, parish_id: int, staff_id: int, action: str, user_id: int,
                  figures: dict | None = None, new_figures: dict | None = None) -> dict:
    """ONE decision for an employee's whole row on the Period totals (Jay, 2026-10-11: one Accept and one Reject per
    employee; every kind of hours listed on the row, editable before you accept).
      action 'accept': figures = {line_id: typed hours} -- a figure that differs from the line is saved as the diocese's
        own (confirmed); new_figures = {category_key: typed hours} -- a figure typed into a blank cell is a NEW line,
        added by hand and confirmed; every other plain line still waiting for review is accepted. A line with a different
        figure waiting, or in conflict with the daily grid, is left for its own buttons.
      action 'reject': every line of the employee that is not already rejected is rejected (kept, never deleted).
    Returns counts."""
    period = get_period(org_id, period_id)
    if period["status"] not in WRITABLE_PERIOD_STATUSES:
        raise PayrollError("refused", f"This pay period is {period['status']}. Reopen it before changing its hours.")
    st = _person_for_review(org_id, parish_id, staff_id)
    rows = [r for r in _period_rows(org_id, period_id) if r["staff_id"] == staff_id and r["line_id"] is not None]
    by_id = {r["line_id"]: r for r in rows}
    if action == "reject":
        n = 0
        for r in rows:
            if r["review_status"] != "rejected":
                review_line(org_id, r["line_id"], "reject", user_id, note="Rejected with the employee's other lines")
                n += 1
        return {"rejected": n}
    if action != "accept":
        raise PayrollError("invalid", f"Unknown action {action!r}.")
    edited = created = accepted = skipped = 0
    handled: set = set()
    for lid, raw in (figures or {}).items():
        r = by_id.get(lid)
        raw = (raw or "").strip()
        if r is None or raw == "":
            continue
        h = parse_hours(raw)
        if h != Decimal(r["line_hours"]) or r["review_status"] == "rejected":
            review_line(org_id, lid, "edit", user_id, hours=h, note="Edited on the review screen")
            edited += 1
            handled.add(lid)
    for key, raw in (new_figures or {}).items():
        raw = (raw or "").strip()
        if raw == "":
            continue
        h = parse_hours(raw)
        if h == 0:
            continue
        if not st["employee_number"]:
            raise PayrollError("refused", "This employee has no employee number yet, so hours cannot be added.")
        record_period_hours(org_id, period_id=period_id, parish_code=st["parish_code"], category_key=key, hours=h,
                            source="diocese", quote="Added on the review screen", employee_number=st["employee_number"],
                            actor_user_id=user_id, status="confirmed")
        created += 1
    for r in rows:
        if r["line_id"] in handled or r["review_status"] != "needs_review":
            continue
        if r["state"] in ("proposal", "conflict", "pending_hire"):
            skipped += 1
            continue
        review_line(org_id, r["line_id"], "accept", user_id, note="Accepted with the employee's other lines")
        accepted += 1
    return {"accepted": accepted, "edited": edited, "created": created, "skipped": skipped}


def add_manual_line(org_id: int, period_id: int, parish_id: int, staff_id: int, category_key: str, hours,
                    user_id: int, source_ref: str | None = None) -> dict:
    """A line the reader missed, entered by hand (Jay, 2026-10-11). Confirmed on the spot. When an email of this parish
    is chosen the line is tied to it (its PDF shows on the row); otherwise it is the diocese's own entry."""
    period = get_period(org_id, period_id)
    if period["status"] not in WRITABLE_PERIOD_STATUSES:
        raise PayrollError("refused", f"This pay period is {period['status']}. Reopen it before changing its hours.")
    st = _person_for_review(org_id, parish_id, staff_id)
    if not st["employee_number"]:
        raise PayrollError("refused", "This employee has no employee number yet, so hours cannot be added.")
    if not st["is_active"]:
        raise PayrollError("refused", "This employee is inactive. Reactivate them in Roster Review or Employees first.")
    h = parse_hours(hours)
    ref = (source_ref or "").strip() or None
    if ref:
        ok = db.query_one("SELECT 1 AS x FROM portal.time_email_receipts WHERE period_id = %s AND parish_id = %s "
                          "AND source_ref = %s UNION SELECT 1 FROM portal.time_period_totals t "
                          "JOIN portal.staff_roster s ON s.id = t.staff_id WHERE t.period_id = %s AND s.parish_id = %s "
                          "AND t.source_ref = %s", (period_id, parish_id, ref, period_id, parish_id, ref))
        if not ok:
            raise PayrollError("refused", "That email was not found for this parish and pay period.")
    return record_period_hours(
        org_id, period_id=period_id, parish_code=st["parish_code"], category_key=category_key, hours=h,
        source="email" if ref else "diocese", source_ref=ref, quote="Added by hand from this email" if ref else "Added by hand",
        employee_number=st["employee_number"], actor_user_id=user_id, status="confirmed")


def parish_email_choices(org_id: int, period_id: int, parish_id: int) -> list[dict]:
    """The emails of this parish and period a hand-added line can be tied to: [{source_ref, received_at}]."""
    return db.query(
        "SELECT r.source_ref, r.received_at FROM portal.time_email_receipts r WHERE r.period_id = %s AND r.parish_id = %s "
        "UNION SELECT DISTINCT t.source_ref, NULL::timestamptz FROM portal.time_period_totals t JOIN portal.staff_roster s ON s.id = t.staff_id "
        "WHERE t.period_id = %s AND s.parish_id = %s AND t.source = 'email' AND t.source_ref IS NOT NULL "
        "AND t.source_ref NOT IN (SELECT source_ref FROM portal.time_email_receipts WHERE period_id = %s AND parish_id = %s) "
        "ORDER BY 2 NULLS LAST", (period_id, parish_id, period_id, parish_id, period_id, parish_id))


def carry_forward_standing(org_id: int, period_id: int, user_id: int, parish_id: int | None = None) -> dict:
    """For every active employee whose hours_basis is standing, records their standing_hours as a
    source=standing line in the Regular category. The line is confirmed on the spot when the
    employee's last three periods' Regular totals were confirmed at the same figure, otherwise it
    waits for review. With parish_id, only that parish's people."""
    period = get_period(org_id, period_id)
    if period["status"] not in WRITABLE_PERIOD_STATUSES:
        raise PayrollError("refused", f"This pay period is {period['status']}.")
    svc = service_user_id()
    people = db.query(
        "SELECT s.id, s.employee_number, s.standing_hours, p.code FROM portal.staff_roster s "
        "JOIN portal.parishes p ON p.id = s.parish_id WHERE p.org_id = %s AND s.is_active "
        "AND s.hours_basis = 'standing' AND s.standing_hours IS NOT NULL AND s.employee_number IS NOT NULL"
        + (" AND s.parish_id = %s" if parish_id is not None else ""),
        (org_id, parish_id) if parish_id is not None else (org_id,))
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
        "SELECT id, code, name, short_name FROM portal.parishes WHERE org_id = %s", (org_id,))}
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
            "dept_name": parishes[parish_id]["short_name"] or parishes[parish_id]["name"],
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
            "parish_code": par["code"], "parish_name": par["name"], "dept_name": par["short_name"] or par["name"],
            "employee_number": s["employee_number"],
            "last_name": s["last_name"], "first_name": s["first_name"], "category_id": cat_id,
            "category_key": cats[cat_id]["key"], "category_label": cats[cat_id]["label"],
            "category_sort": cats[cat_id]["sort_order"], "line_hours": None, "grid_hours": g,
            "counted_hours": g, "source": "parish", "source_ref": None, "quote": None,
            "review_status": "entered", "flags": [], "proposed_hours": None, "proposed_ref": None,
            "proposed_quote": None, "state": "ok", "captures_hours": bool(s["captures_hours"]),
        })
    out.sort(key=lambda r: (r["parish_code"] or "~", r["parish_name"] or "", r["last_name"] or "", r["first_name"] or "",
                            r["category_sort"] or 0))
    return out


def period_rows(org_id: int, period_id: int) -> list[dict]:
    get_period(org_id, period_id)
    return _period_rows(org_id, period_id)


UNRESOLVED_STATES = ("needs_review", "proposal", "conflict", "pending_hire")


def unresolved(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["state"] in UNRESOLVED_STATES]


def finalize_period(org_id: int, period_id: int, user_id: int, require_all_done: bool = True) -> dict:
    """Marks the hours Final for Checkwriters. Refused while any line still needs a decision and, unless
    require_all_done is False, while any parish row is not green (approved, confirmed no hours, confirmed
    standing hours or not expected)."""
    get_period(org_id, period_id)
    left = unresolved(_period_rows(org_id, period_id))
    unentered = sum(count_unentered(org_id, period_id).values())
    if left or unentered:
        parts = []
        if left:
            parts.append(f"{len(left)} line(s) still need a decision")
        if unentered:
            parts.append(f"{unentered} person(s) named in emails were not entered")
        raise PayrollError("refused", " and ".join(parts).capitalize() + ", so the hours cannot be marked Final.")
    if require_all_done:
        notdone = parishes_not_done(org_id, period_id)
        if notdone:
            codes = ", ".join(p["code"] or p["name"] for p in notdone[:8]) + (" and more" if len(notdone) > 8 else "")
            raise PayrollError("refused", f"{len(notdone)} parish row(s) are not green yet ({codes}), so the hours cannot "
                                          "be marked Final. Approve each one, confirm no hours, or confirm its standing hours.")
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
    review, conflicts and waiting proposals. For the Time Review board."""
    get_period(org_id, period_id)
    out: dict[int, dict] = {}

    def slot(pid):
        return out.setdefault(pid, {"pattern": None, "channel": None, "emails": 0, "first_received_at": None,
                                    "needs_review": 0, "conflicts": 0, "proposals": 0, "pending_hires": 0,
                                    "hours": Decimal(0), "recorded": 0, "sources": set(),
                                    # standing hours (source = standing) and everything else, for the Std / Entered columns
                                    "std": Decimal(0), "entered": Decimal(0), "entered_lines": 0,
                                    "standing_expected": Decimal(0), "standing_people": 0,
                                    "sub_id": None, "sub_status": None, "unentered": 0, "_fp": []})

    for r in db.query("SELECT p.id, pr.pattern FROM portal.parishes p JOIN portal.parish_payroll_profile pr "
                      "ON pr.parish_id = p.id WHERE p.org_id = %s", (org_id,)):
        slot(r["id"])["pattern"] = r["pattern"]
    for r in db.query("SELECT s.id, s.status, s.parish_id, s.channel, s.first_received_at FROM portal.time_entry_submissions s "
                      "JOIN portal.parishes p ON p.id = s.parish_id WHERE s.period_id = %s AND p.org_id = %s",
                      (period_id, org_id)):
        d = slot(r["parish_id"])
        d["channel"], d["first_received_at"] = r["channel"], r["first_received_at"]
        d["sub_id"], d["sub_status"] = r["id"], r["status"]
        d["_fp"].append(("sub", str(r["id"]), str(r["status"])))
    for r in db.query("SELECT e.parish_id, count(*) AS n, min(e.received_at) AS first FROM portal.time_email_receipts e "
                      "JOIN portal.parishes p ON p.id = e.parish_id WHERE e.period_id = %s AND p.org_id = %s "
                      "GROUP BY e.parish_id", (period_id, org_id)):
        d = slot(r["parish_id"])
        d["emails"], d["first_received_at"] = r["n"], r["first"] or d["first_received_at"]
        d["_fp"].append(("rcpt", str(r["n"])))
    # An email that only produced "named, not entered" people recorded no hours, so it has no receipt row, but it DID arrive.
    # Count it (once per email) so the board does not say "Not Started" for a parish that has emailed.
    for r in db.query("SELECT u.parish_id, count(DISTINCT u.source_ref) AS n, min(coalesce(nt.received_at, u.created_at)) AS first "
                      "FROM portal.time_email_unentered u JOIN portal.parishes p ON p.id = u.parish_id "
                      "LEFT JOIN portal.time_email_notes nt ON nt.period_id = u.period_id AND nt.source_ref = u.source_ref "
                      "WHERE u.period_id = %s AND p.org_id = %s AND NOT EXISTS (SELECT 1 FROM portal.time_email_receipts e "
                      "WHERE e.period_id = u.period_id AND e.parish_id = u.parish_id AND e.source_ref = u.source_ref) "
                      "GROUP BY u.parish_id", (period_id, org_id)):
        d = slot(r["parish_id"])
        d["emails"] += r["n"]
        d["_fp"].append(("named", str(r["n"])))
        if r["first"] and (d["first_received_at"] is None or r["first"] < d["first_received_at"]):
            d["first_received_at"] = r["first"]
    for r in _period_rows(org_id, period_id):
        d = slot(r["parish_id"])
        # what the parish's period holds: counted hours, plus the grid's hours where a conflict holds them back
        hrs = (r["counted_hours"] if r["counted_hours"] is not None else (r["grid_hours"] or Decimal(0)))
        d["hours"] += hrs
        d["recorded"] += 1
        d["sources"].add(r["source"])
        if r["source"] == "standing":
            d["std"] += hrs
        else:
            d["entered"] += hrs
            d["entered_lines"] += 1
        d["_fp"].append(("line", str(r["line_id"]), str(r["staff_id"]), str(r["category_id"]), str(r["counted_hours"]),
                         str(r["review_status"]), str(r["state"]), str(r["proposed_hours"]), str(r["source"])))
        if r["state"] == "needs_review":
            d["needs_review"] += 1
        elif r["state"] == "conflict":
            d["conflicts"] += 1
        elif r["state"] == "proposal":
            d["proposals"] += 1
        elif r["state"] == "pending_hire":
            d["pending_hires"] += 1
    # people an email named that nobody entered
    for pid, n in count_unentered(org_id, period_id).items():
        d = slot(pid)
        d["unentered"] = n
        d["_fp"].append(("unentered", str(n)))
    # what the roster says the parish's standing hours are (shown before anyone confirms them)
    for r in db.query("SELECT s.parish_id, count(*) AS n, COALESCE(sum(s.standing_hours), 0) AS h FROM portal.staff_roster s "
                      "JOIN portal.parishes p ON p.id = s.parish_id WHERE p.org_id = %s AND s.is_active AND s.captures_hours "
                      "AND s.hours_basis = 'standing' AND s.standing_hours IS NOT NULL AND s.employee_number IS NOT NULL "
                      "GROUP BY s.parish_id", (org_id,)):
        d = slot(r["parish_id"])
        d["standing_people"], d["standing_expected"] = r["n"], Decimal(r["h"])
    for d in out.values():
        d["fingerprint"] = _fingerprint(d.pop("_fp"))
    return out


def _fingerprint(parts: list) -> str:
    """A short code for everything a parish holds in a period. An approval stores it; if it changes later, the approval is stale."""
    return hashlib.md5(repr(sorted(parts)).encode("utf-8")).hexdigest()


EMPTY_FINGERPRINT = _fingerprint([])


# ------------------------------------------------------------- Time Review: reviewed and approved
# One approval per parish and period (migration 093). Green on the board means DONE: approved, confirmed no hours,
# confirmed standing hours, or a parish set to "No hours expected". An approval stores a fingerprint of what the parish
# held at that moment, so anything that changes afterwards shows the row as "changed after approval".

APPROVAL_KINDS = ("approved", "no_hours", "standing")


def approvals_for_period(org_id: int, period_id: int) -> dict[int, dict]:
    """Per parish id: the approval record (kind, fingerprint, who, when). {} when the table does not exist yet
    (migration 093 not applied), so the board still works."""
    from psycopg import errors as pgerr
    try:
        rows = db.query(
            "SELECT a.parish_id, a.kind, a.fingerprint, a.approved_at, a.approved_by_user_id, u.display_name, u.email "
            "FROM portal.time_parish_approval a JOIN portal.parishes p ON p.id = a.parish_id "
            "LEFT JOIN checkreq.app_users u ON u.id = a.approved_by_user_id "
            "WHERE a.period_id = %s AND p.org_id = %s", (period_id, org_id))
    except pgerr.UndefinedTable:
        return {}
    return {r["parish_id"]: r for r in rows}


def classify_parish(ex: dict, sub_id, sub_status, approval: dict | None) -> dict:
    """The ONE rule for a board row. state: attn (red) | ready (blue) | miss (amber) | wait (no colour) | ok (green)
    | idle (green, a parish set to No hours expected). pill names the status pill the board draws."""
    pattern = ex.get("pattern")
    open_items = (ex.get("needs_review", 0) + ex.get("pending_hires", 0) + ex.get("unentered", 0)
                  + ex.get("conflicts", 0) + ex.get("proposals", 0))
    recorded = ex.get("recorded", 0) > 0
    emails = ex.get("emails", 0)
    valid = bool(approval) and approval["fingerprint"] == ex.get("fingerprint", EMPTY_FINGERPRINT)
    stale = bool(approval) and not valid
    only_std = pattern == "standing_hours" and not ex.get("entered_lines") and not emails and not sub_id
    out = {"approval_kind": approval["kind"] if valid else None, "stale": stale, "only_std": only_std,
           "open_items": open_items}
    if valid and not open_items:
        out["state"] = "ok"
        out["pill"] = {"approved": "approved", "no_hours": "no_hours", "standing": "only_std_done"}[approval["kind"]]
    elif stale:
        out["state"], out["pill"] = "attn", "changed"
    elif open_items:
        out["state"], out["pill"] = "attn", "legacy"
    elif pattern == "none" and not sub_id and not recorded and not emails:
        out["state"], out["pill"] = "idle", "not_expected"
    elif only_std:
        out["state"], out["pill"] = "ready", "only_std"
    elif not recorded and not sub_id:
        out["state"] = "miss" if pattern in ("every_period", "most_periods") else "wait"
        out["pill"] = "legacy"
    elif sub_status == "draft" and not recorded:
        out["state"], out["pill"] = "wait", "legacy"
    else:
        out["state"], out["pill"] = "ready", "ready"
    out["done"] = out["state"] in ("ok", "idle")
    return out


def parishes_not_done(org_id: int, period_id: int) -> list[dict]:
    """Parish rows of the Time Review board that are not green yet (code, name, state)."""
    ex_all = board_extras(org_id, period_id)
    appr = approvals_for_period(org_id, period_id)
    out = []
    for p in db.query("SELECT id, code, name FROM portal.parishes WHERE org_id = %s AND is_active "
                      "AND (modules->>'timekeeping') = 'true' ORDER BY code NULLS LAST", (org_id,)):
        ex = ex_all.get(p["id"], {})
        c = classify_parish(ex, ex.get("sub_id"), ex.get("sub_status"), appr.get(p["id"]))
        if not c["done"]:
            out.append({"parish_id": p["id"], "code": p["code"], "name": p["name"], "state": c["state"]})
    return out


def _open_period(org_id: int, period_id: int) -> dict:
    period = get_period(org_id, period_id)
    if period["status"] != "open":
        raise PayrollError("refused", f"This pay period is {period['status']}. Approvals are made while it is Open.")
    return period


def _parish_extras(org_id: int, period_id: int, parish_id: int) -> dict:
    if not db.query_one("SELECT id FROM portal.parishes WHERE id = %s AND org_id = %s", (parish_id, org_id)):
        raise PayrollError("not_found", "That parish was not found for this diocese.")
    return board_extras(org_id, period_id).get(parish_id) or {"fingerprint": EMPTY_FINGERPRINT}


def _store_approval(period_id: int, parish_id: int, kind: str, user_id: int, ex: dict) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.time_parish_approval (period_id, parish_id, kind, fingerprint, approved_by_user_id) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (period_id, parish_id) DO UPDATE SET kind = EXCLUDED.kind, "
                "fingerprint = EXCLUDED.fingerprint, approved_by_user_id = EXCLUDED.approved_by_user_id, approved_at = now()",
                (period_id, parish_id, kind, ex.get("fingerprint", EMPTY_FINGERPRINT), user_id))
            conn.commit()


def approve_parish(org_id: int, period_id: int, parish_id: int, user_id: int) -> dict:
    """Mark reviewed and approved. Refused while anything in the parish still needs a decision, or when no hours are
    recorded (that is Confirm no hours)."""
    _open_period(org_id, period_id)
    ex = _parish_extras(org_id, period_id, parish_id)
    c = classify_parish(ex, ex.get("sub_id"), ex.get("sub_status"), None)
    if c["open_items"]:
        raise PayrollError("refused", f"{c['open_items']} item(s) in this parish still need a decision, so it cannot be approved yet.")
    if not ex.get("recorded"):
        raise PayrollError("refused", "No hours are recorded for this parish. If none are expected, use Confirm no hours this week.")
    _store_approval(period_id, parish_id, "approved", user_id, ex)
    return {"kind": "approved"}


def confirm_no_hours(org_id: int, period_id: int, parish_id: int, user_id: int) -> dict:
    """HR confirms this parish has no hours this period. Refused when it has hours, an email or anything waiting."""
    _open_period(org_id, period_id)
    ex = _parish_extras(org_id, period_id, parish_id)
    c = classify_parish(ex, ex.get("sub_id"), ex.get("sub_status"), None)
    if c["open_items"] or ex.get("recorded") or ex.get("emails"):
        raise PayrollError("refused", "This parish has hours, an email or people waiting for this period. Review those instead.")
    _store_approval(period_id, parish_id, "no_hours", user_id, ex)
    return {"kind": "no_hours"}


def confirm_standing(org_id: int, period_id: int, user_id: int, parish_id: int | None = None) -> dict:
    """Records the standing hours (carry_forward_standing) and approves every parish that holds ONLY standing hours.
    A parish that also has other hours keeps its normal approval. parish_id limits it to one parish."""
    _open_period(org_id, period_id)
    if parish_id is not None:
        _parish_extras(org_id, period_id, parish_id)  # not_found when it is another diocese's
    res = carry_forward_standing(org_id, period_id, user_id, parish_id)
    approved = 0
    appr = approvals_for_period(org_id, period_id)
    for pid, ex in board_extras(org_id, period_id).items():
        if parish_id is not None and pid != parish_id:
            continue
        if not ex.get("standing_people") or not ex.get("std"):
            continue
        c = classify_parish(ex, ex.get("sub_id"), ex.get("sub_status"), appr.get(pid))
        if c["only_std"] and not c["open_items"] and c["state"] != "ok":
            _store_approval(period_id, pid, "standing", user_id, ex)
            approved += 1
    res["approved"] = approved
    return res


def undo_approval(org_id: int, period_id: int, parish_id: int) -> dict:
    _open_period(org_id, period_id)
    _parish_extras(org_id, period_id, parish_id)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM portal.time_parish_approval WHERE period_id = %s AND parish_id = %s", (period_id, parish_id))
            n = cur.rowcount
            conn.commit()
    return {"removed": n}


def approve_all(org_id: int, period_id: int, user_id: int) -> dict:
    """Actions > Approve all. Approves every department that is ready: hours recorded and nothing waiting for a decision
    (a department that only has standing hours gets them confirmed first). It NEVER approves over something that needs a
    decision, and it never decides 'no hours' for anyone (that stays Confirm no hours). Returns counts."""
    _open_period(org_id, period_id)
    appr = approvals_for_period(org_id, period_id)
    approved = already = waiting = no_hours = 0
    for pid, ex in board_extras(org_id, period_id).items():
        c = classify_parish(ex, ex.get("sub_id"), ex.get("sub_status"), appr.get(pid))
        if c["state"] in ("ok", "idle"):
            already += 1
        elif c["open_items"]:
            waiting += 1
        elif c["only_std"]:
            approved += confirm_standing(org_id, period_id, user_id, pid).get("approved", 0)
        elif ex.get("recorded"):
            _store_approval(period_id, pid, "approved", user_id, ex)
            approved += 1
        else:
            no_hours += 1
    return {"approved": approved, "already": already, "waiting": waiting, "no_hours": no_hours}


def parish_approval_state(org_id: int, period_id: int, parish_id: int) -> dict:
    """What the parish's own page needs to draw its Approve button: the board's one rule for this parish."""
    ex = _parish_extras(org_id, period_id, parish_id)
    c = classify_parish(ex, ex.get("sub_id"), ex.get("sub_status"), approvals_for_period(org_id, period_id).get(parish_id))
    return {"state": c["state"], "pill": c["pill"], "kind": c["approval_kind"], "only_std": c["only_std"],
            "open_items": c["open_items"], "stale": c["stale"], "recorded": bool(ex.get("recorded")),
            "emails": ex.get("emails", 0)}


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
        "SELECT rh.staff_id, rh.category_id, rh.hours, c.label, c.key AS cat_key, c.sort_order, sr.employee_number, sr.first_name, "
        "sr.last_name, sr.captures_hours, p.code AS parish_code, p.name AS parish_name, "
        "COALESCE(p.short_name, p.name) AS dept_name "
        "FROM portal.time_register_hours rh JOIN portal.staff_roster sr ON sr.id = rh.staff_id "
        "JOIN portal.parishes p ON p.id = sr.parish_id JOIN portal.timekeeping_categories c ON c.id = rh.category_id "
        "WHERE rh.period_id = %s AND p.org_id = %s", (period_id, org_id))
    rmap = {(x["staff_id"], x["category_id"]): x for x in reg if x["captures_hours"]}
    decisions = pdec.for_period(period_id)      # how each differing line was settled at close-out (26-158, 2026-10-22)
    manual_exp = pdec.manual_expected(period_id)  # hours the previous period paid by interpay manual check, due on THIS register
    manual_left = dict(manual_exp)
    rows = []
    tot_b: dict = {}  # per person, ALL categories together: what Beacon counts and what was paid
    tot_g: dict = {}
    def order_key(k):
        """Department, then employee (last, first, employee number: one person's lines stay together), then the kind of hours in the
        screen order Reg, OT, Sick, Vac, Holiday (Jay, 2026-10-17)."""
        src = beacon.get(k) or rmap.get(k)
        return (src.get("parish_code") or "~", (src.get("last_name") or "").lower(), (src.get("first_name") or "").lower(),
                src.get("employee_number") or "", k[0], category_order.rank(src.get("category_key") or src.get("cat_key")),
                src.get("category_sort") or src.get("sort_order") or 0, k[1])

    for key in sorted(set(beacon) | set(rmap), key=order_key):
        b, g = beacon.get(key), rmap.get(key)
        bh = None if b is None else b["counted_hours"]
        gh_raw = None if g is None else Decimal(g["hours"])
        # Hours the previous period paid by an interpay manual check show on THIS register but are not Beacon hours of this
        # period (they were counted in the earlier one), so they are taken off the register side before comparing.
        mc = Decimal(0)
        if gh_raw is not None and key in manual_exp and gh_raw >= manual_exp[key]:
            mc = manual_exp[key]
            manual_left.pop(key, None)
        gh = None if gh_raw is None else gh_raw - mc
        if (g is None and bh == 0) or (b is None and gh == 0):
            continue  # nothing paid and nothing counted is agreement (an actualized "not paid" line holds 0)
        tot_b[key[0]] = tot_b.get(key[0], Decimal(0)) + (bh or Decimal(0))
        tot_g[key[0]] = tot_g.get(key[0], Decimal(0)) + (gh or Decimal(0))
        if b is not None and g is not None:
            kind = "match" if abs(bh - gh) < Decimal("0.005") else "differs"
        elif g is not None:
            kind = "paid_no_beacon_hours"
        else:
            kind = "beacon_hours_not_paid"
        src = b or g
        diff = (bh or Decimal(0)) - (gh or Decimal(0))
        dec = decisions.get(key)
        rows.append({"kind": kind, "staff_id": src["staff_id"], "category_id": src["category_id"],
                     # the decision made for this line at close-out, if any (stale = made about a different difference)
                     "decision": dec["decision"] if dec else None, "decision_note": dec["note"] if dec else None,
                     "decision_applied": bool(dec and dec["applied_at"]),
                     "decision_stale": bool(dec and pdec.is_stale(dec, diff)),
                     "manual_check_hours": float(mc),
                     "parish_code": src["parish_code"], "parish_name": src["parish_name"],
                     "dept_name": src.get("dept_name") or src["parish_name"],
                     "employee_number": src["employee_number"], "last_name": src["last_name"],
                     "first_name": src["first_name"],
                     "category_label": src["category_label"] if b else src["label"],
                     # standing hours (source = standing) and everything else Beacon counted, side by side
                     "std_hours": float(bh) if (b is not None and bh is not None and b["source"] == "standing") else None,
                     "entered_hours": float(bh) if (b is not None and bh is not None and b["source"] != "standing") else None,
                     "beacon_hours": None if bh is None else float(bh),
                     "register_hours": None if gh_raw is None else float(gh_raw),  # as the register shows it
                     "difference": float(diff)})  # Beacon minus paid (less any manual-check hours due from the earlier period); a missing side is 0, so a person's rows add up
    # A person whose categories differ but whose TOTAL hours tie to the paid hours is not a difference (Jay, 2026-10-17):
    # for example Holiday or Sick hours Beacon keeps as entered that payroll paid as Regular. Such rows are marked
    # category_only; Actualize leaves them alone and notes them for review.
    for r in rows:
        tb, tg = tot_b.get(r["staff_id"], Decimal(0)), tot_g.get(r["staff_id"], Decimal(0))
        r["person_beacon_total"] = float(tb)
        r["person_register_total"] = float(tg)
        if r["kind"] != "match" and abs(tb - tg) < Decimal("0.005"):
            r["kind"] = "category_only"
    # A line settled as "manual check" and already applied is resolved: Beacon keeps its hours for this period and the
    # interpay check shows on the NEXT period's register (payroll_decisions.py). It is listed, but it is no longer a difference.
    for r in rows:
        if r["kind"] not in ("match", "category_only") and r["decision"] == pdec.MANUAL_CHECK \
                and r["decision_applied"] and not r["decision_stale"]:
            r["kind"] = "manual_check"
    counts = {k: sum(1 for r in rows if r["kind"] == k)
              for k in ("match", "differs", "paid_no_beacon_hours", "beacon_hours_not_paid", "category_only", "manual_check")}
    category_only_people = len({r["staff_id"] for r in rows if r["kind"] == "category_only"})
    # lines that still differ: each needs ONE decision before Actualize (a stale decision counts as none)
    open_kinds = ("differs", "paid_no_beacon_hours", "beacon_hours_not_paid")
    undecided = sum(1 for r in rows if r["kind"] in open_kinds and (r["decision"] is None or r["decision_stale"]))
    decided = sum(1 for r in rows if r["kind"] in open_kinds) - undecided
    # manual-check hours the earlier period said would be on THIS register but are not (or are fewer)
    manual_missing = []
    if reg and manual_left:
        people = {s["id"]: s for s in db.query(
            "SELECT sr.id, sr.employee_number, sr.first_name, sr.last_name, p.code AS parish_code, "
            "COALESCE(p.short_name, p.name) AS dept_name FROM portal.staff_roster sr JOIN portal.parishes p ON p.id = sr.parish_id "
            "WHERE sr.id = ANY(%s)", ([k[0] for k in manual_left],))}
        cats = {c["id"]: c["label"] for c in db.query(
            "SELECT id, label FROM portal.timekeeping_categories WHERE org_id = %s", (org_id,))}
        for (sid, cid), hrs in sorted(manual_left.items()):
            s = people.get(sid)
            if s:
                manual_missing.append({"staff_id": sid, "category_id": cid, "category_label": cats.get(cid, ""),
                                       "employee_number": s["employee_number"], "first_name": s["first_name"],
                                       "last_name": s["last_name"], "parish_code": s["parish_code"],
                                       "dept_name": s["dept_name"], "hours": float(hrs)})
    # per department, hours only: what Beacon counts against what was paid, and how many rows differ
    depts: dict = {}
    for r in rows:
        d = depts.setdefault(r["parish_code"], {"parish_code": r["parish_code"], "dept_name": r["dept_name"],
                                                "beacon_hours": 0.0, "register_hours": 0.0, "differing": 0})
        d["beacon_hours"] += r["beacon_hours"] or 0.0
        d["register_hours"] += (r["register_hours"] or 0.0) - r["manual_check_hours"]  # less what an earlier period paid by manual check
        if r["kind"] not in ("match", "category_only", "manual_check"):
            d["differing"] += 1
    departments = sorted(({**d, "beacon_hours": round(d["beacon_hours"], 2), "register_hours": round(d["register_hours"], 2),
                           "difference": round(d["beacon_hours"] - d["register_hours"], 2)} for d in depts.values()),
                         key=lambda d: d["parish_code"] or "~")
    return {"rows": rows, "counts": counts, "register_loaded": bool(reg), "departments": departments,
            "category_only_people": category_only_people, "undecided": undecided, "decided": decided,
            "manual_missing": manual_missing}
