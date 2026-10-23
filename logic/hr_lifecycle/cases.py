"""
cases.py -- one person going through one process (hr.cases) and the checklist copied onto it (hr.case_items).

Rules (Plan section 2, plus what we borrowed from other tools, section 3b):
  * a case copies its template's items when it opens (never linked), each with its own due and hard-deadline dates;
  * an item whose condition is false is copied as Not applicable, with the reason, so nothing disappears silently;
  * changing the hire date, the last day, the hours or the clergy/housing answers recomputes the dates of open items and
    re-checks the conditions of items that are still open or were set N/A by a condition (never an item a person closed);
  * item states: open, done, waived (needs a reason), not_applicable (a person needs a reason too);
  * a case completes only when every required item is done, waived or N/A; it can be withdrawn or marked not hired at
    any time; nothing is deleted;
  * every change writes hr.case_events and bumps hr.cases.last_activity_at (the board's stalled signal).
Every function takes the entity ids the caller may act for and refuses a case outside them (a crafted id from another
entity reads as not found).
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import psycopg

import db
import org_features

from . import clock, conditions, dates, eligibility, privacy, roster_link, templates
from .errors import HrError

OPEN_STATUSES = ("draft", "requested", "in_progress", "returned")
ITEM_STATUSES = ("open", "done", "waived", "not_applicable")
AUTO_NA_PREFIX = "Applies only to "
SEPARATION_CATEGORIES = {"resignation": "Resignation", "retirement": "Retirement", "end_of_term": "End of term",
                         "involuntary": "Involuntary", "other": "Other"}
OUTCOMES = {"completed": "Completed", "withdrawn": "Withdrawn", "not_hired": "Not hired"}
COVERAGES = {"medical": "Medical", "dental": "Dental", "life": "Group life", "std": "Short-term disability",
             "ltd": "Long-term disability", "retirement": "Retirement (RSVP / Lay DC)", "clergy_pension": "Clergy pension (CPF)"}
ELECTIONS = {"enroll": "Enroll", "decline": "Decline", "not_eligible": "Not eligible"}
ENROLL_STATUSES = {"pending": "Pending", "submitted": "Submitted", "confirmed": "Confirmed", "ended": "Ended"}
PLANS = {"ppo90": "PPO 90", "ppo80": "PPO 80", "cdhp20_hsa": "CDHP-20/HSA", "cdhp40_hsa": "CDHP-40/HSA",
         "delta_premium": "Delta Premium", "delta_comprehensive": "Delta Comprehensive", "rsvp": "RSVP", "lay_dc": "Lay DC",
         "clergy_db": "Clergy DB", "lay_db": "Lay DB"}
TIERS = {"single": "Single", "emp_spouse": "Employee + spouse", "emp_children": "Employee + children", "family": "Family"}

EDITABLE = ("first_name", "last_name", "preferred_name", "position", "is_clergy", "exemption", "hours_per_week",
            "housing_provided", "start_date", "separation_date", "separation_category", "parish_id",
            "payroll_employee_ref", "notes", "databank_contact_id")


# ------------------------------------------------------------------ settings and facts

def settings(org_id: int) -> dict:
    row = db.query_one("SELECT * FROM hr.entity_settings WHERE org_id = %s", (org_id,))
    base = {"health_hours_threshold": 1500, "pension_hours_threshold": 1000, "alert_lead_days": [10, 3],
            "stalled_after_days": 5, "file_retention_years": 7, "hr_folder": "800. HR Administration",
            "entity_hr_subfolder": "HRPayroll", "payroll_system": "none"}
    if row:
        base.update({k: v for k, v in dict(row).items() if v is not None})
    return base


def org_code(org_id: int) -> str:
    row = db.query_one("SELECT code FROM checkreq.organizations WHERE id = %s", (org_id,))
    if not row:
        raise HrError("not_found", "Unknown entity.")
    return row["code"]


def facts_for(case: dict, code: str, enrolled: set[str] | None = None) -> dict:
    return {"is_clergy": bool(case.get("is_clergy")), "health_eligible": case.get("health_eligible"),
            "pension_eligible": case.get("pension_eligible"), "housing_provided": case.get("housing_provided"),
            "org_code": code, "enrolled": enrolled}


def _enrolled_coverages(case_id: int | None) -> set[str] | None:
    """The coverages the linked case enrolled; None when there is no linked case or it has no coverage rows at all (unknown:
    then the offboarding items that end coverage all apply, rather than being dropped)."""
    if not case_id:
        return None
    rows = db.query("SELECT coverage, election, status FROM hr.enrollments WHERE case_id = %s", (case_id,))
    if not rows:
        return None
    return {r["coverage"] for r in rows if r["election"] == "enroll" and r["status"] != "ended"}


# ------------------------------------------------------------------ pure planning

def plan_items(template_items: list[dict], case: dict, facts: dict) -> list[dict]:
    """The items a case would get: each template item with applies (bool), na_reason, due_date, hard_deadline_date,
    and its time bucket. Pure (no database), so the New Case preview and open_case() agree by construction."""
    out = []
    for it in template_items:
        applies = conditions.evaluate(it.get("condition"), facts)
        due, hard = dates.due_dates(it, case)
        bkey, blabel = dates.bucket(it["due_basis"], it.get("due_offset_days") or 0)
        out.append({**it, "applies": applies,
                    "na_reason": None if applies else AUTO_NA_PREFIX + conditions.describe(it.get("condition")),
                    "due_date": due, "hard_deadline_date": hard, "bucket_key": bkey, "bucket": blabel})
    return out


def normalize_fields(raw: dict, *, for_update: bool = False) -> dict:
    """Clean and check the case fields a screen or the API sends. Raises HrError('invalid'|'pii')."""
    f: dict = {}
    for k in EDITABLE:
        if k not in raw:
            continue
        v = raw[k]
        if k in ("first_name", "last_name", "preferred_name", "position", "payroll_employee_ref", "databank_contact_id",
                 "notes", "exemption", "separation_category") and v is not None and not isinstance(v, str):
            if isinstance(v, (int, float)) and k in ("payroll_employee_ref", "databank_contact_id"):
                v = str(v)
            else:
                raise HrError("invalid", f"{k.replace('_', ' ').capitalize()} must be text.")
        if isinstance(v, str):
            v = v.strip()
        if k in ("first_name", "last_name"):
            if not v:
                raise HrError("invalid", "First and last name are required.")
            if len(v) > 80:
                raise HrError("invalid", "Names must be 80 characters or fewer.")
            privacy.check_text(v, "Name")
        elif k in ("preferred_name", "position", "payroll_employee_ref", "databank_contact_id"):
            v = v or None
            if v and len(v) > 120:
                raise HrError("invalid", f"{k.replace('_', ' ').capitalize()} is too long.")
            if v and k != "payroll_employee_ref":  # a payroll employee ID is not personal data (Jay, 2026-10-22)
                privacy.check_text(v, k.replace("_", " ").capitalize())
        elif k in ("is_clergy", "housing_provided"):
            if v in ("", None):
                v = None if k == "housing_provided" else False
            elif isinstance(v, str):
                v = v.lower() in ("1", "true", "yes", "on", "y")
            else:
                v = bool(v)
        elif k == "exemption":
            v = v or None
            if v not in (None, "exempt", "non_exempt"):
                raise HrError("invalid", "Exempt status must be exempt or non_exempt.")
        elif k == "hours_per_week":
            try:
                v = eligibility.parse_hours(v)
            except ValueError as e:
                raise HrError("invalid", str(e))
        elif k in ("start_date", "separation_date"):
            if v in ("", None):
                v = None
            elif not isinstance(v, dt.date):
                try:
                    v = dt.date.fromisoformat(str(v))
                except ValueError:
                    raise HrError("invalid", f"{'Hire date' if k == 'start_date' else 'Last day'} must be a date like 2026-11-02.")
        elif k == "separation_category":
            v = v or None
            if v and v not in SEPARATION_CATEGORIES:
                raise HrError("invalid", "Unknown separation reason.")
        elif k == "parish_id":
            try:
                v = int(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                raise HrError("invalid", "The employing unit must be chosen from the list.")
        elif k == "notes":
            v = privacy.check_text(v, "Case notes")
        f[k] = v
    if not for_update:
        for need in ("first_name", "last_name"):
            if not f.get(need):
                raise HrError("invalid", "First and last name are required.")
    return f


def derive(case: dict, st: dict) -> dict:
    """Fill the computed fields: eligibility (unless overridden) and the benefits effective date."""
    out = dict(case)
    if not out.get("eligibility_overridden"):
        out["health_eligible"], out["pension_eligible"] = eligibility.compute(
            out.get("hours_per_week"), st["health_hours_threshold"], st["pension_hours_threshold"])
    out["benefits_effective_date"] = dates.benefits_effective(out.get("start_date"))
    return out


def preview(org_id: int, kind: str, raw: dict, linked_case_id: int | None = None) -> dict:
    """What opening this case would produce, without saving anything: the template, the computed fields and every item
    with applies / N/A reason and dates (BambooHR-style preview-and-adjust)."""
    code = org_code(org_id)
    tpl = templates.active_template(kind, code)
    if not tpl:
        raise HrError("refused", f"There is no active {templates.KINDS.get(kind, kind).lower()} checklist for {code}. "
                                 "Load the templates first.")
    fields = normalize_fields(raw)
    case = derive({**fields, "opened_at": clock.today(org_id)}, settings(org_id))
    items = plan_items(tpl["items"], case, facts_for(case, code, _enrolled_coverages(linked_case_id)))
    return {"template": {k: tpl[k] for k in ("id", "key", "version", "label", "case_kind")}, "case": case, "items": items}


# ------------------------------------------------------------------ events

def _event(cur, case_id: int, event: str, actor_id: int | None, detail: dict | None = None, item_id: int | None = None):
    cur.execute("INSERT INTO hr.case_events (case_id, case_item_id, event, detail, actor_user_id) VALUES (%s,%s,%s,%s,%s)",
                (case_id, item_id, event, json.dumps(detail or {}, default=str), actor_id))
    cur.execute("UPDATE hr.cases SET last_activity_at = now(), updated_at = now() WHERE id = %s", (case_id,))


def _next_case_number(cur, today: dt.date) -> str:
    prefix = f"HR-{today:%y}-"
    cur.execute("SELECT coalesce(max(substring(case_number from 7)::int), 0) AS n FROM hr.cases "
                "WHERE case_number LIKE %s AND substring(case_number from 7) ~ '^[0-9]+$'", (prefix + "%",))
    return f"{prefix}{cur.fetchone()['n'] + 1:04d}"


# ------------------------------------------------------------------ open

def _check_unit(org_id: int, parish_id: int | None, kind: str) -> None:
    """Diocesan cases (onboard / offboard) take only an employing unit -- a non-congregation record such as Diocesan Office --
    never a parish, or completing the case would put a diocesan employee on a parish's roster and payroll register."""
    if parish_id is None:
        return
    row = db.query_one("SELECT coalesce(is_congregation, true) AS cong FROM portal.parishes WHERE id = %s AND org_id = %s",
                       (parish_id, org_id))
    if not row:
        raise HrError("invalid", "That employing unit or parish is not part of this entity.")
    if kind in ("onboard", "offboard") and row["cong"] and not org_features.is_enabled(org_id, "hr_staff_by_department"):
        # an entity whose staff are filed by department (DME: Phoenix payroll, one department per parish) switches this on
        raise HrError("invalid", "Diocesan staff go under an employing unit (such as Diocesan Office), not a parish.")


