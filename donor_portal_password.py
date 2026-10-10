"""
donor_portal_password.py -- Beacon Donor Management: a member's own password, and changing the sign-in email (Jay, 2026-10-10, Phase 5).

The sign-in the plan settled on: the first time a member comes in with an emailed six-digit code and is offered a password ("Not now" is
fine). From then on, email address and password. The code stays one tap away ("Email me a code instead") and is also the way back in when
a password is forgotten, so nobody is locked out and there is no separate reset flow.

  password_signin(email, password, ip)   email + password. ALWAYS the same work and the same answer for an address that matched, one with no
                                         password, a wrong password and a locked login (a dummy hash is checked when there is nothing real
                                         to check). 5 wrong tries lock THAT LOGIN's password for 15 minutes (the emailed code still works).
                                         A right password opens the same narrow session a code does (donor_portal_login._open_session), or
                                         the same "choose" step when it matches two spouses or two parishes.
  set_password / has_password            set or change the password of the signed-in login. Other live sessions of that person end.
  email_change_request / _confirm        change the address the sign-in goes to: a code is emailed to the NEW address, and only a right code
                                         moves the login. The old address is told. Other sessions end. The old address stays on the profile
                                         as an ordinary email (a member can then remove it themselves).

Passwords are hashed with argon2id (argon2-cffi defaults), never stored or logged in clear. Nothing here logs an address, a password or a code:
only ids and short reasons. The person and the parish always come from the session row, never from the browser.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets as pysecrets
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

import db
import donor_portal as PP
import donor_portal_login as L
from donor_core import InvalidInput, clean_email, log_change, tx

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200
MAX_FAILED = 5
LOCK_MINUTES = 15
MAX_PASSWORD_FAILS_PER_IP = 20
# The password sign-in answer takes at least this long whatever happened, so the time does not tell a member from a stranger. Tests set it to 0.
MIN_PASSWORD_SECONDS = 1.0
CHANGE_TTL_MINUTES = 10
MAX_CHANGE_REQUESTS_PER_HOUR = 3
MAX_CHANGE_ATTEMPTS = 5
FRESH_PROOF_SECONDS = 900       # after an emailed code, a password may be set or changed without the old one for this long
BAD_SIGNIN = "That email address and password did not match. You can also have a code emailed to you instead."

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("beacon-portal-timing-stand-in")


class PasswordError(ValueError):
    """A new password that does not meet the rules. The message is plain words for the person typing it."""


def validate_new_password(password: str, email: str | None = None) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(f"Your password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordError(f"Your password can be at most {MAX_PASSWORD_LENGTH} characters.")
    if email and password.strip().lower() == email.strip().lower():
        raise PasswordError("Your password can't be the same as your email address.")


# ── the password ────────────────────────────────────────────────────────────────────────────────
def has_password(person_id: int, parish_id: int) -> bool:
    row = db.query_one("SELECT password_hash FROM donor.parishioner_login WHERE person_id = %s AND parish_id = %s AND is_enabled",
                       (person_id, parish_id))
    return bool(row and row["password_hash"])


def set_password(ps: dict, new_password: str, *, keep_token: str | None = None, ip: str | None = None) -> None:
    """Set or change the password of the signed-in login. The caller has checked who is allowed to (the current password, or a fresh
    emailed code). Every OTHER live session of this person at this parish ends (the one named by keep_token continues)."""
    login = db.query_one("SELECT login_email FROM donor.parishioner_login WHERE person_id = %s AND parish_id = %s AND is_enabled",
                         (ps["person_id"], ps["parish_id"]))
    if not login:
        raise PasswordError("Your sign-in is not turned on, so a password can't be set.")
    validate_new_password(new_password, login["login_email"])
    hashed = _hasher.hash(new_password)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE donor.parishioner_login SET password_hash = %s, password_set_at = NOW(), failed_password_attempts = 0, "
                        "password_locked_until = NULL WHERE person_id = %s AND parish_id = %s AND is_enabled",
                        (hashed, ps["person_id"], ps["parish_id"]))
    _end_other_sessions(ps["person_id"], ps["parish_id"], keep_token, "password_changed")
    L.log_event("password_set", person_id=ps["person_id"], parish_id=ps["parish_id"], ip=ip)


def check_current_password(person_id: int, parish_id: int, password: str) -> bool:
    """Is this the login's current password? Counts a wrong try like a sign-in does (and refuses while the login is locked)."""
    row = db.query_one("SELECT id, password_hash, failed_password_attempts, password_locked_until FROM donor.parishioner_login "
                       "WHERE person_id = %s AND parish_id = %s AND is_enabled", (person_id, parish_id))
    if not row or not row["password_hash"]:
        return False
    return _verify_one(row, password or "")


