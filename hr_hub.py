"""
hr_hub.py -- the "HR & Payroll" menu and its "HR Administrative Tasks" submenu (26-158 / 26-129, 2026-10-17).

Jay: "All HR/Timekeeping menu items should be part of a submenu named 'HR & Payroll' from the main menu. The HR Administrative
Tasks would also move to a sub menu under HR & Payroll", and "Time Review and Roster Review would be under this new menu".

    Main menu -> HR & Payroll (/hr)
                   Time Review (/admin/timekeeping/status)
                   Roster Review (/admin/timekeeping/review)
                   HR Administrative Tasks (/hr/admin)
                     HR Activation, Employees, Payroll Periods, Time Categories

These pages only LINK to the screens that already exist; every one of those screens still enforces its own gate. A card is shown
only when the person holds one of the roles that screen's own route accepts at the CURRENT diocese, and only when the diocese has
that card's own switch on (org_features): Timekeeping unless the card names another ("feature"; Onboarding & Offboarding uses
hr_lifecycle, 26-162), which also keeps Cornerstone Mode out. The roles below must stay in step with each
screen's own gate (timekeeping_status / timekeeping_review: hr_admin or beacon_admin; timekeeping_employees: hr_admin or
beacon_admin; timekeeping.py's diocese-admin screens: setup_admin or hr_admin; timekeeping_activation: setup_admin).
Carried over as it was on the old Administrative Tasks hub, NOT widened: the Payroll Periods / Time Categories routes also accept
beacon_admin and the HR Activation route also accepts beacon_admin, but their cards have only ever shown to the roles listed in
_ADMIN_TASKS below. A Beacon Admin who holds none of those roles can still open the screens by address.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import hr_hub_badges
import org_features
import rbac

router = APIRouter()

_current_user = None
_current_org = None
_render = None


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


# The day-to-day screens, on the HR & Payroll page itself.
_OPERATIONS = [
    # 26-162 (2026-10-22): Onboarding & Offboarding. Its own switch (hr_lifecycle), not Timekeeping's, so EDOM and Claggett
    # (Timekeeping off) still get it. "feature" on a card names the org_features key it needs; Timekeeping is the default.
    {"title": "Onboarding & Offboarding", "url": "/hr/onboarding", "role": ["hr_admin", "benefits_admin", "beacon_admin"],
     "feature": "hr_lifecycle", "guide": "hr-onboarding",
     "desc": "The HR Status Board: every open onboarding and offboarding case on one screen, what is late and who it is waiting on."},
    {"title": "Time Review", "url": "/admin/timekeeping/status", "role": ["hr_admin", "beacon_admin"], "guide": "payroll-hours",
     "desc": "Review each parish's hours, open or close a period, close it out after payroll, enter missing hours, and export."},
    {"title": "Roster Review", "url": "/admin/timekeeping/review", "role": ["hr_admin", "beacon_admin"], "guide": "roster-review",
     "desc": "Approve or reject the roster changes (new hires, terminations, edits) parishes and emails ask for."},
]

# The former HR group of the Administrative Tasks hub, now one level down.
_ADMIN_TASKS = [
    {"title": "HR Activation", "url": "/admin/timekeeping/parishes", "role": ["setup_admin"],
     "desc": "Choose which parishes use Timekeeping / HR."},
    {"title": "Employees", "url": "/admin/timekeeping/employees", "role": ["hr_admin", "beacon_admin"],
     "desc": "Add, change or inactivate staff across every parish, set standing hours, and open an employee's hours history."},
    {"title": "Payroll Periods", "url": "/admin/timekeeping/periods", "role": ["setup_admin", "hr_admin"],
     "desc": "Define diocese payroll periods, and open or close each one."},
    {"title": "Time Categories", "url": "/admin/timekeeping/categories", "role": ["setup_admin", "hr_admin"],
     "desc": "Configure the categories parishes report hours against."},
]

_SUBMENU_CARD = {"title": "HR Administrative Tasks", "url": "/hr/admin", "submenu": True,
                 "desc": "HR Activation, Employees, Payroll Periods and Time Categories."}


def _visible(user, org_id, cards) -> list[dict]:
    """The cards this person may see at the current diocese. Nothing at all when no diocese is selected; each card also needs
    its own switch on (org_features: the card's "feature", Timekeeping unless it says otherwise -- 26-162)."""
    if not org_id:
        return []
    return [c for c in cards if org_features.is_enabled(org_id, c.get("feature", "timekeeping"))
            and rbac.user_has_any_role(user["id"], c["role"], org_id=org_id)]


@router.get("/hr", response_class=HTMLResponse)
def hr_menu(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    org = _current_org(request)
    org_id = org["id"] if org else None
    # copies: the card dicts above are module-level and shared by every request; the red counts belong to this visit only
    cards = hr_hub_badges.with_badges(org_id, [dict(c) for c in _visible(user, org_id, _OPERATIONS)])
    if _visible(user, org_id, _ADMIN_TASKS):
        cards = cards + [_SUBMENU_CARD]
    if not cards:
        return RedirectResponse("/portal")
    return _render(request, "hr_hub.html", user, {
        "page_title": "HR & Payroll", "back_url": "/portal", "back_label": "Back to Portal",
        "sub": "Time Review, Roster Review and the HR administrative tasks. Only the screens you have access to are shown.",
        "cards": cards})


@router.get("/hr/admin", response_class=HTMLResponse)
def hr_admin_tasks(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    org = _current_org(request)
    org_id = org["id"] if org else None
    cards = _visible(user, org_id, _ADMIN_TASKS)
    if not cards:
        return RedirectResponse("/hr" if _visible(user, org_id, _OPERATIONS) else "/portal")
    return _render(request, "hr_hub.html", user, {
        "page_title": "HR Administrative Tasks", "back_url": "/hr", "back_label": "Back to HR & Payroll",
        "sub": "Employees, payroll periods, time categories and which parishes use Timekeeping. Only the screens you have access to are shown.",
        "cards": cards})
