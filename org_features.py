"""
org_features.py -- generic per-diocese feature-flag store (2026-08-16).

Jay: "We had talked about turning on HR/Timekeeping at a Diocese level...
There will be other features that we can turn on and off for Dioceses, so
think about how you want to add that." A deliberately generic, key-based
table (checkreq.org_features: org_id, feature_key, enabled) rather than a
new boolean column on checkreq.organizations per feature -- the same
reasoning behind checkreq.roles (a new role is a plain INSERT, never a
migration). Timekeeping/HR ("timekeeping") is the first real feature_key;
any future on/off-able diocese feature reuses this table with its own key.

KNOWN_FEATURES is the one place a new feature_key needs to be added (for
the admin toggle screen's own display label) -- nothing else in this
module is feature-specific.
"""
from __future__ import annotations

import db

KNOWN_FEATURES = [
    ("timekeeping", "Timekeeping / HR"),
]


def is_enabled(org_id: int | None, feature_key: str) -> bool:
    if not org_id:
        return False
    row = db.query_one(
        "SELECT enabled FROM checkreq.org_features WHERE org_id = %s AND feature_key = %s",
        (org_id, feature_key),
    )
    return bool(row and row["enabled"])


def enabled_features_for_org(org_id: int | None) -> set[str]:
    """All feature_keys currently ON for this org -- one query, used by
    admin_hub.py so it doesn't run a separate is_enabled() per HR card."""
    if not org_id:
        return set()
    rows = db.query(
        "SELECT feature_key FROM checkreq.org_features WHERE org_id = %s AND enabled",
        (org_id,),
    )
    return {r["feature_key"] for r in rows}


def matrix_for_orgs(org_ids: list[int]) -> dict[tuple[int, str], bool]:
    """{(org_id, feature_key): enabled} for every KNOWN_FEATURES row that
    has ever been set for any of org_ids -- feeds the admin toggle grid.
    A missing (org_id, feature_key) pair means "never toggled," which the
    caller should treat as False (off by default), not an error."""
    if not org_ids:
        return {}
    rows = db.query(
        "SELECT org_id, feature_key, enabled FROM checkreq.org_features WHERE org_id = ANY(%s)",
        (org_ids,),
    )
    return {(r["org_id"], r["feature_key"]): r["enabled"] for r in rows}


def set_feature(org_id: int, feature_key: str, enabled: bool, updated_by_user_id: int | None) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO checkreq.org_features (org_id, feature_key, enabled, updated_by_user_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (org_id, feature_key)
                DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = NOW(),
                              updated_by_user_id = EXCLUDED.updated_by_user_id
                """,
                (org_id, feature_key, enabled, updated_by_user_id),
            )
