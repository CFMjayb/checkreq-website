"""
payroll_api.py -- the service API the DME payroll inbox routine (26-158) uses to put emailed hours
into Beacon. One endpoint = one named tool (tools/beacon_tools.py in 26-158 calls each by name).
All business logic lives in payroll_totals.py; this file only authenticates, parses, calls one
function and shapes the answer.

AUTH. A machine key in the X-API-Key header, checked against Secret Manager secret
`beacon-payroll-api-keys` (a JSON object {"<diocese code>": "<key>"}, e.g. {"DME": "..."}).
THE DIOCESE COMES FROM THE KEY, never from a parameter: a key issued for DME can only ever touch
DME's parishes, periods, roster and lines. A missing secret, a missing key or a wrong key is a
401 and nothing runs (fails closed). The secret is re-read about every 5 minutes, so adding or
rotating a key never needs a restart (26-107's read_secret_fresh lesson).

Environment override for tests and local runs: BEACON_PAYROLL_API_KEYS (same JSON).

Hours only. Nothing here accepts or returns pay. Errors: 401 no/bad key, 404 not found, 409
refused (closed period, over 300 hours, ...), 422 invalid input or personal data in a quote.
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import time

from fastapi import APIRouter, Body, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

import db
import org_features
import payroll_totals as pt

router = APIRouter()

_KEYS_SECRET = "beacon-payroll-api-keys"  # pragma: allowlist secret (a Secret Manager NAME, not a value)
_KEYS_TTL_SECONDS = 300
_cache: dict = {"at": 0.0, "keys": {}}


def register(app) -> None:
    app.include_router(router)


def _load_keys() -> dict[str, str]:
    env = os.environ.get("BEACON_PAYROLL_API_KEYS", "").strip()
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


def _auth(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """(org, None) when the key is good, (None, 401 response) otherwise."""
    supplied = request.headers.get("x-api-key", "")
    if not supplied:
        return None, _err(401, "unauthorized", "An API key is required.")
    keys = _load_keys()
    org_code = None
    for code, key in keys.items():
        # compare against EVERY key so the time taken does not say which one is closest
        if hmac.compare_digest(supplied.encode(), key.encode()):
            org_code = code
    if not org_code:
        return None, _err(401, "unauthorized", "That API key is not valid.")
    org = db.query_one("SELECT id, code, name FROM checkreq.organizations WHERE upper(code) = %s", (org_code,))
    if not org or not org_features.is_enabled(org["id"], "timekeeping"):
        return None, _err(401, "unauthorized", "That key is not set up for a diocese with timekeeping on.")
    return org, None


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


_STATUS = {"not_found": 404, "refused": 409, "invalid": 422, "pii": 422}


def _ok(payload) -> JSONResponse:
    return JSONResponse(jsonable_encoder(payload))


def _pe(e: pt.PayrollError) -> JSONResponse:
    return _err(_STATUS.get(e.code, 422), e.code, e.message)


def _date(v, field: str) -> dt.date | None:
    if v in (None, ""):
        return None
    try:
        return dt.date.fromisoformat(str(v))
    except ValueError:
        raise pt.PayrollError("invalid", f"{field} must be a date like 2026-10-15.")


def _when(v, field: str) -> dt.datetime | None:
    if v in (None, ""):
        return None
    try:
        d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise pt.PayrollError("invalid", f"{field} must be a date and time like 2026-10-15T09:12:00-04:00.")
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _need(body: dict, *names: str) -> None:
    missing = [n for n in names if body.get(n) in (None, "")]
    if missing:
        raise pt.PayrollError("invalid", "Missing: " + ", ".join(missing))


# ---------------------------------------------------------------- reads

@router.get("/api/payroll/periods")
def beacon_list_payroll_periods(request: Request):
    """Pay periods with start, end, deadline, pay date, status and whether the hours are Final."""
    org, bad = _auth(request)
    if bad:
        return bad
    return _ok({"periods": pt.list_periods(org["id"])})


@router.get("/api/payroll/roster")
def beacon_get_payroll_roster(request: Request, parish_code: str | None = None):
    """Roster by parish code (no pay) and pending new hires."""
    org, bad = _auth(request)
    if bad:
        return bad
    return _ok(pt.get_roster(org["id"], parish_code))


@router.get("/api/payroll/senders")
def beacon_get_expected_senders(request: Request, parish_code: str | None = None):
    """Time submitters per parish (role time_submitter) plus the parish's usual pattern. The payroll
    report recipients come back apart, for fallback matching only."""
    org, bad = _auth(request)
    if bad:
        return bad
    return _ok({"parishes": pt.get_senders(org["id"], parish_code)})


@router.get("/api/payroll/export")
def beacon_get_period_export(request: Request, period_id: int):
    """The period's Excel export (the Checkwriters entry sheet), for saving to the pay-date folder."""
    org, bad = _auth(request)
    if bad:
        return bad
    try:
        import payroll_export
        content, filename = payroll_export.build_for_period(org, period_id)
    except pt.PayrollError as e:
        return _pe(e)
    return Response(content=content,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------- writes

@router.put("/api/payroll/period-totals")
def beacon_record_period_hours(request: Request, body: dict = Body(...)):
    """One line: period, parish code, employee number (or pending change id), category key, hours,
    source, source ref, quote, confidence. Applies the plan's A5 rules."""
    org, bad = _auth(request)
    if bad:
        return bad
    try:
        _need(body, "period_id", "parish_code", "category_key", "hours")
        res = pt.record_period_hours(
            org["id"], period_id=int(body["period_id"]), parish_code=str(body["parish_code"]),
            category_key=str(body["category_key"]), hours=body["hours"],
            source=str(body.get("source") or "email"), source_ref=body.get("source_ref"),
            quote=body.get("quote"), confidence=body.get("confidence"),
            employee_number=(str(body["employee_number"]) if body.get("employee_number") not in (None, "") else None),
            pending_change_id=(int(body["pending_change_id"]) if body.get("pending_change_id") not in (None, "") else None))
        return _ok(res)
    except pt.PayrollError as e:
        return _pe(e)
    except (TypeError, ValueError):
        return _err(422, "invalid", "period_id and pending_change_id must be whole numbers.")


@router.put("/api/payroll/register-hours")
def beacon_record_register_hours(request: Request, body: dict = Body(...)):
    """One paid-register line (hours only) for the variance report. Closed periods are fine."""
    org, bad = _auth(request)
    if bad:
        return bad
    try:
        _need(body, "period_id", "parish_code", "employee_number", "category_key", "hours")
        return _ok(pt.record_register_hours(
            org["id"], period_id=int(body["period_id"]), parish_code=str(body["parish_code"]),
            employee_number=str(body["employee_number"]), category_key=str(body["category_key"]),
            hours=body["hours"], source_ref=body.get("source_ref")))
    except pt.PayrollError as e:
        return _pe(e)
    except (TypeError, ValueError):
        return _err(422, "invalid", "period_id must be a whole number.")


@router.post("/api/payroll/submissions/received")
def beacon_mark_submission_received(request: Request, body: dict = Body(...)):
    """Notes that an email arrived for a parish and period (idempotent on the message id)."""
    org, bad = _auth(request)
    if bad:
        return bad
    try:
        _need(body, "period_id", "parish_code", "source_ref")
        return _ok(pt.mark_submission_received(
            org["id"], period_id=int(body["period_id"]), parish_code=str(body["parish_code"]),
            source_ref=str(body["source_ref"]), received_at=_when(body.get("received_at"), "received_at"),
            channel=str(body.get("channel") or "email")))
    except pt.PayrollError as e:
        return _pe(e)
    except (TypeError, ValueError):
        return _err(422, "invalid", "period_id must be a whole number.")


@router.post("/api/payroll/roster-changes")
def beacon_propose_roster_change(request: Request, body: dict = Body(...)):
    """One PENDING roster change (add, edit, deactivate, reactivate), raised from an email. Same
    queue the parishes use. Idempotent on source_ref."""
    org, bad = _auth(request)
    if bad:
        return bad
    try:
        _need(body, "parish_code", "change_type", "source_ref")
        ch = body.get("captures_hours")
        return _ok(pt.propose_roster_change(
            org["id"], parish_code=str(body["parish_code"]), change_type=str(body["change_type"]),
            source_ref=str(body["source_ref"]), quote=body.get("quote"),
            employee_number=body.get("employee_number"), first_name=body.get("first_name"),
            last_name=body.get("last_name"), position=body.get("position"),
            period_id=(int(body["period_id"]) if body.get("period_id") not in (None, "") else None),
            captures_hours=(None if ch is None else bool(ch)), as_of=_date(body.get("as_of"), "as_of")))
    except pt.PayrollError as e:
        return _pe(e)
    except (TypeError, ValueError):
        return _err(422, "invalid", "period_id must be a whole number.")


@router.post("/api/payroll/senders")
def beacon_add_learned_sender(request: Request, body: dict = Body(...)):
    """Adds or refreshes one time submitter on a parish."""
    org, bad = _auth(request)
    if bad:
        return bad
    try:
        _need(body, "parish_code", "email")
        return _ok(pt.add_learned_sender(
            org["id"], parish_code=str(body["parish_code"]), email=str(body["email"]), name=body.get("name"),
            source=str(body.get("source") or "payroll inbox (learned)")))
    except pt.PayrollError as e:
        return _pe(e)
