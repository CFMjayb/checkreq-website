"""
payroll_screens.py -- the hr_admin screens for emailed and period-total hours (26-158, plan B3),
plus the parish's read-only view of what the diocese recorded.

New file, per the standing rule: the existing timekeeping screens gain only wiring (one include on
the backfill grid, extra columns on the status board). All rules live in payroll_totals.py.

ROUTES (all hr_admin or beacon_admin at the diocese, except the parish view):
  POST /admin/timekeeping/status/{period}/{parish}/totals/accept-all     accept every plain line
  POST /admin/timekeeping/status/{period}/{parish}/totals/{line}/{act}   accept, reject, edit, ...
  POST /admin/timekeeping/status/{period}/standing                       carry forward standing hours
  POST /admin/timekeeping/status/{period}/final | unfinal                mark the hours Final
  GET  /admin/timekeeping/status/{period}/variance                       Beacon vs the paid register
  GET/POST /admin/timekeeping/parish-profile/{parish}[/save|/sender-add|/sender-remove]
  GET  /timekeeping/received                                             the parish's read-only view

Register this BEFORE timekeeping_status: its literal /variance, /final, /standing paths must be
matched before the generic /status/{period}/{parish} route.
"""
from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import category_order
import cornerstone_mode
import db
import email_pdf
import org_time
import payroll_decisions as pdec
import payroll_totals as pt
import rbac
import registry
import timekeeping
import timekeeping_roster_changes

router = APIRouter()

_current_user = None
_current_org = None
_render = None

PATTERN_LABELS = {
    "every_period": "Every period",
    "most_periods": "Most periods",
    "two_period_batches": "Two periods at a time",
    "standing_hours": "Standing hours (the same each period)",
    "occasional": "Occasionally",
    "none": "No hours expected",
}
NOT_EXPECTED_PATTERNS = ("standing_hours", "none")
CHANNEL_LABELS = {"email": "Email", "parish": "Parish (in Beacon)", "diocese": "Diocese", "standing": "Standing hours",
                  "register": "Paid register"}
# the order of the kinds of hours (Reg, OT, Sick, Vac, Holiday) now lives in category_order.py, shared with the daily grid below
STATE_LABELS = {
    "ok": "OK", "needs_review": "Needs review", "proposal": "Different figure waiting",
    "conflict": "Conflict with the daily grid", "pending_hire": "New hire not on the roster yet",
}


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    import closeout_screens  # the close-out checklist (26-158); registered here so it sits before /status/{period}/{parish}
    closeout_screens.register(app, current_user=current_user, current_org=current_org, render=render, gate=_require_hr_admin)
    import hours_history  # the hours history screens (26-158): one person / one department, a row per pay period
    hours_history.register(app, current_user=current_user, current_org=current_org, render=render, gate=_require_hr_admin)
    app.include_router(router)


def _require_hr_admin(request: Request):
    """(user, org, None) when allowed, (None, None, response) when not. Same rule as every other
    diocese-wide Timekeeping screen: hr_admin or beacon_admin, never a Cornerstone-Mode entity."""
    user = _current_user(request)
    if not user:
        return None, None, RedirectResponse("/login")
    org = _current_org(request)
    org_id = org["id"] if org else None
    if org_id is None or cornerstone_mode.is_cornerstone_org(org_id):
        return None, None, RedirectResponse("/portal")
    if not rbac.user_has_any_role(user["id"], ["hr_admin", "beacon_admin"], org_id=org_id):
        return None, None, _forbidden()
    return user, org, None


def _forbidden():
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "HR Admin access required"}, status_code=403)


