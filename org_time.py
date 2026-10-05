"""
org_time.py -- per-diocese time zone + the one date/time display format
(2026-10-05, Jay's User Access screen feedback).

Jay: dates and times should read "year, month, day, then hour:minute a.m./p.m.,
no seconds", and "for maximum utility, make [the time zone] a global option in
the main/Diocesan setup screen -- all parishes under the Diocese adhere to the
same time zone." So the zone is a property of the DIOCESE (the checkreq.
organizations row, edited on /admin/setup/organizations), and every parish
under that diocese formats its times with it.

Display shape: ``2026-10-05 4:37 PM ET`` -- the label is the short, always-correct
zone name (ET/CT/MT/PT...), not EST/EDT, so it stays right on both sides of the
daylight-saving change. The conversion itself is done by zoneinfo, so the clock
time is right either way.

Deployment safety: checkreq.organizations.time_zone comes from migration 072.
Until that is applied on an environment, every lookup here falls back to
America/New_York (the zone Beacon has always used) and the setup screen hides its
Time Zone column -- nothing errors on an environment that has not been migrated.
The column is read with to_jsonb(o) ->> 'time_zone' for the same reason (a missing
column yields NULL instead of an UndefinedColumn error).

Depends only on db.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import db

DEFAULT_ZONE = "America/New_York"

# (IANA name, label shown in the setup dropdown, short label shown after a time)
_ZONES = [
    ("America/New_York",    "Eastern (ET)",                          "ET"),
    ("America/Chicago",     "Central (CT)",                          "CT"),
    ("America/Denver",      "Mountain (MT)",                         "MT"),
    ("America/Phoenix",     "Mountain, no daylight saving (MST)",    "MST"),
    ("America/Los_Angeles", "Pacific (PT)",                          "PT"),
    ("America/Anchorage",   "Alaska (AKT)",                          "AKT"),
    ("Pacific/Honolulu",    "Hawaii (HST)",                          "HST"),
]
ALLOWED_ZONES = [(name, label) for name, label, _ in _ZONES]
_SHORT = {name: short for name, _, short in _ZONES}


def is_valid_zone(name: str | None) -> bool:
    return bool(name) and name in _SHORT


def column_exists() -> bool:
    """True once migration 072 has been applied on this environment. Cheap
    (information_schema) and only called from the setup screen."""
    row = db.query_one(
        "SELECT 1 AS ok FROM information_schema.columns "
        "WHERE table_schema = 'checkreq' AND table_name = 'organizations' "
        "AND column_name = 'time_zone'"
    )
    return row is not None


def zone_name_for_org(org_id: int | None) -> str:
    """The diocese's configured IANA zone, or the default when none is set,
    the column does not exist yet, or the stored value is not one we allow."""
    if not org_id:
        return DEFAULT_ZONE
    row = db.query_one(
        "SELECT to_jsonb(o) ->> 'time_zone' AS tz FROM checkreq.organizations o WHERE o.id = %s",
        (org_id,),
    )
    tz = (row or {}).get("tz")
    return tz if is_valid_zone(tz) else DEFAULT_ZONE


def format_local(dt, zone_name: str | None = None) -> str:
    """``2026-10-05 4:37 PM ET`` -- no seconds, 12-hour clock, short zone label.
    A naive datetime is taken as UTC (what the database hands back for
    timestamptz columns read without a connection time zone). None -> an em
    dash, so a template can print the result unconditionally."""
    if dt is None:
        return "—"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    name = zone_name if is_valid_zone(zone_name) else DEFAULT_ZONE
    local = dt.astimezone(ZoneInfo(name))
    hour12 = local.hour % 12 or 12
    meridiem = "AM" if local.hour < 12 else "PM"
    return f"{local:%Y-%m-%d} {hour12}:{local:%M} {meridiem} {_SHORT[name]}"
