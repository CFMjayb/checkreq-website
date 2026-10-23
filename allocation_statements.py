"""
allocation_statements.py -- 26-129 / Beacon "Allocation Statements" board (Diocese Mode, Administrative Tasks).
Jay, 2026-10-23: "a screen in the diocesan mode ... it's going to show every church and a status board of what their
allocation is, the drop-down box will be by year, and then it'll show what the current balance is ... buttons at the top like
generate all statements ... check individual churches and say generate these statements ... a button that says email all
of these statements and they'll be the ones that are checked. And the emails you'll use will be the emails that you already
pull when you actually generate the statement."

    GET  /admin/allocation-statements?year=YYYY                  the board (every active church of the CURRENT diocese)
    GET  /admin/allocation-statements/row/{parish_id}?year=      one church's figures from QuickBooks (the board fills rows in as they arrive)
    POST /admin/allocation-statements/build/{parish_id}?year=&action=generate|email
                                                                 build that church's statement PDF, keep a copy, and (email) send it
    GET  /admin/allocation-statements/pdf/{parish_id}?year=      the kept copy of one church's statement
    GET  /admin/allocation-statements/download.zip?year=&ids=1,2 the kept copies of the listed churches, in one zip

HOW IT WORKS.  Every figure comes live from QuickBooks through parish_finance.py (the same code the parish's own Finance page and
statement use), so the board can never disagree with a parish's own statement.  QuickBooks is asked ONE CHURCH AT A TIME (two quick
calls each): the page lists the churches at once and the browser fills each row in as its answer arrives, so a diocese of 100 churches
shows a usable board right away instead of one long wait.  Generating or emailing is also one church per request (a statement is a
real browser-rendered PDF, a few seconds each, one at a time on the server), with progress shown on the screen.

EMAIL.  Goes to the addresses on the church's QuickBooks Customer record -- the same field the statement's own mailing block prints
(parish_finance.customer_email_addresses).  It is sent through the 26-122 email server with the PDF attached.  EDOM mail goes out as
businessoffice@episcopalmaryland.org.  The email server cannot send as an episcopalmaine.org address yet, so Maine statements go from the
default notifications address with the diocese's accounting address as the reply-to (parish_finance._STATEMENT_BRANDING).  A DEVELOPMENT
Beacon never reaches a church: email_client redirects every send to the test address (M17), and the board says so on screen.
Nothing is ever emailed except the churches someone ticked and confirmed.

THE LOG.  portal.sma_statement_log (migration 099) records each generate / email / failed email, so the board shows "Generated" and
"Emailed" per church and asks before anyone is emailed twice.  If the table is not there yet the board still works for generating,
but Email is switched off (no record, no way to warn about a repeat) and the screen says why.

ACCESS: Setup Admin or Beacon Admin AT THE CURRENT ENTITY (the staff who may see any church's finances).  Entity-scoped: never
org_id=None, and every church id is looked up inside the current diocese (registry.get_parish), so another diocese's church is a 404.

New file per the standing main.py rule; main.py gains an import and one register() call only.
"""
from __future__ import annotations

import base64
import datetime
import html
import io
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from psycopg import errors as pgerr

import db
import email_client
import gcs_client
import org_time
import parish_finance
import qbo_mcp_client
import rbac
import registry
import statement_store

router = APIRouter()

_current_user = None
_current_org = None
_render = None

ROLES = ["setup_admin", "beacon_admin"]
YEARS_BACK = 6                      # the year list runs from this year back this many years
LOG_TABLE = "portal.sma_statement_log"
_YEAR_RE = re.compile(r"^20\d\d$")


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


# --- access ---------------------------------------------------------------------------------------

def _guard(request: Request, *, as_json: bool = False):
    """-> (user, org, None) when allowed, else (None, None, a response)."""
    def deny(message: str, status: int):
        if as_json:
            return JSONResponse({"ok": False, "error": message}, status_code=status)
        return HTMLResponse(message, status_code=status)

    user = _current_user(request)
    if not user:
        return None, None, (JSONResponse({"ok": False, "error": "Sign in again."}, status_code=401) if as_json
                            else RedirectResponse("/login"))
    org = _current_org(request)
    if not org:
        return None, None, deny("Select an entity first.", 403)
    if not rbac.user_has_any_role(user["id"], list(ROLES), org_id=org["id"]):
        return None, None, deny("Setup Admin or Beacon Admin access at this entity is required.", 403)
    return user, org, None


