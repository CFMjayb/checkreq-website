"""
qbo_mcp_client.py — thin REST client for qbo-mcp-server's 26-129 checkreq
QBO-posting endpoints (POST /api/checkreq/vendor/{company},
POST /api/check-request/{company}). AP Review Workflow Plan.md, Section 4:
this is their first real caller — both endpoints have existed since
2026-07-17/25, tested in isolation, but never called from anywhere in this
app until the AP Review "Post to QBO" action.

Auth: X-API-Key header, GCP Secret Manager secret `qbo-mcp-api-key`
(project cfm-qbo-mcp) — the same admin-level fallback auth qbo-mcp-server's
own AuthMiddleware accepts (grants allowed_companies=["*"], read_only=False).
Reads the secret live via the Secret Manager API client, same pattern as
email_client.py's _get_api_key() and this app's own sharepoint_client.py /
auth_azure.py / document_extract.py Secret Manager reads — qbo-mcp-sa
(this service's runtime identity) already has project-wide
secretmanager.secretAccessor on cfm-qbo-mcp, confirmed before writing this
(no new IAM grant needed).

Every function here returns (result_dict_or_None, error_str_or_None) —
never raises. Callers (post_to_qbo route in main.py) surface error_str
verbatim to the AP reviewer, per this codebase's hard-won 2026-07-22 lesson
from 26-124's JE-DocNumber-collision incident: always show the real Fault
detail QBO returned, never a generic message — qbo_client.py's own _post()
on the qbo-mcp-server side already does the work of extracting that detail
into the JSON error body; this client just passes it through unmodified.
"""
from __future__ import annotations

import os

import requests

QBO_MCP_URL = os.environ.get(
    "QBO_MCP_URL", "https://qbo-mcp-server-xltaug3m6q-ue.a.run.app"
).rstrip("/")

_SECRET_PROJECT = os.environ.get("FIRESTORE_PROJECT", "cfm-qbo-mcp")
_cached_api_key: str | None = None


def _get_api_key() -> str:
    global _cached_api_key
    env = os.environ.get("QBO_MCP_API_KEY", "").strip()
    if env:
        return env
    if _cached_api_key is None:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{_SECRET_PROJECT}/secrets/qbo-mcp-api-key/versions/latest"
        _cached_api_key = client.access_secret_version(name=name).payload.data.decode("utf-8").strip()
    return _cached_api_key