def _check_not_duplicate(org_id: int, kind: str, first: str, last: str, parish_id: int | None) -> None:
    """One open case of a kind per person. A double click, a re-entry or two people working the same hire would otherwise
    open the same person twice (Beacon had exactly that with check requests CR26-016 and CR26-017). Parish benefits cases
    also compare the parish, since two parishes can have staff with the same name."""
    sql = ("SELECT case_number FROM hr.cases WHERE org_id = %s AND case_kind = %s AND status = ANY(%s) "
           "AND lower(first_name) = lower(%s) AND lower(last_name) = lower(%s)")
    params: list = [org_id, kind, list(OPEN_STATUSES), first, last]
    if kind in ("benefits_enroll", "benefits_end"):
        sql += " AND coalesce(parish_id, 0) = coalesce(%s, 0)"
        params.append(parish_id)
    dup = db.query_one(sql + " ORDER BY id LIMIT 1", tuple(params))
    if dup:
        raise HrError("refused", f"{first} {last} already has an open {templates.KINDS[kind].lower()} case "
                                 f"({dup['case_number']}). Open that one, or withdraw it first.")


def open_case(org_id: int, kind: str, raw: dict, actor_id: int, *, source: str = "hr",
              linked_case_id: int | None = None, status: str | None = None, backfilled: bool = False) -> int:
    """Open a case from the active template. Returns the new case id."""
    if kind not in templates.KINDS:
        raise HrError("invalid", "Unknown case type.")
    p = preview(org_id, kind, raw, linked_case_id)
    case, items = p["case"], p["items"]
    _check_unit(org_id, case.get("parish_id"), kind)
    _check_not_duplicate(org_id, kind, case["first_name"], case["last_name"], case.get("parish_id"))
    if kind in ("offboard", "benefits_end") and not case.get("separation_date"):
        raise HrError("invalid", "The last day of employment is required to start offboarding.")
    status = status or ("requested" if source == "parish" else "in_progress")
    today = clock.today(org_id)
    for attempt in range(3):
        try:
            with db.connect() as conn:
                with conn.cursor() as cur:
                    number = _next_case_number(cur, today)
                    cur.execute(
                        "INSERT INTO hr.cases (case_number, org_id, parish_id, case_kind, template_id, linked_case_id, source, "
                        " first_name, last_name, preferred_name, position, is_clergy, databank_contact_id, exemption, "
                        " hours_per_week, health_eligible, pension_eligible, housing_provided, start_date, "
                        " benefits_effective_date, separation_date, separation_category, payroll_employee_ref, status, notes, "
                        " opened_by_user_id, submitted_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s, %s,"
                        " CASE WHEN %s = 'parish' THEN now() END) RETURNING id",
                        (number, org_id, case.get("parish_id"), kind, p["template"]["id"], linked_case_id, source,
                         case["first_name"], case["last_name"], case.get("preferred_name"), case.get("position"),
                         bool(case.get("is_clergy")), case.get("databank_contact_id"), case.get("exemption"),
                         case.get("hours_per_week"), case.get("health_eligible"), case.get("pension_eligible"),
                         case.get("housing_provided"), case.get("start_date"), case.get("benefits_effective_date"),
                         case.get("separation_date"), case.get("separation_category"), case.get("payroll_employee_ref"),
                         status, case.get("notes"), actor_id, source))
                    case_id = cur.fetchone()["id"]
                    for it in items:
                        if backfilled:
                            st, note = ("done", "Backfilled: in place before Beacon tracked it.") if it["applies"] \
                                else ("not_applicable", it["na_reason"])
                        else:
                            st, note = ("open", None) if it["applies"] else ("not_applicable", it["na_reason"])
                        cur.execute(
                            "INSERT INTO hr.case_items (case_id, template_item_id, item_key, section, label, owner_group, "
                            " required, doc_type_key, due_date, hard_deadline_date, status, status_note, auto_na, sort_order, "
                            " done_at, done_by_user_id) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s = 'done' THEN now() END, "
                            " CASE WHEN %s = 'done' THEN %s::int END)",
                            (case_id, it["id"], it["item_key"], it["section"], it["label"], it["owner_group"],
                             it["required"], it.get("doc_type_key"), it["due_date"], it["hard_deadline_date"], st, note,
                             not it["applies"], it.get("sort_order") or 0, st, st, actor_id))
                    _event(cur, case_id, "opened", actor_id, {"case_number": number, "kind": kind,
                                                              "template": f"{p['template']['key']} v{p['template']['version']}",
                                                              "backfilled": backfilled or None})
                conn.commit()
            return case_id
        except psycopg.errors.UniqueViolation:
            if attempt == 2:
                raise
    raise HrError("refused", "Could not assign a case number. Try again.")


