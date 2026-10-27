"""
payroll_import_csv.py -- the period's hours as a Checkwriters PAYROLL IMPORT file (26-129 / 26-158, 2026-10-26).

Checkwriters' import (help.checkwriters.com/knowledge/payroll-import) takes a CSV or .txt file with NO header row and
one line per earning code per employee:   ID, DET, DET Code, Hours, Rate, Amount
  ID        the employee's Checkwriters number (portal.staff_roster.employee_number)
  DET       E = earning (this file only ever carries earnings)
  DET Code  a code set up for the company (2Reg, OT, ...): must exist in Checkwriters or the line is an error
  Hours     blank Rate means the employee's base rate is used
  Rate      always blank: Beacon never stores or sends pay
  Amount    always blank, for the same reason (flat or per-unit payments stay hand-keyed)
Figures cannot contain commas; the file name must be under 15 characters. Checkwriters support has to enable the import and
map its columns for the company: this file follows the standard layout they document.

WHAT GOES IN: exactly the hours the Excel export's Summary sheet calls "Grand total" (the hours to KEY): every line that is
in force (Regular standing hours included), one row per employee and kind of hours. NEVER in the file: a line still waiting
for a decision, hours a manual check or void-and-reissue already paid (they are not in period_rows at all), pay of any kind.

WHEN IT WILL NOT BUILD (a bare CSV cannot carry the "NOT FINAL" banner the Excel sheet shows, and a wrong import is easy to
miss): the hours must be APPROVED (Jay, 2026-10-27: Final now means they were ENTERED into Checkwriters, so the file is made before
Final: every department green and no late email waiting, or the hours already Final), nothing may still need a decision, nobody
named in an email may be waiting to be entered, every hour must belong to a person with a plain numeric Checkwriters number, and
every kind of hours that has hours must have a Checkwriters code. Each refusal says which. The file is never built "as far as it can".

CODES: default 2Reg / OT / Sick / Vaca / Holi (the codes DME's paid register and Labor Distribution already show). A
different company setting lives in checkreq.app_settings, key  payroll_import_codes:<org id>, as JSON
{"regular": "2Reg", ...}; keys it leaves out keep the default. Nothing else about the layout is configurable.
"""
from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

import app_settings
import payroll_late_rules as late_rules
import payroll_totals as pt

DEFAULT_CODES = {"regular": "2Reg", "overtime": "OT", "sick": "Sick", "vacation": "Vaca", "holiday": "Holi"}
SETTING_KEY = "payroll_import_codes:{org_id}"
_CODE_OK = re.compile(r"^[A-Za-z0-9_\-]{1,12}$")
_ID_OK = re.compile(r"^[0-9]{1,12}$")
_TWO = Decimal("0.01")
MAX_FILENAME = 14  # Checkwriters: under 15 characters, extension included (stricter reading, never rejected for length)


