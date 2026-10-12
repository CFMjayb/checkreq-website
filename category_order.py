"""
category_order.py -- the ONE order and the short names for the kinds of hours, used by BOTH grids of the Time Review entry screen
(Jay, 2026-10-11: "the order on both should be reg/ot/sick/vacation/holiday" and the short names Reg / OT / Sick / Vac / Holiday so the
number columns of the top and bottom grids line up).  Display only: nothing here changes what is stored, the Checkwriters export or
any category's own record.  A kind of hours this list does not know sorts after these, by its own sort order and name.
"""
from __future__ import annotations

ORDER = {"regular": 1, "overtime": 2, "sick": 3, "vacation": 4, "holiday": 5}
SHORT = {"regular": "Reg", "overtime": "OT", "sick": "Sick", "vacation": "Vac", "holiday": "Holiday"}


def rank(key: str | None) -> int:
    return ORDER.get(key or "", 50)


def short(key: str | None, label: str | None) -> str:
    """The short column name for a kind of hours (its own label when it is not one of the five)."""
    return SHORT.get(key or "", label or "")


def sort_categories(rows: list[dict]) -> list[dict]:
    """The categories in the screen order, each with a 'short' name."""
    out = sorted(rows, key=lambda c: (rank(c.get("key")), c.get("sort_order") or 0, c.get("label") or ""))
    for c in out:
        c["short"] = short(c.get("key"), c.get("label"))
    return out
