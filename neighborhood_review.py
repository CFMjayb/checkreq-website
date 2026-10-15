"""
neighborhood_review.py -- the "Neighborhood Review" table for diocesan staff (26-XXX Congregational Vitality, phase 4, mockup C).

One row per parish: eight neighborhood measures with a shaded cell where the parish's area is clearly higher or lower than its own
state, county or the U.S., plus the one thing that stands out most. Higher is never called better or worse. Four count cards above the
table say how many parishes sit in growing, older, lower-income or SNAP-heavy areas.

Pure functions: numbers in, plain dicts out. No database and no web code. The numbers per parish come from
neighborhood_snapshot.collect_stats (the same ones the parish's own Neighborhood View uses), so the two pages always agree; the
"clearly higher or lower" test is neighborhood_insights.classify, the same rule that picks "What stands out" on that page.
"""
from __future__ import annotations

import neighborhood_insights as ins

LEVELS = ("us", "state", "county")
LEVEL_WORDS = {"us": "the U.S.", "state": "the state", "county": "the county"}
LEVEL_NAMES = {"us": "U.S.", "state": "State", "county": "County"}

SMALL_AREA_PEOPLE = 2000        # growth in an area this small is shown with a caution and never shaded

# (stats key, column heading, number style). People has no comparison: it is the size of the area.
COLUMNS = (
    ("total_population", "People", "count"),
    ("pop_cagr_2010_2020", "Growth a year, 2010 to 2020", "percent2"),
    ("median_age", "Typical age", "years"),
    ("median_hh_income", "Typical household income", "dollars"),
    ("hh_below_poverty_pct", "Homes in poverty", "percent"),
    ("hh_snap_pct", "Homes using SNAP", "percent"),
    ("living_alone", "Living alone", "percent"),
    ("renter_share", "Homes that are rented", "percent"),
)
COLUMN_KEYS = tuple(c[0] for c in COLUMNS)

# the four cards above the table: (stats key, direction that counts, card title, what it counts)
SUMMARY = (
    ("pop_cagr_2010_2020", "above", "Growing faster", "population growth, 2010 to 2020"),
    ("median_age", "above", "Older", "typical age clearly higher"),
    ("median_hh_income", "below", "Lower income", "typical household income clearly lower"),
    ("hh_snap_pct", "above", "More SNAP use", "share of homes using SNAP"),
)


def _f(x):
    return None if x is None else float(x)


def add_living_alone(stats: dict, values: dict, bm: dict) -> dict:
    """Adds the "living alone" share to `stats` (the same shape as every collect_stats entry) and returns `stats`. It is the one-person
    nonfamily group of the household-size table over all households, for the area and for each comparison level. Left out when the
    area's household count or that group is missing."""
    rows = {r["component"]: r for r in values.get("hh_nonfamily_by_size", ())}
    one = rows.get("Nonfamily: 1 person")
    hh = stats.get("total_households")
    if one is None or one.get("value") is None or not hh or not hh.get("value"):
        return stats
    benches = {}
    for lv in LEVELS:
        n = _f(bm.get(lv, {}).get("hh_nonfamily_by_size", {}).get("Nonfamily: 1 person"))
        d = (hh.get("benches") or {}).get(lv)
        benches[lv] = (n / d * 100.0) if n is not None and d else None
    stats["living_alone"] = {"value": float(one["value"]) / hh["value"] * 100.0, "reliability": one.get("reliability"), "benches": benches, "moe": None}
    return stats


def classify_cell(key: str, style: str, stat: dict | None, level: str) -> dict:
    """{value, bench, shade ('hi' | 'lo' | ''), word, reliability, small}. No shading when the number is unreliable, when the area is
    tiny (growth only), or when there is nothing to compare with."""
    if not stat or stat.get("value") is None:
        return {"value": None, "bench": None, "shade": "", "word": "", "reliability": None, "small": False}
    value = float(stat["value"])
    bench = None if key == "total_population" else _f((stat.get("benches") or {}).get(level))   # People is the size of the area: nothing to compare
    rel = stat.get("reliability")
    shade, word = "", ""
    if bench is not None:
        direction, _score = ins.classify(style, value, bench)
        shade, word = {"above": ("hi", "higher"), "below": ("lo", "lower")}.get(direction, ("", "about the same"))
    if rel == "unreliable":
        shade = ""
    return {"value": value, "bench": bench, "shade": shade, "word": word, "reliability": rel, "small": False}


