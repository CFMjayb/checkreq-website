"""
payroll_report_filing.py -- "File Reports Now Here": Beacon files a closed pay period's DEPARTMENT payroll reports into the Documents
site itself (26-158 / 26-129, 2026-10-22, Jay: "Reports Posted via Phoenix" or "File Reports Now Here").

What it does, for one pay period:
  reads    Accounting library > 17. Payroll > <year> Payroll Reports > <pay date YYYY.MM.DD>   (the Phoenix workbook / payroll PC saves
           each pay date's reports there; the final-set folder wins over the working folders, as in the 2026-10-20 backfill)
  copies   each department's two reports (the Labor Distribution split and the CDF payroll funding report) into
           Beacon Documents > <NNN. entity> > HRPayroll > Payroll Reports > PD YY.MM.DD PP YY.MM.DD to YY.MM.DD #NN
  records  who filed it and when (payroll_closeout.mark_reports_filed, how = 'beacon').

Rules (the same ones the backfill followed, so a re-run of either is harmless):
  * COPY ONLY. Nothing in the Accounting library is moved, renamed, edited or deleted. Only paths under
    <entity>/HRPayroll/Payroll Reports/<period folder>/ on the Documents site are ever written.
  * Only files whose NAME says they are one department's Labor Distribution or CDF funding report are filed. The all-department
    files (register, summary, YTD, the whole LaborDist.pdf, workbooks, logs) are never filed to a parish folder.
  * A file already there with the same size is left alone. One with a DIFFERENT size is a conflict: it is reported, never overwritten,
    and the period is not marked filed.
  * Everything is checked before anything is written (every department has a folder, no duplicate department files). Counts and
    department codes only in messages: never a file's contents, never an employee's name.
Beacon's DME login (secret dme-sharepoint-credentials) must be allowed to READ the Accounting site. If it is not, the button says
so and writes nothing (the DME Microsoft 365 admin grants it, the same kind of one-time step as for the Documents site).
"""
from __future__ import annotations

import collections
import concurrent.futures
import re
import time
from urllib.parse import quote

import db
import payroll_totals as pt
from payroll_totals import PayrollError

try:  # the real client; tests put a fake one in CLIENT
    import sharepoint_client as _real_client
except Exception:  # pragma: no cover - the module always imports on the server
    _real_client = None
CLIENT = _real_client

# Per diocese (checkreq.organizations.id). Only a diocese listed here gets the "File Reports Now Here" button.
CONFIG = {
    10: {"host": "episcopalmaine.sharepoint.com",
         "source_site": "/sites/Accounting", "source_root": "17. Payroll/{year} Payroll Reports",
         "dest_site": "/sites/BeaconDocuments", "dest_sub": "HRPayroll/Payroll Reports"},
}

LABOR_RE = re.compile(r"^DioME (\d{6}) (\d{6}) (\d{2,3}) (.+)\.pdf$", re.I)
FUND_RE = re.compile(r"^DIOME CDF PAYROLL (\d{6}) - (\d{6}) - 656(\d{3}) - (.+)\.pdf$", re.I)
FINAL_RE = re.compile(r"^(Files|2025\.05\.15|FINAL FILES DISTRIBUTED|FINAL SENT|2026\.06\.25 FINAL Distribution Set|Final Distribution|Final Reports)$")
MAX_WORKERS = 6
NO_ACCESS = ("Beacon cannot read the Accounting site yet. Ask the DME Microsoft 365 administrator to give Beacon's DME SharePoint app "
             "read access to https://episcopalmaine.sharepoint.com/sites/Accounting, then press the button again. "
             "Nothing was filed.")


def enabled_for(org_id: int) -> bool:
    return org_id in CONFIG


def parse_dept(fname: str):
    """-> (kind, code3, name_in_file) or None. kind: 'labor' | 'funding'. code3 is zero-padded to 3 digits."""
    m = LABOR_RE.match(fname)
    if m:
        return ("labor", m.group(3).zfill(3), m.group(4))
    m = FUND_RE.match(fname)
    if m:
        return ("funding", m.group(3), m.group(4))
    return None


def role(rel: str) -> str:
    """Which kind of folder a source file sits in (rel = its folder under the pay date folder, '.' for the pay date folder itself)."""
    if rel in (".", ""):
        return "top"
    first = rel.split("/")[0]
    if FINAL_RE.match(first):
        return "final"
    if first.startswith("DME Labor Dist Split") or "Pre-Run" in first:
        return "prerun"
    if first.startswith("DME Labor Dist") or first.startswith("DME Payroll Funding"):
        return "working"
    return "other"


