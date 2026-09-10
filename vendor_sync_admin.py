"""
vendor_sync_admin.py -- on-demand QBO vendor sync for Beacon, 2026-09-10.

Jay's own request, relayed from a separate "Vendor lookup in Beacon" session
(recovered verbatim via that session's transcript, since the two sessions
ran concurrently): "can we add a manual 'sync vendors now' trigger to
Beacon? and can we do a second check to QBO if a vendor is selected that
doesn't exist yet in Beacon's table?" Bundled into the same production
release as this session's other vendor-matching fixes (the misleading
vendor-selection UX fix and the Last-First/First-Last token-match fix in
main.py's document-extraction route) -- see 26-129's CLAUDE.md for that
context. Still pending Jay's go-ahead to promote to production.

Both capabilities below reuse the exact same live-QBO vendor list
(qbo_mcp_client.get_live_vendors(), which wraps qbo-mcp-server's own
GET /api/vendors/{company} -- the identical endpoint the nightly
vendor_sync_job.py Cloud Run Job already calls, 26-124 GCP Daily Jobs) and
the same diff/upsert shape that job uses against checkreq.vendors (match
key: qbo_vendor_id + org_id) -- just scoped to ONE org instead of looping
every COMPANIES entry, and triggered on demand instead of nightly at
11:05 PM ET. No new qbo-mcp-server endpoint was needed for either capability.

New file per the standing main.py rule (2026-08-07, Jay): new capabilities
get new files, main.py gains wiring only.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

import db
import qbo_mcp_client
import rbac

router = APIRouter()

_current_user = None
_current_org = None


def register(app, *, current_user, current_org) -> None:
    global _current_user, _current_org
    _current_user, _current_org = current_user, current_org
    app.include_router(router)


def sync_vendors_now(org_id: int, company_code: str) -> dict:
    """Diffs the current org's live QBO vendor list against
    checkreq.vendors and upserts -- same match key and create/update/
    deactivate logic as vendor_sync_job.py's sync_vendors(), scoped to one
    org rather than looping every company, and using this app's own db.py
    rather than 26-124's pg_store.py (a different repo/deploy, same
    Postgres instance/table). Returns {"created", "updated", "deactivated",
    "unchanged"} on success, or {"error": "..."} if the live QBO call
    itself failed."""
    data, err = qbo_mcp_client.get_live_vendors(company_code)
    if err:
        return {"error": err}
    vendors = (data or {}).get("vendors", [])
    qbo_map = {v["id"]: v for v in vendors if v.get("id") and v.get("active", True)}

    existing = db.query(
        "SELECT qbo_vendor_id, display_name, company_name, address, email, is_active "
        "FROM checkreq.vendors WHERE org_id = %s",
        (org_id,),
    )
    pg_map = {r["qbo_vendor_id"]: r for r in existing}

    created = updated = deactivated = unchanged = 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            for qbo_id, vendor in qbo_map.items():
                name_new = vendor.get("name", "")
                company_name_new = vendor.get("company_name", "")
                address_new = vendor.get("address", "")
                email_new = vendor.get("email", "")
                if qbo_id in pg_map:
                    row = pg_map[qbo_id]
                    changed = (
                        row["display_name"] != name_new or
                        (row["company_name"] or "") != company_name_new or
                        (row["address"] or "") != address_new or
                        (row["email"] or "") != email_new or
                        row["is_active"] is False
                    )
                    if changed:
                        cur.execute(
                            "UPDATE checkreq.vendors SET display_name=%s, company_name=%s, "
                            "address=%s, email=%s, is_active=TRUE, updated_at=now() "
                            "WHERE org_id=%s AND qbo_vendor_id=%s",
                            (name_new, company_name_new, address_new, email_new, org_id, qbo_id),
                        )
                        updated += 1
                    else:
                        unchanged += 1
                else:
                    cur.execute(
                        "INSERT INTO checkreq.vendors "
                        "(org_id, qbo_vendor_id, display_name, company_name, address, email, is_active, updated_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,TRUE,now())",
                        (org_id, qbo_id, name_new, company_name_new, address_new, email_new),
                    )
                    created += 1
            for qbo_id, row in pg_map.items():
                if qbo_id not in qbo_map and row["is_active"] is not False:
                    cur.execute(
                        "UPDATE checkreq.vendors SET is_active=FALSE, updated_at=now() "
                        "WHERE org_id=%s AND qbo_vendor_id=%s",
                        (org_id, qbo_id),
                    )
                    deactivated += 1

    return {"created": created, "updated": updated, "deactivated": deactivated, "unchanged": unchanged}


def find_and_sync_one_vendor(org_id: int, company_code: str, vendor_name: str) -> int | None:
    """Live single-vendor QBO fallback -- called from main.py's
    /api/extract-document route only after BOTH local checkreq.vendors
    matches (a plain substring match, then the 2026-09-10 token-based
    Last-First/First-Last reorder fallback) have already found nothing.
    Fetches the full live QBO vendor list (the same call sync_vendors_now()
    makes -- qbo-mcp-server has no per-name QBO vendor search endpoint) and
    applies the identical token-based match main.py's own local fallback
    uses, so a vendor that is real in QBO but was never synced into
    Beacon's table (created in QBO after the last nightly sync, or simply
    never picked up yet) still resolves to a real match instead of forcing
    the submitter through "Add a new vendor" for a vendor that already
    exists.

    On a match, upserts that ONE vendor into checkreq.vendors immediately
    so it has a real local id usable as matched_vendor_id right away, not
    just a name remembered until the next nightly sync.

    Returns the new/existing local checkreq.vendors.id, or None if the live
    QBO list has no name match either (a genuinely new vendor)."""
    data, err = qbo_mcp_client.get_live_vendors(company_code)
    if err or not data:
        return None
    vendors = [v for v in data.get("vendors", []) if v.get("id") and v.get("active", True)]

    tokens = [t.lower() for t in re.split(r"[\s,]+", vendor_name) if len(t) > 1]
    if not tokens:
        return None

    match = None
    for v in vendors:
        name_lower = (v.get("name") or "").lower()
        if all(t in name_lower for t in tokens):
            match = v
            break
    if not match:
        return None

    qbo_id = match["id"]
    name_new = match.get("name", "")
    company_name_new = match.get("company_name", "")
    address_new = match.get("address", "")
    email_new = match.get("email", "")

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkreq.vendors "
                "(org_id, qbo_vendor_id, display_name, company_name, address, email, is_active, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,TRUE,now()) "
                "ON CONFLICT (org_id, qbo_vendor_id) DO UPDATE SET "
                "display_name=EXCLUDED.display_name, company_name=EXCLUDED.company_name, "
                "address=EXCLUDED.address, email=EXCLUDED.email, is_active=TRUE, updated_at=now() "
                "RETURNING id",
                (org_id, qbo_id, name_new, company_name_new, address_new, email_new),
            )
            row = cur.fetchone()
            return row["id"] if row else None


@router.post("/admin/vendors/sync-now")
def vendors_sync_now_route(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    org = _current_org(request)
    org_id = org["id"] if org else None
    if org_id is None or not rbac.user_has_role(user["id"], "setup_admin", org_id=org_id):
        return RedirectResponse("/admin/setup/vendors", status_code=303)

    result = sync_vendors_now(org_id, org["code"])
    if "error" in result:
        msg = f"Sync failed: {result['error']}"
    else:
        msg = (f"Synced from QBO: {result['created']} created, {result['updated']} updated, "
               f"{result['deactivated']} deactivated, {result['unchanged']} unchanged.")
    return RedirectResponse(f"/admin/setup/vendors?sync_result={quote(msg)}", status_code=303)
