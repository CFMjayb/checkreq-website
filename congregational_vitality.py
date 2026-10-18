"""
congregational_vitality.py -- the diocese's Congregational Vitality pages (26-XXX Congregational Vitality, phase 4).

A top-level button on the diocesan portal opens this menu:
  * Neighborhood Review              one sortable row per parish with eight neighborhood measures (mockup C), the compare-with switch
                                     (U.S., state, county) and the 7 / 15 minute switch; a parish name opens its Neighborhood View
  * Parish Sustainability Review     one sortable row per parish with Sunday attendance, deficit years, reserves and whether a priest
                                     is affordable; a parish name opens its review with the diocesan-only controls
  * Projection defaults              the diocese's own rates, safe draw rate and health insurance (the values the parish pages start from)

Who sees it: Beacon Admin or Setup Admin at the CURRENT diocese (the same two the Finance page already accepts for diocesan staff),
never in Cornerstone Mode. The parish pages opened from here are the parish pages' own builders (parish_neighborhood.page_context,
parish_sustain.page_response and the calculator and scenario functions behind it), so a number cannot differ between the diocese's
view of a parish and the parish's own view. A parish id from the URL is always looked up INSIDE the current diocese, and a parish of
another diocese is a plain "not found". Giving figures: the parish pages hide the amount pledged for any parish with fewer than 10
pledges for every viewer; the lists here show no giving figure at all.

Writes: only the projection defaults (sustain.assumption_default) and, through the parish page, saved scenarios. Everything else is read.

New file per the standing main.py rule: main.py gets one import and one register line, a tile, and one pseudo-role line.
"""
from __future__ import annotations

import math
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import cornerstone_mode
import neighborhood_review
import parish_neighborhood
import parish_sustain
import rbac
import registry
import sustain_data
import sustain_defaults
import sustain_review
import vitality_data
import vitality_viz
from offloop import offloop

router = APIRouter()

_current_user = None
_current_org = None
_render = None

BASE = "/congregational-vitality"
STAFF_ROLES = ["beacon_admin", "setup_admin"]

# (key, label, kind, low, high, may be blank). Percent fields are typed as percent numbers and stored as fractions.
DEFAULT_FIELDS = (
    ("safe_draw_rate", "Safe endowment draw", "percent", 0.0, 20.0, False,
     "The share of unrestricted reserves a parish can draw each year without wearing them down."),
    ("health_annual", "Health insurance for a full-time priest, a year", "dollars", 0.0, 200000.0, True,
     "Leave blank until it is decided; the parish pages then say \"Health insurance not included.\""),
    ("income_growth", "Giving and income growth, a year", "percent", -20.0, 25.0, False, "Applied to plate and pledge, investments used for operations and other revenue."),
    ("salary_growth", "Priest pay raise, a year", "percent", -20.0, 25.0, False, ""),
    ("other_exp_growth", "Other costs growth, a year", "percent", -20.0, 25.0, False, ""),
    ("pledge_growth", "Pledge growth, a year", "percent", -20.0, 25.0, False, "Shown on the page only; the amount pledged does not drive the projection."),
    ("pension_rate", "Pension rate on the priest's pay", "percent", 0.0, 50.0, False, "Shown to parishes, not editable by them."),
)


# ---------------------------------------------------------------------------------------------- access


def staff_ok(user, org_id) -> bool:
    """Beacon Admin or Setup Admin at this diocese, and the entity is a real diocese (not a Cornerstone-served parish's own entity).
    Never raises."""
    try:
        return bool(user and org_id and rbac.user_has_any_role(user["id"], STAFF_ROLES, org_id) and not cornerstone_mode.is_cornerstone_org(org_id))
    except Exception:  # noqa: BLE001
        return False


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


def _gate(request: Request):
    """(user, org, denial). A denial is a response to return as it is."""
    user = _current_user(request)
    if not user:
        return None, None, RedirectResponse("/login")
    org = _current_org(request)
    if not org:
        return user, None, RedirectResponse("/portal")
    if not staff_ok(user, org["id"]):
        resp = _render(request, "vitality_hub.html", user, {"state": "denied", "org": org})
        resp.status_code = 403
        return user, org, resp
    return user, org, None


def _gate_json(request: Request):
    user = _current_user(request)
    org = _current_org(request) if user else None
    if not user or not org or not staff_ok(user, org["id"]):
        return None, None, JSONResponse({"ok": False, "error": "You do not have permission to do this."}, status_code=403)
    return user, org, None


