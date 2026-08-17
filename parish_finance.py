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
    Returns ([{year, original_amount, paid_to_date, current_balance}], error)
    sorted by year descending — error is None on success (an empty list is
    a valid, non-error result: this parish simply has no SMA invoices yet,
    or none matching the pattern)."""
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
                                       "paid_to_date": 0.0, "current_balance": 0.0})
        total = float(inv.get("total_amt") or 0)
        balance = float(inv.get("balance") or 0)
        row["original_amount"] += total
        row["current_balance"] += balance
        row["paid_to_date"] += (total - balance)
    return sorted(years.values(), key=lambda r: r["year"], reverse=True), None


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

    pdf_bytes = render_sma_statement_pdf(parish, diocese_org, sma_years)
    filename = f"{parish['name']} SMA Statement.pdf".replace("/", "-")
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def render_sma_statement_pdf(parish: dict, diocese_org: dict, sma_years: list[dict]) -> bytes:
    """Renders sma_statement.html + inlined tokens.css and hands it to
    main.py's real-headless-Chromium PDF renderer — same two-step pattern
    render_check_voucher_pdf() already uses (fragment render, then wrap
    with tokens.css + a Google Fonts <link> so a real browser resolves
    var(--token) natively). Local imports from main.py to avoid a circular
    import at module-load time (main.py imports this module) — resolved
    lazily, by which point main.py is fully loaded."""
    import os
    from main import templates as _templates, _html_to_pdf_bytes as _to_pdf

    fragment = _templates.env.get_template("sma_statement.html").render(
        parish=parish, diocese_org=diocese_org, sma_years=sma_years,
    )
    static_css_dir = os.path.join(os.path.dirname(__file__), "static", "css")
    with open(os.path.join(static_css_dir, "tokens.css"), encoding="utf-8") as f:
        tokens_css = f.read()

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Merriweather:wght@300;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
{tokens_css}
body {{ font-family: 'Inter', sans-serif; margin: 0; padding: 24px; background: #fff; color: var(--color-ink); }}
h1 {{ font-family: 'Merriweather', serif; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
th, td {{ padding: 6px 10px; text-align: left; border-bottom: 1px solid #ddd; }}
th {{ background: var(--color-franciscan-green); color: #fff; }}
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
