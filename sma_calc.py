"""
sma_calc.py -- 26-129 SMA letters (plan revision 12): the allocation arithmetic and the letter's merge fields.

Pure functions, no I/O, standard library only (importable from anywhere with no circular-import risk).

THE FORMULA (the historic EDOM formula, confirmed by Jay 2026-09-26, read from the Task Force model's own
Assumptions tab -- see sma_model.py):

    Formula A = noi_rate * (three-year average NOI)  - noe_rate * (latest-year operating expense) - flat_deduction
    Formula B = noi_rate * (latest-year NOI)          - noe_rate * (latest-year operating expense) - flat_deduction
    Allocation = the LESSER of A and B (when lesser_of is on, else A alone), rounded to a whole dollar and
                 never below the model's minimum allocation (0).

Money is Decimal throughout, never float. Rounding is "half away from zero", which is what Excel's ROUND does,
so a result that lands on exactly .5 matches the model.

The letter prints WHOLE dollars (Formula A 39,275 for 39,274.76). The tie-out against the model compares the
unrounded results to the cent, so a rounding difference cannot hide a real one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

CENT = Decimal("0.01")
TIE_TOLERANCE = Decimal("0.01")     # the model stores its two tests to the cent


def D(value) -> Decimal:
    """Any number-ish (int, float from openpyxl, str, Decimal, None) -> Decimal. None and '' are 0.
    A float goes through str() so 0.18 is 0.18, not 0.17999999999999999."""
    if value is None or value == "":
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    try:
        d = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        raise ValueError(f"not a number: {value!r}")
    if not d.is_finite():                    # NaN and Infinity parse as Decimals but are never a figure
        raise ValueError(f"not a number: {value!r}")
    return d


_TAG = re.compile(r"\[[^\]]*\|[^\]]*\]")            # a Formstack Sign tag such as [sig|req|signer1]
_MERGE = re.compile(r"\{\s*\$")                      # the start of a Formstack Documents merge field, {$Name}


def safe_text(value, maxlen: int = 400) -> str:
    """Text an admin typed that will be MERGED into a letter (the parish name, an adjustment sentence). Formstack reads
    merge values as HTML and the signing form reads signature tags out of the text, so markup, merge-field syntax and
    signature tags are taken out. Control characters go and whitespace is collapsed."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    s = _TAG.sub("", s)
    s = _MERGE.sub("{", s)
    s = re.sub(r"<[^>]*>", "", s).replace("<", "").replace(">", "")      # whole tags, then any stray bracket
    return re.sub(r"\s+", " ", s).strip()[:maxlen]


