"""
hr_hub_badges.py -- the red count on a tile of the HR & Payroll menu (26-129 / 26-158, 2026-10-25).

Jay: "Time Review and Roster Review need the red round number dot in the upper right-hand corner of the menu pill ... I think it's
really easy to miss what's in these screens that is new."

What each number means is the SAME number the screen itself works from, so the dot and the screen can never disagree:

    Time Review (/admin/timekeeping/status)    lines waiting for a decision in the open pay period, plus people an email named
                                               that nobody entered: the figure "Mark hours Final" waits on (payroll_screens.board_summary)
    Roster Review (/admin/timekeeping/review)  roster changes parishes and emails asked for that nobody has answered yet

A dot shows only when the number is above zero. Nothing here may ever break the menu: every count is computed on its own, and a
count that fails is simply left off the tile (the failure is printed for the logs).

(The dev copy also counts Onboarding & Offboarding cases (26-162). That tile does not exist in production yet, so the counter is
left out of this release and comes with 26-162.)
"""
from __future__ import annotations

import db

# the tile's url -> what the number counts: shown when the pointer rests on the dot
_TIPS = {
    "/admin/timekeeping/status": "Lines and emailed names waiting for a decision in the open pay period.",
    "/admin/timekeeping/review": "Roster changes waiting for you to approve or reject.",
}


def time_review(org_id: int) -> int:
    import timekeeping                      # loaded here: these modules import each other at import time
    import payroll_totals as pt
    period = timekeeping.get_current_open_period(org_id)
    if not period:
        return 0
    prows = pt.period_rows(org_id, period["id"])
    return len(pt.unresolved(prows)) + sum(pt.count_unentered(org_id, period["id"]).values())


def roster_review(org_id: int) -> int:
    row = db.query_one(
        "SELECT count(*) AS n FROM portal.staff_roster_changes src "
        "JOIN portal.payroll_periods pp ON pp.id = src.payroll_period_id "
        "WHERE pp.org_id = %s AND src.status = 'pending'", (org_id,))
    return int(row["n"]) if row else 0


_COUNTERS = {
    "/admin/timekeeping/status": time_review,
    "/admin/timekeeping/review": roster_review,
}


def with_badges(org_id: int | None, cards: list[dict]) -> list[dict]:
    """The same cards, each carrying card["badge"] (a whole number above zero) and card["badge_tip"] when it has something
    waiting. Works on the dicts it is given, so pass copies of any module-level card list."""
    if not org_id:
        return cards
    for c in cards:
        fn = _COUNTERS.get(c.get("url"))
        if not fn:
            continue
        try:
            n = int(fn(org_id))
        except Exception as exc:            # never let a count take the menu down
            print(f"[hr_hub_badges] {c.get('url')}: {type(exc).__name__}: {exc}")
            continue
        if n > 0:
            c["badge"] = n
            c["badge_tip"] = _TIPS[c["url"]]
    return cards
