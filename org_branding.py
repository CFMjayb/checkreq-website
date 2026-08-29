"""
org_branding.py -- per-diocese banner-color + logo customization (2026-08-29).

Jay: each diocese needs to set the color of its own Diocesan Mode and
Parish Mode banners, plus a diocese-supplied logo shown next to the
"Beacon" wordmark. The Cornerstone Mode banner color is shared app-wide,
not per-diocese, so it lives in the existing generic checkreq.app_settings
key/value store (the same table Test Mode already uses) rather than a new
column -- there's only ever one value, no per-org dimension needed.

Diocesan/Parish colors are plain nullable columns on checkreq.organizations
(migration 051): NULL means "hasn't set one, use the app's own default" --
the pre-existing --color-franciscan-green / --color-parish-red tokens in
tokens.css. Only the base hex is stored per mode; the darker hover/accent
shade every existing token pair already has (e.g. franciscan-green
#1f3d2e -> -dark #142822) is derived here at render time via a flat darken,
rather than asking an admin to pick two colors for one concept.

Logos are stored in the same GCS bucket main.py's attachment pipeline
already uses (cfm-checkreq-attachments), under an org-logos/ prefix -- no
new bucket, no new IAM grant.
"""
from __future__ import annotations

import re

import app_settings

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

CORNERSTONE_COLOR_SETTING_KEY = "cornerstone_mode_color"
DEFAULT_CORNERSTONE_COLOR = "#14294d"  # tokens.css's own --color-cornerstone-blue
DEFAULT_DIOCESAN_COLOR = "#1f3d2e"     # tokens.css's own --color-franciscan-green
DEFAULT_PARISH_COLOR = "#7a1f2b"       # tokens.css's own --color-parish-red

# Same bucket main.py's attachment pipeline already uses -- logos live
# under their own prefix in it, so no new bucket or IAM grant was needed.
LOGO_BUCKET = "cfm-checkreq-attachments"
LOGO_GCS_PREFIX = "org-logos"
MAX_LOGO_BYTES = 2 * 1024 * 1024  # 2MB -- a header logo has no business being bigger
ALLOWED_LOGO_CONTENT_TYPES = {"image/png", "image/jpeg", "image/svg+xml", "image/webp"}


def is_valid_hex(value: str | None) -> bool:
    return bool(value and HEX_RE.match(value))


def darken(hex_color: str, factor: float = 0.35) -> str:
    """A flat, consistent darken -- matches the rough ratio between every
    existing token pair closely enough to look intentional without needing
    a second color picker per mode."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    r, g, b = (max(0, round(c * (1 - factor))) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def get_cornerstone_color() -> str:
    return app_settings.get_setting(CORNERSTONE_COLOR_SETTING_KEY, DEFAULT_CORNERSTONE_COLOR)


def set_cornerstone_color(hex_color: str, updated_by_user_id: int | None) -> None:
    app_settings.set_setting(CORNERSTONE_COLOR_SETTING_KEY, hex_color, updated_by_user_id)


def theme_style_block(*, mode: str, org: dict | None) -> str:
    """A <style> block (or '' when there's nothing to override) that
    replaces --color-franciscan-green / -dark for the CURRENT page's mode
    -- matching the exact selector base.css's own static default rule
    already uses for that mode (:root for Diocesan Mode, body.parish-mode /
    body.cornerstone-mode otherwise), so this carries the SAME specificity
    and only needs to be placed after base.css's own <link> in <head> to
    win on source order -- no !important needed. Returns '' (letting the
    existing static default apply unchanged) whenever there's no custom
    color set for this org/mode, which is exactly the NULL-means-default
    behavior the schema is built around.

    mode: "diocesan" | "parish" | "cornerstone".
    org: for "diocesan"/"parish", the relevant checkreq.organizations row
    (the current org, or a Parish-Mode parish's own diocese org). None, or
    a row missing the column (e.g. pre-migration), is treated as "not set"
    -- this must never raise on an old row shape.
    """
    if mode == "cornerstone":
        color, selector = get_cornerstone_color(), "body.cornerstone-mode"
    elif mode == "parish":
        color, selector = (org or {}).get("parish_mode_color"), "body.parish-mode"
    elif mode == "diocesan":
        color, selector = (org or {}).get("diocesan_mode_color"), ":root"
    else:
        return ""
    if not is_valid_hex(color):
        return ""
    dark = darken(color)
    return (
        f"<style>{selector}{{--color-franciscan-green:{color};"
        f"--color-franciscan-green-dark:{dark};}}</style>"
    )


def logo_path(org_id: int, content_type: str) -> str:
    ext = {
        "image/png": "png", "image/jpeg": "jpg",
        "image/svg+xml": "svg", "image/webp": "webp",
    }.get(content_type, "bin")
    return f"{LOGO_GCS_PREFIX}/{org_id}/logo.{ext}"
