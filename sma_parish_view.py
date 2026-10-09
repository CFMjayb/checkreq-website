"""
sma_parish_view.py -- the read-only "SMA Letter" section on a parish's own Finance page (26-129 plan revision 12).

A parish sees ONLY its own letter, and only once a REAL run has been posted: a letter in a test run carries the
SAMPLE watermark and is never shown to a parish, and a letter that is still being prepared (draft or created) is
staff's working copy, not the parish's. Step 1 posts nothing, so until step 2 exists this section stays hidden.

This module owns no route. parish_finance.py calls it from its own Finance page and its own PDF route, after its
own `_parish_context` / `can_view_finance` checks, and passes in the parish it resolved from the SESSION. The parish
id is never taken from the request, so one parish cannot ask for another's letter.
"""
from __future__ import annotations

import logging
import re

import db
import sma_store as store

log = logging.getLogger("beacon.sma_parish_view")

# A letter is the parish's to see once the run is posted and the letter has moved past staff preparation.
VISIBLE_STATUSES = ("awaiting_action", "signing", "uploaded_review", "appeal", "expired", "complete")

STATUS_TEXT = {
    "awaiting_action": "Waiting for your response. The signers were emailed with the ways to respond.",
    "signing": "Out for signature.",
    "uploaded_review": "A signed copy was uploaded and is waiting for the Business Office to review it.",
    "appeal": "An appeal was filed and is being reviewed.",
    "expired": "The signing link expired. Ask the Business Office to send a new one.",
    "complete": "Complete. Thank you.",
}


def for_parish(parish: dict) -> dict | None:
    """The newest visible letter for this parish, shaped for the Finance page, or None. Never raises: this page
    must keep working if the SMA tables are missing or the database hiccups."""
    try:
        row = db.query_one(
            "SELECT l.id, l.letter_name, l.status, l.total_allocation, l.current_version, l.completed_at, "
            "       r.id AS run_id, r.year, r.posted_at "
            "FROM portal.sma_letters l JOIN portal.sma_letter_runs r ON r.id = l.run_id "
            "WHERE l.parish_id = %s AND r.org_id = %s AND r.run_type = 'real' AND r.status = 'posted' "
            "  AND l.status = ANY(%s) AND l.current_version > 0 "
            "ORDER BY r.posted_at DESC NULLS LAST, r.year DESC, r.id DESC LIMIT 1",
            (parish["id"], parish["org_id"], list(VISIBLE_STATUSES)))
    except Exception as exc:                      # missing table (migration not applied) or a database problem
        log.warning("SMA letter lookup failed: %s", type(exc).__name__)
        return None
    if not row:
        return None
    return {
        "year": row["year"], "letter_name": row["letter_name"], "status": row["status"],
        "status_text": STATUS_TEXT.get(row["status"], ""),
        "amount": f"{int(row['total_allocation']):,}" if row["total_allocation"] is not None else "",
        "posted_at": row["posted_at"],
    }


def pdf_for_parish(parish: dict) -> tuple[bytes, str] | None:
    """(bytes, filename) of the letter `for_parish` would show, or None."""
    try:
        row = db.query_one(
            "SELECT l.id AS letter_id, r.id AS run_id FROM portal.sma_letters l JOIN portal.sma_letter_runs r ON r.id = l.run_id "
            "WHERE l.parish_id = %s AND r.org_id = %s AND r.run_type = 'real' AND r.status = 'posted' "
            "  AND l.status = ANY(%s) AND l.current_version > 0 "
            "ORDER BY r.posted_at DESC NULLS LAST, r.year DESC, r.id DESC LIMIT 1",
            (parish["id"], parish["org_id"], list(VISIBLE_STATUSES)))
        if not row:
            return None
        return store.letter_pdf(parish["org_id"], row["run_id"], row["letter_id"])
    except Exception as exc:
        log.warning("SMA letter PDF failed: %s", type(exc).__name__)
        return None


def safe_filename(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z ._-]+", "", name or "")[:120].strip() or "letter.pdf"
