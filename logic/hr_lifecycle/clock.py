"""
clock.py -- "today" and local dates for an entity (26-162, added in the 2026-10-22 review).

Cloud Run runs in UTC, so date.today() turns over at 8 PM Eastern: an item due tomorrow would show as late the evening
before, and a case opened at 9 PM would be dated the next day. Every due date, late flag and "opened" date in schema hr is
worked out in the ENTITY's own time zone (checkreq.organizations.time_zone, default America/New_York), the same zone the
User Access screen already shows (org_time.py). No FastAPI here.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import org_time


def zone(org_id: int | None) -> ZoneInfo:
    try:
        return ZoneInfo(org_time.zone_name_for_org(org_id))
    except Exception:
        return ZoneInfo(org_time.DEFAULT_ZONE)


def today(org_id: int | None = None) -> dt.date:
    """Today's date where the entity is."""
    return dt.datetime.now(zone(org_id)).date()


def local_date(value, org_id: int | None = None) -> dt.date | None:
    """The calendar date of a timestamp in the entity's zone (a plain date or None passes through)."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(zone(org_id)).date()
    return value
