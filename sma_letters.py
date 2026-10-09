"""
sma_letters.py -- 26-129 SMA (Shared Ministry Allocation) letters, plan revision 12, step 1: the admin screens.

    /admin/sma-letters                          this entity's runs, and "New run" (upload the Task Force model)
    /admin/sma-letters/{run}                    the CHECK SHEET: every parish with its figures, flags, signers, notes
    /admin/sma-letters/{run}/letters/{letter}   one parish: figures, adjustment, signers, notes, letter versions
    ...plus the actions (confirm rates, cover letter, send templates to Formstack, build letters, per-parish edits,
    export, the letter PDF).

AUTHORIZATION: every route needs Beacon Admin or Setup Admin at the CURRENT entity, and every run, letter and file is
looked up THROUGH that entity (a run of another entity is a 404). No role check here ever uses org_id=None.
Step 1 sends no email and posts nothing: posting, the signer emails, upload, appeals and signing are steps 2 to 4.

New file per the standing rule: main.py only imports this and calls register().
"""
from __future__ import annotations

import csv
import io
import os
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import formstack_documents_client as fs
from starlette.concurrency import run_in_threadpool
import org_time
import rbac
import sma_calc as C
import sma_flags
import sma_model
import sma_pdf
import sma_signers
import sma_store as store
import upload_guard

router = APIRouter()

_current_user = None
_current_org = None
_render = None

MAX_UPLOAD = 16 * 1024 * 1024
STATUS_LABEL = {"draft": "Not built", "created": "Built", "excluded": "Excluded", "awaiting_action": "Awaiting action",
                "signing": "Signing in progress", "uploaded_review": "Uploaded, awaiting review", "appeal": "Appeal filed",
                "expired": "Expired", "complete": "Complete"}
RUN_STATUS_LABEL = {"draft": "Preparing", "created": "Letters built", "posted": "Posted", "closed": "Closed", "cancelled": "Cancelled"}
ADJ_LABEL = {"none": "None", "half": "50% reduction", "latest_year": "Latest-year test (Formula B)", "custom": "Custom amount"}


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


# ---------------------------------------------------------------------------------------------
# guards and helpers
# ---------------------------------------------------------------------------------------------
def _guard(request: Request, *, as_json: bool = False):
    """-> (user, org, None) or (None, None, error response)."""
    def refuse(status, text):
        return JSONResponse({"error": text}, status_code=status) if as_json else HTMLResponse(text, status_code=status)

    user = _current_user(request)
    if not user:
        return None, None, (JSONResponse({"error": "Sign in required"}, status_code=401) if as_json else RedirectResponse("/login"))
    org = _current_org(request)
    if not org or not rbac.user_has_any_role(user["id"], list(store.ADMIN_ROLES), org_id=org["id"]):
        return None, None, refuse(403, "Beacon Admin or Setup Admin access at this entity is required.")
    if not store.entity_allowed(org):
        return None, None, refuse(403, "SMA letters are available for the Episcopal Diocese of Maryland only.")
    if not store.tables_ready():
        return None, None, refuse(503, "SMA letters are not set up yet (migration 075 has not been applied).")
    return user, org, None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _who(user: dict) -> str:
    return user.get("display_name") or user.get("email") or "Admin"


def _redirect(url: str, **q) -> RedirectResponse:
    qs = "&".join(f"{k}={quote(str(v))}" for k, v in q.items() if v)
    return RedirectResponse(url + (("?" + qs) if qs else ""), status_code=303)


def _run_url(rid: int) -> str:
    return f"/admin/sma-letters/{rid}"


def _letter_url(rid: int, lid: int) -> str:
    return f"/admin/sma-letters/{rid}/letters/{lid}"


def _not_found():
    return HTMLResponse("Not found.", status_code=404)


def _fmt(dt, org_id: int) -> str:
    return org_time.format_local(dt, org_time.zone_name_for_org(org_id)) if dt else ""


def _csv_safe(value) -> str:
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


