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


def _apply_test_mode(to: str, subject: str) -> tuple[str, str]:
    """Test Mode (Jay, 2026-07-28): when on, EVERY outgoing email -- from any
    call site, present or future -- gets redirected to one designated test
    address instead of its real recipient, with the subject prefixed to show
    who it *would* have gone to. Centralized here (the one real send_email()
    choke point) rather than at each call site in main.py, so a future email
    feature can never accidentally forget to check this.

    Fails open to "off" (sends to the real recipient unchanged) on any
    settings-read error -- a DB hiccup must never silently swallow a real
    email that was never meant to be redirected.

    Dev/Prod Split Plan.md (2026-07-31), Decision 5: Test Mode must NEVER be
    active in production, as a real code-level lock -- not just a policy to
    remember. Checked FIRST, before checkreq.app_settings is even read, so a
    stale 'on' value left over from dev testing (or someone flipping it on
    by mistake) can never redirect a real production email, regardless of
    what the database says."""
    if _BEACON_ENV == "prod":
        return to, subject
    if app_settings.get_setting("email_test_mode", "false") != "true":
        return to, subject
    test_address = app_settings.get_setting("email_test_mode_address")
    if not test_address:
        return to, subject
    return test_address, f"[TEST MODE — would have gone to: {to}] {subject}"


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
    to, subject = _apply_test_mode(to, subject)
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
