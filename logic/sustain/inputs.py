"""Turn what a browser sent (or a saved scenario holds) into the inputs dict that logic.sustain.projection runs on. Pure: no I/O.

Nothing from a browser is trusted: every number is range-checked and a bad value raises InputError with a plain-words message.
Defaults come from sustain.assumption_default (the caller passes them in as a plain dict); only what a parish leader may change
is read from `overrides`. The pension rate is shown but not editable, so it is never taken from `overrides`.

Spreadsheet mode (diocesan staff only; the caller decides who) is for checking numbers against the old workbook: fixed rates, no
health insurance, last year's costs include a priest, investments counted as income.
"""
from __future__ import annotations

from collections.abc import Mapping

from logic.sustain.projection import DEFAULT_YEARS, default_included

RATE_KEYS = ("income_growth", "salary_growth", "other_exp_growth")
SPREADSHEET_RATES = {  # fixed in the diocese's Financial Projection workbook; the diocese defaults do not apply in this mode
    "income_growth": 0.03, "pledge_growth": 0.03, "salary_growth": 0.05, "other_exp_growth": 0.04, "pension_rate": 0.18,
}
_RANGES = {  # (low, high, label for messages)
    "salary": (0, 1_000_000, "Salary and housing"),
    "health_annual": (0, 200_000, "Health insurance"),
    "income_growth": (-0.20, 0.25, "Giving and income growth"),
    "salary_growth": (-0.20, 0.25, "Priest pay raise"),
    "other_exp_growth": (-0.20, 0.25, "Other costs growth"),
    "safe_draw_rate": (0.0, 0.20, "Safe endowment draw"),
}


class InputError(ValueError):
    """A projection input is not a number or is outside the range the page allows."""


def _number(overrides: Mapping, key: str):
    value = overrides[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise InputError(f"{_RANGES[key][2]} must be a number.")
    low, high, label = _RANGES[key]
    if not low <= value <= high:
        raise InputError(f"{label} must be between {low:g} and {high:g}.")
    return float(value)


def _switch(overrides: Mapping, key: str, default: bool) -> bool:
    if key not in overrides or overrides[key] is None:
        return default
    if not isinstance(overrides[key], bool):
        raise InputError("Switches must be on or off.")
    return overrides[key]


def build_inputs(defaults: Mapping, overrides: Mapping | None, base: Mapping, *, default_salary=None, spreadsheet_mode: bool = False) -> dict:
    """The complete inputs dict for project().

    defaults        sustain.assumption_default values for the diocese: pledge_growth, income_growth, salary_growth,
                    other_exp_growth, pension_rate, safe_draw_rate, health_annual (a value may be None)
    overrides       what the user changed: salary, health_annual, income_growth, salary_growth, other_exp_growth,
                    safe_draw_rate, included, count_inv_as_draw (anything else is ignored)
    base            the latest filing, for the default of the "already included a full-time priest" switch
    default_salary  the diocese's guideline salary for the parish's size, or None ("Enter a salary")
    """
    overrides = overrides or {}
    inputs = {
        "salary": default_salary, "pension_rate": defaults.get("pension_rate"), "health_annual": defaults.get("health_annual"),
        "income_growth": defaults.get("income_growth"), "pledge_growth": defaults.get("pledge_growth"),
        "salary_growth": defaults.get("salary_growth"), "other_exp_growth": defaults.get("other_exp_growth"),
        "safe_draw_rate": defaults.get("safe_draw_rate"),
        "included": default_included(base), "count_inv_as_draw": True, "years": DEFAULT_YEARS,
    }
    for key in _RANGES:
        if key in overrides:
            inputs[key] = _number(overrides, key)
    inputs["included"] = _switch(overrides, "included", inputs["included"])
    inputs["count_inv_as_draw"] = _switch(overrides, "count_inv_as_draw", inputs["count_inv_as_draw"])

    if spreadsheet_mode:
        inputs.update(SPREADSHEET_RATES, health_annual=0.0, included=True, count_inv_as_draw=False)

    missing = [k for k in ("pension_rate", "income_growth", "pledge_growth", "salary_growth", "other_exp_growth", "safe_draw_rate") if inputs[k] is None]
    if missing:
        raise InputError("The diocese has not set: " + ", ".join(missing) + ".")
    inputs["spreadsheet_mode"] = spreadsheet_mode
    return inputs
