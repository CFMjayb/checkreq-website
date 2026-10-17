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
                          + (f" ({p['register_source_ref']})" if p["register_source_ref"] else "") + "."
                          + (f" {un} paid person(s) are not on the roster: refresh the roster from the register, then load again."
                             if un else "")))
    else:
        s3.update(state="todo", detail="On the payroll PC, double-click Close Out Pay Period.bat. It reads the labor distribution "
                                       "in this pay date's folder and sends the hours only. Pay stays on the PC.")
    steps.append(s3)

    s4 = {"n": 4, "title": "Tie out: Beacon against the paid hours"}
    if var is None:
        s4.update(state="waiting", detail="Waits for the paid hours.")
    elif differing == 0:
        s4.update(state="done", detail=f"All {counts.get('match', 0)} lines match the paid hours." + review_text,
                  **({"link": {"label": "See the lines for review", "url": f"{base}/variance"}} if for_review else {}))
    else:
        s4.update(state="todo", detail=(f"{counts.get('match', 0)} match. {counts.get('differs', 0)} differ, "
                                         f"{counts.get('paid_no_beacon_hours', 0)} were paid with no Beacon hours, "
                                         f"{counts.get('beacon_hours_not_paid', 0)} Beacon hours were not paid." + review_text),
                  link={"label": "See each difference", "url": f"{base}/variance"})
    steps.append(s4)

    s5 = {"n": 5, "title": "Actualize: make Beacon match the paid hours"}
    if st["actualized"]:
        s5.update(state="done", detail=f"Actualized {_when(p['actualized_at'], org_id)}"
                                       + (" automatically." if p["actualized_automatically"] else
                                          f" by {st['actualized_by']}." if st["actualized_by"] else ".") + review_text)
    elif not st["closed"] or var is None:
        s5.update(state="waiting", detail="Needs the period closed and the paid hours loaded.")
    elif differing == 0 and p["register_unmatched"]:
        s5.update(state="waiting", detail="Every line matches, but some paid people are not on the roster (step 3).")
    else:
        s5.update(state="todo",
                  detail=((f"{differing} change(s) to make. The old figures stay in the edit history. "
                           "Beacon hours that were not paid become 0 with a note.") if differing else
                          "No line needs to change. Press the button to record the period as Actualized.") + review_text,
                  action={"label": "Make Beacon match the paid hours", "url": f"{base}/closeout/actualize",
                          "confirm": (f"Change {differing} line(s) to the paid hours?" if differing else
                                      "Record this period as Actualized?")})
    steps.append(s5)

    steps.append({"n": 6, "title": "File the reports in the Documents folders", "state": "waiting",
                  "detail": "Waits for the DME Documents site and the HR Payroll folder. Not connected yet."})

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
                    "departments_off": off})


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
    def do(org_id, user_id):
        period = pt.get_period(org_id, period_id)
        if period["status"] == "closed":
            return {"already": True}
        if period["status"] != "open":
            raise pt.PayrollError("refused", "Only an open pay period can be closed here.")
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE portal.payroll_periods SET status = 'closed', processed_by_user_id = %s, processed_at = now(), "
                            "updated_at = now() WHERE id = %s AND org_id = %s", (user_id, period_id, org_id))
                conn.commit()
        return {"already": False}
    return _act(request, period_id, do, lambda r: "Already closed." if r["already"] else "The pay period is closed.")


@router.post("/admin/timekeeping/status/{period_id}/closeout/actualize")
async def closeout_actualize(period_id: int, request: Request):
    def msg(r):
        parts = [f"{r['changed']} changed to the paid hours", f"{r['added']} added", f"{r['zeroed']} set to 0 (not paid)"]
        tail = " Beacon now matches the paid hours." if r["tied"] else f" {r['left']} difference(s) left" + (
            f" and {r['unmatched']} paid person(s) not on the roster." if r["unmatched"] else ".")
        if r.get("for_review"):
            tail += f" {r['for_review']} person(s) kept as entered because their total hours match (noted for review)."
        return "Actualized: " + ", ".join(parts) + "." + tail
    return _act(request, period_id, lambda o, u: pc.actualize_period(o, period_id, u), msg)


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
