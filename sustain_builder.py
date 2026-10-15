"""
sustain_builder.py -- turns a parish's parochial-report history into everything the Parish Sustainability Review page shows
(26-XXX Congregational Vitality). Used by the Parish Mode page (parish_sustain.py) and, later, the diocesan pages.

Split in two so each half can be tested without the other:
  * review_context(...)   the trends half: what stands out, the four ten-year charts, the header facts
  * calc_result(...)      the "Can we afford a priest?" half: validates the inputs, runs logic.sustain.projection, and returns
                          the answer sentence, the four tiles, the chart and the table. The page asks for it once on load and again
                          (as JSON) each time an input changes, so the numbers always come from this one function.
No database access here except through sustain_data; no web code.
"""
from __future__ import annotations

from logic.sustain.cpi import cpi_adjust
from logic.sustain.harmonize import PLEDGE_PRIVACY_MIN, average_pledge
from logic.sustain.inputs import InputError, build_inputs
from logic.sustain.narrative import answer_sentence, dollars
from logic.sustain.projection import ProjectionError, guideline_salary, project, reserves_summary
from logic.sustain.takeaways import trend_takeaways
import sustain_data
import sustain_viz as viz

OLD_FILING_YEARS = 2  # a latest filing this many years behind the newest anywhere gets a notice


def _series(name: str, rows: list[dict], key: str, style: int, transform=None) -> dict:
    values = {}
    for r in rows:
        v = r.get(key)
        values[r["filing_year"]] = None if v is None else (transform(r, v) if transform else float(v))
    return {"name": name, "values": values, "style": style}


def _year_span(rows: list[dict]) -> list[int]:
    first, last = rows[0]["filing_year"], rows[-1]["filing_year"]
    return list(range(first, last + 1))


def review_context(parish: dict) -> dict:
    """The data half of the page. {"state": "no_report"} when the parish has no parochial-report id or no rows; otherwise
    {"state": "ok", rows, latest, latest_year, as_of, takeaways, charts, cpi_note, base (for the calculator)}."""
    rows = sustain_data.parish_years(parish)
    if not rows:
        return {"state": "no_report"}
    latest = rows[-1]
    latest_year = latest["filing_year"]
    years = _year_span(rows)
    cpi = sustain_data.cpi_map()

    def real(row, v):
        adjusted = cpi_adjust(v, row["filing_year"], latest_year, cpi)
        return None if adjusted is None else adjusted

    attendance = chart_attendance(rows, years, latest_year)
    plate = viz.chart_card("sv-plate", "Plate and pledge", [
        _series(f"In {latest_year} dollars", rows, "plate_pledge", 0, real),
        _series("As reported", rows, "plate_pledge", 1),
    ], years, fmt="money", latest_year=latest_year,
        note=f"The solid line restates each year in {latest_year} dollars with the Consumer Price Index (CPI-U). The dashed line is what the parish reported.")
    pledges = viz.chart_card("sv-pledges", "Number of pledges", [_series("Pledges", rows, "pledge_cards", 0)], years, fmt="count",
                             latest_year=latest_year, note=pledge_note(rows, latest_year))
    money = viz.chart_card("sv-money", "Total revenue and total expenses", [
        _series("Revenue", rows, "total_revenues", 0),
        _series("Expenses", rows, "total_expenses", 1),
    ], years, fmt="money", latest_year=latest_year,
        note="Every line on the report, including capital and one-time items, so the years compare after the 2024 form change. As reported, not adjusted for inflation.")

    src = next((r.get("source") for r in sustain_data.cpi_rows() if r["year"] == latest_year), None)
    return {
        "state": "ok", "rows": rows, "latest": latest, "latest_year": latest_year, "as_of": sustain_data.data_as_of(rows),
        "takeaways": trend_takeaways(rows, cpi), "charts": [attendance, plate, pledges, money],
        "cpi_note": (f"The {latest_year} price index is the average of the months BLS published ({src.split(',', 1)[1].strip()})."
                     if src and "mean of" in src else ""),
    }


def chart_attendance(rows: list[dict], years: list[int], latest_year: int):
    flagged = any("asa_zero_with_giving" in r.get("flags", ()) for r in rows)
    note = "Members were not asked on the 2024 form, so that year is blank. Online attendance is counted separately, from 2024."
    if flagged:
        note += " A year with no Sunday attendance reported but money given is left blank, not shown as zero."
    return viz.chart_card("sv-attendance", "Sunday attendance (in person) and members", [
        _series("Sunday attendance (in person)", rows, "asa_in_person", 0),
        _series("Members", rows, "members", 1),
        _series("Sunday attendance (online)", rows, "asa_online", 2),
    ], years, fmt="count", latest_year=latest_year, note=note)


