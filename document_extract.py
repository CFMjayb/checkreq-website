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
        # Vendor address block (Jay, 2026-07-29: "if I decide to add a new
        # vendor... you should pull that information" -- a "Sold By" /
        # remit-to block on the invoice). Deliberately separate fields, not
        # one address string, so they map directly onto the new-vendor
        # form's own First/Last-or-Company/Address/City/State/Zip/Phone/
        # Contact Email inputs without any parsing on the client side.
        # Never a tax ID/SSN/EIN -- this app already has a hard rule against
        # collecting those anywhere outside the W-9 itself.
        "vendor_address_line1": {"type": ["string", "null"]},
        "vendor_address_line2": {"type": ["string", "null"], "description": "Suite/unit number, if present"},
        "vendor_city": {"type": ["string", "null"]},
        "vendor_state": {"type": ["string", "null"], "description": "2-letter US state code"},
        "vendor_zip": {"type": ["string", "null"]},
        "vendor_phone": {"type": ["string", "null"]},
        "vendor_contact_email": {"type": ["string", "null"], "description": "The vendor's own contact/support email, not the customer's"},
        # 2026-09-10 (Jay): "if you see account coding on the check request,
        # you should prefill in the gl coding". A GL account number an AP
        # staffer has already handwritten, stamped, or otherwise manually
        # annotated on the document itself while coding it for accounting --
        # e.g. a handwritten "6677" or "code to 2405.26W" in a margin.
        # Deliberately NOT any account/customer number the VENDOR printed on
        # the invoice (that identifies the customer to the vendor, not a GL
        # account) -- only a genuine internal accounting annotation counts.
        "coded_gl_account": {"type": ["string", "null"], "description": "A GL account number handwritten/stamped/annotated on the document by whoever is coding it for accounting -- not a vendor-printed account or customer number. Null if no such annotation is present."},
        # 2026-09-22 (Jay's feedback batch): "checking property location on a
        # property-related invoice" -- a multi-property vendor (a utility,
        # landscaper, etc. billing several distinct EDOM-owned properties
        # under one vendor account) prints the SPECIFIC property/service
        # address on the invoice, distinct from the vendor's own remit-to
        # address above. Research Coding (api_vendor_coding_history) uses
        # this to match against checkreq.art_list.group_label and suggest
        # that PROPERTY's own GL account, not just whatever this vendor was
        # coded to most recently regardless of which property.
        "service_address": {"type": ["string", "null"], "description": "The specific property/site/service address this invoice is billing FOR, if the document names one distinct from the vendor's own remit-to address (e.g. a utility bill's service address, a property-management line item's site address). Null if the invoice doesn't name a specific serviced property."},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "caveats": {"type": "array", "items": {"type": "string"}, "description": "Anything ambiguous that affects confidence -- multiple totals found, low image quality, an illegible handwritten annotation, etc. Do NOT report an ambiguous date format here (Jay, 2026-09-22: not valuable) -- just pick the most plausible interpretation for the date field itself."},
    },
    "required": [
        "vendor_name", "amount", "date", "description",
        "vendor_address_line1", "vendor_address_line2", "vendor_city",
        "vendor_state", "vendor_zip", "vendor_phone", "vendor_contact_email",
        "coded_gl_account", "service_address", "confidence", "caveats",
    ],
    "additionalProperties": False,
}

_PROMPT = (
    "This is a check-request supporting document -- an invoice or receipt from "
    "an arbitrary vendor. Extract the vendor name, the total amount due, the "
    "document date, a brief description or invoice/PO number, and -- if a "
    "'Sold By' / 'From' / remit-to address block for the VENDOR itself is "
    "present (not the customer's own 'Sold To'/'Bill To' address) -- the "
    "vendor's own mailing address, city, state, zip, phone, and contact "
    "email, as JSON. Also look for a GL account number someone has "
    "handwritten, stamped, or otherwise manually annotated on the document "
    "itself while coding it for accounting (e.g. a handwritten '6677' or "
    "'code to 2405.26W' in a margin or on a coding stamp) -- this is "
    "distinct from any account/customer number the vendor itself printed on "
    "the invoice to identify the customer, which is never a GL account and "
    "must not be returned here. Also extract the specific property/site/"
    "service address this invoice is billing FOR, if the document names one "
    "distinct from the vendor's own remit-to address above (common for a "
    "utility, landscaper, or property-management vendor that bills several "
    "different properties under one account) -- null if none is named. "
    "Only extract what is legibly present. "
    "Return null for anything genuinely absent or illegible rather than "
    "guessing a plausible-looking value. Never extract a tax ID, SSN, or "
    "EIN even if one is visible -- that is out of scope here regardless. "
    "If something is "
    "ambiguous (e.g. multiple dollar amounts -- subtotal vs. tax vs. total), "
    "pick your best interpretation for the field but set confidence "
    "accordingly and explain the ambiguity in caveats. If the date format "
    "itself is ambiguous, just pick the most plausible interpretation "
    "silently -- do not report that as a caveat, it isn't useful here. "
    "This is a financial document -- a confident-looking wrong answer is "
    "worse than an honest low-confidence one."
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