# ------------------------------------------------------------------ read

def _scoped_case(case_id: int, org_ids: list[int]) -> dict:
    row = db.query_one(
        "SELECT c.*, o.code AS org_code, o.name AS org_name, p.name AS parish_name, p.code AS parish_code, "
        "       coalesce(p.short_name, p.name) AS parish_label, t.key AS template_key, t.version AS template_version, "
        "       u.display_name AS opened_by_name "
        "FROM hr.cases c JOIN checkreq.organizations o ON o.id = c.org_id "
        "LEFT JOIN portal.parishes p ON p.id = c.parish_id "
        "JOIN hr.templates t ON t.id = c.template_id "
        "LEFT JOIN checkreq.app_users u ON u.id = c.opened_by_user_id "
        "WHERE c.id = %s AND c.org_id = ANY(%s)", (case_id, list(org_ids)))
    if not row:
        raise HrError("not_found", "That case was not found.")
    return dict(row)


def get_case(case_id: int, org_ids: list[int]) -> dict:
    """The case with its items (grouped data ready for the screen), enrollments and recent history."""
    c = _scoped_case(case_id, org_ids)
    items = [dict(r) for r in db.query(
        "SELECT i.*, ti.due_basis, ti.due_offset_days, ti.hard_deadline_days, ti.help_text, ti.condition, "
        "       u.display_name AS done_by_name "
        "FROM hr.case_items i LEFT JOIN hr.template_items ti ON ti.id = i.template_item_id "
        "LEFT JOIN checkreq.app_users u ON u.id = i.done_by_user_id "
        "WHERE i.case_id = %s ORDER BY i.sort_order, i.id", (case_id,))]
    today = clock.today(c["org_id"])
    for it in items:
        it["bucket_key"], it["bucket"] = dates.bucket(it.get("due_basis") or "start_date", it.get("due_offset_days") or 0)
        it["state"] = item_state(it, today)
        if not it["required"] and it["state"] == "late":
            it["state"] = "due"  # an optional item past its date is a reminder, never a red flag
    c["items"] = items
    c["enrollments"] = [dict(r) for r in db.query(
        "SELECT * FROM hr.enrollments WHERE case_id = %s ORDER BY id", (case_id,))]
    c["events"] = [dict(r) for r in db.query(
        "SELECT e.*, u.display_name AS actor_name FROM hr.case_events e "
        "LEFT JOIN checkreq.app_users u ON u.id = e.actor_user_id WHERE e.case_id = %s ORDER BY e.at DESC, e.id DESC LIMIT 60",
        (case_id,))]
    c["counts"] = section_counts(items, today)
    c["ready_to_close"] = c["status"] in OPEN_STATUSES and not any(
        i["required"] and i["status"] == "open" for i in items)
    c["linked"] = None
    if c.get("linked_case_id"):
        c["linked"] = db.query_one("SELECT id, case_number, case_kind, status FROM hr.cases WHERE id = %s",
                                   (c["linked_case_id"],))
    c["followups"] = [dict(r) for r in db.query(
        "SELECT id, case_number, case_kind, status FROM hr.cases WHERE linked_case_id = %s ORDER BY id", (case_id,))]
    return c


