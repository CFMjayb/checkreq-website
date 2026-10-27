"""
roster_departures.py -- a termination that takes effect AFTER the last day (26-158 / 26-129, 2026-10-27; Jay: "some terminations
occur after the current pay period").

THE RULE. A request to make someone inactive carries a LAST DAY (portal.staff_roster_changes.as_of_date). When an HR Admin
approves it:
  - last day in the past (or none)       -> the person is switched off at once, exactly as before;
  - last day TODAY OR LATER (the diocese's own "today", org_time.py: the entity's time zone, not the server's
    UTC) -> the request is approved and recorded, the person STAYS ACTIVE and keeps getting hours, and becomes inactive
    automatically once the last day has passed.

NO SCHEMA CHANGE. The meaning of the two existing columns on portal.staff_roster is:
  is_active TRUE,  inactive_as_of NULL      -> an ordinary active person;
  is_active TRUE,  inactive_as_of = a date  -> LEAVING: still active, last day = that date (new in this change; until now the
                                               date was only ever set together with is_active FALSE);
  is_active FALSE, inactive_as_of = a date  -> inactive; the date is the last day (unchanged);
  is_active FALSE, inactive_as_of NULL      -> inactive, no date (unchanged).
Every reader of is_active therefore keeps treating a leaving person as active, which is what is wanted (they keep their row in
the time-entry grid, on Time Review, in the payroll reader's roster and in the Std hrs / Confirm standing hours rules). The only
readers of inactive_as_of itself are the four listed in the 2026-10-27 report: payroll_totals.get_roster (passes it through to the
service API; the payroll email reader ignores it), timekeeping_employees (marks "Leaving"), timekeeping_roster_changes (writes
it) and logic/hr_lifecycle/roster_link (the HR offboarding case closes a person at once with the separation date).

THE PAY-PERIOD RULE (Jay, 2026-10-27: "wait until the pay period closes"). A person with a last day stays on -- still active, still
getting hours -- until the last day has PASSED and the pay period that CONTAINS the last day is CLOSED (portal.payroll_periods.status
= 'closed'; 'open' and 'future', and any other value, wait). Hours for that last pay period can still come in until then. A last
day that no pay period holds (an entity without pay periods, or a date outside them) is judged by the day alone, as it always was.
keep_until() is the one place that decides this: the Roster Review approval, the HR offboarding case and the sweep all ask it.

THE SWITCH-OFF. apply_due_departures() is idempotent and cheap (one small SELECT when nobody is leaving). It is called lazily at
the top of the roster-reading paths (see the call sites in the report; there is no scheduled job, and a scheduler could call it
later). It switches off a person whose last day has passed (last day < today in the diocese's zone) and whose pay period is
closed. It writes NO history row of its own: the approved 'deactivate' row of the request (or HR case) that set the date is already
the dated history, and a second row made the parish's Proposed changes list show one termination twice (Jay, 2026-10-27: "show as
one").

SAFETY GUARD. A person is switched off only when the staff row's date is backed by an approved 'deactivate' history row with the
same date. That protects an active person who happens to carry a stale inactive_as_of from an older script (none on dev; the
approve / Employees / HR-offboarding doors all write that history row): a wrongly deactivated payroll person is far worse than a
leaving mark that never switches off, which is visible on the Employees screen and can be cancelled there.

FAIL OPEN. A read path must never break because this sweep had a problem: apply_due_departures() catches, prints the error type
(never a name or number) and returns 0.

Depends only on db.py and org_time.py. (The zone rule is the same as logic/hr_lifecycle/clock.today(), written out here so nothing from
logic/* is needed: this module is read by the roster screens, which must load wherever those screens load.)
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import db
import org_time


def fmt(d) -> str:
    """MM/DD/YY, the date style every roster screen uses."""
    return d.strftime("%m/%d/%y") if d else ""


def today_for_org(org_id: int | None) -> dt.date:
    """Today in the diocese's own time zone (its zone setting, the default zone when none is set)."""
    try:
        zone = ZoneInfo(org_time.zone_name_for_org(org_id))
    except Exception:
        zone = ZoneInfo(org_time.DEFAULT_ZONE)
    return dt.datetime.now(zone).date()


def org_of_parish(parish_id: int) -> int | None:
    row = db.query_one("SELECT org_id FROM portal.parishes WHERE id = %s", (parish_id,))
    return row["org_id"] if row else None


