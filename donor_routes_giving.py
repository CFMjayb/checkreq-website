"""
donor_routes_giving.py -- Beacon Donor Management, Phase 2 screens: /giving (batches and one-screen gift entry), /giving/funds,
/pledges. Routes only: every rule lives in donor_funds / donor_batches / donor_gifts / donor_corrections / donor_pledges.

The parish ALWAYS comes from parish_mode.effective_parish_mode through donor_web.gate. A parish id, batch id or gift id in a URL is
only ever a key that the service looks up INSIDE the signed-in parish. Nothing here reads a parish id from the browser.

Every action POSTs and redirects back with a short message (donor_web.back keeps it in the session, never in the URL). There is
no DELETE route and no route that deletes: lines are voided, gifts are reversed, returned or reclassified, pledges are cancelled.
Nothing on any screen sends anything to QuickBooks: the entry is built, shown and stored.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

import db
import donor_batches as B
import donor_corrections as K
import donor_funds as F
import donor_gifts as G
import donor_households as H
import donor_pledges as PL
import donor_qbo_entry as Q
import donor_web as W
from donor_core import DonorError, NotFound

router = APIRouter(dependencies=[Depends(W.check_path_ids)])
VIEW_BATCH_CAPS = ("batch.view", "totals.read", "giving.read", "giving.read.diocese")
VIEW_PLEDGE_CAPS = ("pledges.manage", "giving.read", "giving.read.diocese")


def register(app) -> None:
    app.include_router(router)


def _opt_int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _flag(form, key) -> bool:
    return form.get(key) is not None


def _text(form, key):
    v = form.get(key)
    return v.strip() if isinstance(v, str) and v.strip() else None


def _gate(request: Request, caps: tuple, active: str):
    """Signed in, a parish in view, giving turned on for it, and at least one of `caps`."""
    user, parish, ctx, resp = W.gate(request, need=None, feature="giving", active=active)
    if resp:
        return None, None, None, resp
    if not any(ctx.can(c) for c in caps):
        return None, None, None, W.page(request, "donor_off.html", user, parish, ctx, active, {"reason": "permission", "feature": "giving"}, status_code=403)
    return user, parish, ctx, None


def _not_found(request, user, parish, ctx, what: str, back_href: str):
    return W.page(request, "donor_off.html", user, parish, ctx, "giving", {"reason": "notfound", "feature": "giving", "what": what, "back_href": back_href}, status_code=404)


def _splits_from_form(form, prefix_amount="amount", prefix_fund="fund_id") -> list[dict]:
    """The first fund and amount, then up to two more rows (split2_*, split3_*) when the clerk used them."""
    rows = [{"fund_id": form.get(prefix_fund), "amount": form.get(prefix_amount)}]
    for i in (2, 3):
        f, a = form.get(f"split{i}_fund"), form.get(f"split{i}_amount")
        if f or a:
            rows.append({"fund_id": f, "amount": a})
    return rows


# ── Batches ─────────────────────────────────────────────────────────────────────────────────────
@router.get("/giving", response_class=HTMLResponse)
def giving_home(request: Request, status: str = ""):
    user, parish, ctx, resp = _gate(request, VIEW_BATCH_CAPS, "giving")
    if resp:
        return resp
    rows, funds, totals, error = [], [], [], None
    try:
        rows = B.batch_list(ctx, status=status if status in ("open", "pending", "closed", "reconciled") else None)
    except DonorError as e:
        error = e.message
    try:
        funds = F.fund_list(ctx, include_closed=False)
    except DonorError:
        funds = []
    today = dt.date.today()
    try:
        totals = G.report_totals_by_fund(ctx, dt.date(today.year, 1, 1), today)
    except DonorError:
        totals = []
    return W.page(request, "donor_batches.html", user, parish, ctx, "giving", {
        "batches": rows, "funds": funds, "totals": totals, "status": status, "year": today.year, "error": error,
        "today": today.isoformat()})


@router.post("/giving/batches/open")
async def batch_open_route(request: Request):
    user, parish, ctx, resp = _gate(request, ("batch.open",), "giving")
    if resp:
        return resp
    form = await request.form()
    data = {k: form.get(k) for k in ("kind", "deposit_date", "expected_amount", "expected_count", "default_fund_id", "default_gift_type",
                                     "deposit_ref", "cash_account", "memo")}
    data["settles_to_diocese"] = _flag(form, "settles_to_diocese")
    try:
        r = B.batch_open(ctx, data)
        return W.back(request, f"/giving/batches/{r['id']}", ok=f"Batch {r['number']} opened. Enter the gifts below.")
    except DonorError as e:
        return W.back(request, "/giving", err=e.message)


@router.get("/giving/api/envelope")
def envelope_lookup(request: Request, n: str = ""):
    user, parish, ctx, resp = _gate(request, ("batch.line",), "giving")
    if resp:
        return JSONResponse({"error": "not available"}, status_code=403)
    try:
        rows = H.envelope_lookup(ctx, n)
    except DonorError:
        rows = []
    # H.envelope_lookup already returns the display name (person_label) and the primary-contact flag
    return JSONResponse({"results": [{"id": r["id"], "name": r["name"], "primary": bool(r.get("is_primary_contact"))} for r in rows]})


@router.get("/giving/batches/{batch_id}", response_class=HTMLResponse)
def batch_page(batch_id: int, request: Request):
    user, parish, ctx, resp = _gate(request, VIEW_BATCH_CAPS, "giving")
    if resp:
        return resp
    try:
        bt = B.batch_get(ctx, batch_id)
    except NotFound:
        return _not_found(request, user, parish, ctx, "batch", "/giving")
    except DonorError as e:
        return W.back(request, "/giving", err=e.message)
    funds = []
    try:
        funds = F.fund_list(ctx, include_closed=False)
    except DonorError:
        pass
    batch = bt["batch"]
    live = [l for l in (bt["lines"] or []) if l["status"] != "voided"]
    last = live[-1] if live else None
    last_fund = (last["splits"][0]["fund_id"] if last and last["splits"] else None) or batch["default_fund_id"]
    last_type = (last["gift_type"] if last else None) or batch["default_gift_type"]
    types = G.DEPOSIT_TYPES if batch["kind"] == "deposit" else G.NON_DEPOSIT_TYPES
    entry = bt["entry"] or bt["preview"]
    uids = list({e["user_id"] for e in bt["events"]})
    names = {r["id"]: r["name"] for r in db.query("SELECT id, COALESCE(display_name, email) AS name FROM checkreq.app_users WHERE id = ANY(%s)", (uids,))} if uids else {}
    return W.page(request, "donor_batch.html", user, parish, ctx, "giving", {
        "bt": bt, "b": batch, "bid": batch_id, "funds": funds, "last_fund": last_fund, "last_type": last_type, "types": types, "names": names,
        "entry": entry, "entry_is_stored": bool(bt["entry"]), "live_count": len(live), "today": dt.date.today().isoformat(),
        "can_line": ctx.can("batch.line"), "can_close": ctx.can("batch.close"), "can_reopen": ctx.can("batch.reopen"),
        "can_correct": ctx.can("gift.correct"), "can_open": ctx.can("batch.open"), "see_gifts": G.can_read_gifts(ctx)})


@router.get("/giving/batches/{batch_id}/entry.json")
def batch_entry_json(batch_id: int, request: Request):
    """The journal entry as the QuickBooks server would take it, for the screen's 'show the entry' link. Never sent anywhere."""
    user, parish, ctx, resp = _gate(request, ("batch.close", "giving.read", "giving.read.diocese"), "giving")
    if resp:
        return JSONResponse({"error": "not available"}, status_code=403)
    try:
        bt = B.batch_get(ctx, batch_id)
    except DonorError:
        return JSONResponse({"error": "not found"}, status_code=404)
    entry = bt["entry"] or bt["preview"]
    if not entry:
        return JSONResponse({"error": "no entry yet"}, status_code=404)
    payload = Q.entry_payload(entry)
    payload["sent_to_quickbooks"] = False
    payload["status"] = entry.get("status")
    payload["problems"] = entry.get("problems") or []
    return JSONResponse(payload)


