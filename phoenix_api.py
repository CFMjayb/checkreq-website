"""
phoenix_api.py -- the calls the DME Phoenix payroll workbook makes to Beacon (26-158 / 26-129, 2026-10-22, Jay's design):

    GET  /api/payroll/phoenix/whoami        who Beacon thinks is signed in, and whether they may use these calls
    PUT  /api/payroll/phoenix/paid-hours    the paid hours (hours only) for one pay date; answers with the comparison summary
    GET  /api/payroll/phoenix/tie-out       the full comparison for the Beacon Tie-Out tab (hours only)
    POST /api/payroll/phoenix/finish-period when everything ties out: mark Final, close, Actualize, optionally open the next period

THE HARD RULE: hours only. Nothing here reads, stores or returns a rate, salary, wage or any amount of money.

AUTH. The person signs in to the workbook with their own Microsoft account (the same sign-in the workbook uses for QuickBooks) and sends the
token as `Authorization: Bearer ...`. No shared key lives in a workbook. entra_token.py checks the token; Beacon's own roles decide what the
person may do: they must be an hr_admin or beacon_admin of the diocese (DME), exactly like the Timekeeping screens. A Microsoft sign-in whose
address differs from the person's Beacon login (Sherri Quint: SherriQ@episcopalmaine.org against sherriq@episcopalmaine.net) is linked by the
setting `phoenix_api_aliases` (checkreq.app_settings, JSON {"microsoft address": "beacon login address"}), set by an administrator.

The machine-key calls in payroll_api.py (X-API-Key) are unchanged. /api/payroll/ is outside the form-token (CSRF) check like them.
"""
from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, Body, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

import db
import entra_token
import org_features
import payroll_closeout as pc
import payroll_totals as pt
import rbac

router = APIRouter()
ORG_CODE = "DME"
ALIAS_SETTING = "phoenix_api_aliases"
_STATUS = {"not_found": 404, "refused": 409, "invalid": 422, "pii": 422}


def register(app) -> None:
    app.include_router(router)


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def _ok(payload) -> JSONResponse:
    return JSONResponse(jsonable_encoder(payload))


def _pe(e: pt.PayrollError) -> JSONResponse:
    return _err(_STATUS.get(e.code, 422), e.code, e.message)


def _aliases() -> dict:
    try:
        import app_settings
        raw = app_settings.get_setting(ALIAS_SETTING, "{}") or "{}"
        d = json.loads(raw)
        return {str(k).strip().lower(): str(v).strip().lower() for k, v in d.items()} if isinstance(d, dict) else {}
    except Exception:
        return {}


def find_user(payload: dict) -> dict | None:
    """The active Beacon user this Microsoft sign-in stands for: by the token's own addresses, then through the alias setting."""
    cands = entra_token.candidate_emails(payload)
    aliases = _aliases()
    for c in list(cands):
        if c in aliases and aliases[c] not in cands:
            cands.append(aliases[c])
    if not cands:
        return None
    return db.query_one("SELECT id, email, display_name FROM checkreq.app_users WHERE lower(email) = ANY(%s) AND is_active "
                        "ORDER BY id LIMIT 1", (cands,))


def _identity(request: Request):
    """(user, org, None) when the caller may use these calls, else (None, None, an error response)."""
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        return None, None, _err(401, "unauthorized", "Sign in to Microsoft in the workbook first (a Bearer token is required).")
    try:
        payload = entra_token.validate_token(h[7:].strip())
    except Exception:
        return None, None, _err(401, "unauthorized", "Your Microsoft sign-in was not accepted. It may have expired: sign in again.")
    user = find_user(payload)
    if not user:
        return None, None, _err(403, "forbidden", "Beacon does not know this Microsoft account "
                                                  f"({entra_token.display_name(payload)}). Ask a Beacon administrator to link it.")
    org = db.query_one("SELECT id, code, name FROM checkreq.organizations WHERE upper(code) = %s", (ORG_CODE,))
    if not org or not org_features.is_enabled(org["id"], "timekeeping"):
        return None, None, _err(403, "forbidden", "Timekeeping is not turned on for this diocese.")
    if not rbac.user_has_any_role(user["id"], ["hr_admin", "beacon_admin"], org_id=org["id"]):
        return None, None, _err(403, "forbidden", "HR Admin access is required for the diocese.")
    return {**user, "microsoft_name": entra_token.display_name(payload)}, org, None


def _date(v, field: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        raise pt.PayrollError("invalid", f"{field} must be a date like 2026-10-29.")


def _base_url(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    scheme = "http" if host.startswith(("localhost", "127.0.0.1")) else "https"
    return f"{scheme}://{host}" if host else ""


def _period(org: dict, pay_date: dt.date) -> dict:
    return pc.period_by_pay_date(org["id"], pay_date)


def _report(request: Request, org: dict, period: dict) -> dict:
    out = pc.tie_out_report(org["id"], period["id"])
    out["closeout_url"] = f"{_base_url(request)}/admin/timekeeping/status/{period['id']}/closeout"
    out["variance_url"] = f"{_base_url(request)}/admin/timekeeping/status/{period['id']}/variance"
    return out


@router.get("/api/payroll/phoenix/whoami")
def phoenix_whoami(request: Request):
    user, org, err = _identity(request)
    if err:
        return err
    return _ok({"user": user["display_name"] or user["email"], "email": user["email"], "diocese": org["name"], "may_use": True})


@router.put("/api/payroll/phoenix/paid-hours")
def phoenix_paid_hours(request: Request, body: dict = Body(...)):
    """The PAID hours of one pay period, hours only (see payroll_closeout.load_register_batch). Answers with what loaded and the
    comparison, so the workbook can show it straight away."""
    user, org, err = _identity(request)
    if err:
        return err
    try:
        pay_date = _date(body.get("pay_date"), "pay_date")
        load = pc.load_register_batch(org["id"], pay_date=pay_date, lines=body.get("lines"),
                                      source_ref=(str(body.get("source_ref") or "").strip() or "Phoenix workbook")[:200],
                                      period_start=_date(body["period_start"], "period_start") if body.get("period_start") else None,
                                      period_end=_date(body["period_end"], "period_end") if body.get("period_end") else None,
                                      actor_user_id=user["id"])
        period = _period(org, pay_date)
        return _ok({"load": load, "tie_out": _report(request, org, period)})
    except pt.PayrollError as e:
        return _pe(e)


@router.get("/api/payroll/phoenix/tie-out")
def phoenix_tie_out(request: Request, pay_date: str = ""):
    user, org, err = _identity(request)
    if err:
        return err
    try:
        return _ok(_report(request, org, _period(org, _date(pay_date, "pay_date"))))
    except pt.PayrollError as e:
        return _pe(e)


@router.post("/api/payroll/phoenix/finish-period")
def phoenix_finish_period(request: Request, body: dict = Body(...)):
    """Mark Final, close, Actualize (and open the next period when asked): ONLY when the hours tie out perfectly. Refused, with the
    reasons, otherwise. The person asking is stamped on every step."""
    user, org, err = _identity(request)
    if err:
        return err
    try:
        period = _period(org, _date(body.get("pay_date"), "pay_date"))
        res = pc.finish_period(org["id"], period["id"], user["id"], open_next=bool(body.get("open_next")))
        return _ok({"result": res, "tie_out": _report(request, org, pt.get_period(org["id"], period["id"]))})
    except pt.PayrollError as e:
        return _pe(e)
