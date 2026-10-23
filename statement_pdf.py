"""
statement_pdf.py -- Beacon Donor Management: a giving statement as a PDF. The statement is the ONE shared layout (templates/statement_sheet.html) rendered to HTML,
wrapped with its own stylesheet, and turned into a PDF by main.py's headless-Chromium renderer (_html_to_pdf_bytes, one browser at a time, so a request that finds
it busy gets a plain "try again in a minute", never a hang). Imports from main.py are done inside the functions, as parish_finance.py does, because main.py imports
the modules that import this one.

  logo_data_uri(parish_id)      the parish's logo as a data: URI (a PDF built with set_content() has no base address, so a normal image link would not load),
                                only for png, jpeg, gif or webp, checked by the file's own first bytes, and not larger than 1.5 MB; None otherwise (never an error)
  sheet_html(sheet_ctx)         the statement as an HTML fragment
  document_html(fragment)       the fragment inside a full page with the statement stylesheet inlined (no outside fonts or addresses are loaded)
  render_pdf(sheet_ctx)         bytes of the PDF
"""
from __future__ import annotations

import base64
import os

MAX_LOGO_BYTES = 1_500_000
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))


def _sniff(data: bytes) -> str | None:
    for magic, media in _MAGIC:
        if data.startswith(magic):
            return media
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def logo_data_uri(parish_id: int) -> str | None:
    try:
        import db
        import gcs_client
        import org_branding
        row = db.query_one("SELECT logo_gcs_path FROM portal.parishes WHERE id = %s", (parish_id,))
        if not row or not row.get("logo_gcs_path"):
            return None
        got = gcs_client.download_bytes(org_branding.LOGO_BUCKET, row["logo_gcs_path"])
        if not got:
            return None
        data = got[0]
        media = _sniff(data) if data and len(data) <= MAX_LOGO_BYTES else None
        return f"data:{media};base64,{base64.b64encode(data).decode('ascii')}" if media else None
    except Exception:
        return None


def sheet_html(sheet_ctx: dict) -> str:
    from main import templates
    import donor_web as W
    return templates.env.get_template("statement_sheet.html").render(d=W.fmt_date, money=W.fmt_money, **sheet_ctx)


def _css() -> str:
    path = os.path.join(os.path.dirname(__file__), "static", "css", "donor_statements.css")
    with open(path, encoding="utf-8") as f:
        return f.read()


def document_html(fragment: str) -> str:
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><style>"
            ":root { --color-border: #ddd4c0; --color-franciscan-green: #1f3d2e; }"
            "body { margin: 0; background: #fff; }"
            + _css() +
            "@media print { .stm-sheet { margin: 0; max-width: none; padding: .35in .4in; } }"
            "</style></head><body>" + fragment + "</body></html>")


def render_pdf(sheet_ctx: dict) -> bytes:
    from main import _html_to_pdf_bytes
    return _html_to_pdf_bytes(document_html(sheet_html(sheet_ctx)))


def render_many(documents: list[str]) -> list[bytes]:
    """PDFs for many full HTML pages with ONE browser start (a bulk run of 141 statements must not start 141 browsers; each is about 550 MB on a 1 GiB instance).
    Holds main.py's one-at-a-time render lock for the whole batch, so a member's single statement waits its turn and a second bulk chunk cannot run beside it.
    Raises RuntimeError when the renderer stays busy for too long, and re-raises whatever the browser raised."""
    import threading
    from main import _PDF_RENDER_LOCK, _PDF_RENDER_WAIT_SECONDS
    result: dict = {"pdfs": []}

    def _run():
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    for html in documents:
                        page = browser.new_page()
                        try:
                            page.set_content(html, wait_until="load", timeout=20000)
                            result["pdfs"].append(page.pdf(format="Letter", print_background=True, margin={"top": "0.4in", "bottom": "0.4in", "left": "0.4in", "right": "0.4in"}))
                        finally:
                            page.close()
                finally:
                    browser.close()
        except Exception as exc:
            result["error"] = exc

    if not documents:
        return []
    t = threading.Thread(target=_run)
    if not _PDF_RENDER_LOCK.acquire(timeout=_PDF_RENDER_WAIT_SECONDS):
        raise RuntimeError("The PDF renderer is busy right now. Please try again in a minute.")
    try:
        t.start()
        t.join()
    finally:
        _PDF_RENDER_LOCK.release()
    if "error" in result:
        raise result["error"]
    return result["pdfs"]
