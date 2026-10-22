"""
sustain_review.py -- the diocese list of the Parish Sustainability Review for diocesan staff (26-XXX Congregational Vitality, phase 4).

One row per parish, built from the same cleaned parochial-report rows (sustain.parish_year_v through sustain_data) and the same
projection the parish's own page uses (logic.sustain), so a number on the list is the number on the parish's page:
  * Sunday attendance: the latest in-person figure, the change since five filings earlier, and a small line of the last ten years
  * Deficit years: how many of the last five filings spent more than they took in (whole-form totals, as the page's chart does)
  * Reserves: cash plus unrestricted reserves in the latest filing, and how long they last with a priest at the test salary
  * Can it afford a priest at the guideline? affordable = no gap in the final year, or reserves that last 10 years or more
    (logic.sustain.projection.SAFE_RUNWAY_YEARS)

The diocese's pay guideline table is empty until Jay says which figures are official, so each parish's default salary is blank and
the column says so. Staff can type a test salary for the whole list; it replaces the guideline for every parish while it is set.

Pure functions: rows in, plain dicts out. No database and no web code.
"""
from __future__ import annotations

from logic.sustain.inputs import InputError, build_inputs
from logic.sustain.projection import ProjectionError, guideline_salary, project, reserves_summary
from sustain_defaults import friendly_error

WINDOW = 5                # filings looked at for deficit years and for the attendance change
STEADY = 0.05              # a change under 5% is called steady
SPARK_YEARS = 10
RUNWAY_NOT_NEEDED = 1e9    # sorts after every real runway

SORTS = ("name", "attendance", "change", "deficit", "reserves", "runway", "affordable")


def _f(x):
    return None if x is None else float(x)


def attendance_trend(years: list[dict]) -> dict:
    """{latest, year, then, then_year, change_pct, direction ('up'|'down'|'steady'|None), word, series [(year, value)]}.
    `years` is the parish's rows, oldest first. The comparison is the filing exactly WINDOW years before the latest one; when that
    year is missing or blank there is no change to show (never a guess from another year)."""
    by_year = {r["filing_year"]: _f(r.get("asa_in_person")) for r in years}
    last_year = years[-1]["filing_year"]
    latest = by_year.get(last_year)
    then_year = last_year - WINDOW
    then = by_year.get(then_year)
    change = None
    if latest is not None and then:
        change = (latest - then) / then
    if change is None:
        direction, word = None, ""
    elif abs(change) < STEADY:
        direction, word = "steady", "Steady"
    else:
        direction = "up" if change > 0 else "down"
        word = f"{'Up' if change > 0 else 'Down'} {abs(change) * 100:.0f}%"
    series = [(y, by_year[y]) for y in sorted(by_year) if y > last_year - SPARK_YEARS and by_year[y] is not None]
    return {"latest": latest, "year": last_year, "then": then, "then_year": then_year,
            "change_pct": None if change is None else change * 100.0, "direction": direction, "word": word, "series": series}


def deficit_years(years: list[dict]) -> dict:
    """How many of the last WINDOW filing years spent more than they took in, by the whole-form totals. {years, of, word}.
    A year missing either total is left out of both numbers."""
    last_year = years[-1]["filing_year"]
    counted = deficit = 0
    for r in years:
        if r["filing_year"] <= last_year - WINDOW:
            continue
        rev, exp = _f(r.get("total_revenues")), _f(r.get("total_expenses"))
        if rev is None or exp is None:
            continue
        counted += 1
        if exp > rev:
            deficit += 1
    return {"years": deficit, "of": counted, "word": f"{deficit} of {counted}" if counted else ""}