def item_state(it: dict, today: dt.date | None = None) -> str:
    """done | na | late | due | open -- the same rule the board's chips use."""
    today = today or dt.date.today()
    if it["status"] in ("done", "waived"):
        return "done"
    if it["status"] == "not_applicable":
        return "na"
    hard, due = it.get("hard_deadline_date"), it.get("due_date")
    if (due and due < today) or (hard and hard <= today + dt.timedelta(days=3)):
        return "late"
    if (due and due <= today + dt.timedelta(days=10)) or (hard and hard <= today + dt.timedelta(days=10)):
        return "due"
    return "open"


BOARD_SECTIONS = [("documents", "Documents"), ("benefits", "Benefits"), ("payroll", "Payroll"), ("access", "Access"),
                  ("orientation", "Orientation / records")]


def _board_section(section: str) -> str:
    return "orientation" if section in ("orientation", "records") else section


def section_counts(items: list[dict], today: dt.date | None = None) -> dict:
    """{board section: {"done", "total", "state", "label"}} over REQUIRED items that apply. state: ok | late | due |
    open | na. Optional items never turn a chip red, but an open optional item past due shows in the case page."""
    today = today or dt.date.today()
    out = {k: {"done": 0, "total": 0, "late": 0, "due": 0} for k, _ in BOARD_SECTIONS}
    for it in items:
        if not it["required"] or it["status"] == "not_applicable":
            continue
        s = out[_board_section(it["section"])]
        s["total"] += 1
        st = item_state(it, today)
        if st == "done":
            s["done"] += 1
        elif st == "late":
            s["late"] += 1
        elif st == "due":
            s["due"] += 1
    for s in out.values():
        if s["total"] == 0:
            s["state"], s["label"] = "na", "N/A"
        elif s["done"] == s["total"]:
            s["state"], s["label"] = "ok", "Done"
        elif s["late"]:
            s["state"], s["label"] = "late", "Late"
        elif s["due"]:
            s["state"], s["label"] = "due", "Due soon"
        else:
            s["state"], s["label"] = "open", "Open"
    return out