async def _read_upload(form, name: str) -> tuple[bytes, str]:
    f = form.get(name)
    if f is None or not hasattr(f, "read"):
        raise store.SmaError("Choose a file first.")
    data = await f.read(MAX_UPLOAD + 1)
    if not data:
        raise store.SmaError("That file is empty.")
    if len(data) > MAX_UPLOAD:
        raise store.SmaError("That file is too large.")
    return data, str(getattr(f, "filename", "") or "file")


_USER_ERRORS = (store.SmaError, sma_model.ModelError, sma_pdf.PdfError, fs.FormstackError)


# ---------------------------------------------------------------------------------------------
# display shaping
# ---------------------------------------------------------------------------------------------
def _row(L: dict, run: dict) -> dict:
    flags = L.get("flags") or []
    prior = C.D(L["prior_allocation"])
    total = L["total_allocation"]
    change = None
    if total is not None and prior > 0:
        change = round((Decimal_to_float(C.D(total) - prior) / float(prior)) * 100)
    notes = L.get("notes") or []
    # the newest note of ANY kind (a staff note, or an automatic one such as a signer change) except the one that only
    # says where the row came from -- Jay (D4): signer changes are recorded in the notes on the check sheet
    last = next((n for n in reversed(notes) if not str(n.get("text") or "").startswith("Loaded from the model file")), None)
    signers = sma_signers.numbered(L.get("signers") or [])
    return {
        "id": L["id"], "code": L.get("parish_code") or "", "parish_name": L.get("parish_name") or "", "model_name": L["model_name"],
        "letter_name": L["letter_name"], "status": L["status"], "status_label": STATUS_LABEL.get(L["status"], L["status"]),
        "total": C.money(total) if total is not None else "", "formula_total": C.money(L["formula_total"]) if L["formula_total"] is not None else "",
        "prior": C.money(prior) if prior else "", "change": ((f"{change:+d}%" if change else "0%") if change is not None else ""),
        "model_total_text": C.money(L["model_total"]) if L.get("model_total") is not None else "",
        "adjustment": ADJ_LABEL.get(L["adjustment_kind"], ""), "adjusted": L["adjustment_kind"] != "none",
        "tie": L["tie_out"], "flags": flags,
        "blocks": sum(1 for f in flags if f["severity"] == "block"), "warns": sum(1 for f in flags if f["severity"] == "warn"),
        "signers": [{"role": sma_signers.ROLE_LABEL[s["role"]], "name": s.get("name") or s.get("email")} for s in signers],
        "version": L["current_version"], "note": (last or {}).get("text", ""), "note_auto": bool((last or {}).get("auto")),
        "note_by": (last or {}).get("by", ""), "note_at": str((last or {}).get("at", ""))[:10], "match": L["match_status"],
    }


def Decimal_to_float(d) -> float:
    return float(d)


def _summary_ctx(letters: list[dict]) -> dict:
    return store.summary(letters)


