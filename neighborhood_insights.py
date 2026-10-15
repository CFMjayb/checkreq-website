"""
neighborhood_insights.py -- the "What stands out" sentences for the Neighborhood page (26-XXX Neighborhood Insights).

Pure functions, no database and no web code. Given a parish's numbers and the comparison numbers (U.S., state or county), pick the
few biggest differences and put them in plain words.

The wording lives in a table (DEFAULT_PHRASES below, same fields as the CSV CoWork writes: see load_phrases). A phrase file next to
this module (neighborhood_insight_phrases.csv) overrides the built-in wording for the metrics it names.

Rules:
  * a number whose reliability is "unreliable" is never a key message; "use caution" ones are kept and say the estimate is rough
  * nothing is called good or bad: higher is only higher
  * "may point to" lines are hedged ideas to discuss, never causes
"""
from __future__ import annotations

import csv
import os

# ------------------------------------------------------------------------------------------------ formatting


def fmt_value(style: str, v) -> str:
    if v is None:
        return "n/a"
    v = float(v)
    if style == "count":
        return f"{round(v):,}"
    if style == "percent":
        return f"{v:.1f}%"
    if style == "percent2":
        return f"{v:.2f}%"
    if style == "dollars":
        return f"${round(v):,}"
    if style == "years":
        return f"{v:.1f}"
    if style == "index":
        return f"{v:.0f}"
    return f"{v:,.1f}"


# ------------------------------------------------------------------------------------------------ wording