def _parish_in_diocese(parish_id: int, org: dict):
    """The parish if it belongs to this diocese and is an active congregation, else None (a clean 404 either way)."""
    parish = registry.get_parish(parish_id, org["id"])
    if not parish or not parish.get("is_active"):
        return None
    return parish


def _not_found(request: Request, user, org):
    resp = _render(request, "vitality_hub.html", user, {"state": "notfound", "org": org})
    resp.status_code = 404
    return resp


def _url(path: str, **params) -> str:
    q = {k: v for k, v in params.items() if v not in (None, "")}
    return f"{path}?{urlencode(q)}" if q else path


def _desc(request: Request, key: str) -> bool | None:
    """True for dir=desc, False for dir=asc, None when not given."""
    d = request.query_params.get("dir")
    return True if d == "desc" else (False if d == "asc" else None)


def _who(user) -> str:
    return (user.get("display_name") or user.get("email") or "")[:120]


# ---------------------------------------------------------------------------------------------- the menu


@router.get(BASE, response_class=HTMLResponse)
def hub_page(request: Request):
    user, org, denial = _gate(request)
    if denial:
        return denial
    tiles = [
        {"href": f"{BASE}/neighborhood", "label": "Neighborhood Review",
         "desc": "Every parish's neighborhood side by side: who lives within a short drive of each church, from public Census data."},
        {"href": f"{BASE}/sustainability", "label": "Parish Sustainability Review",
         "desc": "Every parish's Sunday attendance, deficit years and reserves, and whether it could afford a priest."},
        {"href": f"{BASE}/sustainability/defaults", "label": "Projection defaults",
         "desc": "The diocese's rates, safe draw rate and health insurance that the parish reviews start from."},
    ]
    return _render(request, "vitality_hub.html", user, {"state": "ok", "org": org, "tiles": tiles})


# ---------------------------------------------------------------------------------------------- Neighborhood Review


@router.get(BASE + "/neighborhood", response_class=HTMLResponse)
def neighborhood_review_page(request: Request):
    user, org, denial = _gate(request)
    if denial:
        return denial
    org_id = org["id"]
    rings = vitality_data.ring_minutes(org_id)
    try:
        ring = int(request.query_params.get("ring", ""))
    except ValueError:
        ring = 0
    if ring not in rings:
        ring = 15 if 15 in rings else (rings[-1] if rings else 15)
    level = request.query_params.get("level", "state")
    if level not in neighborhood_review.LEVELS:
        level = "state"
    sort = request.query_params.get("sort", "name")
    desc = _desc(request, sort)

    parishes, run = vitality_data.neighborhood_parishes(org_id, ring, level)
    if sort != "name" and sort not in neighborhood_review.COLUMN_KEYS:
        sort = "name"
    if desc is None:
        desc = sort != "name"
    table = neighborhood_review.build(parishes, level, sort, desc)

    path = f"{BASE}/neighborhood"
    for col in table["columns"]:
        flip = (not desc) if col["active"] else (col["key"] != "name")
        col["href"] = _url(path, ring=ring, level=level, sort=col["key"], dir="desc" if flip else "asc")
    for row in table["rows"]:
        row["href"] = _url(f"{path}/{row['id']}", ring=ring) if row["has_numbers"] else ""
    ctx = {
        "state": "ok" if run else "no_run", "org": org, "table": table, "ring": ring, "level": level, "level_name": neighborhood_review.LEVEL_NAMES[level],
        "ring_links": [{"minutes": m, "active": m == ring, "href": _url(path, ring=m, level=level, sort=sort, dir="desc" if desc else "asc")} for m in rings],
        "level_links": [{"key": lv, "name": neighborhood_review.LEVEL_NAMES[lv], "active": lv == level,
                         "href": _url(path, ring=ring, level=lv, sort=sort, dir="desc" if desc else "asc")} for lv in neighborhood_review.LEVELS],
        "run_date": run["finished_at"].strftime("%m/%d/%y") if run and run.get("finished_at") else "", "vintage": run["acs_vintage"] if run else "",
    }
    return _render(request, "vitality_neighborhood.html", user, ctx)


def _parish_back() -> dict:
    return {"back_url": f"{BASE}/neighborhood", "back_label": "Back to Neighborhood Review"}


@router.get(BASE + "/neighborhood/{parish_id}/map.json")
def neighborhood_map(request: Request, parish_id: int):
    user, org, denial = _gate_json(request)
    if denial:
        return JSONResponse({"error": "not available"}, status_code=404)
    parish = _parish_in_diocese(parish_id, org)
    if not parish:
        return JSONResponse({"error": "not available"}, status_code=404)
    return parish_neighborhood.map_response(parish)


