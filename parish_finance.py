"""
parish_finance.py — Parish Portal Plan.md, S6/S7 addendum (2026-08-16),
Jay's direct spec: Shared Ministry Allocation (SMA) status per year
(original amount, payments posted, current balance), a printable
statement, "ask a question to the Business Office," SMA direct-debit
enrollment (a REQUEST only — Beacon never collects bank details), and
Middendorf loan progress (current balance only, for parishes that have
one). On the AP side: payments processed (real QBO BillPayment history).

New file per NFR-11 / the standing main.py rule. All data is queried live
from QBO on every view — no local cache table, matching this codebase's own
"don't build a second copy of a fact another system already owns" lesson
(the Firestore/Postgres saga, the global_approvers.org_id incident).

Gate: mirrors timekeeping.py's own can_manage_timekeeping() three-way
pattern — beacon_admin (anywhere), setup_admin at this parish's own
diocese, or a genuine parish_finance grant at this specific parish. Staff
can always view a parish's finances on its behalf; a parish user needs the
Parish Finance role specifically (Parish Portal Plan.md §1 decision log).

SMA invoice identification (resolved with Jay 2026-08-16, corrected
2026-08-17 against real QBO data): the pattern Jay first gave
(^\\d{4}SMA-\\d{6}) does not exist anywhere in production — pulling every
real EDOM parish's invoices found the real, high-volume annual Allocation
invoice is DocNumber matching ^\\d{4}A- (e.g. "2026A-ASTFRE", one per
parish per year, ~130 real examples). A parish's AR Customer record is NOT
SMA-only, so every invoice pulled from QBO is filtered down to this
pattern before being grouped by its own leading 4-digit year into the
per-year rollup — the year comes from the DocNumber itself, not TxnDate,
since that's the field the diocese's own numbering scheme authoritatively
encodes the assessment year into. No parish-code cross-check is needed:
invoices are already scoped to this one parish via its own AR Customer id.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse

import db
import rbac
import parish_mode
import parish_roles
import parish_requests
import qbo_mcp_client
import email_client
import registry

router = APIRouter()

_current_user = None
_render = None

_SMA_DOCNUMBER_RE = re.compile(r"^(\d{4})A-")
BUSINESS_OFFICE_EMAIL = "businessoffice@episcopalmaryland.org"


def register(app, *, current_user, render) -> None:
    global _current_user, _render
    _current_user, _render = current_user, render
    app.include_router(router)


# ── Context + authorization ─────────────────────────────────────────────────

def _parish_context(request: Request):
    """(user, parish, diocese_org, err). Mirrors timekeeping.py's own
    parish_context() — resolves the current parish regardless of how the
    viewer arrived (a CFO's Parish Mode preview, or a native parish login).
    err is None on success."""
    user = _current_user(request)
    if not user:
        return None, None, None, RedirectResponse("/login")
    parish, _is_preview = parish_mode.effective_parish_mode(request, user)
    if not parish:
        return None, None, None, RedirectResponse("/parish-view")
    diocese_org = db.query_one("SELECT * FROM checkreq.organizations WHERE id = %s", (parish["org_id"],))
    return user, parish, diocese_org, None


def can_view_finance(user: dict, parish: dict) -> bool:
    """Same three-way "staff acting as this entity's own back office"
    pattern timekeeping.py's can_manage_timekeeping() already uses."""
    if rbac.user_has_role(user["id"], "beacon_admin", org_id=None):
        return True
    if rbac.user_has_role(user["id"], "setup_admin", parish["org_id"]):
        return True
    return parish_roles.user_has_parish_role(user["id"], "parish_finance", parish["id"])


def _company_code(org: dict) -> str:
    return (org.get("code") or "").lower()


# ── SMA data ─────────────────────────────────────────────────────────────────

def get_sma_years(company: str, qbo_customer_id: str) -> tuple[list[dict], str | None]:
    """Every SMA (Allocation) invoice for this parish's AR Customer, grouped
    by the 4-digit year encoded in its own DocNumber (^\\d{4}A-, e.g.
    "2026A-ASTFRE") — not every invoice under the customer, since that
    record isn't SMA-only.
    Returns ([{year, original_amount, paid_to_date, current_balance,
    invoice_txn_ids}], error) sorted by year descending — error is None on
    success (an empty list is a valid, non-error result: this parish simply
    has no SMA invoices yet, or none matching the pattern). invoice_txn_ids
    (2026-08-27) is the list of real QBO Invoice internal ids behind this
    year's total — normally just one, kept as a list since nothing here
    guarantees exactly one SMA invoice per year — needed by
    get_sma_year_payments()/the statement PDF to pull real payment history
    for this specific year, since Payment has no direct link to "a year,"
    only to the specific Invoice(s) it was applied against."""
    data, error = qbo_mcp_client.get_parish_invoices(company, qbo_customer_id)
    if error:
        return [], error
    years: dict[str, dict] = {}
    for inv in (data or {}).get("invoices", []):
        m = _SMA_DOCNUMBER_RE.match(inv.get("doc_number") or "")
        if not m:
            continue
        year = m.group(1)
        row = years.setdefault(year, {"year": year, "original_amount": 0.0,
                                       "paid_to_date": 0.0, "current_balance": 0.0,
                                       "invoice_txn_ids": []})
        total = float(inv.get("total_amt") or 0)
        balance = float(inv.get("balance") or 0)
        row["original_amount"] += total
        row["current_balance"] += balance
        row["paid_to_date"] += (total - balance)
        if inv.get("txn_id"):
            row["invoice_txn_ids"].append(inv["txn_id"])
    return sorted(years.values(), key=lambda r: r["year"], reverse=True), None


def get_sma_year_payments(
    company: str, qbo_customer_id: str, invoice_txn_ids: list[str],
) -> tuple[list[dict], str | None]:
    """Every real QBO Payment applied against a given SMA year's invoice(s)
    (normally just one), merged and sorted ascending by date — the data
    behind both the "click on Payments Posted, see the list" drill-down
    (sma_payments_page) and the printable statement's payment-history
    table (render_sma_statement_pdf). error is None on success — an empty
    list is a valid, non-error result (no payments posted yet this year)."""
    all_payments: list[dict] = []
    for txn_id in invoice_txn_ids:
        data, error = qbo_mcp_client.get_invoice_payments(company, qbo_customer_id, txn_id)
        if error:
            return [], error
        all_payments.extend((data or {}).get("payments", []))
    all_payments.sort(key=lambda p: p.get("txn_date") or "")
    return all_payments, None


def _display_date(iso_date: str) -> str:
    """QBO's TxnDate ('YYYY-MM-DD') reformatted to 'MM/DD/YYYY' for display —
    matches the Diocese's own Excel statement's date formatting. Falls back
    to the raw string on anything unexpected rather than raising, since this
    is presentation-only."""
    import datetime
    try:
        return datetime.datetime.strptime(iso_date or "", "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return iso_date or ""


def _payments_with_running_balance(original_amount: float, payments: list[dict]) -> list[dict]:
    """Walks a year's payments (already ascending by date) subtracting each
    from the invoice's original amount, so every row can show the same
    "Total Remaining" running balance the Diocese's own hand-built Excel
    statement shows — matches that reference format exactly (Congregational
    Allocations\\{year}\\Statements\\{parish}.xlsx, the "{year}" tab). Also
    stamps a human-formatted display_date, used by both the in-app
    drill-down (sma_payments.html) and the printable statement."""
    remaining = original_amount
    rows = []
    for p in payments:
        remaining -= float(p.get("amount_applied") or 0)
        rows.append({**p, "remaining": remaining, "display_date": _display_date(p.get("txn_date"))})
    return rows


# ── Routes: the finance page ─────────────────────────────────────────────────

@router.get("/parish-finance", response_class=HTMLResponse)
def parish_finance_page(request: Request):
    user, parish, diocese_org, err = _parish_context(request)
    if err:
        return err
    if not can_view_finance(user, parish):
        return _render(request, "parish_finance.html", user, {
            "parish": parish, "can_view": False,
        })

    company = _company_code(diocese_org)
    sma_years, sma_error = ([], None)
    if parish.get("qbo_ar_customer_id"):
        sma_years, sma_error = get_sma_years(company, parish["qbo_ar_customer_id"])

    loan = None
    loan_error = None
    if parish.get("middendorf_gl_account"):
        data, loan_error = qbo_mcp_client.get_account_balance(company, parish["middendorf_gl_account"])
        if data and data.get("found"):
            loan = data["account"]

    payments = []
    payments_error = None
    if parish.get("qbo_ap_vendor_id"):
        data, payments_error = qbo_mcp_client.get_parish_bill_payments(company, parish["qbo_ap_vendor_id"])
        if data:
            payments = data.get("payments", [])

    return _render(request, "parish_finance.html", user, {
        "parish": parish, "can_view": True,
        "sma_years": sma_years, "sma_error": sma_error,
        "has_ar": bool(parish.get("qbo_ar_customer_id")),
        "loan": loan, "loan_error": loan_error,
        "has_loan_mapping": bool(parish.get("middendorf_gl_account")),
        "payments": payments, "payments_error": payments_error,
        "has_ap": bool(parish.get("qbo_ap_vendor_id")),
    })


@router.get("/parish-finance/sma-payments/{year}", response_class=HTMLResponse)
def sma_payments_page(year: str, request: Request):
    """The "click on Payments Posted, see the list" drill-down (Jay's
    direct request, 2026-08-27): every real QBO Payment applied against
    this one allocation year's SMA invoice(s), with a running "remaining
    balance" column matching the Diocese's own hand-built Excel statement's
    own Payments section."""
    user, parish, diocese_org, err = _parish_context(request)
    if err:
        return err
    if not can_view_finance(user, parish):
        return _render(request, "sma_payments.html", user, {
            "parish": parish, "year": year, "can_view": False,
        })

    company = _company_code(diocese_org)
    sma_years, sma_error = ([], None)
    if parish.get("qbo_ar_customer_id"):
        sma_years, sma_error = get_sma_years(company, parish["qbo_ar_customer_id"])
    year_row = next((y for y in sma_years if y["year"] == year), None)

    if sma_error or not year_row:
        return _render(request, "sma_payments.html", user, {
            "parish": parish, "year": year, "can_view": True,
            "year_row": None, "sma_error": sma_error,
        })

    payments, pay_error = get_sma_year_payments(
        company, parish["qbo_ar_customer_id"], year_row["invoice_txn_ids"])
    rows = _payments_with_running_balance(year_row["original_amount"], payments) if not pay_error else []

    return _render(request, "sma_payments.html", user, {
        "parish": parish, "year": year, "can_view": True,
        "year_row": year_row, "sma_error": None,
        "payments": rows, "pay_error": pay_error,
    })


@router.get("/parish-finance/sma-statement.pdf")
def sma_statement_pdf(request: Request):
    user, parish, diocese_org, err = _parish_context(request)
    if err:
        return err
    if not can_view_finance(user, parish):
        return JSONResponse({"error": "You don't have permission to view this parish's finances."}, status_code=403)

    company = _company_code(diocese_org)
    sma_years = []
    if parish.get("qbo_ar_customer_id"):
        sma_years, _ = get_sma_years(company, parish["qbo_ar_customer_id"])

    if not sma_years:
        return JSONResponse(
            {"error": "No Shared Ministry Allocation invoices found for this parish."}, status_code=404)

    year = request.query_params.get("year", "").strip()
    year_row = next((y for y in sma_years if y["year"] == year), None) if year else sma_years[0]
    if not year_row:
        return JSONResponse({"error": f"No SMA invoice found for {year}."}, status_code=404)

    payments, _pay_error = get_sma_year_payments(
        company, parish["qbo_ar_customer_id"], year_row["invoice_txn_ids"])
    payment_rows = _payments_with_running_balance(year_row["original_amount"], payments)

    customer = None
    if parish.get("qbo_ar_customer_id"):
        customer, _cust_error = qbo_mcp_client.get_customer_raw(company, parish["qbo_ar_customer_id"])

    pdf_bytes = render_sma_statement_pdf(parish, diocese_org, year_row, payment_rows, customer)
    filename = f"{parish['name']} SMA Statement {year_row['year']}.pdf".replace("/", "-")
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _image_data_uri(filename: str) -> str:
    """Base64 data: URI for a static image under Website/static/img/ —
    required because main.py's _html_to_pdf_bytes() calls
    page.set_content() with no base_url, so a relative/absolute file path
    in an <img src=...> would never resolve inside headless Chromium.
    Read fresh on every render, matching this module's existing pattern
    for tokens.css (this is a low-traffic internal tool — one PDF per
    statement print, never a hot path — so no caching is needed)."""
    import base64
    import os
    path = os.path.join(os.path.dirname(__file__), "static", "img", filename)
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def render_sma_statement_pdf(
    parish: dict, diocese_org: dict, year_row: dict,
    payment_rows: list[dict], customer: dict | None,
) -> bytes:
    """Renders sma_statement.html + inlined tokens.css and hands it to
    main.py's real-headless-Chromium PDF renderer — same two-step pattern
    render_check_voucher_pdf() already uses (fragment render, then wrap
    with tokens.css + a Google Fonts <link> so a real browser resolves
    var(--token) natively). Local imports from main.py to avoid a circular
    import at module-load time (main.py imports this module) — resolved
    lazily, by which point main.py is fully loaded.

    Rebuilt 2026-08-27 to match the Diocese's own hand-built Excel
    statement format exactly (Congregational Allocations\\{year}\\
    Statements\\{parish}.xlsx, the "{year}" tab, per Jay's direct
    request) — a real letterhead header/footer, a customer mailing block
    (sourced live from the parish's own QBO Customer record — see
    qbo_mcp_client.get_customer_raw()'s docstring for why this is read
    live rather than duplicated into Postgres), the allocation line item,
    every real payment for this one year with a running "Total Remaining"
    balance, and Total Paid / Total Due — one statement per allocation
    year, not the old all-years summary table."""
    import datetime
    import os
    from main import templates as _templates, _html_to_pdf_bytes as _to_pdf

    bill_addr = (customer or {}).get("BillAddr") or {}
    address_line1 = bill_addr.get("Line1") or ""
    city = bill_addr.get("City") or ""
    state = bill_addr.get("CountrySubDivisionCode") or ""
    zip_code = bill_addr.get("PostalCode") or ""
    city_state_zip = ", ".join(p for p in [city, state] if p)
    if zip_code:
        city_state_zip = f"{city_state_zip} {zip_code}".strip()
    customer_email = ((customer or {}).get("PrimaryEmailAddr") or {}).get("Address") or ""

    monthly_amount = (year_row["original_amount"] / 12) if year_row["original_amount"] else 0.0

    # payment_rows already carries display_date + remaining, stamped by
    # _payments_with_running_balance() at the call site.
    fragment = _templates.env.get_template("sma_statement.html").render(
        parish=parish, diocese_org=diocese_org, year=year_row["year"], year_row=year_row,
        payments=payment_rows, monthly_amount=monthly_amount,
        total_paid=year_row["paid_to_date"], total_due=year_row["current_balance"],
        statement_date=datetime.date.today().strftime("%m/%d/%Y"),
        business_office_email=BUSINESS_OFFICE_EMAIL,
        customer_address_line1=address_line1, customer_city_state_zip=city_state_zip,
        customer_email=customer_email,
        header_data_uri=_image_data_uri("edom_statement_header.png"),
        footer_data_uri=_image_data_uri("edom_statement_footer.png"),
    )
    static_css_dir = os.path.join(os.path.dirname(__file__), "static", "css")
    with open(os.path.join(static_css_dir, "tokens.css"), encoding="utf-8") as f:
        tokens_css = f.read()

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
{tokens_css}
body {{ font-family: 'Inter', sans-serif; margin: 0; padding: 0; background: #fff; color: #1a1a1a; }}
</style>
</head><body>{fragment}</body></html>"""

    return _to_pdf(html)


# ── Routes: Ask the Business Office / SMA direct-debit enrollment ──────────

def _submit_and_notify(parish: dict, user: dict, subject: str, message: str) -> None:
    """Both new request kinds get the same treatment (resolved with Jay
    2026-08-16): logged to the existing portal.parish_requests review queue
    AND emailed to the Business Office — calling parish_requests.create_request()
    directly (not its HTTP route) so this module can also fire the email as
    part of the same action. Email failure never blocks the logged request
    from existing — same fail-soft philosophy as the W-9 request email
    (email_client.send_email() itself never raises).

    Always logged as kind='general_request' — migration 031's own CHECK
    constraint only allows 'feedback'/'general_request', and a schema
    widening isn't something to do without asking first (standing rule).
    Both an Ask-the-Business-Office question and an SMA direct-debit
    enrollment request are genuinely general requests to the diocese; the
    distinct `subject` line (below) is what tells them apart in the
    existing /admin/parish-requests review queue."""
    parish_requests.create_request(parish["id"], user["id"], "general_request", subject, message)
    body_text = (
        f"Parish: {parish['name']}\n"
        f"Submitted by: {user.get('display_name') or user.get('email')}\n\n"
        f"{message}"
    )
    email_client.send_email(
        to=BUSINESS_OFFICE_EMAIL,
        subject=subject,
        body_text=body_text,
        body_html=f"<p><strong>Parish:</strong> {parish['name']}</p>"
                   f"<p><strong>Submitted by:</strong> {user.get('display_name') or user.get('email')}</p>"
                   f"<p>{message}</p>",
        sender=BUSINESS_OFFICE_EMAIL,
    )


@router.post("/parish-finance/ask-business-office")
async def ask_business_office(request: Request):
    user, parish, diocese_org, err = _parish_context(request)
    if err:
        return err
    if not can_view_finance(user, parish):
        return JSONResponse({"error": "You don't have permission to do this for this parish."}, status_code=403)
    form = await request.form()
    message = (form.get("message") or "").strip()
    if not message:
        return RedirectResponse("/parish-finance?error=empty_question", status_code=303)
    _submit_and_notify(
        parish, user,
        f"Business Office question — {parish['name']}", message,
    )
    return RedirectResponse("/parish-finance?asked=1", status_code=303)


@router.post("/parish-finance/request-direct-debit")
async def request_direct_debit(request: Request):
    user, parish, diocese_org, err = _parish_context(request)
    if err:
        return err
    if not can_view_finance(user, parish):
        return JSONResponse({"error": "You don't have permission to do this for this parish."}, status_code=403)
    if parish.get("sma_direct_debit_status") in ("requested", "enrolled"):
        return RedirectResponse("/parish-finance?error=already_requested", status_code=303)
    registry.update_parish(parish["id"], parish["org_id"], sma_direct_debit_status="requested")
    _submit_and_notify(
        parish, user,
        f"SMA direct debit enrollment request — {parish['name']}",
        f"{parish['name']} has requested to enroll in direct debit for their Shared Ministry "
        f"Allocation payments. Please follow up to complete enrollment through our normal process "
        f"(Beacon does not collect any bank account information).",
    )
    return RedirectResponse("/parish-finance?requested=1", status_code=303)


# ── Diocese admin: set/clear a parish's Middendorf GL account + direct-debit status ──

def _require_setup_admin(request: Request):
    """(user, org, None) when allowed, (None, None, response) when not —
    entity-scoped, same convention as parish_org_admin.py's own
    _require_setup_admin() (Setup Tables' precedent: the one admin area
    that's genuinely entity-scoped, not cross-entity by design)."""
    user = _current_user(request)
    if not user:
        return None, None, RedirectResponse("/login")
    from main import _current_org as _get_current_org
    org = _get_current_org(request)
    org_id = org["id"] if org else None
    if org_id is None or not rbac.user_has_any_role(user["id"], ["setup_admin", "beacon_admin"], org_id=org_id):
        return None, None, JSONResponse({"error": "Setup Admin access required"}, status_code=403)
    return user, org, None


@router.post("/admin/manage-parishes/{parish_id}/finance-settings")
async def update_finance_settings(parish_id: int, request: Request):
    """Diocese-side control for the two fields a parish can't set for
    itself: middendorf_gl_account (which loan is theirs, if any — a manual,
    ID-based mapping, deliberately never guessed from loan_accounts.xlsx's
    own ambiguous customer NAMES) and sma_direct_debit_status (the
    "designate somewhere that the parish is on the SMA direct debit plan"
    Jay asked for — flipped to 'enrolled' only once the diocese has
    actually completed real enrollment through its own separate process)."""
    user, org, err = _require_setup_admin(request)
    if err:
        return err
    parish = registry.get_parish(parish_id, org["id"])
    if not parish:
        return RedirectResponse("/admin/manage-parishes")
    form = await request.form()
    gl_account = (form.get("middendorf_gl_account") or "").strip() or None
    status = (form.get("sma_direct_debit_status") or "not_enrolled").strip()
    if status not in ("not_enrolled", "requested", "enrolled"):
        status = "not_enrolled"
    registry.update_parish(parish_id, org["id"], middendorf_gl_account=gl_account,
                            sma_direct_debit_status=status)
    return RedirectResponse("/admin/manage-parishes?finance_saved=1", status_code=303)
