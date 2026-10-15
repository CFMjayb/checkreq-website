"""CPI-U annual averages from monthly indexes. Pure: no I/O. See CONGREGATIONAL_VITALITY_PLAN.md sections 3 and 4.

BLS's annual average is the mean of the 12 monthly indexes, rounded to 3 decimals. That was checked against BLS's published
annual averages for 2019 to 2024 on 2026-10-15 (255.657, 258.811, 270.970, 292.655, 304.702, 313.689).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal

SERIES_ID = "CUUR0000SA0"
DEFAULT_SOURCE = f"BLS {SERIES_ID}"
_MONTHS = tuple(f"M{m:02d}" for m in range(1, 13))


def cpi_adjust(amount, from_year: int, to_year: int, cpi: Mapping[int, object]):
    """Restate `amount` (dollars of from_year) in to_year dollars: amount * cpi[to_year] / cpi[from_year].

    `cpi` maps year -> CPI-U index (Decimal or float). Returns a float, or None when the amount is missing or either year has
    no index (the page skips what it cannot compute rather than guessing).
    """
    if amount is None:
        return None
    then, now = cpi.get(from_year), cpi.get(to_year)
    if then is None or now is None or float(then) <= 0:
        return None
    return float(amount) * float(now) / float(then)


class PartialYearError(ValueError):
    """A year has fewer than 12 published months and the caller did not allow a partial year."""


def annual_averages(records: Iterable[dict], allow_partial_years: Iterable[int] = ()) -> list[dict]:
    """Turn monthly records ({"year", "periods": {"M01": "317.671" or None, ...}}) into annual rows
    {"year", "cpi_u" (Decimal, 3 places), "source"}, oldest first.

    A year with all 12 months is the plain mean. A year with fewer raises PartialYearError, unless its year is in
    allow_partial_years: then it is the mean of the months that exist, and its source text says how many and why. A year with
    no published month at all is always an error (nothing to average).
    """
    allowed = set(allow_partial_years)
    rows = []
    for rec in sorted(records, key=lambda r: r["year"]):
        year = rec["year"]
        have = {m: Decimal(str(rec["periods"][m])) for m in _MONTHS if rec["periods"].get(m) not in (None, "", "-")}
        if not have:
            raise PartialYearError(f"{year}: BLS published no monthly index, nothing to average")
        if len(have) < 12 and year not in allowed:
            missing = ", ".join(m for m in _MONTHS if m not in have)
            raise PartialYearError(f"{year}: only {len(have)} of 12 months published (missing {missing}); pass it in allow_partial_years to use the mean of the months that exist")
        mean = (sum(have.values()) / len(have)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        if len(have) == 12:
            source = DEFAULT_SOURCE
        else:
            why = "; ".join(rec.get("footnotes") or []) or "month not published"
            source = f"{DEFAULT_SOURCE}, mean of {len(have)} months ({why})"
        rows.append({"year": year, "cpi_u": mean, "source": source})
    return rows