@router.get(BASE + "/neighborhood/{parish_id}", response_class=HTMLResponse)
def neighborhood_parish_page(request: Request, parish_id: int):
    user, org, denial = _gate(request)
    if denial:
        return denial
    parish = _parish_in_diocese(parish_id, org)
    if not parish:
        return _not_found(request, user, org)
    ctx = parish_neighborhood.page_context(request, user, parish, base_path=f"{BASE}/neighborhood/{parish_id}", diocesan=True,
                                           **_parish_back())
    return _render(request, "parish_neighborhood.html", user, ctx)


# ---------------------------------------------------------------------------------------------- Parish Sustainability Review (the list)


def _parse_salary(raw: str | None):
    """(value or None, error text). Accepts "$65,000" and "65000"; blank is no test salary."""
    text = (raw or "").replace("$", "").replace(",", "").strip()
    if not text:
        return None, ""
    try:
        v = float(text)
    except ValueError:
        return None, "The test salary must be a number, such as 65000."
    if not math.isfinite(v) or not 0 <= v <= 1_000_000:
        return None, "The test salary must be between 0 and 1,000,000."
    return v, ""


@router.get(BASE + "/sustainability/defaults", response_class=HTMLResponse)
def defaults_page(request: Request):
    user, org, denial = _gate(request)
    if denial:
        return denial
    return _render(request, "vitality_defaults.html", user, _defaults_ctx(org, request.query_params.get("saved")))


def _defaults_ctx(org, saved=None, errors=None, typed=None):
    current = {d["key"]: d for d in vitality_data.get_defaults(org["id"])}
    fields = []
    for key, label, kind, low, high, blank_ok, hint in DEFAULT_FIELDS:
        d = current[key]
        value = d["value"]
        shown = "" if value is None else (f"{value * 100:g}" if kind == "percent" else f"{value:g}")
        if typed is not None and key in typed:
            shown = typed[key]
        fields.append({"key": key, "label": label, "kind": kind, "low": low, "high": high, "blank_ok": blank_ok, "hint": hint, "value": shown,
                       "note": d["note"] or "", "set_by": d["set_by"] or "", "set_at": d["set_at"].strftime("%m/%d/%y") if d["set_at"] else "",
                       "error": (errors or {}).get(key, "")})
    return {"state": "ok", "org": org, "fields": fields, "saved": saved, "errors": bool(errors)}


def _parse_default(kind: str, low: float, high: float, blank_ok: bool, raw: str):
    """(stored value or None, error). Percent numbers are stored as fractions."""
    text = (raw or "").replace("$", "").replace(",", "").replace("%", "").strip()
    if not text:
        return (None, "") if blank_ok else (None, "A value is needed here.")
    try:
        v = float(text)
    except ValueError:
        return None, "This must be a number."
    if not math.isfinite(v) or not low <= v <= high:
        return None, f"This must be between {low:g} and {high:g}."
    return round(v / 100.0, 6) if kind == "percent" else round(v, 2), ""


@router.post(BASE + "/sustainability/defaults", response_class=HTMLResponse)
@offloop
async def defaults_save(request: Request):
    user, org, denial = _gate(request)
    if denial:
        return denial
    form = await request.form()
    errors, parsed, typed = {}, {}, {}
    for key, _label, kind, low, high, blank_ok, _hint in DEFAULT_FIELDS:
        raw = str(form.get("d_" + key, ""))
        typed[key] = raw
        value, err = _parse_default(kind, low, high, blank_ok, raw)
        if err:
            errors[key] = err
        else:
            parsed[key] = value
    if errors:
        resp = _render(request, "vitality_defaults.html", user, _defaults_ctx(org, None, errors, typed))
        resp.status_code = 400
        return resp
    current = {d["key"]: d["value"] for d in vitality_data.get_defaults(org["id"])}
    changed = 0
    for key, value in parsed.items():
        old = current.get(key)
        if (old is None) != (value is None) or (old is not None and abs(old - value) > 1e-9):
            vitality_data.set_default(org["id"], key, value, _who(user))
            changed += 1
    return RedirectResponse(_url(f"{BASE}/sustainability/defaults", saved=changed), status_code=303)


