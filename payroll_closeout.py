"""
payroll_closeout.py -- the pay period close-out (26-158, 2026-10-15).

After payroll is processed: the period is closed, the PAID hours are loaded (hours only, from the Checkwriters labor
distribution, by the close-out script on the payroll PC), and Beacon's hours are made to match them ("actualize"), so
every closed period reads the same as pay #1 to #20. The checklist screen (payroll_screens.py) and the service API
(payroll_api.py) are thin layers over these functions. All the shared rules (hours parsing, the edit log, the variance
report) stay in payroll_totals.py.

THE HARD RULE: hours only. Nothing here reads, stores or returns a rate, salary, wage or any amount of money.

ORDER (Jay, 2026-10-27; it was Final -> close -> load ... until then): hours Final (approved AND entered into Checkwriters) ->
load the paid hours -> tie out -> actualize -> file the documents -> CLOSE the period -> open the next period. Actualize needs the
hours Final (or a period already Closed) so emailed hours are no longer changing (Final holds them in the Late emails queue). Closing
is the next-to-last step, right before the next period opens, so Time Review never sits with no open period for long.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import db
import payroll_decisions as pdec
import payroll_totals as pt
import roster_departures
from payroll_totals import PayrollError

AUTO_ACTUALIZE_KEY = "payroll_auto_actualize:{org_id}"
# Checkwriters earning code -> Beacon category key (1Sal is salary: reference only, never loaded)
CATEGORY_OF_CODE = {"2Reg": "regular", "OT": "overtime", "Sick": "sick", "Vaca": "vacation", "Holi": "holiday"}
MAX_LINES = 5000


# ------------------------------------------------------------------ the diocese's auto switch

def auto_actualize_enabled(org_id: int) -> bool:
    """When ON, the moment the paid hours are loaded into a period whose hours are Final (or already Closed) Beacon's hours are made to
    match them without a person pressing a button. OFF by default; each diocese decides."""
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
    diocese's auto switch is ON and the period's hours are Final (or it is closed), Beacon is actualized straight away."""
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
        h = pt.parse_paid_hours(ln.get("hours"))      # may be negative: a voided check is a negative line (migration 107)
        if h == 0:
            continue
        want[(emp, cats[key])] = want.get((emp, cats[key]), Decimal(0)) + h
    # the register holds the NET per person and category (Roberge: +14, -80, +14 = -52). A net of zero stores nothing, and a net
    # beyond 300 either way is refused here, before anything is written.
    for k, net in list(want.items()):
        if net == 0:
            del want[k]
        elif abs(net) > pt.MAX_HOURS:
            raise PayrollError("refused", f"Employee {k[0]}: the net paid hours ({net}) are more than the 300 hours one line may hold, either way.")
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
    if pt.settle_ready(period) and auto_actualize_enabled(org_id):
        try:
            out["actualized"] = actualize_period(org_id, period["id"], actor, auto=True)
        except PayrollError as e:
            out["actualized"] = {"error": e.message}
    return out


# ------------------------------------------------------------------ tie-out decisions (Jay, 2026-10-22)

OPEN_KINDS = ("differs", "paid_no_beacon_hours", "beacon_hours_not_paid")


def need_final_or_closed(period: dict, then: str) -> None:
    """The gate for settling a period (Jay, 2026-10-27: it used to be "Close the pay period first"): the hours are Final, or the period
    is already Closed. `then` finishes the sentence ("settle the differences")."""
    if period["status"] == "future":
        raise PayrollError("refused", "That pay period has not started.")
    if not pt.settle_ready(period):
        raise PayrollError("refused", f"Mark the hours Final first (Time Review > Actions, once they are entered into Checkwriters), then {then}.")


def _need_closed_unactualized(period: dict) -> None:
    need_final_or_closed(period, "settle the differences")
    if period["register_loaded_at"] is None:
        raise PayrollError("refused", "The paid hours have not been loaded for this period yet.")
    if period["actualized_at"] is not None:
        raise PayrollError("refused", "This pay period is already actualized, so its decisions are locked.")


