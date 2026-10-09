"""
sma_pdf.py -- 26-129 SMA letters (plan revision 12): checks the uploaded cover-letter PDF and joins it in front
of a parish's letter.

Why Beacon joins the PDFs itself: a Formstack Documents Data Route costs one merge PER DOCUMENT (tested
2026-10-08), so putting the cover letter in a route would triple the merges. The cover letter is the same text
for every parish, so staff upload it once as a PDF and Beacon puts it first.

Pure functions over bytes (pypdf only, no network, no database).
"""
from __future__ import annotations

import io

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError

import upload_guard

MAX_COVER_BYTES = 10 * 1024 * 1024
MAX_COVER_PAGES = 10
MAX_LETTER_PAGES = 12


class PdfError(ValueError):
    """The PDF cannot be used. The message is shown to the admin."""


def page_count(content: bytes) -> int:
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise PdfError("That PDF is password protected. Save an unprotected copy and upload that.")
        return len(reader.pages)
    except PdfError:
        raise
    except (PyPdfError, ValueError, OSError, KeyError, TypeError, AttributeError):
        raise PdfError("Beacon could not read that PDF. Save it again from Word or Acrobat and try again.")


def check_cover_letter(content: bytes) -> int:
    """A cover letter must be a real PDF (by its bytes, never its name), unprotected, 1 to 10 pages and at most
    10 MB. Returns the page count."""
    if not content or len(content) > MAX_COVER_BYTES:
        raise PdfError(f"The cover letter must be a PDF of at most {MAX_COVER_BYTES // (1024 * 1024)} MB.")
    if upload_guard.sniff(content) != "application/pdf":
        raise PdfError("The cover letter must be a PDF file (the file's contents are not a PDF).")
    pages = page_count(content)
    if not 1 <= pages <= MAX_COVER_PAGES:
        raise PdfError(f"The cover letter must be between 1 and {MAX_COVER_PAGES} pages (this one has {pages}).")
    return pages


def check_letter(content: bytes) -> int:
    """The PDF Formstack returns for a parish's letter: a PDF with a sensible number of pages."""
    if upload_guard.sniff(content) != "application/pdf":
        raise PdfError("Formstack did not return a PDF.")
    pages = page_count(content)
    if not 1 <= pages <= MAX_LETTER_PAGES:
        raise PdfError(f"The letter has {pages} pages, which is not expected.")
    return pages


def can_join(cover: bytes) -> None:
    """Prove now, at upload, that this cover letter can be put in front of a letter (some PDFs read fine but refuse to
    be merged), so the failure does not show up later as a spent Formstack merge per parish."""
    blank = PdfWriter()
    blank.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    blank.write(buf)
    try:
        join(cover, buf.getvalue())
    except Exception:
        raise PdfError("Beacon could not combine that PDF with a letter. Save it again from Word or Acrobat (print to PDF) and try again.")


def join(cover: bytes | None, letter: bytes, title: str = "") -> bytes:
    """The cover letter (when there is one) followed by the letter, as one PDF."""
    writer = PdfWriter()
    for part in ([cover] if cover else []) + [letter]:
        writer.append(PdfReader(io.BytesIO(part)))
    if title:
        writer.add_metadata({"/Title": title[:200]})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
