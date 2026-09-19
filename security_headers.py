"""
security_headers.py -- HTTP security headers on every response (Security
Assessment 2026-09-19, finding M2; also the nosniff half of H2).

Before 2026-09-19 every Beacon response carried only content-type/date/
content-length -- no HSTS, no CSP, no X-Frame-Options, no nosniff, no
Cache-Control on authenticated HTML. This is a pure ASGI middleware (not
Starlette's BaseHTTPMiddleware, which buffers streaming responses and has
known issues with background tasks) that only ever ADDS headers on
`http.response.start`; it never reads or alters a body.

What it sets, and why each is shaped the way it is:

  X-Frame-Options: DENY
  Content-Security-Policy-Report-Only: ... frame-ancestors 'none'
      Nothing in this app is meant to be embedded. The one iframe the app
      itself creates (new_request.js's uploaded-document preview) frames a
      blob: URL, which has no response headers of ours, so DENY does not
      affect it.

  X-Content-Type-Options: nosniff
      Makes the serve-side half of upload_guard.py hold: a browser must
      honor the Content-Type we derived from the bytes, never re-interpret
      a file as HTML.

  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: geolocation=(), microphone=(), camera=()

  Cache-Control: no-store  (HTML responses on authenticated paths only)
      Approval/AP pages should not sit in a shared-machine browser cache.
      Static assets, /health, /login, /auth/*, and /org-logo/* are exempt
      -- they are either public or explicitly cache-busted already.

  Strict-Transport-Security: max-age=31536000; includeSubDomains
      ONLY when on_cloud_run is true. Local dev runs on plain
      http://localhost; HSTS there would poison the browser for every other
      localhost project on the machine.

  Content-Security-Policy-Report-Only  (NOT enforcing -- report-only first)
      Derived from a real inventory of every asset origin in templates/ and
      static/ on 2026-09-19: the only external origin anywhere is Google
      Fonts (fonts.googleapis.com stylesheet -> fonts.gstatic.com font
      files); Tom Select is self-hosted under /static/vendor/; ~25 templates
      carry inline <script> blocks and ~130 inline on*= handlers, ~465
      inline style= attributes and 4 inline <style> blocks (hence
      'unsafe-inline' on both script-src and style-src for now -- removing
      it is a template refactor, M9's own follow-up, not this change);
      new_request.js previews an uploaded file via URL.createObjectURL in
      an <iframe>/<img> (hence blob: on img-src/frame-src); every fetch()
      target is same-origin; the two OAuth providers are reached by a 303
      after a same-origin form POST (/auth/route), which Chrome evaluates
      against form-action, hence those two origins there. Violations show
      up in the browser console as
      "[Report Only] Refused to ..." -- watch dev for a week; if nothing
      unexpected appears, the same string can move to the enforcing
      Content-Security-Policy header. No report-uri/report-to endpoint is
      configured (console only), deliberately -- this app has nowhere
      sensible to POST violation reports yet.
"""
from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP_REPORT_ONLY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "frame-src 'self' blob:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self' https://login.microsoftonline.com https://accounts.google.com; "
    "frame-ancestors 'none'"
)

# Path prefixes whose HTML (if any) is public or already cache-managed --
# these do NOT get Cache-Control: no-store. Everything else that returns
# text/html does. Matched as a prefix on the raw request path.
NO_STORE_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/static/",
    "/health",
    "/login",
    "/auth/",
    "/org-logo/",
)

HSTS_VALUE = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, on_cloud_run: bool,
                 csp_report_only: str = CSP_REPORT_ONLY,
                 no_store_exempt_prefixes: tuple[str, ...] = NO_STORE_EXEMPT_PREFIXES) -> None:
        self.app = app
        self.on_cloud_run = on_cloud_run
        self.csp_report_only = csp_report_only
        self.no_store_exempt_prefixes = no_store_exempt_prefixes

    def _is_no_store_exempt(self, path: str) -> bool:
        return any(path == p.rstrip("/") or path.startswith(p) for p in self.no_store_exempt_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        exempt_from_no_store = self._is_no_store_exempt(path)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # setdefault everywhere: a route that deliberately set one of
                # these itself (none do today) keeps its own value.
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
                if self.csp_report_only:
                    headers.setdefault("Content-Security-Policy-Report-Only", self.csp_report_only)
                if self.on_cloud_run:
                    headers.setdefault("Strict-Transport-Security", HSTS_VALUE)
                content_type = headers.get("content-type", "")
                if content_type.startswith("text/html") and not exempt_from_no_store:
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)
