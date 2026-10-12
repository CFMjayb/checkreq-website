"""
payroll_inbox_run.py -- 26-158 / 26-129: the payroll email reader's last run ("Last updated") and the
"Review Inbox" action on Time Review (Jay, 2026-10-12).

Two jobs, both small:

  1. STATUS.  The reader (today a scheduled task on a diocesan PC, soon a Cloud Run job) tells Beacon when a review
     started, finished or failed and what it did.  Beacon keeps ONE small JSON value per diocese in
     checkreq.app_settings (key payroll_import:<org id>; no migration, the table exists in both environments).
     Time Review shows it as "Last updated: MM/DD HH:MM" at the bottom right of the locked frame.
     Hours and counts only; no message text, no names.

  2. REVIEW INBOX.  An HR Admin starts a review of the payroll mailbox by hand.  Beacon starts the Cloud Run job
     named in checkreq.app_settings key payroll_inbox_job:<org id>.  No job named = "not connected": nothing is
     started and the screen says so (the reader on the diocese PC keeps checking by itself).

Rules (each is tested):
  * Only HR Admin / Beacon Admin of the diocese can start a review (the route's own gate); the browser sends nothing
    but a button press, never a job name or an argument.
  * A review that is running (reported running < STALE_RUNNING_SECONDS ago) or a job execution still running in
    Cloud Run, or one started in the last 2 minutes, is not started again.  If Cloud Run cannot be asked, nothing
    is started (fails closed).
  * Every start writes one JSON line to the application log naming WHO pressed the button.
  * Cloud Run's own text is never shown: errors become one of a few fixed messages.
  * The reader's report is checked: a known state, real dates, whole-number counts only, nothing in the future.

New file per the standing main.py rule; payroll_api.py and payroll_screens.py gain only calls to it.
"""
from __future__ import annotations

import datetime as dt
import json

import app_settings
import cloud_run_jobs
import org_time
import payroll_totals as pt

STATUS_KEY = "payroll_import:{org}"
JOB_KEY = "payroll_inbox_job:{org}"
STATES = ("running", "done", "failed")
SOURCES = ("pc", "cloud")
COUNT_KEYS = ("messages", "lines_recorded", "recorded", "review", "noise", "closed_period", "error")
STALE_RUNNING_SECONDS = 30 * 60
FUTURE_SLACK_SECONDS = 5 * 60

MESSAGES = {
    "started": "Inbox review started. It takes a few minutes. Refresh the page to see the new Last updated time.",
    "busy": "A review is already running. Wait for it to finish, then try again.",
    "cooldown": "A review was started a moment ago. Wait a couple of minutes before starting another.",
    "not_connected": "The cloud reader is not connected yet, so nothing was started. The reader on the diocese PC "
                     "still checks the inbox by itself (every 30 minutes, every 5 minutes on Mondays 9 to 4).",
    "permission": "Beacon is not allowed to start the review job yet (a one-time Cloud Run permission is needed). Nothing was started.",
    "notfound": "The review job was not found in Cloud Run, so nothing was started.",
    "unavailable": "Cloud Run could not be reached or did not answer as expected, so nothing was started. Please try again in a few minutes.",
}
OK_CODES = ("started",)


# ----------------------------------------------------------------------------- time helpers
def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _when(v) -> dt.datetime | None:
    if not v:
        return None
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    try:
        d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _fmt(d: dt.datetime | None, zone: str, pattern: str) -> str | None:
    """The moment in the diocese's own time zone, 24-hour, or None."""
    if not d:
        return None
    from zoneinfo import ZoneInfo
    z = zone if org_time.is_valid_zone(zone) else org_time.DEFAULT_ZONE
    return d.astimezone(ZoneInfo(z)).strftime(pattern)


# ----------------------------------------------------------------------------- stored status
def get_status(org_id: int) -> dict:
    """{state, started_at, finished_at, failed_at, source, counts}: datetimes (UTC) or None; state '' when never reported."""
    try:
        raw = json.loads(app_settings.get_setting(STATUS_KEY.format(org=int(org_id))) or "{}")
    except ValueError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    counts = {}
    if isinstance(raw.get("counts"), dict):
        counts = {k: int(raw["counts"][k]) for k in COUNT_KEYS if isinstance(raw["counts"].get(k), (int, float))}
    state = raw.get("state") if raw.get("state") in STATES else ""
    return {"state": state, "started_at": _when(raw.get("started_at")), "finished_at": _when(raw.get("finished_at")),
            "failed_at": _when(raw.get("failed_at")), "source": raw.get("source") if raw.get("source") in SOURCES else "",
            "counts": counts}


def _save(org_id: int, st: dict, user_id: int | None = None) -> None:
    def iso(d):
        return d.isoformat() if d else None
    app_settings.set_setting(STATUS_KEY.format(org=int(org_id)), json.dumps({
        "state": st["state"], "started_at": iso(st["started_at"]), "finished_at": iso(st["finished_at"]),
        "failed_at": iso(st["failed_at"]), "source": st["source"], "counts": st["counts"]}), user_id)


