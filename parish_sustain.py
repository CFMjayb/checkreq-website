"""
parish_sustain.py -- the "Parish Sustainability Review" page in Parish Mode (26-XXX Congregational Vitality, phase 3).

Ten years of the parish's own parochial-report numbers, what stands out in them, and "Can we afford a priest?": a five-year projection
from the latest report that follows the diocese's Financial Projection spreadsheet (with two fixes, shown as switches). The numbers
come from the `sustain` schema (see sustain_data.py); all of the arithmetic is in logic/sustain (pure, unit tested, parity-checked
against the spreadsheet); sustain_builder.py assembles the page; sustain_viz.py draws the charts. This file is routes and access only.

Who sees it (plan section 2): the people who already see the parish's Finance section, nobody else. The check is parish_finance.
can_view_finance: Beacon Admin or Setup Admin at the parish's own diocese (diocesan staff previewing the parish) or a holder of the
Parish Finance role at the parish (Treasurers get it automatically). The parish is always the one Parish Mode resolved; no parish id
is ever taken from the request. The page writes only saved scenarios, scoped to that parish.

Giving figures never reach another parish: this page is per parish, and with fewer than 10 pledges the amount pledged and the average
pledge are hidden for every viewer, diocesan staff included (sustain.parish_year_v and logic.sustain.harmonize both enforce it).

The page and every JSON call are built by functions that take the parish as an argument (page_response, project_response, the
scenario *_response functions). This file's routes resolve the parish from Parish Mode and call them; congregational_vitality.py
(the diocese's pages, phase 4) resolves it from the diocese's own list, checks the diocese gate, and calls the same functions, so
the two can never drift apart.

New file per the standing main.py rule: main.py gets one import and one register line. Same shape as parish_neighborhood.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import parish_finance
import parish_mode
import parish_roles
import rbac
import sustain_builder
import sustain_data
import sustain_defaults
from offloop import offloop

router = APIRouter()

_current_user = None
_render = None
_templates = None

NAME_MAX = 60


# ---------------------------------------------------------------------------------------------- access


def can_view(user, parish) -> bool:
    """Same people as the parish's Finance section. Never raises (a tile must not break its page)."""
    try:
        return bool(user and parish and parish_finance.can_view_finance(user, parish))
    except Exception:  # noqa: BLE001
        return False


def is_diocesan_staff(user, parish) -> bool:
    """The diocesan half of can_view: Beacon Admin or Setup Admin at the parish's own diocese. Only these see Spreadsheet mode."""
    try:
        return bool(parish_roles.holds_role_at_parish_org(user["id"], "beacon_admin", parish.get("org_id"))
                    or rbac.user_has_role(user["id"], "setup_admin", parish["org_id"]))
    except Exception:  # noqa: BLE001
        return False


def sustain_tile(user, parish) -> dict:
    """{show, href, label, desc} for the Resources for Parishes menu: only for someone who can open the page. Never raises."""
    if not can_view(user, parish):
        return {"show": False}
    return {"show": True, "href": "/sustainability", "label": "Parish Sustainability Review",
            "desc": "Ten years of your parochial report numbers, and what a priest would cost."}


def _resolve(request: Request):
    """(user, parish, redirect): signed in, and a parish resolved by Parish Mode."""
    user = _current_user(request)
    if not user:
        return None, None, RedirectResponse("/login")
    parish, _is_preview = parish_mode.effective_parish_mode(request, user)
    if not parish:
        return user, None, RedirectResponse("/parish-view")
    return user, parish, None


def _who(user) -> str:
    return (user.get("display_name") or user.get("email") or "")[:120]


def register(app, *, current_user, render, templates) -> None:
    global _current_user, _render, _templates
    _current_user, _render, _templates = current_user, render, templates
    app.include_router(router)
    templates.env.globals["sustain_tile"] = sustain_tile


# ---------------------------------------------------------------------------------------------- the page


