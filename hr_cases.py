"""
hr_cases.py -- 26-162 Beacon Onboarding and Offboarding: the screens (HR & Payroll > Onboarding & Offboarding).

    /hr/onboarding                       the HR Status Board (Jay: "the status of everything on one easy to use screen")
    /hr/onboarding/new                   open a case: details, then a preview of every item (applies / N/A and why)
    /hr/onboarding/case/{id}             the case page: the checklist on the left, the person, coverage and history on the right
    /hr/onboarding/templates             the checklist templates (read here; changed in the editor, hr_template_editor.py)

Every POST answers with a redirect and a one-line ?saved= or ?error= message, like the Employees screen.

ACCESS: the current entity must have `hr_lifecycle` turned on (checkreq.org_features) and the person must hold hr_admin,
benefits_admin or beacon_admin there. Cornerstone Mode entities are refused. "All my entities" on the board widens it to
every entity where the person passes the same test (so Jay sees EDOM and Claggett on one screen).

All rules live in logic/hr_lifecycle; this module only checks access, calls one function and renders.
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import cornerstone_mode
import db
import hr_case_setup
import hr_documents
import hr_my_steps
import hr_template_editor
import org_features
import org_time
import rbac
from logic.hr_lifecycle import board as board_mod
from logic.hr_lifecycle import cases, clock, conditions, documents, template_edit, templates
from logic.hr_lifecycle.errors import HrError

router = APIRouter()

FEATURE = "hr_lifecycle"
ROLES = ["hr_admin", "benefits_admin", "beacon_admin"]
HR_KINDS = ("onboard", "offboard")

_current_user = None
_current_org = None
_render = None


def register(app, *, current_user, current_org, render, templates=None) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)
    # the checklist editor is its own module; it is handed this module's gate and renderer, never imports them
    hr_template_editor.register(app, gate=_gate, render=lambda *a, **k: _render(*a, **k))
    # the case's documents (upload and view, Phase 2) are their own module too, handed the same gate and the entity lookup
    hr_documents.register(app, gate=_gate, allowed_org_ids=allowed_org_ids)
    # setting up one person's checklist (change / cross out / add / assign), and where "Open this case" lands (2026-10-27)
    hr_case_setup.register(app, gate=_gate, render=lambda *a, **k: _render(*a, **k), allowed_org_ids=allowed_org_ids)
    # "My steps": the steps assigned to the signed-in person, whatever their role (needs the template engine for the user-menu entry)
    if templates is not None:
        hr_my_steps.register(app, templates=templates, current_user=current_user, render=lambda *a, **k: _render(*a, **k))


# ------------------------------------------------------------------ access

def allowed_org_ids(user_id: int) -> list[int]:
    """Every entity where this person may use Onboarding & Offboarding."""
    rows = db.query(
        "SELECT DISTINCT ur.org_id FROM checkreq.user_roles ur "
        "JOIN checkreq.roles r ON r.key = ur.role_key AND r.is_active "
        "JOIN checkreq.org_features f ON f.org_id = ur.org_id AND f.feature_key = %s AND f.enabled "
        "WHERE ur.user_id = %s AND ur.revoked_at IS NULL AND ur.role_key = ANY(%s)", (FEATURE, user_id, ROLES))
    return sorted(r["org_id"] for r in rows if not cornerstone_mode.is_cornerstone_org(r["org_id"]))


def can_use(user: dict | None, org_id: int | None) -> bool:
    return bool(user and org_id and not cornerstone_mode.is_cornerstone_org(org_id)
                and org_features.is_enabled(org_id, FEATURE)
                and rbac.user_has_any_role(user["id"], ROLES, org_id=org_id))


def _gate(request: Request):
    """(user, org, None) when allowed; (None, None, response) otherwise."""
    user = _current_user(request)
    if not user:
        return None, None, RedirectResponse("/login", status_code=303)
    org = _current_org(request)
    org_id = org["id"] if org else None
    if not can_use(user, org_id):
        return None, None, RedirectResponse("/portal", status_code=303)
    return user, org, None


def _scope(request: Request, user: dict, org: dict) -> list[int]:
    if request.query_params.get("scope") == "all":
        ids = allowed_org_ids(user["id"])
        return ids or [org["id"]]
    return [org["id"]]


def _back(url: str, **msg) -> RedirectResponse:
    q = urlencode({k: v for k, v in msg.items() if v})
    return RedirectResponse(url + (("&" if "?" in url else "?") + q if q else ""), status_code=303)


async def _form(request: Request) -> dict:
    f = await request.form()
    return {k: v for k, v in f.items()}


def _units(org_id: int, kind: str) -> list[dict]:
    """Employing units for types 1-2: the entity's non-congregation records (Diocesan Office, Claggett Center). An entity whose
    staff are filed by department (DME) lists every department instead: the active parishes with a payroll code, as '002 Wilton'."""
    if kind in ("onboard", "offboard") and org_features.is_enabled(org_id, "hr_staff_by_department"):
        return [dict(r) for r in db.query(
            "SELECT id, code, coalesce(short_name, name) AS label FROM portal.parishes "
            "WHERE org_id = %s AND is_active AND coalesce(code, '') <> '' ORDER BY code", (org_id,))]
    return [dict(r) for r in db.query(
        "SELECT id, code, coalesce(short_name, name) AS label FROM portal.parishes "
        "WHERE org_id = %s AND is_active AND NOT coalesce(is_congregation, true) "
        "ORDER BY code, lower(coalesce(short_name, name))", (org_id,))]


# ------------------------------------------------------------------ the board

@router.get("/hr/onboarding", response_class=HTMLResponse)
def board_page(request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    org_ids = _scope(request, user, org)
    b = board_mod.build(org_ids)
    f = request.query_params.get("f") or "all"
    if f not in board_mod.FILTERS:
        f = "all"
    kind = request.query_params.get("kind") or ""
    rows = [r for r in b["rows"] if (f == "all" or r["flags"].get(f)) and (not kind or r["kind"] == kind)]
    view = request.query_params.get("view") or "cases"
    multi = len(allowed_org_ids(user["id"])) > 1
    closed_rows = [r for r in board_mod.closed(org_ids) if not kind or r["kind_label"] == templates.KINDS.get(kind)] if view == "closed" else []
    return _render(request, "hr_board.html", user, {
        "board": b, "rows": rows, "f": f, "kind": kind, "view": view, "owners": board_mod.by_owner(b), "closed_rows": closed_rows,
        "scope": request.query_params.get("scope") or "", "multi_entity": multi, "current_org": org,
        "kinds": templates.KINDS, "has_templates": bool(templates.list_templates(org["code"])),
    })


# ------------------------------------------------------------------ new case

def _new_ctx(org: dict, kind: str, values: dict, preview: dict | None = None, error: str | None = None) -> dict:
    return {"current_org": org, "kind": kind, "kind_label": templates.KINDS.get(kind, kind), "values": values,
            "units": _units(org["id"], kind), "preview": preview, "error": error,
            "separation_categories": cases.SEPARATION_CATEGORIES, "section_labels": templates.SECTION_LABELS,
            "describe": conditions.describe}


@router.get("/hr/onboarding/new", response_class=HTMLResponse)
def new_case_page(request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    kind = request.query_params.get("kind") or "onboard"
    if kind not in HR_KINDS:
        kind = "onboard"
    units = _units(org["id"], kind)
    values = {"is_clergy": "", "parish_id": units[0]["id"] if len(units) == 1 else ""}
    return _render(request, "hr_case_new.html", user, _new_ctx(org, kind, values))


@router.post("/hr/onboarding/new", response_class=HTMLResponse)
async def new_case_post(request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    form = await _form(request)
    kind = form.get("kind") if form.get("kind") in HR_KINDS else "onboard"
    raw = {k: form.get(k) for k in cases.EDITABLE if k in form}
    raw["is_clergy"] = form.get("is_clergy") == "yes"
    raw["housing_provided"] = {"yes": True, "no": False}.get(form.get("housing_provided") or "")
    try:
        if form.get("action") == "create":
            cid = cases.open_case(org["id"], kind, raw, user["id"])
            num = db.query_one("SELECT case_number FROM hr.cases WHERE id = %s", (cid,))["case_number"]
            # a new staff case opens on the checklist setup screen, so the person who opened it reviews the list before anything else
            return _back(f"/hr/onboarding/case/{cid}/setup" if kind in HR_KINDS else f"/hr/onboarding/case/{cid}",
                         saved=f"Opened {num}.", new="1" if kind in HR_KINDS else "")
        p = cases.preview(org["id"], kind, raw)
        return _render(request, "hr_case_new.html", user, _new_ctx(org, kind, form, preview=p))
    except HrError as e:
        return _render(request, "hr_case_new.html", user, _new_ctx(org, kind, form, error=e.message))


# ------------------------------------------------------------------ the case page

@router.get("/hr/onboarding/case/{case_id}", response_class=HTMLResponse)
def case_page(request: Request, case_id: int):
    user, org, err = _gate(request)
    if err:
        return err
    try:
        c = cases.get_case(case_id, allowed_org_ids(user["id"]) or [org["id"]])
    except HrError as e:
        return _back("/hr/onboarding", error=e.message)
    sections = []
    for key in templates.SECTIONS:
        items = [i for i in c["items"] if i["section"] == key]
        if not items:
            continue
        buckets: dict[tuple, list] = {}
        for i in items:
            buckets.setdefault((i["bucket_key"], i["bucket"]), []).append(i)
        req = [i for i in items if i["required"] and i["status"] != "not_applicable"]
        sections.append({"key": key, "label": templates.SECTION_LABELS[key],
                         "buckets": [{"label": k[1], "items": v} for k, v in sorted(buckets.items())],
                         "done": sum(1 for i in req if i["status"] in ("done", "waived")), "total": len(req)})
    zone = org_time.zone_name_for_org(c["org_id"])
    # documents (Phase 2): metadata only here, never a file read. An item that expects a document shows its filed copies and, for a role
    # that may file that kind, an Upload File button; the right-hand pane lists everything filed and views it.
    docs = documents.list_for_case(case_id, allowed_org_ids(user["id"]) or [org["id"]], user["id"])
    doc_options = documents.doc_type_options(user["id"], c["org_id"])
    item_docs: dict[str, list] = {}
    for d in docs:
        if d["is_current"] and d["item_key"]:
            item_docs.setdefault(d["item_key"], []).append(d)

    def local_date(v):
        """A timestamp as MM/DD/YY in the entity's time zone (the server runs in UTC)."""
        d = clock.local_date(v, c["org_id"])
        return d.strftime("%m/%d/%y") if d else ""

    return _render(request, "hr_case.html", user, {
        "c": c, "sections": sections, "current_org": org, "kinds": templates.KINDS, "units": _units(c["org_id"], c["case_kind"]),
        "coverages": cases.COVERAGES, "elections": cases.ELECTIONS, "enroll_statuses": cases.ENROLL_STATUSES,
        "plans": cases.PLANS, "tiers": cases.TIERS, "outcomes": cases.OUTCOMES,
        "separation_categories": cases.SEPARATION_CATEGORIES, "is_open": c["status"] in cases.OPEN_STATUSES,
        "today": clock.today(c["org_id"]), "describe": conditions.describe,
        "local_date": local_date, "local_time": lambda v: org_time.format_local(v, zone) if v else "",
        "enrollments": {e["coverage"]: e for e in c["enrollments"]},
        "item_labels": {i["item_key"]: i["label"] for i in c["items"]},
        "docs": docs, "current_docs": [d for d in docs if d["is_current"]], "old_docs": [d for d in docs if not d["is_current"]],
        "item_docs": item_docs, "doc_options": doc_options, "fileable": {o["key"] for o in doc_options},
        "doc_labels": {r["key"]: r["label"] for r in db.query("SELECT key, label FROM hr.doc_types")},
        "max_upload_mb": documents.MAX_BYTES // (1024 * 1024),
    })