def _locked(row: dict) -> bool:
    until = row.get("password_locked_until")
    return bool(until and until > datetime.now(timezone.utc))


def _verify_one(row: dict, password: str) -> bool:
    """Check one login's password. A locked login is refused (a stand-in hash is checked so the time is the same). A wrong password
    counts toward the lock, a right one clears the count."""
    if _locked(row):
        _burn()
        return False
    try:
        _hasher.verify(row["password_hash"], password)
        ok = True
    except VerificationError:
        ok = False
    except Exception:
        ok = False
    with db.connect() as conn:
        with conn.cursor() as cur:
            if ok:
                if row["failed_password_attempts"] or row.get("password_locked_until"):
                    cur.execute("UPDATE donor.parishioner_login SET failed_password_attempts = 0, password_locked_until = NULL WHERE id = %s", (row["id"],))
            else:
                # one atomic statement: count the failure and lock on the fifth, so two wrong tries together cannot both slip under
                cur.execute("UPDATE donor.parishioner_login SET "
                            "failed_password_attempts = CASE WHEN failed_password_attempts + 1 >= %s THEN 0 ELSE failed_password_attempts + 1 END, "
                            "password_locked_until = CASE WHEN failed_password_attempts + 1 >= %s THEN NOW() + make_interval(mins => %s) "
                            "ELSE password_locked_until END WHERE id = %s", (MAX_FAILED, MAX_FAILED, LOCK_MINUTES, row["id"]))
    return ok


def _burn() -> None:
    """Spend the time of one hash check on nothing, so a missing or locked login takes as long as a real one."""
    try:
        _hasher.verify(_DUMMY_HASH, "not-the-password")
    except Exception:
        pass


def _fails_by_ip(ip: str) -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM donor.parishioner_signin_event WHERE ip = %s AND kind = 'password_failed' "
                       "AND created_at > NOW() - make_interval(mins => %s)", (ip, L.IP_WINDOW_MINUTES))
    return row["n"] if row else 0


def password_signin(email: str, password: str, ip: str) -> dict:
    """{"status": "signed_in", "session_token", "person_id", "parish_id"}, {"status": "choose", "challenge_token"} when it matches two
    spouses or two parishes, or {"status": "bad"} (one answer for everything that did not work)."""
    typed = (email or "").strip().lower()[:254]
    ekey = L.email_key(typed)
    pw = password or ""
    if not typed or not pw or len(pw) > MAX_PASSWORD_LENGTH:
        _burn()
        L.log_event("password_failed", email_key_value=ekey, ip=ip, detail="malformed")
        return {"status": "bad"}
    if _fails_by_ip(ip) >= MAX_PASSWORD_FAILS_PER_IP:
        _burn()
        L.log_event("password_failed", email_key_value=ekey, ip=ip, detail="ip limit")
        return {"status": "bad"}
    candidates = L.find_candidates(typed)                       # the same query whoever it is
    logins = []
    for person_id, parish_id in candidates:
        row = db.query_one("SELECT id, password_hash, failed_password_attempts, password_locked_until FROM donor.parishioner_login "
                           "WHERE person_id = %s AND parish_id = %s AND is_enabled", (person_id, parish_id))
        if row and row["password_hash"]:
            logins.append(((person_id, parish_id), row))
    if not logins:
        _burn()                                                 # nothing real to check: the same time as a real check
    matched = [list(key) for key, row in logins if _verify_one(row, pw)]
    if not matched:
        L.log_event("password_failed", email_key_value=ekey, ip=ip, detail="no match")
        return {"status": "bad"}
    result = None
    with db.connect() as conn:
        with conn.cursor() as cur:
            options = L._options_for(cur, matched)
            if options:
                raw = pysecrets.token_urlsafe(32)
                cur.execute(
                    "INSERT INTO donor.parishioner_challenge (token_hash, email_key, code_hash, candidates, options, parish_ids, sent, throttled, "
                    "expires_at, verified_at, ip) VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s,FALSE,FALSE,NOW() + make_interval(mins => %s),NOW(),%s) "
                    "RETURNING id", (L._token_hash(raw), ekey, L._code_hash(raw, pysecrets.token_urlsafe(8)), json.dumps(matched),
                                     json.dumps(options), sorted({o["parish_id"] for o in options}), L.CODE_TTL_MINUTES, ip))
                cid = cur.fetchone()["id"]
                if len(options) == 1:
                    result = L._open_session(cur, cid, options[0]["person_id"], options[0]["parish_id"], ip)
                else:
                    result = {"status": "choose", "challenge_token": raw}
    if not result:
        L.log_event("password_failed", email_key_value=ekey, ip=ip, detail="not eligible")
        return {"status": "bad"}
    if result["status"] == "signed_in":
        L.log_event("signed_in", email_key_value=ekey, person_id=result["person_id"], parish_id=result["parish_id"], ip=ip, detail="password")
    return result


