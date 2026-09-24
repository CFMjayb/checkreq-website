"""
report_template_editor.py -- 26-149 Phase 3 (2026-09-23): create and edit
Actual vs Budget report templates in Beacon.

Jay, 2026-09-23: "start Phase 3, the template setup screen" (Plan.md Section 5,
items 1-3). Screens:
  * GET  /admin/report-templates/new             -- new-template form (Options)
  * GET  /admin/report-templates/{id}/edit       -- the editor: Options, Lines,
                                                    Recipients, Schedule
  * POST .../{id}/options | schedule             -- plain form posts
  * POST .../{id}/lines/save | recipients/save   -- batched JSON grid saves
  * POST .../{id}/preview                        -- which accounts each line
                                                    matches (live QBO chart)
  * POST .../{id}/starter-lines                  -- "Start from chart of accounts"
  * POST .../{id}/clone | toggle-active
  * GET  /admin/report-templates/api/qbo-reference -- budget names for the picker

Rules this module enforces, all on the server:
  * Same gate as the Run Now screen (report_templates.py): setup_admin or
    beacon_admin AT the current entity, and every template id in a URL is
    re-checked against the current entity -- never trusted on its own.
  * No hard deletes, anywhere. A line or recipient is retired by switching
    Active off; a template by Deactivate (template_runs has ON DELETE RESTRICT
    for the audit trail, so a template with history could not be deleted anyway).
  * Lines are also edited from the 26-125 PG Data Review workbook. Every line
    save carries the updated_at it was loaded with; if the row changed since
    (someone saved the workbook), the whole save is refused rather than
    silently overwriting their edit (the "Shadow-diff" concern 26-149's
    CLAUDE.md flagged).
  * A clone starts INACTIVE, so a copy can never be picked up by the nightly
    job (Phase 4) before someone has finished editing it.
  * Nothing here emails anyone or writes template_runs.

Writes go straight to this app's own database (db.py, cfmqbo or cfmqbo_prod per
BEACON_ENV), which is the same copy qbo-mcp-server's engine reads for that env.

Registered from report_templates.register() -- main.py is untouched.

2026-09-23 (later), Jay: "The new menu screen for setting up and editing the
Actual vs Budget Report is a model for all reports we will build ... Can you
merge the Fund Summary into the Report Templates system?" Every template now
has a Report Type (migration 067). Schedule, recipients and review are shared;
lines and options depend on the type:
  * Actual vs Budget ('bva'): lines have a Revenue/Expense section.
  * Fund Summary ('fund_summary'): lines have a fund group instead; a new
    Fund Summary template starts with a copy of the entity's current
    fund_account_masks rows (Jay: lines are per template). Its own options
    (exclude zero accounts, MTD tab) live in reports.templates.options.
The type can only be changed while a template has no active lines, so an
Actual vs Budget line is never silently read as a Fund Summary line or back.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db
import qbo_mcp_client
import cornerstone_mode
import rbac
import report_masks

router = APIRouter()

_current_user = None
_render = None
_require_access = None

_SENDERS = ["businessoffice@episcopalmaryland.org", "notifications@cfmins.org"]
_EDOM_FAMILY = {"EDOM", "CLAGGETT"}
_FREQUENCIES = ["monthly", "quarterly", "annual"]
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
_BOOL_OPTIONS = ["full_entity", "include_monthly_tab", "include_fund_tabs",
                 "include_txn_detail", "show_annual_budget", "show_accounts_under_lines",
                 "requires_review", "cc_owner_on_auto_send"]

# Every report a template can build. Adding a report later = one entry here, one
# engine in qbo-mcp-server (reports/template_store.run_template), and the CHECK
# constraint on reports.templates.report_type.
REPORT_TYPES = {"bva": "Actual vs Budget", "fund_summary": "Fund Summary"}
# Settings only one report type has, kept in reports.templates.options (JSON).
# Checkbox name on the Options form -> (options key, default).
_TYPE_OPTIONS = {
    "fund_summary": {"fs_exclude_zeros": ("exclude_zeros", True),
                     "fs_include_mtd_tab": ("include_mtd_tab", True)},
}
_ORG_CODE_TO_QBO_COMPANY = {"DME": "dmecdf"}

# Chart-of-accounts cache per org code: (fetched_at, data). Ten minutes is short
# enough that a new account shows up the same morning, long enough that a
# session of preview clicks costs one QBO pull, not one per click.
_COA_TTL = 600
_coa_cache: dict[str, tuple[float, dict]] = {}


def register(app, *, current_user, render, require_access) -> None:
    global _current_user, _render, _require_access
    _current_user, _render, _require_access = current_user, render, require_access
    app.include_router(router)


# ── helpers ───────────────────────────────────────────────────────────────────

def default_sender(org_code: str) -> str:
    """EDOM and Claggett send as the diocesan business office; everyone else as
    notifications@cfmins.org (Jay, 2026-09-22, Plan.md Section 9 item 3)."""
    return _SENDERS[0] if str(org_code or "").upper() in _EDOM_FAMILY else _SENDERS[1]


def _template(template_id: int, org_id: int) -> dict | None:
    return db.query_one(
        "SELECT * FROM reports.templates WHERE id = %s AND org_id = %s",
        (template_id, org_id),
    )


def _not_yours() -> JSONResponse:
    return JSONResponse({"error": "That template doesn't belong to the entity you're working in."},
                        status_code=404)


def _reviewer_choices(org_id: int, current_id: int | None) -> list[dict]:
    rows = rbac.users_with_org_access(org_id)
    if current_id and not any(r["id"] == current_id for r in rows):
        cur = db.query_one("SELECT id, email, display_name FROM checkreq.app_users WHERE id = %s",
                           (current_id,))
        if cur:
            rows.append(cur)
    return sorted(rows, key=lambda r: (r.get("display_name") or r["email"]).lower())


def _iso(ts) -> str:
    return ts.isoformat() if ts else ""


def _lines(template_id: int) -> list[dict]:
    rows = db.query(
        """SELECT id, section, fund_group, line_label, account_mask, sort_order, is_active,
                  updated_at
             FROM reports.template_lines WHERE template_id = %s
         ORDER BY is_active DESC, sort_order, id""",
        (template_id,),
    )
    for r in rows:
        r["updated_at"] = _iso(r["updated_at"])
    return rows


def _recipients(template_id: int) -> list[dict]:
    return db.query(
        """SELECT id, email, name, recipient_type, is_active
             FROM reports.template_recipients WHERE template_id = %s
         ORDER BY is_active DESC, recipient_type DESC, id""",
        (template_id,),
    )


def _schedule(template_id: int) -> dict | None:
    # The engine and the future nightly job read every ACTIVE schedule row; this
    # screen manages one. Prefer the active one, else the oldest.
    return db.query_one(
        """SELECT id, frequency, send_day_of_month, is_active, last_period_end_run
             FROM reports.template_schedules WHERE template_id = %s
         ORDER BY is_active DESC, id LIMIT 1""",
        (template_id,),
    )


def _options_from_form(form, org_code: str, existing_options: dict | None = None
                       ) -> tuple[dict | None, str | None]:
    """Validated option values from the Options form, or (None, error)."""
    name = str(form.get("name") or "").strip()
    if not name:
        return None, "Template name is required."
    if len(name) > 120:
        return None, "Template name is too long (120 characters at most)."
    vals = {
        "name": name,
        "description": str(form.get("description") or "").strip() or None,
        "budget_name": str(form.get("budget_name") or "").strip() or None,
    }
    for k in _BOOL_OPTIONS:
        vals[k] = form.get(k) == "on"
    sender = str(form.get("sender_email") or "").strip().lower() or default_sender(org_code)
    if sender not in _SENDERS:
        return None, "Sender must be one of the two addresses the email server is authorized for."
    vals["sender_email"] = sender
    reviewer = str(form.get("reviewer_user_id") or "").strip()
    if reviewer:
        try:
            rid = int(reviewer)
        except ValueError:
            return None, "Pick a reviewer from the list."
        if not db.query_one("SELECT 1 FROM checkreq.app_users WHERE id = %s AND is_active", (rid,)):
            return None, "That reviewer isn't an active Beacon user."
        vals["reviewer_user_id"] = rid
    else:
        vals["reviewer_user_id"] = None
    if vals["requires_review"] and not vals["reviewer_user_id"]:
        return None, "A template with review turned on needs a reviewer."
    rtype = str(form.get("report_type") or "bva").strip()
    if rtype not in REPORT_TYPES:
        return None, "Pick a report type from the list."
    vals["report_type"] = rtype
    opts = dict(existing_options or {})          # keep keys this form doesn't own
    for field, (key, _default) in _TYPE_OPTIONS.get(rtype, {}).items():
        opts[key] = form.get(field) == "on"
    vals["options"] = json.dumps(opts)
    return vals, None


def fund_mask_company(org: dict) -> str:
    """QBO company code fund_account_masks is keyed by (same rule as the engine)."""
    code = str(org.get("code") or "")
    return _ORG_CODE_TO_QBO_COMPANY.get(code.upper(), code.lower())


def _seed_fund_lines(cur, template_id: int, org: dict) -> int:
    """Copy the entity's active fund_account_masks rows into a new Fund Summary
    template's lines. Returns how many were copied."""
    rows = cornerstone_mode.get_fund_account_masks(fund_mask_company(org))
    for m in rows:
        cur.execute(
            """INSERT INTO reports.template_lines
                   (template_id, section, fund_group, line_label, account_mask, sort_order, is_active)
               VALUES (%s, NULL, %s, %s, %s, %s, TRUE)""",
            (template_id, m.get("fund_group") or None,
             m.get("display_label") or m["account_mask"], m["account_mask"],
             int(m.get("sort_order") or 100)))
    return len(rows)


