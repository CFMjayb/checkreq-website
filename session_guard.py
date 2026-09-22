"""
session_guard.py -- session absolute-timeout, app-level rate limiting, and
the raw-*.run.app-hostname redirect (Security Assessment 2026-09-19,
findings M15/L1/L9). One small module rather than growing main.py further,
per this codebase's own standing convention.

Three independent pieces, each its own ASGI middleware, wired into main.py.
Registration order matters and is easy to get backwards: Starlette's
add_middleware() PREPENDS to the middleware list, and the stack is built by
wrapping in REVERSED list order -- so the middleware added MOST RECENTLY
ends up OUTERMOST (runs FIRST on the way in, LAST on the way out), and the
one added EARLIEST ends up INNERMOST (runs LAST on the way in, closest to
the route). SessionAbsoluteCapMiddleware reads request.session, so it must
be added BEFORE SessionMiddleware (making it inner, so it runs after
SessionMiddleware has populated the scope) -- added after, as an earlier
draft of main.py did, it 500s on every single request, caught only by
booting the app and hitting a real route (py_compile can't see this).

Actual main.py add_middleware() call order (first-added to last-added):
SessionAbsoluteCapMiddleware, SessionMiddleware, SecurityHeadersMiddleware,
RateLimitMiddleware, RawRunAppRedirectMiddleware, then canonicalize_localhost
(a plain @app.middleware("http") def further down the file -- registered
last of all, so it's the true OUTERMOST layer and runs before every one of
the above, confirmed live by its own docstring). Reversing that list gives
the real outermost-to-innermost execution order on the way in:
canonicalize_localhost -> RawRunAppRedirectMiddleware -> RateLimitMiddleware
-> SecurityHeadersMiddleware -> SessionMiddleware -> SessionAbsoluteCapMiddleware
-> the route.

Why an in-process dict, not Redis/Memorystore: this is a small internal
staff app (`max-instances: 2`), and the report's own §2 M15 explicitly rates
this Medium-Low and offers app-level limiting as option (a) versus a real
edge (Cloud Armor, ~$20/mo, option (b)) -- Jay chose (a). An in-memory
counter is NOT perfectly consistent across 2 instances (a client could in
principle get roughly double the stated limit by hitting both), but it is
real, free, and closes the "unlimited requests" gap the report actually
flagged -- a determined attacker being slowed by ~2x the limit instead of
not at all is still a large improvement. If this app ever needs true
cross-instance accuracy, Postgres (already available everywhere else in
this codebase) is the natural upgrade, not a new dependency.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

# ── L9: session absolute cap ────────────────────────────────────────────────
# Jay, 2026-09-19: "60/8" -- 60-minute idle timeout (the SessionMiddleware
# cookie's own max_age, set in main.py; Starlette re-issues the cookie's
# Max-Age on every response, so it naturally expires 60 min after the LAST
# request) plus an 8-hour ABSOLUTE cap from login regardless of activity,
# which needs its own clock since cookie max_age alone only ever measures
# "time since the last request," never "time since login."
SESSION_MAX_AGE_SECONDS = 60 * 60           # idle timeout, applied to SessionMiddleware itself
SESSION_ABSOLUTE_CAP_SECONDS = 8 * 60 * 60  # absolute cap from _login_at


class SessionAbsoluteCapMiddleware(BaseHTTPMiddleware):
    """Forces a fresh login once 8 real hours have passed since `_login_at`
    was stamped (auth_routes.py's _complete_login -- the one gate every
    provider funnels through), independent of how recently the user was
    last active. A session with no `_login_at` at all (e.g. one already
    live before this shipped) is left alone rather than force-logged-out --
    it will pick up a real timestamp the next time it re-authenticates."""

    async def dispatch(self, request: Request, call_next):
        login_at = request.session.get("_login_at")
        if login_at is not None and (time.time() - login_at) > SESSION_ABSOLUTE_CAP_SECONDS:
            request.session.clear()
            if request.url.path.startswith("/api/") or request.url.path.startswith("/internal/"):
                return JSONResponse({"error": "session expired"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)


# ── M15: app-level rate limiting on /auth/* + /internal/* ──────────────────
# Deliberately narrow scope, matching the report's own finding: "/login,
# /auth/route, and the emailed-code/password fallbacks accept unlimited
# requests." Everything behind a real login already has its own
# authorization gate; this targets only the pre-authentication surface an
# anonymous caller can hammer.
_RATE_LIMITED_PREFIXES = ("/auth/", "/login", "/internal/")
_WINDOW_SECONDS = 60
_MAX_REQUESTS_PER_WINDOW = 20  # per client IP, per rate-limited path prefix

_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def _bucket_key(ip: str, path: str) -> tuple[str, str]:
    # Bucket by the FIRST path segment under /auth/ (e.g. /auth/start,
    # /auth/callback) rather than the full path, so a burst across several
    # distinct auth sub-routes from one IP still counts against one limit
    # instead of getting 20 free requests per sub-route.
    parts = path.strip("/").split("/", 2)
    prefix = "/".join(parts[:2]) if len(parts) > 1 else parts[0]
    return ip, prefix


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, client_ip_fn):
        super().__init__(app)
        self._client_ip_fn = client_ip_fn

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not any(path.startswith(p) for p in _RATE_LIMITED_PREFIXES):
            return await call_next(request)

        ip = self._client_ip_fn(request) or "unknown"
        key = _bucket_key(ip, path)
        now = time.time()
        bucket = _hits[key]
        while bucket and now - bucket[0] > _WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= _MAX_REQUESTS_PER_WINDOW:
            return JSONResponse(
                {"error": "Too many requests. Please wait a minute and try again."},
                status_code=429,
                headers={"Retry-After": str(_WINDOW_SECONDS)},
            )
        bucket.append(now)
        return await call_next(request)


# ── M15: redirect the raw *.run.app hostname to the real branded domain ────
# Jay's own served hostnames are all custom domains; the raw Cloud Run URL
# answering identically alongside them just widens the reachable surface
# for no reason (report: "both default *.run.app URLs answer alongside the
# 6 custom hostnames"). A redirect (not a hard block) keeps a raw-URL bookmark
# or an old link still usable, rather than dead.
class RawRunAppRedirectMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, canonical_host: str):
        super().__init__(app)
        self._canonical_host = canonical_host

    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "")
        if host.endswith(".run.app"):
            target = f"https://{self._canonical_host}{request.url.path}"
            if request.url.query:
                target += f"?{request.url.query}"
            return RedirectResponse(target, status_code=307)
        return await call_next(request)