def _end_other_sessions(person_id: int, parish_id: int, keep_token: str | None, reason: str) -> int:
    keep = L._token_hash(keep_token) if keep_token else None
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE donor.parishioner_session SET revoked_at = NOW(), revoke_reason = %s WHERE person_id = %s AND parish_id = %s "
                        "AND revoked_at IS NULL AND (%s::text IS NULL OR token_hash <> %s) RETURNING id", (reason, person_id, parish_id, keep, keep))
            n = len(cur.fetchall())
    if n:
        L.log_event("session_ended", person_id=person_id, parish_id=parish_id, detail=reason)
    return n


# ── changing the sign-in email ──────────────────────────────────────────────────────────────────
def _change_hash(person_id: int, parish_id: int, new_email: str, code: str) -> str:
    return L._hmac("portal-email-change", f"{person_id}|{parish_id}|{new_email}|{code}")


def mask_email(address: str) -> str:
    """j***@example.org: enough to recognise it, not enough to learn it."""
    local, _, domain = (address or "").partition("@")
    if not domain:
        return "your email address"
    return f"{local[:1]}***@{domain}"


def email_change_request(ps: dict, new_email, ip: str) -> dict:
    """Start a change of the sign-in address. Raises InvalidInput for a malformed address, the same address as now, or too many tries.
    Otherwise returns {"send": (address, code)} when a code should be emailed to the new address, or {"send": None} when it must not
    be (another member already uses it). The page answers the same either way, so it cannot be used to find out who is a member."""
    new = clean_email(new_email, field="email")
    if not new:
        raise InvalidInput("Enter the new email address.", "email")
    new = new.lower()
    pid, par = ps["person_id"], ps["parish_id"]
    login = db.query_one("SELECT login_email FROM donor.parishioner_login WHERE person_id = %s AND parish_id = %s AND is_enabled", (pid, par))
    if not login:
        raise InvalidInput("Your sign-in is not turned on.", "email")
    if new == login["login_email"]:
        raise InvalidInput("That is already the email address you sign in with.", "email")
    code = f"{pysecrets.randbelow(1_000_000):06d}"
    with tx() as c:
        c.execute("SELECT COUNT(*) AS n FROM donor.parishioner_email_change WHERE person_id = %s AND parish_id = %s "
                  "AND created_at > NOW() - INTERVAL '1 hour'", (pid, par))
        if c.fetchone()["n"] >= MAX_CHANGE_REQUESTS_PER_HOUR:
            raise InvalidInput("You have asked for a code several times. Please wait a while and try again, or use Get help.", "email")
        taken = PP._email_taken_by_other(c, pid, new)
        c.execute("INSERT INTO donor.parishioner_email_change (person_id, parish_id, new_email, code_hash, expires_at) "
                  "VALUES (%s,%s,%s,%s,NOW() + make_interval(mins => %s))", (pid, par, new, _change_hash(pid, par, new, code), CHANGE_TTL_MINUTES))
    L.log_event("email_change_requested", person_id=pid, parish_id=par, ip=ip)
    return {"send": None if taken else (new, code)}


def send_change_code_email(to: str, code: str, ip: str | None = None) -> bool:
    """Email the confirmation code to the NEW address (during the request, like the sign-in code). Never raises, never logs the address or code."""
    import email_client
    try:
        resp = email_client.send_email(
            to=to, subject=f"Your code to confirm your new sign-in email: {code}",
            body_text=(f"Your confirmation code is {code}\n\nEnter it on the page to start signing in with this email address. It expires in "
                       f"{CHANGE_TTL_MINUTES} minutes and can only be used once.\n\nIf you did not ask for this you can safely ignore this email."),
            body_html=(f"<p>Your confirmation code is:</p><p style=\"font-size:28px;font-weight:700;letter-spacing:4px;\">{code}</p>"
                       f"<p>Enter it on the page to start signing in with this email address. It expires in {CHANGE_TTL_MINUTES} minutes and can only be used once.</p>"
                       "<p style=\"color:#666;font-size:13px;\">If you did not ask for this you can safely ignore this email.</p>"),
            sender=L._SENDER_EMAIL)
        ok = isinstance(resp, dict) and not resp.get("error") and resp.get("status") == "sent"
        reason = None if ok else (resp.get("error") if isinstance(resp, dict) else "the email service gave no answer")
    except Exception as exc:
        ok, reason = False, f"{type(exc).__name__}: {exc}"
    if not ok:
        reason = L._safe_reason(reason)
        print(f"[portal-signin] email-change code not sent: {reason}")
        L.log_event("email_change_email_failed", ip=ip, detail=reason)
    return ok


