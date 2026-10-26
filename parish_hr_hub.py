"""
parish_hr_hub.py -- the "HR & Payroll" menu in Parish Mode (26-129 / 26-162, 2026-10-23).

Jay: "the parish mode should see one menu item on the Main Menu for HR and Payroll, then have whatever it has access to underneath.
Obviously, if they have no access, they don't see the menu option." This is the parish-side twin of the Diocese Mode menu in hr_hub.py.

    Parish Home -> HR & Payroll (/parish-hr)
                     Staff       (/timekeeping/roster)   the parish's staff roster and the changes it proposes to the diocese
                     Timekeeping (/timekeeping)          hours for the current payroll period

Parish Home shows ONE tile for all of it, and only when at least one card below would open for this person. The page only LINKS to
screens that already exist; every one of them still enforces its own gate. A card's visibility must match its route's gate (the
standing rule): both screens above open through timekeeping.timekeeping_context(), which needs Timekeeping on for the diocese
(org_features "timekeeping"), HR-activated for the parish (timekeeping_activation) AND, since 2026-10-23 (Jay: "the HR & Payroll tile
and both screens show only to people with Timekeeping, Parish Finance or Parish Admin"), timekeeping.can_manage_timekeeping(): the
parish_time, parish_finance or parish_admin role at this parish, or at the parish's diocese hr_admin (the diocesan HR role),
beacon_admin, setup_admin, or a Cornerstone employee of the linked entity. A bare Parish Member sees no tile and gets a plain
"ask your parish administrator" page if they open a screen by address.

To add a card later (Staff Benefits and Onboarding & Offboarding for parishes, 26-162): add an entry to _CARDS with a `visible(user,
parish)` check that copies the new screen's own gate, and its link. Parish Home and this page pick it up with no other change.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import org_features
import timekeeping_activation

router = APIRouter()

_current_user = None
_current_org = None
_render = None


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


def _timekeeping_on(user, parish) -> bool:
    """The same three checks timekeeping.timekeeping_context() makes: the diocese's feature, the parish's own HR switch, and the
    person's role (timekeeping.can_manage_timekeeping)."""
    import timekeeping                      # loaded here: timekeeping imports parish_mode, which imports this module
    return bool(org_features.is_enabled(parish["org_id"], "timekeeping") and timekeeping_activation.parish_hr_enabled(parish)
                and timekeeping.can_manage_timekeeping(user, parish))


_CARDS = [
    {"title": "Staff", "url": "/timekeeping/roster", "visible": _timekeeping_on,
     "desc": "Your parish's staff roster: see who is on it, and propose adding, changing or ending someone. The diocese reviews each change."},
    {"title": "Timekeeping", "url": "/timekeeping", "visible": _timekeeping_on, "guide": "parish-timekeeping",
     "desc": "Enter and submit staff hours for the current payroll period."},
]


def cards_for(user, parish) -> list[dict]:
    """The cards this person may see for this parish, as {title, url, desc}. Empty when there is nothing to open. Never raises: a
    failed check just hides that card, so Parish Home can never be broken by this menu."""
    if not user or not parish:
        return []
    out = []
    for c in _CARDS:
        try:
            ok = bool(c["visible"](user, parish))
        except Exception:
            ok = False
        if ok:
            out.append({"title": c["title"], "url": c["url"], "desc": c["desc"], "guide": c.get("guide")})
    return out


@router.get("/parish-hr", response_class=HTMLResponse)
def parish_hr_menu(request: Request):
    import timekeeping                      # loaded here: timekeeping imports parish_mode, which imports this module
    user, parish, _diocese_org, err = timekeeping.parish_context(request)
    if err:
        return err
    cards = cards_for(user, parish)
    if not cards:
        return RedirectResponse("/parish-view")
    return _render(request, "hr_hub.html", user, {
        "page_title": "HR & Payroll", "back_url": "/parish-view", "back_label": "Back to " + parish["name"] + (", " + parish["city"] if parish.get("city") else ""),
        "sub": "Staff and Timekeeping. Only the screens you have access to are shown.",
        "page_guide": "parish-hr",
        "cards": cards})