def period_folder_name(period: dict) -> str:
    """Jay's rule: 'PD YY.MM.DD PP YY.MM.DD to YY.MM.DD #NN' (pay date, period start to end, pay number zero-padded), from the period row."""
    m = re.search(r"#\s*(\d+)", period.get("label") or "")
    if not m or not period.get("pay_date") or not period.get("period_start") or not period.get("period_end"):
        raise PayrollError("refused", "This pay period has no pay number, pay date or dates, so its folder name cannot be worked out. "
                                      "Fix it under Payroll Periods.")
    yy = lambda d: d.strftime("%y.%m.%d")  # noqa: E731
    return f"PD {yy(period['pay_date'])} PP {yy(period['period_start'])} to {yy(period['period_end'])} #{int(m.group(1)):02d}"


def _q(path: str) -> str:
    """Percent-encode every segment of a path used inside a Graph URL ('#', '%', '?' and the like in names would break it)."""
    return "/".join(quote(seg, safe="") for seg in path.strip("/").split("/") if seg != "")


def _retry(fn, *a, tries=4):
    for i in range(tries):
        try:
            return fn(*a)
        except RuntimeError as e:
            transient = any(s in str(e) for s in (" 429", " 500", " 502", " 503", " 504", "timed out", "Connection"))
            if not transient or i == tries - 1:
                raise
            time.sleep(2 * (i + 1) ** 2)


def _is_denied(e: Exception) -> bool:
    s = str(e)
    return "(403)" in s or "accessDenied" in s or "(401)" in s


def _walk(client, token, site_id, path_q: str, rel: str = ".", depth: int = 0) -> list[dict]:
    """Every file under a source folder: {name, rel, size, path_q}. Only listing, never anything else."""
    out = []
    for it in _retry(client.list_folder, token, site_id, path_q):
        if it["is_folder"]:
            if depth < 4:
                sub = it["name"] if rel == "." else f"{rel}/{it['name']}"
                out.extend(_walk(client, token, site_id, f"{path_q}/{quote(it['name'], safe='')}", sub, depth + 1))
        else:
            out.append({"name": it["name"], "rel": rel, "size": it.get("size", 0),
                        "path_q": f"{path_q}/{quote(it['name'], safe='')}"})
    return out


def plan_source(files: list[dict]) -> dict:
    """Which of the source files are filed: the department reports of the final-set folder when there is one, else of the working
    folders. Everything else is held (counted, never filed)."""
    for f in files:
        f["role"] = role(f["rel"])
        f["parsed"] = parse_dept(f["name"])
    primary = "final" if any(f["role"] == "final" for f in files) else "working"
    prim = [f for f in files if f["role"] == primary and f["parsed"]]
    problems = []
    names = collections.Counter(f["name"] for f in prim)
    if any(v > 1 for v in names.values()):
        problems.append("two department files have the same name")
    kinds = collections.Counter((f["parsed"][0], f["parsed"][1]) for f in prim)
    if any(v > 1 for v in kinds.values()):
        problems.append("a department has two reports of the same kind")
    return {"primary": primary, "files": prim, "held": len(files) - len(prim), "problems": problems,
            "codes": sorted({f["parsed"][1] for f in prim})}


def entity_folders(client, token, dest_site_id) -> dict:
    """LIVE listing of the Documents site's root: {code3: [folder names]} for the 'NNN. Name' folders."""
    out: dict = {}
    for e in _retry(client.list_folder, token, dest_site_id, ""):
        if e["is_folder"]:
            m = re.match(r"^(\d{3})\. ", e["name"])
            if m:
                out.setdefault(m.group(1), []).append(e["name"])
    return out


