"""
payroll_closeout.py -- the pay period close-out (26-158, 2026-10-15).

After payroll is processed: the period is closed, the PAID hours are loaded (hours only, from the Checkwriters labor
distribution, by the close-out script on the payroll PC), and Beacon's hours are made to match them ("actualize"), so
every closed period reads the same as pay #1 to #20. The checklist screen (payroll_screens.py) and the service API
(payroll_api.py) are thin layers over these functions. All the shared rules (hours parsing, the edit log, the variance
report) stay in payroll_totals.py.

THE HARD RULE: hours only. Nothing here reads, stores or returns a rate, salary, wage or any amount of money.

ORDER (Jay, 2026-10-15): hours Final -> close the period -> load the paid hours -> tie out -> actualize -> file the
documents -> open the next period. Actualize needs a CLOSED period so email hours are no longer changing.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import db
import payroll_totals as pt
from payroll_totals import PayrollError

AUTO_ACTUALIZE_KEY = "payroll_auto_actualize:{org_id}"
# Checkwriters earning code -> Beacon category key (1Sal is salary: reference only, never loaded)
CATEGORY_OF_CODE = {"2Reg": "regular", "OT": "overtime", "Sick": "sick", "Vaca": "vacation", "Holi": "holiday"}
MAX_LINES = 5000


# ------------------------------------------------------------------ the diocese's auto switch

def auto_actualize_enabled(org_id: int) -> bool:
    """When ON, the moment the paid hours are loaded into a CLOSED period Beacon's hours are made to match them without
    a person pressing a button. OFF by default; each diocese decides."""
    import app_settings
    return (app_settings.get_setting(AUTO_ACTUALIZE_KEY.format(org_id=org_id), "false") or "false").lower() == "true"


def set_auto_actualize(org_id: int, on: bool, user_id: int | None) -> None:
    import app_settings
    if not db.query_one("SELECT id FROM checkreq.organizations WHERE id = %s", (org_id,)):
        raise PayrollError("not_found", "That diocese was not found.")
    app_settings.set_setting(AUTO_ACTUALIZE_KEY.format(org_id=org_id), "true" if on else "false", user_id)


# ------------------------------------------------------------------ loading the paid hours

def period_by_pay_date(org_id: int, pay_date) -> dict:
    """The diocese's period whose pay date is this date (2026-10-15 or 2026.10.15)."""
    text = str(pay_date or "").strip().replace(".", "-").replace("/", "-")
    try:
        day = dt.date.fromisoformat(text)
    except ValueError:
        raise PayrollError("invalid", "The pay date must look like 2026-10-15.")
    p = db.query_one("SELECT * FROM portal.payroll_periods WHERE org_id = %s AND pay_date = %s", (org_id, day))
    if not p:
        raise PayrollError("not_found", f"No pay period has the pay date {day.isoformat()}.")
    return p


def _check_dates(period: dict, period_start, period_end) -> None:
    for label, given, mine in (("start", period_start, period["period_start"]), ("end", period_end, period["period_end"])):
        if given is None or not str(given).strip():
            continue
        try:
            g = dt.date.fromisoformat(str(given).strip())
        except ValueError:
            raise PayrollError("invalid", f"The period {label} date must look like 2026-10-12.")
        if g != mine:
            raise PayrollError("refused", f"These paid hours are for a period that {label}s {g.isoformat()}, but pay "
                                          f"{period['pay_date'].isoformat()} {label}s {mine.isoformat()}. Wrong file?")


