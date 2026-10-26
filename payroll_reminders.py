"""
payroll_reminders.py -- Time Review > Actions > Send reminder emails (26-158 / 26-129, 2026-10-26).

Jay: "a new feature in the time review screen under Action to 'send reminder emails' - this will bring up a screen of all
incomplete departments that you can then check which ones you want to send a reminder to, along with the email address(es) to send
them to - this should be a list you are gathering from each email that comes in."

    GET  /admin/timekeeping/status/{period}/reminders        the screen
    POST /admin/timekeeping/status/{period}/reminders/send   send one email per ticked department

WHO IS LISTED: the departments whose hours have not come in, using the Time Review board's own rule (payroll_totals.classify_parish
through payroll_screens.board_rows), so the two screens cannot disagree: "Missing hours" (a department that normally sends every
period or most periods and has sent nothing) and "Not started". A department whose hours are in (needs review, ready, approved, only
standing hours) or that is not expected to send is left out.

WHERE THE ADDRESSES COME FROM: the people who emailed the department's hours. The payroll inbox reader saves each sender on the
department (portal.parishes.contacts, role time_submitter, see payroll_totals.add_learned_sender). Those addresses are ticked for you.
The department's payroll contacts (role payroll, the payroll report recipients) are listed UNticked as extras, and one more address
can be typed on the row. The server accepts only addresses it offered for that department, or a valid one typed on the row, never an
address that only the browser claims.

THE MESSAGE: one email per department, to all of its ticked addresses, from the email server's default sender, with Reply-To set to
the payroll mailbox so an answer lands in the inbox the reader scans. {department} in the subject or body becomes the department's
name. No square brackets in a subject (the mail system silently drops mail that has them).

THE LOG: portal.time_reminder_log (migration 104) records each send and each failed send. It drives "Reminded ..." and starts a row
unticked when it was reminded in the last day. Sending is switched off where the table does not exist yet.

Dev: email_client sends everything to the test address with a DEV subject, never to a real church.
Gate: hr_admin or beacon_admin of the current diocese (payroll_screens._require_hr_admin). Hours and addresses only, never pay.
"""
from __future__ import annotations

import datetime as dt
import html
import re
from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from psycopg import errors as pgerr
from starlette.concurrency import run_in_threadpool

import app_settings
import db
import email_client
import org_time
import payroll_screens
import payroll_totals as pt
import timekeeping_status

router = APIRouter()

_current_user = None
_current_org = None
_render = None
_gate = None  # payroll_screens._require_hr_admin

LOG_TABLE = "portal.time_reminder_log"
RECENT_HOURS = 24                      # a department reminded within this many hours starts unticked
MAX_ADDRESSES_PER_DEPARTMENT = 8
MAX_SUBJECT = 200
MAX_BODY = 3000
SETTING_KEY = "payroll_reminder_reply_to"          # app_settings key "<key>:<org id>" overrides the default below
DEFAULT_REPLY_TO = {"DME": "payroll@episcopalmaine.net"}   # the payroll mailbox the reader scans (26-158)
STATUS_TEXT = {"miss": "Missing hours", "wait": "Not started"}

_EMAIL_RX = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")


def register(app, *, current_user, current_org, render, gate) -> None:
    global _current_user, _current_org, _render, _gate
    _current_user, _current_org, _render, _gate = current_user, current_org, render, gate
    app.include_router(router)


# ------------------------------------------------------------------ small rules (no database)

def parse_addresses(text: str | None) -> tuple[list[str], list[str]]:
    """(good, bad): every address in text (separated by commas, semicolons or spaces), lower case, no repeats; and the pieces
    that do not look like one email address."""
    good: list[str] = []
    bad: list[str] = []
    for token in re.split(r"[,;\s]+", text or ""):
        token = token.strip().strip("<>").lower()
        if not token:
            continue
        if len(token) <= 120 and _EMAIL_RX.match(token):
            if token not in good:
                good.append(token)
        else:
            bad.append(token[:60])
    return good, bad


