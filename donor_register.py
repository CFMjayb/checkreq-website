"""
donor_register.py -- Beacon Donor Management: the ONE entry point main.py needs.

    import donor_register
    donor_register.register(app, current_user=_current_user, render=_render, templates=templates)

That is the whole main.py change (plus one tile snippet in parish_view.html; see "Donor Management - main.py
wiring.md"). Nothing here runs until a parish is turned on in donor.parish_settings, so wiring it in changes
nothing for any parish by itself.

register() also adds the Jinja global `donor_tile(user, parish)` that the Parish Home page uses to decide
whether to show a People tile. It never raises: any failure just means no tile.
"""
from __future__ import annotations

import donor_roles
import donor_web


def donor_tile(user, parish) -> dict:
    """{show, href, label, desc} for the Parish Home tile. Visible only when Donor Management is on for this
    parish AND the person holds a role that opens at least one of its screens."""
    try:
        if not user or not parish:
            return {"show": False}
        ctx = donor_roles.build_ctx(user, parish)
        items = donor_web.nav_items(ctx) + [i for i in donor_web.setup_items(ctx) if i["key"] not in ("settings", "users")]
        if not items:
            return {"show": False}
        return {"show": True, "href": items[0]["href"], "label": "People & Giving" if ctx.settings.get("giving_enabled") else "People",
                "desc": "Members, households, and parish records." if not ctx.settings.get("giving_enabled")
                else "Members, households, giving, and pledges."}
    except Exception:       # a tile must never break the page it sits on
        return {"show": False}


def register(app, *, current_user, render, templates=None) -> None:
    donor_web.configure(current_user=current_user, render=render)
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
