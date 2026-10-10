"""
donor_portal_login.py -- Beacon Donor Management: the Parishioner Self-Service sign-in and session.

A parishioner is NOT a Beacon user. They are never an app_users row, hold no donor role and no capability, and never get a staff
session. A PARISHIONER LOGIN is an explicit record (donor.parishioner_login) that staff enable for one person at one parish from the
person's System tab (donor_portal_admin), naming the active profile email the code goes to. Nobody is signed in by email match alone.
They sign in with a six-digit code emailed to THAT address, and what they get is a separate, narrow session that carries one person and
one parish and nothing else. See "Donor Management - Parishioner Self-Service Plan.md" (section 2 and the Build addendum).

  request_code(email, ip)     the first step. ALWAYS does the same work and gives the same answer, whether the typed address
                              matched anyone or not (no enumeration): the same queries run, a challenge row is stored either way,
                              and the code is emailed (by the caller, after the response) only when the address is the sign-in email
                              of an ENABLED parishioner login at a parish that has turned the portal on.
  verify_code(token, code)    the second step. Attempts are counted BEFORE the code is compared, in one atomic statement, so a code
                              can be tried at most five times even when requests arrive together.
  pending_choices / choose    when one address reaches several people (spouses sharing a mailbox) or one person at two parishes
  current(token)              the session behind every request. The row is read and checked EVERY time: switching the portal off,
                              staff disabling the login, the sign-in email leaving the profile, an archived or deceased person,
                              a person who turns out to be a minor, the 30-minute idle clock, the 2-hour absolute clock and a
                              sign-out all take effect on the very next request.
  revoke_sessions(person, parish, reason)   end every live session of one person at once (staff disable or "sign out everywhere")
  sign_out(token)             revokes the row.

Secrets: the typed address is stored only as a keyed hash (HMAC with SESSION_SECRET, the key auth_code.py also uses), the code only
as a keyed hash, the cookie token only as a keyed hash. Nothing here puts an address, a code, a name or an amount in a log line.

The person id and parish id of a session come from the session ROW, never from the browser. The one browser-sent value in this
module is the index of a choice after the code was verified, and it is only ever an index into a list the server stored itself.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets as pysecrets
from datetime import timedelta

import db
import email_client
from donor_core import is_minor

CODE_TTL_MINUTES = 10
MAX_VERIFY_ATTEMPTS = 5
MAX_CODES_PER_ADDRESS = 3
ADDRESS_WINDOW_MINUTES = 15
MAX_REQUESTS_PER_IP = 10
IP_WINDOW_MINUTES = 15
MAX_FAILED_CODES_PER_IP = 20
CHOICE_TTL_MINUTES = 10
SESSION_IDLE_MINUTES = 30
SESSION_ABSOLUTE_MINUTES = 120

SESSION_KEY = "portal_token"          # the signed Starlette session cookie key for the parishioner session (never user_id)
CHALLENGE_KEY = "portal_challenge"

_HMAC_KEY = os.environ.get("SESSION_SECRET", "dev-only-not-secure").encode("utf-8")
_SENDER_EMAIL = os.environ.get("W9_SENDER_EMAIL", "businessoffice@episcopalmaryland.org")


# ── keyed hashes ────────────────────────────────────────────────────────────────────────────────
def _hmac(purpose: str, value: str) -> str:
    return hmac.new(_HMAC_KEY, f"{purpose}|{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def email_key(email: str) -> str:
    """The stored stand-in for a typed address: a keyed hash of the lower-cased text. The address itself is never stored."""
    return _hmac("portal-email", (email or "").strip().lower())


def _token_hash(raw: str) -> str:
    return _hmac("portal-token", raw or "")


def _code_hash(raw_token: str, code: str) -> str:
    return _hmac("portal-code", f"{raw_token}|{code}")


def client_ip(request) -> str:
    """The same rule main._client_ip uses: the LAST X-Forwarded-For entry (the one Google's front end appends and no client can
    forge), else the socket address. Kept here so this module never imports main."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[-1].strip()[:64] or "unknown"
    return ((request.client.host if request.client else "") or "unknown")[:64]


# ── seams (replaced by the tests: the synthetic parishes have no portal.parishes row) ───────────
def parish_lookup(parish_id: int) -> dict | None:
    """{id, name, is_active} for the parish, from the existing registry (read only). None when there is no such row."""
    return db.query_one("SELECT id, name, is_active FROM portal.parishes WHERE id = %s", (parish_id,))