def priest_test(base: dict, defaults: dict, guidelines: list[dict], salary: float | None) -> dict:
    """{status, affordable, runway_kind, runway_years, reserves_today, salary, word, error}. status is 'ok', 'no_salary' (no guideline
    figure and no test salary), or 'error' (the inputs or base year cannot be projected; `error` says why in plain words)."""
    reserves_today = (_f(base.get("total_cash")) or 0.0) + (_f(base.get("unrestricted_reserves")) or 0.0)
    out = {"status": "ok", "affordable": None, "runway_kind": None, "runway_years": None, "reserves_today": reserves_today,
           "salary": None, "word": "", "error": ""}
    try:
        inputs = build_inputs(defaults, {"salary": salary} if salary is not None else {}, base,
                              default_salary=guideline_salary(base.get("asa_in_person"), guidelines))
    except InputError as exc:
        out["status"], out["error"], out["word"] = "error", friendly_error(str(exc)), "Cannot test"
        return out
    out["salary"] = inputs["salary"]
    if inputs["salary"] is None:
        out["status"], out["word"] = "no_salary", "No salary to test"
        return out
    try:
        rows = project(base, inputs)
    except ProjectionError as exc:
        out["status"], out["error"], out["word"] = "error", str(exc), "Cannot test"
        return out
    s = reserves_summary(base, rows, inputs)
    out["affordable"], out["runway_kind"], out["runway_years"] = s["affordable"], s["runway_kind"], s["runway_years"]
    out["word"] = "Yes" if s["affordable"] else "No"
    return out


def runway_text(t: dict) -> str:
    if t["status"] != "ok":
        return ""
    if t["runway_kind"] == "years":
        return f"{t['runway_years']:.1f} years"
    return "Reserves gone" if t["runway_kind"] == "none" else "Not needed"


def parish_row(parish: dict, defaults: dict, guidelines: list[dict], salary: float | None) -> dict:
    """One row. `parish` = {id, name, place, years (oldest first, or None/[] when no report is linked)}."""
    years = parish.get("years") or []
    row = {"id": parish["id"], "name": parish["name"], "place": parish.get("place", ""), "has_report": bool(years)}
    if not years:
        row.update(year=None, attendance=None, deficit=None, priest=None, runway_text="", runway_sort=None, reserves_today=None)
        return row
    base = years[-1]
    pt = priest_test(base, defaults, guidelines, salary)
    if pt["runway_kind"] == "years":
        rs = pt["runway_years"]
    elif pt["runway_kind"] == "none":
        rs = 0.0
    elif pt["runway_kind"] == "not_needed":
        rs = RUNWAY_NOT_NEEDED
    else:
        rs = None
    row.update(year=base["filing_year"], attendance=attendance_trend(years), deficit=deficit_years(years), priest=pt,
               runway_text=runway_text(pt), runway_sort=rs, reserves_today=pt["reserves_today"])
    return row


def _sort_value(row: dict, key: str):
    if not row["has_report"]:
        return None
    if key == "attendance":
        return row["attendance"]["latest"]
    if key == "change":
        return row["attendance"]["change_pct"]
    if key == "deficit":
        return row["deficit"]["years"] if row["deficit"]["of"] else None
    if key == "reserves":
        return row["reserves_today"]
    if key == "runway":
        return row["runway_sort"]
    if key == "affordable":
        a = row["priest"]["affordable"]
        return None if a is None else (1 if a else 0)
    return None


def build(parishes: list[dict], defaults: dict, guidelines: list[dict], salary: float | None = None,
          sort: str = "name", descending: bool = False) -> dict:
    """The whole list. Parishes with no value for the chosen column always come last; ties stay alphabetical.
    {rows, sort, descending, counts: {with_report, without, affordable, not_affordable, untested}, guideline_loaded, salary}."""
    if sort not in SORTS:
        sort = "name"
    rows = [parish_row(p, defaults, guidelines, salary) for p in parishes]
    alpha = sorted(rows, key=lambda r: r["name"].lower())
    if sort == "name":
        ordered = list(reversed(alpha)) if descending else alpha
    else:
        have = [r for r in alpha if _sort_value(r, sort) is not None]
        lack = [r for r in alpha if _sort_value(r, sort) is None]
        have.sort(key=lambda r: _sort_value(r, sort), reverse=descending)
        ordered = have + lack
    tested = [r for r in rows if r["has_report"] and r["priest"]["status"] == "ok"]
    counts = {"with_report": sum(1 for r in rows if r["has_report"]), "without": sum(1 for r in rows if not r["has_report"]),
              "affordable": sum(1 for r in tested if r["priest"]["affordable"]), "not_affordable": sum(1 for r in tested if not r["priest"]["affordable"]),
              "untested": sum(1 for r in rows if r["has_report"] and r["priest"]["status"] != "ok")}
    return {"rows": ordered, "sort": sort, "descending": descending, "counts": counts, "guideline_loaded": bool(guidelines), "salary": salary}
