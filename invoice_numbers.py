"""invoice_numbers.py -- the vendor's invoice number on a payment request, the
QuickBooks Bill number built from it, and the duplicate check around it.

Jay, 2026-10-09: "the CR screen doesn't give you the ability to enter an invoice number", the invoice
number should be the Bill no. in QuickBooks (or the CR number when there is none), and "there should be
a check to see if an invoice is posting twice" (CR26-020 and CR26-021, two Holt invoices for the same
amount, made him worry one had been entered twice; they turned out to be two real invoices, which is why
the rules below never flag two requests whose invoice numbers differ).

What lives here (main.py only calls these, per the standing "no new logic in main.py" rule):

  clean / normalize        the invoice number as typed, and the form two numbers are compared in
  bill_number / bill_memo  what Beacon sends QuickBooks as the Bill no. and the Bill memo
  find_matches             earlier requests that look like the same invoice
  posting_block            the matches that hold a request at "Post to QBO" until AP acknowledges them
  log_confirmation         the audit row written when a submitter confirms past a warning
  register                 POST /requests/{n}/duplicate-ack, AP's "this is not a duplicate" action

THE RULES (decided with Jay):
  * Same entity + same vendor + same invoice number (case, spaces and punctuation ignored) as any request
    that is not Cancelled, Rejected or a Draft -> an INVOICE match. The submitter must confirm with a
    reason, and AP cannot post the LATER request until AP acknowledges it, with a reason of their own. The
    earlier one is not held up by a later duplicate that has not been posted -- but if the later one WAS
    posted first, the earlier one is then held too (never two Bills for one invoice). An acknowledgement is
    for one invoice number, at one vendor, against the specific requests it named.
  * Placeholders people type for "no invoice number" ('N/A', 'none', 'TBD', '-') are stored as blank.
  * Two requests that both carry an invoice number and the numbers differ are NEVER matched, whatever the
    amount.
  * Same vendor + same amount within AMOUNT_WINDOW_DAYS, where one of the two has no invoice number -> an
    AMOUNT match. A warning at submission only (the submitter confirms); it never holds a post, because
    recurring bills of one amount are normal.
  * Only existing vendors are compared (a vendor still being onboarded has nothing to match against).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import Request          # module level on purpose: with `from __future__ import annotations`
from fastapi.responses import RedirectResponse   # FastAPI resolves the route's annotations from here

import db

MAX_LEN = 21              # QuickBooks' limit for a Bill number
AMOUNT_WINDOW_DAYS = 14   # short on purpose: monthly recurring bills of one amount must not trigger it
_IGNORED_STATUSES = ["Cancelled", "Rejected", "Draft"]
ACK_ACTION = "Duplicate Acknowledged"
CONFIRM_ACTION = "Duplicate Confirmed by Submitter"
_REASON_MAX = 500


# What people type when there is NO invoice number. Stored as blank, never compared: otherwise every later request
# for the vendor that also said "N/A" would be treated as a duplicate of the first.
_PLACEHOLDERS = {"na", "none", "nil", "tbd", "tba", "unknown", "pending", "notapplicable", "noinvoice",
                 "noinvoicenumber", "nonumber"}


def _squash(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.lower())      # \W is Unicode-aware: letters and digits of any script are kept


def clean(raw) -> str:
    """The invoice number as it will be stored: whitespace collapsed and trimmed. Blank, punctuation only
    ('-') and placeholders ('N/A', 'none', 'TBD') all become ''."""
    s = re.sub(r"\s+", " ", str(raw or "")).strip()
    key = _squash(s)
    return "" if (not key or key in _PLACEHOLDERS) else s


def normalize(raw) -> str:
    """The comparison key: lower case, letters and digits only, so 'INV-0012', 'inv 0012' and 'INV0012'
    are the same invoice. Leading zeros are kept ('0012' is not '12'): a false 'different' only means a
    duplicate is caught later, a false 'same' would block a real invoice."""
    s = clean(raw)
    return _squash(s) if s else ""


def too_long(raw) -> bool:
    return len(clean(raw)) > MAX_LEN


def bill_number(invoice_number, request_number: str) -> str:
    """The Bill no. sent to QuickBooks: the invoice number, or the CR number when there is none (or when
    a legacy value is longer than QuickBooks allows)."""
    inv = clean(invoice_number)
    return inv if inv and len(inv) <= MAX_LEN else request_number


def bill_memo(invoice_number, request_number: str, description) -> str:
    """The Bill memo. Unchanged from before when the Bill no. is still the CR number. When the invoice
    number took over the Bill no., the CR number moves here so AP can still find the Bill from a CR."""
    desc = (description or "").strip()
    if bill_number(invoice_number, request_number) == request_number:
        return desc or f"Check Request {request_number}"
    return f"{request_number} - {desc}" if desc else f"Check Request {request_number}"


def _utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _view(r: dict) -> dict:
    created = _utc(r.get("created_at"))
    return {
        "id": r["id"],
        "request_number": r["request_number"],
        "status": r["status"],
        "invoice_number": clean(r.get("invoice_number")),
        "amount": float(r["amount"] or 0),
        "date": created.strftime("%m/%d/%Y") if created else "",
        "submitter": r.get("submitter") or "",
    }


def find_matches(org_id: int, vendor_id, invoice_number, amount, exclude_request_id=None) -> dict:
    """Earlier requests that look like this invoice. Returns {"invoice": [...], "amount": [...]}.
    Pass amount=0 to switch the amount rule off (the posting gate does). Never raises on bad input: no
    vendor, or nothing to compare, simply matches nothing."""
    empty = {"invoice": [], "amount": []}
    if not org_id or not vendor_id:
        return empty
    inv_key = normalize(invoice_number)
    amount = float(amount or 0)
    if not inv_key and not amount:
        return empty
    rows = db.query(
        "SELECT pr.id, pr.request_number, pr.status, pr.invoice_number, pr.amount, pr.created_at, "
        "       u.display_name AS submitter "
        "FROM checkreq.payment_requests pr "
        "JOIN checkreq.app_users u ON u.id = pr.submitter_user_id "
        "WHERE pr.org_id = %s AND pr.vendor_id = %s AND pr.status <> ALL(%s) AND pr.id <> %s "
        "ORDER BY pr.created_at DESC LIMIT 3000",
        (org_id, vendor_id, _IGNORED_STATUSES, exclude_request_id or 0),
    )
    now = datetime.now(timezone.utc)
    by_invoice: list[dict] = []
    by_amount: list[dict] = []
    for r in rows:
        other_key = normalize(r["invoice_number"])
        if inv_key and other_key == inv_key:
            by_invoice.append(_view(r))
        elif (not inv_key or not other_key) and amount and abs(float(r["amount"] or 0) - amount) < 0.005:
            created = _utc(r["created_at"])
            if created and (now - created).days <= AMOUNT_WINDOW_DAYS:
                by_amount.append(_view(r))
    return {"invoice": by_invoice, "amount": by_amount}


def describe(matches: list[dict], names: bool = False) -> str:
    """'CR26-020 (Posted to QBO, $244.22, 10/09/2026)', one per match. `names=True` (AP's own screens and
    messages only) adds who submitted it: a submitter's dialog and the audit text that is copied into the
    Approval & Audit Log never carry another person's name."""
    return "; ".join(
        f"{m['request_number']} ({m['status']}, ${m['amount']:,.2f}, {m['date']}"
        + (f", {m['submitter']}" if names and m["submitter"] else "") + ")"
        for m in matches
    )


