"""
logic/overview_docs/store.py -- who may read the Beacon Overview library, and the two reads (26-163). No FastAPI.

The library is CFM-wide, not scoped to the entity a person is working in (Jay, 2026-10-25: "all Cornerstone staff"): anyone holding
cornerstone_employee at ANY organization may read it. Beacon's web login can only read these tables (migration 103), and only
published documents are ever returned.
"""
from __future__ import annotations

import re

import db
import rbac

STAFF_ROLE = "cornerstone_employee"
AUDIENCE = "cornerstone_staff"
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def is_cornerstone_staff(user_id: int | None) -> bool:
    """cornerstone_employee at any organization (org_id=None means 'anywhere')."""
    if not user_id:
        return False
    return rbac.user_has_any_role(user_id, [STAFF_ROLE], org_id=None)


def list_docs() -> list[dict]:
    """Published documents for staff, by category, then sort_order, then title. No body text."""
    return db.query(
        """
        SELECT slug, title, summary, category, sort_order, updated_at, version
          FROM checkreq.overview_docs
         WHERE is_published AND audience = %s
         ORDER BY category, sort_order, title
        """,
        (AUDIENCE,),
    )


def get_doc(slug: str) -> dict | None:
    """One published document, or None (unknown slug, unpublished, or not a valid slug)."""
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        return None
    return db.query_one(
        """
        SELECT slug, title, summary, category, body_format, body_html, sort_order, updated_at, version
          FROM checkreq.overview_docs
         WHERE slug = %s AND is_published AND audience = %s
        """,
        (slug, AUDIENCE),
    )