def load_register_batch(org_id: int, *, pay_date, lines: list, source_ref: str | None = None,
                        period_start=None, period_end=None, actor_user_id: int | None = None) -> dict:
    """The PAID hours of one pay period (hours only), replacing whatever paid hours the period held. Each line is
    {employee_number, category_key, hours}. People are found by their Checkwriters ID across the diocese; an ID that is not
    on the roster is reported (never stored, never guessed) and keeps the period from being stamped Actualized. If the
    diocese's auto switch is ON and the period is closed, Beacon is actualized straight away."""
    period = period_by_pay_date(org_id, pay_date)
    _check_dates(period, period_start, period_end)
    if period["status"] == "future":
        raise PayrollError("refused", "That pay period has not started, so there are no paid hours for it yet.")
    if not isinstance(lines, list) or not lines:
        raise PayrollError("invalid", "There are no lines to load.")
    if len(lines) > MAX_LINES:
        raise PayrollError("invalid", "That is more lines than one pay period can hold.")
    cats = {c["key"]: c["id"] for c in db.query(
        "SELECT id, key FROM portal.timekeeping_categories WHERE org_id = %s AND is_active", (org_id,))}
    want: dict = {}
    for i, ln in enumerate(lines, start=1):
        if not isinstance(ln, dict):
            raise PayrollError("invalid", f"Line {i} is not a line.")
        emp = str(ln.get("employee_number") or "").strip()
        key = str(ln.get("category_key") or "").strip().lower()
        if not emp:
            raise PayrollError("invalid", f"Line {i} has no employee number.")
        if key not in cats:
            raise PayrollError("not_found", f"No active category {key!r} for this diocese (line {i}).")
        h = pt.parse_hours(ln.get("hours"))
        if h == 0:
            continue
        want[(emp, cats[key])] = want.get((emp, cats[key]), Decimal(0)) + h
    emps = sorted({e for e, _ in want})
    staff_rows = db.query(
        "SELECT s.id, s.employee_number FROM portal.staff_roster s JOIN portal.parishes p ON p.id = s.parish_id "
        "WHERE p.org_id = %s AND s.employee_number = ANY(%s) ORDER BY s.employee_number, s.is_active DESC, s.id",
        (org_id, emps)) if emps else []
    staff_of: dict = {}
    for r in staff_rows:
        staff_of.setdefault(r["employee_number"], r["id"])  # the active row first, else the lowest id
    unmatched = sorted(e for e in emps if e not in staff_of)
    actor = actor_user_id if actor_user_id is not None else pt.service_user_id()
    ref = (str(source_ref).strip() if source_ref else None) or f"LaborDist {period['pay_date'].strftime('%Y.%m.%d')}"
    created = updated = unchanged = removed = loaded = 0
    keep: set = set()
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, staff_id, category_id, hours FROM portal.time_register_hours "
                        "WHERE period_id = %s FOR UPDATE", (period["id"],))
            have = {(r["staff_id"], r["category_id"]): r for r in cur.fetchall()}
            for (emp, cat_id), h in want.items():
                sid = staff_of.get(emp)
                if sid is None:
                    continue
                loaded += 1
                keep.add((sid, cat_id))
                ex = have.get((sid, cat_id))
                if ex is not None and Decimal(ex["hours"]) == h:
                    unchanged += 1
                    continue
                cur.execute(
                    "INSERT INTO portal.time_register_hours (period_id, staff_id, category_id, hours, source_ref, "
                    "loaded_by_user_id) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (period_id, staff_id, category_id) "
                    "DO UPDATE SET hours = EXCLUDED.hours, source_ref = EXCLUDED.source_ref, "
                    "loaded_by_user_id = EXCLUDED.loaded_by_user_id, loaded_at = now()",
                    (period["id"], sid, cat_id, h, ref, actor))
                if ex is None:
                    created += 1
                else:
                    updated += 1
            stale = [r["id"] for k, r in have.items() if k not in keep]
            if stale:
                cur.execute("DELETE FROM portal.time_register_hours WHERE id = ANY(%s)", (stale,))
                removed = len(stale)
            reset = ", actualized_at = NULL, actualized_by_user_id = NULL, actualized_automatically = FALSE" \
                if (created or updated or removed) else ""
            cur.execute(
                "UPDATE portal.payroll_periods SET register_loaded_at = now(), register_loaded_by_user_id = %s, "
                "register_source_ref = %s, register_unmatched = %s, updated_at = now()" + reset + " WHERE id = %s",
                (actor, ref, len(unmatched), period["id"]))
            conn.commit()
    out = {"period_id": period["id"], "pay_date": period["pay_date"].isoformat(), "period_status": period["status"],
           "loaded": loaded, "created": created, "updated": updated, "unchanged": unchanged, "removed": removed,
           "unmatched": unmatched, "actualized": None}
    if period["status"] == "closed" and auto_actualize_enabled(org_id):
        try:
            out["actualized"] = actualize_period(org_id, period["id"], actor, auto=True)
        except PayrollError as e:
            out["actualized"] = {"error": e.message}
    return out