def parish_row(parish: dict, level: str) -> dict:
    """One table row. `parish` = {id, name, place, stats (collect_stats plus living_alone), top (text or '')}. Parishes with no
    stats (no confirmed church location, or no numbers yet) still get a row, with `has_numbers` False."""
    stats = parish.get("stats")
    cells = {}
    pop = (stats or {}).get("total_population", {}).get("value")
    for key, _title, style in COLUMNS:
        cell = classify_cell(key, style, (stats or {}).get(key), level)
        if key == "pop_cagr_2010_2020" and cell["value"] is not None and pop is not None and pop < SMALL_AREA_PEOPLE:
            cell["small"] = True
            cell["shade"] = ""
        cell["text"] = ins.fmt_value(style, cell["value"]) if cell["value"] is not None else "n/a"
        cell["bench_text"] = ins.fmt_value(style, cell["bench"]) if cell["bench"] is not None else ""
        cells[key] = cell
    return {"id": parish["id"], "name": parish["name"], "place": parish.get("place", ""), "cells": cells,
            "top": parish.get("top") or "", "has_numbers": bool(stats), "reason": parish.get("reason", "")}


def summary_cards(rows: list[dict], level: str) -> list[dict]:
    """How many of the parishes that have numbers are clearly higher or lower, per SUMMARY. {title, count, of, what, against}."""
    with_numbers = [r for r in rows if r["has_numbers"]]
    out = []
    for key, direction, title, what in SUMMARY:
        want = "hi" if direction == "above" else "lo"
        out.append({"title": title, "count": sum(1 for r in with_numbers if r["cells"][key]["shade"] == want), "of": len(with_numbers),
                    "what": what, "against": LEVEL_WORDS[level] if level == "us" else f"their {'state' if level == 'state' else 'county'}"})
    return out


def sort_rows(rows: list[dict], key: str, descending: bool) -> list[dict]:
    """Parishes with no number for the chosen column always come last, whichever way it is sorted. Ties keep alphabetical order."""
    alpha = sorted(rows, key=lambda r: r["name"].lower())
    if key == "name" or key not in COLUMN_KEYS:
        return list(reversed(alpha)) if (descending and key == "name") else alpha
    have = [r for r in alpha if r["cells"][key]["value"] is not None]
    lack = [r for r in alpha if r["cells"][key]["value"] is None]
    have.sort(key=lambda r: r["cells"][key]["value"], reverse=descending)   # stable: ties stay alphabetical
    return have + lack


def build(parishes: list[dict], level: str = "state", sort: str = "name", descending: bool = False) -> dict:
    """The whole table. `level` is 'us', 'state' or 'county'; unknown values fall back to 'state'. Returns
    {level, level_word, columns: [{key, title, active, arrow}], rows, summary, counts: {with_numbers, without}}."""
    if level not in LEVELS:
        level = "state"
    rows = [parish_row(p, level) for p in parishes]
    ordered = sort_rows(rows, sort, descending)
    active = sort if (sort == "name" or sort in COLUMN_KEYS) else "name"
    cols = [{"key": "name", "title": "Parish", "active": active == "name"}] + \
           [{"key": k, "title": t, "active": active == k} for k, t, _s in COLUMNS]
    for c in cols:
        c["arrow"] = ("down" if descending else "up") if c["active"] else ""
    return {"level": level, "level_word": LEVEL_WORDS[level], "columns": cols, "rows": ordered, "summary": summary_cards(rows, level),
            "counts": {"with_numbers": sum(1 for r in rows if r["has_numbers"]), "without": sum(1 for r in rows if not r["has_numbers"])}}


def top_sentence(stats: dict, level: str, phrases: dict | None = None) -> str:
    """The one thing that stands out most for a parish, as the same sentence its own Neighborhood View leads with. '' when
    nothing differs much."""
    if not stats:
        return ""
    bench_name = {"us": "U.S. average", "state": "state average", "county": "county average"}[level]
    picks = ins.stands_out({k: {"value": s["value"], "bench": (s.get("benches") or {}).get(level), "reliability": s.get("reliability")}
                            for k, s in stats.items()}, bench_name, phrases, max_items=1)
    return picks[0]["headline"] if picks else ""
