"""
neighborhood_snapshot.py -- builds the "Snapshot" view of the Neighborhood page (26-XXX Neighborhood Insights).

The Snapshot is the picture first: big-number tiles with a comparison bar, donuts, 10-by-10 grids, and a short "What stands out"
list. It reads the same numbers as the detailed cards (nothing new is computed from outside), so the two views always agree.

Pure: takes the already-loaded rows (values = {metric_key: [row, ...]}, bm = {level: {metric_key: {component: value}}}) and returns
plain dicts and lists for the template. No database and no web code here.

Comparison level: every tile carries all three comparisons (U.S., state, county) and the page shows the one the reader picked.
"""
from __future__ import annotations

import neighborhood_insights as ins
import neighborhood_viz as viz

LEVELS = ("us", "state", "county")

GENERATION_LABELS = {
    "Generation Alpha": "Generation Alpha (born 2013 or later)", "Generation Z": "Generation Z (1997-2012)", "Millennials": "Millennials (1981-1996)",
    "Generation X": "Generation X (1965-1980)", "Baby Boomers": "Baby Boomers (1946-1964)", "Silent and Greatest": "Silent and Greatest (1945 or earlier)",
}
# fixed color per group, so a group keeps its color whatever its size (see neighborhood_viz)
RACE_COLORS = {"White": 0, "Hispanic or Latino (any race)": 1, "Black or African American": 2, "Asian": 3, "Two or more races": 4,
               "Some other race": 5, "American Indian and Alaska Native": 6, "Native Hawaiian and Pacific Islander": 7}
RACE_SHORT = {"Hispanic or Latino (any race)": "Hispanic or Latino", "American Indian and Alaska Native": "American Indian or Alaska Native",
              "Native Hawaiian and Pacific Islander": "Native Hawaiian or Pacific Islander"}
EDUCATION_BANDS = (
    ("Less than high school", ("Less than 9th grade", "9th to 12th grade, no diploma")),
    ("High school or GED", ("High school graduate", "GED or alternative credential")),
    ("Some college or associate's", ("Some college, no degree", "Associate's degree")),
    ("Bachelor's degree", ("Bachelor's degree",)),
    ("Graduate or professional", ("Graduate or professional degree",)),
)


def _f(x):
    return None if x is None else float(x)


def _row(values, key, comp=""):
    for r in values.get(key, ()):
        if r["component"] == comp:
            return r
    return None


def _bench(bm, level, key, comp=""):
    return _f(bm.get(level, {}).get(key, {}).get(comp))


def _comps(values, key, skip=("", "top_bracket")):
    return [r for r in values.get(key, ()) if r["component"] not in skip]


def _bench_comps(bm, level, key, skip=("", "top_bracket")):
    return {c: _f(v) for c, v in bm.get(level, {}).get(key, {}).items() if c not in skip}


def _stat(value, reliability, benches, moe=None):
    return {"value": _f(value), "reliability": reliability, "benches": benches, "moe": _f(moe)}