# metric_key -> fields. theme groups ideas so one idea does not fill the whole strip. priority 1 (rarely) to 5 (often a key message).
# short_above / short_below: the two or three words used in the one-sentence summary.
DEFAULT_PHRASES: dict[str, dict] = {
    "median_age": dict(plain_name="Median age", style="years", theme="age", priority=5, short_above="an older population", short_below="a younger population",
        above="Older than the {bench_name}: median age {value}, against {bench}.",
        below="Younger than the {bench_name}: median age {value}, against {bench}.",
        similar="Median age is about the same as the {bench_name}: {value}, against {bench}.",
        pt_above="May point to: more need for senior ministries and pastoral care.",
        pt_below="May point to: more young adults and families to welcome."),
    "senior_share": dict(plain_name="People 65 and over", style="percent", theme="age", priority=4, short_above="more seniors", short_below="fewer seniors",
        above="More people 65 and over than the {bench_name}: {value}, against {bench}.",
        below="Fewer people 65 and over than the {bench_name}: {value}, against {bench}.",
        similar="People 65 and over are about the same share as the {bench_name}: {value}, against {bench}.",
        pt_above="Worth asking: how do we reach neighbors who are home-bound or alone?",
        pt_below=""),
    "youth_share": dict(plain_name="People under 35", style="percent", theme="age", priority=3, short_above="more young people", short_below="fewer young people",
        above="More people under 35 than the {bench_name}: {value}, against {bench}.",
        below="Fewer people under 35 than the {bench_name}: {value}, against {bench}.",
        similar="People under 35 are about the same share as the {bench_name}: {value}, against {bench}.",
        pt_above="May point to: interest in family, youth and young-adult ministry.",
        pt_below=""),
    "median_hh_income": dict(plain_name="Median household income", style="dollars", theme="money", priority=5, short_above="higher incomes", short_below="lower incomes",
        above="Higher incomes than the {bench_name}: median {value}, against {bench}.",
        below="Lower incomes than the {bench_name}: median {value}, against {bench}.",
        similar="Median household income is about the same as the {bench_name}: {value}, against {bench}.",
        pt_above="",
        pt_below="Worth asking: which needs does the neighborhood have that the parish could help meet?"),
    "hh_below_poverty_pct": dict(plain_name="Households below the poverty level", style="percent", theme="money", priority=5, short_above="more poverty", short_below="less poverty",
        above="More households below the poverty level than the {bench_name}: {value}, against {bench}.",
        below="Fewer households below the poverty level than the {bench_name}: {value}, against {bench}.",
        similar="Households below the poverty level are about the same share as the {bench_name}: {value}, against {bench}.",
        pt_above="Worth asking: food, housing and emergency-help ministries nearby.",
        pt_below=""),
    "hh_snap_pct": dict(plain_name="Households receiving SNAP", style="percent", theme="money", priority=3, short_above="more SNAP households", short_below="fewer SNAP households",
        above="More households receiving SNAP than the {bench_name}: {value}, against {bench}.",
        below="Fewer households receiving SNAP than the {bench_name}: {value}, against {bench}.",
        similar="Households receiving SNAP are about the same share as the {bench_name}: {value}, against {bench}.",
        pt_above="Worth asking: is there a food pantry or meal program close by?",
        pt_below=""),
    "unemployment_rate": dict(plain_name="Unemployment", style="percent", theme="work", priority=4, short_above="higher unemployment", short_below="lower unemployment",
        above="Higher unemployment than the {bench_name}: {value}, against {bench}.",
        below="Lower unemployment than the {bench_name}: {value}, against {bench}.",
        similar="Unemployment is about the same as the {bench_name}: {value}, against {bench}.",
        pt_above="", pt_below=""),
    "commute_over_30_pct": dict(plain_name="Workers commuting 30 minutes or more", style="percent", theme="work", priority=2, short_above="longer commutes", short_below="shorter commutes",
        above="More long commutes than the {bench_name}: {value} of workers travel 30 minutes or more, against {bench}.",
        below="Fewer long commutes than the {bench_name}: {value} of workers, against {bench}.",
        similar="Commute times are about the same as the {bench_name}: {value} travel 30 minutes or more, against {bench}.",
        pt_above="May point to: busy weekday schedules; timing of programs may matter.",
        pt_below=""),
    "renter_share": dict(plain_name="Homes that are rented", style="percent", theme="housing", priority=4, short_above="more renters", short_below="more homeowners",
        above="More renters than the {bench_name}: {value} of occupied homes, against {bench}.",
        below="Fewer renters than the {bench_name}: {value} of occupied homes, against {bench}.",
        similar="Renting is about as common as in the {bench_name}: {value} of occupied homes, against {bench}.",
        pt_above="May point to: neighbors who move more often.",
        pt_below=""),
    "median_home_value": dict(plain_name="Median home value", style="dollars", theme="housing", priority=3, short_above="higher home values", short_below="lower home values",
        above="Higher home values than the {bench_name}: median {value}, against {bench}.",
        below="Lower home values than the {bench_name}: median {value}, against {bench}.",
        similar="Median home value is about the same as the {bench_name}: {value}, against {bench}.",
        pt_above="", pt_below=""),
    "rent_burden": dict(plain_name="Renters paying 30% or more of income", style="percent", theme="housing", priority=4, short_above="heavier rent burdens", short_below="lighter rent burdens",
        above="More renters stretched by rent than the {bench_name}: {value} pay 30% or more of income, against {bench}.",
        below="Fewer renters stretched by rent than the {bench_name}: {value} pay 30% or more, against {bench}.",
        similar="Rent burden is about the same as the {bench_name}: {value} pay 30% or more of income, against {bench}.",
        pt_above="Worth asking: housing stress is common here.", pt_below=""),
    "diversity_index": dict(plain_name="Diversity index", style="index", theme="people", priority=4, short_above="more diversity", short_below="less diversity",
        above="More diverse than the {bench_name}: index {value}, against {bench}.",
        below="Less diverse than the {bench_name}: index {value}, against {bench}.",
        similar="Diversity is about the same as the {bench_name}: index {value}, against {bench}.",
        pt_above="May point to: neighbors from many backgrounds; welcome and language matter.",
        pt_below=""),
    "no_english": dict(plain_name="Residents who speak no English", style="percent2", theme="people", priority=3, short_above="more non-English speakers", short_below="fewer non-English speakers",
        above="More residents who speak no English than the {bench_name}: {value}, against {bench}.",
        below="Fewer residents who speak no English than the {bench_name}: {value}, against {bench}.",
        similar="Residents who speak no English are about the same share as the {bench_name}: {value}, against {bench}.",
        pt_above="Worth asking: would services or materials in another language help?",
        pt_below=""),
    "college_share": dict(plain_name="Adults with a bachelor's degree or more", style="percent", theme="people", priority=3, short_above="more college-educated", short_below="fewer college degrees",
        above="More adults with a bachelor's degree than the {bench_name}: {value}, against {bench}.",
        below="Fewer adults with a bachelor's degree than the {bench_name}: {value}, against {bench}.",
        similar="Adults with a bachelor's degree are about the same share as the {bench_name}: {value}, against {bench}.",
        pt_above="", pt_below=""),
    "pop_cagr_2010_2020": dict(plain_name="Population growth per year", style="percent2", theme="growth", priority=4, short_above="faster growth", short_below="slower growth",
        above="Growing faster than the {bench_name}: {value} a year from 2010 to 2020, against {bench}.",
        below="Growing slower than the {bench_name}: {value} a year from 2010 to 2020, against {bench}.",
        similar="Growth is about the same as the {bench_name}: {value} a year from 2010 to 2020, against {bench}.",
        pt_above="May point to: new neighbors who do not yet know the parish.",
        pt_below="May point to: a shrinking pool of neighbors; welcome of newcomers may matter more."),
    "insured_pct": dict(plain_name="Residents with health coverage", style="percent", theme="health", priority=3, short_above="more people with coverage", short_below="more people without coverage",
        above="More residents with health coverage than the {bench_name}: {value}, against {bench}.",
        below="Fewer residents with health coverage than the {bench_name}: {value}, against {bench}.",
        similar="Health coverage is about the same as the {bench_name}: {value}, against {bench}.",
        pt_above="", pt_below="Worth asking: do neighbors need help finding care?"),
    "hh_with_disability_pct": dict(plain_name="Households with a person with a disability", style="percent", theme="health", priority=3, short_above="more disability", short_below="less disability",
        above="More households with a person with a disability than the {bench_name}: {value}, against {bench}.",
        below="Fewer households with a person with a disability than the {bench_name}: {value}, against {bench}.",
        similar="Households with a person with a disability are about the same share as the {bench_name}: {value}, against {bench}.",
        pt_above="Worth asking: is the building and the worship welcoming to everyone?", pt_below=""),
    "hh_no_internet_pct": dict(plain_name="Households without internet", style="percent", theme="health", priority=2, short_above="more homes without internet", short_below="fewer homes without internet",
        above="More households without internet than the {bench_name}: {value}, against {bench}.",
        below="Fewer households without internet than the {bench_name}: {value}, against {bench}.",
        similar="Households without internet are about the same share as the {bench_name}: {value}, against {bench}.",
        pt_above="May point to: online-only services may miss some neighbors.", pt_below=""),
    "avg_household_size": dict(plain_name="Average household size", style="dec", theme="people", priority=2, short_above="larger households", short_below="smaller households",
        above="Larger households than the {bench_name}: {value} people, against {bench}.",
        below="Smaller households than the {bench_name}: {value} people, against {bench}.",
        similar="Household size is about the same as the {bench_name}: {value} people, against {bench}.",
        pt_above="", pt_below=""),
}

