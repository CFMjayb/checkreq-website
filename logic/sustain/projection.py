"""The five-year "Can we afford a priest?" projection. Pure: no I/O. Plan section 6.

project(base, inputs) -> one dict per projected year. reserves_summary(base, rows, inputs) -> the four tiles and the "affordable" flag.

`base` is the parish's latest filing (a harmonized row). `inputs` is a plain dict (build it with logic.sustain.inputs.build_inputs):
  salary             priest salary and housing in the first projected year (required)
  pension_rate       pension on top of salary
  health_annual      yearly health insurance, or None = "not included" (the page says so); not grown in v1
  income_growth      (gi) plate and pledge, investments used for operations, other revenue
  pledge_growth      amount pledged only (shown; it does not drive revenue, as in the spreadsheet)
  salary_growth      (gs) priest pay raise
  other_exp_growth   (ge) other costs
  safe_draw_rate     share of unrestricted reserves a parish may draw a year
  included           True: last year's costs already include a full-time priest (spreadsheet behavior)
  count_inv_as_draw  True: investments used for operations are a draw on reserves, not new income
  years              projected years (default 5)

Spreadsheet mode (included=True, count_inv_as_draw=False, health 0) must match the diocese's workbook to the cent: see
tests/test_sustain_projection.py and sustainability_review/parity_cases.json.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

DEFAULT_YEARS = 5
SAFE_RUNWAY_YEARS = 10  # "affordable" in the diocese list: no final-year gap, or reserves that last at least this long


class ProjectionError(ValueError):
    """The projection cannot be run (no salary, or the base year has no operating expenses)."""


def _num(x) -> float:
    return 0.0 if x is None else float(x)


def default_included(base: Mapping) -> bool:
    """Default for "Last year's costs already included a full-time priest": on when the latest filing shows a full-time clergy
    person and the clergy is not long-term supply or interim; otherwise off."""
    full_time, supply = base.get("full_time_clergy"), base.get("supply_or_interim")
    return full_time is not None and full_time >= 1 and supply is not True


def project(base: Mapping, inputs: Mapping) -> list[dict]:
    """Project `years` years after the base filing. Each row: year, plate_pledge, inv_ops, other_rev, amount_pledged, salary,
    priest (salary x (1 + pension) + health), other_exp, revenue, expenses, net, other_exp_clamped."""
    if inputs.get("salary") is None:
        raise ProjectionError("Enter a salary.")
    if base.get("operating_expenses") is None or base.get("filing_year") is None:
        raise ProjectionError("The latest filing has no operating expenses to project from.")

    gi, pg, gs, ge = (float(inputs[k]) for k in ("income_growth", "pledge_growth", "salary_growth", "other_exp_growth"))
    pension, health = float(inputs["pension_rate"]), _num(inputs.get("health_annual"))
    included, draw = bool(inputs.get("included", True)), bool(inputs.get("count_inv_as_draw", False))
    years = int(inputs.get("years", DEFAULT_YEARS))

    plate, inv, other_rev = _num(base.get("plate_pledge")), _num(base.get("investments_for_operations")), _num(base.get("other_operating_revenue"))
    pledged = base.get("amount_pledged")  # None when hidden (under 10 pledges)
    salary, other_exp = float(inputs["salary"]), 0.0

    rows = []
    for k in range(1, years + 1):
        plate, inv, other_rev = plate * (1 + gi), inv * (1 + gi), other_rev * (1 + gi)
        pledged = None if pledged is None else float(pledged) * (1 + pg)
        clamped = False
        if k == 1:
            priest = salary * (1 + pension) + health
            other_exp = float(base["operating_expenses"]) * (1 + ge) - (priest if included else 0.0)
            if other_exp < 0:  # last year's costs are too small to hold this priest's pay
                other_exp, clamped = 0.0, True
        else:
            salary *= 1 + gs
            priest = salary * (1 + pension) + health
            other_exp *= 1 + ge
        revenue = plate + other_rev + (0.0 if draw else inv)
        expenses = other_exp + priest
        rows.append({
            "year": int(base["filing_year"]) + k, "plate_pledge": plate, "inv_ops": inv, "other_rev": other_rev,
            "amount_pledged": pledged, "salary": salary, "priest": priest, "other_exp": other_exp,
            "revenue": revenue, "expenses": expenses, "net": revenue - expenses, "other_exp_clamped": clamped,
        })
    return rows


def reserves_summary(base: Mapping, rows: Sequence[Mapping], inputs: Mapping) -> dict:
    """The tiles under the answer sentence.

    reserves_start     cash + unrestricted reserves in the base filing
    safe_draw          safe_draw_rate x unrestricted reserves, a year
    total_shortfall    the sum of the yearly deficits (years with a surplus are not netted against them)
    reserves_end       reserves_start - total_shortfall
    runway_kind/years  how long reserves_end lasts at the final year's gap: "years" (a number), "none" (reserves already gone),
                       or "not_needed" (no deficits at all, or reserves left and no gap in the final year)
    affordable         final-year gap is 0 or better, or reserves last at least SAFE_RUNWAY_YEARS (diocese list, plan phase 4)

    Investments used for operations (count_inv_as_draw): revenue already leaves them out, so the larger deficit that results is
    what draws the reserves down. The plan's section 6 also subtracts the total of them again from reserves_end; that counts the
    same dollars twice (a $30,900 investment draw would lower the reserves by $61,800 while the deficit has already grown by
    $30,900), so it is not done here. To check: tests/test_sustain_projection.py, test_draw_mode_counts_investments_once.
    """
    reserves_start = _num(base.get("total_cash")) + _num(base.get("unrestricted_reserves"))
    safe_draw = float(inputs["safe_draw_rate"]) * _num(base.get("unrestricted_reserves"))
    shortfall = sum(-r["net"] for r in rows if r["net"] < 0)
    reserves_end = reserves_start - shortfall
    final_net = rows[-1]["net"]

    if shortfall == 0:
        kind, years = "not_needed", None
    elif reserves_end <= 0:
        kind, years = "none", 0.0
    elif final_net < 0:
        kind, years = "years", reserves_end / -final_net
    else:
        kind, years = "not_needed", None

    return {
        "priest_cost_first_year": rows[0]["priest"],
        "total_shortfall": shortfall,
        "reserves_start": reserves_start,
        "reserves_end": reserves_end,
        "safe_draw": safe_draw,
        "final_net": final_net,
        "runway_kind": kind,
        "runway_years": years,
        "affordable": final_net >= 0 or kind == "not_needed" or (kind == "years" and years >= SAFE_RUNWAY_YEARS),
        "health_included": inputs.get("health_annual") is not None,
        "replacing_current_priest": bool(inputs.get("included", True)),
        "other_exp_clamped": any(r["other_exp_clamped"] for r in rows),
    }


def guideline_salary(asa, guidelines: Sequence[Mapping]):
    """The diocese's minimum priest salary and housing for a parish of this in-person Sunday attendance, from
    sustain.comp_guideline rows of one year ({"asa_min", "asa_max", "min_salary_housing"}; an open end is None). None when no
    band fits or the table is empty (the page then says "Enter a salary")."""
    if asa is None:
        return None
    for g in guidelines:
        lo, hi = g.get("asa_min"), g.get("asa_max")
        if (lo is None or asa >= lo) and (hi is None or asa <= hi) and g.get("min_salary_housing") is not None:
            return float(g["min_salary_housing"])
    return None
