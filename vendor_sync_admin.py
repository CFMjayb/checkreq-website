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

import hmac
import re
import threading
import time
from urllib.parse import quote

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, RedirectResponse

import db
import qbo_mcp_client
import rbac

router = APIRouter()

_current_user = None
_current_org = None
_get_internal_key = None
_user_has_org_access = None

# Orgs qbo-mcp-server's own _COMPANY_TO_CODE actually recognizes for
# /api/vendors/{company} -- the 11 Cornerstone-served parish orgs each have
# their own checkreq.organizations row (for QBO Bill posting) but were never
# wired into that dict, so a code outside this set would just 404/ValueError
# there. Kept as a literal set here (not "every org") so adding a served
# parish never silently expands what this hourly job tries to hit.
_HOURLY_SYNC_CODES = {"edom", "claggett", "dsw", "dme"}


def register(app, *, current_user, current_org, get_internal_key=None, user_has_org_access=None) -> None:
    global _current_user, _current_org, _get_internal_key, _user_has_org_access
    _current_user, _current_org = current_user, current_org
    _get_internal_key = get_internal_key
    # main.py's _user_has_org_access(user_id, org_id): a live role grant or a program-area assignment AT that
    # org. The QuickBooks vendor lookup below needs it; with none supplied it refuses everyone (fails closed).
    _user_has_org_access = user_has_org_access
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

    return _upsert_one_vendor(org_id, match)


def _upsert_one_vendor(org_id: int, vendor: dict) -> int | None:
    """Upserts ONE live-QBO vendor dict ({id, name, company_name, address, email}) into checkreq.vendors
    (match key: org_id + qbo_vendor_id) and returns its local checkreq.vendors.id. Shared by
    find_and_sync_one_vendor() above and the Check Request form's "Check QuickBooks" import below; the
    columns written are exactly the ones sync_vendors_now() writes, and an inactive local row is
    reactivated, as the sync does."""
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
                (org_id, vendor["id"], vendor.get("name", ""), vendor.get("company_name", ""),
                 vendor.get("address", ""), vendor.get("email", "")),
            )
            row = cur.fetchone()
            return row["id"] if row else None


# ---------------------------------------------------------------------------------------------------------------
# "Check QuickBooks" on the Check Request form (Jay, 2026-10-08): "when using the Check Request and there is a
# vendor that hasn't been synched yet ... I thought the system would have an option to check the QBO vendor
# records." The vendor picker only searches checkreq.vendors (synced hourly 10 AM-5 PM ET + nightly); the live
# single-vendor fallback above only ran inside the upload-a-document path. Without this, a vendor added in QBO
# since the last sync could only be handled with "Add a new vendor", which would create a duplicate.
#
# Two routes the form's "Not in the list? Check QuickBooks" link calls:
#   GET  /api/vendors/{org_id}/qbo-search?q=   -> matches in the live QBO vendor list (name + city only)
#   POST /api/vendors/{org_id}/qbo-import      -> {qbo_id}: upsert THAT vendor into checkreq.vendors, return its id
# qbo-mcp-server has no search-by-name, so both use its full active-vendor list (about 1,250 EDOM vendors, 3-4
# seconds, timed 2026-10-08), kept for 60 seconds per company so a search followed by an import is one QBO pull.
# The import never trusts anything the browser says about the vendor: it re-reads the vendor from that cached
# server-side list by QBO id.
# ---------------------------------------------------------------------------------------------------------------
# Beacon org code -> qbo-mcp-server company code where they differ (DME's QBO company is "dmecdf").
_QBO_COMPANY_FOR_ORG = {"dme": "dmecdf"}
_LIVE_CACHE_TTL = 60            # seconds
_MAX_RESULTS = 25
_live_cache: dict[str, tuple[float, list]] = {}
_live_lock = threading.Lock()


def _live_vendor_list(company: str) -> tuple[list | None, str | None]:
    """The active live-QBO vendor list for a qbo-mcp-server company code, cached for _LIVE_CACHE_TTL seconds.
    Returns (vendors, None) or (None, "error text")."""
    hit = _live_cache.get(company)
    if hit and time.monotonic() - hit[0] < _LIVE_CACHE_TTL:
        return hit[1], None
    with _live_lock:
        hit = _live_cache.get(company)                 # another request may have filled it while we waited
        if hit and time.monotonic() - hit[0] < _LIVE_CACHE_TTL:
            return hit[1], None
        data, err = qbo_mcp_client.get_live_vendors(company)
        if err:
            return None, err
        vendors = [v for v in (data or {}).get("vendors", []) if v.get("id") and v.get("active", True)]
        _live_cache[company] = (time.monotonic(), vendors)
        return vendors, None