def _target_for(org_id: int, period: dict, decision: str) -> int | None:
    """The pay period a 'paid next period' or 'manual check' decision points at: the one after this."""
    if decision == pdec.BEACON_WRONG:
        return None
    nxt = next_period(org_id, period)
    if nxt is None:
        raise PayrollError("refused", "No later pay period is set up, so there is nowhere to carry these hours. "
                                      "Add the next pay period under Payroll Periods first.")
    if nxt["status"] == "closed":
        raise PayrollError("refused", f"The next pay period ({nxt['label']}) is already closed, so these hours cannot be "
                                      "carried into it.")
    return nxt["id"]


def _decide_rows(org_id: int, period: dict, rows: list, decision: str, user_id: int, note) -> dict:
    """Stores `decision` for each of these tie-out rows that it fits. Rows already applied are left alone. A row it does not fit
    (next period / manual check on a line where Beacon has FEWER hours than were paid) is skipped and counted."""
    if decision not in pdec.DECISIONS:
        raise PayrollError("invalid", "That is not one of the three decisions.")
    note = pdec.clean_note(note)
    existing = pdec.for_period(period["id"])
    target = None
    n_set = n_skipped = 0
    first_reason = None
    with db.connect() as conn:
        with conn.cursor() as cur:
            for r in rows:
                key = (r["staff_id"], r["category_id"])
                if existing.get(key) and existing[key]["applied_at"]:
                    n_skipped += 1
                    first_reason = first_reason or "A decision that was already applied cannot be changed."
                    continue
                if decision != pdec.MANUAL_CHECK and r.get("register_hours") is not None and r["register_hours"] < 0:
                    n_skipped += 1
                    first_reason = first_reason or ("The paid hours for this line are negative (a voided check). Beacon never takes a "
                                                    "negative figure, so it can only be settled as a Manual check / void-reissue.")
                    continue
                why = pdec.valid_for(decision, r["difference"])
                if why:
                    n_skipped += 1
                    first_reason = first_reason or why
                    continue
                if target is None and decision != pdec.BEACON_WRONG:
                    target = _target_for(org_id, period, decision)
                cur.execute(
                    "INSERT INTO portal.time_variance_decisions (period_id, staff_id, category_id, decision, hours, note, "
                    "target_period_id, decided_by_user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (period_id, staff_id, category_id) DO UPDATE SET decision = EXCLUDED.decision, "
                    "hours = EXCLUDED.hours, note = EXCLUDED.note, target_period_id = EXCLUDED.target_period_id, "
                    "decided_by_user_id = EXCLUDED.decided_by_user_id, decided_at = now(), applied_at = NULL, "
                    "applied_by_user_id = NULL",
                    (period["id"], r["staff_id"], r["category_id"], decision, pdec.d2(r["difference"]), note,
                     None if decision == pdec.BEACON_WRONG else target, user_id))
                n_set += 1
        conn.commit()
    return {"set": n_set, "skipped": n_skipped, "reason": first_reason}


def _differing_rows(org_id: int, period_id: int):
    period = pt.get_period(org_id, period_id)
    _need_closed_unactualized(period)
    rep = pt.variance(org_id, period_id)
    return period, [r for r in rep["rows"] if r["kind"] in OPEN_KINDS]


def set_decision(org_id: int, period_id: int, staff_id: int, category_id: int, decision: str, user_id: int, note=None) -> dict:
    """One decision for one line (an employee and a category) that differs from the paid hours."""
    period, rows = _differing_rows(org_id, period_id)
    mine = [r for r in rows if r["staff_id"] == staff_id and r["category_id"] == category_id]
    if not mine:
        raise PayrollError("not_found", "That line no longer differs from the paid hours.")
    res = _decide_rows(org_id, period, mine, decision, user_id, note)
    if not res["set"]:
        raise PayrollError("refused", res["reason"] or "Nothing was changed.")
    return res


def set_employee_decision(org_id: int, period_id: int, staff_id: int, decision: str, user_id: int, note=None) -> dict:
    """The same decision for every differing line of one employee where it fits; the others are left as they were."""
    period, rows = _differing_rows(org_id, period_id)
    mine = [r for r in rows if r["staff_id"] == staff_id]
    if not mine:
        raise PayrollError("not_found", "That person has no line that differs from the paid hours.")
    res = _decide_rows(org_id, period, mine, decision, user_id, note)
    if not res["set"]:
        raise PayrollError("refused", res["reason"] or "Nothing was changed.")
    return res