async def _batch_act(request: Request, batch_id: int, action, *, caps: tuple, anchor: str = ""):
    user, parish, ctx, resp = _gate(request, caps, "giving")
    if resp:
        return resp
    form = await request.form()
    try:
        msg = action(ctx, form)
        return W.back(request, f"/giving/batches/{batch_id}{anchor}", ok=msg)
    except DonorError as e:
        return W.back(request, f"/giving/batches/{batch_id}{anchor}", err=e.message)


@router.post("/giving/batches/{batch_id}/header")
async def batch_header(batch_id: int, request: Request):
    def act(ctx, form):
        changes = {k: form.get(k) for k in ("deposit_date", "expected_amount", "expected_count", "deposit_ref", "cash_account", "memo") if k in form}
        if "has_settles_to_diocese" in form:
            changes["settles_to_diocese"] = _flag(form, "settles_to_diocese")
        r = B.batch_update_header(ctx, batch_id, changes)
        return "Saved." if r["changed"] else "Nothing to change."
    return await _batch_act(request, batch_id, act, caps=("batch.open",))


@router.post("/giving/batches/{batch_id}/line")
async def batch_line(batch_id: int, request: Request):
    def act(ctx, form):
        data = {k: form.get(k) for k in ("person_id", "gift_type", "check_number", "memo", "gift_date", "postmark_date", "goods_value", "fee_amount",
                                         "in_kind_description", "book_value", "stock_shares", "stock_symbol", "stock_value")}
        data["fee_covered_by_donor"] = _flag(form, "fee_covered_by_donor")
        data["confirm_duplicate"] = _flag(form, "confirm_duplicate")
        data["splits"] = _splits_from_form(form)
        r = B.batch_add_line(ctx, batch_id, data)
        return f"Line added: ${G.money(r['amount'])}."
    return await _batch_act(request, batch_id, act, caps=("batch.line",), anchor="#entry")


