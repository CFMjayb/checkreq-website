"""
donor_routes_admin.py -- Beacon Donor Management: import, duplicates and merge, directory and export, settings.

Routes only; every rule lives in the donor_* services. Must be included BEFORE donor_routes_people's
/people/{person_id} (see donor_register.py) so /people/import, /people/settings and the rest are not read as ids.
"""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

import db
import donor_membership as MS
import donor_merge as MG
import donor_people as P
import donor_roles as R
import donor_template as TPL
import donor_web as W
from donor_core import DonorError, DIOCESAN_CATEGORIES, InvalidInput, PARISH_DONOR_ROLES, label

router = APIRouter(dependencies=[Depends(W.check_path_ids)])
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def register(app) -> None:
    app.include_router(router)


def _opt_int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ── Import (the Excel template) ─────────────────────────────────────────────────────────────────
@router.get("/people/import", response_class=HTMLResponse)
def import_form(request: Request):
    user, parish, ctx, resp = W.gate(request, need="people.edit", active="import")
    if resp:
        return resp
    return W.page(request, "donor_import.html", user, parish, ctx, "import", {"report": None})


@router.get("/people/import/template.xlsx")
def import_template(request: Request):
    user, parish, ctx, resp = W.gate(request, need="people.edit", active="import")
    if resp:
        return resp
    return Response(TPL.template_build(), media_type=XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{TPL.template_filename()}"'})


@router.post("/people/import", response_class=HTMLResponse)
async def import_run(request: Request):
    user, parish, ctx, resp = W.gate(request, need="people.edit", active="import")
    if resp:
        return resp
    form = await request.form()
    up = form.get("file")
    report, error = None, None
    if up is None or not hasattr(up, "read"):
        error = "Choose a file to upload."
    else:
        size = getattr(up, "size", None)
        if size is not None and size > TPL.MAX_BYTES:
            error = f"That file is larger than {TPL.MAX_BYTES // (1024 * 1024)} MB."
        else:
            data = await up.read()
            if not data[:4] == b"PK\x03\x04":                # an .xlsx is a zip file; anything else is refused up front
                error = "That is not an Excel (.xlsx) workbook. Download the template and fill it in."
            else:
                real = form.get("load_for_real") is not None
                try:
                    report = TPL.template_import(ctx, data, dry_run=not real, skip_duplicates=form.get("create_duplicates") is None)
                except DonorError as e:
                    error = e.message
    return W.page(request, "donor_import.html", user, parish, ctx, "import", {"report": report, "error": error})


# ── Duplicates and merge ────────────────────────────────────────────────────────────────────────
def _diocese_parish_ids(ctx):
    if not ctx.is_diocesan_admin or ctx.org_id is None:
        return None
    return [r["id"] for r in db.query("SELECT id FROM portal.parishes WHERE org_id = %s", (ctx.org_id,))]


@router.get("/people/duplicates", response_class=HTMLResponse)
def duplicates(request: Request, scope: str = ""):
    user, parish, ctx, resp = W.gate(request, need="people.edit", active="duplicates")
    if resp:
        return resp
    ids = _diocese_parish_ids(ctx) if scope == "diocese" else None
    error = None
    try:
        pairs = MG.duplicate_candidates(ctx, parish_ids=ids)
    except DonorError as e:
        pairs, error = [], e.message
    return W.page(request, "donor_duplicates.html", user, parish, ctx, "duplicates",
                  {"pairs": pairs, "scope": scope if ids is not None else "", "error": error,
                   "can_diocese": ctx.is_diocesan_admin})


@router.get("/people/merge", response_class=HTMLResponse)
def merge_form(request: Request, a: int = 0, b: int = 0, scope: str = ""):
    user, parish, ctx, resp = W.gate(request, need="people.edit", active="duplicates")
    if resp:
        return resp
    ids = _diocese_parish_ids(ctx) if scope == "diocese" else None
    try:
        prev = MG.merge_preview(ctx, a, b, diocese_parish_ids=ids)
    except DonorError as e:
        return W.back(request, "/people/duplicates", err=e.message)
    return W.page(request, "donor_merge.html", user, parish, ctx, "duplicates", {"prev": prev, "scope": scope, "a": a, "b": b,
                                                                                  "P": P})


@router.post("/people/merge")
async def merge_run(request: Request):
    user, parish, ctx, resp = W.gate(request, need="people.edit", active="duplicates")
    if resp:
        return resp
    form = await request.form()
    scope = form.get("scope") or ""
    ids = _diocese_parish_ids(ctx) if scope == "diocese" else None
    # The merge screen posts the two record ids plus a "keep" button (a or b) and a per-field pick (a or b).
    # Callers that already know which record survives can post survivor_id / merged_id and choice_<field> instead.
    keep = form.get("keep")
    choices = {k[7:]: v for k, v in form.items() if k.startswith("choice_") and v in ("survivor", "merged")}
    if keep in ("a", "b"):
        id_a, id_b = _opt_int(form.get("a_id")), _opt_int(form.get("b_id"))
        a, b = (id_a, id_b) if keep == "a" else (id_b, id_a)
        for k, v in form.items():
            if k.startswith("pick_") and v in ("a", "b"):
                choices[k[5:]] = "survivor" if v == keep else "merged"
    else:
        a, b = _opt_int(form.get("survivor_id")), _opt_int(form.get("merged_id"))
    try:
        if a is None or b is None:
            raise InvalidInput("Pick the two people to merge.")
        r = MG.person_merge(ctx, a, b, choices, diocese_parish_ids=ids)
        notes = " ".join(r["notes"])
        return W.back(request, f"/people/{a}" if scope != "diocese" else "/people/duplicates?scope=diocese",
                      ok="Merged. " + notes if notes else "Merged.")
    except DonorError as e:
        return W.back(request, f"/people/merge?a={a or 0}&b={b or 0}&scope={scope}", err=e.message)


@router.post("/people/not-duplicate")
async def not_duplicate(request: Request):
    user, parish, ctx, resp = W.gate(request, need="people.edit", active="duplicates")
    if resp:
        return resp
    form = await request.form()
    scope = form.get("scope") or ""
    url = "/people/duplicates" + ("?scope=diocese" if scope == "diocese" else "")
    try:
        MG.not_a_duplicate(ctx, _opt_int(form.get("a")) or 0, _opt_int(form.get("b")) or 0)
        return W.back(request, url, ok="Marked as not a duplicate.")
    except DonorError as e:
        return W.back(request, url, err=e.message)


# ── Directory and export ────────────────────────────────────────────────────────────────────────
@router.get("/people/directory", response_class=HTMLResponse)
def directory(request: Request, minors: str = ""):
    user, parish, ctx, resp = W.gate(request)
    if resp:
        return resp
    error = None
    try:
        rows = P.directory_list(ctx, include_minors=bool(minors))
    except DonorError as e:
        rows, error = [], e.message
    return W.page(request, "donor_directory.html", user, parish, ctx, "people", {"rows": rows, "minors": bool(minors), "error": error})


@router.get("/people/export.csv")
def export_csv(request: Request, kind: str = "roll", minors: str = ""):
    user, parish, ctx, resp = W.gate(request)
    if resp:
        return resp
    try:
        text = P.people_export(ctx, kind=kind, include_minors=bool(minors))
    except DonorError as e:
        return W.back(request, "/people", err=e.message)
    name = f"People {kind} {_dt.date.today().isoformat()}.csv"
    return Response(text, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ── Users (Setup > Users): who has a login here and which donor roles each holds, like TouchPoint's Users list ──────
@router.get("/people/users", response_class=HTMLResponse)
def users_page(request: Request, q: str = "", role: str = "", within: str = "", idle: str = "", sort: str = "name",
               dir: str = "asc", page: str = "1", rows: str = "25"):
    user, parish, ctx, resp = W.gate(request, need="roles.manage", feature="", active="users")
    if resp:
        return resp
    error, data = None, {"rows": [], "total": 0, "page": 1, "pages": 1, "per_page": 25, "role_options": []}
    try:
        data = R.users_list(ctx, q=q, role=role, within_days=within, idle_days=idle, sort=sort, direction=dir, page=page, rows=rows)
    except DonorError as e:
        error = e.message
    return W.page(request, "donor_users.html", user, parish, ctx, "users", {
        "data": data, "error": error, "f": {"q": q, "role": role, "within": within, "idle": idle, "sort": sort, "dir": dir, "rows": rows},
        "row_choices": R.USER_ROW_CHOICES})


@router.get("/people/users/{user_id}", response_class=HTMLResponse)
def user_edit_page(user_id: int, request: Request):
    user, parish, ctx, resp = W.gate(request, need="roles.manage", feature="", active="users")
    if resp:
        return resp
    try:
        u = R.user_detail(ctx, user_id)
    except DonorError as e:
        return W.back(request, "/people/users", err=e.message)
    return W.page(request, "donor_user_edit.html", user, parish, ctx, "users", {"u": u})


@router.post("/people/users/{user_id}/roles")
async def user_roles_save(user_id: int, request: Request):
    user, parish, ctx, resp = W.gate(request, need="roles.manage", feature="", active="users")
    if resp:
        return resp
    form = await request.form()
    back = f"/people/users/{user_id}"
    try:
        r = R.roles_set(ctx, user_id, form.getlist("role_key"), form.get("note"))
    except DonorError as e:
        return W.back(request, back, err=e.message)
    if not (r["added"] or r["removed"]):
        return W.back(request, back, ok="Nothing to change.")
    parts = ["Saved."]
    if r["added"]:
        parts.append("Gave " + ", ".join(r["added"]) + ".")
    if r["removed"]:
        parts.append("Took away " + ", ".join(r["removed"]) + ".")
    return W.back(request, back, ok=" ".join(parts))


# ── Settings: access, status codes, activation ──────────────────────────────────────────────────
@router.get("/people/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    user, parish, ctx, resp = W.gate(request, need=None, feature="", active="settings")
    if resp:
        return resp
    if not (ctx.can("roles.manage") or ctx.can("parish.activate") or (ctx.settings.get("people_enabled") and ctx.can("membership.edit"))):
        return W.page(request, "donor_off.html", user, parish, ctx, "settings", {"reason": "permission", "feature": "people"}, status_code=403)
    codes = MS.status_code_list(ctx, include_inactive=True) if (ctx.settings.get("people_enabled") and ctx.can("membership.view")) else []
    catalog = R.role_catalog(max_phase=2 if ctx.settings.get("giving_enabled") else 1)
    return W.page(request, "donor_settings.html", user, parish, ctx, "settings", {
        "codes": codes, "catalog": [r for r in catalog if r["scope"] == "parish"],
        "categories": DIOCESAN_CATEGORIES, "s": ctx.settings,
    })


async def _settings_act(request: Request, need_feature: bool, action):
    user, parish, ctx, resp = W.gate(request, need=None, feature="people" if need_feature else "", active="settings")
    if resp:
        return resp
    form = await request.form()
    try:
        return W.back(request, "/people/settings", ok=action(ctx, form))
    except DonorError as e:
        return W.back(request, "/people/settings", err=e.message)


@router.post("/people/settings/activation")
async def settings_activation(request: Request):
    def act(ctx, form):
        changes = {}
        for k in ("people_enabled", "giving_enabled", "allow_single_person_batch", "qbo_posting_enabled", "portal_enabled"):
            if f"has_{k}" in form:
                changes[k] = form.get(k) is not None
        for k in ("qbo_company_key", "default_cash_account", "processing_fee_account", "due_from_diocese_account",
                  "investment_account", "in_kind_account", "default_class"):
            if k in form:
                changes[k] = form.get(k)
        R.settings_update(ctx, changes)
        return "Settings saved."
    return await _settings_act(request, False, act)


@router.post("/people/settings/status-code")
async def settings_status_code(request: Request):
    def act(ctx, form):
        cid = _opt_int(form.get("code_id"))
        r = MS.status_code_save(ctx, code_id=cid, code=form.get("code"), label=form.get("label"),
                                diocesan_category=form.get("diocesan_category") or None,
                                sort_order=_opt_int(form.get("sort_order")),
                                is_active=(form.get("is_active") is not None) if cid else None)
        return "Status code added." if r.get("created") else "Status code saved."
    return await _settings_act(request, True, act)


@router.post("/people/settings/role/grant")
async def settings_role_grant(request: Request):
    def act(ctx, form):
        uid = _opt_int(form.get("user_id"))
        if uid is None:
            raise InvalidInput("Pick a person.")
        r = R.role_grant(ctx, uid, form.get("role_key") or "", form.get("note"))
        return "Role given." if r["created"] else "They already have that role."
    return await _settings_act(request, False, act)


@router.post("/people/settings/role/revoke")
async def settings_role_revoke(request: Request):
    def act(ctx, form):
        uid = _opt_int(form.get("user_id"))
        done = R.role_revoke(ctx, uid or 0, form.get("role_key") or "", form.get("reason"))
        return "Role removed." if done else "They did not have that role."
    return await _settings_act(request, False, act)