def decide_all_remaining(org_id: int, period_id: int, decision: str, user_id: int, note=None) -> dict:
    """The same decision for every differing line that has none yet (or whose decision went stale)."""
    period, rows = _differing_rows(org_id, period_id)
    todo = [r for r in rows if r["decision"] is None or r["decision_stale"]]
    if not todo:
        return {"set": 0, "skipped": 0, "reason": None}
    return _decide_rows(org_id, period, todo, decision, user_id, note)


def clear_decision(org_id: int, period_id: int, staff_id: int, category_id: int) -> dict:
    """Takes a decision back (back to "needs a decision"). Not once Actualize has applied it."""
    period = pt.get_period(org_id, period_id)
    _need_closed_unactualized(period)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM portal.time_variance_decisions WHERE period_id = %s AND staff_id = %s AND category_id = %s "
                        "AND applied_at IS NULL", (period_id, staff_id, category_id))
            n = cur.rowcount
        conn.commit()
    return {"cleared": n}


# ------------------------------------------------------------------ actualize

def _carry_into(cur, target_id: int, staff_id: int, category_id: int, hours: Decimal, actor, from_label: str) -> None:
    """Adds `hours` (paid in the following pay period) to this person's line in the same category of that period, so it shows
    in Time Review and on the Checkwriters Excel to be keyed. A line that does not exist yet is created, confirmed, source
    diocese. Every change is in the edit history. An emailed figure arriving later is stored on top (payroll_totals)."""
    note = f"Carried in from {from_label}: {hours} hours to be paid in this pay period"
    cur.execute("SELECT id, hours, review_status, proposed_hours FROM portal.time_period_totals "
                "WHERE period_id = %s AND staff_id = %s AND category_id = %s FOR UPDATE", (target_id, staff_id, category_id))
    line = cur.fetchone()
    if line is None:
        cur.execute(
            "INSERT INTO portal.time_period_totals (period_id, staff_id, category_id, hours, source, source_ref, source_quote, "
            "review_status, flags, reviewed_by_user_id, reviewed_at, created_by_user_id) "
            "VALUES (%s,%s,%s,%s,'diocese',%s,%s,'confirmed',ARRAY['carried_in'],%s,now(),%s) RETURNING id",
            (target_id, staff_id, category_id, hours, f"Carried from {from_label}", note[:300], actor, actor))
        pt._log_edit(cur, cur.fetchone()["id"], None, hours, None, "confirmed", actor, "diocese", note)
    else:
        old = Decimal(line["hours"])
        rejected = line["review_status"] == "rejected"
        new = hours if rejected else old + hours  # a rejected figure is not wanted: the carried hours stand alone
        if new > pt.MAX_HOURS:
            raise PayrollError("refused", f"{new} hours would be more than the 300 hours one line may hold in the next pay period.")
        proposed = line["proposed_hours"]
        new_prop = None if rejected or proposed is None else Decimal(proposed) + hours  # an open proposal keeps the carried hours too
        cur.execute(
            "UPDATE portal.time_period_totals SET hours = %s, proposed_hours = %s, review_status = %s, "
            "reviewed_by_user_id = CASE WHEN %s THEN %s ELSE reviewed_by_user_id END, "
            "reviewed_at = CASE WHEN %s THEN now() ELSE reviewed_at END, "
            "flags = (SELECT COALESCE(array_agg(DISTINCT f), '{}') FROM unnest(flags || ARRAY['carried_in']) f), "
            "updated_at = now() WHERE id = %s",
            (new, new_prop, "confirmed" if rejected else line["review_status"], rejected, actor, rejected, line["id"]))
        pt._log_edit(cur, line["id"], old, new, line["review_status"], "confirmed" if rejected else line["review_status"],
                     actor, "diocese", note)
    pt._unfinalize(cur, target_id)


