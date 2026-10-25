"""
logic/overview_docs/render.py -- turn one stored document into the page that goes inside the reader's sandboxed iframe (26-163).
No FastAPI, no database: strings in, string out.

    rewrite_links(html)        overview:<slug> -> /cfm/overview/<slug> (opens in the whole window), http(s) -> a new tab,
                               #anchor left alone, Beacon page paths (/hr) -> the whole window
    build_srcdoc(doc, theme)   the complete iframe page for one document

rewrite_links expects the CANONICAL output of sanitize.sanitize (double-quoted attributes, one href per link), which is the only
thing build_srcdoc ever gives it.
"""
from __future__ import annotations

import html as _html
import re
from functools import lru_cache
from pathlib import Path

from .sanitize import DOCUMENT, FRAGMENT, sanitize_parts

THEMES = ("light", "dark")

# What the page inside the iframe may do: show text and styles, and show images and fonts that are inline in the document.
# Nothing is fetched from anywhere, nothing runs, no form can post. (The iframe sandbox has no allow-scripts either.)
CSP = ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; "
       "base-uri 'none'; form-action 'none'")

_BASE_CSS_PATH = Path(__file__).resolve().parents[2] / "static" / "css" / "overview_doc_base.css"
_A_TAG = re.compile(r"<a\b([^>]*)>")
_HREF = re.compile(r'\bhref="([^"]*)"')


@lru_cache(maxsize=1)
def _base_css() -> str:
    return _BASE_CSS_PATH.read_text(encoding="utf-8").replace("</", "<\\/")


def _rewrite_one(m: re.Match) -> str:
    attrs = m.group(1)
    h = _HREF.search(attrs)
    if not h:
        return m.group(0)
    href = _html.unescape(h.group(1))
    extra = ""
    if href.startswith("overview:"):
        slug, _, frag = href[len("overview:"):].partition("#")
        new = f"/cfm/overview/{slug}" + (f"#{frag}" if frag else "")
        attrs = attrs.replace(h.group(0), f'href="{_html.escape(new, quote=True)}"', 1)
        extra = ' target="_top"'
    elif re.match(r"https?://", href, re.I):
        extra = ' target="_blank" rel="noopener noreferrer"'
    elif href.startswith("/"):
        extra = ' target="_top"'
    return f"<a{attrs}{extra}>"


def rewrite_links(html: str) -> str:
    return _A_TAG.sub(_rewrite_one, html or "")


def build_srcdoc(doc: dict, theme: str = "light") -> str:
    """The whole page for the iframe. doc needs body_html, body_format (and a title)."""
    theme = theme if theme in THEMES else "light"
    fmt = DOCUMENT if doc.get("body_format") == DOCUMENT else FRAGMENT
    styles, body = sanitize_parts(doc.get("body_html") or "", fmt)
    body = rewrite_links(body)
    css = _base_css() if fmt == FRAGMENT else "\n".join(styles)
    title = _html.escape(str(doc.get("title") or "Beacon Overview"), quote=False)
    return (
        f'<!doctype html><html lang="en" data-theme="{theme}"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{_html.escape(CSP, quote=True)}">'
        f"<title>{title}</title><style>{css}</style></head><body>{body}</body></html>"
    )