# ------------------------------------------------------------------ actualize

def actualize_period(org_id: int, period_id: int, user_id: int | None, auto: bool = False) -> dict:
    """Makes Beacon's hours for a CLOSED period match the paid hours, hours only:
      differs                the line takes the paid hours (confirmed, source register); the old figure stays in the edit history
      paid, no Beacon hours  the person gets a line with the paid hours
      Beacon hours not paid  the hours become 0 with a note (the line is kept, never deleted)
    A line the parish's daily grid also covers is marked 'resolved_total' so the paid figure is the one that counts.
    When nothing differs afterwards, and every paid person was found on the roster, the period is stamped Actualized."""
    period = pt.get_period(org_id, period_id)
    if period["status"] == "open":
        raise PayrollError("refused", "Close the pay period first, then make Beacon match the paid hours.")
    if period["status"] == "future":
        raise PayrollError("refused", "That pay period has not started.")
    if period["register_loaded_at"] is None:
        raise PayrollError("refused", "The paid hours have not been loaded for this period yet.")
    actor = user_id if user_id is not None else pt.service_user_id()
    rep = pt.variance(org_id, period_id)
    todo = [r for r in rep["rows"] if r["kind"] != "match"]
    ref = period["register_source_ref"] or f"LaborDist {period['pay_date'].strftime('%Y.%m.%d')}"
    n_diff = n_new = n_zero = 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT staff_id, category_id FROM portal.time_entries WHERE period_id = %s "
                        "GROUP BY staff_id, category_id HAVING SUM(hours) > 0", (period_id,))
            grid = {(r["staff_id"], r["category_id"]) for r in cur.fetchall()}
            for r in todo:
                key = (r["staff_id"], r["category_id"])
                paid = Decimal(str(r["register_hours"])) if r["register_hours"] is not None else Decimal(0)
                flags = ["actualized"] + (["resolved_total"] if key in grid else [])
                cur.execute("SELECT id, hours, review_status FROM portal.time_period_totals "
                            "WHERE period_id = %s AND staff_id = %s AND category_id = %s FOR UPDATE",
                            (period_id, r["staff_id"], r["category_id"]))
                line = cur.fetchone()
                if r["kind"] == "beacon_hours_not_paid":
                    note = f"Actualized: Beacon had {r['beacon_hours']} hours, nothing was paid ({ref})"
                    n_zero += 1
                elif r["kind"] == "paid_no_beacon_hours":
                    note = f"Actualized: paid {paid} hours, Beacon had none counted ({ref})"
                    n_new += 1
                else:
                    note = f"Actualized: Beacon had {r['beacon_hours']} hours, paid {paid} ({ref})"
                    n_diff += 1
                if line:
                    cur.execute(
                        "UPDATE portal.time_period_totals SET hours = %s, source = 'register', review_status = 'confirmed', "
                        "proposed_hours = NULL, proposed_source_ref = NULL, proposed_quote = NULL, "
                        "flags = (SELECT COALESCE(array_agg(DISTINCT f), '{}') FROM unnest(flags || %s::text[]) f), "
                        "reviewed_by_user_id = %s, reviewed_at = now(), updated_at = now() WHERE id = %s",
                        (paid, flags, actor, line["id"]))
                    pt._log_edit(cur, line["id"], line["hours"], paid, line["review_status"], "confirmed", actor,
                                 "register", note)
                else:
                    cur.execute(
                        "INSERT INTO portal.time_period_totals (period_id, staff_id, category_id, hours, source, source_ref, "
                        "review_status, flags, reviewed_by_user_id, reviewed_at, created_by_user_id) "
                        "VALUES (%s,%s,%s,%s,'register',%s,'confirmed',%s,%s,now(),%s) RETURNING id",
                        (period_id, r["staff_id"], r["category_id"], paid, ref, flags, actor, actor))
                    pt._log_edit(cur, cur.fetchone()["id"], None, paid, None, "confirmed", actor, "register", note)
            conn.commit()
    after = pt.variance(org_id, period_id)
    left = sum(1 for r in after["rows"] if r["kind"] != "match")
    tied = left == 0 and not period["register_unmatched"]
    if tied:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE portal.payroll_periods SET actualized_at = now(), actualized_by_user_id = %s, "
                            "actualized_automatically = %s, updated_at = now() WHERE id = %s", (actor, bool(auto), period_id))
                conn.commit()
    return {"changed": n_diff, "added": n_new, "zeroed": n_zero, "left": left, "unmatched": period["register_unmatched"],
            "tied": tied, "automatic": bool(auto)}


