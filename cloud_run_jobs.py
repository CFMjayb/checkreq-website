"""
cloud_run_jobs.py -- start a Cloud Run JOB from Beacon and ask whether it is already running (26-158, 2026-10-12).

First caller: payroll_inbox_run.py ("Review Inbox" on Time Review).  Small and self-contained on purpose: the 26-152
deposit_run_now.py (not shipped yet) has its own copy of the same few lines; when that ships the two can share this file.

Rules (each one is tested):
  * Only a job NAME that looks like a Cloud Run name is ever used (lower-case letters, digits, hyphens).
  * Cloud Run's own text is never passed on: every failure becomes one of three fixed codes
    ('permission' | 'notfound' | 'unavailable').
  * check_start_allowed() refuses a start while an execution is still running, or one was created in the last
    COOLDOWN_SECONDS (fails CLOSED: if the executions cannot be listed nothing is started).
  * Beacon's runtime service account needs, on the job, roles/run.jobsExecutor (start) and roles/run.viewer (list
    executions).  This code does not grant it.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

PROJECT = "cfm-qbo-mcp"
REGION = "us-east1"
COOLDOWN_SECONDS = 120
HTTP_TIMEOUT = 15
EXECUTIONS_PAGE = 5
_JOB_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class CloudRunError(Exception):
    """Carries only a fixed code ('permission' | 'notfound' | 'unavailable'), never Google's text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def valid_job_name(name) -> bool:
    return isinstance(name, str) and bool(_JOB_NAME.match(name))


class CloudRunClient:
    """Cloud Run Admin API v2: list a job's executions and start it, with the runtime service account's own credentials."""

    def __init__(self):
        self._session = None

    def _sess(self):
        if self._session is None:
            import google.auth                                          # noqa: PLC0415 - heavy import, only when a button is used
            from google.auth.transport.requests import AuthorizedSession  # noqa: PLC0415
            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self._session = AuthorizedSession(creds)
        return self._session

    @staticmethod
    def _job_url(job: str) -> str:
        if not valid_job_name(job):
            raise CloudRunError("notfound")
        return f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{job}"

    @staticmethod
    def _raise_for(status: int):
        if status in (401, 403):
            raise CloudRunError("permission")
        if status == 404:
            raise CloudRunError("notfound")
        raise CloudRunError("unavailable")

    def list_executions(self, job: str) -> list[dict]:
        url = self._job_url(job)
        try:
            r = self._sess().get(url + "/executions", params={"pageSize": EXECUTIONS_PAGE}, timeout=HTTP_TIMEOUT)
        except CloudRunError:
            raise
        except Exception as exc:                                        # noqa: BLE001 - any failure is "unavailable", never raw text
            print(f"[cloud_run_jobs] list executions failed: {type(exc).__name__}", flush=True)
            raise CloudRunError("unavailable")
        if r.status_code != 200:
            self._raise_for(r.status_code)
        try:
            return list((r.json() or {}).get("executions") or [])
        except Exception:                                               # noqa: BLE001
            raise CloudRunError("unavailable")

    def run_job(self, job: str, args: list[str] | None = None) -> str:
        """Start the job.  args=None -> exactly as scheduled (no overrides).  Returns the execution's short name ('' if unknown)."""
        url = self._job_url(job)
        body: dict = {}
        if args is not None:
            body = {"overrides": {"containerOverrides": [{"args": list(args)}]}}
        try:
            r = self._sess().post(url + ":run", json=body, timeout=HTTP_TIMEOUT)
        except CloudRunError:
            raise
        except Exception as exc:                                        # noqa: BLE001
            print(f"[cloud_run_jobs] run failed: {type(exc).__name__}", flush=True)
            raise CloudRunError("unavailable")
        if r.status_code not in (200, 201):
            self._raise_for(r.status_code)
        try:
            meta = ((r.json() or {}).get("metadata") or {})
            return str(meta.get("name") or "").rsplit("/", 1)[-1][:80]
        except Exception:                                               # noqa: BLE001
            return ""


_CLIENT: CloudRunClient | None = None


def client() -> CloudRunClient:
    """The one shared client.  Tests replace this function's result by assigning set_client()."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = CloudRunClient()
    return _CLIENT


def set_client(c) -> None:
    global _CLIENT
    _CLIENT = c


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def check_start_allowed(executions: list[dict], *, now: datetime | None = None) -> str | None:
    """-> None when the job may be started, else 'busy' | 'cooldown'.  A job still running (no completion time, not
    deleted) is busy; one created less than COOLDOWN_SECONDS ago is in cooldown even if it already finished."""
    now = now or datetime.now(timezone.utc)
    for ex in executions or []:
        if isinstance(ex, dict) and not ex.get("completionTime") and not ex.get("deleteTime"):
            return "busy"
    for ex in executions or []:
        created = _parse_ts(ex.get("createTime")) if isinstance(ex, dict) else None
        if created is not None and (now - created).total_seconds() < COOLDOWN_SECONDS:
            return "cooldown"
    return None