def _years() -> list[str]:
    this_year = datetime.date.today().year
    return [str(y) for y in range(this_year, this_year - YEARS_BACK - 1, -1)]


def _year(value: str | None) -> str:
    value = (value or "").strip()
    return value if _YEAR_RE.match(value) else str(datetime.date.today().year)


def _is_dev() -> bool:
    return email_client._BEACON_ENV != "prod"


# --- the log ----------------------------------------------------------------------------------------

def _log_ready() -> bool:
    try:
        db.query(f"SELECT 1 FROM {LOG_TABLE} LIMIT 1")
        return True
    except (pgerr.UndefinedTable, pgerr.InsufficientPrivilege):
        return False


def _log_summary(org_id: int, year: str) -> tuple[bool, dict[int, dict]]:
    """(log is there, {parish_id: {'generated': row, 'emailed': row, 'email_failed': row}}) -- the newest row of each kind."""
    try:
        rows = db.query(
            f"SELECT DISTINCT ON (parish_id, action) parish_id, action, amount_due, recipients, error, created_at "
            f"FROM {LOG_TABLE} WHERE org_id = %s AND year = %s ORDER BY parish_id, action, created_at DESC",
            (org_id, year))
    except (pgerr.UndefinedTable, pgerr.InsufficientPrivilege):
        return False, {}
    out: dict[int, dict] = {}
    for r in rows:
        out.setdefault(r["parish_id"], {})[r["action"]] = r
    return True, out


def _write_log(org_id: int, parish_id: int, year: str, action: str, amount_due, recipients: str | None,
               error: str | None, user_id: int) -> None:
    try:
        db.execute(
            f"INSERT INTO {LOG_TABLE} (org_id, parish_id, year, action, amount_due, recipients, error, by_user_id) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (org_id, parish_id, year, action, amount_due,
             (recipients or None) and recipients[:600], (error or None) and error[:600], user_id))
    except (pgerr.UndefinedTable, pgerr.InsufficientPrivilege):
        pass        # the board already says the log is not set up


def _when(org: dict, dt) -> str:
    return org_time.format_local(dt, org.get("time_zone")) if dt else ""


# --- the kept copy of each statement ------------------------------------------------------------------

def _stored_path(org_id: int, year: str, parish_id: int) -> str:
    return f"sma-statements/{int(org_id)}/{year}/{int(parish_id)}.pdf"


def _store(org_id: int, year: str, parish_id: int, pdf: bytes) -> str | None:
    """Keep the PDF (private bucket, overwritten by each new build).  -> None on success, else why not."""
    try:
        gcs_client.upload_bytes(statement_store.BUCKET, _stored_path(org_id, year, parish_id), pdf, "application/pdf")
        return None
    except Exception as exc:                       # noqa: BLE001 -- storage trouble must not hide as a crash
        print(f"[allocation_statements] could not keep statement {parish_id}/{year}: {type(exc).__name__}")
        return "the statement could not be saved for download"


def _stored_pdf(org_id: int, year: str, parish_id: int) -> bytes | None:
    try:
        got = gcs_client.download_bytes(statement_store.BUCKET, _stored_path(org_id, year, parish_id))
    except Exception as exc:                       # noqa: BLE001
        print(f"[allocation_statements] could not read statement {parish_id}/{year}: {type(exc).__name__}")
        return None
    return got[0] if got else None


# --- the board ------------------------------------------------------------------------------------------