def _back(period_id: int, parish_id: int, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    q = ""
    if msg:
        q = "?pmsg=" + quote_plus(msg)
    elif err:
        q = "?perr=" + quote_plus(err)
    return RedirectResponse(f"/admin/timekeeping/status/{period_id}/{parish_id}{q}", status_code=303)


def _status_back(period_id: int, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    q = ""
    if msg:
        q = "?saved=" + quote_plus(msg)
    elif err:
        q = "?error=" + quote_plus(err)
    return RedirectResponse(f"/admin/timekeeping/status/{period_id}{q}", status_code=303)


# ---------------------------------------------- helpers the existing screens call

_EMAIL_CHECK_TIPS = {
    "email_match": "Email check: matches the email.",
    "email_differs": "Email check: the email shows a different number.",
    "email_named_no_number": "Email check: named in an email, no number found.",
    "email_standing": "Email check: standing hours.",
    "email_none": "Email check: no email found.",
}


def _line_tip(r: dict) -> str:
    """What the hover tip over a line's Source says: the sentence from the email, the sentence a new figure came
    from, and the email-check result (kept from the one-row-per-line layout)."""
    parts = []
    if r.get("quote"):
        parts.append("\u201c" + r["quote"] + "\u201d")
    if r.get("proposed_quote"):
        parts.append("New figure: \u201c" + r["proposed_quote"] + "\u201d")
    for f in r.get("flags") or []:
        if f in _EMAIL_CHECK_TIPS:
            parts.append(_EMAIL_CHECK_TIPS[f])
    return "\n".join(parts)


def people_view(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """The period totals as ONE ROW PER EMPLOYEE (Jay, 2026-10-11): each kind of hours is a column, so a person's
    Regular and Overtime sit on the same row. Returns (people, columns). Every line keeps its own actions inside
    the person's row."""
    cats: dict = {}
    groups: dict = {}
    order: list = []
    for r in rows:
        cats[r["category_key"]] = (r["category_sort"] or 0, r["category_label"])
        key = ("s", r["staff_id"]) if r["staff_id"] else ("p", r["pending_change_id"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"last_name": r["last_name"], "first_name": r["first_name"],
                               "employee_number": r["employee_number"], "cells": {}, "lines": [], "pdfs": [],
                               "sources": [], "tips": [], "staff_id": r["staff_id"], "pending_change_id": r["pending_change_id"]}
            order.append(key)
        g["cells"][r["category_key"]] = r
        g["lines"].append(r)
        if r.get("pdf_url") and r["pdf_url"] not in g["pdfs"]:
            g["pdfs"].append(r["pdf_url"])
        if r.get("source") and r["source"] not in g["sources"]:
            g["sources"].append(r["source"])
        t = _line_tip(r)
        if t and t not in g["tips"]:
            g["tips"].append(t)
    people = []
    for key in order:
        g = groups[key]
        flags = {f for ln in g["lines"] for f in (ln.get("flags") or [])}
        bad = [ln for ln in g["lines"] if ln["state"] != "ok"]
        g["attn"] = bool(bad)
        g["state_label"] = STATE_LABELS.get((bad[0] if bad else g["lines"][0])["state"], "") if g["lines"] else ""
        g["tip"] = "\n\n".join(g["tips"])
        g["auto_accepted"] = "auto_accepted" in flags
        g["jump"] = "jump" in flags
        g["carried_in"] = "carried_in" in flags
        g["inactive"] = "inactive_employee" in flags
        g["not_hourly"] = "not_hourly" in flags
        g["by_elimination"] = any((ln.get("quote") or "").startswith("(written as") for ln in g["lines"])
        people.append(g)
    cols = [{"key": k, "label": v[1]} for k, v in sorted(cats.items(), key=lambda kv: (kv[1][0], kv[1][1]))]
    return people, cols


def parish_approval_context(org_id: int, period: dict, parish_id: int) -> dict | None:
    """What the parish's own page needs for its Approve department button (None when the period is not open)."""
    if period["status"] != "open":
        return None
    try:
        return pt.parish_approval_state(org_id, period["id"], parish_id)
    except pt.PayrollError:
        return None


def section_context(org_id: int, period: dict, parish_id: int) -> dict:
    """What _payroll_totals_section.html shows on the diocese's backfill grid for one parish."""
    rows = [r for r in pt.period_rows(org_id, period["id"]) if r["parish_id"] == parish_id and r["line_id"] is not None]
    for r in rows:
        # The email's stored PDF (a masked copy the reader drew), or None when none is stored yet.
        r["pdf_url"] = email_pdf.url_for(org_id, r["source_ref"])
        r["proposed_pdf_url"] = email_pdf.url_for(org_id, r["proposed_ref"])
        r["state_label"] = STATE_LABELS.get(r["state"], r["state"])
    # People the emails named that nobody entered yet: shown beside the same PDF as the lines that were entered.
    unentered = pt.list_unentered(org_id, period["id"], parish_id)
    for u in unentered:
        u["pdf_url"] = email_pdf.url_for(org_id, u["source_ref"])
    roster = db.query("SELECT id, first_name, last_name, employee_number, captures_hours FROM portal.staff_roster "
                      "WHERE parish_id = %s AND is_active AND employee_number IS NOT NULL "
                      "ORDER BY captures_hours DESC, last_name, first_name", (parish_id,)) if unentered else []
    people, cat_cols = people_view(rows)
    # Every kind of hours is a column (like the summary grid), so a figure can be typed into any of them.
    have = {c["key"] for c in cat_cols}
    for c in db.query("SELECT key, label FROM portal.timekeeping_categories WHERE org_id = %s AND is_active "
                      "ORDER BY sort_order, label", (org_id,)):
        if c["key"] not in have:
            cat_cols.append({"key": c["key"], "label": c["label"]})
    cat_cols.sort(key=lambda c: (category_order.rank(c["key"]), c["label"]))
    for c in cat_cols:
        c["short"] = category_order.short(c["key"], c["label"])
    writable = period["status"] in pt.WRITABLE_PERIOD_STATUSES
    add_roster = db.query("SELECT id, first_name, last_name, employee_number, captures_hours FROM portal.staff_roster "
                          "WHERE parish_id = %s AND is_active AND employee_number IS NOT NULL "
                          "ORDER BY captures_hours DESC, last_name, first_name", (parish_id,)) if writable else []
    emails = []
    if writable:
        zone = org_time.zone_name_for_org(org_id)
        for e in pt.parish_email_choices(org_id, period["id"], parish_id):
            emails.append({"ref": e["source_ref"], "label": ("Email received " + org_time.format_local(e["received_at"], zone))
                           if e["received_at"] else "Email behind the lines above"})
    # The parish's standing hours, shown BEFORE anyone confirms them so they can be reviewed (Jay, 2026-10-12: "they don't show at all on the detail").
    standing = []
    by_staff = {r["staff_id"]: r for r in rows if r["category_key"] == "regular" and r["staff_id"] is not None}
    for s in db.query("SELECT id, first_name, last_name, employee_number, standing_hours FROM portal.staff_roster "
                      "WHERE parish_id = %s AND is_active AND captures_hours AND hours_basis = 'standing' "
                      "AND standing_hours IS NOT NULL AND employee_number IS NOT NULL ORDER BY last_name, first_name", (parish_id,)):
        line = by_staff.get(s["id"])
        if line is None:
            status = "pending"
        elif line["source"] != "standing":
            status = "other"
        elif line["review_status"] == "confirmed":
            status = "confirmed"
        else:
            status = "waiting"
        standing.append({"id": s["id"], "name": f"{s['last_name']}, {s['first_name']}", "employee_number": s["employee_number"],
                         "hours": float(s["standing_hours"]), "status": status})
    return {"rows": rows, "people": people, "cat_cols": cat_cols, "unentered": unentered, "roster": roster, "writable": writable,
            "parish_id": parish_id, "period_id": period["id"], "add_roster": add_roster, "emails": emails,
            "standing": standing, "standing_total": sum(s["hours"] for s in standing),
            "standing_open": sum(1 for s in standing if s["status"] in ("pending", "waiting"))}


def board_rows(org_id: int, period: dict, rows: list[dict]) -> list[dict]:
    """Adds the payroll columns to the status-board rows (in place, and returned)."""
    extras = pt.board_extras(org_id, period["id"])
    approvals = pt.approvals_for_period(org_id, period["id"])
    zone = org_time.zone_name_for_org(org_id)
    for r in rows:
        e = extras.get(r["parish_id"], {})
        r["pattern"] = e.get("pattern")
        r["pattern_label"] = PATTERN_LABELS.get(e.get("pattern"))
        srcs = sorted(e.get("sources") or ())
        r["channel"] = e.get("channel") or (srcs[0] if len(srcs) == 1 else None)
        r["channel_label"] = CHANNEL_LABELS.get(r["channel"], "Mixed" if len(srcs) > 1 else "")
        # Period totals count too: the board used to show only daily-grid hours, so a period holding
        # emailed or loaded hours read "Not Started" and 0.00.
        r["recorded"] = e.get("recorded", 0) > 0
        if r["recorded"]:
            r["total_hours"] = float(e["hours"])
        r["emails"] = e.get("emails", 0)
        r["first_received"] = org_time.format_local(e.get("first_received_at"), zone) if e.get("first_received_at") else None
        # a person an email named that nobody entered counts as something to review, so the row turns red
        r["needs_review"] = e.get("needs_review", 0) + e.get("pending_hires", 0) + e.get("unentered", 0)
        r["conflicts"] = e.get("conflicts", 0)
        r["proposals"] = e.get("proposals", 0)
        r["by_email"] = (e.get("channel") == "email" and r.get("submission_status") != "submitted")
        # emailed, but nothing could be recorded from it yet: a person has to look (the row is red for the same reason)
        r["named_only"] = bool(r["emails"] and not r["recorded"] and not r.get("submission_id"))
        # standing hours and everything else, side by side (Std, Entered, Total)
        r["std_hours"] = float(e["std"]) if e.get("std") else None
        r["entered_hours"] = float(e["entered"]) if e.get("entered") else None
        r["std_expected"] = float(e["standing_expected"]) if e.get("standing_people") else None
        # the ONE rule for the row's colour and pill, shared with Mark hours Final (pt.classify_parish)
        a = approvals.get(r["parish_id"])
        c = pt.classify_parish(e, r.get("submission_id"), r.get("submission_status"), a)
        r["state"], r["pill"], r["done"] = c["state"], c["pill"], c["done"]
        r["only_std"], r["stale"], r["approval_kind"] = c["only_std"], c["stale"], c["approval_kind"]
        r["not_expected"] = c["pill"] == "not_expected"
        if r["approval_kind"] == "no_hours" and not r["recorded"]:
            r["total_hours"] = 0
        r["approval_tip"] = ("by " + (a["display_name"] or a["email"] or "someone") + " on "
                             + org_time.format_local(a["approved_at"], zone)) if a else None
    return rows


def board_summary(org_id: int, period: dict, rows: list[dict] | None = None) -> dict:
    prows = pt.period_rows(org_id, period["id"])
    return {"left": len(pt.unresolved(prows)) + sum(pt.count_unentered(org_id, period["id"]).values()),
            "not_done": sum(1 for r in (rows or []) if not r.get("done")),
            "final": bool(period.get("hours_finalized_at")),
            "final_at": org_time.format_local(period.get("hours_finalized_at"), org_time.zone_name_for_org(org_id))
            if period.get("hours_finalized_at") else None}


# ------------------------------------------------------------------ the stored email PDF

@router.get("/admin/timekeeping/email/{token}.pdf")
def email_pdf_view(token: str, request: Request):
    """The masked PDF copy of one payroll email. HR Admin or Beacon Admin of THIS diocese only; the
    diocese comes from the signed-in session, so a token for another diocese's email finds nothing."""
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    data = email_pdf.fetch(org["id"], token)
    if data is None:
        return Response("That email copy was not found.", status_code=404, media_type="text/plain",
                        headers={"Cache-Control": "private, no-store"})
    return Response(content=data, media_type="application/pdf", headers={
        "Content-Disposition": 'inline; filename="payroll-email.pdf"',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


# ------------------------------------------------------------------ review actions

# ------------------------------------------------------------------ the Payroll notes page

def notes_groups(org_id: int, period_id: int) -> list[dict]:
    """The period's notes grouped by department, then by email: [{label, emails: [{received, sender, subject, pdf_url, items}]}]."""
    zone = org_time.zone_name_for_org(org_id)
    groups: dict = {}
    order: list = []
    for n in pt.list_email_notes(org_id, period_id):
        gkey = n["parish_code"] or ""
        g = groups.get(gkey)
        if g is None:
            g = groups[gkey] = {"label": (f"{n['parish_code']} {n['parish_name']}" if n["parish_code"] else "Not tied to one department"),
                                "full": n.get("parish_full_name"), "emails": {}}
            order.append(gkey)
        e = g["emails"].get(n["source_ref"])
        if e is None:
            e = g["emails"][n["source_ref"]] = {
                "received": org_time.format_local(n["received_at"], zone) if n.get("received_at") else None,
                "sender": n.get("email_sender"), "subject": n.get("email_subject"),
                "pdf_url": email_pdf.url_for(org_id, n["source_ref"]), "items": []}
        e["items"].append(n)
    out = []
    for k in order:
        g = groups[k]
        out.append({"label": g["label"], "full": g["full"], "emails": list(g["emails"].values())})
    return out


@router.get("/admin/timekeeping/status/{period_id}/notes", response_class=HTMLResponse)
def notes_page(period_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    try:
        period = pt.get_period(org["id"], period_id)
        groups = notes_groups(org["id"], period_id)
    except pt.PayrollError as e:
        return _status_back(period_id, err=e.message)
    return _render(request, "timekeeping_notes.html", user,
                   {"current_org": org, "period": period, "groups": groups,
                    "total": sum(len(e["items"]) for g in groups for e in g["emails"])})


@router.get("/admin/timekeeping/status/{period_id}/notes.xlsx")
def notes_export(period_id: int, request: Request):
    import payroll_export
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    try:
        period = pt.get_period(org["id"], period_id)
        notes = pt.list_email_notes(org["id"], period_id)
    except pt.PayrollError as e:
        return _status_back(period_id, err=e.message)
    data = payroll_export.build_notes_workbook(org, period, notes, org_time.zone_name_for_org(org["id"]), str(request.base_url).rstrip("/"))
    return Response(content=data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{payroll_export.notes_filename_for(org, period)}"',
                             "Cache-Control": "private, no-store"})


@router.post("/admin/timekeeping/status/{period_id}/{parish_id}/totals/accept-all")
async def totals_accept_all(period_id: int, parish_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    try:
        res = pt.accept_all(org["id"], period_id, parish_id, user["id"])
    except pt.PayrollError as e:
        return _back(period_id, parish_id, err=e.message)
    msg = f"Accepted {res['accepted']} line(s)."
    if res["skipped"]:
        msg += f" {res['skipped']} need a look on their own (flagged, a different figure waiting, or a conflict)."
    return _back(period_id, parish_id, msg=msg)


@router.post("/admin/timekeeping/status/{period_id}/{parish_id}/unentered/{item_id}/dismiss")
async def unentered_dismiss(period_id: int, parish_id: int, item_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    form = await request.form()
    try:
        pt.dismiss_unentered(org["id"], item_id, user["id"], note=form.get("note"))
    except pt.PayrollError as e:
        return _back(period_id, parish_id, err=e.message)
    return _back(period_id, parish_id, msg="Dismissed. It will not show again.")


@router.post("/admin/timekeeping/status/{period_id}/{parish_id}/unentered/{item_id}/assign")
async def unentered_assign(period_id: int, parish_id: int, item_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    form = await request.form()
    try:
        staff_id = int(form.get("staff_id") or 0)
    except ValueError:
        staff_id = 0
    try:
        res = pt.assign_unentered(org["id"], item_id, staff_id, user["id"], hours=form.get("hours"))
    except pt.PayrollError as e:
        return _back(period_id, parish_id, err=e.message)
    return _back(period_id, parish_id, msg=f"Entered {len(res['entered'])} line(s) for the employee you chose.")


@router.post("/admin/timekeeping/status/{period_id}/{parish_id}/unentered/{item_id}/reactivate")
async def unentered_reactivate(period_id: int, parish_id: int, item_id: int, request: Request):
    """The name in the email is an INACTIVE roster person: reactivate her through the Roster Review path (so it is logged
    like any reactivation) and her hours from the email are entered, to wait for review."""
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    item = db.query_one("SELECT u.*, p.code AS parish_code FROM portal.time_email_unentered u JOIN portal.parishes p ON p.id = u.parish_id "
                        "WHERE u.id = %s AND u.parish_id = %s AND p.org_id = %s", (item_id, parish_id, org["id"]))
    if not item or item["status"] != "open":
        return _back(period_id, parish_id, err="That item is not waiting any more.")
    staff = db.query_one("SELECT id, first_name, last_name, employee_number, is_active FROM portal.staff_roster "
                         "WHERE id = %s AND parish_id = %s", (item["suggested_staff_id"], parish_id)) if item["suggested_staff_id"] else None
    if not staff or staff["is_active"]:
        return _back(period_id, parish_id, err="There is no inactive roster person to reactivate for this name.")
    if not staff["employee_number"]:
        return _back(period_id, parish_id, err="She has no employee number yet. Reactivate her on the Employees screen.")
    try:
        pend = db.query_one("SELECT id FROM portal.staff_roster_changes WHERE staff_id = %s AND change_type = 'reactivate' "
                            "AND status = 'pending' ORDER BY id LIMIT 1", (staff["id"],))
        change_id = pend["id"] if pend else pt.propose_roster_change(
            org["id"], parish_code=item["parish_code"], change_type="reactivate", source_ref=item["source_ref"],
            quote=item["quote"], employee_number=staff["employee_number"], first_name=staff["first_name"],
            last_name=staff["last_name"], period_id=period_id, actor_user_id=user["id"]).get("change_id")
    except pt.PayrollError as e:
        return _back(period_id, parish_id, err=e.message)
    change = timekeeping_roster_changes.get_change_for_review(change_id, org["id"]) if change_id else None
    if change is None:
        return _back(period_id, parish_id, err="Could not find the Reactivate request. Use the Employees screen.")
    timekeeping_roster_changes.approve_change(change, user["id"], "Reactivated from the Time Review list.")
    return _back(period_id, parish_id, msg=f"Reactivated {staff['first_name']} {staff['last_name']}. Her hours from the email are entered and wait for review.")


@router.post("/admin/timekeeping/status/{period_id}/{parish_id}/totals/person/{staff_id}/{action}")
async def totals_person_action(period_id: int, parish_id: int, staff_id: int, action: str, request: Request):
    """ONE Accept or Reject for an employee's whole row: figures typed into the row's boxes are saved first."""
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    form = await request.form()
    figures, new_figures = {}, {}
    for k in form.keys():
        v = form.get(k)
        if k.startswith("h_") and k[2:].isdigit():
            figures[int(k[2:])] = v
        elif k.startswith("n_") and len(k) > 2:
            new_figures[k[2:]] = v
    try:
        res = pt.review_person(org["id"], period_id, parish_id, staff_id, action, user["id"],
                               figures=figures, new_figures=new_figures)
    except pt.PayrollError as e:
        return _back(period_id, parish_id, err=e.message)
    if action == "reject":
        return _back(period_id, parish_id, msg=f"Rejected {res['rejected']} line(s) for the employee.")
    bits = []
    if res["accepted"]:
        bits.append(f"{res['accepted']} accepted")
    if res["edited"]:
        bits.append(f"{res['edited']} changed")
    if res["created"]:
        bits.append(f"{res['created']} added")
    msg = (", ".join(bits) if bits else "Nothing to accept") + "."
    if res["skipped"]:
        msg += f" {res['skipped']} line(s) have a different figure waiting or a conflict: use the buttons beside them."
    return _back(period_id, parish_id, msg=msg)


@router.post("/admin/timekeeping/status/{period_id}/{parish_id}/totals/add-line")
async def totals_add_line(period_id: int, parish_id: int, request: Request):
    """A line the reader missed, entered by hand (optionally tied to one of this parish's emails)."""
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    form = await request.form()
    try:
        staff_id = int(form.get("staff_id") or 0)
    except ValueError:
        staff_id = 0
    try:
        pt.add_manual_line(org["id"], period_id, parish_id, staff_id, form.get("category_key") or "", form.get("hours"),
                           user["id"], source_ref=form.get("source_ref"))
    except pt.PayrollError as e:
        return _back(period_id, parish_id, err=e.message)
    return _back(period_id, parish_id, msg="Line added and confirmed.")


@router.post("/admin/timekeeping/status/{period_id}/{parish_id}/totals/{line_id}/{action}")
async def totals_line_action(period_id: int, parish_id: int, line_id: int, action: str, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    form = await request.form()
    try:
        pt.review_line(org["id"], line_id, action, user["id"], hours=form.get("hours"), note=form.get("note"))
    except pt.PayrollError as e:
        return _back(period_id, parish_id, err=e.message)
    return _back(period_id, parish_id, msg="Saved.")


def _standing_msg(res: dict) -> str:
    return (f"Standing hours: {res['recorded']} line(s) recorded, {res['confirmed_automatically']} confirmed "
            f"automatically, {res['unchanged']} already there. {res['approved']} parish row(s) approved.")


@router.post("/admin/timekeeping/status/{period_id}/standing")
async def standing_carry_forward(period_id: int, request: Request):
    """Actions > Confirm all standing hours."""
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    try:
        res = pt.confirm_standing(org["id"], period_id, user["id"])
    except pt.PayrollError as e:
        return _status_back(period_id, err=e.message)
    return _status_back(period_id, msg=_standing_msg(res))


@router.post("/admin/timekeeping/status/{period_id}/{parish_id}/standing")
async def standing_one(period_id: int, parish_id: int, request: Request):
    """A parish row's menu, or the Standing hours panel on the parish's own page: Confirm standing hours."""
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    form = await request.form()
    on_parish_page = form.get("back") == "parish"
    try:
        res = pt.confirm_standing(org["id"], period_id, user["id"], parish_id)
    except pt.PayrollError as e:
        return _back(period_id, parish_id, err=e.message) if on_parish_page else _status_back(period_id, err=e.message)
    return _back(period_id, parish_id, msg=_standing_msg(res)) if on_parish_page else _status_back(period_id, msg=_standing_msg(res))


def _reply(period_id: int, parish_id: int, form, msg: str | None = None, err: str | None = None):
    """Back to the parish's own page when the button was pressed there (form field back=parish), else to the board."""
    if form.get("back") == "parish":
        return _back(period_id, parish_id, msg=msg, err=err)
    return _status_back(period_id, msg=msg, err=err)


@router.post("/admin/timekeeping/status/{period_id}/approve-all")
async def approve_all(period_id: int, request: Request):
    """Actions > Approve all: every department that is ready (hours in, nothing waiting for a decision)."""
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    try:
        res = pt.approve_all(org["id"], period_id, user["id"])
    except pt.PayrollError as e:
        return _status_back(period_id, err=e.message)
    msg = f"Approved {res['approved']} department{'' if res['approved'] == 1 else 's'}."
    if res["waiting"]:
        msg += f" {res['waiting']} still {'has' if res['waiting'] == 1 else 'have'} something waiting for a decision and {'was' if res['waiting'] == 1 else 'were'} left."
    if res["no_hours"]:
        msg += f" {res['no_hours']} {'has' if res['no_hours'] == 1 else 'have'} no hours yet (use Confirm no hours this week when none are expected)."
    return _status_back(period_id, msg=msg)


@router.post("/admin/timekeeping/status/{period_id}/{parish_id}/approve")
async def parish_approve(period_id: int, parish_id: int, request: Request):
    """A parish row's menu, or the parish's own page: Approve department (mark reviewed and approved)."""
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    form = await request.form()
    try:
        pt.approve_parish(org["id"], period_id, parish_id, user["id"])
    except pt.PayrollError as e:
        return _reply(period_id, parish_id, form, err=e.message)
    return _reply(period_id, parish_id, form, msg="Reviewed and approved.")


@router.post("/admin/timekeeping/status/{period_id}/{parish_id}/no-hours")
async def parish_no_hours(period_id: int, parish_id: int, request: Request):
    """A parish row's menu, or the parish's own page: Confirm no hours this week."""
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    form = await request.form()
    try:
        pt.confirm_no_hours(org["id"], period_id, parish_id, user["id"])
    except pt.PayrollError as e:
        return _reply(period_id, parish_id, form, err=e.message)
    return _reply(period_id, parish_id, form, msg="Confirmed: no hours this period.")


@router.post("/admin/timekeeping/status/{period_id}/{parish_id}/undo-approval")
async def parish_undo(period_id: int, parish_id: int, request: Request):
    """A parish row's menu, or the parish's own page: Undo (approval, confirmed no hours or confirmed standing hours)."""
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    form = await request.form()
    try:
        pt.undo_approval(org["id"], period_id, parish_id)
    except pt.PayrollError as e:
        return _reply(period_id, parish_id, form, err=e.message)
    return _reply(period_id, parish_id, form, msg="The approval was removed.")


@router.post("/admin/timekeeping/status/{period_id}/final")
async def mark_final(period_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    try:
        pt.finalize_period(org["id"], period_id, user["id"])
    except pt.PayrollError as e:
        return _status_back(period_id, err=e.message)
    return _status_back(period_id, msg="The hours are marked Final for Checkwriters.")


@router.post("/admin/timekeeping/status/{period_id}/unfinal")
async def mark_not_final(period_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    try:
        pt.unfinalize_period(org["id"], period_id)
    except pt.PayrollError as e:
        return _status_back(period_id, err=e.message)
    return _status_back(period_id, msg="The hours are no longer marked Final.")


# ------------------------------------------------------------------ variance report

@router.get("/admin/timekeeping/status/{period_id}/variance", response_class=HTMLResponse)
def variance_page(period_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    try:
        period = pt.get_period(org["id"], period_id)
        report = pt.variance(org["id"], period_id)
    except pt.PayrollError as e:
        return _status_back(period_id, err=e.message)
    # decisions (paid next period / manual check / Beacon wrong) can be made only on a closed period whose paid hours are loaded
    # and which is not actualized yet (payroll_closeout.py)
    can_decide = (period["status"] == "closed" and period["register_loaded_at"] is not None
                  and period["actualized_at"] is None)
    return _render(request, "timekeeping_variance.html", user,
                   {"current_org": org, "period": period, "report": report, "can_decide": can_decide,
                    "decision_labels": pdec.LABELS, "decision_explain": pdec.EXPLAIN, "decision_order": pdec.DECISIONS})


# ------------------------------------------------------------------ parish profile

def _profile_ctx(org: dict, parish: dict) -> dict:
    prof = db.query_one("SELECT * FROM portal.parish_payroll_profile WHERE parish_id = %s", (parish["id"],))
    senders = pt.get_senders(org["id"], parish.get("code"))
    mine = senders[0] if senders else {"time_submitters": [], "report_recipients": []}
    return {"current_org": org, "parish": parish, "profile": prof, "patterns": PATTERN_LABELS,
            "submitters": mine["time_submitters"], "recipients": mine["report_recipients"]}


@router.get("/admin/timekeeping/parish-profile/{parish_id}", response_class=HTMLResponse)
def profile_page(parish_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    parish = registry.get_parish(parish_id, org["id"])
    if not parish:
        return RedirectResponse("/admin/timekeeping/status?error=That+parish+was+not+found.", status_code=303)
    return _render(request, "timekeeping_parish_profile.html", user, _profile_ctx(org, parish))


@router.post("/admin/timekeeping/parish-profile/{parish_id}/save")
async def profile_save(parish_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    form = await request.form()
    try:
        pt.set_profile(org["id"], parish_id=parish_id, pattern=str(form.get("pattern") or ""),
                       notes=form.get("notes"), user_id=user["id"])
    except pt.PayrollError as e:
        return RedirectResponse(f"/admin/timekeeping/parish-profile/{parish_id}?error={quote_plus(e.message)}", status_code=303)
    return RedirectResponse(f"/admin/timekeeping/parish-profile/{parish_id}?saved=1", status_code=303)


@router.post("/admin/timekeeping/parish-profile/{parish_id}/sender-add")
async def profile_sender_add(parish_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    parish = registry.get_parish(parish_id, org["id"])
    form = await request.form()
    try:
        if not parish or not parish.get("code"):
            raise pt.PayrollError("not_found", "That parish was not found.")
        pt.add_learned_sender(org["id"], parish_code=parish["code"], email=str(form.get("email") or ""),
                              name=form.get("name"), source="manual")
    except pt.PayrollError as e:
        return RedirectResponse(f"/admin/timekeeping/parish-profile/{parish_id}?error={quote_plus(e.message)}", status_code=303)
    return RedirectResponse(f"/admin/timekeeping/parish-profile/{parish_id}?saved=1", status_code=303)


@router.post("/admin/timekeeping/parish-profile/{parish_id}/sender-remove")
async def profile_sender_remove(parish_id: int, request: Request):
    user, org, err = _require_hr_admin(request)
    if err:
        return err
    parish = registry.get_parish(parish_id, org["id"])
    form = await request.form()
    if parish and parish.get("code"):
        pt.remove_sender(org["id"], parish_code=parish["code"], email=str(form.get("email") or ""))
    return RedirectResponse(f"/admin/timekeeping/parish-profile/{parish_id}?saved=1", status_code=303)


# ------------------------------------------------------------ the parish's read-only view

@router.get("/timekeeping/received", response_class=HTMLResponse)
def received_page(request: Request):
    """What the diocese recorded for THIS parish from its emails, for the current pay period and any
    later one that already has hours. The parish is always the viewer's own (never a parameter)."""
    user, parish, diocese_org, err = timekeeping.timekeeping_context(request)
    if err:
        return err
    periods = []
    cur = timekeeping.get_current_open_period(diocese_org["id"])
    if cur:
        periods.append(cur)
    for p in pt.list_periods(diocese_org["id"]):
        if p["status"] == "future" and (not cur or p["period_start"] > cur["period_start"]):
            periods.append(p)
    blocks = []
    for p in periods:
        rows = pt.parish_view(diocese_org["id"], parish["id"], p["id"])
        if rows or (cur and p["id"] == cur["id"]):
            for r in rows:
                r["state_label"] = ("Recorded" if r["state"] == "ok" else "Waiting for the diocese to review")
            blocks.append({"period": p, "rows": rows})
    return _render(request, "timekeeping_received.html", user, {"parish": parish, "blocks": blocks})
