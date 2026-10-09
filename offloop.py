"""offloop.py -- keep slow synchronous work in an `async def` route off the server's event loop.

Why (2026-10-09, the 8.5 s stall behind production CR26-017): Beacon runs ONE uvicorn process, so ONE event loop serves every
request on an instance. An `async def` route that calls synchronous slow work (a QuickBooks or SharePoint call, an email, a
Chromium PDF render, the Anthropic API) holds that loop for the whole time, and every other request on the instance waits
behind it: other users' pages, approvals, /health. Plain `def` routes are fine (Starlette runs them in a thread pool); the
problem is the ~30 `async def` routes that only need `async` for `await request.form()` / `await file.read()` and then do
blocking work.

    @router.post("/something")
    @offloop
    async def something(request: Request): ...            # unchanged body, still using `await request.form()`

What the decorator does
  1. On the server's own loop, reads the raw request body (`await request.body()`) and, for a form, parses it
     (`await request.form()`). Starlette caches both on the Request, so `await request.form()`, `await request.json()` and
     `await request.body()` inside the body return instantly, without touching the connection (which belongs to the
     server's loop).
  2. Runs the unchanged coroutine to completion in a worker thread on its OWN private event loop (a plain selector loop,
     not uvloop: the server runs uvloop, and this loop only ever awaits file reads and `asyncio.to_thread`). Blocking there
     costs nobody else anything. The request's contextvars (the per-request database connection) travel with the thread.
  3. Keeps the route's signature (path / query / form / dependency parameters) exactly as FastAPI sees it, including
     modules that use `from __future__ import annotations` (the annotations are resolved in the route's OWN module).

Not for routes that stream, hold a WebSocket, or rely on the server's loop (none do today). A route with no `await` at
all can simply be changed to a plain `def` instead.
"""
from __future__ import annotations

import asyncio
import functools
import inspect

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

_BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_FORM_TYPES = ("multipart/form-data", "application/x-www-form-urlencoded")


def run_in_own_event_loop(coro_fn, *args, **kwargs):
    """Runs one coroutine function to completion on a fresh event loop in the calling (worker) thread."""
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coro_fn(*args, **kwargs))


def offloop(fn):
    """Decorator for an `async def` route (or background task): see the module docstring. Place it directly above the
    `async def`, below the route decorator."""
    if not inspect.iscoroutinefunction(fn):
        return fn
    signature = inspect.signature(fn, eval_str=True)     # resolved in fn's own module, so FastAPI needs no globals of ours

    @functools.wraps(fn)
    async def shell(*args, **kwargs):
        request = next((v for v in (*args, *kwargs.values()) if isinstance(v, Request)), None)
        if request is not None and request.method in _BODY_METHODS:
            content_type = (request.headers.get("content-type") or "").lower()
            # Errors propagate exactly as they would have from the route itself (a bad multipart body, a dropped
            # connection): nothing is swallowed here. The RAW body is cached first, whatever the content type: a route
            # may call form(), json() or body() on it, in any order and whatever the client sent, and each of them
            # then works from the cache (reading the stream as a form first would leave json() with "Stream consumed").
            try:
                await request.body()
            except RuntimeError as exc:
                # FastAPI has already parsed the form itself (the route declares Form(...) / File(...) parameters) and
                # cached it on this same Request: that cached form is what the route will use, so there is nothing to
                # read. Any other error is real and propagates.
                if "Stream consumed" not in str(exc):
                    raise
            if content_type.startswith(_FORM_TYPES):
                await request.form()
        return await run_in_threadpool(run_in_own_event_loop, fn, *args, **kwargs)

    shell.__signature__ = signature
    shell.__offloop__ = True                              # lets tests and the startup check see which routes are covered
    return shell
