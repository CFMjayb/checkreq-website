"""
csrf_guard.py -- CSRF tokens on every state-changing request (Security
Assessment 2026-09-19, finding M8). One small module rather than growing
main.py further, mirroring session_guard.py's own convention (both are
2026-09-21 overnight-batch work).

No CSRF protection existed anywhere in this app before this. Protection
rested entirely on SameSite=Lax cookies, which the report itself called
out as real but incomplete: Lax still sends the session cookie on a
top-level GET navigation, so main.py's GET /select-entity/{org_id},
auth_routes.py's GET /logout, and parish_mode.py's GET /parish-view/switch
were all reachable cross-site with no user interaction beyond visiting an
attacker-controlled page (all three converted to POST in this same pass --
see their own docstrings). This module is the real per-session token the
report asked for, checked on every POST/PUT/DELETE/PATCH request.

Token lifecycle -- one function, two call sites:
ensure_token() is get-or-create (session.setdefault, not unconditional
overwrite), called from BOTH auth_routes.py's _complete_login() (a fresh
token stamped at every real login, mirroring exactly where _login_at is
stamped -- Jay's own "one gate every provider funnels through" pattern from
the L9 fix) AND main.py's _render()/the csrf_token Jinja global below (a
plain no-op setdefault for a session that already has one). The second call
site exists ONLY to backfill a session that was already live before this
shipped -- without it, every already-signed-in user's very next POST after
this deploys would 403 with no token to check against, forcing a surprise
re-login mid-session. With it, their next ordinary page GET silently gains
a token (via the csrf_token(request) Jinja global every form/meta-tag
below calls) before they ever submit anything.

Where the submitted token is read from -- and the one real implementation
gotcha this hinges on, found empirically before writing a line of the
middleware, not assumed: calling `await request.form()` inside
BaseHTTPMiddleware.dispatch() on this app's installed Starlette (1.3.1)
consumes the request body in a way that does NOT replay to the downstream
route -- the route's own later `await request.form()` call comes back
empty. Every POST route in this app that reads form data would have broken
silently. `await request.body()` (the raw bytes), by contrast, DOES replay
correctly to the downstream route -- confirmed with a real BaseHTTPMiddleware
+ TestClient round trip, both urlencoded and multipart, including a real
file upload arriving intact on the other side. So this middleware reads the
raw body once via request.body() (which Starlette then correctly replays),
and parses the csrf_token field out of THAT buffered copy by building a
disposable "shadow" Request on a one-shot fake receive() that just returns
the buffered bytes -- this reuses Starlette's own real urlencoded/multipart
parser (not a hand-rolled regex) and never touches the real request's
receive channel at all, so the actual route handler downstream sees its
form exactly as if this middleware had never run.

For a request with no <form> to carry a hidden field at all -- a JSON body,
or an intentionally empty POST like "mark this notification read" -- the
token instead travels as an X-CSRF-Token header (static/js/csrf.js reads it
off base.html's <meta name="csrf-token"> tag). The header is checked FIRST,
before ever touching the body, so a JSON/no-body POST costs nothing extra.

Exempt paths -- routes that are deliberately unauthenticated or carry their
own single-use token instead of a session, so there is no session-bound
CSRF token to check in the first place:
  /email-action/{token}     -- the token IN THE PATH is itself the
                                single-use authorization; no session at all
  /vendor-w9-upload/{token} -- same
  /internal/*               -- shared-secret X-Internal-Key header,
                                machine-to-machine, no session
  /auth/*                   -- the pre-login surface itself (POST /auth/
                                route, /auth/password/verify, /auth/code/
                                request, /auth/code/verify). No session-
                                bound token exists yet at this point --
                                ensure_token() is only ever stamped ONCE a
                                real identity is established, inside
                                _complete_login(), which these routes call
                                INTO, not out of. A forged cross-site POST
                                to one of these can only ever attempt to
                                authenticate the VICTIM'S OWN browser as
                                whatever the attacker chose (classic "login
                                CSRF") -- a real but categorically different,
                                lower-severity concern than M8's "ride an
                                already-authenticated session to perform a
                                privileged action," and out of scope here.
  /dev/auth-as/{email}      -- GET-only (main.py), never reachable through
                                this middleware regardless (it only ever
                                inspects POST/PUT/DELETE/PATCH) -- listed
                                for completeness since the Security
                                Assessment's own M8 write-up names it
                                alongside the others.
"""
from __future__ import annotations

