"""Log every error response a Beacon user sees, so they can be reviewed.

2026-09-24 (Jay): "make sure that all of these black screen error messages
are caught in the logs so they can be reviewed and mitigated." Many routes
answer a refused/failed action with a bare JSONResponse ({"error": "..."}),
which the browser shows as raw text on a dark page and which previously left
no trace beyond Cloud Run's one-line request log (status code only, no
message, no user).

This pure-ASGI middleware watches every response. For any status >= 400 it
writes ONE structured JSON line to stdout. Cloud Run turns that into a
jsonPayload entry with the right severity, so the whole set can be pulled
with:

    gcloud logging read 'resource.labels.service_name="checkreq-website-dev"
      AND jsonPayload.event="beacon_error_response"' --project=cfm-qbo-mcp

Each line carries: status, method, path, query, the signed-in user id and any
impersonated user id (from the session), and the response's own error text
when the body is JSON or plain text (first 500 chars). HTML error pages get
the status only. An unhandled exception is logged with its traceback, then
re-raised unchanged so Starlette's own 500 handling still runs.

Noise rule: a 404 from an anonymous visitor is skipped (scanner traffic --
about 500 a week in prod). A 404 for a signed-in user IS logged, since that
is a real person hitting a dead link.

Registered BEFORE SessionMiddleware in main.py, so it runs inside it and
scope["session"] is already populated.
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone

_MAX_BODY_CAPTURE = 4096
_MAX_MESSAGE_CHARS = 500
_SKIP_PREFIXES = ("/static/", "/health")


def _emit(record: dict) -> None:
    try:
        print(json.dumps(record, default=str), file=sys.stdout, flush=True)
    except Exception:
        pass  # logging must never break a response


def _extract_message(content_type: str, body: bytes) -> str | None:
    if not body:
        return None
    text = body[:_MAX_BODY_CAPTURE].decode("utf-8", errors="replace")
    if "application/json" in content_type:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                for key in ("error", "detail", "message"):
                    if data.get(key):
                        return str(data[key])[:_MAX_MESSAGE_CHARS]
        except ValueError:
            pass
        return text[:_MAX_MESSAGE_CHARS]
    if "text/plain" in content_type:
        return text[:_MAX_MESSAGE_CHARS]
    return None


class ErrorResponseLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").startswith(_SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return

        state = {"status": 200, "content_type": "", "body": b""}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                for name, value in message.get("headers", []):
                    if name.lower() == b"content-type":
                        state["content_type"] = value.decode("latin-1").lower()
            elif message["type"] == "http.response.body" and state["status"] >= 400:
                if len(state["body"]) < _MAX_BODY_CAPTURE:
                    state["body"] += message.get("body", b"")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            self._log(scope, 500, f"Unhandled {type(exc).__name__}: {exc}",
                      traceback.format_exc())
            raise

        status = state["status"]
        if status < 400:
            return
        session = scope.get("session") or {}
        if status == 404 and not session.get("user_id"):
            return
        self._log(scope, status, _extract_message(state["content_type"], state["body"]))

    @staticmethod
    def _log(scope, status: int, message: str | None, tb: str | None = None) -> None:
        session = scope.get("session") or {}
        record = {
            "severity": "ERROR" if status >= 500 else "WARNING",
            "event": "beacon_error_response",
            "status": status,
            "method": scope.get("method"),
            "path": scope.get("path"),
            "query": scope.get("query_string", b"").decode("latin-1")[:300] or None,
            "user_id": session.get("user_id"),
            "impersonating_user_id": session.get("impersonating_user_id"),
            "current_org_id": session.get("current_org_id"),
            "message": message,
            "logged_at": datetime.now(timezone.utc).isoformat(),
        }
        if tb:
            record["traceback"] = tb[-4000:]
        _emit(record)
