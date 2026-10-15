"""
sustain_data.py -- the database side of the Parish Sustainability Review (26-XXX Congregational Vitality).

Everything the page reads comes from the `sustain` schema (built by 26-XXX Neighborhood Insights, migrations 007 and 008):
  sustain.parish_year_v      one row per parish per parochial-report year, harmonized across the form eras. Amount pledged is
                             already hidden (NULL) under 10 pledge cards; logic.sustain.harmonize enforces it again.
  sustain.cpi_annual         CPI-U annual averages, for "in latest-year dollars"
  sustain.assumption_default the diocese's projection defaults (editable by diocesan staff only)
  sustain.comp_guideline     diocesan minimum clergy pay by size band (empty until Jay says which figures are official)
  sustain.scenario           a parish's saved "Can we afford a priest?" inputs
The raw public.parochial_report table is never read here; the web role has no grant on it (the view runs with its owner's rights).

Only scenarios are written. Every read is filtered by the parish's own diocese as well as its parochial-report id.
"""
from __future__ import annotations

import json

import db
from logic.sustain.harmonize import harmonize_year

MAX_SCENARIOS_PER_PARISH = 50


def parish_years(parish: dict) -> list[dict]:
    """The parish's cleaned rows, oldest first. [] when it has no parochial-report id or none filed."""
    ueid = parish.get("parochial_report_id")
    if not ueid:
        return []
    rows = db.query(
        "SELECT * FROM sustain.parish_year_v WHERE parish_ueid = %s AND org_id = %s ORDER BY filing_year",
        (ueid, parish["org_id"]),
    )
    return [harmonize_year(r) for r in rows]


def data_as_of(years: list[dict]):
    """When the parish's newest parochial-report row was loaded (the page's "data as of"), or None."""
    stamps = [y["loaded_at"] for y in years if y.get("loaded_at") is not None]
    return max(stamps) if stamps else None


def cpi_rows() -> list[dict]:
    return db.query("SELECT year, cpi_u, source FROM sustain.cpi_annual ORDER BY year")


def cpi_map() -> dict[int, float]:
    return {r["year"]: float(r["cpi_u"]) for r in cpi_rows()}


def assumption_defaults(org_id: int) -> dict[str, float | None]:
    return {r["key"]: (float(r["value"]) if r["value"] is not None else None)
            for r in db.query("SELECT key, value FROM sustain.assumption_default WHERE org_id = %s", (org_id,))}


def guideline_rows(org_id: int) -> list[dict]:
    """The diocese's size bands for its latest guideline year ([] while the table is empty)."""
    return db.query(
        "SELECT asa_min, asa_max, min_salary_housing FROM sustain.comp_guideline "
        "WHERE org_id = %s AND year = (SELECT max(year) FROM sustain.comp_guideline WHERE org_id = %s) ORDER BY asa_min NULLS FIRST",
        (org_id, org_id),
    )


# ------------------------------------------------------------------------------------------------ saved scenarios


def list_scenarios(parish_id: int) -> list[dict]:
    return db.query(
        "SELECT id, name, created_by, created_at FROM sustain.scenario WHERE parish_id = %s ORDER BY lower(name), id", (parish_id,)
    )


def get_scenario(parish_id: int, scenario_id: int) -> dict | None:
    return db.query_one("SELECT id, name, inputs FROM sustain.scenario WHERE id = %s AND parish_id = %s", (scenario_id, parish_id))


def save_scenario(parish_id: int, name: str, inputs: dict, created_by: str) -> dict | None:
    """Insert, or replace the inputs of the parish's scenario of the same name. Returns {id, name}, or None when the parish
    already holds MAX_SCENARIOS_PER_PARISH and this would be a new one."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM sustain.scenario WHERE parish_id = %s AND name = %s", (parish_id, name))
            existing = cur.fetchone()
            if existing:
                cur.execute("UPDATE sustain.scenario SET inputs = %s::jsonb, created_by = %s, created_at = now() WHERE id = %s RETURNING id, name",
                            (json.dumps(inputs), created_by, existing["id"]))
                return cur.fetchone()
            cur.execute("SELECT count(*) AS n FROM sustain.scenario WHERE parish_id = %s", (parish_id,))
            if cur.fetchone()["n"] >= MAX_SCENARIOS_PER_PARISH:
                return None
            cur.execute("INSERT INTO sustain.scenario (parish_id, name, inputs, created_by) VALUES (%s, %s, %s::jsonb, %s) RETURNING id, name",
                        (parish_id, name, json.dumps(inputs), created_by))
            return cur.fetchone()


def delete_scenario(parish_id: int, scenario_id: int) -> bool:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sustain.scenario WHERE id = %s AND parish_id = %s", (scenario_id, parish_id))
            return cur.rowcount > 0
