"""
guides.py -- short, task-focused "how to use this" pages for Beacon features.

Jay (2026-10-01): "write a concise instruction artifact on how to use this that
will be a pill on the menu option. I would like to have more artifacts that
explain how to use certain features." -- then refined: the entry point is a small
beacon icon on the menu option; clicking it opens the guide.

Why in-app pages and not published Claude Artifacts: the people who need these
(setup admins at each diocese) have no claude.ai access, and a private artifact
link would simply not open for them. A guide is an ordinary Beacon page, styled
like the existing "How Beacon Works" documentation pages, so it works for
everyone who can sign in.

ADDING A GUIDE = two small steps, nothing else:
  1. Write templates/guide_<name>.html (extend base.html, reuse how_it_works.css
     and guides.css -- copy guide_report_template_lines.html as the model).
  2. Add one entry to GUIDES below.
To show the beacon icon on an Administrative Tasks card, add
`"guide": "<slug>"` to that card's dict in admin_hub.py. To put the same icon
anywhere else, import the macro: {% from "_guide_link.html" import guide_link %}
then {{ guide_link("<slug>", "How to use ...") }} (see admin_report_template_edit.html).

Guides are read-only documentation with no data behind them, so the only gate is
being signed in; a guide never reveals anything the signed-in person could not
already see on the screen it describes.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

_current_user = None
_render = None

# slug -> page definition. `back_url`/`back_label` feed the "Back to ..." link.
GUIDES = {
    "report-template-lines": {
        "title": "Report Template Lines",
        "template": "guide_report_template_lines.html",
        "back_url": "/admin/report-templates",
        "back_label": "Report Templates",
    },
    # 2026-10-01 (Jay): "make sure there is concise explanation of how this
    # works" -- AP Review, the AP edit screen, and the W-9 review panel.
    "ap-review-w9": {
        "title": "AP Review & W-9s",
        "template": "guide_ap_review_w9.html",
        "back_url": "/admin/ap-review",
        "back_label": "AP Review",
    },
    # 2026-10-10 (26-158): parish hours that arrive by email, the Time Status checklist.
    "payroll-hours": {
        "title": "Payroll Hours from Email",
        "template": "guide_payroll_hours.html",
        "back_url": "/admin/timekeeping/status",
        "back_label": "Time Status",
    },
    # 2026-10-06 (26-156): polling the holders of a Beacon role by email.
    "polls": {
        "title": "Polls & Surveys",
        "template": "guide_polls.html",
        "back_url": "/admin/polls",
        "back_label": "Polls & Surveys",
    },
    # 2026-10-08 (26-129): the SMA letters run list, check sheet and parish pages (sma_letters.py).
    "sma-letters": {
        "title": "SMA Letters",
        "template": "guide_sma_letters.html",
        "back_url": "/admin/sma-letters",
        "back_label": "SMA Letters",
    },
}


def register(app, *, current_user, render) -> None:
    global _current_user, _render
    _current_user, _render = current_user, render
    app.include_router(router)


@router.get("/guides/{slug}", response_class=HTMLResponse)
def guide_page(request: Request, slug: str):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    guide = GUIDES.get(slug)
    if not guide:
        return HTMLResponse("Guide not found.", status_code=404)
    return _render(request, guide["template"], user, {"guide": guide})