def confirmation_needed(found: dict, invoice_number: str) -> dict | None:
    """What the 409 answer to the browser carries when a submission needs the submitter's confirmation,
    or None when there is nothing to confirm."""
    if found["invoice"]:
        return {
            "needs_duplicate_confirmation": True,
            "level": "invoice",
            "reason_required": True,
            "detail": (f"Invoice {clean(invoice_number)} from this vendor was already submitted: "
                       f"{describe(found['invoice'])}."),
        }
    if found["amount"]:
        return {
            "needs_duplicate_confirmation": True,
            "level": "amount",
            "reason_required": False,
            "detail": (f"This vendor already has a request for the same amount in the last "
                       f"{AMOUNT_WINDOW_DAYS} days: {describe(found['amount'])}."),
        }
    return None


def confirmed(form, found: dict) -> dict | None:
    """The submitter's confirmation, if the form carries a valid one: {'reason': str} -- or None when
    they have not confirmed (or an invoice match has no reason)."""
    if form.get("confirmed_duplicate") != "1":
        return None
    reason = clean(form.get("duplicate_reason"))[:_REASON_MAX]
    if found["invoice"] and not reason:
        return None
    return {"reason": reason}


def log_confirmation(cur, payment_request_id: int, user_id: int, found: dict, confirm: dict,
                     impersonated_by=None) -> None:
    """The audit row for a submitter who went ahead past a duplicate warning. Written inside the same
    transaction that creates or updates the request."""
    kind = "same invoice number" if found["invoice"] else "same vendor and amount"
    matches = found["invoice"] or found["amount"]
    comment = f"Submitter confirmed this is not a duplicate ({kind}) of {describe(matches)}."
    if confirm.get("reason"):
        comment += f" Reason: {confirm['reason']}"
    cur.execute(
        "INSERT INTO checkreq.audit_log "
        "(payment_request_id, action_by_user_id, action_type, comment, impersonated_by_user_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (payment_request_id, user_id, CONFIRM_ACTION, comment, impersonated_by),
    )


