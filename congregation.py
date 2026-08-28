"""
congregation.py — Parish Portal S6 (Congregation info), 2026-08-26. New
file per NFR-11 / the standing main.py rule.

Owns the READ side of portal.congregation_cache (migration 049). Beacon
never calls Databank live at page-view time — a nightly Cloud Run Job
(congregation_sync_job.py, in 26-124 GCP Daily Jobs) refreshes this table
for every active parish, the same pattern every OTHER Databank-sourced
table in this codebase already uses (db_contribs, db_contacts, the parish
registry itself). This module was originally built live-first (a per-page-view
Databank call, falling back to the cache only on failure), matching the
plan doc's own literal "clergy slice live... cache + as_of" wording — Jay
corrected that design directly the same day: "doesn't the real S6 access a
Postgres table to provide the parish and leadership information to Beacon?
If so, why wouldn't refreshing this data table be part of this?" This
module was rebuilt to match every sibling Databank table's convention:
Postgres-primary, refreshed on a schedule, the app just reads.

get_congregation() is the one function a caller (parish_info.py) should
use. It never raises and never calls out to Databank — a genuinely missing
or unrefreshed parish (e.g. added since the last nightly run, or with no
`databank_contact_id` linked yet) returns an honest empty result, not an
error page. Schema is role-agnostic (role_category/email/phone/
databank_person_webacct all nullable) so a future fuller Databank
leadership endpoint (lay leadership — Wardens/Treasurers/vestry — still
blocked on the vendor, see 26-120's CLAUDE.md) can populate alongside
today's clergy-only rows with no schema change. See
049_congregation_cache.sql's own header for the full design note.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import db

_ET = ZoneInfo("America/New_York")


def _format_as_of(dt) -> str:
    """Human-readable Eastern time, matching this codebase's established
    convention (summary_job.py etc.) -- e.g. "August 28, 2026 at 5:20 PM
    ET". Real bug fixed 2026-08-28: this used to be a bare
    `dt.isoformat()`, which rendered a raw UTC string like
    "2026-08-28T21:20:21.413987+00:00" directly on the page -- correct data,
    unreadable to a human, and not even in the right timezone."""
    # %-d/%-I (no leading zero) are Linux-only strftime flags -- this
    # codebase has been bitten by that once already (summary_job.py, see
    # 26-124's CLAUDE.md). Use %B/plain int day-year and lstrip("0") on the
    # 12-hour time instead, which works identically on Windows and Linux.
    local = dt.astimezone(_ET)
    time_str = local.strftime("%I:%M %p").lstrip("0")
    return f"{local.strftime('%B')} {local.day}, {local.year} at {time_str} ET"


def get_congregation(parish_id: int) -> dict:
    """Read-only. Returns:
    {"rows": [...], "as_of": human_readable_et_str_or_None}

    `rows` is [] and `as_of` is None when this parish has never been
    refreshed (not yet synced, or genuinely has no clergy on file in
    Databank's ChurchDirectory) — never an error, since an empty result is
    a normal, expected outcome here, not a failure. All rows share one
    `as_of` per refresh (one row per leadership contact — see the
    congregation_sync_job.py docstring for why); ORDER BY id preserves the
    fetch-time role ordering the sync job's source already applies
    (Rector/Priest-in-Charge first, then associates, then deacons, then
    everything else)."""
    rows = db.query(
        "SELECT * FROM portal.congregation_cache WHERE parish_id = %s ORDER BY id",
        (parish_id,),
    )
    if not rows:
        return {"rows": [], "as_of": None}
    return {"rows": rows, "as_of": _format_as_of(rows[0]["as_of"])}