SIMILAR_RELATIVE = 0.10        # within 10% of the comparison number counts as "about the same"
SIMILAR_POINTS = 1.5           # and a percentage must differ by at least this many points to count as different
MAX_PER_THEME = 2


def load_phrases(path: str | None = None) -> dict[str, dict]:
    """The built-in phrases, with any rows of the phrase CSV laid over them. CSV columns (written by CoWork):
    metric_key, plain_name, style, headline_above, headline_below, headline_similar, may_point_to_above, may_point_to_below,
    caution, priority, review_note. A blank cell keeps the built-in text."""
    phrases = {k: dict(v) for k, v in DEFAULT_PHRASES.items()}
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "neighborhood_insight_phrases.csv")
    if not os.path.exists(path):
        return phrases
    rename = {"headline_above": "above", "headline_below": "below", "headline_similar": "similar",
              "may_point_to_above": "pt_above", "may_point_to_below": "pt_below"}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = (row.get("metric_key") or "").strip()
            if not key:
                continue
            slot = phrases.setdefault(key, dict(plain_name=key, style="dec", theme="people", priority=1, above="", below="", similar="", pt_above="", pt_below=""))
            for col, val in row.items():
                val = (val or "").strip()
                if not val or col in ("metric_key", "review_note"):
                    continue
                name = rename.get(col, col)
                if name == "priority":
                    try:
                        slot["priority"] = int(val)
                    except ValueError:
                        pass
                else:
                    slot[name] = val
    return phrases