def record_report(org_id: int, *, state, started_at=None, finished_at=None, counts=None, source=None) -> dict:
    """The reader reports.  running needs started_at; done needs started_at and finished_at; failed needs started_at
    (finished_at, when given, is when it failed).  'Last updated' only ever moves on a DONE report."""
    if state not in STATES:
        raise pt.PayrollError("invalid", "state must be running, done or failed.")
    started, finished = _when(started_at), _when(finished_at)
    if not started:
        raise pt.PayrollError("invalid", "started_at must be a date and time like 2026-10-15T09:12:00-04:00.")
    if state == "done" and not finished:
        raise pt.PayrollError("invalid", "finished_at must be a date and time like 2026-10-15T09:12:00-04:00.")
    limit = _utcnow() + dt.timedelta(seconds=FUTURE_SLACK_SECONDS)
    for d in (started, finished):
        if d and d > limit:
            raise pt.PayrollError("invalid", "A time in the report is in the future.")
    if finished and finished < started:
        raise pt.PayrollError("invalid", "finished_at is before started_at.")
    if source is not None and source not in SOURCES:
        raise pt.PayrollError("invalid", "source must be pc or cloud.")
    st = get_status(org_id)
    if state == "running":
        st.update(state="running", started_at=started)
    elif state == "done":
        # an older finished report never moves the time backwards
        if st["finished_at"] and finished < st["finished_at"]:
            return {"action": "ignored_older"}
        st.update(state="done", started_at=started, finished_at=finished,
                  counts={k: int(counts[k]) for k in COUNT_KEYS if isinstance(counts, dict) and isinstance(counts.get(k), (int, float))})
    else:
        st.update(state="failed", started_at=started, failed_at=finished or _utcnow())
    if source:
        st["source"] = source
    _save(org_id, st)
    return {"action": "recorded", "state": state}


# ----------------------------------------------------------------------------- what the screen shows
def job_name(org_id: int) -> str:
    """The Cloud Run job to start for this diocese, or '' when none is connected (or the setting is not a real job name)."""
    name = (app_settings.get_setting(JOB_KEY.format(org=int(org_id)), "") or "").strip()
    return name if cloud_run_jobs.valid_job_name(name) else ""


def is_running(st: dict, now: dt.datetime | None = None) -> bool:
    now = now or _utcnow()
    return bool(st["state"] == "running" and st["started_at"]
                and (now - st["started_at"]).total_seconds() < STALE_RUNNING_SECONDS)


def summary(org_id: int, now: dt.datetime | None = None) -> dict:
    """For Time Review: last updated MM/DD HH:MM, whether a review runs now, whether a later review failed, whether the
    Review Inbox action is connected."""
    st = get_status(org_id)
    zone = org_time.zone_name_for_org(org_id)
    c = st["counts"]
    detail = None
    if st["finished_at"] and c:
        detail = f"{c.get('messages', 0)} messages, {c.get('lines_recorded', 0)} hour lines, {c.get('review', 0)} to a person"
    running = is_running(st, now)
    failed = bool(st["state"] == "failed" and st["failed_at"] and (not st["finished_at"] or st["failed_at"] > st["finished_at"]))
    return {"last": _fmt(st["finished_at"], zone, "%m/%d %H:%M"), "detail": detail,
            "running": running, "running_since": _fmt(st["started_at"], zone, "%H:%M") if running else None,
            "failed": failed, "failed_at": _fmt(st["failed_at"], zone, "%m/%d %H:%M") if failed else None,
            "connected": bool(job_name(org_id))}


# ----------------------------------------------------------------------------- Review Inbox
def _log(user: dict, org: dict, outcome: str, execution: str = "") -> None:
    """One JSON line in the application log: WHO pressed the button (Cloud Run only sees Beacon's service account)."""
    print(json.dumps({"event": "beacon_payroll_inbox_review", "user_id": user.get("id"), "user_email": user.get("email"),
                      "entity": org.get("code"), "job": job_name(org["id"]), "outcome": outcome, "execution": execution}), flush=True)


def start_review(org: dict, user: dict) -> str:
    """-> a MESSAGES code.  'started' only when the job really was started."""
    org_id = int(org["id"])
    job = job_name(org_id)
    if not job:
        _log(user, org, "not_connected")
        return "not_connected"
    if is_running(get_status(org_id)):
        _log(user, org, "busy")
        return "busy"
    c = cloud_run_jobs.client()
    try:
        refusal = cloud_run_jobs.check_start_allowed(c.list_executions(job))
        if refusal:
            _log(user, org, refusal)
            return refusal
        execution = c.run_job(job, None)
    except cloud_run_jobs.CloudRunError as exc:
        code = exc.code if exc.code in MESSAGES else "unavailable"
        _log(user, org, code)
        return code
    except Exception as exc:                                            # noqa: BLE001 - never let a Cloud Run problem become an error page
        print(f"[payroll_inbox_run] unexpected {type(exc).__name__}", flush=True)
        _log(user, org, "unavailable")
        return "unavailable"
    st = get_status(org_id)
    st.update(state="running", started_at=_utcnow(), source="cloud")
    try:
        _save(org_id, st, user.get("id"))
    except Exception as exc:                                            # noqa: BLE001 - the job IS running; a saved flag is a nicety
        print(f"[payroll_inbox_run] could not save running flag: {type(exc).__name__}", flush=True)
    _log(user, org, "started", execution)
    return "started"