def _case_ids(user) -> list[int]:
    return allowed_org_ids(user["id"])


@router.post("/hr/onboarding/case/{case_id}/item/{item_key}")
async def item_post(request: Request, case_id: int, item_key: str):
    user, org, err = _gate(request)
    if err:
        return err
    form = await _form(request)
    url = f"/hr/onboarding/case/{case_id}#item-{item_key}"
    try:
        cases.set_item_status(case_id, _case_ids(user), item_key, form.get("status") or "", form.get("note"), user["id"])
    except HrError as e:
        return _back(f"/hr/onboarding/case/{case_id}", error=e.message)
    return RedirectResponse(url, status_code=303)


@router.post("/hr/onboarding/case/{case_id}/update")
async def update_post(request: Request, case_id: int):
    user, org, err = _gate(request)
    if err:
        return err
    form = await _form(request)
    raw = {k: form.get(k) for k in cases.EDITABLE if k in form}
    if "is_clergy" in form:
        raw["is_clergy"] = form.get("is_clergy") == "yes"
    if "housing_provided" in form:
        raw["housing_provided"] = {"yes": True, "no": False}.get(form.get("housing_provided") or "")
    override = None
    if form.get("override_eligibility") == "clear":
        override = "clear"
    elif form.get("override_eligibility") == "yes":
        override = (form.get("health_eligible") == "yes", form.get("pension_eligible") == "yes", form.get("eligibility_note"))
    try:
        res = cases.update_case(case_id, _case_ids(user), raw, user["id"], eligibility_override=override)
    except HrError as e:
        return _back(f"/hr/onboarding/case/{case_id}", error=e.message)
    msg = "No changes." if not res["changed"] else (
        "Saved." + (f" {res['items']} checklist item(s) updated for the new dates or answers." if res["items"] else ""))
    return _back(f"/hr/onboarding/case/{case_id}", saved=msg)


