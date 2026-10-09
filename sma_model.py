"""
sma_model.py -- 26-129 SMA letters (plan revision 12): reads the SMA Task Force model workbook
(EDOM_<year>_Shared_Ministry_Allocation_Model.xlsx) that an admin uploads.

What it reads (by HEADER NAME, never by column position, so a re-ordered or extended model still works):
  * Assumptions, the "METHOD A" block: the NOI rate, the operating-expense credit rate, the flat deduction,
    the operating expense basis, the lesser-of switch and the minimum allocation (these become the run's
    rates, and the page shows them for the admin to confirm).
  * Parish Data: one row per congregation: Parish UEID, name, city, NOI for the three years, operating
    expense, and the model's own data-quality labels.
  * Method A - Historic: the model's OWN results for each parish (Test 1, Test 2, the allocation, last year's
    allocation) so Beacon can prove its arithmetic ties out for every parish.

Refuses, with a plain message, anything it cannot be sure of (missing sheet or column, an operating expense
basis other than "Latest", no calculated values because the file was saved without Excel) rather than quietly
producing a letter from a different rule.

Safety: the file comes from an upload, so it is size-checked, must really be a zip, has its uncompressed size
and entry count capped (zip bombs), is read read-only with formulas replaced by their cached values, and is
never executed or served back. Pure parsing: no database, no network.
"""
from __future__ import annotations

import io
import re
import zipfile
from decimal import Decimal

import openpyxl

from sma_calc import D, Rates

MAX_BYTES = 15 * 1024 * 1024
MAX_UNCOMPRESSED = 60 * 1024 * 1024
MAX_ENTRIES = 500
MAX_ROWS = 400

SHEET_ASSUMPTIONS = "Assumptions"
SHEET_PARISH = "Parish Data"
SHEET_METHOD_A = "Method A - Historic"


class ModelError(ValueError):
    """The uploaded workbook is not a model Beacon can build letters from. The message is shown to the admin."""


def _check_container(content: bytes) -> None:
    if not content or len(content) > MAX_BYTES:
        raise ModelError(f"The model file must be an Excel workbook of at most {MAX_BYTES // (1024 * 1024)} MB.")
    if not content.startswith(b"PK\x03\x04"):
        raise ModelError("That file is not an Excel workbook (.xlsx).")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            infos = z.infolist()
            if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
                raise ModelError("That workbook is larger than expected once opened. Use the Task Force model as saved by Excel.")
            if "xl/workbook.xml" not in {i.filename for i in infos}:
                raise ModelError("That file is not an Excel workbook (.xlsx).")
    except zipfile.BadZipFile:
        raise ModelError("That file is not an Excel workbook (.xlsx).")


def _header(rows: list[tuple], first: str, second: str, sheet: str) -> tuple[int, dict[str, int]]:
    for i, r in enumerate(rows[:15]):
        if r and len(r) > 1 and str(r[0] or "").strip() == first and str(r[1] or "").strip() == second:
            names = {}
            for j, h in enumerate(r):
                if h is not None and str(h).strip():
                    names.setdefault(str(h).strip(), j)
            return i, names
    raise ModelError(f'The "{sheet}" sheet does not have the expected header row ("{first}", "{second}", ...).')


def _need(cols: dict[str, int], name: str, sheet: str) -> int:
    if name not in cols:
        raise ModelError(f'The "{sheet}" sheet has no "{name}" column.')
    return cols[name]


def _find(cols: dict[str, int], pattern: str, label: str, sheet: str) -> int:
    for name, j in cols.items():
        if re.fullmatch(pattern, name):
            return j
    raise ModelError(f'The "{sheet}" sheet has no "{label}" column.')


def _blank(row: tuple, idx: int) -> bool:
    v = row[idx] if idx < len(row) else None
    return v is None or str(v).strip() == ""


def _num(row: tuple, idx: int, parish: str, what: str, *, required: bool = True):
    v = row[idx] if idx < len(row) else None
    if v is None or v == "":
        return Decimal(0) if required else None
    try:
        return D(v)
    except ValueError:
        raise ModelError(f'{parish}: "{what}" is not a number ({v!r}).')


def _assumptions(rows: list[tuple]) -> dict[str, object]:
    """The labelled values of the METHOD A block (and the global rounding line), label -> value."""
    out: dict[str, object] = {}
    in_block = False
    for r in rows:
        label = str(r[1]).strip() if len(r) > 1 and r[1] is not None else ""
        if re.match(r"^2\.\s+METHOD A", label.upper()):
            in_block = True
            continue
        if in_block:
            if not label:
                break
            if re.match(r"^\d\.\s+", label):
                break
            out[label] = r[2] if len(r) > 2 else None
    for r in rows:
        label = str(r[1]).strip() if len(r) > 1 and r[1] is not None else ""
        if label.lower().startswith("round allocations to the nearest"):
            out[label] = r[2] if len(r) > 2 else None
    return out