# ------------------------------------------------------------------ change

def _recompute(cur, case: dict, code: str, actor_id: int) -> int:
    """Recompute dates for open items and re-check conditions for items still open or set N/A by a condition.
    Returns how many items changed."""
    cur.execute("SELECT i.*, ti.due_basis, ti.due_offset_days, ti.hard_deadline_days, ti.business_days, ti.condition "
                "FROM hr.case_items i JOIN hr.template_items ti ON ti.id = i.template_item_id WHERE i.case_id = %s",
                (case["id"],))
    facts = facts_for(case, code, _enrolled_coverages(case.get("linked_case_id")))
    changed = 0
    for it in cur.fetchall():
        auto_na = it["status"] == "not_applicable" and it["auto_na"]
        if it["status"] != "open" and not auto_na:
            continue
        due, hard = dates.due_dates(it, case)
        applies = conditions.evaluate(it["condition"], facts)
        new_status = "open" if applies else "not_applicable"
        new_note = None if applies else AUTO_NA_PREFIX + conditions.describe(it["condition"])
        if not applies and it["status"] == "not_applicable":
            new_note = it["status_note"]  # already N/A by its condition: keep the note as it is
        if (due, hard, new_status, new_note) != (it["due_date"], it["hard_deadline_date"], it["status"], it["status_note"]):
            cur.execute("UPDATE hr.case_items SET due_date = %s, hard_deadline_date = %s, status = %s, status_note = %s, "
                        "auto_na = %s WHERE id = %s", (due, hard, new_status, new_note, not applies, it["id"]))
            changed += 1
    return changed


