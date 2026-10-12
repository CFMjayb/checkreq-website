"""
statement_text.py -- Beacon Donor Management: the SAFE TEXT of a statement template. Pure: no database, no web.

A statement template's wording (the notice, the opening, the closing, the signer lines, the questions line) is turned into a PDF by a real
browser engine on our server. Free HTML could make that browser fetch an outside address or run code, so a template never holds free HTML:

  * clean_rich(text)       keeps only plain text plus a small allow-list of tags (p, br, b, strong, i, em, u, ul, ol, li). Every attribute is dropped,
                           so no link, image, style, class or event handler can exist. A script, style, iframe, object, embed, form, link, meta, svg
                           or template element is removed WITH its content. Anything else unknown is dropped and its text kept. All text is escaped.
                           Blank lines between paragraphs become paragraphs, single line breaks become <br>.
  * check_fields(text)     the only braces allowed are merge fields on the allow-list below, like {{donor_name}}; any other brace pair is refused.
  * fill(html, values)     replaces each merge field with the HTML-ESCAPED value, after cleaning, so a value (a donor's name) can never add markup.

The cleaner runs when a template is saved AND again when it is rendered, so a row written some other way is still safe.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

MERGE_FIELDS = (
    "parish_name", "parish_legal_name", "ein", "parish_address", "year", "period", "donor_name", "donor_address", "today",
    "signer_name", "signer_title", "contact_email", "deductible_total", "gift_count", "envelope_number", "recorded_as_of",
)
ALLOWED_TAGS = {"p", "br", "b", "strong", "i", "em", "u", "ul", "ol", "li"}
VOID_TAGS = {"br"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "form", "link", "meta", "svg", "template", "noscript", "head", "title", "math", "frame", "frameset", "applet", "base"}
MAX_TEXT = 4000

_FIELD_RE = re.compile(r"\{\{\s*([A-Za-z_]+)\s*\}\}")


class TextError(ValueError):
    """A plain-language reason the text cannot be saved."""


class _Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.skip = 0                       # depth inside a dropped element

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip:
            if tag in DROP_WITH_CONTENT:
                self.skip += 1
            return
        if tag in DROP_WITH_CONTENT:
            self.skip = 1
            return
        if tag not in ALLOWED_TAGS:
            return
        if tag in VOID_TAGS:
            self.out.append("<br>")
            return
        self.stack.append(tag)
        self.out.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self.skip:
            return
        if tag == "br":
            self.out.append("<br>")
        # any other self-closing tag is dropped

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip:
            if tag in DROP_WITH_CONTENT:
                self.skip -= 1
            return
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS and tag in self.stack:
            while self.stack:
                top = self.stack.pop()
                self.out.append(f"</{top}>")
                if top == tag:
                    break

    def handle_data(self, data):
        if self.skip:
            return
        self.out.append(html.escape(data, quote=True))

    def close_all(self):
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")


def check_fields(text: str) -> None:
    """Refuse any brace pair that is not an allowed merge field (and any stray template syntax)."""
    s = text or ""
    for m in _FIELD_RE.finditer(s):
        if m.group(1) not in MERGE_FIELDS:
            raise TextError("{{" + m.group(1) + "}} is not a field a statement can use. The fields are: " + ", ".join("{{" + f + "}}" for f in MERGE_FIELDS) + ".")
    stripped = _FIELD_RE.sub("", s)
    if "{{" in stripped or "}}" in stripped or "{%" in stripped or "%}" in stripped or "{#" in stripped:
        raise TextError("Braces are only for merge fields like {{donor_name}}. Take out any other { or } pair.")


def _clean_block(block: str) -> str:
    p = _Cleaner()
    p.feed(block)
    p.close()
    p.close_all()
    return "".join(p.out)


_BLOCK_TAGS = re.compile(r"</?(p|ul|ol|li)\b", re.I)


def clean_rich(text, *, max_len: int = MAX_TEXT) -> str:
    """The safe HTML for a piece of template wording. Empty text gives ''."""
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return ""
    if len(s) > max_len:
        raise TextError(f"That text is too long (more than {max_len} characters).")
    check_fields(s)
    if _BLOCK_TAGS.search(s):                       # the author used block tags themselves: keep their structure
        result = _clean_block(re.sub(r"\n+", " ", s))
    else:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", s) if p.strip()]
        out = []
        for p in paragraphs:
            inner = _clean_block(p.replace("\n", "<br>"))
            if inner:
                out.append(f"<p>{inner}</p>")
        result = "".join(out)
    check_fields(result)                            # braces that only appear once character references are decoded (&#123;) are refused too
    return result


def fill(clean_html: str, values: dict) -> str:
    """Put each merge field's value into already-cleaned HTML. The value is escaped; a missing value leaves nothing."""
    def repl(m):
        if m.group(1) not in MERGE_FIELDS:
            return m.group(0)                       # never evaluated, never filled
        v = values.get(m.group(1))
        return html.escape("" if v is None else str(v), quote=True)
    return _FIELD_RE.sub(repl, clean_html or "")


def editable(clean_html: str) -> str:
    """Cleaned wording back into what a person types in the edit box: paragraphs separated by a blank line, line breaks as new lines. Other allowed tags
    (bold, italic, underline, lists) stay as typed tags."""
    s = clean_html or ""
    s = re.sub(r"</p>\s*<p>", "\n\n", s)
    s = re.sub(r"</?p>", "", s)
    s = re.sub(r"<br\s*/?>", "\n", s)
    return html.unescape(s).strip()                 # the cleaner escaped &, <, > and quotes; the edit box shows them as typed


def plain(clean_html: str) -> str:
    """The text of cleaned HTML without its tags (for a short preview line)."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", clean_html or "")).strip()
