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
) -> tuple[dict | None, str | None]:
    """POST /api/check-request/{company}. gl_lines: list of
    {"account_ref": "5100", "amount": 1500.00, "description": "..."}.
    Returns ({"status": "created", "bill_id", "bill_number", "qbo_url",
    "total"}, None) on success, or (None, "error text") on failure."""
    body = {
        "vendor_id": vendor_id,
        "txn_date": txn_date,
        "gl_lines": gl_lines,
        "doc_number": doc_number,
        "private_note": private_note,
    }
    return _post("/api/check-request/{company}", company, body)