class PayrollImportError(Exception):
    """A plain-words reason the file was not built."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ------------------------------------------------------------------ pure pieces (no database)

def parse_code_override(raw: str | None) -> dict[str, str]:
    """The company's own codes, or {} when none are set. A setting that cannot be read is an error, never a quiet fall-back
    to the defaults: a wrong code is exactly what a company override exists to prevent."""
    if raw is None or not str(raw).strip():
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise PayrollImportError("The Checkwriters code setting for this diocese is not valid. Ask a Beacon Admin to fix it.")
    if not isinstance(data, dict):
        raise PayrollImportError("The Checkwriters code setting for this diocese is not valid. Ask a Beacon Admin to fix it.")
    out: dict[str, str] = {}
    for key, code in data.items():
        if not isinstance(key, str) or not isinstance(code, str) or not _CODE_OK.match(code.strip()):
            raise PayrollImportError(f"The Checkwriters code for {key!r} is not valid: use letters and digits only, up to 12.")
        out[key.strip().lower()] = code.strip()
    return out


def merged_codes(override: dict[str, str] | None) -> dict[str, str]:
    codes = dict(DEFAULT_CODES)
    codes.update(override or {})
    return codes


def format_hours(h) -> str:
    """40.00 -> '40', 32.50 -> '32.5', 7.25 -> '7.25'. Refuses a figure that is not a whole number of hundredths (a figure
    with more places would be rounded silently otherwise), zero or negative, and anything with a comma."""
    d = Decimal(str(h))
    if d <= 0:
        raise PayrollImportError("An hours figure is zero or negative, so the file was not built.")
    q = d.quantize(_TWO, rounding=ROUND_HALF_UP)
    if q != d:
        raise PayrollImportError(f"An hours figure ({d}) has more than two decimal places, so the file was not built.")
    s = f"{q:f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _who(r: dict) -> str:
    name = ", ".join(x for x in (r.get("last_name"), r.get("first_name")) if x) or "A person"
    return f"{name} ({r.get('dept_name') or r.get('parish_code') or 'department'})"


def approval_waiting(org_id: int, period_id: int) -> list[str]:
    """What still stands between this period's hours and 'approved' (the rest of what Mark hours Final waits on): departments that are
    not green yet, and late emails waiting in the queue. [] means nothing is waiting."""
    out = []
    notdone = pt.parishes_not_done(org_id, period_id)
    if notdone:
        codes = ", ".join(p["code"] or p["name"] for p in notdone[:8]) + (" and more" if len(notdone) > 8 else "")
        out.append(f"{len(notdone)} department(s) are not approved yet ({codes})")
    late = late_rules.waiting_count(org_id)
    if late:
        out.append(f"{late} late email(s) are waiting in the Late emails queue")
    return out


def check_ready(period: dict, rows: list[dict], unentered: int, waiting: list[str] | None = None) -> None:
    """The refusals that do not depend on the codes. Raises PayrollImportError with the reason.

    Jay, 2026-10-27: Final now means the hours were ENTERED INTO Checkwriters, so this file is made BEFORE Final, from approved hours.
    A period that is not Final yet needs `waiting` (approval_waiting(): [] = checked and nothing is waiting); None means nobody
    checked, which is refused, so a caller that forgets cannot get a file. A Final period was checked when it was marked Final."""
    if not period.get("hours_finalized_at"):
        if waiting is None:
            raise PayrollImportError("The approvals could not be checked, so the file was not built.")
    left = pt.unresolved(rows)
    if left:
        raise PayrollImportError(f"{len(left)} line(s) still need a decision, so the file was not built. Settle them, then try again.")
    if unentered:
        raise PayrollImportError(f"{unentered} person(s) named in emails were not entered yet, so the file was not built.")
    if not period.get("hours_finalized_at") and waiting:
        raise PayrollImportError("; ".join(waiting).capitalize() + ", so the file was not built. Approve each department on Time Review first.")


def build_lines(rows: list[dict], codes: dict[str, str]) -> list[dict]:
    """One dict per employee and kind of hours: {emp, code, hours (Decimal), category_key, dept, name}, sorted by employee
    number then the usual kind-of-hours order. Raises PayrollImportError when any hour cannot be written correctly."""
    totals: dict[tuple, dict] = {}
    missing_code: dict[str, str] = {}
    bad_id: list[str] = []
    for r in rows:
        h = r.get("counted_hours")
        if h is None:
            continue
        if r["state"] != "ok":
            raise PayrollImportError(f"{_who(r)} has hours in an unexpected state ({r['state']}), so the file was not built.")
        h = Decimal(h)
        if h == 0:
            continue
        emp = (r.get("employee_number") or "").strip()
        if not _ID_OK.match(emp):
            bad_id.append(_who(r))
            continue
        code = codes.get(r["category_key"])
        if not code:
            missing_code[r["category_key"]] = r.get("category_label") or r["category_key"]
            continue
        key = (emp, r["category_key"])
        t = totals.setdefault(key, {"emp": emp, "code": code, "hours": Decimal(0), "category_key": r["category_key"],
                                    "category_sort": r.get("category_sort") or 0, "dept": r.get("dept_name"), "name": _who(r)})
        t["hours"] += h
    if bad_id:
        shown = "; ".join(bad_id[:5]) + (f" and {len(bad_id) - 5} more" if len(bad_id) > 5 else "")
        raise PayrollImportError(f"No usable Checkwriters number (digits only) for: {shown}. Fix it on the Employees screen, then try again.")
    if missing_code:
        names = ", ".join(sorted(missing_code.values()))
        raise PayrollImportError(f"No Checkwriters code is set for: {names}. The file was not built.")
    out = sorted(totals.values(), key=lambda t: (int(t["emp"]), t["category_sort"], t["category_key"]))
    for t in out:
        format_hours(t["hours"])  # refuses zero, negative and over-precise sums here, before any file exists
    return out


def render_csv(lines: list[dict]) -> bytes:
    """No header. ID,E,code,hours,, (Rate and Amount left empty), CRLF line ends, ASCII only."""
    if not lines:
        raise PayrollImportError("There are no hours in force for this period, so there is nothing to import.")
    body = "".join(f"{t['emp']},E,{t['code']},{format_hours(t['hours'])},,\r\n" for t in lines)
    return body.encode("ascii")


def filename_for(org: dict, period: dict) -> str:
    """DME261029.csv: the organization's code (up to 3 letters) and the pay date as YYMMDD, 13 characters."""
    d = period.get("pay_date") or period["period_end"]
    code = re.sub(r"[^A-Za-z0-9]", "", str(org.get("code") or "PAY"))[:3].upper() or "PAY"
    name = f"{code}{d.strftime('%y%m%d')}.csv"
    if len(name) > MAX_FILENAME:  # cannot happen with the rule above; kept so a future change fails loudly
        raise PayrollImportError("The file name would be too long for Checkwriters.")
    return name