@router.get(BASE + "/sustainability", response_class=HTMLResponse)
def sustainability_review_page(request: Request):
    user, org, denial = _gate(request)
    if denial:
        return denial
    org_id = org["id"]
    salary, salary_error = _parse_salary(request.query_params.get("salary"))
    sort = request.query_params.get("sort", "name")
    if sort not in sustain_review.SORTS:
        sort = "name"
    desc = _desc(request, sort)
    if desc is None:
        desc = sort != "name"

    parishes, loaded = vitality_data.sustain_parishes(org_id)
    defaults = sustain_data.assumption_defaults(org_id)
    guidelines = sustain_data.guideline_rows(org_id)
    lst = sustain_review.build(parishes, defaults, guidelines, salary, sort, desc)

    path = f"{BASE}/sustainability"
    keep = {"salary": request.query_params.get("salary") if salary is not None else None}
    columns = []
    for key, title in (("name", "Parish"), ("attendance", "Sunday attendance"), ("change", "Change in five years"), ("deficit", "Deficit years, last five"),
                       ("reserves", "Cash and reserves"), ("runway", "How long reserves last"), ("affordable", "Priest affordable?")):
        active = key == sort
        flip = (not desc) if active else (key != "name")
        columns.append({"key": key, "title": title, "active": active, "arrow": ("down" if desc else "up") if active else "",
                        "href": _url(path, sort=key, dir="desc" if flip else "asc", **keep)})
    for row in lst["rows"]:
        row["href"] = f"{path}/{row['id']}" if row["has_report"] else ""
        if row["has_report"] and row["attendance"]:
            row["spark"] = vitality_viz.sparkline(row["attendance"]["series"])
    ctx = {"state": "ok", "org": org, "lst": lst, "columns": columns, "salary": salary, "salary_text": request.query_params.get("salary", "") if salary is not None else "",
           "salary_error": salary_error, "as_of": loaded, "sort": sort, "dir": "desc" if desc else "asc", "defaults_href": f"{path}/defaults", "list_href": path,
           # a diocese that has not set its projection defaults cannot test any parish: say so once, not on every row
           "defaults_missing": sustain_defaults.labels(sustain_defaults.missing_defaults(defaults))}
    return _render(request, "vitality_sustain.html", user, ctx)


# ---------------------------------------------------------------------------------------------- Parish Sustainability Review (one parish)


@router.get(BASE + "/sustainability/{parish_id}", response_class=HTMLResponse)
def sustainability_parish_page(request: Request, parish_id: int):
    user, org, denial = _gate(request)
    if denial:
        return denial
    parish = _parish_in_diocese(parish_id, org)
    if not parish:
        return _not_found(request, user, org)
    return parish_sustain.page_response(
        request, user, parish, base_path=f"{BASE}/sustainability/{parish_id}", back_url=f"{BASE}/sustainability",
        back_label="Back to Parish Sustainability Review", neighborhood_href=f"{BASE}/neighborhood/{parish_id}", diocesan=True)


def _json_parish(request: Request, parish_id: int):
    user, org, denial = _gate_json(request)
    if denial:
        return None, None, denial
    parish = _parish_in_diocese(parish_id, org)
    if not parish:
        return None, None, JSONResponse({"ok": False, "error": "That parish was not found."}, status_code=404)
    return user, parish, None


@router.post(BASE + "/sustainability/{parish_id}/project")
@offloop
async def sustainability_parish_project(request: Request, parish_id: int):
    user, parish, denial = _json_parish(request, parish_id)
    return denial or await parish_sustain.project_response(request, user, parish)


@router.get(BASE + "/sustainability/{parish_id}/scenarios")
def sustainability_parish_scenarios(request: Request, parish_id: int):
    user, parish, denial = _json_parish(request, parish_id)
    return denial or parish_sustain.scenarios_list_response(user, parish)


@router.get(BASE + "/sustainability/{parish_id}/scenarios/{scenario_id}")
def sustainability_parish_scenario(request: Request, parish_id: int, scenario_id: int):
    user, parish, denial = _json_parish(request, parish_id)
    return denial or parish_sustain.scenario_load_response(user, parish, scenario_id)


@router.post(BASE + "/sustainability/{parish_id}/scenarios")
@offloop
async def sustainability_parish_scenario_save(request: Request, parish_id: int):
    user, parish, denial = _json_parish(request, parish_id)
    return denial or await parish_sustain.scenario_save_response(request, user, parish)


@router.post(BASE + "/sustainability/{parish_id}/scenarios/{scenario_id}/delete")
def sustainability_parish_scenario_delete(request: Request, parish_id: int, scenario_id: int):
    user, parish, denial = _json_parish(request, parish_id)
    return denial or parish_sustain.scenario_delete_response(user, parish, scenario_id)