def page_response(request: Request, user, parish, *, base_path: str = "/sustainability", back_url: str = "/parish-resources",
                  back_label: str = "Back to Resources for Parishes", neighborhood_href: str | None = None, diocesan: bool = False):
    """The review page for `parish`. Used by the parish's own route and by the diocese's (congregational_vitality.py), which differ only
    in where the page's links and calculator calls point (`base_path`), where Back goes, and whether the diocesan-only controls show."""
    if not can_view(user, parish):
        resp = _render(request, "parish_sustain.html", user, {"parish": parish, "state": "denied", "back_url": back_url, "back_label": back_label})
        resp.status_code = 403
        return resp
    ctx = sustain_builder.review_context(parish)
    ctx.update(parish=parish, can_spreadsheet_mode=is_diocesan_staff(user, parish), base_path=base_path, back_url=back_url,
               back_label=back_label, diocesan=diocesan)
    if ctx["state"] == "ok":
        defaults = sustain_data.assumption_defaults(parish["org_id"])
        guidelines = sustain_data.guideline_rows(parish["org_id"])
        base = ctx["latest"]
        nb = _neighborhood_link(user, parish)
        if neighborhood_href and nb.get("show"):
            nb = dict(nb, href=neighborhood_href)
        ctx.update(scenarios=sustain_data.list_scenarios(parish["id"]), guideline_missing=not guidelines, neighborhood=nb)
        unset = sustain_defaults.missing_defaults(defaults)
        if unset:
            # The diocese has not set its projection defaults (build_inputs would raise, and used to answer 500). The ten-year trends
            # still show; the calculator is replaced by a short notice. Nothing is made up. Only diocesan staff are told where to set
            # them; a parish leader is told to ask the diocese.
            ctx.update(defaults_missing=sustain_defaults.labels(unset), can_set_defaults=ctx["can_spreadsheet_mode"],
                       result={"ok": False, "error": sustain_defaults.NOT_SET_SENTENCE, "inputs": None}, form=None)
        else:
            result = sustain_builder.calc_result(base, defaults, guidelines, None)
            form_inputs = result["inputs"] or sustain_builder.calc_inputs_for_form(base, defaults, guidelines)
            ctx.update(defaults_missing=[], can_set_defaults=False, result=result, form=sustain_builder.form_values(form_inputs))
    return _render(request, "parish_sustain.html", user, ctx)


@router.get("/sustainability", response_class=HTMLResponse)
def sustainability_page(request: Request):
    user, parish, redirect = _resolve(request)
    if redirect:
        return redirect
    return page_response(request, user, parish)


def _neighborhood_link(user, parish) -> dict:
    tile = _templates.env.globals.get("neighborhood_tile")
    try:
        return tile(user, parish) if tile else {"show": False}
    except Exception:  # noqa: BLE001
        return {"show": False}


# ---------------------------------------------------------------------------------------------- the calculator (JSON)


def _base_and_defaults(parish):
    rows = sustain_data.parish_years(parish)
    if not rows:
        return None, None, None
    return rows[-1], sustain_data.assumption_defaults(parish["org_id"]), sustain_data.guideline_rows(parish["org_id"])


async def project_response(request: Request, user, parish):
    """The projection for the inputs in the body: {"inputs": {...}, "spreadsheet_mode": false}. Returns {"ok": true, "html": <the
    answer, tiles, chart and table, already drawn>, "form": {...}} or {"ok": false, "error": "..."}."""
    if not can_view(user, parish):
        return JSONResponse({"ok": False, "error": "You do not have permission to view this parish's review."}, status_code=403)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Could not read the request."}, status_code=400)
    if not isinstance(body, dict) or not isinstance(body.get("inputs", {}), dict):
        return JSONResponse({"ok": False, "error": "Could not read the request."}, status_code=400)
    spreadsheet_mode = body.get("spreadsheet_mode") is True
    if spreadsheet_mode and not is_diocesan_staff(user, parish):
        return JSONResponse({"ok": False, "error": "Spreadsheet mode is for diocesan staff."}, status_code=403)

    base, defaults, guidelines = _base_and_defaults(parish)
    if base is None:
        return JSONResponse({"ok": False, "error": "No parochial report on file yet."}, status_code=404)
    result = sustain_builder.calc_result(base, defaults, guidelines, body.get("inputs"), spreadsheet_mode=spreadsheet_mode)
    form = sustain_builder.form_values(result["inputs"]) if result.get("inputs") else None
    if not result["ok"]:
        return JSONResponse({"ok": False, "error": result["error"], "form": form}, headers={"Cache-Control": "no-store"})
    html = _templates.get_template("_sustain_result.html").render(result=result)
    return JSONResponse({"ok": True, "html": html, "form": form, "affordable": result["summary"]["affordable"]}, headers={"Cache-Control": "no-store"})


@router.post("/sustainability/project")
@offloop
async def sustainability_project(request: Request):
    user, parish, redirect = _resolve(request)
    if redirect:
        return JSONResponse({"ok": False, "error": "You do not have permission to view this parish's review."}, status_code=403)
    return await project_response(request, user, parish)