def summarize(lines: list[dict]) -> dict:
    return {"lines": len(lines), "employees": len({t["emp"] for t in lines}),
            "hours": sum((t["hours"] for t in lines), Decimal(0))}


# ------------------------------------------------------------------ with the database

def codes_for_org(org_id: int) -> dict[str, str]:
    return merged_codes(parse_code_override(app_settings.get_setting(SETTING_KEY.format(org_id=org_id))))


def build_for_period(org: dict, period_id: int) -> tuple[bytes, str, dict]:
    """(file bytes, file name, {"lines","employees","hours"}). PayrollImportError / pt.PayrollError when it cannot be built."""
    period = pt.get_period(org["id"], period_id)
    rows = pt.period_rows(org["id"], period_id)
    waiting = None if period.get("hours_finalized_at") else approval_waiting(org["id"], period_id)
    check_ready(period, rows, sum(pt.count_unentered(org["id"], period_id).values()), waiting)
    lines = build_lines(rows, codes_for_org(org["id"]))
    return render_csv(lines), filename_for(org, period), summarize(lines)


# ------------------------------------------------------------------ the screen route

router = APIRouter()
_gate = None


def register(app, *, gate) -> None:
    """gate = payroll_screens._require_hr_admin: (user, org, None) when allowed, (None, None, response) when not.
    Registered from payroll_screens.register, so it sits BEFORE timekeeping_status's /status/{period}/{parish} route."""
    global _gate
    _gate = gate
    app.include_router(router)


def _back(period_id: int, err: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/timekeeping/status/{period_id}?error={quote_plus(err)}", status_code=303)


@router.get("/admin/timekeeping/status/{period_id}/checkwriters-csv")
def checkwriters_csv(period_id: int, request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    try:
        data, filename, _summary = build_for_period(org, period_id)
    except PayrollImportError as e:
        return _back(period_id, e.message)
    except pt.PayrollError as e:
        return _back(period_id, e.message)
    return Response(content=data, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "private, no-store"})