def _ack_key(pr: dict) -> str:
    """What an acknowledgement is for: this invoice number AT THIS VENDOR. Changing either re-opens the check."""
    return f"{normalize(pr.get('invoice_number'))}:{pr.get('vendor_id')}"


def _acked_numbers(payment_request_id: int, key: str) -> set[str]:
    """The request numbers AP has already acknowledged as 'not a duplicate' for this request and key. The
    audit comment starts '[key|CR26-001,CR26-002]', so a twin that appears AFTER an acknowledgement is not
    covered by it."""
    out: set[str] = set()
    rows = db.query(
        "SELECT comment FROM checkreq.audit_log WHERE payment_request_id = %s AND action_type = %s",
        (payment_request_id, ACK_ACTION),
    )
    for r in rows:
        m = re.match(r"\[([^|\]]*)\|([^\]]*)\]", r["comment"] or "")
        if m and m.group(1) == key:
            out.update(x for x in m.group(2).split(",") if x)
    return out


def holding_matches(pr: dict) -> list[dict]:
    """The requests with the SAME invoice number for this vendor that can hold THIS request at 'Post to
    QBO': the ones submitted before it (a lower id), and any that is already Posted to QBO whichever came
    first -- so when a later duplicate was posted first, the original cannot then be posted on top of it.
    A later duplicate that has not been posted does not hold the original (the original must not be held
    up by a request that came after it). `pr` needs id, org_id, vendor_id and invoice_number."""
    if not normalize(pr.get("invoice_number")) or not pr.get("vendor_id"):
        return []
    found = find_matches(pr["org_id"], pr["vendor_id"], pr["invoice_number"], 0, pr["id"])["invoice"]
    return [m for m in found if m["id"] < pr["id"] or m["status"] == "Posted to QBO"]


def posting_block(pr: dict) -> list[dict]:
    """The requests that hold this request at 'Post to QBO' and that AP has not yet acknowledged. Empty
    when nothing holds it. An acknowledgement belongs to the invoice number, the vendor and the specific
    requests it was given for: changing the number or the vendor, or a NEW twin appearing, re-opens it."""
    holding = holding_matches(pr)
    if not holding:
        return []
    acked = _acked_numbers(pr["id"], _ack_key(pr))
    return [m for m in holding if m["request_number"] not in acked]


def hold_message(matches: list[dict]) -> str:
    return (f"possible duplicate invoice -- the same invoice number was already submitted: "
            f"{describe(matches, names=True)}. Open the request (Edit) and acknowledge it before posting")


def register(app, *, current_user, impersonated_by, can_ap_edit, request_is_editable) -> None:
    """POST /requests/{request_number}/duplicate-ack -- AP's 'this is not a duplicate' action, from the
    AP edit screen. Writes one audit row tied to the invoice number as it is right now."""
    @app.post("/requests/{request_number}/duplicate-ack")
    async def duplicate_ack(request_number: str, request: Request):
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        pr = db.query_one(
            "SELECT id, org_id, vendor_id, invoice_number, status FROM checkreq.payment_requests "
            "WHERE request_number = %s", (request_number,))
        if not pr:
            return RedirectResponse("/my-requests", status_code=303)
        back = f"/requests/{request_number}/ap-edit"
        if not can_ap_edit(user["id"], pr["org_id"]):
            return RedirectResponse(back, status_code=303)   # the AP edit page itself refuses this user
        if not request_is_editable(pr["status"]):
            return RedirectResponse(f"{back}?error={quote('This request can no longer be changed.')}",
                                    status_code=303)
        form = await request.form()
        reason = clean(form.get("reason"))[:_REASON_MAX]
        if not reason:
            return RedirectResponse(f"{back}?error={quote('Please say why this is not a duplicate.')}",
                                    status_code=303)
        found = holding_matches(pr)
        if found:
            numbers = ",".join(sorted(m["request_number"] for m in found))
            with db.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO checkreq.audit_log "
                        "(payment_request_id, action_by_user_id, action_type, comment, "
                        " impersonated_by_user_id) VALUES (%s, %s, %s, %s, %s)",
                        (pr["id"], user["id"], ACK_ACTION,
                         f"[{_ack_key(pr)}|{numbers}] AP confirmed this is not a duplicate of "
                         f"{describe(found)}. Reason: {reason}",
                         impersonated_by(request)),
                    )
        return RedirectResponse(f"{back}?saved=dupack", status_code=303)