@router.get("/admin/allocation-statements", response_class=HTMLResponse)
def board_page(request: Request):
    user, org, denied = _guard(request)
    if denied:
        return denied
    year = _year(request.query_params.get("year"))
    parishes = registry.list_parishes(org["id"])
    log_ok, log = _log_summary(org["id"], year)
    branding = parish_finance._statement_branding(org)
    rows = []
    for p in parishes:
        entry = log.get(p["id"], {})
        gen, sent, failed = entry.get("generated"), entry.get("emailed"), entry.get("email_failed")
        rows.append({
            "parish": p,
            "linked": bool(p.get("qbo_ar_customer_id")),
            "generated_at": _when(org, gen["created_at"]) if gen else "",
            "emailed_at": _when(org, sent["created_at"]) if sent else "",
            "emailed_to": (sent or {}).get("recipients") or "",
            # a failed attempt newer than the last good send is worth showing in red
            "failed": bool(failed and (not sent or failed["created_at"] > sent["created_at"])),
            "failed_why": (failed or {}).get("error") or "",
        })
    return _render(request, "admin_allocation_statements.html", user, {
        "rows": rows, "year": year, "years": _years(), "log_ok": log_ok,
        "line_description": branding.get("line_description") or parish_finance._DEFAULT_STATEMENT_LINE_DESCRIPTION,
        "reply_to": branding.get("business_office_email") or "",
        "dev_mode": _is_dev(),
        "test_address": (None if not _is_dev() else _test_address()),
    })


def _test_address() -> str | None:
    import app_settings
    return app_settings.get_setting("email_test_mode_address")


@router.get("/admin/allocation-statements/row/{parish_id}")
def row_figures(request: Request, parish_id: int):
    """One church's figures for the year, live from QuickBooks.  Never an error page: a problem comes back as text for the row."""
    user, org, denied = _guard(request, as_json=True)
    if denied:
        return denied
    year = _year(request.query_params.get("year"))
    parish = registry.get_parish(parish_id, org["id"])
    if not parish:
        return JSONResponse({"ok": False, "error": "Church not found."}, status_code=404)
    customer_id = parish.get("qbo_ar_customer_id")
    if not customer_id:
        return {"ok": True, "linked": False, "has_statement": False}
    company = parish_finance._company_code(org)
    # the invoices and the customer record are two separate QuickBooks calls: ask for both at once (halves the wait per church)
    with ThreadPoolExecutor(max_workers=2) as pool:
        years_future = pool.submit(parish_finance.get_sma_years, company, customer_id)
        customer_future = pool.submit(qbo_mcp_client.get_customer_raw, company, customer_id)
        years, error = years_future.result()
        customer, _customer_error = customer_future.result()
    if error:
        return {"ok": True, "linked": True, "has_statement": False, "error": f"QuickBooks did not answer: {error}"}
    row = next((y for y in years if y["year"] == year), None)
    if not row:
        return {"ok": True, "linked": True, "has_statement": False}
    return {"ok": True, "linked": True, "has_statement": True,
            "original": round(row["original_amount"], 2), "paid": round(row["paid_to_date"], 2),
            "balance": round(row["current_balance"], 2),
            "emails": parish_finance.customer_email_addresses(customer),
            "email_unknown": bool(_customer_error)}


# --- generate / email one church ---------------------------------------------------------------------------

def _email_for(org: dict, parish: dict, year: str, built: dict, branding: dict) -> dict:
    """The email for one statement.  Plain wording, no square brackets in the subject (Bracket drops them)."""
    what = branding.get("line_description") or parish_finance._DEFAULT_STATEMENT_LINE_DESCRIPTION
    due = built["year_row"]["current_balance"]
    office = branding.get("business_office_email") or ""
    name = parish["name"]
    lines = [
        "Hello,",
        f"Attached is the {year} {what} statement for {name}.",
        f"Balance due: ${due:,.2f}.",
        "If anything on the statement looks wrong, or you need to change the email address we use for statements, "
        + (f"please reply to this message or write to {office}." if office else "please reply to this message."),
        f"{org.get('name') or 'The diocese'} Business Office",
    ]
    return {
        "subject": f"{year} {what} statement - {name}",
        "body_text": "\n\n".join(lines),
        "body_html": "".join(f"<p>{html.escape(line)}</p>" for line in lines),
    }


