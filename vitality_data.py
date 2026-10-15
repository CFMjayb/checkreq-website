"""
vitality_data.py -- reads for the diocesan Congregational Vitality pages (26-XXX Congregational Vitality, phase 4).

All reads, one set of queries per list (never one per parish), so a 100-parish list is a handful of round trips:
  * diocese_parishes        the diocese's active congregations
  * neighborhood_parishes   every parish's Neighborhood numbers for one drive time, ready for neighborhood_review.build
  * sustain_parishes        every parish's cleaned parochial-report history, ready for sustain_review.build
plus the one write the pages make, set_default (a diocese default; the app decides who may call it).

Everything is scoped to the diocese passed in. The numbers come from the nbhd and sustain schemas through the web role's read grants.
"""
from __future__ import annotations

import json

import db
import neighborhood_insights as ins
import neighborhood_review as nrev
import neighborhood_snapshot as snap
from logic.sustain.harmonize import harmonize_year
from parish_neighborhood import STATE_NAMES

# the metrics neighborhood_snapshot.collect_stats and neighborhood_review read; nothing else is fetched
NEIGHBORHOOD_KEYS = (
    "total_population", "total_households", "housing_units", "avg_household_size", "median_hh_income", "median_home_value", "median_age",
    "diversity_index", "pop_cagr_2010_2020", "hh_cagr_2010_2020", "hh_below_poverty_pct", "hh_snap_pct", "hh_public_assistance_pct",
    "unemployment_rate", "commute_over_30_pct", "hh_with_disability_pct", "hh_no_internet_pct", "insured_pct", "routine_checkup_pct",
    "dental_visit_pct", "no_english_by_age_language", "rent_burden", "mortgage_burden", "generational_balance", "tenure",
    "educational_attainment", "hh_nonfamily_by_size",
)

SUSTAIN_YEARS_BACK = 10      # filings kept per parish: the latest and the ten before it
DEFAULT_KEYS = ("safe_draw_rate", "health_annual", "income_growth", "salary_growth", "other_exp_growth", "pledge_growth", "pension_rate")


def diocese_parishes(org_id: int) -> list[dict]:
    """Active congregations of the diocese, by name. [{id, org_id, name, city, modules, parochial_report_id}]."""
    return db.query(
        "SELECT id, org_id, name, city, modules, parochial_report_id FROM portal.parishes "
        "WHERE org_id = %s AND is_active AND COALESCE(is_congregation, true) ORDER BY lower(name), id", (org_id,),
    )


def _switched_off(parish: dict) -> bool:
    m = parish.get("modules")
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError:
            m = {}
    return isinstance(m, dict) and m.get("neighborhood") is False


def latest_run(org_id: int):
    return db.query_one(
        "SELECT id, finished_at, acs_vintage FROM nbhd.profile_run WHERE org_id = %s AND status = 'success' ORDER BY finished_at DESC LIMIT 1", (org_id,))


def ring_minutes(org_id: int) -> list[int]:
    """The drive times that exist for this diocese's current areas, smallest first (normally [7, 15])."""
    rows = db.query(
        "SELECT DISTINCT i.minutes FROM nbhd.isochrone i JOIN nbhd.site s ON s.id = i.site_id "
        "WHERE s.org_id = %s AND s.is_primary AND s.verified_at IS NOT NULL AND i.is_current ORDER BY i.minutes", (org_id,))
    return [r["minutes"] for r in rows]


