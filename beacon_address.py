"""
beacon_address.py -- which Beacon web address to show in help text
(2026-09-23, Jay).

Resolved from the ENTITY the user is working under -- never from the
user's own email address. Jay: "you have to understand what entity they are
logged in under ... all of the rest go to cfmins. The problem with using a
user's email is they could be @hotmail.com." A parish volunteer on a
personal address belongs to their parish's diocese, not to their mail
provider.

Reads checkreq.sso_auto_hostnames (hostname -> org_id), the same table the
branded-login and auto-SSO features use, so a new entity's branded domain is
picked up automatically once its row exists -- no code change. When an
entity has more than one hostname (EDOM has beacon.episcopalmaryland.org and
the beacon.edom.org alias), the canonical "beacon.<email domain>" form wins.
An entity with no branded domain yet falls back to Cornerstone's own
beacon.cfmins.org.
"""
from __future__ import annotations

import db

DEFAULT_ADDRESS = "beacon.cfmins.org"


def beacon_address(org_id: int | None) -> str:
    if org_id:
        row = db.query_one(
            "SELECT hostname FROM checkreq.sso_auto_hostnames "
            "WHERE is_active AND org_id = %s "
            "ORDER BY (LOWER(hostname) = 'beacon.' || LOWER(expected_email_domain)) DESC, hostname "
            "LIMIT 1",
            (org_id,),
        )
        if row:
            return row["hostname"]
    return DEFAULT_ADDRESS