def collect_stats(values: dict, bm: dict) -> dict[str, dict]:
    """Every single-number measure the Snapshot uses: {key: {value, reliability, moe, benches: {level: number or None}}}."""
    stats: dict[str, dict] = {}
    for key in ("total_population", "total_households", "housing_units", "avg_household_size", "median_hh_income", "median_home_value", "median_age",
                "diversity_index", "pop_cagr_2010_2020", "hh_cagr_2010_2020", "hh_below_poverty_pct", "hh_snap_pct", "hh_public_assistance_pct",
                "unemployment_rate", "commute_over_30_pct", "hh_with_disability_pct", "hh_no_internet_pct", "insured_pct", "routine_checkup_pct",
                "dental_visit_pct"):
        r = _row(values, key)
        if r is not None and r["value"] is not None:
            stats[key] = _stat(r["value"], r["reliability"], {lv: _bench(bm, lv, key) for lv in LEVELS}, r["moe"])
    for out_key, src in (("no_english", "no_english_by_age_language"), ("rent_burden", "rent_burden"), ("mortgage_burden", "mortgage_burden")):
        r = _row(values, src)
        if r is not None and r["value"] is not None:
            stats[out_key] = _stat(r["value"], r["reliability"], {lv: _bench(bm, lv, src) for lv in LEVELS}, r["moe"])
    # shares worked out from groups
    for out_key, comp in (("senior_share", "Wisdom (65+)"), ("youth_share", "Future (0-34)")):
        r = _row(values, "generational_balance", comp)
        if r is not None and r["value"] is not None:
            stats[out_key] = _stat(r["value"], r["reliability"], {lv: _bench(bm, lv, "generational_balance", comp) for lv in LEVELS}, r["moe"])
    ten = {r["component"]: r for r in _comps(values, "tenure")}
    if "Renter occupied" in ten and "Owner occupied" in ten:
        def renter(owner, rent):
            return rent / (owner + rent) * 100.0 if owner is not None and rent is not None and (owner + rent) else None
        v = renter(_f(ten["Owner occupied"]["value"]), _f(ten["Renter occupied"]["value"]))
        benches = {}
        for lv in LEVELS:
            b = bm.get(lv, {}).get("tenure", {})
            benches[lv] = renter(_f(b.get("Owner occupied")), _f(b.get("Renter occupied")))
        if v is not None:
            stats["renter_share"] = _stat(v, ten["Renter occupied"]["reliability"], benches)
    edu = {r["component"]: r for r in _comps(values, "educational_attainment")}
    if edu:
        total = sum(_f(r["value"]) or 0 for r in edu.values())
        top = [edu[c] for c in ("Bachelor's degree", "Graduate or professional degree") if c in edu]
        if total and top:
            benches = {}
            for lv in LEVELS:
                b = {c: _f(v) for c, v in bm.get(lv, {}).get("educational_attainment", {}).items() if c not in ("", "top_bracket")}
                t = sum(x or 0 for x in b.values())
                benches[lv] = (sum(b.get(c) or 0 for c in ("Bachelor's degree", "Graduate or professional degree")) / t * 100.0) if t else None
            rel = "unreliable" if any(r["reliability"] == "unreliable" for r in top) else ("use caution" if any(r["reliability"] == "use caution" for r in top) else "ok")
            stats["college_share"] = _stat(sum(_f(r["value"]) or 0 for r in top) / total * 100.0, rel, benches)
    return stats


# ------------------------------------------------------------------------------------------------ tiles


def _level_names(geo_names: dict) -> dict[str, str]:
    return {"us": geo_names.get("us", "U.S."), "state": geo_names.get("state", "State"), "county": geo_names.get("county", "County")}


def make_tile(key: str, icon_name: str, label: str, style: str, stats: dict, names: dict, sub: str = "", bar: bool = True, big: bool = False,
              compare: bool = True) -> dict | None:
    s = stats.get(key)
    if not s or s["value"] is None:
        return None
    display = ins.fmt_value(style, s["value"])
    cmp = {}
    for lv in LEVELS:
        b = s["benches"].get(lv) if compare else None
        cmp[lv] = {"has": b is not None, "label": names[lv], "display": ins.fmt_value(style, b) if b is not None else ""}
    tip_bits = [f"{label}: {display}" + (f" (the real figure is probably within {ins.fmt_value(style, s['moe'])} of this)" if s.get("moe") else "")]
    tip_bits += [f"{names[lv]} {cmp[lv]['display']}" for lv in LEVELS if cmp[lv]["has"]]
    bullet = viz.Markup("")
    if bar and compare:
        vmax = viz.bullet_scale(s["value"], [s["benches"].get(lv) for lv in LEVELS])
        bullet = viz.bullet(s["value"], {lv: s["benches"].get(lv) for lv in LEVELS}, vmax, label=f"{label}: {display}")
    return {"key": key, "icon": viz.icon(icon_name), "label": label, "sub": sub, "display": display, "cmp": cmp, "bullet": bullet, "big": big, "compare": compare,
            "flag": s["reliability"] if s["reliability"] in ("use caution", "unreliable") else "", "tip": ". ".join(tip_bits) + "."}


