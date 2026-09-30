"""
sma_webhook.py -- receives the fully executed SMA letter from Formstack Documents.

26-129 SMA letters (plan: Tools\\SMA Letters\\sma-covenant-plan.html, step 3).
Flow proven 2026-09-26: Documents routes the Allocation Form to Formstack Sign;
the Sign delivery's "Delay other deliveries until signing complete" holds a
Webhook delivery until the last signer finishes, then Documents POSTs the
SIGNED PDF here.

    POST /webhooks/formstack-documents

Authentication: Formstack can't sign in, so the request must carry the shared
secret in the X-Beacon-Webhook-Secret header, compared in constant time
against app_settings key 'sma_webhook_secret' (set per environment). No
secret configured -> refuse everything (fail closed).

Payload (Documents webhook delivery, "Send data using JSON" on): merge_id,
file_name, file_contents (base64) and/or file_url (temporary download link,
only followed if it is a Formstack/webmerge host), fields (the merge data --
includes LetterId once the real feature exists).

This first cut only STORES the executed PDF plus a JSON sidecar in the
environment's Beacon bucket under sma-letters/executed/. Linking it to a parish
letter row and marking the parish Complete comes with migration 068 (not yet
approved). Deliberately a separate module: main.py only gains the register call.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from fastapi import Request
from fastapi.responses import JSONResponse

import app_settings
import gcs_client

_BEACON_ENV = os.environ.get("BEACON_ENV", "dev")
BUCKET = "cfm-beacon-files-prod" if _BEACON_ENV == "prod" else "cfm-beacon-files-dev"
SECRET_SETTING = "sma_webhook_secret"
HEADER = "x-beacon-webhook-secret"
MAX_BYTES = 20 * 1024 * 1024
_ALLOWED_FILE_HOSTS = ("webmerge.me", "formstack.com", "insuresign.com")


def _safe_name(name: str) -> str:
    name = os.path.basename(name or "executed.pdf")
    name = re.sub(r"[^A-Za-z0-9 ._()'-]+", "_", name).strip() or "executed.pdf"
    return name[:150]


def _authorized(request: Request) -> bool:
    expected = app_settings.get_setting(SECRET_SETTING)
    if not expected:
        return False
    supplied = request.headers.get(HEADER, "")
    return hmac.compare_digest(supplied.encode(), expected.encode())


async def _payload(request: Request) -> dict:
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        return await request.json()
    form = await request.form()
    data = {k: form.get(k) for k in form.keys()}
    if isinstance(data.get("fields"), str):
        try:
            data["fields"] = json.loads(data["fields"])
        except ValueError:
            pass
    return data


def _pdf_bytes(data: dict) -> bytes | None:
    if data.get("file_contents"):
        return base64.b64decode(data["file_contents"])
    url = data.get("file_url")
    if url:
        host = (urlparse(url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in _ALLOWED_FILE_HOSTS):
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            return r.content
    return None


def register(app) -> None:
    @app.post("/webhooks/formstack-documents")
    async def formstack_documents_webhook(request: Request):
        if not _authorized(request):
            print(json.dumps({"event": "sma_webhook_rejected", "reason": "bad or missing secret"}))
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            data = await _payload(request)
            pdf = _pdf_bytes(data)
        except Exception as exc:  # malformed body, bad base64, file_url failure
            print(json.dumps({"event": "sma_webhook_error", "error": type(exc).__name__}))
            return JSONResponse({"error": "could not read the document"}, status_code=400)
        if not pdf or not pdf.startswith(b"%PDF"):
            return JSONResponse({"error": "no PDF received"}, status_code=400)
        if len(pdf) > MAX_BYTES:
            return JSONResponse({"error": "file too large"}, status_code=413)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        merge_id = re.sub(r"[^0-9A-Za-z_-]", "", str(data.get("merge_id") or "nomerge"))[:40]
        base = f"sma-letters/executed/{stamp}_{merge_id}_{_safe_name(data.get('file_name'))}"
        if not base.lower().endswith(".pdf"):
            base += ".pdf"
        gcs_client.upload_bytes(BUCKET, base, pdf, "application/pdf")
        sidecar = {"received_at": stamp, "merge_id": data.get("merge_id"),
                   "file_name": data.get("file_name"), "bytes": len(pdf),
                   "fields": data.get("fields") if isinstance(data.get("fields"), dict) else None}
        gcs_client.upload_bytes(BUCKET, base[:-4] + ".json",
                                json.dumps(sidecar, indent=1).encode(), "application/json")
        print(json.dumps({"event": "sma_webhook_stored", "blob": base, "bytes": len(pdf),
                          "merge_id": data.get("merge_id")}))
        return {"status": "ok", "stored": base}