def _name_taken(org_id: int, name: str, except_id: int | None = None) -> bool:
    return bool(db.query_one(
        "SELECT 1 FROM reports.templates WHERE org_id = %s AND LOWER(name) = LOWER(%s) AND id <> %s",
        (org_id, name, except_id or 0),
    ))


async def _coa(org_code: str, refresh: bool = False) -> tuple[dict | None, str | None]:
    key = org_code.upper()
    hit = _coa_cache.get(key)
    if hit and not refresh and time.time() - hit[0] < _COA_TTL:
        return hit[1], None
    data, err = await asyncio.to_thread(qbo_mcp_client.get_report_accounts, org_code)
    if err:
        return None, err
    _coa_cache[key] = (time.time(), data)
    return data, None


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


# ── new template ──────────────────────────────────────────────────────────────

@router.get("/admin/report-templates/new", response_class=HTMLResponse)
def new_template_page(request: Request):
    user, org, err = _require_access(request)
    if err:
        return err
    return _render(request, "admin_report_template_edit.html", user, {
        "tpl": None,
        "form": {"sender_email": default_sender(org["code"]), "requires_review": True,
                 "include_monthly_tab": True, "include_txn_detail": True,
                 "show_annual_budget": True, "show_accounts_under_lines": True,
                 "reviewer_user_id": user["id"],
                 "report_type": request.query_params.get("type") or "bva",
                 "options": {"exclude_zeros": True, "include_mtd_tab": True}},
        "reviewers": _reviewer_choices(org["id"], user["id"]),
        "report_types": REPORT_TYPES, "type_locked": False,
        "fund_mask_company": fund_mask_company(org),
        "senders": _SENDERS,
        "frequencies": _FREQUENCIES,
        "error": request.query_params.get("error"),
    })