def body_to_html(text: str) -> str:
    """Plain text to simple HTML: blank lines separate paragraphs, a single line break stays a break. Everything is escaped."""
    paragraphs = [p for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    return "".join("<p>" + "<br>".join(html.escape(line) for line in p.splitlines()) + "</p>" for p in paragraphs)


def _fmt_date(value) -> str:
    return value.strftime("%m/%d/%y") if value and hasattr(value, "strftime") else (value or "")


ORIGINAL_SUBJECT = "Payroll hours reminder: {department}"
ORIGINAL_BODY = (
    "Hello,\n\n"
    "We have not yet received the payroll hours for {department} for the pay period {period}. They are due by {deadline}.\n\n"
    "Please reply to this email with the hours for each hourly employee (regular, overtime, sick, vacation and holiday), "
    "or tell us if there are no hours to report this period.\n\n"
    "Thank you,\n{org} payroll"
)
SETTING_SUBJECT = "payroll_reminder_subject"       # app_settings key "<key>:<org id>": the diocese's standard subject
SETTING_BODY = "payroll_reminder_body"             # ... and its standard message, saved WITH {period} and {deadline} in it


def period_text(period: dict) -> str:
    what = f"{_fmt_date(period.get('period_start'))} to {_fmt_date(period.get('period_end'))}"
    return f"{period['label']} ({what})" if period.get("label") else what


def deadline_text(period: dict) -> str:
    return _fmt_date(period.get("submission_deadline")) or "the payroll deadline"


def expand(text: str, period: dict) -> str:
    """{period} and {deadline} become this pay period's text. {department} is left for each department's own email."""
    return (text or "").replace("{period}", period_text(period)).replace("{deadline}", deadline_text(period))


def collapse(text: str, period: dict) -> str:
    """The reverse, for saving: this pay period's own dates go back to {period} and {deadline}, so a saved standard message is right
    for every later pay period instead of keeping this one's dates."""
    out = (text or "").replace(period_text(period), "{period}")
    due = _fmt_date(period.get("submission_deadline"))
    return out.replace(due, "{deadline}") if due else out


def original_message(org: dict, period: dict) -> dict:
    return {"subject": expand(ORIGINAL_SUBJECT, period),
            "body": expand(ORIGINAL_BODY.replace("{org}", org.get("name") or "The diocese"), period)}


def saved_message(org: dict) -> dict | None:
    """The diocese's saved standard message (with its placeholders), or None when nobody saved one."""
    subject = app_settings.get_setting(f"{SETTING_SUBJECT}:{org['id']}")
    body = app_settings.get_setting(f"{SETTING_BODY}:{org['id']}")
    return {"subject": subject, "body": body} if subject and body else None


def default_message(org: dict, period: dict) -> dict:
    """The starting subject and body for this pay period: the diocese's saved standard message if there is one, else the original
    wording. {department} is replaced for each department when the emails are made."""
    saved = saved_message(org)
    if saved:
        return {"subject": expand(saved["subject"], period), "body": expand(saved["body"], period), "saved": True}
    return {**original_message(org, period), "saved": False}


def save_standard_message(org: dict, user: dict, period: dict, subject: str, body: str) -> None:
    """Keeps what was written as the standard message from now on (this pay period's dates become placeholders first)."""
    app_settings.set_setting(f"{SETTING_SUBJECT}:{org['id']}", collapse(subject, period), user["id"])
    app_settings.set_setting(f"{SETTING_BODY}:{org['id']}", collapse(body, period), user["id"])


def clear_standard_message(org: dict) -> None:
    db.execute("DELETE FROM checkreq.app_settings WHERE key = ANY(%s)",
               ([f"{SETTING_SUBJECT}:{org['id']}", f"{SETTING_BODY}:{org['id']}"],))


def reply_to_for(org: dict) -> str:
    """The mailbox an answer should reach: a per-diocese setting if one is saved, else the known default, else nothing."""
    saved = app_settings.get_setting(f"{SETTING_KEY}:{org['id']}")
    good, _bad = parse_addresses(saved)
    if good:
        return good[0]
    return DEFAULT_REPLY_TO.get((org.get("code") or "").upper(), "")


def label_of(row: dict) -> str:
    code = row.get("parish_code") or row.get("code") or ""
    name = row.get("short_name") or row.get("parish_name") or row.get("name") or ""
    return f"{code} {name}".strip()


# ------------------------------------------------------------------ who is missing, and who to write to

def candidates(org_id: int, period: dict) -> list[dict]:
    """Departments whose hours have not come in, by the board's own rule: Missing hours (amber) and Not started."""
    rows = timekeeping_status.list_parish_status_for_period(org_id, period["id"])
    payroll_screens.board_rows(org_id, period, rows)
    return [r for r in rows if r.get("state") in STATUS_TEXT]


def department_contacts(org_id: int, parish_ids: list[int], mailbox: str = "") -> dict[int, dict]:
    """{parish_id: {"senders": [...], "recipients": [...]}}. senders = the people who emailed the department's hours (learned by the
    payroll inbox reader), newest first; recipients = its payroll contacts. An address is listed once (as a sender if it is both), and
    never the reply mailbox itself or a system address."""
    out: dict[int, dict] = {pid: {"senders": [], "recipients": []} for pid in parish_ids}
    if not parish_ids:
        return out
    for r in db.query("SELECT id, contacts FROM portal.parishes WHERE org_id = %s AND id = ANY(%s)", (org_id, list(parish_ids))):
        entries, _envelope = pt.split_contacts(r["contacts"])
        seen: set[str] = set()
        for role, key in (("time_submitter", "senders"), ("payroll", "recipients")):
            for c in entries:
                if not isinstance(c, dict) or c.get("role") != role:
                    continue
                emails, _bad = parse_addresses(c.get("email") or "")
                for em in emails:
                    if em in seen or em == mailbox or em.endswith("@system.invalid"):
                        continue
                    seen.add(em)
                    out[r["id"]][key].append({"email": em, "name": c.get("name") or "", "last_seen": str(c.get("last_seen") or "")})
        out[r["id"]]["senders"].sort(key=lambda a: a["last_seen"], reverse=True)
    return out


def reminder_history(org_id: int, period_id: int) -> tuple[bool, dict[int, dict]]:
    """(the log exists here, {parish_id: {"sent": newest sent row, "failed": newest failed row}})."""
    try:
        rows = db.query(
            f"SELECT DISTINCT ON (parish_id, action) parish_id, action, recipients, error, created_at FROM {LOG_TABLE} "
            "WHERE org_id = %s AND period_id = %s ORDER BY parish_id, action, created_at DESC", (org_id, period_id))
    except (pgerr.UndefinedTable, pgerr.InsufficientPrivilege):
        return False, {}
    out: dict[int, dict] = {}
    for r in rows:
        out.setdefault(r["parish_id"], {})[r["action"]] = r
    return True, out


def _write_log(org_id: int, period_id: int, parish_id: int, action: str, recipients: str | None, error: str | None,
               user_id: int) -> bool:
    try:
        db.execute(
            f"INSERT INTO {LOG_TABLE} (org_id, period_id, parish_id, action, recipients, error, by_user_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (org_id, period_id, parish_id, action, (recipients or None) and recipients[:600], (error or None) and error[:600], user_id))
        return True
    except (pgerr.UndefinedTable, pgerr.InsufficientPrivilege):
        return False


# ------------------------------------------------------------------ the screen

def page_context(org: dict, period: dict, form=None, query=None, error: str | None = None) -> dict:
    """Everything the screen shows. form (the posted FormData) keeps what the person had ticked and typed after a refused send."""
    mailbox = reply_to_for(org)
    cands = candidates(org["id"], period)
    contacts = department_contacts(org["id"], [c["parish_id"] for c in cands], mailbox)
    log_ok, history = reminder_history(org["id"], period["id"])
    zone = org_time.zone_name_for_org(org["id"])
    now = dt.datetime.now(dt.timezone.utc)
    rows = []
    for c in cands:
        pid = c["parish_id"]
        senders, recipients = contacts[pid]["senders"], contacts[pid]["recipients"]
        h = history.get(pid, {})
        sent, failed = h.get("sent"), h.get("failed")
        recent = bool(sent and (now - sent["created_at"]) < dt.timedelta(hours=RECENT_HOURS))
        checked = c["state"] == "miss" and bool(senders) and not recent
        ticked = {a["email"] for a in senders}
        extra, remember = "", False
        if form is not None:
            checked = f"send_{pid}" in form
            ticked = {str(a).strip().lower() for a in form.getlist(f"to_{pid}")}
            extra = str(form.get(f"extra_{pid}") or "")
            remember = f"remember_{pid}" in form
        rows.append({
            "parish_id": pid, "label": label_of(c), "name": c["parish_name"], "state": c["state"], "status": STATUS_TEXT[c["state"]],
            "senders": senders, "recipients": recipients, "ticked": ticked, "extra": extra, "remember": remember, "checked": checked,
            "reminded": org_time.format_local(sent["created_at"], zone) if sent else "",
            "reminded_to": (sent or {}).get("recipients") or "", "recent": recent,
            "failed_why": (failed["error"] or "The last send failed.") if failed and (not sent or failed["created_at"] > sent["created_at"]) else "",
        })
    message = default_message(org, period)
    if form is not None:
        message = {**message, "subject": str(form.get("subject") or ""), "body": str(form.get("body") or "")}
    q = query or {}
    summary = None
    if str(q.get("sent", "")).isdigit() or str(q.get("failed", "")).isdigit():
        summary = {"sent": int(q.get("sent") or 0), "failed": int(q.get("failed") or 0), "skipped": int(q.get("skipped") or 0),
                   "failed_for": str(q.get("failed_for") or "")[:300], "remembered": int(q.get("remembered") or 0) if str(q.get("remembered", "")).isdigit() else 0,
                   "saved_message": q.get("saved_message") == "1"}
    dev = getattr(email_client, "_BEACON_ENV", "dev") != "prod"
    return {
        "current_org": org, "period": period, "rows": rows, "subject": message["subject"], "body": message["body"],
        "message_saved": message.get("saved", False), "save_ticked": form is not None and "save_message" in form,
        "message_reset": q.get("msg") == "reset",
        "reply_to": mailbox, "log_ok": log_ok, "can_send": bool(log_ok and mailbox and rows), "error": error, "summary": summary,
        "dev_mode": dev, "test_address": app_settings.get_setting("email_test_mode_address") if dev else None,
        "recent_hours": RECENT_HOURS, "due": _fmt_date(period.get("submission_deadline")),
    }


def _period(org_id: int, period_id: int) -> dict | None:
    return db.query_one("SELECT * FROM portal.payroll_periods WHERE id = %s AND org_id = %s", (period_id, org_id))


def _board_back(period_id: int, err: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/timekeeping/status/{period_id}?error=" + quote_plus(err), status_code=303)


@router.get("/admin/timekeeping/status/{period_id}/reminders", response_class=HTMLResponse)
def reminders_page(period_id: int, request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    period = _period(org["id"], period_id)
    if not period:
        return RedirectResponse("/admin/timekeeping/periods?error=That+payroll+period+was+not+found+for+this+diocese.", status_code=303)
    if period["status"] != "open":
        return _board_back(period_id, "Reminders can only be sent while the pay period is Open.")
    return _render(request, "timekeeping_reminders.html", user, page_context(org, period, None, request.query_params))


# ------------------------------------------------------------------ sending

def send_reminders(org: dict, user: dict, period: dict, form) -> dict:
    """Checks everything first and sends nothing if anything is wrong, then sends one email per ticked department.
    Returns {"error": text or None, "sent": [labels], "failed": [(label, why)], "skipped": [labels], "remembered": [(label, address)],
    "saved_message": bool}. Two things are kept only when the send itself is going ahead: the wording, when "Save as the standard
    message" is ticked, and an address typed on a row with "Remember" ticked, once that department's email was sent."""
    out: dict = {"error": None, "sent": [], "failed": [], "skipped": [], "remembered": [], "saved_message": False}

    def refuse(text: str) -> dict:
        out["error"] = text
        return out

    if period["status"] != "open":
        return refuse("Reminders can only be sent while the pay period is Open.")
    log_ok, _history = reminder_history(org["id"], period["id"])
    if not log_ok:
        return refuse("Sending is switched off until the reminder log (migration 104) is set up on this site.")
    mailbox = reply_to_for(org)
    if not mailbox:
        return refuse("No reply address is set for this diocese, so a parish could not answer. Nothing was sent.")
    posted_subject = " ".join(str(form.get("subject") or "").split())   # one line: a line break can never ride in a subject
    posted_body = str(form.get("body") or "").replace("\r\n", "\n").strip()
    subject_t, body_t = expand(posted_subject, period), expand(posted_body, period)   # {period} and {deadline}: this pay period's text
    if not subject_t or not body_t:
        return refuse("Write a subject and a message first.")
    if len(subject_t) > MAX_SUBJECT or len(body_t) > MAX_BODY:
        return refuse(f"The subject can be up to {MAX_SUBJECT} characters and the message up to {MAX_BODY}.")
    if "[" in subject_t or "]" in subject_t:
        return refuse("Please leave square brackets out of the subject: the mail system silently drops mail that has them.")

    selected = sorted({int(k[5:]) for k in form.keys() if k.startswith("send_") and k[5:].isdigit()})
    if not selected:
        return refuse("Tick at least one department.")

    cands = {c["parish_id"]: c for c in candidates(org["id"], period)}
    contacts = department_contacts(org["id"], list(cands), mailbox)
    plan: list[tuple[dict, list[str], list[str]]] = []
    for pid in selected:
        c = cands.get(pid)
        if c is None:
            p = db.query_one("SELECT code, name, short_name FROM portal.parishes WHERE id = %s AND org_id = %s", (pid, org["id"]))
            if p:  # hours came in since the page was opened: not an error, just not reminded
                out["skipped"].append(label_of(p))
            continue
        label = label_of(c)
        allowed = {a["email"] for a in contacts[pid]["senders"] + contacts[pid]["recipients"]}
        chosen: list[str] = []
        for a in form.getlist(f"to_{pid}"):
            a = str(a).strip().lower()
            if a not in allowed:
                return refuse(f"{label}: {a[:60]} is not one of the addresses on file for this department. "
                              "Open the screen again and tick from the list, or type the address in the box on the row.")
            if a not in chosen:
                chosen.append(a)
        extra, bad = parse_addresses(form.get(f"extra_{pid}"))
        if bad:
            return refuse(f"{label}: \"{bad[0]}\" does not look like an email address.")
        for a in extra:
            if a != mailbox and a not in chosen:
                chosen.append(a)
        if not chosen:
            return refuse(f"{label}: tick or type at least one address, or untick the department.")
        if len(chosen) > MAX_ADDRESSES_PER_DEPARTMENT:
            return refuse(f"{label}: up to {MAX_ADDRESSES_PER_DEPARTMENT} addresses per department.")
        subject = subject_t.replace("{department}", c["parish_name"])
        if len(subject) > MAX_SUBJECT or "[" in subject or "]" in subject:
            return refuse(f"{label}: the subject is too long or has a square bracket once the department name is added.")
        # "Remember": only an address typed on the row that the department did not already have on file
        to_remember = [a for a in extra if f"remember_{pid}" in form and a != mailbox and a not in allowed]
        plan.append((c, chosen, to_remember))
    if not plan:
        return refuse("None of the ticked departments is missing hours any more, so nothing was sent.")

    if "save_message" in form:   # everything checked out and the send goes ahead: keep this wording as the standard message
        save_standard_message(org, user, period, posted_subject, posted_body)
        out["saved_message"] = True

    for c, chosen, to_remember in plan:
        label, to = label_of(c), ", ".join(chosen)
        text = body_t.replace("{department}", c["parish_name"])
        result = email_client.send_email(
            to=to, subject=subject_t.replace("{department}", c["parish_name"]), body_html=body_to_html(text), body_text=text,
            sender="", reply_to=mailbox, timeout=30)
        if result.get("status") == "sent":
            _write_log(org["id"], period["id"], c["parish_id"], "sent", to, None, user["id"])
            out["sent"].append(label)
            for a in to_remember:   # the email went out, so the address is worth keeping: a sender of this department (added by hand)
                try:
                    pt.add_learned_sender(org["id"], parish_code=c["parish_code"], email=a, source="manual")
                    out["remembered"].append((label, a))
                except pt.PayrollError:
                    pass
        else:
            why = f"The email was not sent: {result.get('error') or 'no answer from the email server'}"
            _write_log(org["id"], period["id"], c["parish_id"], "failed", to, why, user["id"])
            out["failed"].append((label, why))
    return out


@router.post("/admin/timekeeping/status/{period_id}/reminders/send")
async def reminders_send(period_id: int, request: Request):
    user, org, err = _gate(request)
    if err:
        return err
    period = _period(org["id"], period_id)
    if not period:
        return RedirectResponse("/admin/timekeeping/periods?error=That+payroll+period+was+not+found+for+this+diocese.", status_code=303)
    if period["status"] != "open":
        return _board_back(period_id, "Reminders can only be sent while the pay period is Open.")
    form = await request.form()
    result = await run_in_threadpool(send_reminders, org, user, period, form)
    if result["error"]:
        ctx = await run_in_threadpool(page_context, org, period, form, None, result["error"])
        return _render(request, "timekeeping_reminders.html", user, ctx)
    failed_for = ", ".join(label for label, _why in result["failed"])
    return RedirectResponse(
        f"/admin/timekeeping/status/{period_id}/reminders?sent={len(result['sent'])}&failed={len(result['failed'])}"
        f"&skipped={len(result['skipped'])}&remembered={len(result['remembered'])}&saved_message={1 if result['saved_message'] else 0}"
        f"&failed_for={quote_plus(failed_for[:300])}", status_code=303)


@router.post("/admin/timekeeping/status/{period_id}/reminders/reset-message")
def reminders_reset_message(period_id: int, request: Request):
    """Back to the original wording: forgets the saved standard message (the button posts the page's form, so the form token is there)."""
    user, org, err = _gate(request)
    if err:
        return err
    period = _period(org["id"], period_id)
    if not period:
        return RedirectResponse("/admin/timekeeping/periods?error=That+payroll+period+was+not+found+for+this+diocese.", status_code=303)
    if period["status"] != "open":
        return _board_back(period_id, "Reminders can only be sent while the pay period is Open.")
    clear_standard_message(org)
    return RedirectResponse(f"/admin/timekeeping/status/{period_id}/reminders?msg=reset", status_code=303)