def _post(path_template: str, company: str, body: dict, timeout: int = 30) -> tuple[dict | None, str | None]:
    url = f"{QBO_MCP_URL}{path_template.format(company=company)}"
    try:
        resp = requests.post(
            url,
            headers={"X-API-Key": _get_api_key()},
            json=body,
            timeout=timeout,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if not resp.ok:
            return None, data.get("error") or f"HTTP {resp.status_code}: {resp.text[:300]}"
        if "error" in data:
            # Some endpoints return 200 with an {"error": ...} body on a
            # validation failure -- treat identically to a non-2xx status.
            return None, data["error"]
        return data, None
    except Exception as exc:
        return None, str(exc)


def _get(path_template: str, company: str, params: dict, timeout: int = 20) -> tuple[dict | None, str | None]:
    url = f"{QBO_MCP_URL}{path_template.format(company=company)}"
    try:
        resp = requests.get(
            url,
            headers={"X-API-Key": _get_api_key()},
            params=params,
            timeout=timeout,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if not resp.ok:
            return None, data.get("error") or f"HTTP {resp.status_code}: {resp.text[:300]}"
        if "error" in data:
            return None, data["error"]
        return data, None
    except Exception as exc:
        return None, str(exc)


def get_budget_status(company: str, account_number: str, fiscal_year: int) -> tuple[dict | None, str | None]:
    """GET /api/checkreq/budget-status/{company}. Returns
    ({"account_number", "fiscal_year", "annual_budget", "budget_found",
      "actual_spend", "as_of_date"}, None) on success, or (None, "error text")
    on failure.

    Budget Overspend Tracking Plan.md (2026-07-26): a live diagnostic query
    confirmed QBO's native Budget entity is real and usable for both
    EDOM/Claggett -- this reads it directly via qbo-mcp-server, no new
    Postgres budget table needed. First callers: main.py's
    /api/budget-status live-preview route and
    _evaluate_gl_line_budgets()'s submission-time enforcement."""
    return _get(
        "/api/checkreq/budget-status/{company}", company,
        {"gl_account_number": account_number, "fiscal_year": fiscal_year},
    )


def get_gl_detail(
    company: str, account_number: str, start_date: str, end_date: str,
) -> tuple[dict | None, str | None]:
    """GET /api/gl-detail/{company}?accounts=<account_number>&format=json.

    Reused verbatim, not a new endpoint -- this is the same canonical GL
    fetch qbo-mcp-server already exposes for the QBO Excel Tools GL Detail
    tab and 26-124's clearing-account reconciliation work (see gl_fetch.py's
    own docstring). First caller here: art_completeness.py's on-demand ART
    completeness check (Invoice Processing Intake Plan.md, Tier 2) --
    matches a period's expected vendor/GL/amount against real transactions
    already posted to QBO for that account, regardless of whether the bill
    went through Beacon or was entered directly by someone else.

    Returns ({"company", "period": {"start","end"}, "accounts": [{"account_id",
    "acct_num", "name", "transactions": [{"date","txn_type","num","name",
    "memo","amount","txn_id",...}]}]}, None) on success, or (None, "error
    text") on failure -- including a clean, non-error result when the
    account had zero activity in the window (an empty "accounts" list, not
    a failure)."""
    return _get(
        "/api/gl-detail/{company}", company,
        {
            "accounts": account_number,
            "start_date": start_date,
            "end_date": end_date,
            "format": "json",
        },
        timeout=30,
    )


def get_vendor_last_bill(company: str, qbo_vendor_id: str) -> tuple[dict | None, str | None]:
    """GET /api/checkreq/vendor-last-bill/{company}?qbo_vendor_id=...

    Invoice Intake, Monkey-See-Monkey-Do GL-coding replay (2026-08-02): a
    vendor's most recent real QBO Bill/Purchase, for replaying its exact
    GL-line split onto a new invoice -- per Jay's direct correction that
    this draws on real QBO transaction history, not Beacon's own (empty,
    for a brand-new intake flow) internal GL-line table.

    Returns ({"qbo_vendor_id", "found": bool, "bill": {...}|None}, None) on
    success, or (None, "error text") on failure. "bill", when found:
    {"txn_id", "txn_type", "txn_date", "total",
     "lines": [{"acct_id", "acct_num", "acct_name", "amount", "description"}]}."""
    return _get(
        "/api/checkreq/vendor-last-bill/{company}", company,
        {"qbo_vendor_id": qbo_vendor_id},
    )


def get_gl_accounts(company: str) -> tuple[dict | None, str | None]:
    """GET /api/checkreq/gl-accounts/{company}. JSON (not the TSV fast-load
    format the Setup Tables workbook uses -- this is a server-side call, not
    a VBA regex parser working around JSON's own slowness, so plain JSON is
    fine). Cornerstone Served Parishes Plan.md, Phase J (item 16): the GL
    Accounts view screen's first and only caller.

    Returns ({"company", "count", "rows": [{"id", "account_number",
    "account_name", "account_type", "is_active", "annual_budget"}]}, None)
    on success, or (None, "error text") on failure. annual_budget is a live
    QBO Budget lookup layered on top of this otherwise-Postgres reference
    data server-side (qbo-mcp-server's checkreq_api.get_gl_accounts()'s own
    docstring) -- None/blank for an account with no budget data, never a
    misleading 0.0."""
    return _get("/api/checkreq/gl-accounts/{company}", company, {})


def get_vendors(company: str) -> tuple[dict | None, str | None]:
    """GET /api/checkreq/vendors/{company}. JSON. Cornerstone Served
    Parishes Plan.md, Phase J (item 16): the Vendors view screen's first
    and only caller.

    Returns ({"company", "count", "rows": [{"id", "qbo_vendor_id",
    "display_name", "company_name", "address", "email", "is_active"}]},
    None) on success, or (None, "error text") on failure."""
    return _get("/api/checkreq/vendors/{company}", company, {})


def get_parish_invoices(company: str, qbo_customer_id: str) -> tuple[dict | None, str | None]:
    """GET /api/checkreq/parish-invoices/{company}?qbo_customer_id=...

    Parish Portal Plan.md S6/S7 addendum (2026-08-16): every real QBO
    Invoice for a parish's AR Customer record -- parish_finance.py filters
    the returned list down to Shared Ministry Allocation invoices
    (DocNumber matching ^\\d{4}SMA-\\d{6}) itself; this just returns the raw
    list.

    Returns ({"qbo_customer_id", "invoices": [{"txn_id", "doc_number",
    "txn_date", "total_amt", "balance"}]}, None) on success, or
    (None, "error text") on failure."""
    return _get(
        "/api/checkreq/parish-invoices/{company}", company,
        {"qbo_customer_id": qbo_customer_id},
    )


def get_parish_bill_payments(company: str, qbo_vendor_id: str) -> tuple[dict | None, str | None]:
    """GET /api/checkreq/parish-bill-payments/{company}?qbo_vendor_id=...

    Parish Portal Plan.md S6/S7 addendum (2026-08-16): real QBO BillPayment
    history for a parish's AP Vendor record -- the "payments processed"
    view.

    Returns ({"qbo_vendor_id", "payments": [{"txn_id", "txn_date",
    "total_amt", "ref_number"}]}, None) on success, or (None, "error text")
    on failure."""
    return _get(
        "/api/checkreq/parish-bill-payments/{company}", company,
        {"qbo_vendor_id": qbo_vendor_id},
    )


def get_account_balance(company: str, acct_num: str) -> tuple[dict | None, str | None]:
    """GET /api/checkreq/account-balance/{company}?acct_num=...

    Parish Portal Plan.md S6/S7 addendum (2026-08-16): a Middendorf loan's
    live current balance (the GL account IS the loan; CurrentBalance IS the
    outstanding principal) -- see qbo-mcp-server's qbo_client.get_account_balance()
    docstring for why this is read directly from QBO's own Account entity
    rather than derived from summed GL-detail transactions.

    Returns ({"acct_num", "found": bool, "account": {"acct_num",
    "acct_name", "current_balance"}|None}, None) on success, or
    (None, "error text") on failure."""
    return _get(
        "/api/checkreq/account-balance/{company}", company,
        {"acct_num": acct_num},
    )


def create_vendor(
    company: str,
    display_name: str,
    company_name: str = "",
    address_line1: str = "",
    address_line2: str = "",
    city: str = "",
    state: str = "",
    zip_code: str = "",
    phone: str = "",
    email: str = "",
) -> tuple[dict | None, str | None]:
    """POST /api/checkreq/vendor/{company}. Returns
    ({"status": "created", "vendor_id": "...", "vendor_name": "..."}, None)
    on success, or (None, "error text") on failure."""
    body = {
        "display_name": display_name,
        "company_name": company_name,
        "address_line1": address_line1,
        "address_line2": address_line2,
        "city": city,
        "state": state,
        "zip": zip_code,
        "phone": phone,
        "email": email,
    }
    return _post("/api/checkreq/vendor/{company}", company, body)


def create_bill(
    company: str,
    vendor_id: str,
    txn_date: str,
    gl_lines: list,
    doc_number: str = "",
    private_note: str = "",
    due_date: str | None = None,
    attachments: list | None = None,
) -> tuple[dict | None, str | None]:
    """POST /api/check-request/{company}. gl_lines: list of
    {"account_ref": "5100", "amount": 1500.00, "description": "..."}.
    due_date (Jay, 2026-07-29): the check request's own Needed By date --
    distinct from txn_date (the actual bill-entry date, i.e. today), which
    qbo-mcp-server sets as QBO's DueDate rather than leaving it to whatever
    the Bill's own Term calculates. Terms itself defaults server-side to
    "Net Check Run" (also 2026-07-29), not passed from here.
    Returns ({"status": "created", "bill_id", "bill_number", "qbo_url",
    "total"}, None) on success, or (None, "error text") on failure."""
    body = {
        "vendor_id": vendor_id,
        "txn_date": txn_date,
        "gl_lines": gl_lines,
        "doc_number": doc_number,
        "private_note": private_note,
        "due_date": due_date,
        "attachments": attachments or [],
    }
    return _post("/api/check-request/{company}", company, body)