# ── eligibility: who may sign in at all ─────────────────────────────────────────────────────────
_ELIGIBLE_SELECT = (
    "SELECT pc.person_id, pc.parish_id, p.birth_date, p.deceased_date, hm.position, l.login_email "
    "  FROM donor.parish_connection pc "
    "  JOIN donor.person p ON p.id = pc.person_id "
    "  JOIN donor.parishioner_login l ON l.person_id = pc.person_id AND l.parish_id = pc.parish_id AND l.is_enabled "
    "  JOIN donor.parish_settings s ON s.parish_id = pc.parish_id AND s.portal_enabled AND s.people_enabled "
    "  LEFT JOIN donor.household_member hm ON hm.person_id = p.id AND hm.left_at IS NULL "
    " WHERE pc.archived_at IS NULL AND p.record_type = 'person' AND NOT p.is_placeholder "
    "   AND p.archived_at IS NULL AND p.deceased_date IS NULL "
    "   AND EXISTS (SELECT 1 FROM donor.person_contact c WHERE c.person_id = p.id AND c.kind = 'email' "
    "               AND c.archived_at IS NULL AND LOWER(c.value) = l.login_email) "
)


def _not_minor(row: dict) -> bool:
    return not is_minor(row["birth_date"], household_position=row["position"], deceased_date=row["deceased_date"])


def _parish_ok(parish_id: int) -> bool:
    p = parish_lookup(parish_id)
    return p is None or bool(p.get("is_active", True))            # no registry row (a synthetic test parish) is not a reason to refuse


def find_candidates(email: str) -> list[list[int]]:
    """[[person_id, parish_id], ...] for every person who has an ENABLED parishioner login whose sign-in email is this address and who
    may sign in: that email is still an active email on their profile, they are a person (not an organization or placeholder), not
    archived, not deceased, not under 18, connected to the parish, at a parish whose portal is on. Nobody is found by email match
    alone: without an enabled login row nothing is found. The query runs for every request, matched or not."""
    rows = db.query(_ELIGIBLE_SELECT + "   AND l.login_email = %s ORDER BY pc.parish_id, pc.person_id", ((email or "").strip().lower(),))
    return [[r["person_id"], r["parish_id"]] for r in rows if _not_minor(r) and _parish_ok(r["parish_id"])]


def still_eligible(person_id: int, parish_id: int) -> bool:
    """The same rules for one person at one parish (the login is still enabled and its email still active included), asked again on
    every request of a session."""
    rows = db.query(_ELIGIBLE_SELECT + "   AND pc.person_id = %s AND pc.parish_id = %s", (person_id, parish_id))
    return bool(rows) and _not_minor(rows[0]) and _parish_ok(parish_id)