@router.post("/admin/allocation-statements/build/{parish_id}")
def build_one(request: Request, parish_id: int):
    user, org, denied = _guard(request, as_json=True)
    if denied:
        return denied
    year = _year(request.query_params.get("year"))
    action = (request.query_params.get("action") or "").strip()
    if action not in ("generate", "email"):
        return JSONResponse({"ok": False, "error": "Unknown action."}, status_code=400)
    parish = registry.get_parish(parish_id, org["id"])
    if not parish:
        return JSONResponse({"ok": False, "error": "Church not found."}, status_code=404)
    if action == "email" and not _log_ready():
        return JSONResponse({"ok": False, "error": "Email is switched off until the statement log (migration 099) is set up."},
                            status_code=409)

    built, error = parish_finance.build_sma_statement(parish, org, year)
    if error:
        return {"ok": False, "error": error}
    due = built["year_row"]["current_balance"]
    saved_error = _store(org["id"], year, parish_id, built["pdf"])
    if action == "generate" and saved_error:
        return {"ok": False, "error": saved_error[0].upper() + saved_error[1:] + "."}
    _write_log(org["id"], parish_id, year, "generated", due, None, None, user["id"])
    now = _when(org, datetime.datetime.now(datetime.timezone.utc))
    result = {"ok": True, "balance": round(due, 2), "generated_at": now, "filename": built["filename"]}
    if action == "generate":
        result["message"] = "Statement generated."
        return result

    # --- email ---
    emails = built["emails"]
    if not emails:
        why = "There is no email address on this church's QuickBooks customer record."
        _write_log(org["id"], parish_id, year, "email_failed", due, None, why, user["id"])
        return {**result, "ok": False, "error": why, "failed": True}
    branding = parish_finance._statement_branding(org)
    content = _email_for(org, parish, year, built, branding)
    to = ", ".join(emails)
    sent = email_client.send_email(
        to=to, subject=content["subject"], body_html=content["body_html"], body_text=content["body_text"],
        sender=branding.get("email_sender") or "", reply_to=branding.get("business_office_email") or "",
        attachments=[{"name": built["filename"], "content_type": "application/pdf",
                      "content_base64": base64.b64encode(built["pdf"]).decode("ascii")}],
        timeout=60)
    if sent.get("status") != "sent":
        why = f"The email was not sent: {sent.get('error') or 'no answer from the email server'}"
        _write_log(org["id"], parish_id, year, "email_failed", due, to, why, user["id"])
        return {**result, "ok": False, "error": why, "failed": True}
    _write_log(org["id"], parish_id, year, "emailed", due, to, None, user["id"])
    result.update({"emailed_at": now, "emailed_to": to, "message": f"Emailed to {to}."})
    if saved_error:
        result["message"] += f" ({saved_error}.)"
    if _is_dev():
        result["message"] += " (Development site: the email went to the test address, not to the church.)"
    return result


# --- the kept copies ----------------------------------------------------------------------------------------------

@router.get("/admin/allocation-statements/pdf/{parish_id}")
def stored_pdf(request: Request, parish_id: int):
    user, org, denied = _guard(request)
    if denied:
        return denied
    year = _year(request.query_params.get("year"))
    parish = registry.get_parish(parish_id, org["id"])
    data = _stored_pdf(org["id"], year, parish_id) if parish else None
    if not data:
        return HTMLResponse("No saved statement for this church and year. Generate it first.", status_code=404)
    name = parish_finance._statement_filename(parish, year, org)
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="{name}"',
        "Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff"})


@router.get("/admin/allocation-statements/download.zip")
def stored_zip(request: Request):
    user, org, denied = _guard(request)
    if denied:
        return denied
    year = _year(request.query_params.get("year"))
    ids: list[int] = []
    for part in (request.query_params.get("ids") or "").split(","):
        if part.strip().isdigit() and int(part) not in ids:
            ids.append(int(part))
    if not ids:
        return HTMLResponse("No churches were listed.", status_code=400)
    buffer = io.BytesIO()
    names_used: set[str] = set()
    count = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for pid in ids[:300]:
            parish = registry.get_parish(pid, org["id"])
            data = _stored_pdf(org["id"], year, pid) if parish else None
            if not data:
                continue
            name = parish_finance._statement_filename(parish, year, org)
            if name in names_used:            # two churches with the same name and city: keep both files
                name = name[:-4] + f" ({parish.get('code') or pid}).pdf"
            names_used.add(name)
            bundle.writestr(name, data)
            count += 1
    if not count:
        return HTMLResponse("None of those churches has a saved statement yet.", status_code=404)
    return Response(buffer.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{(org.get("code") or "Diocese")} Allocation Statements {year}.zip"',
        "Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff"})
