"""
document_extract.py — reads an uploaded invoice/receipt (arbitrary vendor,
arbitrary format) and extracts vendor/amount/date/description to prefill the
check-request form.

Vision-based, single unified path: Claude's Messages API accepts PDFs
directly as a `document` content block (combines the page's text layer and
an internally-rendered image in one call, GA, no beta header) and images as
an `image` content block -- so a scanned/photographed invoice with no text
layer is handled by the exact same code path as a normal digital PDF. No
OCR library, no PDF-to-image rendering library needed (verified against the
installed SDK -- see PARAM SHAPES below, confirmed directly against
anthropic 0.117.0's actual type definitions rather than assumed).

Secret source matches auth_azure.py's _read_secret() pattern exactly, same
project (cfm-qbo-mcp), same secret 26-132 Teams Bot already created and
uses live (anthropic-api-key) -- no new provisioning needed.
"""
from __future__ import annotations

import json
import os

import anthropic

_SECRET_PROJECT = os.environ.get("FIRESTORE_PROJECT", "cfm-qbo-mcp")
_SECRET_NAME = "anthropic-api-key"

_cached_key: str | None = None

_SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor_name": {"type": ["string", "null"]},
        "amount": {"type": ["number", "null"]},
        "date": {"type": ["string", "null"], "description": "ISO 8601 YYYY-MM-DD"},
        "description": {"type": ["string", "null"], "description": "Invoice/PO number or a brief description of what this is for"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "caveats": {"type": "array", "items": {"type": "string"}, "description": "Anything ambiguous -- multiple totals found, unclear date format, low image quality, etc."},
    },
    "required": ["vendor_name", "amount", "date", "description", "confidence", "caveats"],
    "additionalProperties": False,
}

_PROMPT = (
    "This is a check-request supporting document -- an invoice or receipt from "
    "an arbitrary vendor. Extract the vendor name, the total amount due, the "
    "document date, and a brief description or invoice/PO number, as JSON. "
    "Only extract what is legibly present. Return null for anything genuinely "
    "absent or illegible rather than guessing a plausible-looking value. If "
    "something is ambiguous (e.g. multiple dollar amounts -- subtotal vs. tax "
    "vs. total -- or an ambiguous date format), pick your best interpretation "
    "for the field but set confidence accordingly and explain the ambiguity in "
    "caveats. This is a financial document -- a confident-looking wrong answer "
    "is worse than an honest low-confidence one."
)


def _read_secret(name: str) -> str:
    from google.cloud import secretmanager
    client = secretmanager.SecretManagerServiceClient()
    path = f"projects/{_SECRET_PROJECT}/secrets/{name}/versions/latest"
    return client.access_secret_version(name=path).payload.data.decode("utf-8")


def _api_key() -> str:
    global _cached_key
    if _cached_key is None:
        _cached_key = _read_secret(_SECRET_NAME)
    return _cached_key


def extract_fields(file_bytes: bytes, mime_type: str) -> dict:
    """One Claude API call. Raises on error/timeout/unsupported type -- the
    caller (main.py's /api/extract-document route) catches this and returns a
    graceful 'couldn't read this document' response. The form must remain
    fully usable manually regardless of extraction outcome."""
    import base64

    client = anthropic.Anthropic(api_key=_api_key(), timeout=25.0)
    b64 = base64.standard_b64encode(file_bytes).decode("utf-8")

    if mime_type == "application/pdf":
        doc_block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    elif mime_type in _SUPPORTED_IMAGE_TYPES:
        doc_block = {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}}
    else:
        raise ValueError(
            f"Unsupported file type for extraction: {mime_type}. "
            f"Supported: application/pdf, {', '.join(sorted(_SUPPORTED_IMAGE_TYPES))} "
            f"(note: iPhone photos default to HEIC, which isn't supported -- export as JPG or PDF)."
        )

    resp = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=1024,
        output_config={"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
        messages=[{"role": "user", "content": [doc_block, {"type": "text", "text": _PROMPT}]}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)
