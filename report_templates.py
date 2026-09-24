"""
report_templates.py -- 26-149 Actual vs Budget report templates, admin screen.

Jay, 2026-09-22: "there needs to be a button on the admin screen in Beacon that
allows it to be initialized there." First cut: lists the CURRENT entity's
templates (reports.templates, migration 064) with a "Run Now" button per
template -- pick a report month, and the workbook is built on demand by
qbo-mcp-server's report engine (reports/soa_bva.py) and downloaded straight to
the browser.

On-demand only: nothing is emailed, no review is started, and no
reports.template_runs row is written -- those belong to the scheduled run +
review queue (26-149 Phases 4/5), not built yet. Template create/edit screens
(Phase 3) live in report_template_editor.py (2026-09-23), registered from
register() below so main.py stays wiring-only.

Reads reports.templates straight from this app's own database (db.py, which
already points at cfmqbo or cfmqbo_prod per BEACON_ENV), and passes the same
env to qbo-mcp-server so the engine reads the same copy of the template.

Gate: setup_admin or beacon_admin AT THE CURRENT ENTITY (entity-scoped, like
Setup Tables) -- a report is that entity's financial data. The run route also
re-checks that the template belongs to the current entity, never trusting the
id in the URL alone.

New file per the standing main.py rule; main.py gains wiring only.

2026-09-23 (later): templates now have a Report Type (Actual vs Budget or Fund
Summary -- report_template_editor.REPORT_TYPES). The screen is also reachable
from CFM Items for a Cornerstone-served entity, where it is managed by a
Cornerstone Employee or a Beacon Admin at that entity (Jay, 2026-09-23);
dioceses keep the setup_admin / beacon_admin gate.
"""
from __future__ import annotations

import os
import re
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import cornerstone_mode
import db
import qbo_mcp_client
import rbac
import report_template_editor

router = APIRouter()

_current_user = None
_current_org = None
_render = None

_ROLES = ["setup_admin", "beacon_admin"]
_CORNERSTONE_ROLES = ["cornerstone_employee", "beacon_admin"]
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)
    report_template_editor.register(app, current_user=current_user, render=render,
                                    require_access=_require_access)


def _env() -> str:
    return "prod" if os.environ.get("BEACON_ENV", "dev") == "prod" else "dev"


def _require_access(request: Request):
    """(user, org, None) when allowed; (None, None, response) otherwise. No
    selected entity means no access -- never let org_id=None fall through to
    rbac.py's "check every org" meaning."""
    user = _current_user(request)
    if not user:
        return None, None, RedirectResponse("/login")
    org = _current_org(request)
    org_id = org["id"] if org else None
    if org_id is None:
        return None, None, JSONResponse({"error": "Select an entity first"}, status_code=403)
    served = cornerstone_mode.is_cornerstone_org(org_id)
    roles = _CORNERSTONE_ROLES if served else _ROLES
    if not rbac.user_has_any_role(user["id"], roles, org_id=org_id):
        need = "Cornerstone Employee or Beacon Admin" if served else "Setup Admin or Beacon Admin"
        return None, None, JSONResponse({"error": f"{need} access required"}, status_code=403)
    return user, org, None


def _last_completed_month(today: date | None = None) -> str:
    today = today or date.today()
    y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return f"{y:04d}-{m:02d}"


def _month_end(ym: str) -> str | None:
    if not re.fullmatch(r"\d{4}-\d{2}", ym or ""):
        return None
    y, m = int(ym[:4]), int(ym[5:7])
    if not 1 <= m <= 12:
        return None
    import calendar
    return f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


def _templates_for_org(org_id: int) -> list[dict]:
    return db.query(
        """SELECT t.id, t.name, t.description, t.report_type, t.is_active, t.requires_review,
                  t.budget_name, t.full_entity, u.email AS reviewer_email,
                  u.display_name AS reviewer_name,
                  (SELECT string_agg(COALESCE(l.section, l.fund_group, 'Fund') || ': ' ||
                                     l.line_label || ' (' || l.account_mask || ')',
                                     '; ' ORDER BY l.sort_order, l.id)
                     FROM reports.template_lines l
                    WHERE l.template_id = t.id AND l.is_active) AS lines,
                  (SELECT string_agg(s.frequency || ', day ' || s.send_day_of_month, '; ')
                     FROM reports.template_schedules s
                    WHERE s.template_id = t.id AND s.is_active) AS schedule,
                  (SELECT string_agg(r.email, ', ' ORDER BY r.id)
                     FROM reports.template_recipients r
                    WHERE r.template_id = t.id AND r.is_active) AS recipients,
                  (SELECT count(*) FROM reports.template_lines l
                    WHERE l.template_id = t.id AND l.is_active) AS line_count,
                  (SELECT r.status || ' (' || to_char(r.period_end, 'Mon YYYY') || ')'
                     FROM reports.template_runs r
                    WHERE r.template_id = t.id
                 ORDER BY r.created_at DESC LIMIT 1) AS last_run
             FROM reports.templates t
        LEFT JOIN checkreq.app_users u ON u.id = t.reviewer_user_id
            WHERE t.org_id = %s
         ORDER BY t.is_active DESC, t.name""",
        (org_id,),
    )


@router.get("/admin/report-templates", response_class=HTMLResponse)
def report_templates_page(request: Request):
    user, org, err = _require_access(request)
    if err:
        return err
    served = cornerstone_mode.is_cornerstone_org(org["id"])
    return _render(request, "admin_report_templates.html", user, {
        "templates": _templates_for_org(org["id"]),
        "report_types": report_template_editor.REPORT_TYPES,
        "back_url": "/admin/cfm-items" if served else "/admin",
        "back_label": "CFM Items" if served else "Administrative Tasks",
        "default_month": _last_completed_month(),
        "max_month": _last_completed_month(),
        "report_env": _env(),
    })


@router.post("/admin/report-templates/{template_id}/run")
async def report_template_run(request: Request, template_id: int):
    user, org, err = _require_access(request)
    if err:
        return err
    tpl = db.query_one(
        "SELECT id, name, is_active FROM reports.templates WHERE id = %s AND org_id = %s",
        (template_id, org["id"]),
    )
    if not tpl:
        return JSONResponse({"error": "That template doesn't belong to the entity you're working in."},
                            status_code=404)
    if not tpl["is_active"]:
        return JSONResponse({"error": f"'{tpl['name']}' is inactive."}, status_code=409)

    form = await request.form()
    period_end = _month_end(str(form.get("month") or ""))
    if not period_end:
        return JSONResponse({"error": "Pick a report month."}, status_code=400)
    if period_end[:7] > _last_completed_month():
        return JSONResponse({"error": "Pick a month that has already ended."}, status_code=400)

    import asyncio
    content, info, run_err = await asyncio.to_thread(
        qbo_mcp_client.run_report_template, template_id, _env(), period_end)
    if run_err:
        return JSONResponse({"error": run_err}, status_code=502)
    return Response(content, media_type=_XLSX, headers={
        "Content-Disposition": f'attachment; filename="{info["file_name"]}"',
        "X-Report-Holds": str(info.get("holds", 0)),
        "X-Report-Tie-Out": info.get("tie_out", ""),
    })
