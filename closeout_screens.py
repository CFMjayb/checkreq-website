"""
closeout_screens.py -- the "Close out this pay period" checklist (26-158, 2026-10-15), under Time Review.

One page that lists the steps after payroll is processed and what has been done: hours Final, close the period, load the paid
hours (done on the payroll PC), tie out, actualize, file the documents, open the next period. All the rules live in
payroll_closeout.py and payroll_totals.py. hr_admin or beacon_admin of the diocese, like every Timekeeping screen.

Registered from payroll_screens.register() (so it sits BEFORE the generic /status/{period}/{parish} route, which would
otherwise try to read "closeout" as a parish number).
"""
from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

import db
import org_time
import payroll_closeout as pc
import payroll_decisions as pdec
import payroll_report_filing as prf
import payroll_totals as pt

router = APIRouter()

_current_user = None
_current_org = None
_render = None
_gate = None  # payroll_screens._require_hr_admin


def register(app, *, current_user, current_org, render, gate) -> None:
    global _current_user, _current_org, _render, _gate
    _current_user, _current_org, _render, _gate = current_user, current_org, render, gate
    app.include_router(router)


def _back(period_id: int, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    q = ""
    if msg:
        q = "?saved=" + quote_plus(msg)
    elif err:
        q = "?error=" + quote_plus(err)
    return RedirectResponse(f"/admin/timekeeping/status/{period_id}/closeout{q}", status_code=303)


def _when(ts, org_id: int) -> str:
    return org_time.format_local(ts, org_time.zone_name_for_org(org_id)) if ts else ""


def build_steps(org_id: int, st: dict) -> list[dict]:
    """The seven steps, each with a state (done | todo | waiting) and, when a person can do it here, one button."""
    p = st["period"]
    pid = p["id"]
    base = f"/admin/timekeeping/status/{pid}"
    var = st["variance"]
    counts = var["counts"] if var else {}
    differing = sum(counts.get(k, 0) for k in ("differs", "paid_no_beacon_hours", "beacon_hours_not_paid"))
    # people whose total hours tie to the paid total but whose categories differ: left as entered, noted for review
    for_review = var.get("category_only_people", 0) if var else 0
    review_text = (f" {for_review} person(s) have the same total hours as paid but different categories: left as entered, "
                   f"noted for review." if for_review else "")
    nxt = st["next"]
    steps = []

    s1 = {"n": 1, "title": "Hours are Final", "state": "done" if st["final"] else "todo"}
    s1["detail"] = (f"Marked Final {_when(p['hours_finalized_at'], org_id)}." if st["final"] else
                    "Approve every department on Time Review, then Actions > Mark hours Final.")
    if not st["final"]:
        s1["link"] = {"label": "Go to Time Review", "url": base}
    steps.append(s1)

    s2 = {"n": 2, "title": "Close the pay period"}
    if st["closed"]:
        s2.update(state="done", detail=f"Closed {_when(p['processed_at'], org_id)}." if p["processed_at"] else "Closed.")
    elif p["status"] == "future":
        s2.update(state="waiting", detail="This pay period has not started.")
    else:
        s2.update(state="todo", detail="Payroll has been processed. Closing stops new hours from landing in this period.",
                  action={"label": "Close this pay period", "url": f"{base}/closeout/close",
                          "confirm": "Close this pay period? Hours stop landing in it."})
    steps.append(s2)

    s3 = {"n": 3, "title": "Load the paid hours"}
    if st["register_loaded"]:
        un = p["register_unmatched"]
        s3.update(state="done" if not un else "todo",
                  detail=(f"{st['register_lines']} paid line(s) loaded {_when(p['register_loaded_at'], org_id)}"
                          + (f" by {st['register_loaded_by']}" if st.get("register_loaded_by") else "")
                          + (f" ({p['register_source_ref']})" if p["register_source_ref"] else "") + "."
                          + (f" {un} paid person(s) are not on the roster: refresh the roster from the register, then load again."
                             if un else "")))
    else:
        s3.update(state="todo", detail="The Phoenix workbook sends the paid hours (hours only, never pay): after it generates the reports, "
                                       "press Send Paid Hours to Beacon, then review the Beacon Tie-Out tab before sending the reports.")
    steps.append(s3)

    undecided = var.get("undecided", 0) if var else 0
    decided = var.get("decided", 0) if var else 0
    manual_n = counts.get("manual_check", 0)
    manual_text = (f" {manual_n} line(s) settled by manual check or void-reissue: kept as entered, due on the next register."
                   if manual_n else "")
    s4 = {"n": 4, "title": "Tie out: Beacon against the paid hours"}
    if var is None:
        s4.update(state="waiting", detail="Waits for the paid hours.")
    elif differing == 0:
        s4.update(state="done", detail=f"All {counts.get('match', 0)} lines match the paid hours." + manual_text + review_text,
                  **({"link": {"label": "See the lines for review", "url": f"{base}/variance"}} if (for_review or manual_n) else {}))
    else:
        s4.update(state="todo", detail=(f"{counts.get('match', 0)} match. {counts.get('differs', 0)} differ, "
                                         f"{counts.get('paid_no_beacon_hours', 0)} were paid with no Beacon hours, "
                                         f"{counts.get('beacon_hours_not_paid', 0)} Beacon hours were not paid. "
                                         f"{decided} decided, {undecided} still need a decision (include in next period, "
                                         f"manual check / void-reissue, or Beacon wrong)." + manual_text + review_text),
                  link={"label": "Decide each difference" if undecided else "See each difference", "url": f"{base}/variance"})
    steps.append(s4)

    s5 = {"n": 5, "title": "Actualize: settle the differences and match Beacon to the paid hours"}
    if st["actualized"]:
        s5.update(state="done", detail=f"Actualized {_when(p['actualized_at'], org_id)}"
                                       + (" automatically." if p["actualized_automatically"] else
                                          f" by {st['actualized_by']}." if st["actualized_by"] else ".") + review_text)
    elif not st["closed"] or var is None:
        s5.update(state="waiting", detail="Needs the period closed and the paid hours loaded.")
    elif differing == 0 and p["register_unmatched"]:
        s5.update(state="waiting", detail="Every line matches, but some paid people are not on the roster (step 3).")
    elif undecided:
        s5.update(state="todo", detail=f"Decide the {undecided} remaining line(s) on the tie-out page first. Then this step applies "
                                       "the decisions." + review_text,
                  link={"label": "Decide each difference", "url": f"{base}/variance"})
    else:
        by = {k: [r for r in (var["rows"] if var else []) if r["kind"] in pc.OPEN_KINDS and r["decision"] == k]
              for k in pdec.DECISIONS}
        nxt_label = nxt["label"] if nxt else "the next pay period"
        carry_h = sum(abs(r["difference"]) for r in by[pdec.NEXT_PERIOD])
        parts = []
        if by[pdec.BEACON_WRONG]:
            parts.append(f"{len(by[pdec.BEACON_WRONG])} change to the paid hours")
        if by[pdec.NEXT_PERIOD]:
            parts.append(f"{len(by[pdec.NEXT_PERIOD])} include in next period ({carry_h:g} hours move to {nxt_label})")
        if by[pdec.MANUAL_CHECK]:
            parts.append(f"{len(by[pdec.MANUAL_CHECK])} manual check / void-reissue (kept as entered)")
        s5.update(state="todo",
                  detail=((f"All decided: {', '.join(parts)}. The old figures stay in the edit history. "
                           "Beacon hours that were not paid become 0 with a note.") if differing else
                          "No line needs to change. Press the button to record the period as Actualized.") + review_text,
                  action={"label": "Apply the decisions" if differing else "Record as Actualized",
                          "url": f"{base}/closeout/actualize",
                          "confirm": (f"Apply {differing} decision(s) now?" if differing else
                                      "Record this period as Actualized?")})
    steps.append(s5)

    s6 = {"n": 6, "title": "File the reports in the Documents folders"}
    here = prf.enabled_for(org_id)
    if st["reports_filed"]:
        how = {"phoenix": "Posted via the Phoenix workbook", "beacon": "Filed by Beacon"}.get(p.get("reports_filed_how"), "Filed another way")
        s6.update(state="done", detail=f"{how}. Marked {_when(p['reports_filed_at'], org_id)}"
                                       + (f" by {st['reports_filed_by']}." if st["reports_filed_by"] else ".")
                                       + (f" {p['reports_filed_note']}." if p.get("reports_filed_note") else ""),
                  actions=[{"label": "Undo", "url": f"{base}/closeout/reports-unfiled", "confirm": "Take the filed mark back?",
                            "plain": True}])
    elif not st["closed"]:
        s6.update(state="waiting", detail="Needs the pay period closed. Each department's two reports are posted by the Phoenix "
                                          "workbook's File Reports to Beacon Documents button"
                                          + (", or filed from here once payroll is processed." if here else "."))
    else:
        acts = [{"label": "Reports Posted via Phoenix", "url": f"{base}/closeout/reports-filed?how=phoenix",
                 "confirm": "Mark the reports as posted via the Phoenix workbook?"}]
        if here:
            acts.append({"label": "File Reports Now Here", "url": f"{base}/closeout/file-reports",
                         "confirm": "Beacon will copy this pay date's department reports from the Accounting library into each "
                                    "department's HRPayroll folder on the Documents site. It copies only and never overwrites a "
                                    "different file. Continue?", "plain": True})
        s6.update(state="todo",
                  detail=("If the Phoenix workbook already posted each department's reports, say so with Reports Posted via Phoenix. "
                          + ("Otherwise File Reports Now Here copies this pay date's department reports from the Accounting library "
                             "into each department's HRPayroll > Payroll Reports folder (copy only, nothing overwritten, never the "
                             "all-department reports)." if here else "")).strip(),
                  actions=acts)
    steps.append(s6)

    s7 = {"n": 7, "title": "Open the next pay period"}
    if nxt is None:
        s7.update(state="waiting", detail="No later pay period is set up. Add it under Payroll Periods.")
    elif nxt["status"] == "open":
        s7.update(state="done", detail=f"{nxt['label']} is open.")
    elif not st["closed"]:
        s7.update(state="waiting", detail=f"Needs this period closed first. Next is {nxt['label']} ({nxt['status']}).")
    else:
        s7.update(state="todo", detail=f"{nxt['label']} is {nxt['status']}. New payroll mail has nowhere to land until it is open.",
                  action={"label": f"Open {nxt['label']}", "url": f"{base}/closeout/open-next",
                          "confirm": f"Open {nxt['label']}?"})
    steps.append(s7)
    return steps


@router.get("/admin/timekeeping/status/{period_id}/closeout")
def closeout_page(period_id: int, request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    try:
        st = pc.closeout_state(org["id"], period_id)
    except pt.PayrollError as e:
        return RedirectResponse("/admin/timekeeping/status?error=" + quote_plus(e.message), status_code=303)
    var = st["variance"]
    off = [d for d in (var["departments"] if var else []) if d["differing"]]
    return _render(request, "timekeeping_closeout.html", user,
                   {"current_org": org, "period": st["period"], "state": st, "steps": build_steps(org["id"], st),
                    "departments_off": off, "prior": st["prior"], "manual_missing": var.get("manual_missing", []) if var else []})


def _act(request: Request, period_id: int, fn, ok_msg):
    user, org, err = _gate(request)
    if err:
        return err
    try:
        res = fn(org["id"], user["id"])
    except pt.PayrollError as e:
        return _back(period_id, err=e.message)
    return _back(period_id, msg=ok_msg(res))


@router.post("/admin/timekeeping/status/{period_id}/closeout/close")
async def closeout_close(period_id: int, request: Request):
    return _act(request, period_id, lambda o, u: pc.close_period(o, period_id, u),
                lambda r: "Already closed." if r["already"] else "The pay period is closed.")


@router.post("/admin/timekeeping/status/{period_id}/closeout/actualize")
async def closeout_actualize(period_id: int, request: Request):
    def msg(r):
        parts = [f"{r['changed']} changed to the paid hours", f"{r['added']} added", f"{r['zeroed']} set to 0 (not paid)"]
        if r.get("carried"):
            parts.append(f"{r['carried']} included in next period ({r['carried_hours']:g} hours carried to the next pay period)")
        if r.get("manual"):
            parts.append(f"{r['manual']} kept as entered (manual check / void-reissue)")
        tail = " Beacon now matches the paid hours." if r["tied"] else f" {r['left']} difference(s) left" + (
            f" and {r['unmatched']} paid person(s) not on the roster." if r["unmatched"] else ".")
        if r.get("for_review"):
            tail += f" {r['for_review']} person(s) kept as entered because their total hours match (noted for review)."
        return "Actualized: " + ", ".join(parts) + "." + tail
    return _act(request, period_id, lambda o, u: pc.actualize_period(o, period_id, u), msg)


# ---- tie-out decisions (Jay, 2026-10-22): the three ways to settle a difference, per line or per employee

def _back_variance(period_id: int, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    q = ""
    if msg:
        q = "?saved=" + quote_plus(msg)
    elif err:
        q = "?error=" + quote_plus(err)
    return RedirectResponse(f"/admin/timekeeping/status/{period_id}/variance{q}", status_code=303)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _set_msg(res: dict, decision: str) -> str:
    text = f"Saved: {pdec.LABELS[decision]} for {res['set']} line(s)."
    if res["skipped"]:
        text += f" {res['skipped']} line(s) left as they were. {res['reason'] or ''}"
    return text.strip()


@router.post("/admin/timekeeping/status/{period_id}/variance/decide")
async def variance_decide(period_id: int, request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    form = await request.form()
    staff_id, category_id = _int(form.get("staff_id")), _int(form.get("category_id"))
    if staff_id is None or category_id is None:
        return _back_variance(period_id, err="That line was not understood.")
    decision = str(form.get("decision") or "").strip()
    try:
        if form.get("undo") or not decision:  # the Undo button: the decision is taken back
            pc.clear_decision(org["id"], period_id, staff_id, category_id)
            return _back_variance(period_id, msg="The decision was taken back. This line needs a decision.")
        res = pc.set_decision(org["id"], period_id, staff_id, category_id, decision, user["id"], form.get("note"))
    except pt.PayrollError as e:
        return _back_variance(period_id, err=e.message)
    return _back_variance(period_id, msg=_set_msg(res, decision))


@router.post("/admin/timekeeping/status/{period_id}/variance/decide-employee")
async def variance_decide_employee(period_id: int, request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    form = await request.form()
    staff_id = _int(form.get("staff_id"))
    decision = str(form.get("decision") or "").strip()
    if staff_id is None or not decision:
        return _back_variance(period_id, err="Pick a decision for this person.")
    try:
        res = pc.set_employee_decision(org["id"], period_id, staff_id, decision, user["id"], form.get("note"))
    except pt.PayrollError as e:
        return _back_variance(period_id, err=e.message)
    return _back_variance(period_id, msg=_set_msg(res, decision))


@router.post("/admin/timekeeping/status/{period_id}/variance/decide-all")
async def variance_decide_all(period_id: int, request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    form = await request.form()
    decision = str(form.get("decision") or pdec.BEACON_WRONG).strip()
    try:
        res = pc.decide_all_remaining(org["id"], period_id, decision, user["id"], form.get("note"))
    except pt.PayrollError as e:
        return _back_variance(period_id, err=e.message)
    if not res["set"] and not res["skipped"]:
        return _back_variance(period_id, msg="Every differing line already has a decision.")
    return _back_variance(period_id, msg=_set_msg(res, decision))


@router.post("/admin/timekeeping/status/{period_id}/closeout/reports-filed")
async def closeout_reports_filed(period_id: int, request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    how = str(request.query_params.get("how") or "")
    try:
        pc.mark_reports_filed(org["id"], period_id, user["id"], how)
    except pt.PayrollError as e:
        return _back(period_id, err=e.message)
    return _back(period_id, msg="The reports are marked as posted via the Phoenix workbook." if how == "phoenix"
                 else "The reports are marked as filed another way.")


@router.post("/admin/timekeeping/status/{period_id}/closeout/file-reports")
async def closeout_file_reports(period_id: int, request: Request):
    """File Reports Now Here: Beacon copies this pay date's department reports from the Accounting library into each department's
    folder on the Documents site (payroll_report_filing.py)."""
    user, org, err = _gate(request)
    if err:
        return err
    try:
        res = prf.file_period(org["id"], period_id, user["id"])
    except pt.PayrollError as e:
        return _back(period_id, err=e.message)
    if not res["filed"]:
        bad = sorted({c for c, _ in res["problems"]})
        return _back(period_id, err=(f"Not everything could be filed: {res['conflicts']} file(s) already there with a different size, "
                                     f"{res['errors']} that could not be copied (departments {', '.join(bad)}). {res['copied']} copied so far. "
                                     "Nothing was overwritten. Fix those and press the button again: it skips what is already there."))
    return _back(period_id, msg=f"Filed {res['files']} reports for {res['departments']} departments in {res['folder']}: "
                                f"{res['copied']} copied, {res['already_there']} already there.")


@router.post("/admin/timekeeping/status/{period_id}/closeout/reports-unfiled")
async def closeout_reports_unfiled(period_id: int, request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    try:
        pc.clear_reports_filed(org["id"], period_id)
    except pt.PayrollError as e:
        return _back(period_id, err=e.message)
    return _back(period_id, msg="The filed mark was taken back.")


@router.post("/admin/timekeeping/status/{period_id}/closeout/open-next")
async def closeout_open_next(period_id: int, request: Request):
    return _act(request, period_id, lambda o, u: pc.open_next_period(o, period_id, u),
                lambda r: f"{r['label']} was already open." if r["already"] else f"{r['label']} is open.")


@router.post("/admin/timekeeping/status/{period_id}/closeout/auto")
async def closeout_auto(period_id: int, request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    form = await request.form()
    on = str(form.get("on") or "") == "1"
    pc.set_auto_actualize(org["id"], on, user["id"])
    return _back(period_id, msg=("Beacon will now match the paid hours automatically when they are loaded into a closed period."
                                 if on else "Automatic matching is off. A person presses the button on this page."))
