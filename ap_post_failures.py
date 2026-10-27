"""
ap_post_failures.py -- what AP Review shows, and lets AP do, when a post to QuickBooks did not go through.

26-129, Jay 2026-10-27. CR26-025 and CR26-026 were refused by QuickBooks (error 6140: the invoice number was already on
a Bill for that vendor). Beacon correctly left both "Ready to post" -- but the reason only appeared in the one-time banner
after the click, and nothing on the screen said they had failed. Four things, all kept here so main.py only gets wiring:

  1. record_failure / failed       Every failed post is written to the request's history ("QBO Post Failed" + the full
                                   message), so the reason is still there after the banner is gone.
  2. latest_failures / decorate_rows   AP Review rows get a red !, a badge, a short reason, and the full message for the
                                   hover; a failed row gets "Try QBO Post again" and "Cancel CR".
  3. find_existing_bill / refuse_if_in_quickbooks   Before a Bill is created, ask QuickBooks whether this vendor already
                                   has a Bill under the same number. If so the row is held (no checkbox, no Post button)
                                   and nothing is sent. QuickBooks' own refusal stays the real gate: this lookup is
                                   advisory and FAILS OPEN (an unreachable QuickBooks, or a server without the lookup
                                   route, simply means no hold).
  4. POST /admin/ap-review/{request_number}/cancel   AP cancels the request. SOFT ONLY: status becomes 'Cancelled', the
                                   reason goes in the history, the row is never deleted (standing rule 2026-08-15).

Lookup route: GET /api/checkreq/bill-duplicates/{company} on qbo-mcp-server (a read-only wrapper over find_duplicate_bills,
added with this feature). Until that route is live the hold is still found for any request that has already failed once
(the TxnId in QuickBooks' own error text is read back through the existing /api/entity/.../raw route).

No hours, no pay, no personal data here: request numbers, invoice numbers, amounts and QuickBooks' own error text.
"""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

import db
import invoice_numbers
import org_time
import qbo_mcp_client

FAILED_ACTION = "QBO Post Failed"
CANCEL_ACTION = "Cancelled by AP"
MAX_MESSAGE = 2000
MAX_REASON = 500

CHECK_SECONDS = 8            # the most the AP Review page waits for QuickBooks, all rows together
CACHE_SECONDS = 120          # a page reload within this time reuses the answer; a real post never does
ROUTE_MISSING_SECONDS = 600  # after a 404 from the lookup route, stop asking for this long

_DUP_RE = re.compile(r"DocNumber=(.+?)\s+is assigned to TxnType=(\w+)\s+with TxnId=(\d+)", re.IGNORECASE)
_CODE_6140_RE = re.compile(r"\bcode\s*6140\b", re.IGNORECASE)

_cache: dict = {}
_cache_lock = threading.Lock()
_route_missing_until = 0.0


# ── plain-words reading of QuickBooks' error text ─────────────────────────────────────────────────────────────────────
def _clean(text) -> str:
    return " ".join(str(text or "").split())