def actualize_period(org_id: int, period_id: int, user_id: int | None, auto: bool = False) -> dict:
    """Settles every differing line of a period whose hours are Final (or that is Closed), hours only, each by its decision (payroll_decisions.py):
      beacon_wrong   the line takes the paid hours (confirmed, source register); the old figure stays in the edit history
                     (paid, no Beacon hours: the person gets a line; Beacon hours not paid: the hours become 0 with a note)
      next_period    the same, and the extra hours are added to the next period's line to be keyed there
      manual_check   the line STAYS as entered (the interpay check shows on the next period's register); a note is left
    Every differing line needs a decision first, unless the diocese's automatic switch is doing this: then a line nobody
    decided is treated as beacon_wrong. A line the parish's daily grid also covers is marked 'resolved_total' so the paid figure
    is the one that counts. When nothing differs afterwards, and every paid person was found on the roster, the period is
    stamped Actualized."""
    period = pt.get_period(org_id, period_id)
    need_final_or_closed(period, "make Beacon match the paid hours")
    if period["register_loaded_at"] is None:
        raise PayrollError("refused", "The paid hours have not been loaded for this period yet.")
    actor = user_id if user_id is not None else pt.service_user_id()
    rep = pt.variance(org_id, period_id)
    todo = [r for r in rep["rows"] if r["kind"] in OPEN_KINDS]
    # a person whose TOTAL hours tie to the paid total keeps their lines as entered; only a note is left for review
    review = [r for r in rep["rows"] if r["kind"] == "category_only"]
    decs = pdec.for_period(period_id)

    def dec_of(r):
        d = decs.get((r["staff_id"], r["category_id"]))
        return d if d and not pdec.is_stale(d, r["difference"]) else None

    if not auto:
        waiting = [r for r in todo if dec_of(r) is None]
        if waiting:
            raise PayrollError("refused", f"{len(waiting)} line(s) still need a decision: Include in next period, Manual check / "
                                          "void-reissue or Beacon wrong. Settle them on the tie-out page first.")
    targets = {dec_of(r)["target_period_id"] for r in todo
               if dec_of(r) and dec_of(r)["decision"] in (pdec.NEXT_PERIOD, pdec.MANUAL_CHECK)}
    for tid in targets:
        t = db.query_one("SELECT label, status FROM portal.payroll_periods WHERE id = %s AND org_id = %s", (tid, org_id))
        if not t or t["status"] == "closed":
            raise PayrollError("refused", f"The next pay period ({t['label'] if t else 'unknown'}) is closed, so hours cannot "
                                          "be carried into it. Change those decisions first.")
    nxt = next_period(org_id, period)
    nxt_label = nxt["label"] if nxt else "the next pay period"
    ref = period["register_source_ref"] or f"LaborDist {period['pay_date'].strftime('%Y.%m.%d')}"
    n_diff = n_new = n_zero = n_noted = n_carried = n_manual = 0
    carried_hours = Decimal(0)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT staff_id, category_id FROM portal.time_entries WHERE period_id = %s "
                        "GROUP BY staff_id, category_id HAVING SUM(hours) > 0", (period_id,))
            grid = {(r["staff_id"], r["category_id"]) for r in cur.fetchall()}
            for r in todo:
                key = (r["staff_id"], r["category_id"])
                dec = dec_of(r)
                mode = dec["decision"] if dec else pdec.BEACON_WRONG
                extra = f" Note: {dec['note']}" if dec and dec["note"] else ""
                paid = Decimal(str(r["register_hours"])) if r["register_hours"] is not None else Decimal(0)
                if paid < 0 and mode != pdec.MANUAL_CHECK:
                    continue   # a negative paid figure (a voided check) is never taken into Beacon's own line: it stays differing
                flags = ["actualized"] + (["resolved_total"] if key in grid else [])
                cur.execute("SELECT id, hours, review_status FROM portal.time_period_totals "
                            "WHERE period_id = %s AND staff_id = %s AND category_id = %s FOR UPDATE",
                            (period_id, r["staff_id"], r["category_id"]))
                line = cur.fetchone()
                if mode == pdec.MANUAL_CHECK:
                    # the hours are right for THIS period: nothing changes except a note (when there is a line to put it on)
                    if line:
                        if pdec.d2(dec["hours"]) < 0:
                            note = (f"Voided and reissued: Beacon's {r['beacon_hours']} hours were right and stand for this pay period; "
                                    f"this register paid {paid} (more), and the void and reissue will show on {nxt_label}'s register "
                                    f"as {pdec.d2(dec['hours'])} hours ({ref}).{extra}")
                        else:
                            note = (f"Paid by an interpay manual check: Beacon's {r['beacon_hours']} hours stand for this pay period; "
                                    f"this register paid {paid}, and the check will show on {nxt_label}'s register ({ref}).{extra}")
                        pt._log_edit(cur, line["id"], line["hours"], line["hours"], line["review_status"], line["review_status"],
                                     actor, "register", note[:1000])
                    n_manual += 1
                    cur.execute("UPDATE portal.time_variance_decisions SET applied_at = now(), applied_by_user_id = %s "
                                "WHERE id = %s", (actor, dec["id"]))
                    continue
                if r["kind"] == "beacon_hours_not_paid":
                    note = f"Actualized: Beacon had {r['beacon_hours']} hours, nothing was paid ({ref})"
                    n_zero += 1
                elif r["kind"] == "paid_no_beacon_hours":
                    note = f"Actualized: paid {paid} hours, Beacon had none counted ({ref})"
                    n_new += 1
                else:
                    note = f"Actualized: Beacon had {r['beacon_hours']} hours, paid {paid} ({ref})"
                    n_diff += 1
                if mode == pdec.NEXT_PERIOD:
                    note += f". The {pdec.d2(dec['hours'])} extra hours are paid in {nxt_label} and carried there"
                note += extra
                if line:
                    cur.execute(
                        "UPDATE portal.time_period_totals SET hours = %s, source = 'register', review_status = 'confirmed', "
                        "proposed_hours = NULL, proposed_source_ref = NULL, proposed_quote = NULL, "
                        "flags = (SELECT COALESCE(array_agg(DISTINCT f), '{}') FROM unnest(flags || %s::text[]) f), "
                        "reviewed_by_user_id = %s, reviewed_at = now(), updated_at = now() WHERE id = %s",
                        (paid, flags, actor, line["id"]))
                    pt._log_edit(cur, line["id"], line["hours"], paid, line["review_status"], "confirmed", actor,
                                 "register", note[:1000])
                else:
                    cur.execute(
                        "INSERT INTO portal.time_period_totals (period_id, staff_id, category_id, hours, source, source_ref, "
                        "review_status, flags, reviewed_by_user_id, reviewed_at, created_by_user_id) "
                        "VALUES (%s,%s,%s,%s,'register',%s,'confirmed',%s,%s,now(),%s) RETURNING id",
                        (period_id, r["staff_id"], r["category_id"], paid, ref, flags, actor, actor))
                    pt._log_edit(cur, cur.fetchone()["id"], None, paid, None, "confirmed", actor, "register", note[:1000])
                if mode == pdec.NEXT_PERIOD:
                    carry = pdec.d2(dec["hours"])
                    _carry_into(cur, dec["target_period_id"], r["staff_id"], r["category_id"], carry, actor, period["label"])
                    n_carried += 1
                    carried_hours += carry
                if dec:
                    cur.execute("UPDATE portal.time_variance_decisions SET applied_at = now(), applied_by_user_id = %s "
                                "WHERE id = %s", (actor, dec["id"]))
            for r in review:
                if r["beacon_hours"] is None:
                    continue  # nothing of Beacon's in this category to put a note on
                cur.execute("SELECT id, hours, review_status FROM portal.time_period_totals "
                            "WHERE period_id = %s AND staff_id = %s AND category_id = %s",
                            (period_id, r["staff_id"], r["category_id"]))
                line = cur.fetchone()
                if not line:
                    continue  # the hours came only from the parish's daily grid: there is no line to note on
                paid_here = "none" if r["register_hours"] is None else f"{r['register_hours']:g}"
                note = (f"For review: the paid hours add up to the same total ({r['person_register_total']:g}) but the categories "
                        f"differ. Beacon kept {r['category_label']} {r['beacon_hours']:g} as entered; paid {paid_here} in this "
                        f"category ({ref})")
                cur.execute("SELECT 1 FROM portal.time_period_total_edits WHERE total_id = %s AND note = %s", (line["id"], note))
                if cur.fetchone():
                    continue  # already noted by an earlier press of the button
                pt._log_edit(cur, line["id"], line["hours"], line["hours"], line["review_status"], line["review_status"],
                             actor, "register", note)
                n_noted += 1
            conn.commit()
    after = pt.variance(org_id, period_id)
    left = sum(1 for r in after["rows"] if r["kind"] in OPEN_KINDS)
    tied = left == 0 and not period["register_unmatched"]
    if tied:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE portal.payroll_periods SET actualized_at = now(), actualized_by_user_id = %s, "
                            "actualized_automatically = %s, updated_at = now() WHERE id = %s", (actor, bool(auto), period_id))
                conn.commit()
    return {"changed": n_diff, "added": n_new, "zeroed": n_zero, "left": left, "unmatched": period["register_unmatched"],
            "tied": tied, "automatic": bool(auto), "for_review": after.get("category_only_people", 0), "noted": n_noted,
            "carried": n_carried, "carried_hours": float(carried_hours), "manual": n_manual}


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
    roster_departures.apply_due_departures(org_id)   # the roster the checklist reads is current: a last day that has passed is inactive
    rep = pt.variance(org_id, period_id) if period["register_loaded_at"] is not None else None
    who = None
    if period["actualized_by_user_id"]:
        u = db.query_one("SELECT display_name, email FROM checkreq.app_users WHERE id = %s", (period["actualized_by_user_id"],))
        who = (u["display_name"] or u["email"]) if u else None
    loaded_by = None
    if period.get("register_loaded_by_user_id"):
        u = db.query_one("SELECT display_name, email FROM checkreq.app_users WHERE id = %s", (period["register_loaded_by_user_id"],))
        loaded_by = (u["display_name"] or u["email"]) if u else None
    filed_by = None
    if period.get("reports_filed_by_user_id"):
        u = db.query_one("SELECT display_name, email FROM checkreq.app_users WHERE id = %s", (period["reports_filed_by_user_id"],))
        filed_by = (u["display_name"] or u["email"]) if u else None
    return {"period": period, "final": period["hours_finalized_at"] is not None, "closed": period["status"] == "closed",
            "settle_ready": pt.settle_ready(period),   # Final or Closed: the tie-out can be settled and the reports filed
            "register_loaded": period["register_loaded_at"] is not None,
            "register_lines": db.query_one("SELECT count(*) AS n FROM portal.time_register_hours WHERE period_id = %s",
                                           (period_id,))["n"],
            "variance": rep, "actualized": period["actualized_at"] is not None, "actualized_by": who,
            "auto": auto_actualize_enabled(org_id), "next": next_period(org_id, period),
            # step 5: who marked the department reports as filed, and how (the Phoenix workbook or another way)
            "reports_filed": period.get("reports_filed_at") is not None, "reports_filed_by": filed_by, "register_loaded_by": loaded_by,
            # what the PREVIOUS period's decisions mean here: hours carried in (to key) and manual checks (already paid, do not key)
            "prior": pdec.prior_period_notes(period_id)}


