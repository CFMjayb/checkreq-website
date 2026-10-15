"""The "What stands out" sentences for the Parish Sustainability Review. Pure: no I/O. Plan section 5.

trend_takeaways(years, cpi) takes one parish's harmonized rows (logic.sustain.harmonize.harmonize_year, any order) and returns up to
four sentences, in this order, each skipped when its inputs are missing:

  1. In-person Sunday attendance now against 10 years earlier (+ "leveled off" when the last 3 years vary by under 5%).
  2. Plate and pledge as reported, and after inflation.
  3. Pledges against the average pledge (only with 10+ pledges in both years, fewer pledges, a higher average pledge).
  4. Deficit streak: total expenses above total revenue every year, for 3 years or more.

Sentence 4 uses total_revenues and total_expenses in every era (Jay, 2026-10-15, answer 2): the 2024 form moved items between
operating and non-operating, so the operating lines are not comparable across 2023 to 2024.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from logic.sustain.cpi import cpi_adjust
from logic.sustain.harmonize import average_pledge

WINDOW_YEARS = 10          # "now against 10 years earlier"
LEVELED_OFF_YEARS = 3      # the last three filings
LEVELED_OFF_SPREAD = 0.05  # (max - min) / mean under 5%
MIN_STREAK = 3             # years of expenses above revenue before sentence 4 appears


def trend_takeaways(years: Iterable[Mapping], cpi: Mapping[int, object] | None = None) -> list[str]:
    by_year = {int(r["filing_year"]): r for r in years}
    if not by_year:
        return []
    latest_year = max(by_year)
    latest = by_year[latest_year]
    then = by_year.get(latest_year - WINDOW_YEARS)

    sentences = [
        _attendance(by_year, latest_year, latest, then),
        _plate_and_pledge(latest_year, latest, then, cpi or {}),
        _pledges(latest, then),
        _deficit_streak(by_year, latest_year),
    ]
    return [s for s in sentences if s]


def _fmt_count(x: float) -> str:
    return f"{round(x):,}"


def _move(pct: float) -> tuple[str, int]:
    """("up" | "down" | "flat", whole percent). A change that rounds to 0% is flat."""
    n = round(abs(pct))
    if n == 0:
        return "flat", 0
    return ("up" if pct > 0 else "down"), n


# ------------------------------------------------------------------------------------------------ 1. attendance
def _attendance(by_year, latest_year, latest, then) -> str | None:
    now_v = latest.get("asa_in_person")
    then_v = then.get("asa_in_person") if then else None
    if now_v is None or then_v is None or then_v <= 0:
        return None
    base_year = latest_year - WINDOW_YEARS
    direction, n = _move((now_v / then_v - 1) * 100)
    if direction == "flat":
        return f"Sunday attendance is {_fmt_count(now_v)}, about the same as in {base_year} ({_fmt_count(then_v)})."
    sign = "+" if direction == "up" else "-"
    text = f"Sunday attendance is {_fmt_count(now_v)}, {direction} from {_fmt_count(then_v)} in {base_year} ({sign}{n}%)"
    level_since = _leveled_off_since(by_year, latest_year)
    if level_since is not None:
        text += f", and has leveled off since {level_since}"
    return text + "."


def _leveled_off_since(by_year, latest_year) -> int | None:
    first = latest_year - (LEVELED_OFF_YEARS - 1)
    values = []
    for y in range(first, latest_year + 1):
        v = (by_year.get(y) or {}).get("asa_in_person")
        if v is None:
            return None
        values.append(v)
    mean = sum(values) / len(values)
    if mean <= 0:
        return None
    return first if (max(values) - min(values)) / mean < LEVELED_OFF_SPREAD else None


# ------------------------------------------------------------------------------------------------ 2. plate and pledge
def _plate_and_pledge(latest_year, latest, then, cpi) -> str | None:
    if then is None:
        return None
    now_v, then_v = latest.get("plate_pledge"), then.get("plate_pledge")
    if now_v is None or then_v is None or then_v <= 0 or now_v <= 0:
        return None
    base_year = latest_year - WINDOW_YEARS
    then_real = cpi_adjust(then_v, base_year, latest_year, cpi)
    if then_real is None or then_real <= 0:
        return None
    as_reported, after_inflation = _move((now_v / then_v - 1) * 100), _move((now_v / then_real - 1) * 100)

    def phrase(move):
        d, n = move
        return "about unchanged" if d == "flat" else f"{d} {n}%"

    joiner = ", and " if as_reported[0] == after_inflation[0] else ", but "
    return f"Plate and pledge is {phrase(as_reported)} as reported since {base_year}{joiner}{phrase(after_inflation)} after inflation."


# ------------------------------------------------------------------------------------------------ 3. pledges
def _pledges(latest, then) -> str | None:
    """Only with 10+ pledges in both years (so both average pledges may be shown), fewer pledges now, a higher average pledge."""
    if then is None:
        return None
    avg_now, avg_then = average_pledge(latest), average_pledge(then)
    n_now, n_then = latest.get("pledge_cards"), then.get("pledge_cards")
    if avg_now is None or avg_then is None or n_now is None or n_then is None:
        return None
    if not (n_now < n_then and avg_now > avg_then):
        return None
    return f"{n_now:,} pledges, down from {n_then:,}; the average pledge is up, so fewer households carry more of the budget."


# ------------------------------------------------------------------------------------------------ 4. deficit streak
def _deficit_streak(by_year, latest_year) -> str | None:
    """Consecutive filing years, ending with the latest, in which total expenses were above total revenue."""
    start = None
    y = latest_year
    while y in by_year:
        rev, exp = by_year[y].get("total_revenues"), by_year[y].get("total_expenses")
        if rev is None or exp is None or not exp > rev:
            break
        start = y
        y -= 1
    if start is None or latest_year - start + 1 < MIN_STREAK:
        return None
    return f"Expenses have been higher than revenue every year since {start}."