def money(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return ""


def parse_error(message) -> dict:
    """What a failure message means. {"kind": "duplicate_number" | "other", "headline": short plain text,
    "doc_number", "txn_type", "txn_id" (None unless QuickBooks named the Bill)}. Never raises."""
    text = _clean(message)
    if text.lower().startswith("already in quickbooks"):          # our own refusal text (already_text)
        head = text.split(". Nothing was sent", 1)[0]
        return {"kind": "duplicate_number", "doc_number": None, "txn_type": None, "txn_id": None,
                "headline": head if len(head) <= 220 else head[:217] + "..."}
    m = _DUP_RE.search(text)
    if m or _CODE_6140_RE.search(text) or "duplicate document number" in text.lower():
        out = {"kind": "duplicate_number", "doc_number": None, "txn_type": None, "txn_id": None}
        if m:
            out.update(doc_number=m.group(1).strip(), txn_type=m.group(2), txn_id=m.group(3))
            out["headline"] = (f"QuickBooks refused it: invoice number {out['doc_number']} is already used by "
                               f"{out['txn_type']} Id {out['txn_id']} for this vendor.")
        else:
            out["headline"] = "QuickBooks refused it: that invoice number is already used by another Bill for this vendor."
        return out
    short = text if len(text) <= 160 else text[:157] + "..."
    return {"kind": "other", "headline": short or "The post did not go through.",
            "doc_number": None, "txn_type": None, "txn_id": None}


def describe_bill(bill: dict) -> str:
    """'Bill Id 9590: invoice 1053836, $750.00, dated 2026-10-20, unpaid (balance $750.00)'."""
    bits = [f"invoice {bill.get('doc_number')}"]
    if bill.get("total") not in (None, ""):
        bits.append(money(bill.get("total")))
    if bill.get("txn_date"):
        bits.append(f"dated {bill['txn_date']}")
    bal = bill.get("balance")
    if bal not in (None, ""):
        try:
            bits.append("paid in full" if float(bal) <= 0 else f"unpaid (balance {money(bal)})")
        except (TypeError, ValueError):
            pass
    return f"Bill Id {bill.get('id')}: " + ", ".join(bits)


def already_text(bill: dict) -> str:
    return (f"Already in QuickBooks -- {describe_bill(bill)}. Nothing was sent. Change the invoice # on the Edit "
            f"screen if this is a different invoice, or cancel this request.")


# ── the QuickBooks lookup (advisory; fails open) ──────────────────────────────────────────────────────────────────────
def _norm(raw: dict) -> dict:
    """One Bill, from either the lookup route's shape or a raw QuickBooks Bill."""
    vendor = raw.get("vendor_id")
    if vendor is None:
        vendor = (raw.get("VendorRef") or {}).get("value")
    return {
        "id": str(raw.get("id") or raw.get("Id") or ""),
        "doc_number": raw.get("doc_number") if raw.get("doc_number") is not None else raw.get("DocNumber"),
        "txn_date": raw.get("txn_date") or raw.get("TxnDate"),
        "total": raw.get("total") if raw.get("total") is not None else raw.get("TotalAmt"),
        "balance": raw.get("balance") if raw.get("balance") is not None else raw.get("Balance"),
        "vendor_id": str(vendor) if vendor is not None else "",
    }


def _lookup(company: str, vendor_qbo_id: str, bill_number: str, hint_txn_id: str | None) -> dict | None:
    global _route_missing_until
    now = time.time()
    if now >= _route_missing_until:
        data, err = qbo_mcp_client.find_duplicate_bills(company, vendor_qbo_id, bill_number)
        if err is None and isinstance(data, dict) and isinstance(data.get("bills"), list):
            bills = data["bills"]
            return _norm(bills[0]) if bills else None          # QuickBooks answered: that is the answer
        if err and str(err).startswith("HTTP 404"):
            _route_missing_until = now + ROUTE_MISSING_SECONDS   # an older server: do not ask again for a while
    if hint_txn_id and str(hint_txn_id).isdigit():
        raw, err = qbo_mcp_client.get_bill_raw(company, hint_txn_id)
        if err is None and isinstance(raw, dict) and raw.get("Id"):
            b = _norm(raw)
            same_number = _clean(b["doc_number"]).lower() == _clean(bill_number).lower()
            same_vendor = (not vendor_qbo_id) or b["vendor_id"] == str(vendor_qbo_id)
            if same_number and same_vendor:
                return b
    return None


def find_existing_bill(company: str, vendor_qbo_id, bill_number: str, hint_txn_id=None, fresh: bool = False) -> dict | None:
    """The Bill QuickBooks already holds for this vendor under this number, or None (none, or could not tell).
    `fresh=True` (a real post) ignores the short cache. Never raises."""
    if not company or not vendor_qbo_id or not _clean(bill_number):
        return None
    key = (str(company).lower(), str(vendor_qbo_id), _clean(bill_number).lower())
    if not fresh:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    try:
        found = _lookup(str(company), str(vendor_qbo_id), _clean(bill_number), hint_txn_id)
    except Exception as exc:        # advisory only
        print(f"[ap_post_failures] QuickBooks lookup failed ({company}, {bill_number}): {exc}")
        return None
    with _cache_lock:
        _cache[key] = (time.time(), found)
    return found


def clear_cache() -> None:
    global _route_missing_until
    with _cache_lock:
        _cache.clear()
    _route_missing_until = 0.0


# ── history rows ──────────────────────────────────────────────────────────────────────────────────────────────────────
def record_failure(pr_id: int, user_id: int, impersonated_by, message: str) -> None:
    """One 'QBO Post Failed' row in the request's history. Never raises: a failure to record must not hide the failure."""
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO checkreq.audit_log (payment_request_id, action_by_user_id, action_type, comment, "
                    " previous_status, new_status, impersonated_by_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (pr_id, user_id, FAILED_ACTION, _clean(message)[:MAX_MESSAGE], "Approved", "Approved", impersonated_by),
                )
    except Exception as exc:
        print(f"[ap_post_failures] could not record the failure for request id {pr_id}: {exc}")


def failed(pr: dict, user: dict, impersonated_by, message: str) -> tuple[bool, str]:
    """For `return ap_post_failures.failed(...)` at the failure sites of the post: records it and returns (False, message)."""
    record_failure(pr["id"], user["id"], impersonated_by, message)
    return False, message


def refuse_if_in_quickbooks(pr: dict, company: str, qbo_vendor_id, user: dict, impersonated_by) -> str | None:
    """The check made just before a Bill is created. Returns the refusal text (and records it) when QuickBooks already
    has a Bill for this vendor under the number this request would use; None when it is clear or QuickBooks cannot say."""
    number = invoice_numbers.bill_number(pr.get("invoice_number"), pr["request_number"])
    bill = find_existing_bill(company, qbo_vendor_id, number, fresh=True)
    if not bill:
        return None
    msg = already_text(bill)
    record_failure(pr["id"], user["id"], impersonated_by, msg)
    return msg


# ── what the AP Review rows show ──────────────────────────────────────────────────────────────────────────────────────
def latest_failures(pr_ids: list[int]) -> dict:
    """{payment_request_id: {"full", "when_dt", "by", "edited_since"}} for each request whose latest failure belongs to its
    CURRENT approval (a failure from before it was last approved is history, not a live problem)."""
    if not pr_ids:
        return {}
    try:
        rows = db.query(
            """
            SELECT DISTINCT ON (al.payment_request_id)
                   al.id, al.payment_request_id, al.comment, al.action_date,
                   COALESCE(u.display_name, u.email) AS by_name,
                   EXISTS (SELECT 1 FROM checkreq.audit_log e
                            WHERE e.payment_request_id = al.payment_request_id AND e.id > al.id
                              AND e.action_type LIKE 'Edited%%') AS edited_since
              FROM checkreq.audit_log al
              LEFT JOIN checkreq.app_users u ON u.id = al.action_by_user_id
             WHERE al.payment_request_id = ANY(%s) AND al.action_type = %s
               AND al.id > COALESCE((SELECT MAX(f.id) FROM checkreq.audit_log f
                                      WHERE f.payment_request_id = al.payment_request_id
                                        AND f.action_type = 'Fully Approved'), 0)
             ORDER BY al.payment_request_id, al.id DESC
            """,
            (list(pr_ids), FAILED_ACTION),
        )
    except Exception as exc:
        print(f"[ap_post_failures] could not read failures: {exc}")
        return {}
    return {r["payment_request_id"]: {"full": r["comment"] or "", "when_dt": r["action_date"], "by": r["by_name"] or "",
                                      "edited_since": bool(r["edited_since"])} for r in rows}


def _check_many(jobs: dict) -> dict:
    """{request_number: bill or None} for {request_number: (company, vendor_qbo_id, bill_number, hint)}, all at once, within
    CHECK_SECONDS. A row that has not answered in time is simply not held."""
    if not jobs:
        return {}
    ex = ThreadPoolExecutor(max_workers=6)
    futures = {ex.submit(find_existing_bill, *args): rn for rn, args in jobs.items()}
    done, _pending = wait(list(futures), timeout=CHECK_SECONDS)
    ex.shutdown(wait=False, cancel_futures=True)
    out = {}
    for fut in done:
        try:
            out[futures[fut]] = fut.result()
        except Exception:
            out[futures[fut]] = None
    return out


def _alert_title(row: dict) -> str:
    lines = []
    if row.get("qbo_dup_text"):
        lines.append(row["qbo_dup_text"])
    f = row.get("post_failure")
    if f:
        who = f" by {f['by']}" if f.get("by") else ""
        lines.append(f"Last QuickBooks post attempt{who}, {f['when']}, did not go through:")
        lines.append(f["full"])
        if f.get("edited_since"):
            lines.append("(The request was edited after that attempt.)")
    return "\n".join(lines)


def decorate_rows(rows: list[dict]) -> None:
    """Adds to each pending AP Review row: post_failure, qbo_dup, qbo_dup_text, row_alert, alert_title. Rows need
    pr_id, request_number, org_id, org_code, invoice_number and the vendor's QuickBooks id (vendor_qbo_id for an existing
    vendor, vr_qbo_vendor_id for a new one). Rows already held for another reason are not looked up. Never raises."""
    for r in rows:
        r.update(post_failure=None, qbo_dup=None, qbo_dup_text=None, row_alert=False, alert_title="")
    if not rows:
        return
    failures = latest_failures([r["pr_id"] for r in rows])
    jobs = {}
    for r in rows:
        vq = r.get("vendor_qbo_id") or r.get("vr_qbo_vendor_id")
        if not vq or r.get("vendor_gate_wait") or r.get("dup_hold"):
            continue
        f = failures.get(r["pr_id"])
        hint = parse_error(f["full"]).get("txn_id") if f else None
        # a request QuickBooks has already named a Bill for is read back every time (not from the short cache)
        jobs[r["request_number"]] = (r["org_code"], vq,
                                     invoice_numbers.bill_number(r.get("invoice_number"), r["request_number"]), hint,
                                     bool(hint))
    try:
        found = _check_many(jobs)
    except Exception as exc:
        print(f"[ap_post_failures] live check failed: {exc}")
        found = {}
    zones: dict = {}
    for r in rows:
        bill = found.get(r["request_number"])
        f = failures.get(r["pr_id"])
        if bill:
            r["qbo_dup"] = bill
            r["qbo_dup_text"] = already_text(bill)
        if f:
            if r["org_id"] not in zones:
                try:
                    zones[r["org_id"]] = org_time.zone_name_for_org(r["org_id"])
                except Exception:
                    zones[r["org_id"]] = None
            try:
                when = org_time.format_local(f["when_dt"], zones[r["org_id"]])
            except Exception:
                when = str(f["when_dt"])[:16]
            r["post_failure"] = {"headline": parse_error(f["full"])["headline"], "full": f["full"], "when": when,
                                 "by": f["by"], "edited_since": f["edited_since"]}
        r["row_alert"] = bool(bill or f)
        r["alert_title"] = _alert_title(r)


# ── the Cancel CR action ──────────────────────────────────────────────────────────────────────────────────────────────
def register(app, *, current_user, impersonated_by, can_ap_edit, request_is_editable) -> None:
    """POST /admin/ap-review/{request_number}/cancel -- AP cancels a request from the AP Review screen. Soft only."""
    @app.post("/admin/ap-review/{request_number}/cancel")
    async def ap_cancel(request_number: str, request: Request):
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        back = "/admin/ap-review"

        def refuse(text: str):
            return RedirectResponse(f"{back}?post_error={quote(f'{request_number}: {text}')}", status_code=303)

        pr = db.query_one("SELECT id, org_id, status FROM checkreq.payment_requests WHERE request_number = %s",
                          (request_number,))
        if not pr:
            return refuse("request not found")
        if not can_ap_edit(user["id"], pr["org_id"]):
            return JSONResponse({"error": "Not authorized to cancel this request"}, status_code=403)
        if not request_is_editable(pr["status"]):
            return refuse("this request can no longer be cancelled (already posted to QBO or already cancelled)")
        form = await request.form()
        reason = _clean(form.get("cancel_reason"))[:MAX_REASON]
        if not reason:
            return refuse("a reason is required to cancel")
        done = False
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE checkreq.payment_requests SET status = 'Cancelled', updated_at = NOW() "
                    "WHERE id = %s AND status = %s", (pr["id"], pr["status"]))
                if cur.rowcount == 1:
                    cur.execute(
                        "INSERT INTO checkreq.audit_log (payment_request_id, action_by_user_id, action_type, comment, "
                        " previous_status, new_status, impersonated_by_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (pr["id"], user["id"], CANCEL_ACTION, f"Cancelled by AP from AP Review. Reason: {reason}",
                         pr["status"], "Cancelled", impersonated_by(request)))
                    done = True
        if not done:
            return refuse("someone already acted on this request, so it was not cancelled")
        return RedirectResponse(f"{back}?cancelled={quote(request_number)}", status_code=303)