def round_whole(x: Decimal) -> int:
    """Excel ROUND(x, 0): to the nearest whole number, halves away from zero."""
    return int(x.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def money(n) -> str:
    """Whole dollars with thousands separators, as the letter prints them: 41834 -> '41,834', -5000 -> '-5,000'."""
    return f"{round_whole(D(n)):,}"


def pct(rate) -> str:
    """0.18 -> '18%', 0.045 -> '4.5%'."""
    p = (D(rate) * 100).normalize()
    text = format(p, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text}%"


@dataclass(frozen=True)
class Rates:
    noi_rate: Decimal
    noe_rate: Decimal
    flat_deduction: Decimal
    lesser_of: bool = True
    min_allocation: Decimal = Decimal(0)


@dataclass(frozen=True)
class Calc:
    noi_avg: Decimal
    a_income: Decimal        # noi_rate * three-year average
    a_expense: Decimal       # noe_rate * operating expense (the same credit in both formulas)
    formula_a: Decimal
    b_income: Decimal        # noi_rate * latest-year NOI
    formula_b: Decimal
    raw: Decimal             # the lesser (or A alone), unrounded
    total: int               # the whole-dollar allocation
    monthly: int             # suggested monthly payment, whole dollars


def compute(noi_y1, noi_y2, noi_y3, noe, rates: Rates) -> Calc:
    y1, y2, y3, e = D(noi_y1), D(noi_y2), D(noi_y3), D(noe)
    avg = (y1 + y2 + y3) / 3
    a_income = rates.noi_rate * avg
    a_expense = rates.noe_rate * e
    a = a_income - a_expense - rates.flat_deduction
    b_income = rates.noi_rate * y3
    b = b_income - a_expense - rates.flat_deduction
    raw = min(a, b) if rates.lesser_of else a
    total = max(round_whole(raw), round_whole(rates.min_allocation))
    return Calc(noi_avg=avg, a_income=a_income, a_expense=a_expense, formula_a=a,
                b_income=b_income, formula_b=b, raw=raw, total=total,
                monthly=round_whole(Decimal(total) / 12))


# ---------------------------------------------------------------------------------------------
# Adjustments. The model's own "2027 allocation" is not always the plain formula result: the Task Force
# carried documented hand adjustments forward (2026-10-08, from the model's Back-test tab: three parishes
# had a hand-applied 50% reduction, and one parish's three-year average rests on a single year so the
# latest-year test is used). Beacon never applies one by itself. It SUGGESTS one when it explains the
# model's number exactly, and a person confirms it on the check sheet. The letter then says so in words.
ADJ_NONE, ADJ_HALF, ADJ_LATEST, ADJ_CUSTOM = "none", "half", "latest_year", "custom"
ADJUSTMENT_KINDS = (ADJ_NONE, ADJ_HALF, ADJ_LATEST, ADJ_CUSTOM)
DEFAULT_ADJUSTMENT_TEXT = {
    ADJ_HALF: "A 50% reduction applies to your allocation, as in prior years.",
    ADJ_LATEST: "The latest-year test (Formula B) applies: your three-year average rests on fewer than three years.",
}


def apply_adjustment(calc: Calc, kind: str, custom_total=None) -> int:
    """The whole-dollar allocation printed on the letter after any confirmed adjustment."""
    if kind == ADJ_HALF:
        return round_whole(Decimal(calc.total) / 2)
    if kind == ADJ_LATEST:
        return max(round_whole(calc.formula_b), 0)
    if kind == ADJ_CUSTOM:
        if custom_total is None:
            raise ValueError("a custom adjustment needs the amount to print")
        return int(custom_total)
    return calc.total


def suggest_adjustment(calc: Calc, model_total) -> str | None:
    """The adjustment that exactly explains the model's number, or None. Used only to suggest."""
    if model_total is None:
        return None
    target = round_whole(D(model_total))
    if target == calc.total:
        return None
    if target == apply_adjustment(calc, ADJ_HALF):
        return ADJ_HALF
    if target == apply_adjustment(calc, ADJ_LATEST):
        return ADJ_LATEST
    return None


def ties_to_model(calc: Calc, model_a, model_b, model_total, *, final_total: int | None = None) -> str:
    """'match', 'mismatch', or 'none' when the model gave no numbers to compare against. Formula A and B are
    compared to the cent (within TIE_TOLERANCE). The allocation compared is `final_total` (after any
    confirmed adjustment) or, when none is given, the plain formula result."""
    if model_a is None and model_b is None and model_total is None:
        return "none"
    if model_a is not None and abs(calc.formula_a - D(model_a)) > TIE_TOLERANCE:
        return "mismatch"
    if model_b is not None and abs(calc.formula_b - D(model_b)) > TIE_TOLERANCE:
        return "mismatch"
    total = calc.total if final_total is None else final_total
    if model_total is not None and total != round_whole(D(model_total)):
        return "mismatch"
    return "match"


ROUNDING_NOTE = "Amounts are rounded to the nearest dollar, so a line may differ from the sum above by $1."


def footnote(year1: int, year2: int, year3: int) -> str:
    return (f"* The {year2} Parochial Report did not collect Normal Operating Income; {year2} is estimated "
            f"as the average of {year1} and {year3}.")


def merge_fields(*, year: int, prior_year: int, year1: int, year2: int, year3: int, letter_name: str,
                 rates: Rates, noi_y1, noi_y2, noi_y3, noe, prior_allocation,
                 year2_estimated: bool = True, adjustment_kind: str = ADJ_NONE,
                 adjustment_text: str = "", custom_total=None) -> dict[str, str]:
    """The data one Formstack Documents merge needs for the LETTER template (the Allocation Form plus the
    'how your allocation was calculated' page). Every value is a string, exactly as printed. The field names
    are the {$Name} merge fields in the Word template (build_sma_templates.py).

    With a confirmed adjustment the printed allocation is the adjusted one and the letter says so in words
    (AdjustmentNote, AllocationQualifier). With none, both are plain and AdjustmentNote is empty."""
    if adjustment_kind not in ADJUSTMENT_KINDS:
        raise ValueError(f"unknown adjustment kind {adjustment_kind!r}")
    c = compute(noi_y1, noi_y2, noi_y3, noe, rates)
    total = apply_adjustment(c, adjustment_kind, custom_total)
    adjusted = adjustment_kind != ADJ_NONE
    note = (adjustment_text or DEFAULT_ADJUSTMENT_TEXT.get(adjustment_kind, "")).strip() if adjusted else ""
    if adjusted and not note:
        raise ValueError("a custom adjustment needs the words the letter will print")
    return {
        "Year": str(year), "PriorYear": str(prior_year), "ParishName": safe_text(letter_name, 150),
        "Y1": str(year1), "Y2": str(year2), "Y3": str(year3),
        "NOIRatePct": pct(rates.noi_rate), "NOERatePct": pct(rates.noe_rate),
        "FlatDeduction": money(rates.flat_deduction),
        "NOI1": money(noi_y1), "NOI2": money(noi_y2), "NOI3": money(noi_y3),
        "NOI2Note": "(estimated*)" if year2_estimated else "",
        "NOIAvg": money(c.noi_avg), "NOE": money(noe),
        "A_Income": money(c.a_income), "A_Expense": money(c.a_expense), "FormulaA": money(c.formula_a),
        "B_Income": money(c.b_income), "B_Expense": money(c.a_expense), "FormulaB": money(c.formula_b),
        "TotalAllocation": money(total), "MonthlyPayment": money(round_whole(Decimal(total) / 12)),
        "AllocationQualifier": "(the lesser of A and B, adjusted as noted below)" if adjusted else "(the lesser of A and B)",
        "AdjustmentNote": safe_text(note, 400),
        "PriorAllocation": money(prior_allocation),
        "Footnote": (footnote(year1, year2, year3) + " " + ROUNDING_NOTE) if year2_estimated else ROUNDING_NOTE,
    }
