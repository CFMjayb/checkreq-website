"""
board.py -- the HR Status Board (Jay, 2026-10-22: "a status screen that shows the status of everything on one easy to
use screen"). Plan section 10.

One row per open case: person, entity / parish, type, the anchor date, the next deadline, who it is waiting on, whether it
has stalled, and one chip per section (Documents, Benefits, Payroll, Access, Orientation / records) with done of required
and a state in words as well as color (WCAG 1.4.1). A summary strip on top; every tile is a filter (HR Cloud's pattern).

The same function serves the screen and the tool beacon_hr_get_status_board.
"""
from __future__ import annotations

import datetime as dt

import db

from . import cases as cases_mod
from . import clock, dates, templates

FILTERS = {
    "all": "Open cases",
    "late": "Items past due",
    "deadline": "Hard deadlines in 10 days",
    "waiting_parish": "Parish requests waiting",
    "returned": "Returned to parishes",
    "stalled": "Stalled",
    "ready": "Ready to close",
}


def build(org_ids: list[int], today: dt.date | None = None, now: dt.datetime | None = None) -> dict:
    today = today or clock.today(org_ids[0] if org_ids else None)  # the entity's own date, not Cloud Run's UTC date
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = [dict(r) for r in db.query(
        "SELECT c.id, c.case_number, c.case_kind, c.status, c.first_name, c.last_name, c.preferred_name, c.position, "
        "       c.is_clergy, c.start_date, c.separation_date, c.benefits_effective_date, c.org_id, o.code AS org_code, "
        "       o.name AS org_name, c.parish_id, coalesce(p.short_name, p.name) AS parish_label, "
        "       coalesce(p.is_congregation, false) AS is_congregation, c.source, c.last_activity_at, c.opened_at, "
        "       coalesce(s.stalled_after_days, 5) AS stalled_after_days "
        "FROM hr.cases c JOIN checkreq.organizations o ON o.id = c.org_id "
        "LEFT JOIN portal.parishes p ON p.id = c.parish_id "
        "LEFT JOIN hr.entity_settings s ON s.org_id = c.org_id "
        "WHERE c.org_id = ANY(%s) AND c.status = ANY(%s) ORDER BY c.id",
        (list(org_ids), list(cases_mod.OPEN_STATUSES)))]
    ids = [r["id"] for r in rows]
    items_by_case: dict[int, list[dict]] = {i: [] for i in ids}
    if ids:
        for it in db.query("SELECT case_id, item_key, section, label, owner_group, required, status, due_date, "
                           "       hard_deadline_date FROM hr.case_items WHERE case_id = ANY(%s)", (ids,)):
            items_by_case[it["case_id"]].append(dict(it))
    summary = {k: 0 for k in FILTERS}
    out = []
    for r in rows:
        items = items_by_case.get(r["id"], [])
        counts = cases_mod.section_counts(items, today)
        open_items = [i for i in items if i["status"] == "open"]
        open_required = [i for i in open_items if i["required"]]  # an optional item never turns a row red
        late = [i for i in open_required if cases_mod.item_state(i, today) == "late"]
        hard_soon = [i for i in open_required if i["hard_deadline_date"]
                     and i["hard_deadline_date"] <= today + dt.timedelta(days=10)]
        # next deadline: the earliest hard deadline among open items, else the earliest due date
        nxt = None
        with_hard = sorted((i for i in open_items if i["hard_deadline_date"]), key=lambda i: i["hard_deadline_date"])
        if with_hard:
            nxt = {"label": with_hard[0]["label"], "date": with_hard[0]["hard_deadline_date"], "hard": True}
        else:
            with_due = sorted((i for i in open_items if i["due_date"]), key=lambda i: i["due_date"])
            if with_due:
                nxt = {"label": with_due[0]["label"], "date": with_due[0]["due_date"], "hard": False}
        if nxt:
            nxt["days"] = dates.days_until(nxt["date"], today)
        # waiting on: the owner groups of open required items, earliest-due first (who holds the case right now)
        waiting = []
        for i in sorted((i for i in open_items if i["required"]),
                        key=lambda i: (i["due_date"] or dt.date.max, i["owner_group"])):
            if i["owner_group"] not in waiting:
                waiting.append(i["owner_group"])
        if r["status"] == "returned":
            waiting = ["Parish"] + [w for w in waiting if w != "Parish"]
        last = r["last_activity_at"]
        stalled = bool(last and (now - last).days >= int(r["stalled_after_days"]))
        ready = not any(i["required"] and i["status"] == "open" for i in items)
        anchor = r["separation_date"] if r["case_kind"] in ("offboard", "benefits_end") else r["start_date"]
        flags = {
            "late": bool(late),
            "deadline": bool(hard_soon),
            "waiting_parish": r["status"] == "requested",
            "returned": r["status"] == "returned",
            "stalled": stalled and not ready,
            "ready": ready,
        }
        summary["all"] += 1
        summary["late"] += len(late)
        summary["deadline"] += len(hard_soon)
        for k in ("waiting_parish", "returned", "stalled", "ready"):
            summary[k] += 1 if flags[k] else 0
        out.append({
            "id": r["id"], "case_number": r["case_number"], "kind": r["case_kind"],
            "kind_label": templates.KINDS.get(r["case_kind"], r["case_kind"]), "status": r["status"],
            "name": f"{r['last_name']}, {r['preferred_name'] or r['first_name']}", "position": r["position"],
            "track": "Clergy" if r["is_clergy"] else "Lay", "org_code": r["org_code"], "org_id": r["org_id"],
            "unit": r["parish_label"] or r["org_name"], "anchor": anchor,
            "anchor_label": "last day" if r["case_kind"] in ("offboard", "benefits_end") else "hire",
            "next": nxt, "waiting_on": waiting[:3], "stalled": flags["stalled"], "ready": ready,
            "late_count": len(late), "sections": counts, "flags": flags,
            "sort_deadline": (nxt["date"] if nxt else dt.date.max),
        })
    out.sort(key=lambda x: (not x["flags"]["late"], x["sort_deadline"], x["name"]))
    return {"today": today, "summary": summary, "filters": FILTERS, "rows": out,
            "sections": cases_mod.BOARD_SECTIONS}


def by_owner(board: dict) -> list[dict]:
    """Regroup the board by who the work is waiting on (HR Cloud's assignee view): one row per owner group with the
    number of open cases waiting on it and how many of its items are late."""
    groups: dict[str, dict] = {}
    for r in board["rows"]:
        for w in r["waiting_on"][:1]:
            g = groups.setdefault(w, {"owner": w, "cases": 0, "late_cases": 0, "case_ids": []})
            g["cases"] += 1
            g["late_cases"] += 1 if r["flags"]["late"] else 0
            g["case_ids"].append(r["id"])
    return sorted(groups.values(), key=lambda g: (-g["late_cases"], -g["cases"], g["owner"]))
