"""
payroll_export.py -- the period's Excel export: the sheet staff key into Checkwriters.

Sheets: Review (first: everything still needing a decision, and whether the hours are FINAL),
Hours (one row per employee and category: the hours counted, where they came from, the review
status and a link to the email), Detail (the parish's own daily-grid cells, as before).

The export always runs. It says FINAL only when an hr_admin has marked the period's hours Final,
which payroll_totals.finalize_period allows only when nothing is left to review. A line that
still needs a decision shows its hours in the "Period total" column but leaves "Counted hours"
blank, so it cannot be keyed by mistake.

Hours only. Never pay.
"""
from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import db
import payroll_totals as pt

_HEADER_FILL = "1F3D2E"
_ISSUE = {
    "needs_review": "Needs review",
    "proposal": "A different figure is waiting",
    "conflict": "Parish daily grid and email both have hours: pick one",
    "pending_hire": "New hire not on the roster yet",
}
_DETAIL_SQL = """
    SELECT p.code AS parish_code, p.name AS parish_name, sr.last_name, sr.first_name,
           c.label AS category_label, te.work_date, te.hours
      FROM portal.time_entries te
      JOIN portal.staff_roster sr ON sr.id = te.staff_id
      JOIN portal.parishes p ON p.id = sr.parish_id
      JOIN portal.timekeeping_categories c ON c.id = te.category_id
     WHERE te.period_id = %(period_id)s AND p.org_id = %(org_id)s AND te.hours > 0
     ORDER BY p.name, sr.last_name, sr.first_name, te.work_date, c.sort_order
"""


def _header(ws, row: int, headers: list[str]) -> None:
    fill = PatternFill(start_color=_HEADER_FILL, end_color=_HEADER_FILL, fill_type="solid")
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=i, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = fill
        c.alignment = Alignment(vertical="center")
    ws.freeze_panes = ws.cell(row=row + 1, column=1)


def _widths(ws, widths: list[int]) -> None:
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _name(r: dict) -> str:
    return ", ".join(x for x in (r.get("last_name"), r.get("first_name")) if x)


def _link(ws, row: int, col: int, ref: str | None) -> None:
    url = pt.message_link(ref)
    if url:
        c = ws.cell(row=row, column=col, value="Open email")
        c.hyperlink = url
        c.font = Font(color="0563C1", underline="single")


def build_export_workbook(org: dict, period: dict) -> bytes:
    rows = pt.period_rows(org["id"], period["id"])
    left = pt.unresolved(rows)
    final = bool(period.get("hours_finalized_at"))
    wb = Workbook()

    ws = wb.active
    ws.title = "Review"
    if final and not left:
        banner = "FINAL: these hours are approved and ready to key into Checkwriters."
    elif left:
        banner = f"NOT FINAL: {len(left)} line(s) below still need a decision. Do not key those hours yet."
    else:
        banner = "NOT FINAL: nothing is waiting, but an hr_admin has not marked the hours Final yet."
    ws.cell(row=1, column=1, value=banner).font = Font(bold=True, size=12, color=("1F3D2E" if final and not left else "9C0006"))
    label = period.get("label") or f"{period['period_start']} to {period['period_end']}"
    pay = f"  Pay date {period['pay_date']}" if period.get("pay_date") else ""
    ws.cell(row=2, column=1, value=f"{org['code']} {label}{pay}")
    _header(ws, 4, ["Parish Code", "Parish", "Employee #", "Name", "Category", "Hours in force",
                    "Figure waiting", "Why it is listed", "Email"])
    for i, r in enumerate(left, start=5):
        ws.cell(row=i, column=1, value=r["parish_code"])
        ws.cell(row=i, column=2, value=r["parish_name"])
        ws.cell(row=i, column=3, value=r["employee_number"])
        ws.cell(row=i, column=4, value=_name(r))
        ws.cell(row=i, column=5, value=r["category_label"])
        ws.cell(row=i, column=6, value=None if r["counted_hours"] is None else float(r["counted_hours"]))
        ws.cell(row=i, column=7, value=None if r["proposed_hours"] is None else float(r["proposed_hours"]))
        ws.cell(row=i, column=8, value=_ISSUE.get(r["state"], r["state"]))
        _link(ws, i, 9, r["proposed_ref"] if r["state"] == "proposal" else r["source_ref"])
    if not left:
        ws.cell(row=5, column=1, value="Nothing needs review.")
    _widths(ws, [12, 32, 14, 28, 14, 14, 14, 46, 14])

    wh = wb.create_sheet("Hours")
    _header(wh, 1, ["Parish Code", "Parish", "Employee #", "Last Name", "First Name", "Category",
                    "Counted hours", "Source", "Review status", "Email", "Daily grid hours", "Period total"])
    for i, r in enumerate(rows, start=2):
        wh.cell(row=i, column=1, value=r["parish_code"])
        wh.cell(row=i, column=2, value=r["parish_name"])
        wh.cell(row=i, column=3, value=r["employee_number"])
        wh.cell(row=i, column=4, value=r["last_name"])
        wh.cell(row=i, column=5, value=r["first_name"])
        wh.cell(row=i, column=6, value=r["category_label"])
        c = wh.cell(row=i, column=7, value=None if r["counted_hours"] is None else float(r["counted_hours"]))
        c.number_format = "0.00"
        wh.cell(row=i, column=8, value=r["source"])
        wh.cell(row=i, column=9, value=_ISSUE.get(r["state"], "OK" if r["review_status"] != "entered" else "Entered by the parish"))
        _link(wh, i, 10, r["source_ref"])
        wh.cell(row=i, column=11, value=None if r["grid_hours"] is None else float(r["grid_hours"])).number_format = "0.00"
        wh.cell(row=i, column=12, value=None if r["line_hours"] is None else float(r["line_hours"])).number_format = "0.00"
    if not rows:
        wh.cell(row=2, column=1, value="No hours recorded for this period yet.")
    _widths(wh, [12, 32, 14, 18, 14, 14, 14, 10, 40, 14, 16, 14])

    wd = wb.create_sheet("Detail")
    _header(wd, 1, ["Parish Code", "Parish", "Last Name", "First Name", "Category", "Date", "Hours"])
    detail = db.query(_DETAIL_SQL, {"period_id": period["id"], "org_id": org["id"]})
    for i, r in enumerate(detail, start=2):
        wd.cell(row=i, column=1, value=r["parish_code"])
        wd.cell(row=i, column=2, value=r["parish_name"])
        wd.cell(row=i, column=3, value=r["last_name"])
        wd.cell(row=i, column=4, value=r["first_name"])
        wd.cell(row=i, column=5, value=r["category_label"])
        d = r["work_date"]
        wd.cell(row=i, column=6, value=d.isoformat() if hasattr(d, "isoformat") else str(d))
        wd.cell(row=i, column=7, value=float(r["hours"])).number_format = "0.00"
    if not detail:
        wd.cell(row=2, column=1, value="No daily hours were entered in Beacon for this period.")
    _widths(wd, [12, 32, 18, 14, 14, 12, 10])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def filename_for(org: dict, period: dict) -> str:
    d = period.get("pay_date") or period["period_end"]
    return f"{org['code']} Parish Hours - PD {d.strftime('%Y.%m.%d')}.xlsx"


def build_for_period(org: dict, period_id: int) -> tuple[bytes, str]:
    period = pt.get_period(org["id"], period_id)
    return build_export_workbook(org, period), filename_for(org, period)
