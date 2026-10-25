"""
hr_template_editor.py -- the checklist editor (26-162, 2026-10-25).

Jay: "I don't see a place where I can actually build the flow of onboarding and offboarding. And we're going to need that."
and, later: "setting up the items to be included in the onboarding and each items' parameters".

    GET  /hr/onboarding/templates/{id}/edit     the editor: every item of the ACTIVE version of a checklist
    POST /hr/onboarding/templates/{id}/save     check the whole edited list, then publish it as the next version

The rules live in logic/hr_lifecycle/template_edit.py (checking and publishing, the same templates.publish() the CSV seed
uses, so a save that changes nothing makes no new version). This module is only the screen and the gate. Wired from
hr_cases.register() (so main.py is untouched): the gate and the page renderer are handed in, never imported.

A save is a NEW VERSION. A case keeps the items it opened with, so no open case changes.
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import rbac
from logic.hr_lifecycle import conditions, template_edit, templates
from logic.hr_lifecycle.errors import HrError

router = APIRouter()

_gate = None
_render = None

# where a new item's "due date counts from" starts, by kind of checklist
DEFAULT_BASIS = {"onboard": "start_date", "offboard": "separation_date", "benefits_enroll": "benefits_effective",
                 "benefits_end": "benefits_effective", "change": "opened"}


def register(app, *, gate, render) -> None:
    global _gate, _render
    _gate, _render = gate, render
    app.include_router(router)


# ------------------------------------------------------------------ helpers

def _list_url(**msg) -> str:
    q = urlencode({k: v for k, v in msg.items() if v})
    return "/hr/onboarding/templates" + (("?" + q) if q else "")


def can_edit(user: dict, org: dict, case_kind: str) -> bool:
    return bool(user and org and rbac.user_has_any_role(user["id"], template_edit.edit_roles(case_kind), org_id=org["id"]))


def _open(request: Request, template_id: int):
    """(user, org, template, None) when the person may edit this checklist; (None, None, None, response) otherwise."""
    user, org, err = _gate(request)
    if err:
        return None, None, None, err
    t = templates.get_template(template_id, org["code"])
    if not t:
        return None, None, None, RedirectResponse(_list_url(error="That checklist was not found for this entity."), status_code=303)
    if not can_edit(user, org, t["case_kind"]):
        return None, None, None, RedirectResponse(
            _list_url(error="You do not have permission to change this checklist.", id=template_id), status_code=303)
    if not t["is_active"]:
        current = template_edit.active_id(t["key"])
        if current:
            return None, None, None, RedirectResponse(
                f"/hr/onboarding/templates/{current}/edit?" + urlencode({"notice": "That was an older version. This is the current one."}),
                status_code=303)
        return None, None, None, RedirectResponse(_list_url(error="That checklist has no current version."), status_code=303)
    return user, org, t, None


def _shared_with(t: dict) -> dict:
    applies = list(t["applies_to"] or [])
    return {"every_diocese": "*" in applies, "codes": [c for c in applies if c != "*"]}


def _label_for(condition: str | None) -> str:
    try:
        return conditions.describe(condition)
    except Exception:           # a hand-typed condition the describer cannot read is shown as typed
        return condition or "Everyone"


def _shape_posted(raw_items) -> list[dict]:
    """The items as the screen would show them again after a refused save: what the person typed, nothing dropped."""
    out = []
    if not isinstance(raw_items, list):
        return out
    for r in raw_items:
        if not isinstance(r, dict):
            continue
        cond = " ".join(str(r.get("condition") or "").split())
        out.append({
            "item_key": str(r.get("item_key") or ""), "label": str(r.get("label") or ""),
            "help_text": str(r.get("help_text") or ""), "section": str(r.get("section") or ""),
            "owner_group": str(r.get("owner_group") or ""), "due_basis": str(r.get("due_basis") or ""),
            "due_offset_days": r.get("due_offset_days"), "business_days": template_edit._truthy(r.get("business_days")),
            "hard_deadline_days": r.get("hard_deadline_days"), "required": template_edit._truthy(r.get("required")),
            "doc_type_key": str(r.get("doc_type_key") or ""), "condition": cond, "condition_label": _label_for(cond or None),
        })
    return out


def _page(request, user, org, t, *, items, name, base_version, problems=None, notice=None):
    kind = t["case_kind"]
    shared = _shared_with(t)
    data = {
        "items": items,
        "sections": [{"key": k, "label": templates.SECTION_LABELS[k]} for k in templates.SECTIONS],
        "owners": templates.OWNERS,
        "bases": [{"key": k, "label": template_edit.BASIS_LABELS[k]} for k in templates.BASES],
        "docTypes": [{"key": d["key"], "label": d["label"], "sensitivity": d["sensitivity"]} for d in template_edit.doc_types()],
        "presets": [{"value": v, "label": lab} for v, lab in template_edit.CONDITION_PRESETS],
        "coverages": sorted(conditions.COVERAGES),
        "defaultBasis": DEFAULT_BASIS.get(kind, "start_date"),
        "limits": {"label": template_edit.MAX_LABEL, "help": template_edit.MAX_HELP, "name": template_edit.MAX_NAME,
                   "days": template_edit.MAX_DAYS, "items": template_edit.MAX_ITEMS},
        "baseVersion": base_version,
        "shared": shared,
        # as last saved, so the screen can say what is changed even after a refused save brought back what was typed
        "original": template_edit.editor_items(t["id"]),
        "originalName": t["label"],
    }
    return _render(request, "hr_template_edit.html", user, {
        "t": t, "name": name, "base_version": base_version, "editor_data": data, "problems": problems or [],
        "notice": notice, "shared": shared, "history": template_edit.history(t["key"]), "kinds": templates.KINDS,
        "current_org": org, "section_labels": templates.SECTION_LABELS, "max_name": template_edit.MAX_NAME,
    })


# ------------------------------------------------------------------ routes

@router.get("/hr/onboarding/templates/{template_id}/edit", response_class=HTMLResponse)
def edit_page(request: Request, template_id: int):
    user, org, t, err = _open(request, template_id)
    if err:
        return err
    return _page(request, user, org, t, items=template_edit.editor_items(t["id"]), name=t["label"], base_version=t["version"],
                 notice=request.query_params.get("notice"))


@router.post("/hr/onboarding/templates/{template_id}/save", response_class=HTMLResponse)
async def save(request: Request, template_id: int):
    user, org, t, err = _open(request, template_id)
    if err:
        return err
    form = await request.form()
    name = str(form.get("name") or "")
    try:
        base_version = int(str(form.get("base_version") or "0"))
    except ValueError:
        base_version = 0
    try:
        raw_items = json.loads(str(form.get("payload") or "null"))
    except ValueError:
        raw_items = None
    try:
        result = template_edit.publish_edit(t["id"], base_version, name, raw_items, user["id"])
    except HrError as e:
        problems = [p for p in e.message.split("\n") if p.strip()]
        if e.code == "refused":
            # somebody else published first: show what they published, not the stale list (the typed list is not saved either way)
            return RedirectResponse(
                f"/hr/onboarding/templates/{template_edit.active_id(t['key']) or t['id']}/edit?" + urlencode({"notice": e.message}),
                status_code=303)
        return _page(request, user, org, t, items=_shape_posted(raw_items), name=name or t["label"], base_version=base_version,
                     problems=problems)
    return RedirectResponse(_list_url(id=result["template_id"], saved=result["status"]), status_code=303)
