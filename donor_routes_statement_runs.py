"""
donor_routes_statement_runs.py -- Beacon Donor Management, giving (tax) statements: the STAFF RUN screens. Routes only: every rule lives in statement_runs.

  GET  /giving/statements/run                       choose a year and options; the open runs; the history of every run
  POST /giving/statements/run/plan                  the preview list (builds no PDF): who gets a statement and the plain reason for everyone who does not
  GET  /giving/statements/run/{id}                  one run: its counts, the reasons, a Build button with a progress bar, the statements and the downloads
  POST /giving/statements/run/{id}/chunk            build the next few statements; answers JSON with the progress (the page calls it again and again)
  POST /giving/statements/run/{id}/retry            put the failed ones back so the next chunk builds them
  POST /giving/statements/run/{id}/cancel           stop a run that is not finished
  GET  /giving/statements/run/{id}/print.pdf        ONE PDF of every statement meant for print, in the run's order
  GET  /giving/statements/run/{id}/all.zip          a ZIP, one PDF per donor, in a Print and an Email folder
  GET  /giving/statements/run/{id}/skipped.csv      who was skipped and why
  GET  /giving/statements/file/{statement_id}.pdf   one built statement

Nothing here sends a statement to anyone. The parish ALWAYS comes from the session through donor_web.gate; a run id or a statement id in a URL is only a key looked up
INSIDE that parish. The chunk route is a plain `def`, so the web server runs it in its worker threads and the site stays responsive while it renders. Every download is
private (no-store, no-index) and writes one audit line. Run pages need statements.run.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import donor_web as W
import statement_runs as SRN
from donor_core import DonorError, NotFound

router = APIRouter(dependencies=[Depends(W.check_path_ids)])


def register(app) -> None:
    app.include_router(router)


def _gate(request: Request):
    user, parish, ctx, resp = W.gate(request, need=None, feature="giving", active="statements")
    if resp:
        return None, None, None, resp
    if not ctx.can("statements.run"):
        return None, None, None, W.page(request, "donor_off.html", user, parish, ctx, "statements", {"reason": "permission", "feature": "giving"}, status_code=403)
    return user, parish, ctx, None


def _private(resp):
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _missing(request, user, parish, ctx, e: NotFound):
    return W.page(request, "donor_off.html", user, parish, ctx, "statements",
                  {"reason": "notfound", "feature": "giving", "what": e.message, "back_href": "/giving/statements/run"}, status_code=404)


def _download(data: bytes, media: str, name: str, *, inline: bool = False):
    safe = "".join(ch for ch in name if ch.isalnum() or ch in " ._-()")[:120] or "statement"
    return _private(Response(content=data, media_type=media, headers={"Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{safe}"'}))


@router.get("/giving/statements/run", response_class=HTMLResponse)
def run_home(request: Request):
    user, parish, ctx, resp = _gate(request)
    if resp:
        return resp
    try:
        opts = SRN.run_options(ctx)
        runs = SRN.history(ctx)
    except DonorError as e:
        return W.back(request, "/giving/statements", err=e.message)
    return W.page(request, "donor_statement_run_home.html", user, parish, ctx, "statements", {"opts": opts, "runs": runs, "can_templates": ctx.can("statements.templates")})


@router.post("/giving/statements/run/plan")
async def run_plan(request: Request):
    user, parish, ctx, resp = _gate(request)
    if resp:
        return resp
    form = await request.form()
    try:
        tid = int(form.get("template_id")) if form.get("template_id") else None
    except (TypeError, ValueError):
        return W.back(request, "/giving/statements/run", err="Pick a template from the list.")
    try:
        plan = SRN.plan_run(ctx, form.get("year"), template_id=tid, delivery=form.get("delivery") or "all", sort=form.get("sort") or "name",
                            name_prefix=form.get("name_prefix") or "", minimum=form.get("minimum"))
    except DonorError as e:
        return W.back(request, "/giving/statements/run", err=e.message)
    return W.back(request, f"/giving/statements/run/{plan['run_id']}", ok=f"The list is ready: {plan['planned']} statements to build, {plan['skipped']} skipped. Nothing is built yet.")


@router.get("/giving/statements/run/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: int):
    user, parish, ctx, resp = _gate(request)
    if resp:
        return resp
    status = request.query_params.get("status") or None
    if status not in (None, "planned", "built", "failed", "skipped", "replaced"):
        status = None
    try:
        page_no = max(0, int(request.query_params.get("page") or 0))
    except ValueError:
        page_no = 0
    try:
        summary = SRN.run_summary(ctx, run_id)
        rows = SRN.list_statements(ctx, run_id, status=status, limit=100, offset=page_no * 100)
    except NotFound as e:
        return _private(_missing(request, user, parish, ctx, e))
    except DonorError as e:
        return W.back(request, "/giving/statements/run", err=e.message)
    return _private(W.page(request, "donor_statement_run.html", user, parish, ctx, "statements", {**summary, "rows": rows, "status": status, "page_no": page_no}))


@router.post("/giving/statements/run/{run_id}/chunk")
def run_chunk(request: Request, run_id: int):
    user, parish, ctx, resp = _gate(request)
    if resp:
        return _private(JSONResponse({"error": "Please sign in again."}, status_code=401))
    try:
        progress = SRN.build_chunk(ctx, run_id)
    except NotFound as e:
        return _private(JSONResponse({"error": e.message}, status_code=404))
    except DonorError as e:
        return _private(JSONResponse({"error": e.message}, status_code=400))
    except Exception:
        return _private(JSONResponse({"error": "That did not work. Please try again."}, status_code=500))
    return _private(JSONResponse({k: v for k, v in progress.items() if isinstance(v, (int, str, bool, type(None)))}))


@router.post("/giving/statements/run/{run_id}/retry")
async def run_retry(request: Request, run_id: int):
    user, parish, ctx, resp = _gate(request)
    if resp:
        return resp
    try:
        n = SRN.retry_failed(ctx, run_id)
    except DonorError as e:
        return W.back(request, f"/giving/statements/run/{run_id}", err=e.message)
    return W.back(request, f"/giving/statements/run/{run_id}", ok=f"{n} failed statement{'s' if n != 1 else ''} put back. Press Build to try again." if n else "Nothing had failed.")


@router.post("/giving/statements/run/{run_id}/cancel")
async def run_cancel(request: Request, run_id: int):
    user, parish, ctx, resp = _gate(request)
    if resp:
        return resp
    try:
        r = SRN.cancel_run(ctx, run_id)
    except DonorError as e:
        return W.back(request, f"/giving/statements/run/{run_id}", err=e.message)
    return W.back(request, f"/giving/statements/run/{run_id}", ok=f"The run was cancelled. {r['built']} statements already built are kept.")


@router.get("/giving/statements/run/{run_id}/print.pdf")
def run_print(request: Request, run_id: int):
    user, parish, ctx, resp = _gate(request)
    if resp:
        return resp
    try:
        data, name = SRN.print_pdf(ctx, run_id)
    except DonorError as e:
        return W.back(request, f"/giving/statements/run/{run_id}", err=e.message)
    return _download(data, "application/pdf", name)


@router.get("/giving/statements/run/{run_id}/all.zip")
def run_zip(request: Request, run_id: int):
    user, parish, ctx, resp = _gate(request)
    if resp:
        return resp
    try:
        data, name = SRN.zip_all(ctx, run_id)
    except DonorError as e:
        return W.back(request, f"/giving/statements/run/{run_id}", err=e.message)
    return _download(data, "application/zip", name)


@router.get("/giving/statements/run/{run_id}/skipped.csv")
def run_skipped(request: Request, run_id: int):
    user, parish, ctx, resp = _gate(request)
    if resp:
        return resp
    try:
        data, name = SRN.skipped_csv(ctx, run_id)
    except DonorError as e:
        return W.back(request, f"/giving/statements/run/{run_id}", err=e.message)
    return _download(data, "text/csv; charset=utf-8", name)


@router.get("/giving/statements/file/{statement_id}.pdf")
def statement_pdf(request: Request, statement_id: int):
    user, parish, ctx, resp = _gate(request)
    if resp:
        return resp
    try:
        data, name = SRN.statement_file(ctx, statement_id)
    except NotFound as e:
        return _private(_missing(request, user, parish, ctx, e))
    except DonorError as e:
        return W.back(request, "/giving/statements/run", err=e.message)
    return _download(data, "application/pdf", name, inline=True)


@router.get("/giving/statements/changed", response_class=HTMLResponse)
def changed_page(request: Request):
    user, parish, ctx, resp = _gate(request)
    if resp:
        return resp
    year = request.query_params.get("year") or None
    try:
        rows = SRN.changed_since_issued(ctx, year)
    except DonorError as e:
        return W.back(request, "/giving/statements/run", err=e.message)
    return _private(W.page(request, "donor_statement_changed.html", user, parish, ctx, "statements", {"rows": rows, "year": year}))


@router.post("/giving/statements/file/{statement_id}/reissue")
def statement_reissue(request: Request, statement_id: int):
    user, parish, ctx, resp = _gate(request)
    if resp:
        return resp
    try:
        r = SRN.reissue(ctx, statement_id)
    except DonorError as e:
        return W.back(request, "/giving/statements/changed", err=e.message)
    return W.back(request, "/giving/statements/changed", ok="A corrected statement was built. The old one is kept and marked as replaced.")