def make_plain_tile(icon_name: str, label: str, display: str, sub: str = "") -> dict:
    """A tile with no comparison (a count like the number of schools)."""
    return {"key": label, "icon": viz.icon(icon_name), "label": label, "sub": sub, "display": display, "cmp": {lv: {"has": False} for lv in LEVELS},
            "bullet": viz.Markup(""), "big": False, "compare": False, "flag": "", "tip": f"{label}: {display}."}


# ------------------------------------------------------------------------------------------------ charts


def _legend(parts: list[dict], total: float | None = None, show_count: bool = False) -> list[dict]:
    t = total if total is not None else (sum(p["value"] for p in parts if p.get("value")) or 1.0)
    out = []
    for p in parts:
        pct = (p["value"] / t * 100.0) if p.get("value") is not None else None
        out.append({"label": p["label"], "color": p["color"], "pct": ins.fmt_value("percent", pct) if pct is not None else "n/a",
                    "count": ins.fmt_value("count", p["value"]) if show_count and p.get("value") is not None else "",
                    "flag": p.get("flag", "")})
    return out


def generations_chart(values, stats) -> dict | None:
    rows = _comps(values, "generations")
    if not rows:
        return None
    colors = viz.ramp_colors(len(rows))
    parts = [{"label": GENERATION_LABELS.get(r["component"], r["component"]), "value": _f(r["value"]) or 0.0, "color": colors[i],
              "display": ins.fmt_value("count", r["value"]), "flag": r["reliability"] if r["reliability"] in ("use caution", "unreliable") else ""}
             for i, r in enumerate(rows)]
    age = stats.get("median_age")
    center = ins.fmt_value("years", age["value"]) if age else ""
    return {"title": "Generations", "svg": viz.donut(parts, center_big=center, center_small="median age" if center else "", label="Population by generation"),
            "legend": _legend(parts), "note": "Ages as of 2022. Ordered from youngest (lightest) to oldest (darkest)."}


def race_chart(values, stats) -> dict | None:
    rows = _comps(values, "race_ethnicity")
    if not rows:
        return None
    parts = []
    for r in sorted(rows, key=lambda r: -(_f(r["value"]) or 0)):
        name = r["component"]
        parts.append({"label": RACE_SHORT.get(name, name), "value": _f(r["value"]) or 0.0, "color": viz.CATEGORICAL[RACE_COLORS.get(name, 7)],
                      "display": ins.fmt_value("count", r["value"]), "flag": r["reliability"] if r["reliability"] in ("use caution", "unreliable") else ""})
    d = stats.get("diversity_index")
    center = ins.fmt_value("index", d["value"]) if d else ""
    us = d["benches"].get("us") if d else None
    return {"title": "Race and ethnicity", "svg": viz.donut(parts, center_big=center, center_small="diversity index" if center else "", label="Population by race and Hispanic origin"),
            "legend": _legend(parts), "note": ("U.S. diversity index " + ins.fmt_value("index", us) + ". 0 = everyone alike, 100 = every pair of people differs." if us is not None else "")}


def balance_bars(values, bm, names) -> dict | None:
    rows = _comps(values, "generational_balance")
    if not rows:
        return None
    out = []
    allnums = []
    for r in rows:
        allnums.append(_f(r["value"]) or 0.0)
        allnums += [x for x in (_bench(bm, lv, "generational_balance", r["component"]) for lv in LEVELS) if x is not None]
    vmax = (max(allnums) * 1.15) if allnums else 100.0

    def at(x):
        return round(max(0.0, min(100.0, x / vmax * 100.0)), 1) if x is not None else None
    for r in rows:
        v = _f(r["value"])
        ticks = {lv: _bench(bm, lv, "generational_balance", r["component"]) for lv in LEVELS}
        out.append({"label": r["component"], "pct": v, "width": at(v) or 0, "display": ins.fmt_value("percent", v),
                    "ticks": {lv: at(t) for lv, t in ticks.items()},
                    "tick_displays": {lv: (ins.fmt_value("percent", t) if t is not None else "") for lv, t in ticks.items()},
                    "flag": r["reliability"] if r["reliability"] in ("use caution", "unreliable") else ""})
    return {"title": "Generational balance", "rows": out, "names": names,
            "note": "Share of residents in each life stage. The orange tick is the comparison."}