@router.post("/admin/report-templates/new")
async def new_template_create(request: Request):
    user, org, err = _require_access(request)
    if err:
        return err
    form = await request.form()
    vals, verr = _options_from_form(form, org["code"])
    if not verr and _name_taken(org["id"], vals["name"]):
        verr = f"{org['code']} already has a template named '{vals['name']}'."
    if verr:
        fdict = dict(form)
        fdict["options"] = {key: form.get(field) == "on"
                            for field, (key, _d) in _TYPE_OPTIONS.get(fdict.get("report_type"), {}).items()}
        return _render(request, "admin_report_template_edit.html", user, {
            "tpl": None, "form": fdict, "reviewers": _reviewer_choices(org["id"], None),
            "report_types": REPORT_TYPES, "type_locked": False,
            "fund_mask_company": fund_mask_company(org),
            "senders": _SENDERS, "frequencies": _FREQUENCIES, "error": verr,
        })
    cols = ["org_id", "created_by_user_id", "updated_by_user_id"] + list(vals.keys())
    params = [org["id"], user["id"], user["id"]] + list(vals.values())
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO reports.templates ({', '.join(cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                params,
            )
            new_id = cur.fetchone()["id"]
            seeded = _seed_fund_lines(cur, new_id, org) if vals["report_type"] == "fund_summary" else 0
    return RedirectResponse(f"/admin/report-templates/{new_id}/edit?created=1&seeded={seeded}",
                            status_code=303)


# ── editor ────────────────────────────────────────────────────────────────────

@router.get("/admin/report-templates/{template_id}/edit", response_class=HTMLResponse)
def edit_template_page(request: Request, template_id: int):
    user, org, err = _require_access(request)
    if err:
        return err
    tpl = _template(template_id, org["id"])
    if not tpl:
        return RedirectResponse("/admin/report-templates", status_code=303)
    tpl_ctx = dict(tpl)
    tpl_ctx["updated_at"] = _iso(tpl["updated_at"])
    sched = _schedule(template_id)
    lines = _lines(template_id)
    return _render(request, "admin_report_template_edit.html", user, {
        "tpl": tpl_ctx,
        "form": tpl_ctx,
        "report_types": REPORT_TYPES,
        "type_locked": any(ln["is_active"] for ln in lines),
        "fund_mask_company": fund_mask_company(org),
        "lines": lines,
        "recipients": _recipients(template_id),
        "schedule": sched,
        "reviewers": _reviewer_choices(org["id"], tpl["reviewer_user_id"]),
        "senders": _SENDERS,
        "frequencies": _FREQUENCIES,
        "error": request.query_params.get("error"),
    })


@router.post("/admin/report-templates/{template_id}/options")
async def save_options(request: Request, template_id: int):
    user, org, err = _require_access(request)
    if err:
        return err
    tpl = _template(template_id, org["id"])
    if not tpl:
        return _not_yours()
    form = await request.form()
    base = f"/admin/report-templates/{template_id}/edit"
    if str(form.get("updated_at") or "") != _iso(tpl["updated_at"]):
        return RedirectResponse(base + "?error=Someone+else+saved+these+options+after+you+"
                                "opened+the+page.+Reload+and+make+your+change+again.", status_code=303)
    vals, verr = _options_from_form(form, org["code"], tpl.get("options") or {})
    if not verr and vals["report_type"] != tpl["report_type"] and db.query_one(
            "SELECT 1 FROM reports.template_lines WHERE template_id = %s AND is_active",
            (template_id,)):
        verr = ("The report type can only be changed while the template has no active lines. "
                "Untick Active on its lines first, or Clone it.")
    if not verr and _name_taken(org["id"], vals["name"], except_id=template_id):
        verr = f"{org['code']} already has a template named '{vals['name']}'."
    if verr:
        from urllib.parse import quote_plus
        return RedirectResponse(base + "?error=" + quote_plus(verr), status_code=303)
    sets = ", ".join(f"{k} = %s" for k in vals) + ", updated_by_user_id = %s, updated_at = NOW()"
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE reports.templates SET {sets} WHERE id = %s AND org_id = %s",
                        list(vals.values()) + [user["id"], template_id, org["id"]])
    return RedirectResponse(base + "?saved=options", status_code=303)


@router.post("/admin/report-templates/{template_id}/schedule")
async def save_schedule(request: Request, template_id: int):
    user, org, err = _require_access(request)
    if err:
        return err
    if not _template(template_id, org["id"]):
        return _not_yours()
    form = await request.form()
    base = f"/admin/report-templates/{template_id}/edit"
    freq = str(form.get("frequency") or "").strip().lower()
    try:
        day = int(str(form.get("send_day_of_month") or "").strip())
    except ValueError:
        day = 0
    if freq not in _FREQUENCIES or not 1 <= day <= 28:
        return RedirectResponse(base + "?error=Pick+a+frequency+and+a+send+day+from+1+to+28.",
                                status_code=303)
    active = form.get("is_active") == "on"
    sched = _schedule(template_id)
    with db.connect() as conn:
        with conn.cursor() as cur:
            if sched:
                cur.execute(
                    """UPDATE reports.template_schedules
                          SET frequency = %s, send_day_of_month = %s, is_active = %s, updated_at = NOW()
                        WHERE id = %s AND template_id = %s""",
                    (freq, day, active, sched["id"], template_id))
            else:
                cur.execute(
                    """INSERT INTO reports.template_schedules
                           (template_id, frequency, send_day_of_month, is_active)
                       VALUES (%s, %s, %s, %s)""",
                    (template_id, freq, day, active))
    return RedirectResponse(base + "?saved=schedule", status_code=303)


@router.post("/admin/report-templates/{template_id}/toggle-active")
async def toggle_active(request: Request, template_id: int):
    user, org, err = _require_access(request)
    if err:
        return err
    tpl = _template(template_id, org["id"])
    if not tpl:
        return _not_yours()
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE reports.templates
                      SET is_active = NOT is_active, updated_by_user_id = %s, updated_at = NOW()
                    WHERE id = %s AND org_id = %s""",
                (user["id"], template_id, org["id"]))
    word = "deactivated" if tpl["is_active"] else "activated"
    form = await request.form()
    back = "/admin/report-templates" if form.get("from") == "list" else \
        f"/admin/report-templates/{template_id}/edit"
    return RedirectResponse(f"{back}?{'saved' if 'edit' in back else 'done'}={word}", status_code=303)


@router.post("/admin/report-templates/{template_id}/clone")
async def clone_template(request: Request, template_id: int):
    user, org, err = _require_access(request)
    if err:
        return err
    tpl = _template(template_id, org["id"])
    if not tpl:
        return _not_yours()
    name = f"Copy of {tpl['name']}"
    n = 2
    while _name_taken(org["id"], name):
        name = f"Copy of {tpl['name']} ({n})"
        n += 1
    copy_cols = ["description", "full_entity", "include_monthly_tab", "include_fund_tabs",
                 "include_txn_detail", "show_annual_budget", "show_accounts_under_lines",
                 "budget_name", "requires_review", "reviewer_user_id",
                 "cc_owner_on_auto_send", "sender_email", "report_type", "options"]
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO reports.templates
                        (org_id, name, is_active, created_by_user_id, updated_by_user_id, {', '.join(copy_cols)})
                    SELECT org_id, %s, FALSE, %s, %s, {', '.join(copy_cols)}
                      FROM reports.templates WHERE id = %s AND org_id = %s
                    RETURNING id""",
                (name, user["id"], user["id"], template_id, org["id"]))
            new_id = cur.fetchone()["id"]
            cur.execute(
                """INSERT INTO reports.template_lines
                       (template_id, section, fund_group, line_label, account_mask, sort_order, is_active)
                   SELECT %s, section, fund_group, line_label, account_mask, sort_order, TRUE
                     FROM reports.template_lines WHERE template_id = %s AND is_active""",
                (new_id, template_id))
            cur.execute(
                """INSERT INTO reports.template_recipients
                       (template_id, email, name, recipient_type, is_active)
                   SELECT %s, email, name, recipient_type, TRUE
                     FROM reports.template_recipients WHERE template_id = %s AND is_active""",
                (new_id, template_id))
            cur.execute(
                """INSERT INTO reports.template_schedules
                       (template_id, frequency, send_day_of_month, is_active)
                   SELECT %s, frequency, send_day_of_month, is_active
                     FROM reports.template_schedules WHERE template_id = %s AND is_active""",
                (new_id, template_id))
    return RedirectResponse(f"/admin/report-templates/{new_id}/edit?cloned=1", status_code=303)


