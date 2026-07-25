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

EMAIL_SERVER_URL = os.environ.get(
    "EMAIL_SERVER_URL", "https://email-mcp-server-xltaug3m6q-ue.a.run.app"
).rstrip("/")

_SECRET_PROJECT = os.environ.get("FIRESTORE_PROJECT", "cfm-qbo-mcp")
_cached_api_key: str | None = None


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