# ------------------------------------------------------------------------------------------------ the picking


def classify(style: str, value: float, bench: float) -> tuple[str, float]:
    """('above' | 'below' | 'similar', score). The score is how far apart the two numbers are, so bigger = more worth saying."""
    diff = value - bench
    scale = abs(bench) if bench else 1.0
    rel = diff / scale
    percentish = style in ("percent", "percent2")
    if abs(rel) < SIMILAR_RELATIVE or (percentish and abs(diff) < SIMILAR_POINTS):
        return "similar", abs(rel)
    return ("above" if diff > 0 else "below"), min(abs(rel), 1.5)


def stands_out(stats: dict[str, dict], bench_name: str, phrases: dict[str, dict] | None = None, max_items: int = 6) -> list[dict]:
    """stats = {metric_key: {"value": float, "bench": float, "reliability": str|None}}. Returns the biggest differences, most
    notable first: [{key, theme, direction, headline, may_point_to, rough, score}]. Unreliable numbers are never used."""
    phrases = phrases or load_phrases()
    cands = []
    for key, s in stats.items():
        ph = phrases.get(key)
        if not ph or s.get("value") is None or s.get("bench") is None or s.get("reliability") == "unreliable":
            continue
        direction, score = classify(ph["style"], float(s["value"]), float(s["bench"]))
        if direction == "similar":
            continue
        template = ph.get(direction) or ""
        if not template:
            continue
        diff = float(s["value"]) - float(s["bench"])
        headline = template.format(value=fmt_value(ph["style"], s["value"]), bench=fmt_value(ph["style"], s["bench"]), bench_name=bench_name,
                                   diff=fmt_value(ph["style"], abs(diff)))
        rough = s.get("reliability") == "use caution"
        cands.append({"key": key, "theme": ph.get("theme", ""), "direction": direction, "headline": headline,
                      "may_point_to": ph.get("pt_" + direction, "") or "", "rough": rough,
                      "short": ph.get("short_" + direction, ""),
                      "score": score * (ph.get("priority", 3) / 3.0) * (0.6 if rough else 1.0)})
    cands.sort(key=lambda c: -c["score"])
    picked, per_theme = [], {}
    for c in cands:
        if per_theme.get(c["theme"], 0) >= MAX_PER_THEME:
            continue
        per_theme[c["theme"]] = per_theme.get(c["theme"], 0) + 1
        picked.append(c)
        if len(picked) >= max_items:
            break
    return picked


def lead_sentence(parish: str, minutes: int, population, households, bench_name: str, items: list[dict]) -> str:
    """One plain sentence of context, then the top three differences in a few words each."""
    pop = fmt_value("count", population) if population is not None else None
    hh = fmt_value("count", households) if households is not None else None
    if pop and hh:
        first = f"About {pop} people live in {hh} households within a {minutes}-minute drive of {parish}."
    elif pop:
        first = f"About {pop} people live within a {minutes}-minute drive of {parish}."
    else:
        first = f"The area within a {minutes}-minute drive of {parish}."
    shorts = [i["short"] for i in items if i.get("short")][:3]
    if not shorts:
        return first
    if len(shorts) == 1:
        tail = shorts[0]
    else:
        tail = ", ".join(shorts[:-1]) + " and " + shorts[-1]
    return f"{first} Compared with the {bench_name}, it has {tail}."