# ── lines (batched JSON save) ─────────────────────────────────────────────────

def _clean_line(r: dict, report_type: str = "bva") -> tuple[dict | None, str | None]:
    if report_type == "fund_summary":
        section = None
        fund_group = str(r.get("fund_group") or "").strip() or None
        if fund_group and len(fund_group) > 120:
            return None, "Fund group is too long (120 characters at most)."
    else:
        fund_group = None
        section = {"revenue": "Revenue", "expense": "Expense"}.get(str(r.get("section") or "").strip().lower())
        if not section:
            return None, "Section must be Revenue or Expense."
    label = str(r.get("line_label") or "").strip()
    if not label:
        return None, "Line label is required."
    mask = str(r.get("account_mask") or "").strip()
    merr = report_masks.validate_mask(mask)
    if merr:
        return None, merr
    try:
        sort_order = int(str(r.get("sort_order") if r.get("sort_order") not in (None, "") else 100).strip())
    except ValueError:
        return None, "Sort must be a whole number."
    return {"section": section, "fund_group": fund_group, "line_label": label,
            "account_mask": ", ".join(report_masks.split_masks(mask)),
            "sort_order": sort_order, "is_active": bool(r.get("is_active", True))}, None


@router.post("/admin/report-templates/{template_id}/lines/save")
async def save_lines(request: Request, template_id: int):
    user, org, err = _require_access(request)
    if err:
        return err
    tpl = _template(template_id, org["id"])
    if not tpl:
        return _not_yours()
    rows = (await _json_body(request)).get("rows") or []
    if not rows:
        return JSONResponse({"error": "Nothing to save."}, status_code=400)

    cleaned, errors = [], {}
    for r in rows:
        key = str(r.get("key") or "")
        vals, verr = _clean_line(r, tpl["report_type"])
        if verr:
            errors[key] = verr
            continue
        rid = int(r.get("id") or 0)
        cleaned.append((key, rid, str(r.get("updated_at") or ""), vals))
    if errors:
        return JSONResponse({"error": "Fix the highlighted lines, then save again.",
                             "row_errors": errors}, status_code=400)

    stale = {}
    existing = {x["id"]: x for x in _lines(template_id)}
    for key, rid, loaded_at, _ in cleaned:
        if rid and rid not in existing:
            stale[key] = "This line no longer exists on this template."
        elif rid and existing[rid]["updated_at"] != loaded_at:
            stale[key] = "Changed elsewhere (the PG Data Review workbook?) since you opened this page."
    if stale:
        return JSONResponse({"error": "Some lines were changed by someone else after you opened "
                                      "this page. Nothing was saved -- reload to see their version.",
                             "row_errors": stale}, status_code=409)

    with db.connect() as conn:
        with conn.cursor() as cur:
            for _, rid, _, v in cleaned:
                if rid:
                    cur.execute(
                        """UPDATE reports.template_lines
                              SET section = %s, fund_group = %s, line_label = %s, account_mask = %s,
                                  sort_order = %s, is_active = %s, updated_at = NOW()
                            WHERE id = %s AND template_id = %s""",
                        (v["section"], v["fund_group"], v["line_label"], v["account_mask"],
                         v["sort_order"], v["is_active"], rid, template_id))
                else:
                    cur.execute(
                        """INSERT INTO reports.template_lines
                               (template_id, section, fund_group, line_label, account_mask,
                                sort_order, is_active)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (template_id, v["section"], v["fund_group"], v["line_label"],
                         v["account_mask"], v["sort_order"], v["is_active"]))
    return JSONResponse({"ok": True, "saved": len(cleaned), "lines": _lines(template_id)})


# ── recipients (batched JSON save) ────────────────────────────────────────────

@router.post("/admin/report-templates/{template_id}/recipients/save")
async def save_recipients(request: Request, template_id: int):
    user, org, err = _require_access(request)
    if err:
        return err
    if not _template(template_id, org["id"]):
        return _not_yours()
    rows = (await _json_body(request)).get("rows") or []
    if not rows:
        return JSONResponse({"error": "Nothing to save."}, status_code=400)

    current = {r["id"]: r for r in _recipients(template_id)}
    taken = {r["email"].lower(): r["id"] for r in current.values()}
    cleaned, errors = [], {}
    for r in rows:
        key = str(r.get("key") or "")
        rid = int(r.get("id") or 0)
        email = str(r.get("email") or "").strip()
        rtype = str(r.get("recipient_type") or "to").strip().lower()
        if not _EMAIL_RE.match(email):
            errors[key] = "Enter one valid email address."
        elif rtype not in ("to", "cc"):
            errors[key] = "Type must be To or Cc."
        elif rid and rid not in current:
            errors[key] = "This recipient no longer exists on this template."
        elif taken.get(email.lower()) not in (None, rid):
            errors[key] = "Already on this template's list -- switch that row back to Active instead."
        else:
            taken[email.lower()] = rid or -1
            cleaned.append((rid, {"email": email, "name": str(r.get("name") or "").strip() or None,
                                  "recipient_type": rtype, "is_active": bool(r.get("is_active", True))}))
    if errors:
        return JSONResponse({"error": "Fix the highlighted recipients, then save again.",
                             "row_errors": errors}, status_code=400)
    with db.connect() as conn:
        with conn.cursor() as cur:
            for rid, v in cleaned:
                if rid:
                    cur.execute(
                        """UPDATE reports.template_recipients
                              SET email = %s, name = %s, recipient_type = %s, is_active = %s
                            WHERE id = %s AND template_id = %s""",
                        (v["email"], v["name"], v["recipient_type"], v["is_active"], rid, template_id))
                else:
                    cur.execute(
                        """INSERT INTO reports.template_recipients
                               (template_id, email, name, recipient_type, is_active)
                           VALUES (%s, %s, %s, %s, %s)""",
                        (template_id, v["email"], v["name"], v["recipient_type"], v["is_active"]))
    rows_out = _recipients(template_id)
    return JSONResponse({"ok": True, "saved": len(cleaned), "recipients": rows_out})


# ── preview / starter lines / budgets (live QBO chart of accounts) ────────────

@router.post("/admin/report-templates/{template_id}/preview")
async def preview(request: Request, template_id: int):
    user, org, err = _require_access(request)
    if err:
        return err
    tpl = _template(template_id, org["id"])
    if not tpl:
        return _not_yours()
    body = await _json_body(request)
    lines, bad = [], {}
    for r in body.get("rows") or []:
        key = str(r.get("key") or "")
        if not r.get("is_active", True):
            continue
        merr = report_masks.validate_mask(str(r.get("account_mask") or ""))
        if merr:
            bad[key] = merr
            continue
        try:
            sort_order = int(str(r.get("sort_order") or 100))
        except ValueError:
            sort_order = 100
        lines.append({"key": key, "section": str(r.get("section") or ""),
                      "line_label": str(r.get("line_label") or key),
                      "account_mask": str(r.get("account_mask")), "sort_order": sort_order,
                      "id": int(r.get("id") or 0) or 10 ** 9})
    lines.sort(key=lambda x: (x["sort_order"], x["id"]))
    data, qerr = await _coa(org["code"], refresh=bool(body.get("refresh")))
    if qerr:
        return JSONResponse({"error": f"Couldn't load the chart of accounts from QuickBooks: {qerr}"},
                            status_code=502)
    if tpl["report_type"] == "fund_summary":
        # The Fund Summary engine only looks at ACTIVE Equity accounts
        # (reports/fund_summary.build_account_map), so preview exactly those.
        accounts = [a for a in data["accounts"]
                    if a.get("classification") == "Equity" and a.get("active", True)]
        for ln in lines:
            ln["section"] = "Equity"
        result = report_masks.preview_lines(accounts, lines, full_entity=False)
    else:
        accounts = data["accounts"]
        result = report_masks.preview_lines(accounts, lines,
                                            full_entity=bool(body.get("full_entity", tpl["full_entity"])))
    result["invalid"] = bad
    result["account_count"] = len(accounts)
    return JSONResponse(result)


@router.post("/admin/report-templates/{template_id}/starter-lines")
async def starter_lines(request: Request, template_id: int):
    user, org, err = _require_access(request)
    if err:
        return err
    if not _template(template_id, org["id"]):
        return _not_yours()
    body = await _json_body(request)
    section = str(body.get("section") or "both").lower()
    sections = {"revenue": ("Revenue",), "expense": ("Expense",)}.get(section, ("Revenue", "Expense"))
    prefix = re.sub(r"[^0-9.]", "", str(body.get("prefix") or ""))
    data, qerr = await _coa(org["code"])
    if qerr:
        return JSONResponse({"error": f"Couldn't load the chart of accounts from QuickBooks: {qerr}"},
                            status_code=502)
    rows = report_masks.starter_lines(
        data["accounts"], sections=sections, prefix=prefix,
        existing_masks=[str(m) for m in body.get("existing_masks") or []],
        include_inactive=bool(body.get("include_inactive")))
    return JSONResponse({"lines": rows})


@router.post("/admin/report-templates/{template_id}/fund-mask-lines")
async def fund_mask_lines(request: Request, template_id: int):
    """The entity's current fund_account_masks rows, shaped as draft lines for
    the Fund Summary grid ("Load current Fund Account Masks"). Nothing is saved."""
    user, org, err = _require_access(request)
    if err:
        return err
    if not _template(template_id, org["id"]):
        return _not_yours()
    rows = await asyncio.to_thread(cornerstone_mode.get_fund_account_masks, fund_mask_company(org))
    return JSONResponse({"company": fund_mask_company(org), "lines": [
        {"fund_group": m.get("fund_group") or "", "line_label": m.get("display_label") or m["account_mask"],
         "account_mask": m["account_mask"], "sort_order": int(m.get("sort_order") or 100)}
        for m in rows]})


@router.get("/admin/report-templates/api/qbo-reference")
async def qbo_reference(request: Request):
    user, org, err = _require_access(request)
    if err:
        return err
    data, qerr = await _coa(org["code"], refresh=request.query_params.get("refresh") == "1")
    if qerr:
        return JSONResponse({"error": qerr}, status_code=502)
    return JSONResponse({"budgets": data.get("budgets", []), "account_count": len(data["accounts"])})