REPORTS_FILED_HOW = ("phoenix", "beacon", "other")


def mark_reports_filed(org_id: int, period_id: int, user_id: int, how: str, note: str | None = None) -> dict:
    """Step 5 of the checklist: someone says the department payroll reports are filed in the Documents folders, either by the
    Phoenix workbook's "File Reports to Beacon Documents" button ('phoenix'), by Beacon itself ("File Reports Now Here",
    payroll_report_filing.py, 'beacon') or another way. This only records who and when, and a short note of what was filed."""
    if how not in REPORTS_FILED_HOW:
        raise PayrollError("invalid", "Say how the reports were filed: in the Phoenix workbook or another way.")
    period = pt.get_period(org_id, period_id)
    if not pt.settle_ready(period):
        raise PayrollError("refused", "Mark the hours Final first. The reports are filed after payroll has been processed.")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE portal.payroll_periods SET reports_filed_at = now(), reports_filed_by_user_id = %s, "
                        "reports_filed_how = %s, reports_filed_note = %s, updated_at = now() WHERE id = %s AND org_id = %s",
                        (user_id, how, (note or None) and note[:500], period_id, org_id))
        conn.commit()
    return {"how": how}


def clear_reports_filed(org_id: int, period_id: int) -> None:
    pt.get_period(org_id, period_id)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE portal.payroll_periods SET reports_filed_at = NULL, reports_filed_by_user_id = NULL, "
                        "reports_filed_how = NULL, reports_filed_note = NULL, updated_at = now() WHERE id = %s AND org_id = %s",
                        (period_id, org_id))
        conn.commit()