# ---------------------------------------------------------------------------------------------- saved scenarios (JSON)


def _clean_name(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    name = " ".join(raw.split())
    return name if 1 <= len(name) <= NAME_MAX else None


def scenarios_list_response(user, parish):
    if not can_view(user, parish):
        return JSONResponse({"error": "not available"}, status_code=403)
    return JSONResponse({"scenarios": [{"id": s["id"], "name": s["name"], "created_by": s["created_by"] or "",
                                       "created_at": s["created_at"].strftime("%b %d, %Y") if s["created_at"] else ""}
                                      for s in sustain_data.list_scenarios(parish["id"])]}, headers={"Cache-Control": "no-store"})


def scenario_load_response(user, parish, scenario_id: int):
    if not can_view(user, parish):
        return JSONResponse({"error": "not available"}, status_code=403)
    found = sustain_data.get_scenario(parish["id"], scenario_id)
    if not found:
        return JSONResponse({"error": "That scenario was not found."}, status_code=404)
    return JSONResponse({"id": found["id"], "name": found["name"], "inputs": found["inputs"]}, headers={"Cache-Control": "no-store"})


async def scenario_save_response(request: Request, user, parish):
    """Save the inputs under a name: {"name": "...", "inputs": {...}}. The inputs are checked the same way a projection's are, so a
    saved scenario can always be loaded again. The same name replaces the earlier scenario of that name."""
    if not can_view(user, parish):
        return JSONResponse({"ok": False, "error": "You do not have permission to save a scenario for this parish."}, status_code=403)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Could not read the request."}, status_code=400)
    name = _clean_name(body.get("name") if isinstance(body, dict) else None)
    if not name:
        return JSONResponse({"ok": False, "error": f"Give the scenario a name of 1 to {NAME_MAX} characters."}, status_code=400)
    base, defaults, guidelines = _base_and_defaults(parish)
    if base is None:
        return JSONResponse({"ok": False, "error": "No parochial report on file yet."}, status_code=404)
    result = sustain_builder.calc_result(base, defaults, guidelines, body.get("inputs") if isinstance(body.get("inputs"), dict) else {})
    if not result["ok"]:
        return JSONResponse({"ok": False, "error": result["error"]}, status_code=400)
    saved = sustain_data.save_scenario(parish["id"], name, sustain_builder.saved_inputs(result["inputs"]), _who(user))
    if saved is None:
        return JSONResponse({"ok": False, "error": f"This parish already has {sustain_data.MAX_SCENARIOS_PER_PARISH} saved scenarios. Delete one first."}, status_code=400)
    return JSONResponse({"ok": True, "id": saved["id"], "name": saved["name"]}, headers={"Cache-Control": "no-store"})


def scenario_delete_response(user, parish, scenario_id: int):
    if not can_view(user, parish):
        return JSONResponse({"ok": False, "error": "You do not have permission to do this for this parish."}, status_code=403)
    if not sustain_data.delete_scenario(parish["id"], scenario_id):
        return JSONResponse({"ok": False, "error": "That scenario was not found."}, status_code=404)
    return JSONResponse({"ok": True})


def _guard_json(request: Request):
    """(user, parish, denial). For the JSON routes a missing sign-in or parish is a plain 403, not a redirect."""
    user, parish, redirect = _resolve(request)
    if redirect:
        return None, None, JSONResponse({"error": "not available"}, status_code=403)
    return user, parish, None


@router.get("/sustainability/scenarios")
def scenarios_list(request: Request):
    user, parish, denial = _guard_json(request)
    return denial or scenarios_list_response(user, parish)


@router.get("/sustainability/scenarios/{scenario_id}")
def scenario_load(request: Request, scenario_id: int):
    user, parish, denial = _guard_json(request)
    return denial or scenario_load_response(user, parish, scenario_id)


@router.post("/sustainability/scenarios")
@offloop
async def scenario_save(request: Request):
    user, parish, denial = _guard_json(request)
    if denial:
        return JSONResponse({"ok": False, "error": "You do not have permission to save a scenario for this parish."}, status_code=403)
    return await scenario_save_response(request, user, parish)


@router.post("/sustainability/scenarios/{scenario_id}/delete")
def scenario_delete(request: Request, scenario_id: int):
    user, parish, denial = _guard_json(request)
    if denial:
        return JSONResponse({"ok": False, "error": "You do not have permission to do this for this parish."}, status_code=403)
    return scenario_delete_response(user, parish, scenario_id)