def update_case(case_id: int, org_ids: list[int], raw: dict, actor_id: int, *,
                eligibility_override: tuple[bool | None, bool | None, str] | str | None = None) -> dict:
    """Change case fields (names, dates, hours, clergy, housing, unit, notes). Returns {"changed": [...], "items": n}."""
    c = _scoped_case(case_id, org_ids)
    if c["status"] not in OPEN_STATUSES:
        raise HrError("refused", "This case is closed. Reopen is not available; open a new case instead.")
    f = normalize_fields(raw, for_update=True)
    if "parish_id" in f:
        _check_unit(c["org_id"], f["parish_id"], c["case_kind"])
    if c["case_kind"] in ("offboard", "benefits_end") and "separation_date" in f and not f["separation_date"]:
        raise HrError("invalid", "The last day of employment cannot be cleared on an offboarding case.")
    merged = {**c, **f}
    merged["opened_at"] = clock.local_date(c["opened_at"], c["org_id"])  # 'opened' due dates use the entity's own date
    if eligibility_override == "clear":
        merged["eligibility_overridden"] = False
        merged["eligibility_note"] = None
    elif eligibility_override is not None:
        he, pe, note = eligibility_override
        merged["health_eligible"], merged["pension_eligible"] = he, pe
        merged["eligibility_overridden"] = True
        merged["eligibility_note"] = privacy.check_text(note, "Eligibility note")
        if not merged["eligibility_note"]:
            raise HrError("invalid", "Say why eligibility is being set by hand.")
    merged = derive(merged, settings(c["org_id"]))
    cols = [k for k in EDITABLE if k in f and f[k] != c.get(k)]
    computed = ["health_eligible", "pension_eligible", "benefits_effective_date", "eligibility_overridden", "eligibility_note"]
    for k in computed:
        if merged.get(k) != c.get(k):
            cols.append(k)
    if not cols:
        return {"changed": [], "items": 0}
    with db.connect() as conn:
        with conn.cursor() as cur:
            sets = ", ".join(f"{k} = %s" for k in cols)
            cur.execute(f"UPDATE hr.cases SET {sets}, updated_at = now() WHERE id = %s",
                        tuple(merged.get(k) for k in cols) + (case_id,))
            n = _recompute(cur, merged, c["org_code"], actor_id)
            _event(cur, case_id, "updated", actor_id,
                   {"fields": [k for k in cols if k not in ("notes",)], "items_rechecked": n})
        conn.commit()
    return {"changed": cols, "items": n}


def set_item_status(case_id: int, org_ids: list[int], item_key: str, status: str, note: str | None, actor_id: int) -> dict:
    c = _scoped_case(case_id, org_ids)
    if c["status"] not in OPEN_STATUSES:
        raise HrError("refused", "This case is closed.")
    if status not in ITEM_STATUSES:
        raise HrError("invalid", "Status must be open, done, waived or not_applicable.")
    note = privacy.check_text(note, "Item note")
    it = db.query_one("SELECT * FROM hr.case_items WHERE case_id = %s AND item_key = %s", (case_id, item_key))
    if not it:
        raise HrError("not_found", "That checklist item is not on this case.")
    if status in ("waived", "not_applicable") and not note:
        raise HrError("invalid", "Say why (a short reason is required to waive an item or mark it not applicable).")
    if status == it["status"] and (note or None) == (it["status_note"] or None):
        return {"item_key": item_key, "status": status, "changed": False}
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE hr.case_items SET status = %s, status_note = %s, auto_na = FALSE, "
                " done_at = CASE WHEN %s = 'open' THEN NULL ELSE now() END, "
                " done_by_user_id = CASE WHEN %s = 'open' THEN NULL ELSE %s::int END WHERE id = %s",
                (status, note, status, status, actor_id, it["id"]))
            if c["status"] == "draft":
                cur.execute("UPDATE hr.cases SET status = 'in_progress' WHERE id = %s", (case_id,))
            _event(cur, case_id, "item_status", actor_id, {"item": item_key, "from": it["status"], "to": status},
                   item_id=it["id"])
        conn.commit()
    return {"item_key": item_key, "status": status, "changed": True}


