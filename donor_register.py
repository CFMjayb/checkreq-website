"""
donor_register.py -- Beacon Donor Management: the ONE entry point main.py needs.

    import donor_register
    donor_register.register(app, current_user=_current_user, render=_render, templates=templates, current_org=_current_org)

That is the whole main.py change (plus one tile snippet in parish_view.html, and one line in /portal that adds
donor_register.cornerstone_modules(user, org) to the Cornerstone Mode tile list; see "Donor Management - main.py
wiring.md"). Nothing here runs until a parish is turned on in donor.parish_settings, so wiring it in changes
nothing for any parish by itself.

register() also adds the Jinja global `donor_tile(user, parish)` that the Parish Home page uses to decide
whether to show a People tile. It never raises: any failure just means no tile.
"""
from __future__ import annotations

import cornerstone_mode
import donor_roles
import donor_web


def donor_tile(user, parish) -> dict:
    """{show, href, label, desc} for the Parish Home tile (and the Cornerstone Mode tile). Visible only when Donor Management is
    on for this parish AND the person has something to open: a People or Giving screen, a maintenance screen, or (Jay, 2026-10-09)
    the Users screen, which is how a Beacon Admin or Setup Admin gives themselves the role that opens the rest."""
    try:
        if not user or not parish:
            return {"show": False}
        ctx = donor_roles.build_ctx(user, parish)
        if not (ctx.settings.get("people_enabled") or ctx.settings.get("giving_enabled")):
            return {"show": False}
        nav = donor_web.nav_items(ctx)
        setup = [i for i in donor_web.setup_items(ctx) if i["key"] != "settings"]
        items = nav + setup
        if not items:
            return {"show": False}
        giving = bool(ctx.settings.get("giving_enabled"))
        if nav or any(i["key"] != "users" for i in setup):
            desc = "Members, households, giving, and pledges." if giving else "Members, households, and parish records."
        else:
            desc = "Set up who can use this at the parish, including your own role."
        return {"show": True, "href": items[0]["href"], "label": "People & Giving" if giving else "People", "desc": desc}
    except Exception:       # a tile must never break the page it sits on
        return {"show": False}


def cornerstone_modules(user, org) -> list:
    """The portal tile list entry for Cornerstone Mode: working inside a Cornerstone-served client's own entity, the same People &
    Giving tile the parish's own Parish Home shows, so Cornerstone staff do not need Parish Mode (Jay, 2026-10-09). [] when the
    entity has no linked parish, Donor Management is off, or the person has nothing to open. Never raises."""
    try:
        if not user or not org:
            return []
        parish = cornerstone_mode.get_parish_for_org(org["id"])
        tile = donor_tile(user, parish)
        if not tile.get("show"):
            return []
        return [{"key": "donor_management", "title": tile["label"], "desc": tile["desc"], "url": tile["href"],
                 "enabled": True, "gate": None}]
    except Exception:
        return []


def register(app, *, current_user, render, templates=None, current_org=None) -> None:
    donor_web.configure(current_user=current_user, render=render, current_org=current_org)
    # Order matters: the static /people/... paths must be registered before /people/{person_id}.
    import donor_routes_admin
    import donor_routes_people
    donor_routes_admin.register(app)
    try:                                   # Phase 2 routes (giving, pledges), present once migration 077's screens are built
        import donor_routes_giving
    except ImportError:
        donor_routes_giving = None
    if donor_routes_giving is not None:
        donor_routes_giving.register(app)
    donor_routes_people.register(app)
    if templates is not None:
        templates.env.globals["donor_tile"] = donor_tile