# ---------------------------------------------------------------------------------------------
# list and new run
# ---------------------------------------------------------------------------------------------
@router.get("/admin/sma-letters", response_class=HTMLResponse)
def runs_page(request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    runs = store.list_runs(org["id"])
    for r in runs:
        r["status_label"] = RUN_STATUS_LABEL.get(r["status"], r["status"])
        r["created_text"] = _fmt(r["created_at"], org["id"])
    return _render(request, "admin_sma_letters.html", user, {
        "runs": runs, "error": request.query_params.get("error") or "", "msg": request.query_params.get("msg") or ""})


@router.post("/admin/sma-letters/new")
async def run_new(request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    form = await request.form()
    try:
        content, filename = await _read_upload(form, "model_file")
        run_type, title, test_address = str(form.get("run_type") or ""), str(form.get("title") or ""), str(form.get("test_address") or "")
        # reading the workbook and creating every row is slow: off the event loop, so other users are not held up
        rid = await run_in_threadpool(lambda: store.create_run(org, user["id"], content=content, filename=filename, run_type=run_type,
                                                              title=title, test_address=test_address))
    except _USER_ERRORS as exc:
        return _redirect("/admin/sma-letters", error=str(exc))
    return _redirect(_run_url(rid), msg="Run created. Review the rates, upload the cover letter and settle the flags.")


# ---------------------------------------------------------------------------------------------
# the check sheet
# ---------------------------------------------------------------------------------------------
@router.get("/admin/sma-letters/{rid}", response_class=HTMLResponse)
def run_page(rid: int, request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    run = store.get_run(org["id"], rid)
    if not run:
        return _not_found()
    letters = store.list_letters(rid)
    rows = [_row(L, run) for L in letters]
    cfg = run.get("config") or {}
    bs = store.build_status(run, letters)
    test = run["run_type"] == "test"
    try:
        al = fs.allowance(test)
        allowance = {"ok": True, "used": al["used"], "limit": al["limit"], "remaining": al["remaining"],
                     "resets": datetime.fromtimestamp(al["resets_at_epoch"], timezone.utc).astimezone(
                         ZoneInfo(org_time.zone_name_for_org(org["id"]))).strftime("%b %d, %I:%M %p").replace(" 0", " ")
                     if al["resets_at_epoch"] else ""}
    except Exception as exc:       # Formstack unreachable or credentials unavailable: the page still works
        allowance = {"ok": False, "why": str(exc) if isinstance(exc, fs.FormstackError) else "unavailable"}
    ids = store.template_ids()
    assumptions = (run.get("model_summary") or {}).get("assumptions") or {}
    return _render(request, "admin_sma_run.html", user, {
        "run": run, "run_status_label": RUN_STATUS_LABEL.get(run["status"], run["status"]), "rows": rows,
        "summary": _summary_ctx(letters), "build": bs, "allowance": allowance, "test": test,
        "rates": {"noi": C.pct(run["noi_rate"]), "noe": C.pct(run["noe_rate"]), "flat": C.money(run["flat_deduction"]),
                  "lesser": bool(run["lesser_of"])},
        "rates_confirmed": bool(cfg.get("rates_confirmed_at")), "rates_by": cfg.get("rates_confirmed_by", ""),
        "assumptions": assumptions, "model_summary": run.get("model_summary") or {},
        "templates_ready": bool(ids["letter"]), "cover_uploaded": _fmt(run["cover_uploaded_at"], org["id"]),
        "merges": {"real": cfg.get("merges_real", 0), "test": cfg.get("merges_test", 0)},
        "created_text": _fmt(run["created_at"], org["id"]), "open": run["status"] in store.RUN_OPEN,
        "error": request.query_params.get("error") or "", "msg": request.query_params.get("msg") or ""})


@router.post("/admin/sma-letters/{rid}/confirm-rates")
async def confirm_rates(rid: int, request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    if not store.get_run(org["id"], rid):
        return _not_found()
    try:
        store.confirm_rates(org["id"], rid, _who(user))
    except _USER_ERRORS as exc:
        return _redirect(_run_url(rid), error=str(exc))
    return _redirect(_run_url(rid), msg="Rates confirmed.")


@router.post("/admin/sma-letters/{rid}/cover")
async def upload_cover(rid: int, request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    if not store.get_run(org["id"], rid):
        return _not_found()
    form = await request.form()
    try:
        content, filename = await _read_upload(form, "cover_file")
        pages = await run_in_threadpool(store.set_cover, org["id"], rid, content, filename)
    except _USER_ERRORS as exc:
        return _redirect(_run_url(rid), error=str(exc))
    return _redirect(_run_url(rid), msg=f"Cover letter saved ({pages} page{'s' if pages != 1 else ''}). Letters built earlier now need a rebuild.")


@router.post("/admin/sma-letters/{rid}/templates")
async def send_templates(rid: int, request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    if not store.get_run(org["id"], rid):
        return _not_found()
    try:
        ids = await run_in_threadpool(store.sync_templates, user["id"])
        store.attach_templates(org["id"], rid, ids)
    except _USER_ERRORS as exc:
        return _redirect(_run_url(rid), error=str(exc))
    return _redirect(_run_url(rid), msg="The letter and signing-form templates are in Formstack Documents. No merges were used.")


@router.post("/admin/sma-letters/{rid}/build-chunk")
def build_chunk(rid: int, request: Request):
    user, org, err = _guard(request, as_json=True)
    if err:
        return err
    if not store.get_run(org["id"], rid):
        return JSONResponse({"error": "Run not found"}, status_code=404)
    try:
        return store.build_chunk(org["id"], rid, user["id"])
    except _USER_ERRORS as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@router.get("/admin/sma-letters/{rid}/export.csv")
def export_csv(rid: int, request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    run = store.get_run(org["id"], rid)
    if not run:
        return _not_found()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Code", "Parish", "Letter name", "Allocation", "Formula result", "Adjustment", "Prior allocation", "Status",
                "Blocks", "Warnings", "Signers", "Flags", "Notes"])
    for L in store.list_letters(rid):
        r = _row(L, run)
        notes = " | ".join(f"{n.get('by', '')}: {n.get('text', '')}" for n in (L.get("notes") or []))
        w.writerow([_csv_safe(x) for x in [r["code"], r["parish_name"] or r["model_name"], r["letter_name"], r["total"], r["formula_total"],
                                           r["adjustment"], r["prior"], r["status_label"], r["blocks"], r["warns"],
                                           "; ".join(f"{s['role']}: {s['name']}" for s in r["signers"]),
                                           " / ".join(f["text"] for f in r["flags"]), notes]])
    name = f"SMA {run['year']} check sheet.csv"
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": upload_guard.content_disposition(name, True), "Cache-Control": "no-store"})


# ---------------------------------------------------------------------------------------------
# one parish
# ---------------------------------------------------------------------------------------------
@router.get("/admin/sma-letters/{rid}/letters/{lid}", response_class=HTMLResponse)
def letter_page(rid: int, lid: int, request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    run = store.get_run(org["id"], rid)
    L = store.get_letter(rid, lid) if run else None
    if not run or not L:
        return _not_found()
    row = _row(L, run)
    signers = []
    for i, s in enumerate(L.get("signers") or []):
        signers.append({"index": i, "role": s["role"], "role_label": sma_signers.ROLE_LABEL[s["role"]], "name": s.get("name", ""),
                        "email": s.get("email", ""), "title": s.get("title", ""), "source": s.get("source", ""),
                        "chosen": bool(s.get("chosen"))})
    notes = [{"at": _fmt(datetime.fromisoformat(n["at"]), org["id"]), "by": n.get("by", ""), "auto": bool(n.get("auto")),
              "text": n.get("text", "")} for n in reversed(L.get("notes") or [])]
    versions = store.list_versions(lid)
    for v in versions:
        v["created_text"] = _fmt(v["created_at"], org["id"])
    ctx = {
        "run": run, "L": L, "row": row, "signers": signers, "notes": notes, "versions": versions,
        "numbered": sma_signers.numbered(L.get("signers") or []), "roles": [(r, sma_signers.ROLE_LABEL[r]) for r in sma_signers.ROLE_ORDER],
        "parish_options": store.candidate_parishes(org["id"], rid) if not L["parish_id"] else [],
        "adjustment_kinds": [(k, ADJ_LABEL[k]) for k in C.ADJUSTMENT_KINDS],
        "suggested": (L.get("quality") or {}).get("suggested_adjustment"),
        "default_texts": C.DEFAULT_ADJUSTMENT_TEXT, "open": run["status"] in store.RUN_OPEN,
        "can_rebuild": store._buildable(L, allow_failed=True) and L["status"] != "excluded",
        "figures": {"noi_y1": L["noi_y1"], "noi_y2": L["noi_y2"], "noi_y3": L["noi_y3"], "noe": L["noe"], "prior_allocation": L["prior_allocation"]},
        "error": request.query_params.get("error") or "", "msg": request.query_params.get("msg") or ""}
    return _render(request, "admin_sma_letter.html", user, ctx)


@router.get("/admin/sma-letters/{rid}/letters/{lid}/pdf")
def letter_pdf(rid: int, lid: int, request: Request, v: int = 0):
    user, org, err = _guard(request)
    if err:
        return err
    got = store.letter_pdf(org["id"], rid, lid, v or None)
    if not got:
        return _not_found()
    data, name = got
    media, disposition = upload_guard.serve_headers(data, name)
    return Response(data, media_type=media, headers={"Content-Disposition": disposition, "Cache-Control": "private, no-store"})


async def _act(request: Request, rid: int, lid: int, fn):
    """Run one per-parish action: guard, form, call, redirect back with a message or the error."""
    user, org, err = _guard(request)
    if err:
        return err
    run = store.get_run(org["id"], rid)
    if not run or not store.get_letter(rid, lid):
        return _not_found()
    form = await request.form()
    try:
        msg = await run_in_threadpool(fn, org, user, form)
    except _USER_ERRORS as exc:
        return _redirect(_letter_url(rid, lid), error=str(exc))
    return _redirect(_letter_url(rid, lid), msg=msg or "Saved.")


@router.post("/admin/sma-letters/{rid}/letters/{lid}/update")
async def letter_update(rid: int, lid: int, request: Request):
    def go(org, user, form):
        figures = {k: form.get(k) for k in ("noi_y1", "noi_y2", "noi_y3", "noe", "prior_allocation") if form.get(k) is not None}
        store.update_letter(org["id"], rid, lid, _who(user), letter_name=form.get("letter_name"), figures=figures,
                            reason=str(form.get("reason") or ""))
        return "Saved. If the figures or the name changed, rebuild the letter."
    return await _act(request, rid, lid, go)


@router.post("/admin/sma-letters/{rid}/letters/{lid}/parish")
async def letter_parish(rid: int, lid: int, request: Request):
    def go(org, user, form):
        if form.get("action") == "clear":
            store.set_parish(org["id"], rid, lid, _who(user), None)
            return "Parish match removed."
        pid = _int(form.get("parish_id"))
        if pid is None:
            raise store.SmaError("Choose a parish from the list.")
        store.set_parish(org["id"], rid, lid, _who(user), pid)
        return "Parish match saved."
    return await _act(request, rid, lid, go)


@router.post("/admin/sma-letters/{rid}/letters/{lid}/adjustment")
async def letter_adjustment(rid: int, lid: int, request: Request):
    def go(org, user, form):
        store.set_adjustment(org["id"], rid, lid, _who(user), str(form.get("kind") or "none"), str(form.get("text") or ""),
                             form.get("amount"))
        return "Adjustment saved. If the letter is already built, rebuild it so it prints the adjustment."
    return await _act(request, rid, lid, go)


@router.post("/admin/sma-letters/{rid}/letters/{lid}/exclude")
async def letter_exclude(rid: int, lid: int, request: Request):
    def go(org, user, form):
        store.set_excluded(org["id"], rid, lid, _who(user), form.get("action") != "include", str(form.get("reason") or ""))
        return "Saved."
    return await _act(request, rid, lid, go)


@router.post("/admin/sma-letters/{rid}/letters/{lid}/signers")
async def letter_signers(rid: int, lid: int, request: Request):
    def go(org, user, form):
        act = str(form.get("action") or "")
        by = _who(user)
        index = _int(form.get("index"))
        index = -1 if index is None else index
        if act == "choose":
            store.choose_signer(org["id"], rid, lid, by, str(form.get("role") or ""), index)
        elif act == "none":
            store.unchoose_role(org["id"], rid, lid, by, str(form.get("role") or ""))
        elif act == "add":
            store.add_signer(org["id"], rid, lid, by, str(form.get("role") or ""), str(form.get("name") or ""), str(form.get("email") or ""))
        elif act == "remove":
            store.remove_signer(org["id"], rid, lid, by, index)
        elif act == "reset":
            store.reset_signers(org["id"], rid, lid, by)
        else:
            raise store.SmaError("Unknown signer action.")
        return "Signers saved."
    return await _act(request, rid, lid, go)


@router.post("/admin/sma-letters/{rid}/letters/{lid}/note")
async def letter_note(rid: int, lid: int, request: Request):
    def go(org, user, form):
        store.add_note(org["id"], rid, lid, _who(user), str(form.get("text") or ""))
        return "Note added."
    return await _act(request, rid, lid, go)


@router.post("/admin/sma-letters/{rid}/letters/{lid}/rebuild")
async def letter_rebuild(rid: int, lid: int, request: Request):
    def go(org, user, form):
        store.rebuild_letter(org["id"], rid, lid, user["id"], _who(user), str(form.get("reason") or "rebuilt"))
        return "Letter rebuilt as a new version."
    return await _act(request, rid, lid, go)