def _flag(value) -> bool:
    return str(value or "").strip().upper() in ("Y", "YES", "TRUE", "1")


def read_model(content: bytes) -> dict:
    """-> {"years": {...}, "rates": Rates, "assumptions": {...}, "rows": [...], "warnings": [...]}.
    Raises ModelError for anything the admin has to fix."""
    _check_container(content)
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:
        raise ModelError("Beacon could not open that workbook. Save it from Excel and try again.")
    try:
        for name in (SHEET_ASSUMPTIONS, SHEET_PARISH, SHEET_METHOD_A):
            if name not in wb.sheetnames:
                raise ModelError(f'The workbook has no "{name}" sheet. Upload the Task Force model itself.')
        assump_rows = [tuple(r) for r in wb[SHEET_ASSUMPTIONS].iter_rows(max_row=200, values_only=True)]
        parish_rows = [tuple(r) for r in wb[SHEET_PARISH].iter_rows(max_row=MAX_ROWS + 20, values_only=True)]
        method_rows = [tuple(r) for r in wb[SHEET_METHOD_A].iter_rows(max_row=MAX_ROWS + 20, values_only=True)]
    finally:
        wb.close()

    # --- the rates and switches -------------------------------------------------------------------
    a = _assumptions(assump_rows)

    def get(label_start: str):
        for k, v in a.items():
            if k.lower().startswith(label_start.lower()):
                return v
        raise ModelError(f'The Assumptions sheet has no "{label_start}..." line in its Method A block.')

    noi_rate, noe_rate = D(get("Rate applied to Normal Operating Income")), D(get("Credit rate applied to operating expense"))
    flat = D(get("Flat dollar deduction"))
    basis = str(get("Operating expense basis") or "").strip()
    if basis.lower() != "latest":
        raise ModelError(f'The model uses an operating expense basis of "{basis}". Letters support "Latest" only.')
    lesser_of = _flag(get("Apply the lesser-of test"))
    min_alloc = D(get("Minimum allocation"))
    rounding = D(get("Round allocations to the nearest"))
    if rounding != 1:
        raise ModelError("The model rounds allocations to something other than a whole dollar. Letters print whole dollars.")
    if not (Decimal(0) < noi_rate < 1) or not (Decimal(0) <= noe_rate < 1) or flat < 0:
        raise ModelError("The Method A rates on the Assumptions sheet look wrong. Check them in the model.")
    rates = Rates(noi_rate=noi_rate, noe_rate=noe_rate, flat_deduction=flat, lesser_of=lesser_of, min_allocation=min_alloc)

    # --- Parish Data ------------------------------------------------------------------------------
    hi, cols = _header(parish_rows, "#", "Parish", SHEET_PARISH)
    c_ueid, c_city = (_need(cols, n, SHEET_PARISH) for n in ("Parish UEID", "City"))
    c_pr = _find(cols, r"\d{4} parochial report name", "<year> parochial report name", SHEET_PARISH)
    c_filed = _find(cols, r"Filed \d{4}\?", "Filed <year>?", SHEET_PARISH)
    year1_cols = [(int(m.group(1)), j) for n, j in cols.items() if (m := re.fullmatch(r"NOI (\d{4})", n))]
    used_cols = sorted((int(m.group(1)), j) for n, j in cols.items() if (m := re.fullmatch(r"NOI (\d{4}) used", n)))
    if not year1_cols or len(used_cols) < 2:
        raise ModelError('The "Parish Data" sheet must have "NOI <year>" and two "NOI <year> used" columns.')
    year1, c_noi1 = min(year1_cols)
    (year2, c_noi2), (year3, c_noi3) = used_cols[0], used_cols[-1]
    if not (year1 < year2 < year3):
        raise ModelError(f'The "Parish Data" income columns must be three different, increasing years (found {year1}, {year2}, {year3}).')
    c_noe = None
    for n, j in cols.items():
        m = re.fullmatch(r"NOE (\d{4}) used", n)
        if m and int(m.group(1)) == year3:
            c_noe = j
    if c_noe is None:
        raise ModelError(f'The "Parish Data" sheet has no "NOE {year3} used" column.')
    c_noiq, c_noeq = _need(cols, "NOI quality", SHEET_PARISH), _need(cols, "NOE quality", SHEET_PARISH)

    # --- Method A (the model's own results) ---------------------------------------------------------
    mi, mcols = _header(method_rows, "#", "Parish", SHEET_METHOD_A)
    m_avg, m_latest = _need(mcols, "NOI 3-yr avg", SHEET_METHOD_A), _need(mcols, "NOI latest", SHEET_METHOD_A)
    m_noe = _need(mcols, "Operating expense used", SHEET_METHOD_A)
    m_t1, m_t2 = _need(mcols, "Test 1: 3-yr average", SHEET_METHOD_A), _need(mcols, "Test 2: latest year", SHEET_METHOD_A)
    alloc_cols = sorted((int(m.group(1)), j) for n, j in mcols.items() if (m := re.fullmatch(r"(\d{4}) allocation", n)))
    if len(alloc_cols) < 2:
        raise ModelError('The "Method A - Historic" sheet must have "<year> allocation" columns for this and last year.')
    (prior_year, m_prior), (target_year, m_total) = alloc_cols[0], alloc_cols[-1]
    method: dict[tuple[str, str], tuple] = {}
    for r in method_rows[mi + 1:]:
        if len(r) > 1 and r[1] and str(r[1]).strip().upper() != "TOTAL" and isinstance(r[0], (int, float)):
            method[(str(int(r[0])), str(r[1]).strip())] = r

    rows, warnings, seen_ueid = [], [], set()
    for r in parish_rows[hi + 1:]:
        if len(r) < 2 or r[1] is None or str(r[1]).strip() == "":
            continue
        name = str(r[1]).strip()
        if name.upper() == "TOTAL" or not isinstance(r[0], (int, float)):
            continue
        if len(rows) >= MAX_ROWS:
            raise ModelError(f"The model lists more than {MAX_ROWS} parishes. That does not look like the EDOM model.")
        n = str(int(r[0]))
        ueid = str(r[c_ueid]).strip() if r[c_ueid] else ""
        if ueid and ueid in seen_ueid:
            raise ModelError(f"{name}: the Parish UEID {ueid} appears twice in the model.")
        seen_ueid.add(ueid)
        blanks = [label for label, idx in ((f"NOI {year1}", c_noi1), (f"NOI {year2} used", c_noi2), (f"NOI {year3} used", c_noi3),
                                           (f"NOE {year3} used", c_noe)) if _blank(r, idx)]
        mr = method.get((n, name))
        if mr is None:
            warnings.append(f"{name} is on Parish Data but has no row on Method A, so there is nothing to tie its figures to.")
        rows.append({
            "n": int(r[0]), "parish": name, "ueid": ueid or None,
            "pr_name": str(r[c_pr]).strip() if r[c_pr] else "", "city": str(r[c_city]).strip() if r[c_city] else "",
            "filed": str(r[c_filed] or "").strip(), "blank": blanks,
            "noi_y1": _num(r, c_noi1, name, f"NOI {year1}"), "noi_y2": _num(r, c_noi2, name, f"NOI {year2} used"),
            "noi_y3": _num(r, c_noi3, name, f"NOI {year3} used"), "noe": _num(r, c_noe, name, f"NOE {year3} used"),
            "noi_quality": str(r[c_noiq] or "").strip(), "noe_quality": str(r[c_noeq] or "").strip(),
            "model_noi_avg": _num(mr, m_avg, name, "NOI 3-yr avg", required=False) if mr else None,
            "model_noi_latest": _num(mr, m_latest, name, "NOI latest", required=False) if mr else None,
            "model_noe": _num(mr, m_noe, name, "Operating expense used", required=False) if mr else None,
            "model_a": _num(mr, m_t1, name, "Test 1", required=False) if mr else None,
            "model_b": _num(mr, m_t2, name, "Test 2", required=False) if mr else None,
            "model_total": _num(mr, m_total, name, f"{target_year} allocation", required=False) if mr else None,
            "model_prior": _num(mr, m_prior, name, f"{prior_year} allocation", required=False) if mr else None,
        })
    if not rows:
        raise ModelError('The "Parish Data" sheet has no parish rows.')
    computed = sum(1 for x in rows if x["model_a"] is not None)
    if computed < len(rows) // 2:
        raise ModelError("The model has no calculated values (it looks like it was saved without Excel). "
                         "Open it in Excel, save it, and upload it again.")
    for name in {k[1] for k in method} - {x["parish"] for x in rows}:
        warnings.append(f"{name} is on Method A but not on Parish Data.")
    return {
        "years": {"year1": year1, "year2": year2, "year3": year3, "prior_year": prior_year, "target_year": target_year},
        "rates": rates,
        "assumptions": {k: (str(v) if v is not None else None) for k, v in a.items()},
        "rows": rows, "warnings": warnings,
    }
