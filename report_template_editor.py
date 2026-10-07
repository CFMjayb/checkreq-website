"""
report_template_editor.py -- 26-149 Phase 3 (2026-09-23): create and edit
Actual vs Budget report templates in Beacon.

Jay, 2026-09-23: "start Phase 3, the template setup screen" (Plan.md Section 5,
items 1-3). Screens:
  * GET  /admin/report-templates/new             -- new-template form (Options)
  * GET  /admin/report-templates/{id}/edit       -- the editor: Options, Lines,
                                                    Recipients, Schedule
  * POST .../{id}/options | schedule             -- form posts; the editor sends them with
                                                    fetch() (Accept: application/json) and gets
                                                    JSON back, so saving one section never
                                                    reloads the page or touches the others
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
from decimal import Decimal, InvalidOperation

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
# Settings every report type has, kept in the same options JSON (checkbox name -> (key, default)).
# "Show whole dollars" (2026-10-01): the engine shows Page 1 / By Month / Fund Summary figures
# without cents; Transaction Detail keeps them. Needs no migration (options is already JSONB).
_COMMON_OPTIONS = {"opt_whole_dollars": ("whole_dollars", False)}
_ORG_CODE_TO_QBO_COMPANY = {"DME": "dmecdf"}

# Chart-of-accounts cache per org code: (fetched_at, data). Ten minutes is short
# enough that a new account shows up the same morning, long enough that a
# session of preview clicks costs one QBO pull, not one per click.
_COA_TTL = 600
_coa_cache: dict[str, tuple[float, dict]] = {}

# Line options (migration 071, 2026-10-01): % charged, class selection, Sum/Detail.
# The editor works whether or not the columns exist yet in the database it is running
# against (they reach production later than dev): reads never name them directly, writes
# touch them only when they exist, and non-default input is refused -- never silently
# dropped -- on a database that does not have them.
_LINE_OPTION_COLS = ("charge_pct", "class_filter", "display_mode")
_CLASS_TTL = 600
_class_cache: dict[str, tuple[float, dict]] = {}
_MAX_CLASSES = 60
_CLASS_ID_RE = re.compile(r"^[0-9]{1,30}$")        # QuickBooks class ids are numeric strings


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


def _option_columns() -> set[str]:
    """Which of the three line-option columns exist in the database this app is using."""
    rows = db.query(
        """SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'reports' AND table_name = 'template_lines'
              AND column_name = ANY(%s)""",
        (list(_LINE_OPTION_COLS),),
    )
    return {r["column_name"] for r in rows}


def _options_ready() -> bool:
    return _option_columns() == set(_LINE_OPTION_COLS)


def _pct_str(v) -> str:
    """'100.0000' -> '100', '33.3333' stays: a plain string, so no Decimal ever reaches JSON
    and an unchanged value never makes a row look edited."""
    try:
        return format(Decimal(str(v)).quantize(Decimal("0.0001")).normalize(), "f")
    except (InvalidOperation, ValueError):
        return "100"


def _lines(template_id: int) -> list[dict]:
    # to_jsonb(l) ->> 'col' yields NULL for a column that does not exist, so this read
    # works on a database that has not had migration 071 yet (-> the defaults below).
    rows = db.query(
        """SELECT l.id, l.section, l.fund_group, l.line_label, l.account_mask, l.sort_order,
                  l.is_active, l.updated_at,
                  to_jsonb(l) ->> 'charge_pct'   AS charge_pct,
                  to_jsonb(l) -> 'class_filter'  AS class_filter,
                  to_jsonb(l) ->> 'display_mode' AS display_mode
             FROM reports.template_lines l WHERE l.template_id = %s
         ORDER BY l.is_active DESC, l.sort_order, l.id""",
        (template_id,),
    )
    for r in rows:
        r["updated_at"] = _iso(r["updated_at"])
        r["charge_pct"] = _pct_str(r["charge_pct"]) if r.get("charge_pct") is not None else "100"
        cf = r.get("class_filter")
        if isinstance(cf, str):
            try:
                cf = json.loads(cf)
            except ValueError:
                cf = []
        r["class_filter"] = ([{"id": str(e.get("id") or ""), "name": str(e.get("name") or "")}
                              for e in cf if isinstance(e, dict)] if isinstance(cf, list) else [])
        r["display_mode"] = "sum" if r.get("display_mode") == "sum" else "detail"
    return rows


async def _classes(org_code: str, refresh: bool = False) -> tuple[dict | None, str | None]:
    """The entity's QuickBooks classes, cached ten minutes like the chart of accounts:
    {"all": {id: fully qualified name}, "active": {id, ...}}. Two calls (active only, then
    all) because the endpoint does not flag inactive classes -- a class made inactive later
    stays selectable on a line that already uses it, and is tagged in the picker."""
    key = org_code.upper()
    hit = _class_cache.get(key)
    if hit and not refresh and time.time() - hit[0] < _CLASS_TTL:
        return hit[1], None

    def fetch():
        act, err = qbo_mcp_client.get_report_classes(org_code, active_only=True)
        if err:
            return None, err
        allc, err = qbo_mcp_client.get_report_classes(org_code, active_only=False)
        if err:
            return None, err
        return {"all": {c["id"]: (c.get("fully_qualified_name") or c.get("name") or c["id"]) for c in allc},
                "active": {c["id"] for c in act}}, None

    data, err = await asyncio.to_thread(fetch)
    if err:
        return None, err
    _class_cache[key] = (time.time(), data)
    return data, None


def _clean_options(r: dict, stored: dict | None, classes: dict | None) -> tuple[dict, str | None]:
    """Validate the three line options a grid row carries. Only keys PRESENT in the row are
    returned, so an update writes only what the page actually sent (a stale tab, or any
    other client that does not know these fields, can never reset a stored value).
    Rejects rather than rounds or guesses: a blank or garbled percentage is an error, never 100."""
    out: dict = {}
    if "charge_pct" in r:
        raw = r["charge_pct"]
        if isinstance(raw, bool) or raw is None or not str(raw).strip():
            return {}, "% charged is required (enter 100 for the whole line)."
        try:
            d = Decimal(str(raw).strip())
        except InvalidOperation:
            return {}, "% charged must be a number between 0 and 100."
        if not d.is_finite() or d <= 0 or d > 100:
            return {}, "% charged must be above 0 and at most 100."
        if d.as_tuple().exponent < -4:
            return {}, "% charged can have at most 4 decimal places."
        out["charge_pct"] = d.quantize(Decimal("0.0001"))
    if "display_mode" in r:
        m = str(r["display_mode"] or "").strip().lower()
        if m not in ("detail", "sum"):
            return {}, "Show must be Detail or Sum."
        out["display_mode"] = m
    if "class_filter" in r:
        raw = r["class_filter"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw) if raw.strip() else []
            except ValueError:
                return {}, "The class selection isn't valid."
        if not isinstance(raw, list):
            return {}, "The class selection isn't valid."
        ids: list[str] = []
        for e in raw:
            i = e.get("id") if isinstance(e, dict) else e
            i = "" if i is None else str(i).strip()
            if i and not _CLASS_ID_RE.match(i):
                return {}, "The class selection isn't valid."
            if i not in ids:
                ids.append(i)
        if len(ids) > _MAX_CLASSES:
            return {}, f"Select at most {_MAX_CLASSES} classes."
        stored_names = {e["id"]: e["name"] for e in (stored or {}).get("class_filter", [])}
        known = (classes or {}).get("all", {})
        entries = []
        for i in ids:
            if i == "":
                entries.append({"id": "", "name": "(No class)"})
            elif i in known:
                entries.append({"id": i, "name": known[i]})            # name always from QuickBooks
            elif i in stored_names:
                entries.append({"id": i, "name": stored_names[i]})     # already on the line, unchanged
            elif classes is None:
                return {}, "Couldn't check the classes with QuickBooks just now. Try again in a minute."
            else:
                return {}, "One of the selected classes isn't in QuickBooks."
        out["class_filter"] = entries
    return out, None


def _is_default_options(o: dict) -> bool:
    return (o.get("charge_pct", Decimal(100)) == Decimal(100) and not o.get("class_filter")
            and o.get("display_mode", "detail") == "detail")


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
    for field, (key, _default) in _COMMON_OPTIONS.items():
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
                            for field, (key, _d) in {**_TYPE_OPTIONS.get(fdict.get("report_type"), {}),
                                                     **_COMMON_OPTIONS}.items()}
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
        # The % / Class / Show columns exist only for Actual vs Budget lines, and only
        # once migration 071 is on this database.
        "line_options": tpl["report_type"] == "bva" and _options_ready(),
        "lines": lines,
        "recipients": _recipients(template_id),
        "schedule": sched,
        "reviewers": _reviewer_choices(org["id"], tpl["reviewer_user_id"]),
        "senders": _SENDERS,
        "frequencies": _FREQUENCIES,
        "error": request.query_params.get("error"),
    })


def _wants_json(request: Request) -> bool:
    """True when the editor page saved this section with fetch() (it sends Accept: application/json).

    Options and Schedule used to be plain form posts that always answered with a redirect, which
    reloaded the whole editor and threw away any unsaved Lines or Recipients (Jay, 2026-10-07: "I just
    lost all my work in the other sections"). The page now saves each section in place and reads JSON
    back; a plain form post (the new-template page, or a browser with scripts off) still redirects."""
    return "application/json" in (request.headers.get("accept") or "").lower()


def _save_failed(request: Request, base: str, message: str, status: int = 400):
    """The same refusal either way: JSON for the in-place save, a redirect banner for a plain post."""
    if _wants_json(request):
        return JSONResponse({"error": message}, status_code=status)
    from urllib.parse import quote_plus
    return RedirectResponse(base + "?error=" + quote_plus(message), status_code=303)


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
        if _wants_json(request):
            # The page is still open with unsaved work in other sections, so don't tell them to
            # simply reload -- that is exactly what throws that work away.
            return _save_failed(request, base,
                                "Someone else saved these options after you opened the page. Save your Lines "
                                "and Recipients first, then reload the page and make this change again.", 409)
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
        return _save_failed(request, base, verr)
    sets = ", ".join(f"{k} = %s" for k in vals) + ", updated_by_user_id = %s, updated_at = NOW()"
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE reports.templates SET {sets} WHERE id = %s AND org_id = %s",
                        list(vals.values()) + [user["id"], template_id, org["id"]])
    if _wants_json(request):
        # Hand back what the open page needs to stay in step: the new stale-edit stamp (without it the
        # very next Options save would be refused), and the few settings other sections read.
        fresh = _template(template_id, org["id"]) or tpl
        return JSONResponse({"ok": True, "saved": "options",
                             "updated_at": _iso(fresh["updated_at"]), "name": fresh["name"],
                             "report_type": fresh["report_type"], "full_entity": bool(fresh["full_entity"]),
                             "show_accounts_under_lines": bool(fresh["show_accounts_under_lines"])})
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
        return _save_failed(request, base, "Pick a frequency and a send day from 1 to 28.")
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
    if _wants_json(request):
        return JSONResponse({"ok": True, "saved": "schedule", "frequency": freq,
                             "send_day_of_month": day, "is_active": active})
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
            # A copy keeps each line's % charged, class selection and Sum/Detail (they are
            # the natural way to split an account, so a clone must not reset them).
            opt = ", charge_pct, class_filter, display_mode" if _options_ready() else ""
            cur.execute(
                f"""INSERT INTO reports.template_lines
                       (template_id, section, fund_group, line_label, account_mask, sort_order, is_active{opt})
                   SELECT %s, section, fund_group, line_label, account_mask, sort_order, TRUE{opt}
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

    # A row flagged _delete is being removed: it is never validated (it is going away) and
    # is deleted together with the other changes, in the same transaction, or not at all.
    removals = [r for r in rows if r.get("_delete")]
    rows = [r for r in rows if not r.get("_delete")]

    existing = {x["id"]: x for x in _lines(template_id)}
    bva = tpl["report_type"] == "bva"
    ready = _options_ready() if bva else False

    # Class names come from QuickBooks, never from the browser. Ask QuickBooks (cached)
    # only when this save adds a class a line doesn't already have.
    classes = None
    if bva and ready:
        def _new_ids(r):
            cf = r.get("class_filter")
            if isinstance(cf, str):
                try:
                    cf = json.loads(cf) if cf.strip() else []
                except ValueError:
                    return set()
            stored = {e["id"] for e in (existing.get(int(r.get("id") or 0)) or {}).get("class_filter", [])}
            return {str((e.get("id") if isinstance(e, dict) else e) or "") for e in (cf or [])} - {""} - stored
        if any(_new_ids(r) for r in rows):
            classes, _cerr = await _classes(org["code"])

    cleaned, errors = [], {}
    for r in rows:
        key = str(r.get("key") or "")
        vals, verr = _clean_line(r, tpl["report_type"])
        opts = {}
        if not verr and bva:
            opts, verr = _clean_options(r, existing.get(int(r.get("id") or 0)), classes)
            if not verr and opts and not ready and not _is_default_options(opts):
                verr = ("Line options (% charged, class, Sum) need database migration 071, which "
                        "this environment doesn't have yet.")
        if verr:
            errors[key] = verr
            continue
        rid = int(r.get("id") or 0)
        cleaned.append((key, rid, str(r.get("updated_at") or ""), vals, opts))
    if errors:
        return JSONResponse({"error": "Fix the highlighted lines, then save again.",
                             "row_errors": errors}, status_code=400)

    stale = {}
    for key, rid, loaded_at, _, _o in cleaned:
        if rid and rid not in existing:
            stale[key] = "This line no longer exists on this template."
        elif rid and existing[rid]["updated_at"] != loaded_at:
            stale[key] = "Changed elsewhere (the PG Data Review workbook?) since you opened this page."

    # Removals: a line that is already gone is simply skipped (the goal is met); a line that
    # someone changed after this page loaded is refused, like any other edit, so nobody
    # deletes a version of the line they never saw.
    remove_ids = []
    for r in removals:
        try:
            rid = int(r.get("id") or 0)
        except (TypeError, ValueError):
            rid = 0
        if not rid or rid not in existing:
            continue
        if existing[rid]["updated_at"] != str(r.get("updated_at") or ""):
            stale[str(r.get("key") or "")] = "Changed elsewhere since you opened this page, so it was not removed."
            continue
        remove_ids.append(rid)
    if stale:
        return JSONResponse({"error": "Some lines were changed by someone else after you opened "
                                      "this page. Nothing was saved -- reload to see their version.",
                             "row_errors": stale}, status_code=409)

    with db.connect() as conn:
        with conn.cursor() as cur:
            for rid in remove_ids:
                cur.execute("DELETE FROM reports.template_lines WHERE id = %s AND template_id = %s",
                            (rid, template_id))
            for _, rid, _, v, o in cleaned:
                if rid:
                    # An update writes a line option only if the page sent it (a stale tab
                    # that doesn't know these fields can never reset a stored value).
                    sets = ("section = %s, fund_group = %s, line_label = %s, account_mask = %s, "
                            "sort_order = %s, is_active = %s, updated_at = NOW()")
                    params = [v["section"], v["fund_group"], v["line_label"], v["account_mask"],
                              v["sort_order"], v["is_active"]]
                    if ready:
                        if "charge_pct" in o:
                            sets += ", charge_pct = %s"
                            params.append(o["charge_pct"])
                        if "class_filter" in o:
                            sets += ", class_filter = %s::jsonb"
                            params.append(json.dumps(o["class_filter"]))
                        if "display_mode" in o:
                            sets += ", display_mode = %s"
                            params.append(o["display_mode"])
                    cur.execute(f"UPDATE reports.template_lines SET {sets} "
                                "WHERE id = %s AND template_id = %s", params + [rid, template_id])
                elif ready:
                    cur.execute(
                        """INSERT INTO reports.template_lines
                               (template_id, section, fund_group, line_label, account_mask,
                                sort_order, is_active, charge_pct, class_filter, display_mode)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)""",
                        (template_id, v["section"], v["fund_group"], v["line_label"],
                         v["account_mask"], v["sort_order"], v["is_active"],
                         o.get("charge_pct", Decimal(100)), json.dumps(o.get("class_filter", [])),
                         o.get("display_mode", "detail")))
                else:
                    cur.execute(
                        """INSERT INTO reports.template_lines
                               (template_id, section, fund_group, line_label, account_mask,
                                sort_order, is_active)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (template_id, v["section"], v["fund_group"], v["line_label"],
                         v["account_mask"], v["sort_order"], v["is_active"]))
    if remove_ids:       # no audit table covers templates; leave a trace in the service log
        print(f"[report_template_editor] {user['email']} removed {len(remove_ids)} line(s) "
              f"{remove_ids} from template {template_id}")
    return JSONResponse({"ok": True, "saved": len(cleaned), "removed": len(remove_ids),
                         "lines": _lines(template_id)})


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

    # Rows flagged _delete are removed (never validated). A removed recipient's address is
    # free again, so the same save may add it back; one already gone is simply skipped.
    remove_ids = []
    for r in rows:
        if not r.get("_delete"):
            continue
        try:
            rid = int(r.get("id") or 0)
        except (TypeError, ValueError):
            rid = 0
        if rid and rid in current:
            remove_ids.append(rid)
    rows = [r for r in rows if not r.get("_delete")]

    taken = {r["email"].lower(): r["id"] for r in current.values() if r["id"] not in remove_ids}
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
            for rid in remove_ids:
                cur.execute("DELETE FROM reports.template_recipients WHERE id = %s AND template_id = %s",
                            (rid, template_id))
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
    if remove_ids:
        print(f"[report_template_editor] {user['email']} removed {len(remove_ids)} recipient(s) "
              f"{remove_ids} from template {template_id}")
    rows_out = _recipients(template_id)
    return JSONResponse({"ok": True, "saved": len(cleaned), "removed": len(remove_ids),
                         "recipients": rows_out})


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
        cf = r.get("class_filter")
        if isinstance(cf, str):
            try:
                cf = json.loads(cf) if cf.strip() else []
            except ValueError:
                cf = []
        class_ids = [str((e.get("id") if isinstance(e, dict) else e) or "") for e in (cf or [])] \
            if isinstance(cf, list) else []
        lines.append({"key": key, "section": str(r.get("section") or ""),
                      "line_label": str(r.get("line_label") or key),
                      "account_mask": str(r.get("account_mask")), "sort_order": sort_order,
                      "class_ids": class_ids or None,
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


@router.get("/admin/report-templates/api/classes")
async def qbo_classes(request: Request):
    """The current entity's QuickBooks classes for the Lines grid's Class picker:
    {"classes": [{id, name, active}]} sorted by name. Inactive classes are included
    (flagged) so a line that already uses one can still show and drop it."""
    user, org, err = _require_access(request)
    if err:
        return err
    data, qerr = await _classes(org["code"], refresh=request.query_params.get("refresh") == "1")
    if qerr:
        return JSONResponse({"error": f"Couldn't load the classes from QuickBooks: {qerr}"}, status_code=502)
    rows = [{"id": i, "name": n, "active": i in data["active"]} for i, n in data["all"].items()]
    rows.sort(key=lambda c: c["name"].lower())
    return JSONResponse({"classes": rows})


@router.get("/admin/report-templates/api/qbo-reference")
async def qbo_reference(request: Request):
    user, org, err = _require_access(request)
    if err:
        return err
    data, qerr = await _coa(org["code"], refresh=request.query_params.get("refresh") == "1")
    if qerr:
        return JSONResponse({"error": qerr}, status_code=502)
    return JSONResponse({"budgets": data.get("budgets", []), "account_count": len(data["accounts"])})
