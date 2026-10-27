"""
hr_case_setup.py -- set up ONE person's checklist (26-162, 2026-10-27).

Jay: "when someone is onboarding a new person ... review the checklist for this onboarding with the ability to add/change/remove the item
for this onboarding. Changes/Removing (cross it out as NA) will have the person's stamp", and "this initial setup screen [should] provide
the capability to assign steps to people."

    GET  /hr/onboarding/case/{id}/setup                          the screen: every step of this person's checklist
    POST /hr/onboarding/case/{id}/setup/item/{key}/change        change a step (wording, section, group, dates, required, document)
    POST /hr/onboarding/case/{id}/setup/add                      add a step to this person's checklist
    POST /hr/onboarding/case/{id}/setup/assign                   give steps a named person (only the changed ones are posted)
    POST /hr/onboarding/case/{id}/setup/item/{key}/status        cross a step out (needs a reason) or restore it

"Open this case" lands here (hr_cases.new_case_post), and a Set up checklist button on the case page comes back at any time while the
case is open. The rules live in logic/hr_lifecycle/cases.py; this module is the screen and the gate (the same people who work cases:
HR Admin, Benefits Admin, Beacon Admin). Nothing here touches the template.
"""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import org_time
from logic.hr_lifecycle import assignees, cases, clock, template_edit, templates
from logic.hr_lifecycle.errors import HrError

router = APIRouter()
_gate = None
_render = None
_allowed_org_ids = None


def register(app, *, gate, render, allowed_org_ids) -> None:
    global _gate, _render, _allowed_org_ids
    _gate, _render, _allowed_org_ids = gate, render, allowed_org_ids
    app.include_router(router)


def _ids(user: dict, org: dict) -> list[int]:
    return _allowed_org_ids(user["id"]) or [org["id"]]


def _back(url: str, *, anchor: str = "", **msg) -> RedirectResponse:
    """A redirect carrying a banner message in the query string; the #anchor goes AFTER the query (the other way round loses the message)."""
    q = urlencode({k: v for k, v in msg.items() if v})
    return RedirectResponse(url + (("&" if "?" in url else "?") + q if q else "") + (f"#{anchor}" if anchor else ""), status_code=303)


async def _form(request: Request) -> dict:
    f = await request.form()
    return {k: v for k, v in f.items()}


def _setup_url(case_id: int) -> str:
    return f"/hr/onboarding/case/{case_id}/setup"


@router.get("/hr/onboarding/case/{case_id}/setup", response_class=HTMLResponse)
def setup_page(request: Request, case_id: int):
    user, org, err = _gate(request)
    if err:
        return err
    try:
        c = cases.get_case(case_id, _ids(user, org))
    except HrError as e:
        return _back("/hr/onboarding", error=e.message)
    if c["status"] not in cases.OPEN_STATUSES:
        return _back(f"/hr/onboarding/case/{case_id}", error="This case is closed, so its checklist can no longer be changed.")
    # who a step may be given to at this entity, plus anybody already on a step (assigned when the list was different)
    people = [{"id": p["id"], "name": p["name"]} for p in assignees.people_for(c["org_id"])]
    listed = {p["id"] for p in people}
    for it in c["items"]:
        uid = it.get("assigned_user_id")
        if uid and uid not in listed and it.get("assigned_name"):
            people.append({"id": uid, "name": it["assigned_name"] + " (no longer listed)"})
            listed.add(uid)
    sec_order = {k: n for n, k in enumerate(templates.SECTIONS)}
    rows = sorted(c["items"], key=lambda i: (sec_order.get(i["section"], 99), i["bucket_key"], i["sort_order"] or 0, i["id"]))
    for it in rows:
        it["section_label"] = templates.SECTION_LABELS.get(it["section"], it["section"])
        it["crossed"] = it["status"] == "not_applicable" and not it["auto_na"]
        it["rule_na"] = it["status"] == "not_applicable" and bool(it["auto_na"])
    zone = org_time.zone_name_for_org(c["org_id"])

    def local_date(v):
        d = clock.local_date(v, c["org_id"])
        return d.strftime("%m/%d/%y") if d else ""

    counts = {
        "total": sum(1 for i in rows if not i["rule_na"]),
        "assigned": sum(1 for i in rows if i["status"] == "open" and i.get("assigned_user_id")),
        "crossed": sum(1 for i in rows if i["crossed"]),
        "rule_na": sum(1 for i in rows if i["rule_na"]),
        "by_hand": sum(1 for i in rows if i["added_by_hand"] or i.get("customized_at")),
    }
    return _render(request, "hr_case_setup.html", user, {
        "c": c, "rows": rows, "people": people, "counts": counts, "current_org": org, "kinds": templates.KINDS,
        "sections": [{"key": k, "label": templates.SECTION_LABELS[k]} for k in templates.SECTIONS],
        "owners": templates.OWNERS, "doc_types": template_edit.doc_types(), "is_new": request.query_params.get("new") == "1",
        "local_date": local_date, "local_time": lambda v: org_time.format_local(v, zone) if v else "",
    })


@router.post("/hr/onboarding/case/{case_id}/setup/item/{item_key}/change")
async def change_post(request: Request, case_id: int, item_key: str):
    user, org, err = _gate(request)
    if err:
        return err
    form = await _form(request)
    try:
        res = cases.change_item(case_id, _ids(user, org), item_key, form, user["id"])
    except HrError as e:
        return _back(_setup_url(case_id), error=e.message)
    return _back(_setup_url(case_id), anchor=f"item-{item_key}", saved="Step changed." if res["changed"] else "No changes.")


@router.post("/hr/onboarding/case/{case_id}/setup/add")
async def add_post(request: Request, case_id: int):
    user, org, err = _gate(request)
    if err:
        return err
    form = await _form(request)
    try:
        res = cases.add_item(case_id, _ids(user, org), form, user["id"])
    except HrError as e:
        return _back(_setup_url(case_id), error=e.message)
    return _back(_setup_url(case_id), anchor=f"item-{res['item_key']}", saved="Step added.")


@router.post("/hr/onboarding/case/{case_id}/setup/assign")
async def assign_post(request: Request, case_id: int):
    user, org, err = _gate(request)
    if err:
        return err
    form = await _form(request)
    changes = {k[len("assign_"):]: (v or None) for k, v in form.items() if k.startswith("assign_") and len(k) > len("assign_")}
    try:
        res = cases.assign_many(case_id, _ids(user, org), changes, user["id"])
    except HrError as e:
        return _back(_setup_url(case_id), error=e.message)
    n = res["changed"]
    return _back(_setup_url(case_id), saved=("No changes." if not n else f"{n} assignment{'s' if n != 1 else ''} saved."))


@router.post("/hr/onboarding/case/{case_id}/setup/item/{item_key}/status")
async def status_post(request: Request, case_id: int, item_key: str):
    user, org, err = _gate(request)
    if err:
        return err
    form = await _form(request)
    status = form.get("status") or ""
    if status not in ("open", "not_applicable"):
        return _back(_setup_url(case_id), error="Choose Cross out or Restore.")
    try:
        cases.set_item_status(case_id, _ids(user, org), item_key, status, form.get("note"), user["id"])
    except HrError as e:
        return _back(_setup_url(case_id), error=e.message)
    return _back(_setup_url(case_id), anchor=f"item-{item_key}", saved="Step crossed out." if status == "not_applicable" else "Step restored.")
