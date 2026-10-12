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

from decimal import Decimal

import category_order
import db
import email_pdf
import payroll_totals as pt

_HEADER_FILL = "1F3D2E"
_ISSUE = {
    "needs_review": "Needs review",
    "proposal": "A different figure is waiting",
    "conflict": "Parish daily grid and email both have hours: pick one",
    "pending_hire": "New hire not on the roster yet",
}
_EMAIL_CHECK = {
    "email_match": "matches the email", "email_differs": "email shows a different number",
    "email_named_no_number": "named in an email, no number", "email_standing": "standing hours",
    "email_none": "no email found",
}
_DETAIL_SQL = """
    SELECT p.code AS parish_code, p.name AS parish_name, COALESCE(p.short_name, p.name) AS dept_name, sr.last_name, sr.first_name,
           c.label AS category_label, te.work_date, te.hours
      FROM portal.time_entries te
      JOIN portal.staff_roster sr ON sr.id = te.staff_id
      JOIN portal.parishes p ON p.id = sr.parish_id
      JOIN portal.timekeeping_categories c ON c.id = te.category_id
     WHERE te.period_id = %(period_id)s AND p.org_id = %(org_id)s AND te.hours > 0
     ORDER BY p.code, sr.last_name, sr.first_name, te.work_date, c.sort_order
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


def _link(ws, row: int, col: int, ref: str | None, org_id: int | None = None, base_url: str | None = None) -> None:
    """A "Open email PDF" hyperlink, only when the email's PDF is stored and the caller knows Beacon's
    address (an Excel link has to be absolute). No link at all otherwise: the old Gmail search link
    never worked."""
    url = email_pdf.abs_url_for(org_id, ref, base_url) if (org_id is not None and base_url) else None
    if url:
        c = ws.cell(row=row, column=col, value="Open email PDF")
        c.hyperlink = url
        c.font = Font(color="0563C1", underline="single")


def summary_figures(org_id: int, rows: list[dict]) -> dict:
    """The Summary sheet's numbers, one row per employee, grouped by department (code order).
    Standing = hours whose source is standing. Entered = every other hour in force, by kind of hours.
    Grand total = Standing + Entered, so each department subtotal can be compared with the Labor Summary."""
    cats = category_order.sort_categories(db.query(
        "SELECT id, key, label, sort_order FROM portal.timekeeping_categories WHERE org_id = %s AND is_active "
        "ORDER BY sort_order", (org_id,)))
    people: dict = {}
    for r in rows:
        if r["counted_hours"] is None:
            continue  # only hours in force count here: lines still waiting for a decision are on the Review sheet
        key = (r["parish_code"] or "~", r["parish_name"] or "", r["last_name"] or "", r["first_name"] or "",
               r["staff_id"] if r["staff_id"] is not None else ("p", r["pending_change_id"]))
        d = people.setdefault(key, {"parish_code": r["parish_code"], "dept": r.get("dept_name") or r["parish_name"],
                                    "employee_number": r["employee_number"], "name": _name(r),
                                    "cat": {}, "std": Decimal(0)})
        h = Decimal(r["counted_hours"])
        if r["source"] == "standing":
            d["std"] += h
        else:
            d["cat"][r["category_id"]] = d["cat"].get(r["category_id"], Decimal(0)) + h
    depts: dict = {}
    for key in sorted(people, key=lambda k: k[:4]):
        p = people[key]
        p["entered"] = sum(p["cat"].values(), Decimal(0))
        p["grand"] = p["entered"] + p["std"]
        depts.setdefault((key[0], p["dept"]), []).append(p)
    return {"cats": cats, "depts": depts}


def _summary_sheet(wb, org: dict, rows: list[dict]) -> None:
    """A new first sheet: one row per employee with each kind of hours, the Entered total, Standing and the Grand total,
    a subtotal after each department and a grand total at the bottom."""
    fig = summary_figures(org["id"], rows)
    cats = fig["cats"]
    ws = wb.create_sheet("Summary", 0)
    heads = ["Parish Code", "Department", "Employee #", "Name"] + [c["short"] for c in cats] + [
        "Entered total", "Standing", "Grand total"]
    _header(ws, 1, heads)
    ncat = len(cats)
    bold = Font(bold=True)
    sub_fill = PatternFill(start_color="E8F3E6", end_color="E8F3E6", fill_type="solid")
    row = 2
    grand = {"cat": {c["id"]: Decimal(0) for c in cats}, "entered": Decimal(0), "std": Decimal(0), "grand": Decimal(0)}

    def put(r: int, c: int, v: Decimal | None, is_bold: bool = False, fill=None) -> None:
        cell = ws.cell(row=r, column=c, value=None if v is None else float(v))
        cell.number_format = "0.00"
        if is_bold:
            cell.font = bold
        if fill:
            cell.fill = fill

    for (code, dept), people in fig["depts"].items():
        sub = {"cat": {c["id"]: Decimal(0) for c in cats}, "entered": Decimal(0), "std": Decimal(0), "grand": Decimal(0)}
        for p in people:
            ws.cell(row=row, column=1, value=code)
            ws.cell(row=row, column=2, value=dept)
            ws.cell(row=row, column=3, value=p["employee_number"])
            ws.cell(row=row, column=4, value=p["name"])
            for i, c in enumerate(cats):
                v = p["cat"].get(c["id"])
                put(row, 5 + i, v)
                if v:
                    sub["cat"][c["id"]] += v
            put(row, 5 + ncat, p["entered"] or None)
            put(row, 6 + ncat, p["std"] or None)
            put(row, 7 + ncat, p["grand"], is_bold=True)
            sub["entered"] += p["entered"]
            sub["std"] += p["std"]
            sub["grand"] += p["grand"]
            row += 1
        fill = sub_fill
        ws.cell(row=row, column=1, value=code).font = bold
        ws.cell(row=row, column=2, value=f"{dept} total").font = bold
        for col in range(1, 8 + ncat):
            ws.cell(row=row, column=col).fill = fill
        for i, c in enumerate(cats):
            put(row, 5 + i, sub["cat"][c["id"]], True, fill)
            grand["cat"][c["id"]] += sub["cat"][c["id"]]
        put(row, 5 + ncat, sub["entered"], True, fill)
        put(row, 6 + ncat, sub["std"], True, fill)
        put(row, 7 + ncat, sub["grand"], True, fill)
        grand["entered"] += sub["entered"]
        grand["std"] += sub["std"]
        grand["grand"] += sub["grand"]
        row += 1
    if not fig["depts"]:
        ws.cell(row=2, column=1, value="No hours are in force for this period yet.")
        row = 3
    else:
        ws.cell(row=row, column=2, value="GRAND TOTAL").font = bold
        for i, c in enumerate(cats):
            put(row, 5 + i, grand["cat"][c["id"]], True)
        put(row, 5 + ncat, grand["entered"], True)
        put(row, 6 + ncat, grand["std"], True)
        put(row, 7 + ncat, grand["grand"], True)
        row += 1
    ws.cell(row=row + 1, column=1, value="Only hours in force are counted. Lines still waiting for a decision are on the Review sheet. "
                                         "Standing = standing hours carried forward. Entered = everything else. "
                                         "Compare each department total with the Labor Summary.").font = Font(italic=True)
    _widths(ws, [12, 32, 14, 28] + [9] * ncat + [14, 11, 13])


def build_export_workbook(org: dict, period: dict, base_url: str | None = None) -> bytes:
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
    _header(ws, 4, ["Parish Code", "Department", "Employee #", "Name", "Category", "Hours in force",
                    "Figure waiting", "Why it is listed", "Email"])
    for i, r in enumerate(left, start=5):
        ws.cell(row=i, column=1, value=r["parish_code"])
        ws.cell(row=i, column=2, value=r.get("dept_name") or r["parish_name"])
        ws.cell(row=i, column=3, value=r["employee_number"])
        ws.cell(row=i, column=4, value=_name(r))
        ws.cell(row=i, column=5, value=r["category_label"])
        ws.cell(row=i, column=6, value=None if r["counted_hours"] is None else float(r["counted_hours"]))
        ws.cell(row=i, column=7, value=None if r["proposed_hours"] is None else float(r["proposed_hours"]))
        ws.cell(row=i, column=8, value=_ISSUE.get(r["state"], r["state"]))
        _link(ws, i, 9, r["proposed_ref"] if r["state"] == "proposal" else r["source_ref"], org["id"], base_url)
    if not left:
        ws.cell(row=5, column=1, value="Nothing needs review.")
    _widths(ws, [12, 32, 14, 28, 14, 14, 14, 46, 14])

    wh = wb.create_sheet("Hours")
    _header(wh, 1, ["Parish Code", "Department", "Employee #", "Last Name", "First Name", "Category",
                    "Counted hours", "Source", "Review status", "Email", "Daily grid hours", "Period total",
                    "Email check"])
    for i, r in enumerate(rows, start=2):
        wh.cell(row=i, column=1, value=r["parish_code"])
        wh.cell(row=i, column=2, value=r.get("dept_name") or r["parish_name"])
        wh.cell(row=i, column=3, value=r["employee_number"])
        wh.cell(row=i, column=4, value=r["last_name"])
        wh.cell(row=i, column=5, value=r["first_name"])
        wh.cell(row=i, column=6, value=r["category_label"])
        c = wh.cell(row=i, column=7, value=None if r["counted_hours"] is None else float(r["counted_hours"]))
        c.number_format = "0.00"
        wh.cell(row=i, column=8, value=r["source"])
        wh.cell(row=i, column=9, value=_ISSUE.get(r["state"], "OK" if r["review_status"] != "entered" else "Entered by the parish"))
        _link(wh, i, 10, r["source_ref"], org["id"], base_url)
        wh.cell(row=i, column=11, value=None if r["grid_hours"] is None else float(r["grid_hours"])).number_format = "0.00"
        wh.cell(row=i, column=12, value=None if r["line_hours"] is None else float(r["line_hours"])).number_format = "0.00"
        wh.cell(row=i, column=13, value=", ".join(_EMAIL_CHECK[f] for f in r["flags"] if f in _EMAIL_CHECK) or None)
    if not rows:
        wh.cell(row=2, column=1, value="No hours recorded for this period yet.")
    _widths(wh, [12, 32, 14, 18, 14, 14, 14, 10, 40, 14, 16, 14, 34])

    wd = wb.create_sheet("Detail")
    _header(wd, 1, ["Parish Code", "Department", "Last Name", "First Name", "Category", "Date", "Hours"])
    detail = db.query(_DETAIL_SQL, {"period_id": period["id"], "org_id": org["id"]})
    for i, r in enumerate(detail, start=2):
        wd.cell(row=i, column=1, value=r["parish_code"])
        wd.cell(row=i, column=2, value=r.get("dept_name") or r["parish_name"])
        wd.cell(row=i, column=3, value=r["last_name"])
        wd.cell(row=i, column=4, value=r["first_name"])
        wd.cell(row=i, column=5, value=r["category_label"])
        d = r["work_date"]
        wd.cell(row=i, column=6, value=d.isoformat() if hasattr(d, "isoformat") else str(d))
        wd.cell(row=i, column=7, value=float(r["hours"])).number_format = "0.00"
    if not detail:
        wd.cell(row=2, column=1, value="No daily hours were entered in Beacon for this period.")
    _widths(wd, [12, 32, 18, 14, 14, 12, 10])

    _summary_sheet(wb, org, rows)
    wb.active = 0

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def notes_filename_for(org: dict, period: dict) -> str:
    d = period.get("pay_date") or period["period_end"]
    return f"{org['code']} Payroll Notes - PD {d.strftime('%Y.%m.%d')}.xlsx"


def build_notes_workbook(org: dict, period: dict, notes: list[dict], zone: str | None, base_url: str | None = None) -> bytes:
    """Every comment, note and request from the period's payroll emails, one row each, for the person who keys the payroll."""
    import org_time
    wb = Workbook()
    ws = wb.active
    ws.title = "Payroll notes"
    heads = ["Department", "Email received", "From", "Subject", "Kind", "Employee", "Note", "What was done", "Email PDF"]
    ws.cell(row=1, column=1, value=f"{org['name']}: comments, notes and requests from the payroll emails, pay date "
                                   f"{(period.get('pay_date') or period['period_end']).strftime('%m/%d/%y')}").font = Font(bold=True, size=12)
    _header(ws, 3, heads)
    r = 4
    for n in notes:
        link = email_pdf.abs_url_for(org["id"], n["source_ref"], base_url) if base_url else None
        vals = [(f"{n['parish_code']} {n['parish_name']}" if n.get("parish_code") else "Not tied to one department"),
                org_time.format_local(n["received_at"], zone) if n.get("received_at") else "", n.get("email_sender") or "",
                n.get("email_subject") or "", n["kind_label"], n.get("employee_text") or "", n["note_text"], n.get("result_text") or "",
                "Open email PDF" if link else ""]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.alignment = Alignment(vertical="top", wrap_text=c in (3, 4, 7, 8))
            if c == 9 and link:
                cell.hyperlink = link
                cell.font = Font(color="0563C1", underline="single")
        r += 1
    for col, width in enumerate([26, 20, 22, 34, 16, 22, 70, 30, 16], start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def filename_for(org: dict, period: dict) -> str:
    d = period.get("pay_date") or period["period_end"]
    return f"{org['code']} Parish Hours - PD {d.strftime('%Y.%m.%d')}.xlsx"


def build_for_period(org: dict, period_id: int) -> tuple[bytes, str]:
    period = pt.get_period(org["id"], period_id)
    return build_export_workbook(org, period), filename_for(org, period)
