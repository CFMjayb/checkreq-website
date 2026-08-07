"""
auth_code.py — emailed one-time sign-in code for the 26-129 website.

Universal fallback identity flavor for anyone whose email domain doesn't map
to Microsoft or Google in checkreq.identity_provider_domains (Multi-Provider
Authentication Plan Addendum 2026-08-06, decision 1) — parish volunteers on
Outlook.com/iCloud/ISP/small-custom-domain addresses with no IT department
behind them. Deliberately mirrors auth_azure.py / auth_google.py's shape
(the caller gets a claims-like result) so the third provider reads as part of
the same family, not a bolted-on special case.

This is also the self-service password-reset path (addendum, Sequencing):
a successful code verification re-proves identity exactly as well as a fresh
password would, so there is no separate "forgot password" flow anywhere in
this codebase — see auth_password.py.

Security model, deliberately proportioned to what a 6-digit code actually is
(a short-lived, single-use proof of mailbox control, not a long-term
credential like a password):
  - 6 digits, ~10-minute expiry, single use (used_at stamped on success).
  - Stored as an HMAC-SHA256 of the code, keyed on SESSION_SECRET (the same
    secret this app already provisions for cookie signing — reusing it here
    avoids standing up a whole new secret for one extra HMAC key). This means
    a raw database copy alone can't be brute-forced offline against the
    small 6-digit keyspace; the server-side key is also required.
  - Max 5 verify attempts per code (checked BEFORE comparing, so a code
    already at its limit fails closed even if the guess would otherwise be
    right).
  - Per-email issue rate limit: at most 3 codes per 15 minutes. Silent from
    the caller's perspective (see issue_code()'s docstring) — this only
    stops a NEW code from being generated/sent; it is not surfaced as an
    error, to avoid telling an attacker anything about issue volume either.
  - No account enumeration: issue_code() does the exact same thing (and the
    caller shows the exact same "check your email" response) whether or not
    the email has an app_users row. Nothing is sent for an unregistered
    email, but nothing about the HTTP response reveals that.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets as pysecrets
from datetime import datetime, timedelta, timezone

import db
import email_client

_CODE_TTL_MINUTES = 10
_MAX_VERIFY_ATTEMPTS = 5
_MAX_CODES_PER_WINDOW = 3
_ISSUE_WINDOW_MINUTES = 15

# Same secret main.py's SessionMiddleware already uses -- see module
# docstring. Not a new Secret Manager entry.
_HMAC_KEY = os.environ.get("SESSION_SECRET", "dev-only-not-secure").encode("utf-8")

# Reuses the same allowed 26-122 sender identity every other outbound email
# in this app already uses (main.py's W9_SENDER_EMAIL) -- no new sender.
_SENDER_EMAIL = os.environ.get("W9_SENDER_EMAIL", "businessoffice@episcopalmaryland.org")


def _hash_code(code: str) -> str:
    return hmac.new(_HMAC_KEY, code.encode("utf-8"), hashlib.sha256).hexdigest()


def _generate_code() -> str:
    return f"{pysecrets.randbelow(1_000_000):06d}"


def issue_code(email: str, requesting_ip: str = "") -> None:
    """Request a fresh sign-in code for email. ALWAYS returns None and never
    raises for a caller-visible reason -- the route calling this must show
    the identical "check your email" response regardless of what happened
    here (no account enumeration; see module docstring).

    Internally: rate-limits to _MAX_CODES_PER_WINDOW per email per
    _ISSUE_WINDOW_MINUTES (silently skips issuing/sending past that, same
    response either way), then always inserts a login_codes row and sends
    the email -- deliberately NOT gated on whether an app_users row exists,
    since checking that first and skipping the DB insert only for
    unregistered emails would itself be a timing/behavior side-channel."""
    email = email.strip().lower()
    # _ISSUE_WINDOW_MINUTES is a trusted internal constant, not user input --
    # safe to embed directly; email stays a real bound parameter below.
    recent = db.query_one(
        f"SELECT COUNT(*) AS n FROM checkreq.login_codes "
        f"WHERE email = %s AND created_at > NOW() - INTERVAL '{_ISSUE_WINDOW_MINUTES} minutes'",
        (email,),
    )
    if recent and recent["n"] >= _MAX_CODES_PER_WINDOW:
        return

    code = _generate_code()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=_CODE_TTL_MINUTES)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkreq.login_codes (email, code_hash, expires_at, requested_ip) "
                "VALUES (%s, %s, %s, %s)",
                (email, _hash_code(code), expires_at, requesting_ip or None),
            )

    email_client.send_email(
        to=email,
        subject=f"Your Beacon sign-in code: {code}",
        body_text=(
            f"Your Beacon sign-in code is {code}\n\n"
            f"This code expires in {_CODE_TTL_MINUTES} minutes and can only be used once.\n\n"
            f"If you didn't request this, you can safely ignore this email."
        ),
        body_html=(
            f"<p>Your Beacon sign-in code is:</p>"
            f"<p style=\"font-size:28px;font-weight:700;letter-spacing:4px;\">{code}</p>"
            f"<p>This code expires in {_CODE_TTL_MINUTES} minutes and can only be used once.</p>"
            f"<p style=\"color:#666;font-size:13px;\">If you didn't request this, you can safely ignore this email.</p>"
        ),
        sender=_SENDER_EMAIL,
    )


def verify_code(email: str, code: str, requesting_ip: str = "") -> bool:
    """Check code against the most recent unexpired, unused login_codes row
    for email. Increments attempts on every check (success or failure) and
    marks the row used on success -- a code that succeeds cannot be replayed,
    and a code that hits _MAX_VERIFY_ATTEMPTS fails closed even for the
    correct value from then on."""
    email = email.strip().lower()
    row = db.query_one(
        "SELECT * FROM checkreq.login_codes WHERE email = %s AND used_at IS NULL "
        "AND expires_at > NOW() ORDER BY created_at DESC LIMIT 1",
        (email,),
    )
    if not row or row["attempts"] >= _MAX_VERIFY_ATTEMPTS:
        return False

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.login_codes SET attempts = attempts + 1 WHERE id = %s",
                (row["id"],),
            )

    if not hmac.compare_digest(row["code_hash"], _hash_code(code.strip())):
        return False

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.login_codes SET used_at = NOW() WHERE id = %s",
                (row["id"],),
            )
    return True
