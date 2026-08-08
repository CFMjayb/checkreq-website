"""
registry.py — Parish Portal registry: portal.parishes CRUD + the scoping
helper every portal query goes through (Parish Portal Plan.md Section 2/5,
S2). New file per NFR-11.

Scoping model: a parish belongs to exactly one diocese (checkreq.organizations
row) -- org-level scoping mirrors this app's existing current_org_id pattern
everywhere else. get_parish() is the one scoping choke point every future
portal route must go through for a specific parish_id (never query
portal.parishes directly by id alone) -- this is the "enforced server-side
in one place" requirement the plan calls out, with the cross-entity
CFO-notification bug (fixed 2026-08-01) as its cautionary tale. Parish-USER
scoping (which signed-in person may see which parish) is a separate, later
layer (S3's portal.parish_user_roles) -- this module only enforces the org
boundary, the first of the two dimensions.

sync_from_databank() seeds/refreshes the registry from 26-120's clergy
directory tool (databank_mcp_client.get_congregations(), which wraps
clergy_directory.congregations_from()) — upserts by databank_churchwebacct
where one exists; the ~75 leadership-only name variants (no churchwebacct at
all, per clergy_directory.py's own docstring) are upserted by (org_id, name)
instead, since there's no other stable key for them. This deliberately does
NOT decide which of those need human matching against duplicates already in
the registry (R3 — diocese + Jay's job, Parish Portal Plan.md Section 9) —
it only makes sure they're visible as rows for that reconciliation to happen
against, never silently dropped.
"""
from __future__ import annotations

import db
import databank_mcp_client


def list_parishes(org_id: int, include_inactive: bool = False) -> list[dict]:
    """All parishes for one diocese. include_inactive=False (default) hides
    soft-deleted rows -- this app never hard-deletes (matching its standing
    philosophy), so a "removed" parish's history stays queryable but out of
    normal listings."""
    if include_inactive:
        return db.query(
            "SELECT * FROM portal.parishes WHERE org_id = %s ORDER BY name",
            (org_id,),
        )
    return db.query(
        "SELECT * FROM portal.parishes WHERE org_id = %s AND is_active ORDER BY name",
        (org_id,),
    )


def list_all_parishes(include_inactive: bool = False) -> list[dict]:
    """Every parish across every diocese, with org info -- for the parish
    access-request dropdown (Parish Portal S3), which isn't scoped to any
    one org since a requester could in principle belong to any served
    diocese. Not a scoping choke point itself (nothing here is written back
    per-parish) -- get_parish() remains that for anything mutating."""
    if include_inactive:
        return db.query(
            "SELECT p.*, o.code AS org_code, o.name AS org_name "
            "FROM portal.parishes p JOIN checkreq.organizations o ON o.id = p.org_id "
            "ORDER BY o.name, p.name"
        )
    return db.query(
        "SELECT p.*, o.code AS org_code, o.name AS org_name "
        "FROM portal.parishes p JOIN checkreq.organizations o ON o.id = p.org_id "
        "WHERE p.is_active ORDER BY o.name, p.name"
    )


def get_parish(parish_id: int, org_id: int) -> dict | None:
    """The scoping choke point (module docstring). Returns None if
    parish_id doesn't exist OR belongs to a different org -- callers must
    treat both cases identically (a clean 404, never a hint about which
    reason applied) to avoid leaking cross-org existence information."""
    return db.query_one(
        "SELECT * FROM portal.parishes WHERE id = %s AND org_id = %s",
        (parish_id, org_id),
    )


def create_parish(org_id: int, name: str, **fields) -> dict:
    """fields may include: code, city, status, databank_churchwebacct,
    qbo_ar_customer_id, qbo_ap_vendor_id, served_tier, modules, contacts."""
    allowed = {
        "code", "city", "status", "databank_churchwebacct",
        "qbo_ar_customer_id", "qbo_ap_vendor_id", "served_tier",
        "modules", "contacts",
    }
    cols = ["org_id", "name"] + [k for k in fields if k in allowed]
    vals = [org_id, name] + [fields[k] for k in fields if k in allowed]
    placeholders = ", ".join(["%s"] * len(vals))
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO portal.parishes ({', '.join(cols)}) "
                f"VALUES ({placeholders}) RETURNING *",
                tuple(vals),
            )
            return cur.fetchone()


def update_parish(parish_id: int, org_id: int, **fields) -> dict | None:
    """Scoped update -- returns None (no-op) if parish_id doesn't belong to
    org_id, same choke-point discipline as get_parish()."""
    allowed = {
        "name", "code", "city", "status", "databank_churchwebacct",
        "qbo_ar_customer_id", "qbo_ap_vendor_id", "served_tier",
        "modules", "contacts", "is_active",
    }
    sets = [f"{k} = %s" for k in fields if k in allowed]
    if not sets:
        return get_parish(parish_id, org_id)
    vals = [fields[k] for k in fields if k in allowed]
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE portal.parishes SET {', '.join(sets)}, updated_at = NOW() "
                f"WHERE id = %s AND org_id = %s RETURNING *",
                tuple(vals) + (parish_id, org_id),
            )
            return cur.fetchone()


def sync_from_databank(org_id: int) -> dict:
    """Seed/refresh portal.parishes for org_id from the live Databank
    congregation list. Returns a summary dict — never raises; a Databank/
    network failure is reported in the summary, not an exception, since this
    is meant to be safely callable from a cron-ish admin action without
    crashing anything else.

    {"ok": bool, "error": str|None, "created": int, "updated": int,
     "with_churchwebacct": int, "leadership_only": int}"""
    congregations, error = databank_mcp_client.get_congregations()
    if error:
        return {"ok": False, "error": error, "created": 0, "updated": 0,
                "with_churchwebacct": 0, "leadership_only": 0}

    created = updated = 0
    with_id = sum(1 for c in congregations if c.get("churchwebacct") is not None)
    leadership_only = len(congregations) - with_id

    for c in congregations:
        name = (c.get("company") or "").strip()
        if not name:
            continue
        churchwebacct = c.get("churchwebacct")
        city = c.get("city")

        if churchwebacct is not None:
            existing = db.query_one(
                "SELECT id FROM portal.parishes WHERE org_id = %s AND databank_churchwebacct = %s",
                (org_id, churchwebacct),
            )
        else:
            existing = db.query_one(
                "SELECT id FROM portal.parishes WHERE org_id = %s AND databank_churchwebacct IS NULL AND name = %s",
                (org_id, name),
            )

        if existing:
            update_parish(existing["id"], org_id, name=name, city=city)
            updated += 1
        else:
            create_parish(org_id, name, city=city, databank_churchwebacct=churchwebacct)
            created += 1

    return {"ok": True, "error": None, "created": created, "updated": updated,
            "with_churchwebacct": with_id, "leadership_only": leadership_only}