def employment_chart(values, stats) -> dict | None:
    rows = _comps(values, "collar_mix")
    if not rows:
        return None
    order = {"White collar": 0, "Services": 1, "Blue collar": 2}
    parts = [{"label": r["component"], "value": _f(r["value"]) or 0.0, "color": viz.CATEGORICAL[order.get(r["component"], 2)],
              "display": ins.fmt_value("percent", r["value"]), "flag": r["reliability"] if r["reliability"] in ("use caution", "unreliable") else ""}
             for r in sorted(rows, key=lambda r: order.get(r["component"], 9))]
    u = stats.get("unemployment_rate")
    center = ins.fmt_value("percent", u["value"]) if u else ""
    return {"title": "Where residents work", "svg": viz.donut(parts, center_big=center, center_small="unemployed" if center else "", label="Employed residents by kind of work"),
            "legend": _legend(parts), "note": "Share of employed residents by kind of job. The middle number is the unemployment rate."}


def education_chart(values) -> dict | None:
    by = {r["component"]: r for r in _comps(values, "educational_attainment")}
    if not by:
        return None
    colors = viz.ramp_colors(len(EDUCATION_BANDS))
    parts = []
    for i, (label, comps) in enumerate(EDUCATION_BANDS):
        v = sum(_f(by[c]["value"]) or 0 for c in comps if c in by)
        rels = [by[c]["reliability"] for c in comps if c in by]
        flag = "unreliable" if "unreliable" in rels else ("use caution" if "use caution" in rels else "")
        parts.append({"label": label, "value": v, "color": colors[i], "display": ins.fmt_value("count", v), "flag": flag})
    total = sum(p["value"] for p in parts)
    return {"title": "Schooling of adults 25 and over", "svg": viz.waffle(parts, "square", label="Adults by highest schooling"), "legend": _legend(parts, total),
            "note": "Each square is 1% of adults 25 and over. Lightest = least schooling."}


def enrollment_chart(values) -> dict | None:
    rows = [r for r in _comps(values, "school_enrollment") if r["component"].split(" | ")[0].startswith(("Preschool", "Kindergarten", "Grades"))]
    if not rows:
        return None
    pub = sum(_f(r["value"]) or 0 for r in rows if r["component"].endswith("| public"))
    pri = sum(_f(r["value"]) or 0 for r in rows if r["component"].endswith("| private"))
    if not (pub + pri):
        return None
    parts = [{"label": "Public school", "value": pub, "color": viz.CATEGORICAL[0], "display": ins.fmt_value("count", pub)},
             {"label": "Private school", "value": pri, "color": viz.CATEGORICAL[1], "display": ins.fmt_value("count", pri)}]
    return {"title": "Children in school (pre-K to grade 12)", "svg": viz.donut(parts, center_big=ins.fmt_value("count", pub + pri), center_small="enrolled", label="Pre-K to 12 enrollment, public and private"),
            "legend": _legend(parts), "note": "Residents of the area, wherever they go to school."}


def tenure_chart(values, stats) -> dict | None:
    rows = {r["component"]: r for r in _comps(values, "tenure")}
    if not rows:
        return None
    spec = (("Owner occupied", "Owned", viz.CATEGORICAL[0]), ("Renter occupied", "Rented", viz.CATEGORICAL[1]), ("Vacant", "Vacant", viz.NEUTRAL))
    parts = [{"label": label, "value": _f(rows[c]["value"]) or 0.0, "color": color, "display": ins.fmt_value("count", rows[c]["value"])}
             for c, label, color in spec if c in rows]
    units = stats.get("housing_units")
    return {"title": "Homes: owned, rented or empty", "svg": viz.waffle(parts, "house", label="Housing units by tenure"), "legend": _legend(parts),
            "note": (f"{ins.fmt_value('count', units['value'])} homes in the area. Each house is 1%." if units else "Each house is 1% of homes.")}


