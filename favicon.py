"""
favicon.py -- per-hostname browser-tab icon (2026-10-01, Jay: "put a favicon
depending on how you got to beacon -- if you used beacon.episcopalmaryland.org
you would have the Episcopal Maryland shield, and so forth").

GET /favicon.ico answers according to the HOSTNAME the visitor typed, not who
they are or which entity they have selected:

  * a branded hostname (checkreq.sso_auto_hostnames -- beacon.episcopalmaryland.org,
    beacon.edom.org, beacon.claggettcenter.org, beacon.episcopalmaine.org, ...)
    -> that diocese's own uploaded logo, the same one the login page and the
    header already show (checkreq.organizations.logo_gcs_path);
  * anything else (beacon.cfmins.org, beacondev.cfmins.org, localhost) or a
    branded hostname whose diocese has no usable logo -> Beacon's own default
    icon, static/img/beacon-favicon.png.

Host -> org resolution is NOT re-implemented here. It is injected from
auth_routes._branding_for_host (the login page's resolver), so the tab icon
and the login screen can never disagree about which diocese a hostname is.

Hand-made override: if static/img/favicon-overrides/{org_id}.png exists it wins
over the org's logo. Use it when a logo is a wide wordmark that turns to mush at
16px. Delete the file and the org's real logo is used again. (Claggett, org 2,
has one as of 2026-10-01 -- its sun emblem -- until its new logo arrives.)

Unauthenticated on purpose: browsers fetch the icon before sign-in and often
without cookies, and the login page is exactly where a branded hostname matters
most. It exposes nothing a logged-out visitor can't already see -- /org-logo/{id}
is unauthenticated too (see main.py).

A favicon must never break a page, so every failure path (database down, GCS
hiccup, a legacy SVG logo, a corrupt or enormous image) quietly serves the
default icon instead of an error.

Logos are not square (EDOM's shield is 0.63:1), and a browser stretches or crops
a non-square favicon badly, so each logo is trimmed of transparent margin, scaled
to fit, and centred on a transparent square canvas. Results are cached in memory
per org for an hour; a replaced logo therefore shows up in tabs within ~an hour
(browsers cache favicons independently and aggressively, so a hard refresh may
be needed to see it sooner).
"""
from __future__ import annotations

import io
import os
import threading
import time
from functools import lru_cache

from fastapi import FastAPI, Request
from fastapi.responses import Response
from PIL import Image

import db
import gcs_client
import org_branding

ICON_PX = 64  # one size; browsers downscale to 16/32 themselves
_IMG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "img")
_DEFAULT_PATH = os.path.join(_IMG_DIR, "beacon-favicon.png")
_OVERRIDE_DIR = os.path.join(_IMG_DIR, "favicon-overrides")

_ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP"}  # org_branding.ALLOWED_LOGO_CONTENT_TYPES
_MAX_PIXELS = 25_000_000  # decompression-bomb guard; a 2MB PNG can still claim a huge canvas

_HIT_TTL = 3600   # seconds a rendered logo is reused
_MISS_TTL = 300   # seconds a failed lookup is remembered, so a GCS blip heals quickly
_cache: dict[int, tuple[float, bytes | None]] = {}
_cache_lock = threading.Lock()


@lru_cache(maxsize=1)
def _default_icon() -> bytes:
    with open(_DEFAULT_PATH, "rb") as fh:
        return fh.read()


def fit_square(data: bytes, size: int = ICON_PX) -> bytes | None:
    """PNG bytes of `data` trimmed, fitted and centred on a transparent size x size
    canvas, or None if it isn't a safe raster image we can use."""
    try:
        im = Image.open(io.BytesIO(data))
        if im.format not in _ALLOWED_FORMATS:
            return None
        if im.size[0] * im.size[1] > _MAX_PIXELS:
            return None
        im = im.convert("RGBA")
        bbox = im.getchannel("A").getbbox()  # drop transparent margin; None = fully transparent
        if bbox is None:
            return None
        im = im.crop(bbox)
        w, h = im.size
        scale = min(size / w, size / h)
        im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2), im)
        out = io.BytesIO()
        canvas.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:
        return None


def _override_icon(org_id: int) -> bytes | None:
    path = os.path.join(_OVERRIDE_DIR, f"{int(org_id)}.png")
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return fit_square(fh.read())


def _org_icon(org_id: int) -> bytes | None:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(org_id)
        if hit and hit[0] > now:
            return hit[1]
    icon: bytes | None = None
    try:
        icon = _override_icon(org_id)
        if icon is None:
            row = db.query_one("SELECT logo_gcs_path FROM checkreq.organizations WHERE id = %s", (org_id,))
            if row and row.get("logo_gcs_path"):
                blob = gcs_client.download_bytes(org_branding.LOGO_BUCKET, row["logo_gcs_path"])
                if blob:
                    icon = fit_square(blob[0])
    except Exception as exc:  # never let a favicon take a page down
        print(f"[favicon] org {org_id} lookup failed, serving default: {type(exc).__name__}")
    with _cache_lock:
        _cache[org_id] = (now + (_HIT_TTL if icon else _MISS_TTL), icon)
    return icon


def register(app: FastAPI, *, branding_for_request) -> None:
    """branding_for_request(request) -> {"org_id", "has_logo", ...} | None -- the
    login page's host->diocese resolver (auth_routes._branding_for_host), injected
    like every other module here so this file never imports auth_routes."""

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon(request: Request):
        icon = None
        try:
            branding = branding_for_request(request)
            if branding:  # mapped hostname; _org_icon returns None if no override/logo
                icon = _org_icon(branding["org_id"])
        except Exception as exc:
            print(f"[favicon] host lookup failed, serving default: {type(exc).__name__}")
        return Response(
            content=icon or _default_icon(),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )
