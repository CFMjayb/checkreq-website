"""
upload_guard.py -- content-sniffing allowlist for every file Beacon stores or
serves back (Security Assessment 2026-09-19, findings H2 + M12).

WHY THIS EXISTS. Until 2026-09-19 every upload path in this app trusted the
CLIENT's declared Content-Type (UploadFile.content_type -- a value the
browser fills in from the file extension, and which any crafted request can
set to anything), stored it, and later served the file back inline under
that stored type. A submitter, parish user, or anyone holding a W-9 upload
link could therefore store an HTML/SVG/XML file under a harmless-looking
name and have it RENDER on the Beacon origin, under the session of whichever
approver/admin clicked "view" -- the stored-XSS class of bug, exactly the
wrong shape for a workflow whose whole point is approvers opening what
submitters uploaded.

The fix has two halves, both here so they cannot drift apart:
  1. ACCEPT side -- sniff_allowed(): identify the real format from the file's
     own leading bytes (magic numbers), allow only PDF/JPEG/PNG/GIF/WebP,
     and return the CANONICAL media type for that format. Callers store that
     canonical type, never the client's claim.
  2. SERVE side -- serve_headers(): re-sniff the bytes at serve time and
     derive Content-Type + Content-Disposition from what the bytes actually
     are. An allowlisted image/PDF may render inline; anything else (a file
     that predates this guard, or one whose bytes no longer match) is served
     as application/octet-stream with Content-Disposition: attachment, so a
     browser downloads it rather than interpreting it. Paired with the
     X-Content-Type-Options: nosniff header security_headers.py now sets on
     every response, a browser cannot second-guess these types.

Deliberately NOT here: SVG. It is a real image format, but it is also an XML
document that can carry <script> -- org_branding.py's logo allowlist dropped
it the same day (M12). A logo that must be vector should be rasterized
before upload.

Pure functions, no I/O, no imports beyond the standard library -- safe to
import from any module (main.py, parish_documents.py, cornerstone_documents.py,
admin_setup.py, parish_org_admin.py) with zero circular-import risk.
"""
from __future__ import annotations

import re
from urllib.parse import quote

# Canonical media type -> file extension, for every format this app will
# store. Mirrors main.py's pre-existing _EXT_BY_CONTENT_TYPE / the
# extraction route's _EXTRACT_ALLOWED_TYPES exactly -- one list, kept here.
ALLOWED_TYPES: dict[str, str] = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}

# Formats a browser may render INLINE from this origin. Today identical to
# ALLOWED_TYPES (every allowed type is an image or a PDF); kept as its own
# name so a future "store but never render inline" type (e.g. .docx) can be
# added to ALLOWED_TYPES without silently becoming inline-renderable.
INLINE_SAFE_TYPES: frozenset[str] = frozenset(ALLOWED_TYPES)

# One shared size cap for the check-request attachment paths (submission +
# Edit-page Add). parish_documents.py / cornerstone_documents.py already had
# this exact figure as their own MAX_UPLOAD_BYTES; the W-9 path keeps its
# own stricter 10MB.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Acrobat tolerates leading junk before the %PDF- header (up to 1024 bytes);
# real scanner/printer output very occasionally has a BOM or whitespace
# there. Searching this small window keeps such files accepted while still
# rejecting anything that is not fundamentally a PDF.
_PDF_HEADER_WINDOW = 1024