# ------------------------------------------------------------------ closing a period and finishing it from the Phoenix workbook (Jay, 2026-10-22)

def close_period(org_id: int, period_id: int, user_id: int) -> dict:
    """Closes an OPEN pay period (hours stop landing in it) and records who and when. Closed already: nothing changes."""
    period = pt.get_period(org_id, period_id)
    if period["status"] == "closed":
        return {"already": True}
    if period["status"] != "open":
        raise PayrollError("refused", "Only an open pay period can be closed here.")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE portal.payroll_periods SET status = 'closed', processed_by_user_id = %s, processed_at = now(), "
                        "updated_at = now() WHERE id = %s AND org_id = %s", (user_id, period_id, org_id))
        conn.commit()
    return {"already": False}


def close_and_open_next(org_id: int, period_id: int, user_id: int) -> dict:
    """Step 6 and step 7 in one press (Jay, 2026-10-27): close the period, then open the next one. A next period that cannot be opened
    (none set up, another one still open) never undoes the close: the answer says why, in next["message"]."""
    res = close_period(org_id, period_id, user_id)
    nxt = None
    try:
        r = open_next_period(org_id, period_id, user_id)
        nxt = {"label": r["label"], "opened": not r["already"], "message": None}
    except PayrollError as e:
        nxt = {"label": None, "opened": False, "message": e.message}
    return {"already": res["already"], "next": nxt}