@router.post("/hr/onboarding/case/{case_id}/enrollment")
async def enrollment_post(request: Request, case_id: int):
    user, org, err = _gate(request)
    if err:
        return err
    form = await _form(request)
    try:
        r = cases.record_enrollment(case_id, _case_ids(user), form.get("coverage") or "", form, user["id"])
    except HrError as e:
        return _back(f"/hr/onboarding/case/{case_id}", error=e.message)
    return _back(f"/hr/onboarding/case/{case_id}", saved=f"{cases.COVERAGES[r['coverage']]} saved.")


@router.post("/hr/onboarding/case/{case_id}/close")
async def close_post(request: Request, case_id: int):
    user, org, err = _gate(request)
    if err:
        return err
    form = await _form(request)
    try:
        r = cases.close_case(case_id, _case_ids(user), form.get("outcome") or "", user["id"], form.get("note"))
    except HrError as e:
        return _back(f"/hr/onboarding/case/{case_id}", error=e.message)
    tail = f" Roster: {r['roster']}." if r.get("roster") else ""
    return _back(f"/hr/onboarding/case/{case_id}", saved=f"Closed as {cases.OUTCOMES[r['outcome']].lower()}.{tail}")


@router.post("/hr/onboarding/case/{case_id}/offboard")
async def offboard_post(request: Request, case_id: int):
    user, org, err = _gate(request)
    if err:
        return err
    form = await _form(request)
    try:
        new_id = cases.start_offboarding(case_id, _case_ids(user), form, user["id"])
    except HrError as e:
        return _back(f"/hr/onboarding/case/{case_id}", error=e.message)
    return _back(f"/hr/onboarding/case/{new_id}", saved="Offboarding opened.")


# ------------------------------------------------------------------ templates (read here, changed in hr_template_editor.py)

@router.get("/hr/onboarding/templates", response_class=HTMLResponse)
def templates_page(request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    tlist = templates.list_templates(org["code"])
    tid = request.query_params.get("id")
    chosen = None
    if tid and tid.isdigit():
        chosen = templates.get_template(int(tid), org["code"])
    elif tlist:
        chosen = templates.get_template(tlist[0]["id"], org["code"])
    can_edit = bool(chosen and chosen["is_active"] and hr_template_editor.can_edit(user, org, chosen["case_kind"]))
    return _render(request, "hr_templates.html", user, {
        "templates": tlist, "chosen": chosen, "current_org": org, "kinds": templates.KINDS,
        "section_labels": templates.SECTION_LABELS, "describe": conditions.describe, "can_edit": can_edit,
        "history": template_edit.history(chosen["key"]) if chosen else [], "shared": hr_template_editor._shared_with(chosen) if chosen else None,
        "saved": request.query_params.get("saved"), "error": request.query_params.get("error")})
