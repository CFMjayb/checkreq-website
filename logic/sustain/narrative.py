"""The one plain answer sentence above the tiles ("Can we afford a priest?"). Pure: no I/O. Plan section 5, mockup D.

Money in the sentence is rounded to the nearest $100 for display; nothing computed upstream is rounded.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence


def dollars(v: float) -> str:
    """$12,300 (nearest $100), with a real minus sign for negatives."""
    v = round(v / 100) * 100
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _pct(rate: float) -> str:
    return f"{rate * 100:g}%"


def answer_sentence(rows: Sequence[Mapping], summary: Mapping, inputs: Mapping) -> dict:
    """{"kind": "surplus" | "deficit_every_year" | "deficit_some_years", "lead": str, "rest": str, "text": lead + " " + rest}.

    `lead` is the short bold part ("Deficit every year:"). The safe-draw sentence compares a year's safe draw with the largest
    yearly gap, in the year it happens.
    """
    deficits = [r for r in rows if r["net"] < 0]
    first, last = rows[0], rows[-1]

    if not deficits:
        kind = "surplus"
        lead, rest = "Yes, on these assumptions.", f"Revenue covers the budget every year through {last['year']}."
        return {"kind": kind, "lead": lead, "rest": rest, "text": f"{lead} {rest}"}

    worst = min(deficits, key=lambda r: r["net"])
    gap = -worst["net"]
    safe = summary["safe_draw"]
    if safe <= 0:
        cover = "The parish has no unrestricted reserves on file, so a safe endowment draw would not cover the gap."
    elif safe >= gap:
        cover = f"A {_pct(inputs['safe_draw_rate'])} endowment draw ({dollars(safe)} a year) would cover even the {worst['year']} gap."
    else:
        cover = f"A {_pct(inputs['safe_draw_rate'])} endowment draw ({dollars(safe)} a year) would not cover the {worst['year']} gap."

    if len(deficits) == len(rows):
        kind = "deficit_every_year"
        trend = "rising to" if last["net"] < first["net"] else "easing to" if last["net"] > first["net"] else "staying at"
        lead = "Deficit every year:"
        rest = f"{dollars(-first['net'])} in {first['year']}, {trend} {dollars(-last['net'])} in {last['year']}. {cover}"
    else:
        kind = "deficit_some_years"
        lead = f"Deficit in {len(deficits)} of {len(rows)} years:"
        rest = f"the largest is {dollars(gap)} in {worst['year']}. {cover}"
    return {"kind": kind, "lead": lead, "rest": rest, "text": f"{lead} {rest}"}