def final_blockers(org_id: int, period_id: int) -> list[str]:
    """Why the hours cannot be marked Final yet (the same rules as payroll_totals.finalize_period), as plain sentences."""
    out = []
    left = pt.unresolved(pt._period_rows(org_id, period_id))
    unentered = sum(pt.count_unentered(org_id, period_id).values())
    if left:
        out.append(f"{len(left)} line(s) still need a decision on Time Review")
    if unentered:
        out.append(f"{unentered} person(s) named in emails were not entered")
    import payroll_late_rules as late_rules          # the same rule as payroll_totals.finalize_period: held emails block Final
    late = late_rules.waiting_count(org_id)
    if late:
        out.append(f"{late} late email(s) are waiting in the Late emails queue")
    notdone = pt.parishes_not_done(org_id, period_id)
    if notdone:
        codes = ", ".join(p["code"] or p["name"] for p in notdone[:8]) + (" and more" if len(notdone) > 8 else "")
        out.append(f"{len(notdone)} department(s) are not approved yet ({codes})")
    return out


def finish_blockers(org_id: int, period: dict, rep: dict | None = None) -> dict:
    """Can this period be finished in one step (Final, Close, Actualize)? Only when the hours tie out PERFECTLY: nothing differs, every
    paid person is on the roster, and every department is approved. People whose total hours tie but whose categories differ do not
    block (they are left as entered and noted for review). Returns {ready, already, blockers}."""
    pid = period["id"]
    if period["hours_finalized_at"] and period["status"] == "closed" and period["actualized_at"]:
        return {"ready": False, "already": True, "blockers": []}
    blockers = []
    if period["status"] == "future":
        blockers.append("This pay period has not started")
    if period["register_loaded_at"] is None:
        blockers.append("The paid hours have not been loaded yet")
    else:
        rep = rep or pt.variance(period["org_id"], pid)
        n = sum(1 for r in rep["rows"] if r["kind"] in OPEN_KINDS)
        if n:
            blockers.append(f"{n} line(s) differ from the paid hours")
        if period["register_unmatched"]:
            blockers.append(f"{period['register_unmatched']} paid person(s) are not on the roster")
    if not period["hours_finalized_at"] and period["status"] in ("open", "closed"):
        blockers.extend(final_blockers(period["org_id"], pid))
    return {"ready": not blockers, "already": False, "blockers": blockers}