def revoke_sessions(person_id: int, parish_id: int, reason: str, ip: str | None = None) -> int:
    """End EVERY live parishioner session of one person at one parish at once (staff disabled the login, or signed them out everywhere).
    Returns how many were ended. The rows are kept."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE donor.parishioner_session SET revoked_at = NOW(), revoke_reason = %s "
                        "WHERE person_id = %s AND parish_id = %s AND revoked_at IS NULL RETURNING id", (reason, person_id, parish_id))
            n = len(cur.fetchall())
    if n:
        log_event("session_ended", person_id=person_id, parish_id=parish_id, ip=ip, detail=reason)
    return n


# ── audit ───────────────────────────────────────────────────────────────────────────────────────
def log_event(kind: str, *, email_key_value: str | None = None, person_id: int | None = None, parish_id: int | None = None,
              ip: str | None = None, detail: str | None = None) -> None:
    """Every request, throttle, failure, sign-in and sign-out: who (when known), when, from where. Ids only, never an address,
    code or name. Never raises: an audit hiccup must not become a sign-in error."""
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO donor.parishioner_signin_event (kind, email_key, person_id, parish_id, ip, detail) "
                            "VALUES (%s,%s,%s,%s,%s,%s)", (kind, email_key_value, person_id, parish_id, ip, (detail or "")[:200] or None))
    except Exception:
        pass


# ── step 1: ask for a code ──────────────────────────────────────────────────────────────────────
def request_code(email: str, ip: str) -> dict:
    """Store a challenge and decide whether a code goes out. Returns {"token": raw challenge token for the signed cookie,
    "send": None or (address, code) for the caller to email AFTER the response is sent}. The same queries run, one challenge row
    is written and one event is logged whether or not the address matched, so nothing about the page or the timing tells."""
    typed = (email or "").strip().lower()[:254]
    ekey = email_key(typed)
    raw = pysecrets.token_urlsafe(32)
    code = f"{pysecrets.randbelow(1_000_000):06d}"
    candidates = find_candidates(typed)                      # the same query for every request, however the text looks
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM donor.parishioner_challenge WHERE email_key = %s AND NOT throttled "
                        "AND created_at > NOW() - make_interval(mins => %s)", (ekey, ADDRESS_WINDOW_MINUTES))
            per_address = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM donor.parishioner_challenge WHERE ip = %s "
                        "AND created_at > NOW() - make_interval(mins => %s)", (ip, IP_WINDOW_MINUTES))
            per_ip = cur.fetchone()["n"]
            throttled = per_address >= MAX_CODES_PER_ADDRESS or per_ip >= MAX_REQUESTS_PER_IP
            send = bool(candidates) and not throttled
            parish_ids = sorted({c[1] for c in candidates})
            cur.execute(
                "INSERT INTO donor.parishioner_challenge (token_hash, email_key, code_hash, candidates, parish_ids, sent, throttled, "
                "expires_at, ip) VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,NOW() + make_interval(mins => %s),%s)",
                (_token_hash(raw), ekey, _code_hash(raw, code), json.dumps(candidates), parish_ids, send, throttled,
                 CODE_TTL_MINUTES, ip))
    log_event("code_throttled" if throttled else "code_requested", email_key_value=ekey, ip=ip)
    return {"token": raw, "send": (typed, code) if send else None}


def send_code_email(to: str, code: str) -> None:
    """Email the code. Called as a background task after the response. Fails soft: the page already answered."""
    try:
        email_client.send_email(
            to=to, subject=f"Your sign-in code: {code}",
            body_text=(f"Your sign-in code is {code}\n\nThis code expires in {CODE_TTL_MINUTES} minutes and can only be used once.\n\n"
                       "If you did not ask for it you can safely ignore this email. Nobody can see your giving without this code."),
            body_html=(f"<p>Your sign-in code is:</p><p style=\"font-size:28px;font-weight:700;letter-spacing:4px;\">{code}</p>"
                       f"<p>This code expires in {CODE_TTL_MINUTES} minutes and can only be used once.</p>"
                       "<p style=\"color:#666;font-size:13px;\">If you did not ask for it you can safely ignore this email. "
                       "Nobody can see your giving without this code.</p>"),
            sender=_SENDER_EMAIL)
    except Exception:
        pass


# ── step 2: check the code ──────────────────────────────────────────────────────────────────────
def _related_group(cur, person_ids: list[int]) -> bool:
    """One person, or exactly two people who are each other's current spouse. Anything else (three people on one address, two
    people who are not spouses) is a group the portal will not sign anyone in from."""
    if len(person_ids) == 1:
        return True
    if len(person_ids) != 2:
        return False
    a, b = person_ids
    cur.execute("SELECT 1 AS x FROM donor.spouse_link WHERE person_id = %s AND spouse_id = %s AND ended_at IS NULL", (a, b))
    ab = cur.fetchone() is not None
    cur.execute("SELECT 1 AS x FROM donor.spouse_link WHERE person_id = %s AND spouse_id = %s AND ended_at IS NULL", (b, a))
    return ab and cur.fetchone() is not None


def _options_for(cur, candidates: list) -> list[dict]:
    """The people (and parishes) the verified person may continue as. Per parish, the matched people must be one person or two
    spouses. Each is checked again for eligibility now, since up to ten minutes passed."""
    by_parish: dict = {}
    for pid, par in candidates:
        by_parish.setdefault(par, []).append(pid)
    out = []
    for par in sorted(by_parish):
        pids = sorted(by_parish[par])
        if not _related_group(cur, pids):
            continue
        for pid in pids:
            if still_eligible(pid, par):
                out.append({"person_id": pid, "parish_id": par})
    return out


def _fail_count_by_ip(ip: str) -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM donor.parishioner_signin_event WHERE ip = %s AND kind = 'code_failed' "
                       "AND created_at > NOW() - make_interval(mins => %s)", (ip, IP_WINDOW_MINUTES))
    return row["n"] if row else 0


def verify_code(raw_token: str, code: str, ip: str) -> dict:
    """{"status": "signed_in", "session_token": ..., "person_id", "parish_id"} when exactly one person can continue,
    {"status": "choose"} when there is a choice to make, {"status": "blocked"} when the code was right but nobody can be signed in
    (the people on that address are unrelated, or no longer eligible), {"status": "bad"} for everything else (one answer for a
    wrong code, an old one, a used one, one with no tries left, an address that matched nobody, a throttled IP)."""
    if not raw_token or not code or len(code) != 6 or not code.isdigit():
        log_event("code_failed", ip=ip, detail="malformed")
        return {"status": "bad"}
    if _fail_count_by_ip(ip) >= MAX_FAILED_CODES_PER_IP:
        log_event("code_failed", ip=ip, detail="ip limit")
        return {"status": "bad"}
    th = _token_hash(raw_token)
    with db.connect() as conn:
        with conn.cursor() as cur:
            # The attempts are checked and counted in ONE statement, before anything is compared. A row with no tries left, an
            # old one, a used one, or one already verified returns nothing here, so the code is never compared at all.
            cur.execute(
                "UPDATE donor.parishioner_challenge SET attempts = attempts + 1 WHERE token_hash = %s AND verified_at IS NULL "
                "AND consumed_at IS NULL AND expires_at > NOW() AND attempts < %s "
                "RETURNING id, email_key, code_hash, candidates, sent", (th, MAX_VERIFY_ATTEMPTS))
            row = cur.fetchone()
            if not row:
                result, failed = {"status": "bad"}, "no live challenge"
            elif not hmac.compare_digest(row["code_hash"], _code_hash(raw_token, code)):
                result, failed = {"status": "bad"}, "wrong code"
            else:
                result, failed = None, None
            if result is None:
                cur.execute("UPDATE donor.parishioner_challenge SET verified_at = NOW() WHERE id = %s AND verified_at IS NULL RETURNING id", (row["id"],))
                if not cur.fetchone():                                  # a second request with the right code arrived together
                    result = {"status": "bad"}
                else:
                    options = _options_for(cur, row["candidates"] or [])
                    cur.execute("UPDATE donor.parishioner_challenge SET options = %s::jsonb WHERE id = %s", (json.dumps(options), row["id"]))
                    if not options:
                        cur.execute("UPDATE donor.parishioner_challenge SET consumed_at = NOW() WHERE id = %s", (row["id"],))
                        result = {"status": "blocked", "email_key": row["email_key"]}
                    elif len(options) == 1:
                        result = _open_session(cur, row["id"], options[0]["person_id"], options[0]["parish_id"], ip)
                        result["email_key"] = row["email_key"]
                    else:
                        result = {"status": "choose", "email_key": row["email_key"]}
    if result["status"] == "bad":
        log_event("code_failed", ip=ip, detail=failed or "not accepted")
    elif result["status"] == "blocked":
        log_event("blocked_unrelated", email_key_value=result.get("email_key"), ip=ip)
    elif result["status"] == "signed_in":
        log_event("signed_in", email_key_value=result.get("email_key"), person_id=result["person_id"], parish_id=result["parish_id"], ip=ip)
    else:
        log_event("code_verified", email_key_value=result.get("email_key"), ip=ip, detail="choice")
    return result


def _open_session(cur, challenge_id: int, person_id: int, parish_id: int, ip: str) -> dict:
    raw = pysecrets.token_urlsafe(32)
    cur.execute(
        "INSERT INTO donor.parishioner_session (token_hash, person_id, parish_id, challenge_id, expires_at, ip) "
        "VALUES (%s,%s,%s,%s,NOW() + make_interval(mins => %s),%s)",
        (_token_hash(raw), person_id, parish_id, challenge_id, SESSION_ABSOLUTE_MINUTES, ip))
    cur.execute("UPDATE donor.parishioner_challenge SET consumed_at = NOW() WHERE id = %s", (challenge_id,))
    cur.execute("UPDATE donor.parishioner_login SET last_signin_at = NOW() WHERE person_id = %s AND parish_id = %s", (person_id, parish_id))
    return {"status": "signed_in", "session_token": raw, "person_id": person_id, "parish_id": parish_id}


def _person_name(row: dict) -> str:
    first = (row.get("goes_by") or row.get("first_name") or "").strip()
    return " ".join(x for x in (first, (row.get("last_name") or "").strip()) if x)


def pending_choices(raw_token: str) -> list[dict] | None:
    """The choices after a verified code: [{label}], or None when there is nothing to choose (no challenge, not verified, already used,
    or too long ago). The labels name the person and the parish. The index of the one picked is all the browser sends back."""
    if not raw_token:
        return None
    row = db.query_one(
        "SELECT options FROM donor.parishioner_challenge WHERE token_hash = %s AND verified_at IS NOT NULL AND consumed_at IS NULL "
        "AND verified_at > NOW() - make_interval(mins => %s)", (_token_hash(raw_token), CHOICE_TTL_MINUTES))
    if not row:
        return None
    out = []
    for o in row["options"] or []:
        person = db.query_one("SELECT first_name, last_name, goes_by FROM donor.person WHERE id = %s", (o["person_id"],)) or {}
        parish = parish_lookup(o["parish_id"]) or {}
        out.append({"label": f"{_person_name(person) or 'Person'} at {parish.get('name') or 'this parish'}"})
    return out


def choose(raw_token: str, index, ip: str) -> dict | None:
    """Open the session for one of the stored choices. None unless the index is one of them and the person is still eligible."""
    try:
        i = int(str(index).strip())
    except (TypeError, ValueError):
        return None
    if not raw_token:
        return None
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email_key, options FROM donor.parishioner_challenge WHERE token_hash = %s AND verified_at IS NOT NULL "
                "AND consumed_at IS NULL AND verified_at > NOW() - make_interval(mins => %s) FOR UPDATE",
                (_token_hash(raw_token), CHOICE_TTL_MINUTES))
            row = cur.fetchone()
            opts = (row or {}).get("options") or []
            if not row or i < 0 or i >= len(opts):
                return None
            o = opts[i]
            if not still_eligible(o["person_id"], o["parish_id"]):
                return None
            res = _open_session(cur, row["id"], o["person_id"], o["parish_id"], ip)
    log_event("signed_in", email_key_value=row["email_key"], person_id=res["person_id"], parish_id=res["parish_id"], ip=ip, detail="choice")
    return res


# ── the session behind every request ────────────────────────────────────────────────────────────
def current(raw_token: str | None, ip: str = "") -> dict | None:
    """The signed-in parishioner for this token, or None. Every request reads and checks the row: not revoked, inside the idle
    and absolute clocks, and the person and parish still eligible. Anything that fails revokes the row for good."""
    if not raw_token:
        return None
    reason = None
    ps = None
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT s.id, s.person_id, s.parish_id, s.last_seen_at, s.expires_at, NOW() AS now_ts "
                        "FROM donor.parishioner_session s WHERE s.token_hash = %s AND s.revoked_at IS NULL", (_token_hash(raw_token),))
            s = cur.fetchone()
            if not s:
                return None
            now = s["now_ts"]
            if now >= s["expires_at"]:
                reason = "expired_absolute"
            elif now - s["last_seen_at"] > timedelta(minutes=SESSION_IDLE_MINUTES):
                reason = "expired_idle"
            elif not still_eligible(s["person_id"], s["parish_id"]):
                reason = "ineligible"
            if reason:
                cur.execute("UPDATE donor.parishioner_session SET revoked_at = NOW(), revoke_reason = %s WHERE id = %s", (reason, s["id"]))
            else:
                cur.execute("UPDATE donor.parishioner_session SET last_seen_at = NOW() WHERE id = %s", (s["id"],))
                cur.execute("SELECT first_name, last_name, goes_by FROM donor.person WHERE id = %s", (s["person_id"],))
                person = cur.fetchone() or {}
                ps = {"session_id": s["id"], "person_id": s["person_id"], "parish_id": s["parish_id"], "person_name": _person_name(person)}
    if reason:
        log_event("session_ended", person_id=s["person_id"], parish_id=s["parish_id"], ip=ip, detail=reason)
        return None
    parish = parish_lookup(ps["parish_id"]) or {}
    ps["parish_name"] = parish.get("name") or "Your parish"
    return ps


def sign_out(raw_token: str | None, ip: str = "", reason: str = "signed_out") -> None:
    """Revoke the session row. After this the cookie's token opens nothing, even if someone kept a copy of it."""
    if not raw_token:
        return
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE donor.parishioner_session SET revoked_at = NOW(), revoke_reason = %s "
                        "WHERE token_hash = %s AND revoked_at IS NULL RETURNING person_id, parish_id", (reason, _token_hash(raw_token)))
            row = cur.fetchone()
    if row:
        log_event("signed_out" if reason == "signed_out" else "session_ended", person_id=row["person_id"], parish_id=row["parish_id"], ip=ip, detail=reason)