def is_leaving(staff: dict | None) -> bool:
    """True for an ACTIVE person who carries a last day (approved, not yet passed or not yet swept)."""
    return bool(staff) and bool(staff.get("is_active")) and staff.get("inactive_as_of") is not None


def period_not_closed(org_id: int | None, last_day) -> bool:
    """True when a pay period of this entity CONTAINS the last day and is not closed yet (status open, future or anything but
    'closed': the safe direction, the person simply waits). False when no pay period holds that day."""
    if org_id is None or last_day is None:
        return False
    if isinstance(last_day, dt.datetime):
        last_day = last_day.date()
    row = db.query_one(
        "SELECT bool_or(status <> 'closed') AS waiting FROM portal.payroll_periods "
        "WHERE org_id = %s AND period_start <= %s AND period_end >= %s", (org_id, last_day, last_day))
    return bool(row and row["waiting"])


def keep_until(org_id: int | None, as_of, today: dt.date | None = None) -> dt.date | None:
    """The last day when the person must STAY on the roster ("leaving"), else None (switch them off at once, as before).
    Stay when the last day is today or later (the diocese's own today), or when it has passed but the pay period that holds it is
    not closed yet. today overrides the clock (tests)."""
    if as_of is None:
        return None
    if isinstance(as_of, dt.datetime):
        as_of = as_of.date()
    if today is None:
        today = today_for_org(org_id)
    if as_of >= today:
        return as_of
    return as_of if period_not_closed(org_id, as_of) else None


def apply_due_departures(org_id: int | None = None, *, parish_id: int | None = None, today: dt.date | None = None) -> int:
    """Switch off every person whose last day has passed. Returns how many were switched off. Idempotent.

    org_id / parish_id narrow the sweep (None for both = every diocese, each against its own "today"). today overrides the
    clock for every diocese (tests; a scheduler never passes it)."""
    try:
        return _sweep(org_id, parish_id, today)
    except Exception as exc:  # noqa: BLE001 - a read path must never fail because of this
        print(f"[roster_departures] sweep failed: {type(exc).__name__}")
        return 0


def _sweep(org_id, parish_id, today) -> int:
    where, params = [], []
    if org_id is not None:
        where.append("p.org_id = %s")
        params.append(org_id)
    if parish_id is not None:
        where.append("s.parish_id = %s")
        params.append(parish_id)
    rows = db.query(
        "SELECT s.id, s.inactive_as_of, p.org_id FROM portal.staff_roster s JOIN portal.parishes p ON p.id = s.parish_id "
        "WHERE s.is_active AND s.inactive_as_of IS NOT NULL" + "".join(" AND " + w for w in where), tuple(params))
    if not rows:
        return 0
    cutoff: dict = {}
    switched = 0
    for r in rows:
        if today is not None:
            day = today
        else:
            if r["org_id"] not in cutoff:
                cutoff[r["org_id"]] = today_for_org(r["org_id"])
            day = cutoff[r["org_id"]]
        if r["inactive_as_of"] >= day:
            continue                                    # the last day is today or later: still working
        if period_not_closed(r["org_id"], r["inactive_as_of"]):
            continue                                    # the pay period that holds the last day is not closed: hours may still come in
        with db.connect() as conn:
            with conn.cursor() as cur:
                # the guard in the module docstring, and the conditions again so two requests at once switch off once. No history
                # row is written: the approved request (or HR case) that set the date is the history.
                cur.execute(
                    "UPDATE portal.staff_roster s SET is_active = FALSE, updated_at = now() "
                    "WHERE s.id = %s AND s.is_active AND s.inactive_as_of IS NOT NULL AND s.inactive_as_of < %s "
                    "AND EXISTS (SELECT 1 FROM portal.staff_roster_changes c WHERE c.staff_id = s.id "
                    "            AND c.change_type = 'deactivate' AND c.status = 'approved' AND c.as_of_date = s.inactive_as_of) "
                    "RETURNING s.id",
                    (r["id"], day))
                if cur.fetchone():
                    switched += 1
    return switched


def cancel_leaving(cur, staff_id: int, parish_id: int) -> bool:
    """Take the leaving date off an ACTIVE person (the caller's cursor and transaction). True when there was one to cancel.
    Does not touch an inactive person: bringing one back is a reactivation, which clears the date itself."""
    cur.execute("UPDATE portal.staff_roster SET inactive_as_of = NULL, updated_at = now() "
                "WHERE id = %s AND parish_id = %s AND is_active AND inactive_as_of IS NOT NULL RETURNING id",
                (staff_id, parish_id))
    return cur.fetchone() is not None