def pledge_note(rows: list[dict], latest_year: int) -> str:
    latest = rows[-1]
    then = next((r for r in rows if r["filing_year"] == latest_year - 10), None)
    avg_now = average_pledge(latest)
    if latest.get("pledge_cards") is not None and latest["pledge_cards"] < PLEDGE_PRIVACY_MIN:
        return f"The average pledge is not shown for a parish with fewer than {PLEDGE_PRIVACY_MIN} pledges."
    avg_then = average_pledge(then) if then else None
    if avg_now is None:
        return ""
    if avg_then is None:
        return f"The average pledge in {latest_year} was {dollars(avg_now)} (as reported)."
    return f"The average pledge was {dollars(avg_then)} in {latest_year - 10} and {dollars(avg_now)} in {latest_year} (as reported)."


# ------------------------------------------------------------------------------------------------ the calculator


def calc_inputs_for_form(base: dict, defaults: dict, guidelines: list[dict]) -> dict:
    """What the form shows before anyone touches it."""
    return build_inputs(defaults, {}, base, default_salary=guideline_salary(base.get("asa_in_person"), guidelines))


def calc_result(base: dict, defaults: dict, guidelines: list[dict], overrides: dict | None, *, spreadsheet_mode: bool = False) -> dict:
    """Validate the inputs and project. Always returns {"ok": bool, ...}. When ok is False, "error" holds the plain-words reason
    (a bad input, no salary yet, or a base year that cannot be projected). When ok, the answer, tiles, chart, legend, table, notices
    and the full inputs used are there; the page puts them on the screen as they are."""
    try:
        inputs = build_inputs(defaults, overrides, base, default_salary=guideline_salary(base.get("asa_in_person"), guidelines),
                              spreadsheet_mode=spreadsheet_mode)
    except InputError as exc:
        return {"ok": False, "error": str(exc), "inputs": None}
    try:
        rows = project(base, inputs)
    except ProjectionError as exc:
        return {"ok": False, "error": str(exc), "inputs": inputs}  # the form still shows what it holds, e.g. a blank salary
    summary = reserves_summary(base, rows, inputs)
    first, last = rows[0]["year"], rows[-1]["year"]
    # one branch per kind: runway_years is None unless the kind is "years", so the number is only formatted there
    kind = summary["runway_kind"]
    if kind == "years":
        runway = (f"{summary['runway_years']:.1f}", "years the reserves last after %d, at the %d gap" % (last, last))
    elif kind == "none":
        runway = ("None", "the reserves run out by %d" % last)
    else:
        runway = ("Not needed", "no reserves needed at the %d gap" % last)
    tiles = [
        {"value": dollars(summary["priest_cost_first_year"]),
         "label": f"priest cost in {first}, with pension" + (" and health insurance" if summary["health_included"] and inputs["health_annual"] else "")},
        {"value": dollars(summary["total_shortfall"]), "label": f"total shortfall, {first} to {last}"},
        {"value": dollars(summary["reserves_start"]), "label": "cash and unrestricted reserves today"},
        {"value": runway[0], "label": runway[1]},
    ]
    notices = []
    if not summary["health_included"]:
        notices.append({"kind": "gold", "text": "Health insurance not included."})
    if summary["other_exp_clamped"]:
        notices.append({"kind": "warn", "text": "Last year's costs look too low to include this priest's pay; turn off the first switch."})
    if inputs["spreadsheet_mode"]:
        notices.append({"kind": "info", "text": "Spreadsheet mode: the diocese's old workbook rules and fixed rates, for checking numbers against it."})
    return {
        "ok": True, "inputs": inputs, "rows": rows, "summary": summary, "answer": answer_sentence(rows, summary, inputs), "tiles": tiles,
        "tile_heading": "Replacing the current priest" if summary["replacing_current_priest"] else "Adding a priest on top of last year's costs",
        "chart": viz.projection_chart(rows, summary["safe_draw"]), "legend": viz.projection_legend(summary["safe_draw"]),
        "table": viz.projection_table(rows), "notices": notices, "first_year": first, "last_year": last,
    }


def saved_inputs(inputs: dict) -> dict:
    """What a saved scenario keeps: only what a parish leader may change (fractions, not percent). The pension rate, the pledge growth
    and the horizon come from the diocese's defaults whenever the scenario is loaded, so a later change to a default applies to it."""
    keep = ("salary", "health_annual", "income_growth", "salary_growth", "other_exp_growth", "safe_draw_rate", "included", "count_inv_as_draw")
    return {k: inputs[k] for k in keep}


def form_values(inputs: dict) -> dict:
    """The inputs as the form fields hold them (percent numbers for the rates, empty string for a blank)."""
    return {
        "salary": "" if inputs["salary"] is None else round(inputs["salary"]),
        "health_annual": "" if inputs["health_annual"] is None else round(inputs["health_annual"]),
        "income_growth": round(inputs["income_growth"] * 100, 2), "salary_growth": round(inputs["salary_growth"] * 100, 2),
        "other_exp_growth": round(inputs["other_exp_growth"] * 100, 2), "safe_draw_rate": round(inputs["safe_draw_rate"] * 100, 2),
        "pension_rate": round(inputs["pension_rate"] * 100, 2), "included": inputs["included"], "count_inv_as_draw": inputs["count_inv_as_draw"],
    }
