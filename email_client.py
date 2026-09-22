"""
email_client.py — thin wrapper around 26-122 Cloud Email Server's REST
endpoint (POST /api/send-email), used by the New Vendor Onboarding flow's
W-9 request email (New Vendor Onboarding Plan.md, Section 4: "Reuses
26-122 Cloud Email Server's existing send_email MCP tool -- no new email
infrastructure"). This module is the REST-caller equivalent of that MCP
tool -- main.py is a plain FastAPI backend, not an agent, so it calls the
REST endpoint directly rather than going through an MCP client.

Auth: X-API-Key header, GCP Secret Manager secret `email-mcp-api-key`
(project cfm-qbo-mcp, same secret 26-122's own README documents for REST
callers). Same auth pattern this project already uses for SharePoint
(sharepoint_client.py's Secret Manager read).

Fails soft by design: every function here returns a dict (never raises) --
a W-9 email failure must not crash the vendor-approval action that
triggers it. The caller in main.py surfaces whatever comes back
({"status": "sent"} or {"error": "..."}) so the approver can see whether it
actually sent, matching this project's existing archive_warning pattern for
recoverable-but-visible failures.
"""
from __future__ import annotations

import os

import requests

import app_settings

EMAIL_SERVER_URL = os.environ.get(
    "EMAIL_SERVER_URL", "https://email-mcp-server-xltaug3m6q-ue.a.run.app"
).rstrip("/")

_SECRET_PROJECT = os.environ.get("FIRESTORE_PROJECT", "cfm-qbo-mcp")
_cached_api_key: str | None = None

# Dev/Prod Split Plan.md (2026-07-31), Decision 5: same BEACON_ENV flag
# main.py reads -- defaults to "dev" so a misconfigured deploy fails safe
# into "still enforces the prod lock below is a no-op, harmless" rather than
# silently behaving like production with no lock at all.
_BEACON_ENV = os.environ.get("BEACON_ENV", "dev")


def _apply_test_mode(to: str, subject: str) -> tuple[str, str] | None:
    """Test Mode (Jay, 2026-07-28), HARD-LOCKED on non-prod as of M17
    (Security Assessment 2026-09-19, direction: "dev never emails real
    users, only the test address; prod sends for real").

    Dev/Prod Split Plan.md (2026-07-31), Decision 5 already made this a
    real code-level lock in PRODUCTION -- checked first, below, so a stale
    'on' value can never redirect a real production email regardless of
    what the database says. What M17 closes is the OTHER direction: the
    original design still let a toggle (checkreq.app_settings
    'email_test_mode') decide whether DEV redirected at all -- if that
    setting were ever 'false' on dev (its own default value, in fact, until
    someone explicitly turns it on), dev would silently send real email to
    real recipients. Dev must NEVER be able to do that, full stop, so the
    toggle is no longer consulted for the redirect DECISION on dev at all
    -- only whether a real production send happens. On dev, this function
    unconditionally redirects to the configured test address; if none is
    configured, it SUPPRESSES the send entirely (returns None) rather than
    ever letting an unconfigured dev fall through to a real recipient.

    Returns None to mean "do not send this email at all" -- callers
    (send_email(), below) must check for this."""
    if _BEACON_ENV == "prod":
        return to, subject
    test_address = app_settings.get_setting("email_test_mode_address")
    if not test_address:
        return None
    return test_address, f"[DEV — would have gone to: {to}] {subject}"


def _get_api_key() -> str:
    global _cached_api_key
    env = os.environ.get("EMAIL_MCP_API_KEY", "").strip()
    if env:
        return env
    if _cached_api_key is None:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{_SECRET_PROJECT}/secrets/email-mcp-api-key/versions/latest"
        _cached_api_key = client.access_secret_version(name=name).payload.data.decode("utf-8").strip()
    return _cached_api_key


def send_email(
    to: str,
    subject: str,
    body_html: str = "",
    body_text: str = "",
    sender: str = "",
    attachments: list | None = None,
    timeout: int = 30,
) -> dict:
    """POST to 26-122's /api/send-email. Returns the parsed JSON response
    ({"status": "sent"} on success, {"error": "..."} otherwise) -- never
    raises. attachments: optional list of
    {"name", "content_type", "content_base64"} dicts, same shape as
    26-122's own send_email MCP tool (3MB/file, 9MB combined cap enforced
    server-side)."""
    routed = _apply_test_mode(to, subject)
    if routed is None:
        # M17: dev has no test address configured -- suppress rather than
        # ever fall through to the real recipient. Same shape as a real
        # failure ({"error": ...}) so every existing caller's "did it send"
        # check already handles this correctly with no changes needed.
        return {"error": "Suppressed: dev environment has no email_test_mode_address configured.", "status": "suppressed"}
    to, subject = routed
    try:
        resp = requests.post(
            f"{EMAIL_SERVER_URL}/api/send-email",
            headers={"X-API-Key": _get_api_key()},
            json={
                "to": to,
                "subject": subject,
                "body_html": body_html,
                "body_text": body_text,
                "sender": sender,
                "attachments": attachments or None,
            },
            timeout=timeout,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if not resp.ok and "error" not in data:
            data["error"] = f"HTTP {resp.status_code}: {resp.text[:300]}"
        return data
    except Exception as exc:
        return {"error": str(exc), "status": "failed"}