def record_enrollment(case_id: int, org_ids: list[int], coverage: str, raw: dict, actor_id: int) -> dict:
    """Create or change one coverage row on a case. Only plan, tier, status, dates, a confirmation reference and a
    dependent COUNT -- never dependent details or amounts."""
    c = _scoped_case(case_id, org_ids)
    if coverage not in COVERAGES:
        raise HrError("invalid", "Unknown coverage.")
    election = (raw.get("election") or "").strip()
    if election not in ELECTIONS:
        raise HrError("invalid", "Election must be enroll, decline or not_eligible.")
    status = (raw.get("status") or ("pending" if election == "enroll" else "confirmed")).strip()
    if status not in ENROLL_STATUSES:
        raise HrError("invalid", "Unknown enrollment status.")
    plan = (raw.get("plan_key") or "").strip() or None
    if plan and plan not in PLANS:
        raise HrError("invalid", "Unknown plan.")
    tier = (raw.get("tier") or "").strip() or None
    if tier and tier not in TIERS:
        raise HrError("invalid", "Unknown coverage tier.")
    dep = raw.get("dependents_count")
    try:
        dep = None if dep in (None, "") else int(dep)
    except (TypeError, ValueError):
        raise HrError("invalid", "Dependents must be a whole number.")
    if dep is not None and not (0 <= dep <= 20):
        raise HrError("invalid", "Dependents must be a count between 0 and 20.")

    def _d(k):
        v = raw.get(k)
        if v in (None, ""):
            return None
        try:
            return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v))
        except ValueError:
            raise HrError("invalid", f"{k.replace('_', ' ')} must be a date like 2026-12-01.")

    eff = _d("effective_date") or (c.get("benefits_effective_date") if election == "enroll" else None)
    ref = privacy.check_text(raw.get("carrier_ref"), "Confirmation reference")
    note = privacy.check_text(raw.get("note"), "Enrollment note")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hr.enrollments (case_id, coverage, election, plan_key, tier, dependents_count, status, "
                " submitted_at, confirmed_at, effective_date, end_date, carrier_ref, note, updated_by_user_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (case_id, coverage) DO UPDATE SET election = EXCLUDED.election, plan_key = EXCLUDED.plan_key, "
                " tier = EXCLUDED.tier, dependents_count = EXCLUDED.dependents_count, status = EXCLUDED.status, "
                " submitted_at = EXCLUDED.submitted_at, confirmed_at = EXCLUDED.confirmed_at, "
                " effective_date = EXCLUDED.effective_date, end_date = EXCLUDED.end_date, carrier_ref = EXCLUDED.carrier_ref, "
                " note = EXCLUDED.note, updated_by_user_id = EXCLUDED.updated_by_user_id, updated_at = now() RETURNING id",
                (case_id, coverage, election, plan, tier, dep, status, _d("submitted_at"), _d("confirmed_at"), eff,
                 _d("end_date"), ref, note, actor_id))
            _event(cur, case_id, "enrollment", actor_id, {"coverage": coverage, "election": election, "status": status})
        conn.commit()
    return {"coverage": coverage, "election": election, "status": status}


