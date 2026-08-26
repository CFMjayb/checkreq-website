"""
congregation.py — Parish Portal S6 (Congregation info), 2026-08-26. New
file per NFR-11 / the standing main.py rule.

Owns portal.congregation_cache (migration 049): the live-fetch + persist +
resilience-fallback logic Parish Portal Plan.md's S6 line names
("congregation.py against the 26-120 tool -- clergy slice live; wardens
pending vendor -- cache + as_of, correction request type") and NFR-05
(serve last-known-good data if a live vendor call fails, with an honest
"as of" stamp -- never a silent stale answer presented as fresh).

Jay, 2026-08-26: "Build the real S6 now -- noting that it will not be
complete until we receive an endpoint that will give you ALL leadership of
a parish, not just Clergy. That is coming soon. Just make sure your
postgres tables have the ability to store all leadership contacts for a
parish." Today's only real source is 26-120's church_directory.py (via
databank_mcp_client.get_clergy_for_church()) -- clergy only, name/title/
role, no email/mobile, no person-level Databank id in the returned rows.
The cache schema itself is role-agnostic (role_category/email/phone/
databank_person_webacct all present and nullable) so a future fuller
leadership endpoint slots in without a schema change -- see
049_congregation_cache.sql's own header for the full design note.

get_congregation() is the one function a caller (parish_info.py) should
use -- never databank_mcp_client directly. It always tries a live refresh
first (freshest data wins when Databank is reachable); on failure it falls
back to the last cached snapshot, with that snapshot's own as_of stamp, as
long as one exists -- never a bare crash, and never a stale answer
presented as fresh without saying so.

Fails open on migration 049 not being applied yet (a real possibility --
see this file's own apply script for why this session couldn't run it
directly, a Google Workspace RAPT reauth block on this PC's Postgres
access): persistence is best-effort and silently skipped if the table
doesn't exist, but live data still renders. Matches this codebase's
established fail-closed-on-any-DB-error pattern for a not-yet-applied
migration (see cornerstone_mode.py's handling of migrations 036/037 for
the precedent) -- except here "fail closed" means "don't persist," not
"don't show the user anything."
"""
from __future__ import annotations

import db
import databank_mcp_client

SOURCE_CHURCH_DIRECTORY = "databank_church_directory"


def _cache_unavailable(exc: Exception) -> bool:
    """True for the class of error a not-yet-applied migration 049 raises
    (missing table/relation) -- vs. a real, unexpected DB error a caller
    should still see. Matched on message text since the exact psycopg
    exception class isn't imported project-wide in this codebase."""
    msg = str(exc)
    return "congregation_cache" in msg and (
        "does not exist" in msg or "UndefinedTable" in exc.__class__.__name__
    )


def _normalize_live_clergy(clergy: list[dict]) -> list[dict]:
    """Maps church_directory.py's clergy_for_church() row shape
    (clergyrole/title1/FIRST1/MI1/LAST1/suffix1) onto the same field names
    portal.congregation_cache uses, so a template renders identically
    whether the rows came straight from a live call or from the cache."""
    return [
        {
            "role_category": "clergy",
            "role": c.get("clergyrole"),
            "title": c.get("title1"),
            "first_name": c.get("FIRST1"),
            "middle_name": c.get("MI1"),
            "last_name": c.get("LAST1"),
            "suffix": c.get("suffix1"),
            "email": None,
            "phone": None,
        }
        for c in (clergy or [])
    ]


def get_cached_snapshot(parish_id: int) -> tuple[list[dict], str | None]:
    """The last persisted snapshot for this parish, if any. Returns
    (rows, as_of_iso_or_None) -- rows is [] and as_of is None both when
    nothing has ever been cached and when the table doesn't exist yet.
    ORDER BY id preserves the fetch-time ordering church_directory.py's
    clergy_for_church() already applies (Rector/Priest-in-Charge first,
    then associates, then deacons, then everything else) -- rows for one
    parish are always inserted together in one pass, so ascending id is
    equivalent to ascending role rank without re-deriving it here."""
    try:
        rows = db.query(
            "SELECT * FROM portal.congregation_cache WHERE parish_id = %s ORDER BY id",
            (parish_id,),
        )
    except Exception as exc:
        if _cache_unavailable(exc):
            return [], None
        raise
    if not rows:
        return [], None
    return rows, rows[0]["as_of"].isoformat()


def _persist_snapshot(parish_id: int, clergy: list[dict]) -> bool:
    """Best-effort atomic replace of this parish's cache rows. Returns
    False (never raises) when the table doesn't exist yet -- persistence
    is a nice-to-have layered on top of an already-working live call, not
    a hard dependency of it. Any OTHER database error still raises, since
    that's a real bug worth surfacing, not something to silently paper
    over as "couldn't cache today."""
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM portal.congregation_cache WHERE parish_id = %s",
                    (parish_id,),
                )
                for c in clergy or []:
                    cur.execute(
                        "INSERT INTO portal.congregation_cache "
                        "(parish_id, as_of, source, role_category, role, title, "
                        "first_name, middle_name, last_name, suffix) "
                        "VALUES (%s, NOW(), %s, 'clergy', %s, %s, %s, %s, %s, %s)",
                        (
                            parish_id, SOURCE_CHURCH_DIRECTORY,
                            c.get("clergyrole"), c.get("title1"),
                            c.get("FIRST1"), c.get("MI1"), c.get("LAST1"), c.get("suffix1"),
                        ),
                    )
        return True
    except Exception as exc:
        if _cache_unavailable(exc):
            return False
        raise


def get_congregation(parish_id: int, church_webacct) -> dict:
    """The one entry point a page should call. Returns:
    {"rows": [...], "as_of": iso_str_or_None, "error": str_or_None,
     "from_cache": bool, "live_error": str_or_None}

    `rows` is always the best available answer -- live if reachable and
    (if migration 049 is applied) persisted; the last cached snapshot if
    live failed and a snapshot exists; empty only if live failed AND
    nothing has ever been cached, in which case `error` explains why
    there's nothing to show. `from_cache`/`live_error` are for an honest
    "data as of X" freshness note in the UI -- never hide a fallback."""
    if not church_webacct:
        rows, as_of = get_cached_snapshot(parish_id)
        no_link_msg = "This parish isn't linked to a Databank church record yet."
        if rows:
            return {"rows": rows, "as_of": as_of, "error": None,
                     "from_cache": True, "live_error": no_link_msg}
        return {"rows": [], "as_of": None, "error": no_link_msg,
                 "from_cache": False, "live_error": None}

    clergy, live_error = databank_mcp_client.get_clergy_for_church(church_webacct)

    if live_error is None:
        persisted = _persist_snapshot(parish_id, clergy)
        if persisted:
            rows, as_of = get_cached_snapshot(parish_id)
            return {"rows": rows, "as_of": as_of, "error": None,
                     "from_cache": False, "live_error": None}
        # Migration 049 not applied yet -- serve the live data unpersisted,
        # rather than blocking on something that was never a hard
        # dependency of showing today's clergy list.
        return {"rows": _normalize_live_clergy(clergy), "as_of": None, "error": None,
                 "from_cache": False, "live_error": None}

    rows, as_of = get_cached_snapshot(parish_id)
    if rows:
        return {"rows": rows, "as_of": as_of, "error": None,
                 "from_cache": True, "live_error": live_error}
    return {"rows": [], "as_of": None, "error": live_error,
             "from_cache": False, "live_error": live_error}