@router.post("/giving/gifts/{gift_id}/update")
async def gift_update(gift_id: int, request: Request):
    user, parish, ctx, resp = _gate(request, ("batch.line",), "giving")
    if resp:
        return resp
    form = await request.form()
    bid = _opt_int(form.get("batch_id"))       # only where to land afterwards. The service finds the gift's real batch itself.
    url = f"/giving/batches/{bid}#entry" if bid else "/giving"
    changes = {k: form.get(k) for k in ("check_number", "memo", "gift_date", "postmark_date", "goods_value") if k in form}
    if form.get("amount") not in (None, "") or form.get("fund_id") not in (None, ""):
        changes["splits"] = _splits_from_form(form)
    changes["confirm_duplicate"] = _flag(form, "confirm_duplicate")
    try:
        r = B.batch_update_line(ctx, gift_id, changes)
        return W.back(request, url, ok="Saved." if r["changed"] else "Nothing to change.")
    except DonorError as e:
        return W.back(request, url, err=e.message)


@router.post("/giving/gifts/{gift_id}/void")
async def gift_void(gift_id: int, request: Request):
    user, parish, ctx, resp = _gate(request, ("batch.line",), "giving")
    if resp:
        return resp
    form = await request.form()
    bid = _opt_int(form.get("batch_id"))
    url = f"/giving/batches/{bid}#entry" if bid else "/giving"
    try:
        r = B.batch_void_line(ctx, gift_id, form.get("reason") or "")
        return W.back(request, url, ok="Line voided." if len(r["voided"]) == 1 else f"{len(r['voided'])} lines voided together.")
    except DonorError as e:
        return W.back(request, url, err=e.message)


@router.post("/giving/batches/{batch_id}/close")
async def batch_close(batch_id: int, request: Request):
    def act(ctx, form):
        r = B.batch_close(ctx, batch_id)
        tail = " (closed under the single-person exception, and logged)" if r["under_exception"] else ""
        warn = " The QuickBooks entry is incomplete: " + " ".join(r["entry_problems"]) if r["entry_status"] == "incomplete" else ""
        return f"Batch {r['number']} closed{tail}. Nothing was sent to QuickBooks: the entry is built and stored.{warn}"
    return await _batch_act(request, batch_id, act, caps=("batch.close",))