def _norm(text: str | None) -> str:
    """Lowercase, punctuation to spaces: "Dickson-Patrick, Mark (Emp)" -> "dickson patrick mark emp"."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def match_vendors(vendors: list[dict], query: str, limit: int = _MAX_RESULTS) -> tuple[list[dict], int]:
    """Vendors whose name or company name contains EVERY word of the query, in any order and ignoring
    punctuation ("mark dickson" finds "Dickson-Patrick, Mark (Emp)"). Best matches first: words that start a
    word of the name, then names that start with the first word, then shorter names. Returns (the first `limit`
    matches, the total match count)."""
    terms = _norm(query).split()
    if not terms:
        return [], 0
    scored = []
    for v in vendors:
        name = v.get("name") or ""
        words = (_norm(name) + " " + _norm(v.get("company_name"))).split()
        hay = " ".join(words)
        if not all(t in hay for t in terms):
            continue
        all_word_prefix = all(any(w.startswith(t) for w in words) for t in terms)
        starts_with_first = _norm(name).startswith(terms[0])
        scored.append((0 if all_word_prefix else 1, 0 if starts_with_first else 1, len(name), name.lower(), v))
    scored.sort(key=lambda s: s[:4])
    return [s[4] for s in scored[:limit]], len(scored)


def _vendor_detail(vendor: dict) -> str:
    """A short "where is this vendor" hint (the city line of its QBO address, else its first line) so two
    vendors with similar names can be told apart. Never the email or the full address."""
    lines = [ln.strip() for ln in (vendor.get("address") or "").split("\n") if ln.strip()]
    if not lines:
        return ""
    return (lines[-1] if len(lines) > 1 else lines[0])[:80]


def _lookup_context(request: Request, org_id: int):
    """The signed-in user's right to look up vendors for this org, and the org's qbo-mcp-server company code.
    Returns (company, None) or (None, JSONResponse). Same bar as GET /api/vendors/{org_id}: a live role or a
    program-area assignment AT that org."""
    user = _current_user(request)
    if not user:
        return None, JSONResponse({"error": "Not signed in"}, status_code=401)
    if _user_has_org_access is None or not _user_has_org_access(user["id"], org_id):
        return None, JSONResponse({"error": "Not authorized for this entity."}, status_code=403)
    org = db.query_one("SELECT id, code FROM checkreq.organizations WHERE id = %s", (org_id,))
    if not org or not (org.get("code") or "").strip():
        return None, JSONResponse({"error": "Unknown entity."}, status_code=404)
    code = org["code"].strip().lower()
    return _QBO_COMPANY_FOR_ORG.get(code, code), None


_QBO_DOWN = {"error": "Couldn't reach QuickBooks right now. Try again in a minute, or use \"Add a new one\" below."}


@router.get("/api/vendors/{org_id}/qbo-search")
def api_vendors_qbo_search(org_id: int, request: Request, q: str = ""):
    company, err = _lookup_context(request, org_id)
    if err:
        return err
    q = (q or "").strip()[:100]
    if len("".join(_norm(q).split())) < 2:
        return JSONResponse({"error": "Type at least 2 letters of the vendor's name."}, status_code=400)

    vendors, lerr = _live_vendor_list(company)
    if lerr:
        print(f"[vendor_lookup] live QBO vendor list failed for {company}: {lerr}")   # never shown to the user
        return JSONResponse(_QBO_DOWN, status_code=502)

    matches, total = match_vendors(vendors, q)
    local_ids: dict = {}
    if matches:
        rows = db.query(
            "SELECT id, qbo_vendor_id FROM checkreq.vendors "
            "WHERE org_id = %s AND is_active AND qbo_vendor_id = ANY(%s)",
            (org_id, [m["id"] for m in matches]),
        )
        local_ids = {r["qbo_vendor_id"]: r["id"] for r in rows}
    return JSONResponse({
        "query": q,
        "total": total,
        "matches": [
            {"qbo_id": m["id"], "name": m.get("name", ""), "detail": _vendor_detail(m), "local_id": local_ids.get(m["id"])}
            for m in matches
        ],
    })


@router.post("/api/vendors/{org_id}/qbo-import")
def api_vendors_qbo_import(org_id: int, request: Request, payload: dict = Body(default=None)):
    company, err = _lookup_context(request, org_id)
    if err:
        return err
    qbo_id = str((payload or {}).get("qbo_id") or "").strip()
    if not re.fullmatch(r"[0-9]{1,20}", qbo_id):
        return JSONResponse({"error": "Pick a vendor from the QuickBooks results."}, status_code=400)

    vendors, lerr = _live_vendor_list(company)
    if lerr:
        print(f"[vendor_lookup] live QBO vendor list failed for {company}: {lerr}")
        return JSONResponse(_QBO_DOWN, status_code=502)
    match = next((v for v in vendors if str(v.get("id")) == qbo_id), None)
    if not match:
        return JSONResponse({"error": "That vendor isn't an active vendor in QuickBooks."}, status_code=404)

    try:
        local_id = _upsert_one_vendor(org_id, match)
    except Exception as exc:                            # noqa: BLE001 -- the form must get a clean message, not a 500 page
        print(f"[vendor_lookup] saving QBO vendor {qbo_id} for org {org_id} failed: {type(exc).__name__}: {exc}")
        return JSONResponse({"error": "Couldn't save that vendor. Try again."}, status_code=500)
    if not local_id:
        return JSONResponse({"error": "Couldn't save that vendor. Try again."}, status_code=500)
    user = _current_user(request)
    print(f"[vendor_lookup] user {user['id']} brought QBO vendor {qbo_id} into org {org_id} (checkreq.vendors id {local_id})")
    return JSONResponse({"id": local_id, "display_name": match.get("name", "")})


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


def sync_all_orgs_now() -> dict:
    """Runs sync_vendors_now() for every org whose code is one qbo-mcp-server
    actually recognizes (_HOURLY_SYNC_CODES), tolerating a per-org failure
    the same way vendor_sync_job.py (26-124, the nightly 11:05 PM ET job)
    tolerates a per-company failure -- one org's live-QBO-token gap (DSW's
    OAuth 400, DME's missing qbo-dme-tokens secret, both already documented
    live findings as of 2026-09-10) must never block EDOM/Claggett's own
    sync from completing. Returns a per-org breakdown plus overall totals,
    used by both the hourly Cloud Scheduler endpoint below and, if ever
    wanted, a future manual "sync everything" trigger."""
    orgs = db.query(
        "SELECT id, code FROM checkreq.organizations WHERE LOWER(code) = ANY(%s)",
        (list(_HOURLY_SYNC_CODES),),
    )
    results = {}
    totals = {"created": 0, "updated": 0, "deactivated": 0, "unchanged": 0}
    failed = []
    for org in orgs:
        result = sync_vendors_now(org["id"], org["code"])
        results[org["code"]] = result
        if "error" in result:
            failed.append(org["code"])
        else:
            for k in totals:
                totals[k] += result[k]
    return {"orgs": results, "totals": totals, "failed": failed}


@router.post("/internal/sync-vendors-hourly")
def sync_vendors_hourly_route(request: Request):
    """Cloud Scheduler -> this endpoint, hourly 10 AM-5 PM ET (Jay, 2026-09-10:
    "I also think we need to run the QBO vendor sync hourly from 10a to 5p").
    Machine-to-machine, gated by the same shared-secret X-Internal-Key header
    /internal/send-daily-digest already established -- no signed-in user
    drives this call. This is IN ADDITION TO the existing nightly
    vendor-sync-daily Cloud Run Job (26-124, 11:05 PM ET) -- Jay wants
    same-day vendor changes to show up in Beacon's picker sooner than the
    next morning, not a replacement for the nightly job."""
    # L8 (Security Assessment 2026-09-19): constant-time compare, matching
    # main.py's /internal/send-daily-digest and auth_code.py.
    supplied = request.headers.get("x-internal-key", "")
    expected = _get_internal_key() if _get_internal_key else ""
    if not supplied or not expected or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        return JSONResponse({"error": "unauthorized"}, status_code=403)

    result = sync_all_orgs_now()
    return JSONResponse(result)