def daytime_chart(values) -> dict | None:
    head = _row(values, "daytime_population")
    rows = {r["component"]: r for r in _comps(values, "daytime_population")}
    if not head or not rows:
        return None
    parts = [{"label": "People who work here", "value": _f(rows["Workers"]["value"]) or 0.0, "color": viz.CATEGORICAL[0], "display": ins.fmt_value("count", rows["Workers"]["value"])}
             for _ in [0] if "Workers" in rows]
    if "Residents who stay" in rows:
        parts.append({"label": "Residents who stay", "value": _f(rows["Residents who stay"]["value"]) or 0.0, "color": viz.CATEGORICAL[2],
                      "display": ins.fmt_value("count", rows["Residents who stay"]["value"])})
    return {"title": "People here on a workday", "svg": viz.waffle(parts, "person", label="Daytime population"), "legend": _legend(parts),
            "note": f"About {ins.fmt_value('count', head['value'])} people. Each figure is 1%. Jobs in the area plus residents who do not leave for work."}


# ------------------------------------------------------------------------------------------------ the whole Snapshot


def build(values: dict, bm: dict, geo_names: dict, parish_name: str, minutes: int, schools_total: int | None = None,
          nearby_count: int | None = None, phrases: dict | None = None) -> dict:
    names = _level_names(geo_names)
    stats = collect_stats(values, bm)
    phrases = phrases or ins.load_phrases()

    stands, lead = {}, {}
    pop = stats.get("total_population", {}).get("value")
    hh = stats.get("total_households", {}).get("value")
    for lv in LEVELS:
        bench_name = f"{names[lv]} average"
        picks = ins.stands_out({k: {"value": s["value"], "bench": s["benches"].get(lv), "reliability": s["reliability"]} for k, s in stats.items()},
                               bench_name, phrases)
        stands[lv] = picks
        lead[lv] = ins.lead_sentence(parish_name, minutes, pop, hh, f"{names[lv]} average", picks)

    t = lambda *a, **k: make_tile(*a, stats=stats, names=names, **k)
    top = [x for x in (
        t("total_population", "people", "Total population", "count", big=True, compare=False),
        t("total_households", "house", "Households", "count", compare=False),
        t("avg_household_size", "pair", "Household size (average)", "dec"),
        t("median_hh_income", "dollar", "Household income (median)", "dollars"),
        t("pop_cagr_2010_2020", "growth", "Population growth per year", "percent2", sub="2010 to 2020", bar=False),
    ) if x]
    grid = [x for x in (
        t("hh_below_poverty_pct", "coin_down", "Households below the poverty level", "percent"),
        t("unemployment_rate", "briefcase", "Unemployment", "percent"),
        t("commute_over_30_pct", "car", "Workers commuting 30 minutes or more", "percent"),
        t("hh_with_disability_pct", "access", "Households with a person with a disability", "percent"),
        t("hh_no_internet_pct", "wifi", "Households without internet", "percent"),
        t("no_english", "speech", "Residents who speak no English", "percent2"),
        t("median_home_value", "house", "Home value (median)", "dollars"),
        t("rent_burden", "percent", "Renters paying 30% or more of income", "percent"),
        t("mortgage_burden", "percent", "Owners paying 30% or more of income", "percent"),
        t("insured_pct", "heart", "Residents with health coverage", "percent"),
        t("routine_checkup_pct", "plus", "Adults with a checkup in the past year", "percent"),
        t("dental_visit_pct", "tooth", "Adults with a dental visit in the past year", "percent"),
        t("hh_snap_pct", "cart", "Households receiving SNAP", "percent"),
    ) if x]
    if schools_total:
        grid.append(make_plain_tile("school", "Schools in this area", ins.fmt_value("count", schools_total), "public and private"))
    if nearby_count is not None:
        grid.append(make_plain_tile("church", "Other parishes in this area", ins.fmt_value("count", nearby_count), "known to Beacon"))

    return {
        "levels": [{"key": lv, "name": names[lv]} for lv in LEVELS],
        "lead": lead, "stands": stands, "top": top, "grid": grid,
        "generations": generations_chart(values, stats), "race": race_chart(values, stats), "balance": balance_bars(values, bm, names),
        "employment": employment_chart(values, stats), "education": education_chart(values), "enrollment": enrollment_chart(values),
        "tenure": tenure_chart(values, stats), "daytime": daytime_chart(values),
        "any_flagged": any(s["reliability"] in ("use caution", "unreliable") for s in stats.values()),
    }
