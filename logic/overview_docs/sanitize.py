"""
logic/overview_docs/sanitize.py -- the allowlist sanitizer for Beacon Overview documents (26-163). Standard library only.

A document is HTML written by us (Cowork, Claude, a manual build), stored in checkreq.overview_docs and shown inside a sandboxed iframe
with no script rights. This module is the first of three layers that keep it from ever doing more than display text:
  1. THIS: parse the stored HTML and write it out again, keeping only allowlisted tags and attributes. Nothing is "cleaned in place":
     the output is built only from pieces that passed, every attribute value is escaped, and every open tag is closed.
  2. The iframe sandbox (no allow-scripts) and a Content-Security-Policy meta tag in the page, added by render.build_srcdoc().
  3. Sanitizing again on EVERY read (never trusting what was stored).

Two formats (checkreq.overview_docs.body_format):
  fragment  the house format (docs\\DOC-CONTRACT.md). <style> blocks and style="" are dropped: Beacon supplies the stylesheet.
  document  a self-contained page with its own styles (the Beacon - Overview manual). <style> is kept, but every @import is removed
            (the whole statement, including a Google Fonts URL with semicolons in it) and every url(...) that is not a data: image or
            font is emptied. Fonts therefore fall back to the stack's system fonts.

Always removed, with their content: script, iframe, object, embed, noscript, template, svg, math, applet, frame/frameset and form
controls. form, link, meta, base and button tags are removed (their text stays). on* attributes, javascript: and other unknown link
schemes, and any <img> whose src is not an inline data: image never survive, because nothing outside the allowlist is ever copied.
"""
from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser

FRAGMENT = "fragment"
DOCUMENT = "document"

_VOID = {"br", "hr", "img", "col", "wbr"}
_ALLOWED = {
    "a", "abbr", "address", "article", "aside", "b", "blockquote", "br", "caption", "cite", "code", "col", "colgroup", "dd", "del",
    "details", "dfn", "div", "dl", "dt", "em", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
    "i", "img", "ins", "kbd", "li", "main", "mark", "nav", "ol", "p", "pre", "q", "s", "samp", "section", "small", "span", "strong",
    "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr", "u", "ul", "var", "wbr",
}
# These and everything inside them are dropped.
_DROP_WITH_CONTENT = {
    "script", "iframe", "object", "embed", "noscript", "template", "svg", "math", "applet", "frame", "frameset", "textarea", "select",
    "option", "optgroup", "datalist", "canvas", "audio", "video", "title", "xmp", "plaintext", "noembed", "noframes",
}