def file_period(org_id: int, period_id: int, user_id: int, client=None) -> dict:
    """Files one pay period's department reports once its hours are Final or it is closed (see the module docstring). Returns counts, and marks the period filed
    (how = 'beacon') only when everything went through. Raises PayrollError for a refusal; nothing is written in that case."""
    client = client or CLIENT
    cfg = CONFIG.get(org_id)
    if not cfg:
        raise PayrollError("refused", "Filing reports from here is not set up for this diocese. Use Reports Posted via Phoenix.")
    period = pt.get_period(org_id, period_id)
    if not pt.settle_ready(period):    # Jay, 2026-10-27: the hours Final (or the period closed); Close is now the step AFTER the reports
        raise PayrollError("refused", "Mark the hours Final first. The reports are filed after payroll has been processed.")
    pfolder = period_folder_name(period)
    pay_date = period["pay_date"]
    host = cfg["host"]
    try:
        token = client.get_access_token(host)
        dest_site = client.get_site_id(token, host, cfg["dest_site"])
    except Exception as e:  # the Documents site itself: a real problem, not an access question
        raise PayrollError("refused", f"Beacon could not reach the Documents site ({str(e)[:160]}). Nothing was filed.")
    try:
        src_site = client.get_site_id(token, host, cfg["source_site"])
    except Exception as e:
        raise PayrollError("refused", NO_ACCESS if _is_denied(e) else
                           f"Beacon could not reach the Accounting site ({str(e)[:160]}). Nothing was filed.")
    src_root_q = _q(cfg["source_root"].format(year=pay_date.year)) + "/" + quote(pay_date.strftime("%Y.%m.%d"), safe="")
    try:
        files = _walk(client, token, src_site, src_root_q)
    except Exception as e:
        raise PayrollError("refused", NO_ACCESS if _is_denied(e) else
                           f"Beacon could not read the Accounting folder ({str(e)[:160]}). Nothing was filed.")
    if not files:
        raise PayrollError("refused", f"There is no folder with files for pay date {pay_date.strftime('%Y.%m.%d')} in Accounting > "
                                      f"{cfg['source_root'].format(year=pay_date.year)} yet. Nothing was filed.")
    plan = plan_source(files)
    if plan["problems"]:
        raise PayrollError("refused", "The pay date folder is not clear enough to file from (" + "; ".join(plan["problems"]) +
                                      "). Nothing was filed.")
    if not plan["files"]:
        raise PayrollError("refused", f"The pay date folder has no department reports in it yet ({len(files)} other file(s)). Nothing was filed.")
    ef = entity_folders(client, token, dest_site)
    missing = [c for c in plan["codes"] if c not in ef]
    ambiguous = [c for c in plan["codes"] if len(ef.get(c, [])) > 1]
    if missing or ambiguous:
        raise PayrollError("refused", "Not every department has exactly one folder on the Documents site "
                                      f"(no folder: {', '.join(missing) or 'none'}; more than one: {', '.join(ambiguous) or 'none'}). Nothing was filed.")

    by_code = collections.defaultdict(list)
    for f in plan["files"]:
        by_code[f["parsed"][1]].append(f)

    def do_code(code):
        c = collections.Counter()
        problems = []
        folder = ef[code][0]
        base = f"{folder}/{cfg['dest_sub']}"
        dest_q = f"{_q(base)}/{quote(pfolder, safe='')}"
        assert dest_q.startswith(_q(f"{folder}/{cfg['dest_sub']}") + "/")  # never anywhere else
        _retry(client.ensure_folder, token, dest_site, _q(base), pfolder)
        have = {i["name"]: i["size"] for i in _retry(client.list_folder, token, dest_site, dest_q) if not i["is_folder"]}
        for f in sorted(by_code[code], key=lambda x: x["name"]):
            if f["name"] in have:
                if have[f["name"]] == f["size"]:
                    c["already_there"] += 1
                else:
                    c["conflict"] += 1
                    problems.append((code, "a different file with this name is already there"))
                continue
            try:
                data = _retry(client.download_bytes, token, src_site, f["path_q"])
                if len(data) != f["size"]:
                    raise RuntimeError("short read")
                res = _retry(client.upload_bytes, token, dest_site, dest_q, f["name"], data, "application/pdf")
                if res.get("size") != len(data):
                    c["error"] += 1
                    problems.append((code, "the copy came out a different size"))
                else:
                    c["copied"] += 1
            except Exception as e:
                c["error"] += 1
                problems.append((code, f"could not copy a file ({str(e)[:80]})"))
        return c, problems

    total = collections.Counter()
    problems = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for c, p in pool.map(do_code, sorted(by_code)):
            total.update(c)
            problems.extend(p)
    res = {"folder": pfolder, "departments": len(by_code), "files": len(plan["files"]), "copied": total["copied"],
           "already_there": total["already_there"], "conflicts": total["conflict"], "errors": total["error"],
           "held": plan["held"], "source": plan["primary"], "problems": problems, "filed": False}
    if not total["conflict"] and not total["error"]:
        note = (f"{len(plan['files'])} reports for {len(by_code)} departments in {pfolder}: {total['copied']} copied, "
                f"{total['already_there']} already there")
        import payroll_closeout as pc  # late: payroll_closeout does not import this module
        pc.mark_reports_filed(org_id, period_id, user_id, "beacon", note=note)
        res["filed"] = True
    return res