import hmac
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

CSRF_SESSION_KEY = "_csrf_token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"

_STATE_CHANGING_METHODS = {"POST", "PUT", "DELETE", "PATCH"}

_FORM_CONTENT_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")

EXEMPT_PREFIXES = (
    "/email-action/",
    "/vendor-w9-upload/",
    "/internal/",
    "/auth/",
)


def ensure_token(request: Request) -> str:
    """Get-or-create this session's CSRF token -- see module docstring for
    the two call sites and why both exist."""
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def register_globals(templates) -> None:
    """csrf_token(request) as a Jinja2 global, same registration pattern
    main.py already uses for asset_version/BEACON_ENV and auth_routes.py
    for login_branding -- every template that calls TemplateResponse(request,
    ...) gets `request` in context automatically (Starlette), so any
    template can call {{ csrf_token(request) }} directly with no per-route
    context-dict change anywhere. Deliberately get-or-create (calls
    ensure_token(), a real write), not a read-only lookup -- see the module
    docstring's "backfill" paragraph for why a write-on-every-render is the
    right call here, not a hazard."""
    templates.env.globals["csrf_token"] = lambda request: ensure_token(request)


def _is_exempt(path: str) -> bool:
    return any(path.startswith(p) for p in EXEMPT_PREFIXES)


async def _submitted_token(request: Request) -> str | None:
    header_token = request.headers.get(CSRF_HEADER)
    if header_token:
        return header_token

    content_type = request.headers.get("content-type", "")
    if not content_type.startswith(_FORM_CONTENT_TYPES):
        return None

    # See module docstring -- request.form() does NOT replay to the
    # downstream route on this Starlette version; request.body() does.
    body = await request.body()

    async def _replay_once():
        return {"type": "http.request", "body": body, "more_body": False}

    shadow = Request(request.scope, receive=_replay_once)
    try:
        form = await shadow.form()
    except Exception:
        return None
    value = form.get(CSRF_FORM_FIELD)
    return str(value) if value is not None else None


def _reject(request: Request):
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"error": "Your session needs to be refreshed. Please reload the page and try again."},
            status_code=403,
        )
    return HTMLResponse(
        "<h1>Session expired</h1>"
        "<p>Your form session has expired or could not be verified. "
        'Please <a href="javascript:location.reload()">reload the page</a> and try again.</p>',
        status_code=403,
    )


class CSRFMiddleware(BaseHTTPMiddleware):
    """Registered in main.py BEFORE session_guard.SessionAbsoluteCapMiddleware
    (added earliest of all -- see main.py's own comment at the add_middleware
    call site, and session_guard.py's docstring for the general "added
    earliest = innermost = runs last, closest to the route" rule this
    codebase already established). That ordering means: SessionMiddleware
    has already populated request.session by the time this runs (this
    middleware needs it), AND SessionAbsoluteCapMiddleware has already had
    the chance to force-clear and redirect an expired session BEFORE this
    ever evaluates it -- an expired session's stale token is never even
    compared, it's just gone, same as everything else in that session."""

    async def dispatch(self, request: Request, call_next):
        if request.method not in _STATE_CHANGING_METHODS or _is_exempt(request.url.path):
            return await call_next(request)

        session_token = request.session.get(CSRF_SESSION_KEY)
        submitted = await _submitted_token(request)
        if not session_token or not submitted or not hmac.compare_digest(str(submitted), str(session_token)):
            return _reject(request)

        return await call_next(request)