@router.post("/giving/batches/{batch_id}/reopen")
async def batch_reopen(batch_id: int, request: Request):
    def act(ctx, form):
        B.batch_reopen(ctx, batch_id, form.get("reason") or "")
        return "Batch reopened. The reason and your name are logged."
    return await _batch_act(request, batch_id, act, caps=("batch.reopen",))


# ── Corrections of closed gifts ─────────────────────────────────────────────────────────────────
async def _correct(request: Request, gift_id: int, kind: str):
    user, parish, ctx, resp = _gate(request, ("gift.correct",), "giving")
    if resp:
        return resp
    form = await request.form()
    bid = _opt_int(form.get("batch_id"))
    back = f"/giving/batches/{bid}" if bid else "/giving"
    try:
        reason = form.get("reason") or ""
        if kind == "reverse":
            repl = None
            if _opt_int(form.get("repl_person_id")) or form.get("repl_amount"):
                repl = {"person_id": _opt_int(form.get("repl_person_id")), "splits": [{"fund_id": form.get("repl_fund_id"), "amount": form.get("repl_amount")}]}
                repl = {k: v for k, v in repl.items() if v is not None}
            r = K.gift_reverse(ctx, gift_id, reason, repl)
        elif kind == "return":
            r = K.gift_return(ctx, gift_id, reason)
        else:
            r = K.gift_reclass(ctx, gift_id, _splits_from_form(form), reason)
        return W.back(request, f"/giving/batches/{r['batch_id']}", ok=f"Done. It is in today's correction batch {r['batch_number']}, which a different person closes.")
    except DonorError as e:
        return W.back(request, back, err=e.message)


@router.post("/giving/gifts/{gift_id}/reverse")
async def gift_reverse_route(gift_id: int, request: Request):
    return await _correct(request, gift_id, "reverse")


@router.post("/giving/gifts/{gift_id}/return")
async def gift_return_route(gift_id: int, request: Request):
    return await _correct(request, gift_id, "return")


@router.post("/giving/gifts/{gift_id}/reclass")
async def gift_reclass_route(gift_id: int, request: Request):
    return await _correct(request, gift_id, "reclass")


# ── Funds and campaigns ─────────────────────────────────────────────────────────────────────────
@router.get("/giving/funds", response_class=HTMLResponse)
def funds_page(request: Request):
    user, parish, ctx, resp = _gate(request, ("funds.manage", "batch.view", "totals.read", "giving.read"), "giving")
    if resp:
        return resp
    funds = F.fund_list(ctx)
    campaigns = F.campaign_list(ctx)
    return W.page(request, "donor_funds.html", user, parish, ctx, "funds", {"funds": funds, "campaigns": campaigns, "can_manage": ctx.can("funds.manage"),
                                                                              "today": dt.date.today().isoformat()})


@router.post("/giving/funds/save")
async def fund_save(request: Request):
    user, parish, ctx, resp = _gate(request, ("funds.manage",), "giving")
    if resp:
        return resp
    form = await request.form()
    fid = _opt_int(form.get("fund_id"))
    data = {k: form.get(k) for k in ("name", "statement_name", "income_account", "qbo_class", "liability_account", "donor_restriction", "sort_order") if k in form}
    for k in ("is_open", "accepts_pledges", "tax_deductible_default", "allows_recurring_end"):
        if f"has_{k}" in form:
            data[k] = _flag(form, k)
    try:
        if fid:
            r = F.fund_update(ctx, fid, data)
            return W.back(request, "/giving/funds", ok="Saved." if r["changed"] else "Nothing to change.")
        F.fund_create(ctx, data)
        return W.back(request, "/giving/funds", ok="Fund added.")
    except DonorError as e:
        return W.back(request, "/giving/funds", err=e.message)


