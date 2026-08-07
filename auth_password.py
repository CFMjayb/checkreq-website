"""
auth_password.py — opt-in Beacon-managed password for the 26-129 website.

Second half of the universal fallback (Multi-Provider Authentication Plan
Addendum 2026-08-06, decision 1) — anyone signing in via auth_code.py's
emailed code may optionally set a password from that signed-in session, so
repeat sign-ins don't require waiting on an email every time. First sign-in
for a fallback-domain user is ALWAYS via emailed code; a password can only
ever be set or changed from an already-authenticated session (addendum: "set/
change only from a signed-in session") -- there is no separate "create your
password" link anywhere pre-login, and no separate password-reset flow either,
since a successful emailed-code sign-in already re-proves identity just as
well (see auth_code.py's docstring).

Hashing: argon2id via argon2-cffi's high-level PasswordHasher, using its own
sensible defaults -- this is a real, long-lived credential (unlike the
6-digit code), so it gets a real, memory-hard KDF rather than the code's
lighter HMAC treatment.

Breached-password screening (addendum flagged this as "Code's call at build
time"): decided to SKIP an external check (e.g. the HaveIBeenPwned
k-anonymity range API) for v1 -- it would be this app's first-ever outbound
call to a third-party security API, adding a new network dependency and a
privacy question (even k-anonymity still sends a hash prefix of a parish
volunteer's password to an outside service) for a user population that is
mostly using this as a fallback, not their primary security-sensitive
credential. Enforces a plain local minimum instead: at least 10 characters,
and not equal to the account's own email (a common accidental weak choice).
Flagged here, in the addendum, and in CLAUDE.md as a deliberate v1 scope cut,
not an oversight -- worth revisiting if this password flavor sees real
adoption.

Lockout: app_users.failed_password_attempts / password_locked_until (added by
migrations/022_login_code_password.sql). 5 consecutive failures locks the
account for 15 minutes; any successful verify resets the counter to 0.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

import db

_MIN_PASSWORD_LENGTH = 10
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_MINUTES = 15

_hasher = PasswordHasher()


class WeakPasswordError(ValueError):
    """Raised by set_password() when the candidate password fails the local
    minimum checks -- never raised for a reason worth telling an attacker,
    only shown back to the account's own owner on their own set/change form."""


def set_password(user_id: int, email: str, new_password: str) -> None:
    """Set or change the password for an already-authenticated user_id.
    Caller (main.py) must have already confirmed request.session['user_id']
    == user_id -- this function does not itself check who's asking, matching
    db.py's other write helpers, which trust their caller's gating."""
    if len(new_password) < _MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"Password must be at least {_MIN_PASSWORD_LENGTH} characters.")
    if new_password.strip().lower() == email.strip().lower():
        raise WeakPasswordError("Password can't be the same as your email address.")

    password_hash = _hasher.hash(new_password)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.app_users SET password_hash = %s, password_set_at = NOW(), "
                "failed_password_attempts = 0, password_locked_until = NULL WHERE id = %s",
                (password_hash, user_id),
            )


def has_password(email: str) -> bool:
    """Whether email's account already has a password set -- used by the
    login-page fallback branch to decide whether to show a "sign in with your
    password" option at all (addendum: "shown once a password exists")."""
    row = db.query_one(
        "SELECT password_hash FROM checkreq.app_users WHERE email = %s",
        (email.strip().lower(),),
    )
    return bool(row and row["password_hash"])


def verify_password(email: str, password: str) -> bool:
    """Check password against email's stored hash. Returns False for any
    failure reason (no such user, no password set, locked out, wrong
    password) -- the caller shows one generic error either way, same
    anti-enumeration posture as the rest of this login system. Tracks
    failed_password_attempts / password_locked_until; a successful verify
    resets the counter."""
    email = email.strip().lower()
    row = db.query_one(
        "SELECT id, password_hash, failed_password_attempts, password_locked_until "
        "FROM checkreq.app_users WHERE email = %s",
        (email,),
    )
    if not row or not row["password_hash"]:
        return False

    if row["password_locked_until"] and row["password_locked_until"] > datetime.now(timezone.utc):
        return False

    try:
        _hasher.verify(row["password_hash"], password)
    except VerifyMismatchError:
        _record_failed_attempt(row["id"], row["failed_password_attempts"])
        return False
    except Exception:
        _record_failed_attempt(row["id"], row["failed_password_attempts"])
        return False

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.app_users SET failed_password_attempts = 0, "
                "password_locked_until = NULL WHERE id = %s",
                (row["id"],),
            )
    return True


def _record_failed_attempt(user_id: int, prior_failures: int) -> None:
    new_count = prior_failures + 1
    lock_until = None
    if new_count >= _MAX_FAILED_ATTEMPTS:
        lock_until = datetime.now(timezone.utc) + timedelta(minutes=_LOCKOUT_MINUTES)
        new_count = 0  # lockout window itself is the deterrent; reset the counter once it fires
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE checkreq.app_users SET failed_password_attempts = %s, "
                "password_locked_until = %s WHERE id = %s",
                (new_count, lock_until, user_id),
            )
