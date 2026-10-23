"""
donor_routes_statements.py -- Beacon Donor Management, giving (tax) statements: the staff screens. Routes only: every rule lives in
statement_rules / statement_data / statement_profile / statement_service.

  GET  /giving/statements                 the hub: the parish statement profile, the statement settings, and a picker to preview a statement
  POST /giving/statements/profile         save the profile (action=save) or save and confirm it (action=confirm)
  POST /giving/statements/settings        the statement rules (minimum, no-address, not-deductible section, check numbers)
  GET  /giving/statements/preview         one person's statement for one year, as it would print (builds and stores nothing)

The parish ALWAYS comes from parish_mode.effective_parish_mode through donor_web.gate. A person id or a year in a URL is only a key the
service looks up INSIDE the signed-in parish. Nothing here sends a statement to anyone.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

import datetime as dt

import donor_web as W
import statement_profile as SP
import statement_render as RND
import statement_service as SS
import statement_templates as TPL
from donor_core import DonorError, NotFound

router = APIRouter(dependencies=[Depends(W.check_path_ids)])


def register(app) -> None:
    app.include_router(router)


def _gate(request: Request, cap: str):
    """Signed in, a parish in view, giving turned on for it, and `cap`."""
    user, parish, ctx, resp = W.gate(request, need=None, feature="giving", active="statements")
    if resp:
        return None, None, None, resp
    if not ctx.can(cap):
        return None, None, None, W.page(request, "donor_off.html", user, parish, ctx, "statements", {"reason": "permission", "feature": "giving"}, status_code=403)
    return user, parish, ctx, None


def _opt_int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


@router.get("/giving/statements", response_class=HTMLResponse)
def statements_home(request: Request):
    user, parish, ctx, resp = _gate(request, "statements.run")
    if resp:
        return resp
    q = (request.query_params.get("q") or "").strip()
    info = SP.profile_get(ctx)
    tpls = W.safe(lambda: TPL.templates_list(ctx), None)
    default_template = next((t for t in (tpls["parish"] if tpls else []) if t["is_default"]), None)
    found = []
    try:
        found = SS.find_donors(ctx, q) if q else []
    except DonorError:
        found = []
    return W.page(request, "donor_statements.html", user, parish, ctx, "statements", {
        "info": info, "profile": info["profile"], "sug": info["suggestion"], "q": q, "found": found, "default_template": default_template,
        "can_edit": ctx.can("statements.templates")})


@router.post("/giving/statements/profile")
async def statements_profile_save(request: Request):
    user, parish, ctx, resp = _gate(request, "statements.templates")
    if resp:
        return resp
    form = await request.form()
    fields = {k: form.get(k) for k in SP.PROFILE_FIELDS}
    confirm = form.get("action") == "confirm"
    try:
        r = SP.profile_save(ctx, fields, confirm=confirm, use_logo=form.get("use_logo") is not None)
    except DonorError as e:
        return W.back(request, "/giving/statements", err=e.message)
    if confirm:
        return W.back(request, "/giving/statements", ok="Profile saved and confirmed.")
    if r["confirmation_removed"]:
        return W.back(request, "/giving/statements", ok="Profile saved. It was changed, so it needs to be confirmed again.")
    return W.back(request, "/giving/statements", ok="Profile saved." + (" Still to fill in before it can be confirmed: " + ", ".join(r["missing"]) + "." if r["missing"] else ""))


@router.post("/giving/statements/settings")
async def statements_settings_save(request: Request):
    user, parish, ctx, resp = _gate(request, "statements.templates")
    if resp:
        return resp
    form = await request.form()
    values = {"statement_min_total": form.get("statement_min_total"),
              "statement_allow_no_address": form.get("statement_allow_no_address") is not None,
              "statement_include_nondeductible": form.get("statement_include_nondeductible") is not None,
              "statement_show_check_numbers": form.get("statement_show_check_numbers") is not None,
              "member_statements_enabled": form.get("member_statements_enabled") is not None}
    try:
        r = SP.settings_save(ctx, values)
    except DonorError as e:
        return W.back(request, "/giving/statements", err=e.message)
    return W.back(request, "/giving/statements", ok="Settings saved." if r["changed"] else "Nothing changed.")


@router.get("/giving/statements/preview", response_class=HTMLResponse)
def statement_preview(request: Request):
    user, parish, ctx, resp = _gate(request, "statements.run")
    if resp:
        return resp
    pid = _opt_int(request.query_params.get("person_id"))
    year = _opt_int(request.query_params.get("year"))
    try:
        if pid is None:
            raise NotFound("Pick a person to preview.")
        if year is None:
            ys = SS.years_for_person(ctx, pid)
            if not ys:
                raise NotFound("That person has no posted gifts at this parish yet.")
            year = ys[0]
        data = SS.preview_for_person(ctx, pid, year)
    except NotFound as e:
        return W.page(request, "donor_off.html", user, parish, ctx, "statements",
                      {"reason": "notfound", "feature": "giving", "what": e.message, "back_href": "/giving/statements"}, status_code=404)
    except DonorError as e:
        return W.back(request, "/giving/statements", err=e.message)
    return W.page(request, "donor_statement_preview.html", user, parish, ctx, "statements", {**data, **data["sheet"]})


# ── Templates ───────────────────────────────────────────────────────────────────────────────────
@router.get("/giving/statements/templates", response_class=HTMLResponse)
def templates_page(request: Request):
    user, parish, ctx, resp = _gate(request, "statements.run")
    if resp:
        return resp
    data = TPL.templates_list(ctx, include_retired=request.query_params.get("retired") == "1")
    return W.page(request, "donor_statement_templates.html", user, parish, ctx, "statements", {
        "globals": data["globals"], "own": data["parish"], "default_id": data["default_id"], "can_change": ctx.can("statements.templates"), "is_cornerstone": TPL.is_cornerstone_editor(ctx.user_id), "show_retired": request.query_params.get("retired") == "1"})


@router.get("/giving/statements/templates/{template_id}", response_class=HTMLResponse)
def template_page(request: Request, template_id: int):
    user, parish, ctx, resp = _gate(request, "statements.run")
    if resp:
        return resp
    try:
        t = TPL.template_get(ctx, template_id, version_no=_opt_int(request.query_params.get("version")))
    except NotFound as e:
        return W.page(request, "donor_off.html", user, parish, ctx, "statements", {"reason": "notfound", "feature": "giving", "what": e.message, "back_href": "/giving/statements/templates"}, status_code=404)
    sample = TPL.sample_data()
    sheet = RND.sheet_context(t["parts"], parish_name="Example Episcopal Church", profile=sample["profile"], donor=sample["donor"], statement=sample["statement"],
                              period_from=sample["period_from"], period_to=sample["period_to"], recorded_as_of=sample["recorded_as_of"], today=dt.date.today(), settings={})
    import statement_text as TXT
    return W.page(request, "donor_statement_template_edit.html", user, parish, ctx, "statements", {
        **t, **sheet, "TEXT_PARTS": TPL.TEXT_PARTS, "TEXT_LABELS": TPL.TEXT_LABELS, "edit_text": {k: TXT.editable(t["parts"].get(k)) for k in TPL.TEXT_PARTS},
        "ACCENTS": TPL.ACCENTS, "SIZES": TPL.SIZES, "MERGE_FIELDS": TXT.MERGE_FIELDS})


@router.post("/giving/statements/templates/{template_id}/copy")
async def template_copy(request: Request, template_id: int):
    user, parish, ctx, resp = _gate(request, "statements.run")
    if resp:
        return resp
    form = await request.form()
    try:
        r = TPL.template_copy(ctx, template_id, form.get("name"))
    except DonorError as e:
        return W.back(request, "/giving/statements/templates", err=e.message)
    return W.back(request, f"/giving/statements/templates/{r['id']}", ok="Copied to your parish. Change the wording, then make it the default if you want it used.")


@router.post("/giving/statements/templates/{template_id}/save")
async def template_save(request: Request, template_id: int):
    user, parish, ctx, resp = _gate(request, "statements.run")
    if resp:
        return resp
    form = await request.form()
    try:
        r = TPL.template_save(ctx, template_id, TPL.parts_from_form(form), form.get("note"), name=form.get("name"), description=form.get("description"))
    except DonorError as e:
        return W.back(request, f"/giving/statements/templates/{template_id}", err=e.message)
    return W.back(request, f"/giving/statements/templates/{template_id}", ok=f"Saved as version {r['version_no']}.")


@router.post("/giving/statements/templates/{template_id}/default")
async def template_default(request: Request, template_id: int):
    user, parish, ctx, resp = _gate(request, "statements.run")
    if resp:
        return resp
    try:
        TPL.default_set(ctx, template_id)
    except DonorError as e:
        return W.back(request, "/giving/statements/templates", err=e.message)
    return W.back(request, "/giving/statements/templates", ok="This is now the parish's default statement template.")


@router.post("/giving/statements/templates/{template_id}/retire")
async def template_retire(request: Request, template_id: int):
    user, parish, ctx, resp = _gate(request, "statements.run")
    if resp:
        return resp
    form = await request.form()
    retire = form.get("restore") is None
    try:
        TPL.template_retire(ctx, template_id, retire)
    except DonorError as e:
        return W.back(request, "/giving/statements/templates", err=e.message)
    return W.back(request, "/giving/statements/templates", ok="Template retired." if retire else "Template brought back.")