# ------------------------------------------------------------------ opening the next period, and the checklist's facts

def next_period(org_id: int, period: dict) -> dict | None:
    return db.query_one("SELECT id, label, status FROM portal.payroll_periods WHERE org_id = %s AND period_start > %s "
                        "ORDER BY period_start LIMIT 1", (org_id, period["period_start"]))


def open_next_period(org_id: int, period_id: int, user_id: int) -> dict:
    """The next pay period (the one that starts after this one) is opened. This one must be Closed, and no other period
    may be open (the parish grid only ever targets one)."""
    period = pt.get_period(org_id, period_id)
    if period["status"] != "closed":
        raise PayrollError("refused", "Close this pay period first, then open the next one.")
    nxt = next_period(org_id, period)
    if not nxt:
        raise PayrollError("not_found", "No later pay period is set up. Add it under Payroll Periods first.")
    if nxt["status"] == "open":
        return {"id": nxt["id"], "label": nxt["label"], "already": True}
    if nxt["status"] != "future":
        raise PayrollError("refused", f"The next pay period ({nxt['label']}) is {nxt['status']}, not Future, so it was not changed.")
    other = db.query_one("SELECT label FROM portal.payroll_periods WHERE org_id = %s AND status = 'open' AND id <> %s LIMIT 1",
                         (org_id, nxt["id"]))
    if other:
        raise PayrollError("refused", f"{other['label']} is still open. Close it first.")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE portal.payroll_periods SET status = 'open', processed_by_user_id = NULL, processed_at = NULL, "
                        "updated_at = now() WHERE id = %s AND org_id = %s", (nxt["id"], org_id))
            conn.commit()
    return {"id": nxt["id"], "label": nxt["label"], "already": False}


def closeout_state(org_id: int, period_id: int) -> dict:
    """Everything the close-out checklist shows for one period (hours only)."""
    period = pt.get_period(org_id, period_id)
    rep = pt.variance(org_id, period_id) if period["register_loaded_at"] is not None else None
    who = None
    if period["actualized_by_user_id"]:
        u = db.query_one("SELECT display_name, email FROM checkreq.app_users WHERE id = %s", (period["actualized_by_user_id"],))
        who = (u["display_name"] or u["email"]) if u else None
    return {"period": period, "final": period["hours_finalized_at"] is not None, "closed": period["status"] == "closed",
            "register_loaded": period["register_loaded_at"] is not None,
            "register_lines": db.query_one("SELECT count(*) AS n FROM portal.time_register_hours WHERE period_id = %s",
                                           (period_id,))["n"],
            "variance": rep, "actualized": period["actualized_at"] is not None, "actualized_by": who,
            "auto": auto_actualize_enabled(org_id), "next": next_period(org_id, period)}
