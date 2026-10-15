"""
parish_neighborhood.py -- the "Neighborhood" page in Parish Mode (26-XXX Neighborhood Insights, Phase 5).

Shows, for a parish's own church site, who lives within a 7 and a 15 minute drive: population, households, income, age,
race and ethnicity, language, commute, housing, education, health and more, each with the county, state and U.S. comparison,
a source and vintage footnote, and a reliability flag where the Census margin of error is large. The numbers come from the
nbhd schema (built by 26-XXX Neighborhood Insights from public Census data) and are only READ here: nothing on this page
writes to the database.

New file per NFR-11 / the standing main.py rule: main.py gets one import and one register line. Same shape as parish_info.py.

Who sees it (PLAN.md decision 3): anyone who reaches a parish in Parish Mode, the parish's own users and diocesan staff
previewing it. The parish is always the one Parish Mode resolved; no parish id is ever taken from the request. Every query
joins on the parish's own org as well (defense in depth). The parish's pledge figure is shown only to people who can see the
parish's finance screens.

A parish can be switched off with portal.parishes.modules {"neighborhood": false}. The default is on, but the page and its
Parish Home tile only appear for a parish whose church location has been confirmed and whose diocese has a finished run.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db
import neighborhood_snapshot
import parish_finance
import parish_mode

router = APIRouter()

_current_user = None
_render = None

STATE_NAMES = {
    "01": "Alabama", "02": "Alaska", "04": "Arizona", "05": "Arkansas", "06": "California", "08": "Colorado", "09": "Connecticut",
    "10": "Delaware", "11": "District of Columbia", "12": "Florida", "13": "Georgia", "15": "Hawaii", "16": "Idaho", "17": "Illinois",
    "18": "Indiana", "19": "Iowa", "20": "Kansas", "21": "Kentucky", "22": "Louisiana", "23": "Maine", "24": "Maryland",
    "25": "Massachusetts", "26": "Michigan", "27": "Minnesota", "28": "Mississippi", "29": "Missouri", "30": "Montana",
    "31": "Nebraska", "32": "Nevada", "33": "New Hampshire", "34": "New Jersey", "35": "New Mexico", "36": "New York",
    "37": "North Carolina", "38": "North Dakota", "39": "Ohio", "40": "Oklahoma", "41": "Oregon", "42": "Pennsylvania",
    "44": "Rhode Island", "45": "South Carolina", "46": "South Dakota", "47": "Tennessee", "48": "Texas", "49": "Utah",
    "50": "Vermont", "51": "Virginia", "53": "Washington", "54": "West Virginia", "55": "Wisconsin", "56": "Wyoming",
}

# ---------------------------------------------------------------------------------------------- what each section shows


def _stat(key, title, fmt):
    return {"key": key, "title": title, "kind": "stat", "fmt": fmt}


def _bars(key, title, values="count", headline_fmt=None, headline_label=None, sort=None):
    """values: 'count' (percent of the card's total is worked out here) or 'pct' (the numbers already are percents)."""
    return {"key": key, "title": title, "kind": "bars", "values": values, "headline_fmt": headline_fmt,
            "headline_label": headline_label, "sort": sort}


SECTIONS = [
    ("Community Overview", [
        _stat("total_population", "Total population", "count"),
        _stat("total_households", "Households", "count"),
        _stat("avg_household_size", "Average household size", "dec2"),
    ]),
    ("Population and Household Growth", [
        _stat("pop_cagr_2010_2020", "Population growth per year, 2010 to 2020", "pct2"),
        _stat("pop_cagr_2000_2020", "Population growth per year, 2000 to 2020", "pct2"),
        _stat("hh_cagr_2010_2020", "Household growth per year, 2010 to 2020", "pct2"),
        _stat("hh_cagr_2000_2020", "Household growth per year, 2000 to 2020", "pct2"),
    ]),
    ("Household Composition", [
        _bars("hh_family_by_size", "Family households by size"),
        _bars("hh_nonfamily_by_size", "Nonfamily households by size"),
    ]),
    ("Household Income", [
        _stat("median_hh_income", "Median household income", "dollars"),
        _bars("hh_income_distribution", "Households by income"),
    ]),
    ("Age and Generations", [
        _stat("median_age", "Median age", "dec1"),
        _bars("generations", "Population by generation"),
        _bars("generational_balance", "Future, current and wisdom years", values="pct"),
        _stat("child_dependency_ratio", "Child dependency ratio", "dec0"),
        _stat("senior_dependency_ratio", "Senior dependency ratio", "dec0"),
        {"key": "age_by_sex", "title": "Population by age and sex", "kind": "pyramid"},
    ]),
    ("Race and Ethnicity", [
        _bars("race_ethnicity", "Population by race and Hispanic origin"),
        _stat("diversity_index", "Diversity index (0 to 100)", "dec0"),
    ]),
    ("Language", [
        _bars("no_english_by_age_language", "Residents who speak no English", headline_fmt="pct", headline_label="Share of residents age 5 and over"),
    ]),
    ("Commute", [
        _bars("commute_time_distribution", "Workers by travel time to work"),
        _stat("commute_over_30_pct", "Workers commuting 30 minutes or more", "pct"),
    ]),
    ("Economic Need", [
        _stat("hh_below_poverty_pct", "Households below the poverty level", "pct"),
        _stat("hh_snap_pct", "Households receiving SNAP", "pct"),
        _stat("hh_public_assistance_pct", "Households with public assistance income", "pct"),
    ]),
    ("Employment", [
        _stat("unemployment_rate", "Unemployment rate", "pct"),
        _bars("occupation_22", "Employed residents by occupation", sort="value"),
        _bars("collar_mix", "White collar, services and blue collar", values="pct"),
    ]),
    ("Housing", [
        _stat("housing_units", "Housing units", "count"),
        _bars("tenure", "Owner, renter and vacant"),
        _bars("year_built", "Housing units by year built"),
        _bars("rent_burden", "Renters by rent as a share of income", headline_fmt="pct", headline_label="Paying 30% or more"),
        _bars("mortgage_burden", "Owners with a mortgage by cost as a share of income", headline_fmt="pct", headline_label="Paying 30% or more"),
        _bars("home_value_distribution", "Owner-occupied homes by value"),
        _stat("median_home_value", "Median home value", "dollars"),
    ]),
    ("Education and Schools", [
        _bars("educational_attainment", "Adults 25 and over by education"),
        _bars("school_enrollment", "School enrollment by level"),
    ]),
    ("Health", [
        _stat("insured_pct", "Residents with health coverage", "pct"),
        _stat("routine_checkup_pct", "Adults with a routine checkup in the past year", "pct"),
        _stat("dental_visit_pct", "Adults with a dental visit in the past year", "pct"),
        _bars("insurance_payor_mix", "Health insurance by age and type"),
        _stat("hh_with_disability_pct", "Households with a person with a disability", "pct"),
    ]),
    ("Connectivity", [
        _stat("hh_no_internet_pct", "Households without internet access", "pct"),
    ]),
    ("Daytime Population", [
        _bars("daytime_population", "People in the area on a workday", headline_fmt="count", headline_label="Daytime population: workers plus residents who stay"),
    ]),
]


def _fmt(value, fmt: str) -> str:
    if value is None:
        return "n/a"
    v = float(value)
    if fmt == "count":
        return f"{round(v):,}"
    if fmt == "pct":
        return f"{v:.1f}%"
    if fmt == "dollars":
        return f"${round(v):,}"
    if fmt == "pct2":
        return f"{v:.2f}%"
    if fmt == "dec0":
        return f"{v:.0f}"
    if fmt == "dec1":
        return f"{v:.1f}"
    if fmt == "dec2":
        return f"{v:.2f}"
    return f"{v:,.1f}"


# ---------------------------------------------------------------------------------------------- data access


def _modules(parish: dict) -> dict:
    m = parish.get("modules") or {}
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError:
            m = {}
    return m if isinstance(m, dict) else {}


def _switched_off(parish: dict) -> bool:
    return _modules(parish).get("neighborhood") is False


def _site(parish: dict):
    return db.query_one(
        "SELECT id, address_used, lat, lon FROM nbhd.site "
        "WHERE parish_id = %s AND org_id = %s AND is_primary AND verified_at IS NOT NULL",
        (parish["id"], parish["org_id"]),
    )


def _latest_run(org_id: int):
    return db.query_one(
        "SELECT id, finished_at, acs_vintage FROM nbhd.profile_run WHERE org_id = %s AND status = 'success' "
        "ORDER BY finished_at DESC LIMIT 1", (org_id,),
    )


def _rings(site_id: int) -> list[dict]:
    return db.query("SELECT id, minutes FROM nbhd.isochrone WHERE site_id = %s AND is_current ORDER BY minutes", (site_id,))


def neighborhood_tile(user, parish) -> dict:
    """{show, href, label, desc} for the Parish Home tile. Shown only for a parish with a confirmed church location, a
    finished run for its diocese and drive-time areas, and not switched off. Never raises: a tile must not break its page."""
    try:
        if not user or not parish or _switched_off(parish):
            return {"show": False}
        row = db.query_one(
            "SELECT 1 AS ok FROM nbhd.site s WHERE s.parish_id = %s AND s.org_id = %s AND s.is_primary AND s.verified_at IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM nbhd.isochrone i WHERE i.site_id = s.id AND i.is_current) "
            "AND EXISTS (SELECT 1 FROM nbhd.profile_run r WHERE r.org_id = s.org_id AND r.status = 'success')",
            (parish["id"], parish["org_id"]),
        )
        if not row:
            return {"show": False}
        return {"show": True, "href": "/neighborhood", "label": "Neighborhood",
                "desc": "Who lives within a short drive of your church, from public Census data."}
    except Exception:
        return {"show": False}


# ---------------------------------------------------------------------------------------------- building the cards


def _values_by_metric(run_id: int, isochrone_id: int) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in db.query(
        "SELECT metric_key, component, value, moe, cv, reliability, source_label, seq FROM nbhd.profile_value "
        "WHERE run_id = %s AND isochrone_id = %s ORDER BY metric_key, seq NULLS LAST, component", (run_id, isochrone_id),
    ):
        out.setdefault(r["metric_key"], []).append(r)
    return out


def _benchmarks(run_id: int, county: str | None, state: str | None) -> dict[str, dict[str, dict[str, float]]]:
    """{'county'|'state'|'us': {metric_key: {component: value}}}."""
    out: dict[str, dict[str, dict[str, float]]] = {"county": {}, "state": {}, "us": {}}
    rows = db.query(
        "SELECT geo_level, geoid, metric_key, component, value FROM nbhd.benchmark_value WHERE run_id = %s AND "
        "((geo_level = 'county' AND geoid = %s) OR (geo_level = 'state' AND geoid = %s) OR (geo_level = 'us' AND geoid = '1'))",
        (run_id, county or "", state or ""),
    )
    for r in rows:
        out[r["geo_level"]].setdefault(r["metric_key"], {})[r["component"]] = r["value"]
    return out


def _flag(reliability: str | None) -> str:
    return reliability if reliability in ("use caution", "unreliable") else ""


def _pct_of_total(components: dict[str, float]) -> dict[str, float | None]:
    total = sum(float(v) for v in components.values() if v is not None)
    return {c: (float(v) / total * 100.0 if total and v is not None else None) for c, v in components.items()}


def _stat_card(cfg: dict, rows: list[dict], bm: dict, geo_names: dict) -> dict | None:
    head = next((r for r in rows if r["component"] == ""), None)
    if head is None:
        return None
    top = any(r["component"] == "top_bracket" for r in rows)
    display = _fmt(head["value"], cfg["fmt"]) + ("+" if top and head["value"] is not None else "")
    compare = []
    for level in ("county", "state", "us"):
        v = bm[level].get(cfg["key"], {}).get("")
        if v is not None:
            compare.append({"label": geo_names[level], "display": _fmt(v, cfg["fmt"])})
    return {
        "kind": "stat", "key": cfg["key"], "title": cfg["title"], "display": display,
        "moe": _fmt(head["moe"], cfg["fmt"]) if head["moe"] is not None else None,
        "flag": _flag(head["reliability"]), "compare": compare, "source": head["source_label"],
    }


def _bars_card(cfg: dict, rows: list[dict], bm: dict, geo_names: dict) -> dict | None:
    parts = [r for r in rows if r["component"] not in ("", "top_bracket")]
    if not parts:
        return None
    count_mode = cfg["values"] == "count"
    total = sum(float(r["value"] or 0) for r in parts)
    bench = {}
    for level in ("county", "state", "us"):
        comps = bm[level].get(cfg["key"], {})
        comps = {c: v for c, v in comps.items() if c not in ("", "top_bracket")}
        bench[level] = _pct_of_total(comps) if count_mode else {c: (float(v) if v is not None else None) for c, v in comps.items()}
    out_rows = []
    for r in parts:
        value = float(r["value"]) if r["value"] is not None else None
        pct = (value / total * 100.0 if total and value is not None else None) if count_mode else value
        out_rows.append({
            "label": r["component"], "count": _fmt(value, "count") if count_mode else None, "pct": pct,
            "pct_display": _fmt(pct, "pct"), "flag": _flag(r["reliability"]),
            "compare": [bench[level].get(r["component"]) for level in ("county", "state", "us")],
        })
    if cfg.get("sort") == "value":
        out_rows.sort(key=lambda x: -(x["pct"] or 0))
    top = max((x["pct"] or 0) for x in out_rows) or 1
    for x in out_rows:
        x["width"] = round((x["pct"] or 0) / top * 100)
        x["compare_display"] = [_fmt(v, "pct") if v is not None else "" for v in x["compare"]]
    head = next((r for r in rows if r["component"] == ""), None)
    headline = None
    if head is not None and cfg.get("headline_fmt"):
        headline = {"label": cfg.get("headline_label") or "", "display": _fmt(head["value"], cfg["headline_fmt"]),
                    "flag": _flag(head["reliability"]),
                    "compare": [{"label": geo_names[lv], "display": _fmt(bm[lv].get(cfg["key"], {}).get(""), cfg["headline_fmt"])}
                                for lv in ("county", "state", "us") if bm[lv].get(cfg["key"], {}).get("") is not None]}
    return {
        "kind": "bars", "key": cfg["key"], "title": cfg["title"], "rows": out_rows, "headline": headline,
        "columns": [geo_names[lv] for lv in ("county", "state", "us")], "has_compare": any(bench[lv] for lv in bench),
        "source": parts[0]["source_label"],
    }


def _pyramid_card(cfg: dict, rows: list[dict], bm: dict, geo_names: dict) -> dict | None:
    """Population by age band, male and female side by side, each as a share of everyone."""
    parts = [r for r in rows if r["component"]]
    if not parts:
        return None
    total = sum(float(r["value"] or 0) for r in parts) or 1.0
    bands: dict[str, dict] = {}
    for r in parts:
        sex, _, band = r["component"].partition(" ")
        slot = bands.setdefault(band, {"band": band})
        pct = float(r["value"] or 0) / total * 100.0
        slot[sex.lower()] = pct
        slot[sex.lower() + "_flag"] = _flag(r["reliability"])
    top = max((max(b.get("male", 0), b.get("female", 0)) for b in bands.values()), default=1) or 1
    out = [{"band": b["band"], "male": round(b.get("male", 0), 1), "female": round(b.get("female", 0), 1),
            "male_width": round(b.get("male", 0) / top * 100), "female_width": round(b.get("female", 0) / top * 100),
            "male_flag": b.get("male_flag", ""), "female_flag": b.get("female_flag", "")} for b in bands.values()]
    return {"kind": "pyramid", "key": cfg["key"], "title": cfg["title"], "rows": out, "source": parts[0]["source_label"]}


def _sections(values: dict, bm: dict, geo_names: dict) -> list[dict]:
    builders = {"stat": _stat_card, "bars": _bars_card, "pyramid": _pyramid_card}
    out = []
    for title, cards in SECTIONS:
        built = []
        for cfg in cards:
            rows = values.get(cfg["key"])
            if not rows:
                continue
            card = builders[cfg["kind"]](cfg, rows, bm, geo_names)
            if card:
                built.append(card)
        if built:
            out.append({"title": title, "cards": built})
    return out


# ---------------------------------------------------------------------------------------------- live sections (no stored run needed)


# Which other parishes may be shown: active, in the same diocese as the parish being viewed, and not switched off.
_VISIBLE_NEIGHBOR = ("p.is_active AND p.org_id = %s AND COALESCE(p.modules->>'neighborhood', 'true') <> 'false'")


def _nearby_parishes(site_id: int, org_id: int) -> list[dict]:
    return db.query(
        "SELECT p.id AS parish_id, p.name, o.code AS org_code, round(ST_Distance(s1.pt::geography, s2.pt::geography)::numeric / 1609.344, 1) AS miles, "
        "min(i.minutes) AS within_minutes "
        "FROM nbhd.site s1 "
        "JOIN nbhd.isochrone i ON i.site_id = s1.id AND i.is_current "
        "JOIN nbhd.site s2 ON s2.is_primary AND s2.verified_at IS NOT NULL AND s2.parish_id IS NOT NULL AND s2.id <> s1.id AND ST_Covers(i.geom, s2.pt) "
        "JOIN portal.parishes p ON p.id = s2.parish_id "
        "JOIN checkreq.organizations o ON o.id = p.org_id "
        f"WHERE s1.id = %s AND {_VISIBLE_NEIGHBOR} "
        "GROUP BY p.id, p.name, o.code, s1.pt, s2.pt "
        "ORDER BY min(i.minutes), ST_Distance(s1.pt::geography, s2.pt::geography), p.name",
        (site_id, org_id),
    )


_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def _schools_card(site_id: int, minutes: int) -> dict | None:
    """The nearest schools inside the chosen drive-time area (live query, no stored run needed): a list card for Education and Schools."""
    rows = db.query(
        "SELECT sc.name, sc.level, sc.is_private, round((ST_Distance(s.pt::geography, sc.pt::geography) / 1609.344)::numeric, 1) AS miles, "
        "degrees(ST_Azimuth(s.pt::geography, sc.pt::geography)) AS az, count(*) OVER () AS total "
        "FROM nbhd.site s JOIN nbhd.isochrone i ON i.site_id = s.id AND i.is_current AND i.minutes = %s "
        "JOIN nbhd.school_location sc ON ST_Covers(i.geom, sc.pt) WHERE s.id = %s "
        "ORDER BY ST_Distance(s.pt::geography, sc.pt::geography), sc.name LIMIT 12", (minutes, site_id),
    )
    if not rows:
        return None
    items = []
    for r in rows:
        kind = ("Private" if r["is_private"] else "Public") + (f" {r['level'].lower()}" if r["level"] else "")
        items.append({"label": r["name"], "detail": f"{kind}, {r['miles']} miles {_COMPASS[int(((float(r['az']) % 360) + 22.5) // 45) % 8]}"})
    return {"kind": "list", "key": "nearby_schools", "total": int(rows[0]["total"]), "title": f"Schools nearest the church ({rows[0]['total']} in this area)", "entries": items,
            "source": "NCES public school locations 2023-24 and the NCES Private School Survey 2021-22 (a sample: some private schools are not listed)"}


def _parochial(parish: dict, user) -> dict | None:
    ueid = parish.get("parochial_report_id")
    if not ueid:
        return None
    row = db.query_one("SELECT * FROM nbhd.parish_parochial_latest WHERE parish_ueid = %s", (ueid,))
    if not row:
        return None
    try:
        show_pledges = parish_finance.can_view_finance(user, parish)
    except Exception:
        show_pledges = False
    out = {"year": row["filing_year"], "members": row["members_this_year"], "attendance": row["average_sunday_attendance"], "pledges": None}
    if show_pledges and row.get("amount_pledged") is not None:
        out["pledges"] = _fmt(row["amount_pledged"], "dollars")
    out["members"] = _fmt(out["members"], "count") if out["members"] is not None else None
    out["attendance"] = _fmt(out["attendance"], "count") if out["attendance"] is not None else None
    return out


# ---------------------------------------------------------------------------------------------- routes


def register(app, *, current_user, render, templates=None) -> None:
    global _current_user, _render
    _current_user, _render = current_user, render
    app.include_router(router)
    if templates is not None:  # the Parish Home tile reads this global; it never raises
        templates.env.globals["neighborhood_tile"] = neighborhood_tile


def _resolve(request: Request):
    """(user, parish, redirect). The same gate Parish Information uses: signed in, and a parish resolved by Parish Mode."""
    user = _current_user(request)
    if not user:
        return None, None, RedirectResponse("/login")
    parish, _is_preview = parish_mode.effective_parish_mode(request, user)
    if not parish:
        return user, None, RedirectResponse("/parish-view")
    return user, parish, None


@router.get("/neighborhood", response_class=HTMLResponse)
def neighborhood_page(request: Request):
    user, parish, redirect = _resolve(request)
    if redirect:
        return redirect

    base = {"parish": parish, "state": "ok", "message": "", "sections": [], "rings": [], "ring": None}
    if _switched_off(parish):
        base.update(state="off", message="The Neighborhood page is turned off for this parish.")
        return _render(request, "parish_neighborhood.html", user, base)
    site = _site(parish)
    if not site:
        base.update(state="no_site", message="Your church's location has not been confirmed yet, so the Neighborhood page cannot be built for it. "
                                              "Ask the diocesan office to confirm it.")
        return _render(request, "parish_neighborhood.html", user, base)
    run = _latest_run(parish["org_id"])
    rings = _rings(site["id"])
    if not run or not rings:
        base.update(state="no_run", message="The Neighborhood numbers have not been built for your diocese yet.")
        return _render(request, "parish_neighborhood.html", user, base)

    minutes = [r["minutes"] for r in rings]
    try:
        chosen = int(request.query_params.get("ring", minutes[0]))
    except ValueError:
        chosen = minutes[0]
    if chosen not in minutes:
        chosen = minutes[0]
    iso = next(r for r in rings if r["minutes"] == chosen)

    home = db.query_one("SELECT county_geoid, county_name FROM nbhd.site_county WHERE site_id = %s AND is_home", (site["id"],))
    county_geoid = home["county_geoid"] if home else None
    state_fips = county_geoid[:2] if county_geoid else None
    geo_names = {"county": (home["county_name"] if home else "County"), "state": STATE_NAMES.get(state_fips or "", "State"), "us": "U.S."}

    values = _values_by_metric(run["id"], iso["id"])
    bm = _benchmarks(run["id"], county_geoid, state_fips)
    sections = _sections(values, bm, geo_names)
    schools = _schools_card(site["id"], chosen)
    if schools:
        education = next((s for s in sections if s["title"] == "Education and Schools"), None)
        if education is None:
            education = {"title": "Education and Schools", "cards": []}
            sections.append(education)
        education["cards"].append(schools)

    nearby = _nearby_parishes(site["id"], parish["org_id"])
    try:   # the Snapshot is an extra view of the same numbers: if it ever fails, the detailed cards must still show
        snapshot = neighborhood_snapshot.build(values, bm, geo_names, parish["name"], chosen,
                                               schools_total=(schools or {}).get("total"), nearby_count=len(nearby)) if values else None
    except Exception as exc:   # noqa: BLE001
        print(f"[neighborhood] snapshot failed for parish {parish['id']}: {type(exc).__name__}: {exc}")
        snapshot = None
    ctx = {
        "parish": parish, "state": "ok", "message": "", "site": site, "rings": minutes, "ring": chosen, "sections": sections,
        "geo_names": geo_names, "nearby": nearby, "parochial": _parochial(parish, user), "no_values": not values, "snapshot": snapshot,
        "run_date": run["finished_at"].strftime("%m/%d/%y") if run["finished_at"] else "", "vintage": run["acs_vintage"],
    }
    return _render(request, "parish_neighborhood.html", user, ctx)


@router.get("/neighborhood/map.json")
def neighborhood_map(request: Request):
    """The church point, its drive-time areas and the other parishes inside them, as plain JSON for the map."""
    user, parish, redirect = _resolve(request)
    if redirect or _switched_off(parish):
        return JSONResponse({"error": "not available"}, status_code=404)
    site = _site(parish)
    if not site:
        return JSONResponse({"error": "not available"}, status_code=404)
    rings = db.query(
        "SELECT minutes, ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.0002), 5) AS gj FROM nbhd.isochrone "
        "WHERE site_id = %s AND is_current ORDER BY minutes DESC", (site["id"],),
    )
    others = db.query(
        "SELECT p.name, s2.lat, s2.lon FROM nbhd.site s1 "
        "JOIN nbhd.isochrone i ON i.site_id = s1.id AND i.is_current "
        "JOIN nbhd.site s2 ON s2.is_primary AND s2.verified_at IS NOT NULL AND s2.parish_id IS NOT NULL AND s2.id <> s1.id AND ST_Covers(i.geom, s2.pt) "
        f"JOIN portal.parishes p ON p.id = s2.parish_id WHERE s1.id = %s AND {_VISIBLE_NEIGHBOR} GROUP BY p.name, s2.lat, s2.lon",
        (site["id"], parish["org_id"]),
    )
    return JSONResponse({
        "site": {"name": parish["name"], "lat": float(site["lat"]), "lon": float(site["lon"]), "address": site["address_used"]},
        "rings": [{"minutes": r["minutes"], "geometry": json.loads(r["gj"])} for r in rings],
        "parishes": [{"name": o["name"], "lat": float(o["lat"]), "lon": float(o["lon"])} for o in others],
    }, headers={"Cache-Control": "no-store"})