@router.post("/giving/campaigns/save")
async def campaign_save(request: Request):
    user, parish, ctx, resp = _gate(request, ("funds.manage",), "giving")
    if resp:
        return resp
    form = await request.form()
    cid = _opt_int(form.get("campaign_id"))
    try:
        if cid:
            changes = {k: form.get(k) for k in ("name", "period_start", "period_end", "goal_amount") if k in form}
            if "has_is_active" in form:
                changes["is_active"] = _flag(form, "is_active")
            r = F.campaign_update(ctx, cid, changes)
            return W.back(request, "/giving/funds", ok="Saved." if r["changed"] else "Nothing to change.")
        F.campaign_create(ctx, {k: form.get(k) for k in ("fund_id", "name", "period_start", "period_end", "goal_amount")})
        return W.back(request, "/giving/funds", ok="Campaign added.")
    except DonorError as e:
        return W.back(request, "/giving/funds", err=e.message)


# ── Pledges ─────────────────────────────────────────────────────────────────────────────────────
@router.get("/pledges", response_class=HTMLResponse)
def pledges_page(request: Request, campaign: str = "", prior: str = ""):
    user, parish, ctx, resp = _gate(request, VIEW_PLEDGE_CAPS, "pledges")
    if resp:
        return resp
    campaigns = F.campaign_list(ctx)
    cid = _opt_int(campaign) or (campaigns[0]["id"] if campaigns else None)
    pid = _opt_int(prior)
    data = responses = None
    error = None
    if cid:
        try:
            data = PL.pledge_list(ctx, cid)
            if pid is None:
                older = [c for c in campaigns if c["id"] != cid and c["fund_id"] == data["campaign"]["fund_id"] and c["period_end"] < data["campaign"]["period_start"]]
                pid = older[0]["id"] if older else None
            responses = PL.campaign_responses(ctx, cid, pid)
        except DonorError as e:
            error = e.message
    return W.page(request, "donor_pledges.html", user, parish, ctx, "pledges", {
        "campaigns": campaigns, "cid": cid, "prior_id": pid, "data": data, "responses": responses, "error": error,
        "frequencies": PL.FREQUENCIES, "can_manage": ctx.can("pledges.manage"), "today": dt.date.today().isoformat()})


@router.post("/pledges/save")
async def pledge_save(request: Request):
    user, parish, ctx, resp = _gate(request, ("pledges.manage",), "pledges")
    if resp:
        return resp
    form = await request.form()
    cid = _opt_int(form.get("campaign_id"))
    back = f"/pledges?campaign={cid}" if cid else "/pledges"
    try:
        pledge_id = _opt_int(form.get("pledge_id"))
        if pledge_id:
            r = PL.pledge_update(ctx, pledge_id, {k: form.get(k) for k in ("amount", "frequency", "start_date", "end_date", "notes") if k in form})
            return W.back(request, back, ok="Saved." if r["changed"] else "Nothing to change.")
        PL.pledge_create(ctx, {k: form.get(k) for k in ("person_id", "joint_with_person_id", "campaign_id", "amount", "frequency", "start_date", "end_date", "notes")})
        return W.back(request, back, ok="Pledge added.")
    except DonorError as e:
        return W.back(request, back, err=e.message)


@router.post("/pledges/{pledge_id}/cancel")
async def pledge_cancel_route(pledge_id: int, request: Request):
    user, parish, ctx, resp = _gate(request, ("pledges.manage",), "pledges")
    if resp:
        return resp
    form = await request.form()
    cid = _opt_int(form.get("campaign_id"))
    back = f"/pledges?campaign={cid}" if cid else "/pledges"
    try:
        PL.pledge_cancel(ctx, pledge_id, form.get("reason") or "")
        return W.back(request, back, ok="Pledge cancelled (kept on the record).")
    except DonorError as e:
        return W.back(request, back, err=e.message)


@router.post("/pledges/softcredit")
async def soft_credit_route(request: Request):
    user, parish, ctx, resp = _gate(request, ("pledges.manage",), "pledges")
    if resp:
        return resp
    form = await request.form()
    cid = _opt_int(form.get("campaign_id"))
    back = f"/pledges?campaign={cid}" if cid else "/pledges"
    try:
        PL.soft_credit_add(ctx, _opt_int(form.get("gift_id")) or 0, _opt_int(form.get("person_id")) or 0, form.get("note"), form.get("amount") or None)
        return W.back(request, back, ok="Soft credit added. It counts toward the pledge, never toward the person's own giving.")
    except DonorError as e:
        return W.back(request, back, err=e.message)