def close_case(case_id: int, org_ids: list[int], outcome: str, actor_id: int, note: str | None = None) -> dict:
    """completed (every required item done, waived or N/A; updates the roster) | withdrawn | not_hired."""
    c = _scoped_case(case_id, org_ids)
    if c["status"] not in OPEN_STATUSES:
        raise HrError("refused", "This case is already closed.")
    if outcome not in OUTCOMES:
        raise HrError("invalid", "Outcome must be completed, withdrawn or not_hired.")
    note = privacy.check_text(note, "Closing note")
    if outcome == "not_hired" and c["case_kind"] != "onboard":
        raise HrError("invalid", "Not hired applies only to onboarding.")
    open_required = db.query("SELECT label FROM hr.case_items WHERE case_id = %s AND required AND status = 'open' "
                             "ORDER BY sort_order", (case_id,))
    if outcome == "completed" and open_required:
        names = "; ".join(r["label"] for r in open_required[:5])
        more = f" and {len(open_required) - 5} more" if len(open_required) > 5 else ""
        raise HrError("refused", f"{len(open_required)} required item(s) are still open: {names}{more}.")
    roster_note = None
    with db.connect() as conn:
        with conn.cursor() as cur:
            # claim the close first, inside the transaction: a double submit or a retry finds the case already closed
            cur.execute("UPDATE hr.cases SET status = %s, close_outcome = %s, closed_at = now(), closed_by_user_id = %s "
                        "WHERE id = %s AND status = ANY(%s) RETURNING id",
                        ("complete" if outcome == "completed" else "cancelled", outcome, actor_id, case_id,
                         list(OPEN_STATUSES)))
            if not cur.fetchone():
                raise HrError("refused", "This case was closed a moment ago.")
            if outcome == "completed":
                cur.execute("SELECT count(*) AS n FROM hr.case_items WHERE case_id = %s AND required AND status = 'open'",
                            (case_id,))
                if cur.fetchone()["n"]:
                    raise HrError("refused", "An item was reopened a moment ago. Look again before closing.")
                roster_note = roster_link.on_complete(cur, c, actor_id)
            _event(cur, case_id, "closed", actor_id, {"outcome": outcome, "roster": roster_note, "note": note})
        conn.commit()
    return {"outcome": outcome, "roster": roster_note}


def start_offboarding(case_id: int, org_ids: list[int], raw: dict, actor_id: int) -> int:
    """Open an offboarding case (types 1-2) or an end-of-coverage case (type 3) from a person's onboarding or enrollment
    case, carrying the person over and linking the two."""
    c = _scoped_case(case_id, org_ids)
    kinds = {"onboard": "offboard", "benefits_enroll": "benefits_end"}
    if c["case_kind"] not in kinds:
        raise HrError("refused", "Offboarding starts from an onboarding or enrollment case.")
    if c["close_outcome"] != "completed":
        raise HrError("refused", "Offboarding starts from a completed case. Close this one first, or withdraw it.")
    existing = db.query_one("SELECT case_number FROM hr.cases WHERE linked_case_id = %s AND case_kind = %s "
                            "AND status = ANY(%s)", (case_id, kinds[c["case_kind"]], list(OPEN_STATUSES)))
    if existing:
        raise HrError("refused", f"Offboarding is already open for this person ({existing['case_number']}).")
    carry = {k: c.get(k) for k in ("first_name", "last_name", "preferred_name", "position", "is_clergy", "exemption",
                                   "hours_per_week", "housing_provided", "start_date", "parish_id", "payroll_employee_ref",
                                   "databank_contact_id")}
    carry.update({"separation_date": raw.get("separation_date"), "separation_category": raw.get("separation_category"),
                  "notes": raw.get("notes")})
    new_id = open_case(c["org_id"], kinds[c["case_kind"]], carry, actor_id, source=c["source"], linked_case_id=case_id)
    if c.get("staff_id"):
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE hr.cases SET staff_id = %s WHERE id = %s", (c["staff_id"], new_id))
            conn.commit()
    return new_id


def list_cases(org_ids: list[int], *, kind: str | None = None, status: str = "open", parish_id: int | None = None,
               q: str = "") -> list[dict]:
    where, params = ["c.org_id = ANY(%s)"], [list(org_ids)]
    if kind:
        where.append("c.case_kind = %s")
        params.append(kind)
    if status == "open":
        where.append("c.status = ANY(%s)")
        params.append(list(OPEN_STATUSES))
    elif status == "closed":
        where.append("NOT (c.status = ANY(%s))")
        params.append(list(OPEN_STATUSES))
    if parish_id:
        where.append("c.parish_id = %s")
        params.append(parish_id)
    if q:
        where.append("(c.last_name ILIKE %s OR c.first_name ILIKE %s OR c.case_number ILIKE %s)")
        params += [f"%{q}%"] * 3
    return [dict(r) for r in db.query(
        "SELECT c.id, c.case_number, c.case_kind, c.status, c.close_outcome, c.first_name, c.last_name, c.position, "
        "       c.is_clergy, c.start_date, c.separation_date, c.org_id, o.code AS org_code, c.parish_id, "
        "       coalesce(p.short_name, p.name) AS parish_label, c.opened_at, c.closed_at, c.last_activity_at "
        "FROM hr.cases c JOIN checkreq.organizations o ON o.id = c.org_id LEFT JOIN portal.parishes p ON p.id = c.parish_id "
        "WHERE " + " AND ".join(where) + " ORDER BY c.opened_at DESC, c.id DESC LIMIT 500", tuple(params))]