def _send_old_address_notice(old_email: str, new_email: str, parish_name: str) -> None:
    """Tell the OLD address that the sign-in address changed. Best effort: a failure never undoes the change."""
    import email_client
    try:
        email_client.send_email(
            to=old_email, subject="Your portal sign-in email address was changed",
            body_text=(f"The email address you use to sign in to the {parish_name} member portal was changed to {mask_email(new_email)}.\n\n"
                       "If you made this change there is nothing to do. If you did not, please contact the parish office right away."),
            body_html=(f"<p>The email address you use to sign in to the {parish_name} member portal was changed to {mask_email(new_email)}.</p>"
                       "<p>If you made this change there is nothing to do. If you did not, please contact the parish office right away.</p>"),
            sender=L._SENDER_EMAIL)
    except Exception as exc:
        print(f"[portal-signin] old-address notice not sent: {L._safe_reason(f'{type(exc).__name__}: {exc}')}")


def email_change_confirm(ps: dict, code: str, ip: str, *, keep_token: str | None = None) -> dict:
    """{"status": "changed", "old_email", "new_email"} or {"status": "bad"} (one answer for a wrong, old, used or exhausted code).
    Tries are counted BEFORE the code is compared, in one statement, so five is five even when requests arrive together."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    pid, par = ps["person_id"], ps["parish_id"]
    if len(code) != 6:
        L.log_event("email_change_failed", person_id=pid, parish_id=par, ip=ip, detail="malformed")
        return {"status": "bad"}
    result = {"status": "bad"}
    old_email = new_email = None
    with tx() as c:
        c.execute("UPDATE donor.parishioner_email_change SET attempts = attempts + 1 WHERE id = ("
                  "SELECT id FROM donor.parishioner_email_change WHERE person_id = %s AND parish_id = %s AND used_at IS NULL "
                  "AND expires_at > NOW() AND attempts < %s ORDER BY id DESC LIMIT 1 FOR UPDATE) RETURNING id, new_email, code_hash",
                  (pid, par, MAX_CHANGE_ATTEMPTS))
        row = c.fetchone()
        if row and hmac.compare_digest(row["code_hash"], _change_hash(pid, par, row["new_email"], code)):
            c.execute("SELECT id, login_email FROM donor.parishioner_login WHERE person_id = %s AND parish_id = %s AND is_enabled FOR UPDATE", (pid, par))
            login = c.fetchone()
            new_email = row["new_email"]
            if login and not PP._email_taken_by_other(c, pid, new_email) and new_email != login["login_email"]:
                old_email = login["login_email"]
                c.execute("UPDATE donor.parishioner_email_change SET used_at = NOW() WHERE id = %s AND used_at IS NULL RETURNING id", (row["id"],))
                if c.fetchone():
                    # the new address must be an active email on the profile (it is the eligibility rule), the old one stays as an ordinary email
                    c.execute("SELECT id FROM donor.person_contact WHERE person_id = %s AND kind = 'email' AND archived_at IS NULL "
                              "AND LOWER(value) = %s", (pid, new_email))
                    if not c.fetchone():
                        c.execute("INSERT INTO donor.person_contact (person_id, kind, subtype, value, digits, is_preferred, created_by_user_id) "
                                  "VALUES (%s,'email','other',%s,'',FALSE,0) RETURNING id", (pid, new_email))
                        log_change(c, PP.actor(par), "person_contact", c.fetchone()["id"], "value", None, new_email, person_id=pid,
                                   kind="create", reason=f"{PP.SELF_REASON}: new sign-in email")
                    c.execute("UPDATE donor.parishioner_login SET login_email = %s WHERE person_id = %s AND parish_id = %s", (new_email, pid, par))
                    log_change(c, PP.actor(par), "parishioner_login", login["id"], "Sign-in email", old_email, new_email, person_id=pid,
                               kind="update", scope="parish", reason=f"{PP.SELF_REASON}: sign-in email changed")
                    result = {"status": "changed", "old_email": old_email, "new_email": new_email}
    if result["status"] == "changed":
        _end_other_sessions(pid, par, keep_token, "sign_in_email_changed")
        L.log_event("email_changed", person_id=pid, parish_id=par, ip=ip)
        _send_old_address_notice(result["old_email"], result["new_email"], ps.get("parish_name") or "parish")
    else:
        L.log_event("email_change_failed", person_id=pid, parish_id=par, ip=ip, detail="not accepted")
    return result