def finish_period(org_id: int, period_id: int, user_id: int, open_next: bool = False) -> dict:
    """Marks the hours Final, records the period as Actualized, closes it and (when asked) opens the next period, in that order (Jay,
    2026-10-27: Close is the next-to-last step), but ONLY when finish_blockers() is empty. Each step is the normal one, stamped with this
    person, and safe to repeat. If Actualize cannot finish, the hours stay Final and the period is left OPEN. Returns what was done."""
    period = pt.get_period(org_id, period_id)
    fb = finish_blockers(org_id, period)
    if fb["already"]:
        return {"done": [], "already": True, "next": None}
    if fb["blockers"]:
        raise PayrollError("refused", "Not ready to finish: " + "; ".join(fb["blockers"]) + ". Nothing was changed.")
    done = []
    if not period["hours_finalized_at"]:
        pt.finalize_period(org_id, period_id, user_id)
        done.append("Marked the hours Final")
    try:
        res = actualize_period(org_id, period_id, user_id)
    except PayrollError as e:
        raise PayrollError("refused", f"The hours were marked Final, but the period could not be Actualized: {e.message} "
                                      "The period was left open. Open the close-out page in Beacon.")
    if not res["tied"]:
        raise PayrollError("refused", "The hours were marked Final, but Actualize found something that no longer "
                                      f"ties out ({res['left']} line(s) differ). The period was left open. Open the close-out page in Beacon.")
    done.append("Recorded the period as Actualized")
    if period["status"] != "closed":
        close_period(org_id, period_id, user_id)
        done.append("Closed the pay period")
    nxt = None
    if open_next:
        try:
            r = open_next_period(org_id, period_id, user_id)
            nxt = {"label": r["label"], "opened": not r["already"], "message": None}
            done.append(f"{r['label']} was already open" if r["already"] else f"Opened {r['label']}")
        except PayrollError as e:
            nxt = {"label": None, "opened": False, "message": e.message}
    return {"done": done, "already": False, "next": nxt, "for_review": res.get("for_review", 0)}


def tie_out_report(org_id: int, period_id: int) -> dict:
    """Everything the Phoenix workbook's Beacon Tie-Out tab shows for one pay period, hours only: the period's state, counts, each
    department's Beacon and paid hours, each line that is not a plain match (with its decision), and whether the period can be finished
    in one step. Read-only."""
    period = pt.get_period(org_id, period_id)
    loaded = period["register_loaded_at"] is not None
    rep = pt.variance(org_id, period_id) if loaded else None
    fin = finish_blockers(org_id, period, rep)
    # paid lines for people Beacon's roster does not mark as reporting hours are not compared (say so, so the totals make sense)
    nc = db.query_one("SELECT count(*) AS n, COALESCE(sum(rh.hours), 0) AS h FROM portal.time_register_hours rh "
                      "JOIN portal.staff_roster sr ON sr.id = rh.staff_id JOIN portal.parishes p ON p.id = sr.parish_id "
                      "WHERE rh.period_id = %s AND p.org_id = %s AND NOT sr.captures_hours", (period_id, org_id))
    out = {"not_compared": {"lines": int(nc["n"]), "hours": float(nc["h"])}, "period": {"id": period["id"], "label": period["label"], "pay_date": period["pay_date"].isoformat() if period["pay_date"] else None,
                      "period_start": period["period_start"].isoformat(), "period_end": period["period_end"].isoformat(),
                      "status": period["status"], "final": period["hours_finalized_at"] is not None,
                      "closed": period["status"] == "closed", "actualized": period["actualized_at"] is not None,
                      "register_loaded": loaded, "reports_filed": period.get("reports_filed_at") is not None},
           "finish": fin, "register_loaded": loaded}
    if not loaded:
        out.update(counts={}, undecided=0, decided=0, departments=[], lines=[], manual_missing=[], totals={"beacon": 0.0, "paid": 0.0})
        return out
    lines = []
    for r in rep["rows"]:
        if r["kind"] == "match":
            continue
        lines.append({"dept_code": r["parish_code"], "dept_name": r["dept_name"], "employee_number": r["employee_number"],
                      "name": f"{r['last_name']}, {r['first_name']}", "category": r["category_label"],
                      "beacon_hours": r["beacon_hours"], "paid_hours": r["register_hours"], "difference": r["difference"],
                      "kind": r["kind"], "decision": pdec.LABELS.get(r["decision"]) if r["decision"] else None,
                      "decision_stale": r["decision_stale"], "decision_note": r["decision_note"],
                      "manual_check_hours": r["manual_check_hours"], "manual_open_hours": r["manual_open_hours"]})
    out.update(counts=rep["counts"], undecided=rep["undecided"], decided=rep["decided"], category_only_people=rep["category_only_people"],
               departments=[{"code": d["parish_code"], "name": d["dept_name"], "beacon_hours": d["beacon_hours"],
                             "paid_hours": d["register_hours"], "difference": d["difference"], "differing": d["differing"]}
                            for d in rep["departments"]],
               lines=lines, manual_missing=rep["manual_missing"],
               totals={"beacon": round(sum(d["beacon_hours"] for d in rep["departments"]), 2),
                       "paid": round(sum(d["register_hours"] for d in rep["departments"]), 2)})
    return out
