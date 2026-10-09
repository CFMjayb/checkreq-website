"""
donor_web.py -- Beacon Donor Management: helpers shared by the donor routers. No business rules here.

  gate()      resolve who is acting and at which parish, build the Ctx, and apply the two gates every
              screen shares: Donor Management turned on for this parish, and the capability the screen needs.
              The parish ALWAYS comes from parish_mode.effective_parish_mode (the same function the rest of
              the Parish Portal uses). A client-supplied parish id is never read anywhere in this module.
  back()      redirect after a POST with a short confirmation or the service's own (generic) message
  page()      render a template with the donor navigation and formatters in its context
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

import donor_roles
import parish_mode
from donor_core import label

_current_user = None
_render = None


def check_path_ids(request: Request) -> None:
    """A dependency on every donor router: an id in the URL that is too big for the database column is 'not found', not an
    unhandled database error."""
    for v in request.path_params.values():
        if isinstance(v, int) and not (0 <= v < 2 ** 62):
            raise HTTPException(status_code=404, detail="Not found")


def configure(*, current_user, render) -> None:
    global _current_user, _render
    _current_user, _render = current_user, render


def fmt_date(v) -> str:
    if isinstance(v, (dt.date, dt.datetime)):
        return f"{v.month}/{v.day}/{v.year}"
    return v or ""


def fmt_money(v) -> str:
    if v is None or v == "":
        return ""
    d = Decimal(str(v))
    return ("-" if d < 0 else "") + "${:,.2f}".format(abs(d))


def fmt_when(v) -> str:
    if isinstance(v, dt.datetime):
        return f"{v.month}/{v.day}/{v.year}"
    return fmt_date(v)


def nav_items(ctx) -> list[dict]:
    s = ctx.settings
    items = []
    if s.get("people_enabled") and ctx.can("people.view"):
        items.append({"key": "people", "label": "People", "href": "/people"})
    if s.get("giving_enabled") and (ctx.can("batch.view") or ctx.can("totals.read")):
        items.append({"key": "giving", "label": "Giving", "href": "/giving"})
    if s.get("giving_enabled") and (ctx.can("funds.manage") or ctx.can("batch.view") or ctx.can("totals.read") or ctx.can("giving.read")):
        items.append({"key": "funds", "label": "Funds & campaigns", "href": "/giving/funds"})
    if s.get("giving_enabled") and (ctx.can("pledges.manage") or ctx.can("giving.read")):
        items.append({"key": "pledges", "label": "Pledges", "href": "/pledges"})
    return items


def setup_items(ctx) -> list[dict]:
    """The maintenance screens (import, duplicates, settings). They sit in the module bar's Setup menu, never as
    tabs of their own: day-to-day screens (people, giving, funds, pledges) stay on the bar, housekeeping does not."""
    s = ctx.settings
    items = []
    if ctx.can("roles.manage"):
        items.append({"key": "users", "label": "Users", "href": "/people/users"})
    if s.get("people_enabled") and ctx.can("people.edit"):
        items.append({"key": "import", "label": "Import people", "href": "/people/import"})
        items.append({"key": "duplicates", "label": "Possible duplicates", "href": "/people/duplicates"})
    if ctx.can("roles.manage") or ctx.can("parish.activate") or (s.get("people_enabled") and ctx.can("membership.edit")):
        items.append({"key": "settings", "label": "Settings", "href": "/people/settings"})
    return items


def page(request: Request, template: str, user: dict, parish: dict, ctx, active: str, extra: dict | None = None,
         status_code: int = 200):
    flash = request.session.pop("dm_flash", None) if "dm_flash" in request.session else None
    data = {
        "ctx": ctx, "parish": parish, "dm_nav": nav_items(ctx), "dm_setup": setup_items(ctx), "dm_active": active,
        "d": fmt_date, "money": fmt_money,
        "when": fmt_when, "label": label,
        "flash_ok": flash[1] if flash and flash[0] == "ok" else None,
        "flash_err": flash[1] if flash and flash[0] == "err" else None,
    }
    if extra:
        data.update(extra)
    resp = _render(request, template, user, data)
    if status_code != 200:
        resp.status_code = status_code
    return resp


def gate(request: Request, *, need: str | None = "people.view", feature: str = "people", active: str = "people"):
    """Returns (user, parish, ctx, None) when the screen may be shown, or (None, None, None, response) with the
    redirect or the 'not available' page to return instead."""
    user = _current_user(request)
    if not user:
        return None, None, None, RedirectResponse("/login")
    parish, _preview = parish_mode.effective_parish_mode(request, user)
    if not parish:
        return None, None, None, RedirectResponse("/parish-view")
    ctx = donor_roles.build_ctx(user, parish)
    flag = "people_enabled" if feature == "people" else "giving_enabled"
    if feature and not ctx.settings.get(flag):
        return user, parish, ctx, page(request, "donor_off.html", user, parish, ctx, active,
                                       {"reason": "off", "feature": feature})
    if need and not ctx.can(need):
        return user, parish, ctx, page(request, "donor_off.html", user, parish, ctx, active,
                                       {"reason": "permission", "feature": feature}, status_code=403)
    return user, parish, ctx, None


def back(request: Request, url: str, *, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    """Redirect after a POST. The confirmation or error text rides in the signed session cookie and is shown
    once on the next page. It is deliberately NOT put in the URL: a message in a query string could be forged
    into a link someone is tricked into opening."""
    if err:
        request.session["dm_flash"] = ["err", err[:300]]
    elif ok:
        request.session["dm_flash"] = ["ok", ok[:200]]
    return RedirectResponse(url, status_code=303)
