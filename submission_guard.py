"""submission_guard.py -- one form load can produce one request, never two.

Why this exists (2026-10-09, production CR26-016 / CR26-017): a submitter's
Submit click did nothing visible for several seconds (the browser was first
uploading the form to a budget pre-check), so they clicked again, and the
server created two complete, identical $25,000 requests 1.2 seconds apart.
Nothing on the server noticed. The page now also locks its own buttons (see
new_request.js); this module is the server-side backstop, which holds even
when the browser misbehaves, the network drops the reply and the person tries
again, or the Back button brings the form back.

How it works
  * GET /new-request puts a random one-time token in the form (new_token()).
  * Creating a request (a submission or a brand-new draft) claims that token
    INSIDE the same database transaction that inserts the request:
        INSERT INTO checkreq.submission_tokens ... ON CONFLICT DO NOTHING
    The token row and the request commit together or not at all. A refused
    or failed submission rolls the token back too, so the person can fix the
    form and send it again with the same page.
  * A second submission carrying a token that already produced a request does
    not create anything: it is answered with the same redirect the first one
    got (redirect_url()), so both browser calls land on the same page.
  * Two submissions arriving at the very same instant are serialised by the
    database itself: the second INSERT waits for the first transaction, then
    sees the conflict.

Edits to an existing request never carry a token (editing twice is harmless),
and a form with no token, or a malformed one, behaves exactly as before.

Fails open: if checkreq.submission_tokens does not exist yet (the migration
has not been applied to this database), every function here does nothing and
submissions work as they always did.

main.py gains only wiring: the import, the hidden token in the form's context,
the early check, the two try/except around the inserts, and the claim/link
calls inside the two insert transactions.
"""
from __future__ import annotations

import re
import secrets
import time

import db

# 24 random bytes in URL-safe base64 is 32 characters; accept a little range so
# a future length change never silently turns the guard off.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# A missing table is re-checked this often (seconds) so applying the migration
# turns the guard on without a restart; once present it is never asked again.
_RECHECK_SECONDS = 60
_state = {"ok": None, "checked": 0.0}


class DuplicateSubmission(Exception):
    """Raised inside an insert transaction when this form load's token has
    already produced a request. Nothing has been written by that transaction
    (the claim was the first statement), so rolling it back loses nothing."""


def available() -> bool:
    """True once checkreq.submission_tokens exists. False (guard off) while
    the migration is missing, or if the check itself cannot run."""
    if _state["ok"] is True:
        return True
    now = time.monotonic()
    if _state["ok"] is False and now - _state["checked"] < _RECHECK_SECONDS:
        return False
    try:
        row = db.query_one("SELECT to_regclass('checkreq.submission_tokens') IS NOT NULL AS ok")
        ok = bool(row and row["ok"])
    except Exception:
        ok = False
    _state["ok"] = ok
    _state["checked"] = now
    return ok


def new_token() -> str:
    """A fresh one-time token for one rendering of the New Request form."""
    return secrets.token_urlsafe(24)


def token_from_form(form) -> str | None:
    """The posted token, or None when it is missing or not shaped like one we
    issued (an old page opened before this shipped, a crafted post)."""
    raw = (form.get("submission_token") or "").strip()
    return raw if _TOKEN_RE.match(raw) else None


def find_existing(token: str | None) -> dict | None:
    """The request this token already produced -- {request_number, status} --
    or None (unknown token, no request yet, or the guard is off)."""
    if not token or not available():
        return None
    return db.query_one(
        "SELECT pr.request_number, pr.status "
        "FROM checkreq.submission_tokens st "
        "JOIN checkreq.payment_requests pr ON pr.id = st.payment_request_id "
        "WHERE st.token = %s",
        (token,),
    )


def claim(cur, token: str | None) -> None:
    """First statement of an insert transaction. Does nothing without a token
    or while the guard is off; raises DuplicateSubmission when the token has
    already produced (or is in the middle of producing) a request."""
    if not token or not available():
        return
    cur.execute(
        "INSERT INTO checkreq.submission_tokens (token) VALUES (%s) "
        "ON CONFLICT (token) DO NOTHING RETURNING token",
        (token,),
    )
    if cur.fetchone() is None:
        raise DuplicateSubmission(token)


def link(cur, token: str | None, payment_request_id: int) -> None:
    """Records which request a claimed token produced (same transaction)."""
    if not token or not available():
        return
    cur.execute(
        "UPDATE checkreq.submission_tokens SET payment_request_id = %s WHERE token = %s",
        (payment_request_id, token),
    )


def redirect_url(existing: dict | None, ui_variant: str | None = None) -> str:
    """Where a repeated submission is sent: the page the FIRST one ended on.
    A draft goes back to My Requests as a saved draft; a submitted request goes
    to My Requests as submitted (or its detail page for the Easy View).
    Without a known request, plain My Requests."""
    if not existing:
        return "/my-requests"
    number = existing["request_number"]
    if existing["status"] == "Draft":
        return f"/my-requests?draft_saved={number}"
    if ui_variant == "easy":
        return f"/requests/{number}/view?submitted=1"
    return f"/my-requests?submitted={number}"
