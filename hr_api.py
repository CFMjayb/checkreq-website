"""
hr_api.py -- the Onboarding & Offboarding service API (26-162). One endpoint = one named tool, so this maps 1:1 onto an
MCP server later (Jay's standing architecture). All business logic lives in logic/hr_lifecycle; this file only
authenticates, parses, calls one function and shapes the answer. Built the same way as payroll_api.py.

AUTH. A machine key in the X-API-Key header, checked against Secret Manager secret `beacon-hr-api-keys` (a JSON object
{"<entity code>": "<key>"}, e.g. {"EDOM": "..."}). THE ENTITY COMES FROM THE KEY, never from a parameter: an EDOM key can
only ever touch EDOM's cases. A missing secret, key, or an entity without hr_lifecycle on is a 401 and nothing runs.
Environment override for tests and local runs: BEACON_HR_API_KEYS (same JSON). Writes made through the API are recorded
with no user (actor NULL), which the case history shows as "Beacon".

Never returns or accepts an SSN, birth date, pay or bank number. Errors: 401 key, 404 not found, 409 refused, 422 invalid
input or personal data in a note. Body {"error": code, "message": text}.

    beacon_hr_get_status_board   GET   /api/hr/status
    beacon_hr_list_cases         GET   /api/hr/cases?kind=&status=open|closed|all&q=
    beacon_hr_get_case           GET   /api/hr/cases/{id}
    beacon_hr_open_case          POST  /api/hr/cases                         {kind, first_name, last_name, ...}
    beacon_hr_update_case        PATCH /api/hr/cases/{id}                    {any editable field}
    beacon_hr_set_item_status    POST  /api/hr/cases/{id}/items/{item_key}   {status, note}
    beacon_hr_record_enrollment  PUT   /api/hr/cases/{id}/enrollments/{coverage}  {election, plan_key, tier, status, ...}
    beacon_hr_list_due_items     GET   /api/hr/items/due?within_days=10
    beacon_hr_list_templates     GET   /api/hr/templates
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import time

from fastapi import APIRouter, Body, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

import db
import org_features
from logic.hr_lifecycle import board as board_mod
from logic.hr_lifecycle import cases, clock, templates
from logic.hr_lifecycle.errors import HrError

router = APIRouter()

_KEYS_SECRET = "beacon-hr-api-keys"  # pragma: allowlist secret (a Secret Manager NAME, not a value)
_KEYS_TTL_SECONDS = 300
_cache: dict = {"at": 0.0, "keys": {}}


def register(app) -> None:
    app.include_router(router)


def _load_keys() -> dict[str, str]:
    env = os.environ.get("BEACON_HR_API_KEYS", "").strip()
    if env:
        try:
            return {str(k).upper(): str(v) for k, v in json.loads(env).items() if v}
        except Exception:
            return {}
    now = time.time()
    if now - _cache["at"] < _KEYS_TTL_SECONDS and _cache["keys"]:
        return _cache["keys"]
    try:
        from google.cloud import secretmanager
        project = os.environ.get("FIRESTORE_PROJECT", "cfm-qbo-mcp")
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project}/secrets/{_KEYS_SECRET}/versions/latest"
        raw = client.access_secret_version(name=name).payload.data.decode("utf-8").strip()
        keys = {str(k).upper(): str(v) for k, v in json.loads(raw).items() if v}
    except Exception:
        keys = {}
    _cache["at"], _cache["keys"] = now, keys
    return keys


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def _auth(request: Request):
    supplied = request.headers.get("x-api-key", "")
    if not supplied:
        return None, _err(401, "unauthorized", "An API key is required.")
    org_code = None
    for code, key in _load_keys().items():
        if hmac.compare_digest(supplied.encode(), key.encode()):  # every key compared: timing says nothing
            org_code = code
    if not org_code:
        return None, _err(401, "unauthorized", "That API key is not valid.")
    org = db.query_one("SELECT id, code, name FROM checkreq.organizations WHERE upper(code) = %s", (org_code,))
    if not org or not org_features.is_enabled(org["id"], "hr_lifecycle"):
        return None, _err(401, "unauthorized", "That key is not set up for an entity with Onboarding & Offboarding on.")
    return org, None


def _ok(payload) -> JSONResponse:
    return JSONResponse(jsonable_encoder(payload))


def _he(e: HrError) -> JSONResponse:
    return _err(e.status, e.code, e.message)


_CASE_FIELDS = ("id", "case_number", "org_code", "case_kind", "status", "close_outcome", "first_name", "last_name",
                "preferred_name", "position", "is_clergy", "exemption", "hours_per_week", "health_eligible",
                "pension_eligible", "housing_provided", "start_date", "benefits_effective_date", "separation_date",
                "separation_category", "parish_id", "parish_label", "staff_id", "payroll_employee_ref", "linked_case_id",
                "template_key", "template_version", "opened_at", "closed_at", "last_activity_at", "notes")
_ITEM_FIELDS = ("item_key", "section", "label", "owner_group", "required", "status", "status_note", "state", "due_date",
                "hard_deadline_date", "bucket", "done_at", "done_by_name")


def _case_out(c: dict) -> dict:
    out = {k: c.get(k) for k in _CASE_FIELDS}
    out["items"] = [{k: i.get(k) for k in _ITEM_FIELDS} for i in c.get("items", [])]
    out["enrollments"] = [{k: e.get(k) for k in ("coverage", "election", "plan_key", "tier", "dependents_count", "status",
                                                 "submitted_at", "confirmed_at", "effective_date", "end_date", "carrier_ref")}
                          for e in c.get("enrollments", [])]
    out["sections"] = c.get("counts")
    out["ready_to_close"] = c.get("ready_to_close")
    return out


# ---------------------------------------------------------------- reads

@router.get("/api/hr/status")
def beacon_hr_get_status_board(request: Request):
    """The HR Status Board: the summary strip and one row per open case with a chip per section."""
    org, bad = _auth(request)
    if bad:
        return bad
    b = board_mod.build([org["id"]])
    rows = [{k: r[k] for k in ("id", "case_number", "kind", "status", "name", "track", "unit", "anchor", "anchor_label",
                               "next", "waiting_on", "stalled", "ready", "late_count", "sections")} for r in b["rows"]]
    return _ok({"today": b["today"], "summary": b["summary"], "rows": rows})


@router.get("/api/hr/cases")
def beacon_hr_list_cases(request: Request, kind: str | None = None, status: str = "open", q: str = ""):
    org, bad = _auth(request)
    if bad:
        return bad
    if status not in ("open", "closed", "all"):
        return _err(422, "invalid", "status must be open, closed or all.")
    return _ok({"cases": cases.list_cases([org["id"]], kind=kind or None, status=status, q=q)})


@router.get("/api/hr/cases/{case_id}")
def beacon_hr_get_case(request: Request, case_id: int):
    org, bad = _auth(request)
    if bad:
        return bad
    try:
        return _ok(_case_out(cases.get_case(case_id, [org["id"]])))
    except HrError as e:
        return _he(e)


@router.get("/api/hr/items/due")
def beacon_hr_list_due_items(request: Request, within_days: int = 10):
    """Open items on open cases that are past due or due (or at a hard deadline) within N days, hard deadlines first."""
    org, bad = _auth(request)
    if bad:
        return bad
    within_days = max(0, min(int(within_days), 90))
    limit = clock.today(org["id"]) + dt.timedelta(days=within_days)
    rows = db.query(
        "SELECT c.id AS case_id, c.case_number, c.last_name, c.first_name, c.case_kind, i.item_key, i.label, i.section, "
        "       i.owner_group, i.due_date, i.hard_deadline_date "
        "FROM hr.case_items i JOIN hr.cases c ON c.id = i.case_id "
        "WHERE c.org_id = %s AND c.status = ANY(%s) AND i.status = 'open' "
        "  AND (i.due_date <= %s OR i.hard_deadline_date <= %s) "
        "ORDER BY (i.hard_deadline_date IS NULL), i.hard_deadline_date, i.due_date, c.case_number",
        (org["id"], list(cases.OPEN_STATUSES), limit, limit))
    return _ok({"within_days": within_days, "items": rows})


@router.get("/api/hr/templates")
def beacon_hr_list_templates(request: Request):
    org, bad = _auth(request)
    if bad:
        return bad
    out = []
    for t in templates.list_templates(org["code"]):
        full = templates.get_template(t["id"], org["code"])
        if not full:
            continue
        out.append({k: full[k] for k in ("id", "key", "version", "label", "case_kind", "applies_to")} |
                   {"items": [{k: i[k] for k in ("item_key", "section", "label", "owner_group", "due_basis", "due_offset_days",
                                                 "hard_deadline_days", "required", "condition")} for i in full["items"]]})
    return _ok({"templates": out})


# ---------------------------------------------------------------- writes

@router.post("/api/hr/cases")
def beacon_hr_open_case(request: Request, body: dict = Body(...)):
    org, bad = _auth(request)
    if bad:
        return bad
    kind = body.get("kind") or "onboard"
    if kind not in ("onboard", "offboard"):
        return _err(422, "invalid", "kind must be onboard or offboard (parish requests come through Staff Benefits).")
    try:
        cid = cases.open_case(org["id"], kind, {k: body[k] for k in cases.EDITABLE if k in body}, None)
        return _ok({"case": _case_out(cases.get_case(cid, [org["id"]]))})
    except HrError as e:
        return _he(e)


@router.patch("/api/hr/cases/{case_id}")
def beacon_hr_update_case(request: Request, case_id: int, body: dict = Body(...)):
    org, bad = _auth(request)
    if bad:
        return bad
    try:
        res = cases.update_case(case_id, [org["id"]], {k: body[k] for k in cases.EDITABLE if k in body}, None)
        return _ok(res)
    except HrError as e:
        return _he(e)


@router.post("/api/hr/cases/{case_id}/items/{item_key}")
def beacon_hr_set_item_status(request: Request, case_id: int, item_key: str, body: dict = Body(...)):
    org, bad = _auth(request)
    if bad:
        return bad
    try:
        return _ok(cases.set_item_status(case_id, [org["id"]], item_key, body.get("status") or "", body.get("note"), None))
    except HrError as e:
        return _he(e)


@router.put("/api/hr/cases/{case_id}/enrollments/{coverage}")
def beacon_hr_record_enrollment(request: Request, case_id: int, coverage: str, body: dict = Body(...)):
    org, bad = _auth(request)
    if bad:
        return bad
    try:
        return _ok(cases.record_enrollment(case_id, [org["id"]], coverage, body, None))
    except HrError as e:
        return _he(e)
    except (TypeError, ValueError):
        return _err(422, "invalid", "dependents_count must be a whole number.")


# There is deliberately NO close-case tool. Closing a case adds a person to, or inactivates them on, the employee roster and
# is a decision a signed-in person makes on the case page (2026-10-22 review: it was not in the plan's tool list).
