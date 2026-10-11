"""
email_pdf.py -- the stored PDF copy of each payroll email (26-158, 2026-10-11).

The payroll inbox reader draws one PDF per email it records hours from (masked: no Social Security,
bank or birth-date numbers) and PUTs it to /api/payroll/email-pdf. Beacon keeps it in its own private
bucket (org_branding.LOGO_BUCKET, which is per environment) and shows a "PDF" button next to every line
that came from that email. Only hr_admin / beacon_admin can open one (payroll_screens.py).

KEY. The email's Message-ID, stripped of < >. The stored name is the sha256 of it, so the file name
never reveals the Message-ID and the viewer URL cannot be guessed:
    payroll-email/<org_id>/<sha256>.pdf
The org id is part of the path, so a PDF can only ever be read back by the diocese that stored it.

WHAT EXISTS. One bucket listing per diocese, cached for 60 seconds and updated when a PDF is stored, so
a page with many lines costs one call, not one per line. If the bucket cannot be read the answer is
"none stored" (a missing button) and the page still opens.

No pay data is involved: a PDF is a picture of an email. The reader masks personal numbers before drawing.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from types import SimpleNamespace

MAX_BYTES = 25 * 1024 * 1024
CACHE_SECONDS = 60
FAILED_CACHE_SECONDS = 10
_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")

_lock = threading.Lock()
_listing: dict[int, tuple[float, set[str]]] = {}


def _gcs():
    import gcs_client
    return gcs_client


# The storage calls, replaceable in tests (an in-memory fake) so no test touches the bucket.
_backend = SimpleNamespace(
    upload=lambda b, p, d, ct: _gcs().upload_bytes(b, p, d, ct),
    download=lambda b, p: _gcs().download_bytes(b, p),
    list_names=lambda b, prefix: _gcs().list_names(b, prefix),
)


def _bucket() -> str:
    import org_branding
    return org_branding.LOGO_BUCKET


def clean_ref(ref: str | None) -> str:
    return (ref or "").strip().strip("<>").strip()


def token_for(ref: str | None) -> str | None:
    r = clean_ref(ref)
    return hashlib.sha256(r.encode("utf-8")).hexdigest() if r else None


def valid_token(tok: str | None) -> bool:
    return bool(tok and _TOKEN_RE.match(tok))


def _prefix(org_id: int) -> str:
    return f"payroll-email/{int(org_id)}/"


def _path(org_id: int, tok: str) -> str:
    return f"{_prefix(org_id)}{tok}.pdf"


def store(org_id: int, ref: str, data: bytes) -> dict:
    """Keeps (or replaces) the PDF for one email. Raises ValueError with a plain reason."""
    tok = token_for(ref)
    if not tok:
        raise ValueError("source_ref is required.")
    if not data or not data.startswith(b"%PDF-"):
        raise ValueError("That is not a PDF.")
    if len(data) > MAX_BYTES:
        raise ValueError("That PDF is too large.")
    _backend.upload(_bucket(), _path(org_id, tok), data, "application/pdf")
    with _lock:
        got = _listing.get(int(org_id))
        if got:
            got[1].add(tok)
    return {"stored": True, "bytes": len(data)}


def _tokens(org_id: int, *, fresh: bool = False) -> set[str]:
    now = time.time()
    with _lock:
        got = _listing.get(int(org_id))
        if got and not fresh and now - got[0] < CACHE_SECONDS:
            return set(got[1])
    try:
        names = _backend.list_names(_bucket(), _prefix(org_id))
        toks = {n.rsplit("/", 1)[-1][:-4] for n in names if n.endswith(".pdf")}
        stamp = now
    except Exception:
        toks, stamp = set(), now - (CACHE_SECONDS - FAILED_CACHE_SECONDS)
    with _lock:
        _listing[int(org_id)] = (stamp, toks)
    return set(toks)


def present_refs(org_id: int, refs: list[str]) -> list[str]:
    """Which of these Message-IDs already have a stored PDF (always a fresh look; used by the reader)."""
    have = _tokens(org_id, fresh=True)
    return [r for r in refs if (token_for(r) in have)]


def has(org_id: int, ref: str | None) -> bool:
    tok = token_for(ref)
    return bool(tok and tok in _tokens(org_id))


def url_for(org_id: int, ref: str | None) -> str | None:
    """The viewer address for an email's PDF, or None when none is stored (so no button is shown)."""
    tok = token_for(ref)
    return f"/admin/timekeeping/email/{tok}.pdf" if tok and tok in _tokens(org_id) else None


def abs_url_for(org_id: int, ref: str | None, base_url: str) -> str | None:
    u = url_for(org_id, ref)
    return f"{base_url.rstrip('/')}{u}" if u else None


def fetch(org_id: int, tok: str) -> bytes | None:
    if not valid_token(tok):
        return None
    got = _backend.download(_bucket(), _path(org_id, tok))
    return got[0] if got else None


def forget_cache() -> None:
    with _lock:
        _listing.clear()