def sniff(content: bytes) -> str | None:
    """The canonical media type of an allowlisted format, identified from
    the content's own leading bytes -- or None if the bytes are not any
    allowlisted format. Never consults a filename or a declared type."""
    if not content:
        return None
    head = content[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if b"%PDF-" in content[:_PDF_HEADER_WINDOW]:
        return "application/pdf"
    return None


def sniff_allowed(content: bytes, declared_type: str | None) -> tuple[bool, str]:
    """(True, canonical_media_type) if `content` is an allowlisted format,
    else (False, human-readable reason suitable for a 400 message).

    `declared_type` (the client's own claim) is used ONLY to word the
    rejection message -- it never influences acceptance. When the bytes are
    an allowlisted format but the claim disagrees, the bytes win and the
    canonical type is returned; the stored type is therefore always what
    the file actually is."""
    detected = sniff(content)
    if detected:
        return True, detected
    # declared_type is client-controlled and ends up in a 400 message --
    # reduce it to a media-type-shaped token so nothing else rides along.
    claimed = re.sub(r"[^A-Za-z0-9/+.\-]", "", declared_type or "")[:60] or "unknown"
    return False, (
        f"Unsupported or unrecognized file type ({claimed}). "
        f"Please upload a PDF, or a JPG/PNG/GIF/WebP image -- the file's "
        f"contents must actually be one of those formats, not just named like one."
    )


def _header_safe_filename(filename: str) -> tuple[str, str]:
    """(ascii_fallback, utf8_quoted) for a Content-Disposition header.
    Strips CR/LF/double-quote so a filename can never inject a second
    header or break out of the quoted value; the RFC 5987 filename* form
    carries the real (possibly non-ASCII) name."""
    cleaned = "".join(ch for ch in (filename or "file") if ch not in '\r\n"' and ord(ch) >= 32)
    ascii_fallback = cleaned.encode("ascii", "replace").decode("ascii").replace("?", "_") or "file"
    return ascii_fallback, quote(cleaned, safe="")


def content_disposition(filename: str, force_download: bool) -> str:
    """inline vs attachment, with a header-safe filename. Supersedes
    sharepoint_client.content_disposition (which quoted the raw filename
    with no escaping) -- that function now delegates here."""
    kind = "attachment" if force_download else "inline"
    ascii_name, utf8_name = _header_safe_filename(filename)
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"


def serve_headers(content: bytes, filename: str, force_download: bool = False) -> tuple[str, str]:
    """(media_type, content_disposition) for serving `content` back to a
    browser -- derived from the bytes, never from a stored/guessed type.

      - allowlisted image/PDF -> its canonical type; inline unless the
        caller asked for a download.
      - anything else -> application/octet-stream + attachment, always.
        Covers files stored before this guard existed (any type), and any
        file whose bytes do not match an allowlisted format."""
    detected = sniff(content)
    if detected and detected in INLINE_SAFE_TYPES:
        return detected, content_disposition(filename, force_download)
    return "application/octet-stream", content_disposition(filename, force_download=True)


# ── Legacy SVG logos (M12) ──────────────────────────────────────────────────
# SVG is dropped from the logo UPLOAD allowlist as of 2026-09-19, but six SVG
# logos were already live in production that day (org 19/SPF, parishes 280,
# 319, 335, 339) and are referenced by <img> tags on real pages. Serving them
# as octet-stream/attachment would break those images outright, so an
# ALREADY-STORED SVG (stored type says svg AND the bytes really are an SVG
# document) keeps being served inline as image/svg+xml -- but with a
# per-response sandboxing CSP that blocks any script inside it when the URL
# is opened directly (an <img> context never runs SVG script regardless).
# This branch is dead the moment those six are re-uploaded as PNG/JPEG/WebP.
LEGACY_SVG_CSP = "sandbox; default-src 'none'; style-src 'unsafe-inline'; script-src 'none'"


def sniff_svg(content: bytes) -> bool:
    head = content[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<svg"):
        return True
    return head.startswith(b"<?xml") and b"<svg" in content[:4096].lower()


def serve_logo_headers(content: bytes, stored_type: str | None, filename: str = "logo") -> tuple[str, dict]:
    """(media_type, extra_headers) for the /org-logo and /parish-logo
    routes: an allowlisted raster logo inline under its sniffed type; a
    legacy stored SVG inline under LEGACY_SVG_CSP; anything else as an
    octet-stream download."""
    if (stored_type or "").lower() == "image/svg+xml" and sniff_svg(content):
        return "image/svg+xml", {
            "Content-Security-Policy": LEGACY_SVG_CSP,
            "Content-Disposition": content_disposition(f"{filename}.svg", False),
        }
    media_type, disposition = serve_headers(content, filename, force_download=False)
    return media_type, {"Content-Disposition": disposition}