def neighborhood_parishes(org_id: int, minutes: int, level: str) -> tuple[list[dict], dict | None]:
    """([{id, name, place, stats, top, reason}], run). A parish with no confirmed church location, switched off, or without numbers
    in the latest run gets stats None and a plain `reason`. `top` is the one thing that stands out most against `level`."""
    parishes = diocese_parishes(org_id)
    run = latest_run(org_id)
    if not run:
        return [{"id": p["id"], "name": p["name"], "place": p.get("city") or "", "stats": None, "top": "", "reason": "No numbers built yet"} for p in parishes], None

    sites = db.query(
        "SELECT s.id AS site_id, s.parish_id, i.id AS iso_id FROM nbhd.site s JOIN nbhd.isochrone i ON i.site_id = s.id AND i.is_current AND i.minutes = %s "
        "WHERE s.org_id = %s AND s.is_primary AND s.verified_at IS NOT NULL AND s.parish_id IS NOT NULL", (minutes, org_id))
    by_parish = {s["parish_id"]: s for s in sites}
    iso_ids = [s["iso_id"] for s in sites]
    homes = {r["site_id"]: r for r in db.query(
        "SELECT site_id, county_geoid, county_name FROM nbhd.site_county WHERE is_home AND site_id = ANY(%s)", ([s["site_id"] for s in sites],))}

    values: dict[int, dict[str, list[dict]]] = {}
    for r in db.query(
        "SELECT isochrone_id, metric_key, component, value, moe, cv, reliability, seq FROM nbhd.profile_value "
        "WHERE run_id = %s AND isochrone_id = ANY(%s) AND metric_key = ANY(%s) ORDER BY isochrone_id, metric_key, seq NULLS LAST, component",
        (run["id"], iso_ids, list(NEIGHBORHOOD_KEYS)),
    ):
        values.setdefault(r["isochrone_id"], {}).setdefault(r["metric_key"], []).append(r)

    bench: dict[tuple[str, str], dict[str, dict[str, float]]] = {}
    for r in db.query("SELECT geo_level, geoid, metric_key, component, value FROM nbhd.benchmark_value WHERE run_id = %s AND metric_key = ANY(%s)",
                      (run["id"], list(NEIGHBORHOOD_KEYS))):
        bench.setdefault((r["geo_level"], r["geoid"]), {}).setdefault(r["metric_key"], {})[r["component"]] = r["value"]

    out = []
    for p in parishes:
        row = {"id": p["id"], "name": p["name"], "place": p.get("city") or "", "stats": None, "top": "", "reason": ""}
        site = by_parish.get(p["id"])
        if _switched_off(p):
            row["reason"] = "Turned off for this parish"
        elif not site:
            row["reason"] = "Church location not confirmed yet"
        else:
            vals = values.get(site["iso_id"])
            home = homes.get(site["site_id"])
            geoid = home["county_geoid"] if home else None
            state = geoid[:2] if geoid else None
            if home:
                row["place"] = f"{home['county_name']}, {STATE_NAMES.get(state or '', '')}".rstrip(", ")
            if not vals:
                row["reason"] = "No numbers for this area"
            else:
                bm = {"county": bench.get(("county", geoid or ""), {}), "state": bench.get(("state", state or ""), {}), "us": bench.get(("us", "1"), {})}
                stats = nrev.add_living_alone(snap.collect_stats(vals, bm), vals, bm)
                row["stats"], row["top"] = stats, nrev.top_sentence(stats, level)
        out.append(row)
    return out, run


def sustain_parishes(org_id: int) -> tuple[list[dict], object]:
    """([{id, name, place, years}], data_as_of). years are the parish's harmonized rows, oldest first, newest SUSTAIN_YEARS_BACK + 1
    filings at most; None when no parochial report is linked to the parish."""
    parishes = diocese_parishes(org_id)
    newest = db.query_one("SELECT max(filing_year) AS y, max(loaded_at) AS loaded FROM sustain.parish_year_v WHERE org_id = %s", (org_id,))
    cutoff = (newest["y"] or 0) - SUSTAIN_YEARS_BACK
    by_parish: dict[int, list[dict]] = {}
    for r in db.query(
        "SELECT * FROM sustain.parish_year_v WHERE org_id = %s AND portal_parish_id IS NOT NULL AND filing_year >= %s ORDER BY portal_parish_id, filing_year",
        (org_id, cutoff),
    ):
        by_parish.setdefault(r["portal_parish_id"], []).append(harmonize_year(r))
    out = [{"id": p["id"], "name": p["name"], "place": p.get("city") or "", "years": by_parish.get(p["id"])} for p in parishes]
    return out, newest["loaded"]


def get_defaults(org_id: int) -> list[dict]:
    """The diocese's projection defaults with their notes, in display order: [{key, value, note, set_by, set_at}]."""
    rows = {r["key"]: r for r in db.query("SELECT key, value, note, set_by, set_at FROM sustain.assumption_default WHERE org_id = %s", (org_id,))}
    return [{"key": k, "value": (float(rows[k]["value"]) if k in rows and rows[k]["value"] is not None else None),
             "note": (rows[k]["note"] if k in rows else None), "set_by": (rows[k]["set_by"] if k in rows else None),
             "set_at": (rows[k]["set_at"] if k in rows else None)} for k in DEFAULT_KEYS]


def set_default(org_id: int, key: str, value: float | None, who: str) -> None:
    """Set one default (None means "not decided yet"). The caller has already checked the key and the range."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sustain.assumption_default (org_id, key, value, set_by, set_at) VALUES (%s, %s, %s, %s, now()) "
                "ON CONFLICT (org_id, key) DO UPDATE SET value = EXCLUDED.value, set_by = EXCLUDED.set_by, set_at = now()",
                (org_id, key, value, who),
            )