_GLOBAL_ATTRS = {"class", "id", "title", "lang", "dir"}
_TAG_ATTRS = {
    "a": {"href"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan", "scope"},
    "th": {"colspan", "rowspan", "scope"},
    "col": {"span"},
    "colgroup": {"span"},
    "ol": {"start", "type", "reversed"},
    "li": {"value"},
    "time": {"datetime"},
    "details": {"open"},
}
_DIGITS = re.compile(r"[0-9]{1,6}")
_PCT_OR_DIGITS = re.compile(r"[0-9]{1,4}%?")
_SAFE_WORDS = re.compile(r"[A-Za-z0-9_\- .:]{0,120}")
_SCOPE = {"row", "col", "rowgroup", "colgroup"}
_DIR = {"ltr", "rtl", "auto"}
_OL_TYPE = {"1", "a", "A", "i", "I"}

_ANCHOR = re.compile(r"#[A-Za-z0-9_\-:.]{0,100}")
_OVERVIEW = re.compile(r"overview:[a-z0-9]+(?:-[a-z0-9]+)*(?:#[A-Za-z0-9_\-:.]{1,100})?")
_HTTP = re.compile(r"https?://[^\s<>\"'`\\\x00-\x1f\x7f]+", re.I)
_MAILTO = re.compile(r"mailto:[^\s<>\"'`\\\x00-\x1f\x7f]+", re.I)
_SITE_PATH = re.compile(r"/(?!/)[^\s<>\"'`\\\x00-\x1f\x7f]*")
_DATA_IMG = re.compile(r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]{1,2000000}")

_ENTITY = re.compile(r"[A-Za-z][A-Za-z0-9]{1,31}")
_CHARREF = re.compile(r"(?:[xX][0-9A-Fa-f]{1,6}|[0-9]{1,7})")


# ---------------------------------------------------------------- CSS (document format only)

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CSS_URL = re.compile(r"""url\(\s*(?:'([^']*)'|"([^"]*)"|([^)'"\s][^)]*?))\s*\)""", re.I)
_CSS_BAD = re.compile(r"expression\s*\(|-moz-binding|behavior\s*:|javascript\s*:|vbscript\s*:", re.I)
_CSS_DATA_OK = re.compile(r"data:(?:image/|font/|application/(?:x-)?font)", re.I)


def _drop_at_imports(css: str) -> str:
    """Remove every @import statement whole, honoring quotes and parentheses (a Google Fonts URL has semicolons inside it)."""
    out: list[str] = []
    low = css.lower()
    i, n = 0, len(css)
    while i < n:
        j = low.find("@import", i)
        if j < 0:
            out.append(css[i:])
            break
        out.append(css[i:j])
        k, quote, depth = j + 7, None, 0
        while k < n:
            c = css[k]
            if quote:
                if c == "\\":
                    k += 1
                elif c == quote:
                    quote = None
            elif c in "'\"":
                quote = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth = max(0, depth - 1)
            elif c == ";" and depth == 0:
                k += 1
                break
            elif c == "}" and depth == 0:
                break
            k += 1
        i = k
    return "".join(out)


def _url_repl(m: re.Match) -> str:
    single, double, bare = m.group(1), m.group(2), m.group(3)
    arg = single if single is not None else double if double is not None else bare
    if arg is not None and _CSS_DATA_OK.match(arg.strip()):
        if single is not None:
            return f"url('{single}')"
        if double is not None:
            return f'url("{double}")'
        return f"url({bare})"
    return "url()"


def scrub_css(css: str) -> str:
    """Make a style sheet (or a style="" value) inert: no @import, no url() except data: images/fonts, no script hooks."""
    css = _CSS_COMMENT.sub("", css or "")
    css = _drop_at_imports(css)
    css = _CSS_URL.sub(_url_repl, css)
    css = _CSS_BAD.sub("blocked", css)
    return css.replace("</", "<\\/")


# ---------------------------------------------------------------- attributes

def _clean_href(raw: str) -> str | None:
    v = raw.strip()
    if _ANCHOR.fullmatch(v) or _OVERVIEW.fullmatch(v) or _HTTP.fullmatch(v) or _MAILTO.fullmatch(v) or _SITE_PATH.fullmatch(v):
        return v
    return None


def _clean_attr(tag: str, name: str, value: str | None, mode: str) -> str | None:
    """The cleaned value for one attribute, or None to drop it."""
    value = "" if value is None else value
    if name == "style":
        return scrub_css(value) if mode == DOCUMENT else None
    if name not in _GLOBAL_ATTRS and name not in _TAG_ATTRS.get(tag, ()):
        return None
    if name in ("class", "title", "alt"):
        return value[:300]
    if name == "id":
        return value if _SAFE_WORDS.fullmatch(value) and value else None
    if name == "lang":
        return value if re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", value) else None
    if name == "dir":
        return value if value in _DIR else None
    if name == "href":
        return _clean_href(value)
    if name == "src":
        v = value.strip()
        return v if _DATA_IMG.fullmatch(v) else None
    if name in ("width", "height"):
        return value if _PCT_OR_DIGITS.fullmatch(value) else None
    if name in ("colspan", "rowspan", "span", "start", "value"):
        return value if _DIGITS.fullmatch(value) else None
    if name == "scope":
        return value if value in _SCOPE else None
    if name == "type":
        return value if value in _OL_TYPE else None
    if name == "datetime":
        return value if re.fullmatch(r"[0-9T:\-+.Z ]{1,40}", value) else None
    if name in ("reversed", "open"):
        return ""
    return None


# ---------------------------------------------------------------- the parser

class _Scrubber(HTMLParser):
    def __init__(self, mode: str):
        super().__init__(convert_charrefs=False)
        self.mode = mode
        self.out: list[str] = []
        self.styles: list[str] = []
        self.stack: list[str] = []
        self._drop: str | None = None
        self._drop_depth = 0
        self._in_style = False
        self._style_buf: list[str] = []

    # -- tags
    def handle_starttag(self, tag, attrs):
        self._open(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs, True)

    def _open(self, tag: str, attrs, selfclosing: bool) -> None:
        tag = tag.lower()
        if self._drop:
            if tag == self._drop and not selfclosing:
                self._drop_depth += 1
            return
        if self._in_style:
            return
        if tag in _DROP_WITH_CONTENT or (tag == "style" and self.mode != DOCUMENT):
            if not selfclosing:
                self._drop, self._drop_depth = tag, 1
            return
        if tag == "style":
            if not selfclosing:
                self._in_style, self._style_buf = True, []
            return
        if tag not in _ALLOWED:
            return                                   # unwrap: the tag goes, its children stay
        parts = []
        for name, value in attrs:
            cleaned = _clean_attr(tag, (name or "").lower(), value, self.mode)
            if cleaned is None:
                continue
            parts.append(f' {name.lower()}' if cleaned == "" and name.lower() in ("reversed", "open")
                         else f' {name.lower()}="{_html.escape(cleaned, quote=True)}"')
        if tag == "img" and not any(p.startswith(" src=") for p in parts):
            return                                   # an image with no inline src is just gone
        self.out.append(f"<{tag}{''.join(parts)}>")
        if tag in _VOID:
            return
        if selfclosing:
            self.out.append(f"</{tag}>")
        else:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._drop:
            if tag == self._drop:
                self._drop_depth -= 1
                if self._drop_depth <= 0:
                    self._drop = None
            return
        if self._in_style:
            if tag == "style":
                self._finish_style()
            return
        if tag in _VOID or tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def _finish_style(self) -> None:
        css = scrub_css("".join(self._style_buf)).strip()
        if css:
            self.styles.append(css)
        self._in_style, self._style_buf = False, []

    # -- text
    def handle_data(self, data):
        if self._drop:
            return
        if self._in_style:
            self._style_buf.append(data)
            return
        self.out.append(_html.escape(data, quote=False))

    def handle_entityref(self, name):
        if self._drop or self._in_style:
            return
        self.out.append(f"&{name};" if _ENTITY.fullmatch(name) else _html.escape(f"&{name};", quote=False))

    def handle_charref(self, name):
        if self._drop or self._in_style:
            return
        if _CHARREF.fullmatch(name):
            try:
                code = int(name[1:], 16) if name[0] in "xX" else int(name)
            except ValueError:
                return
            if 0x20 <= code <= 0x10FFFF and not (0xD800 <= code <= 0xDFFF) or code in (0x09, 0x0A, 0x0D):
                self.out.append(f"&#{name};")
        # anything else: dropped

    # comments, declarations, processing instructions, CDATA: not copied
    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass

    # -- finish
    def result(self) -> tuple[list[str], str]:
        self.close()
        if self._in_style:
            self._finish_style()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return self.styles, "".join(self.out)


def sanitize_parts(html: str, body_format: str) -> tuple[list[str], str]:
    """(style sheets, body html). The style list is empty for a fragment."""
    mode = DOCUMENT if body_format == DOCUMENT else FRAGMENT
    try:
        p = _Scrubber(mode)
        p.feed(html or "")
        return p.result()
    except Exception:                                # fail closed: show the text, escaped, never the markup
        return [], _html.escape(html or "", quote=False)


def sanitize(html: str, body_format: str = FRAGMENT) -> str:
    """The safe version of one document body. For a 'document' the kept style sheets come first, as <style> blocks."""
    styles, body = sanitize_parts(html, body_format)
    return "".join(f"<style>{s}</style>" for s in styles) + body
