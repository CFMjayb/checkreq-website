"""
formstack_documents_client.py -- 26-129 SMA letters (plan revision 12): the Formstack Documents (formerly
WebMerge) REST client. Creates and updates the letter templates and merges one parish's data into one PDF.

API: https://www.webmerge.me/api/ with HTTP Basic auth (the Documents key and secret). A merge is a POST to
the document's own merge URL (it carries its own key) and returns the PDF.

THE ALLOWANCE (Starter plan, $1,800 a year, 2026-10-08): 150 real merges and 150 test merges per month, 10 active
documents. EVERY merged document counts one, whether merged alone or through a Data Route (a 3-document route
used 3 merges, tested 2026-10-08). `?test=1` merges count against the TEST allowance, not the real one.
ensure_allowance() reads the account's own counters and refuses a run that would go over, so a run can never
quietly spend the month's merges.

Credentials: Secret Manager (project cfm-qbo-mcp). The names the plan wants are formstack-documents-key and
formstack-documents-secret. Until the key is regenerated and re-saved under those names (plan open item 9), the
original names formstack-sign-client-id and formstack-sign-client-secret are used as a fallback. Nothing here ever
prints, logs or returns a credential, the merge URL's key, or a delivery's settings.

Network and Secret Manager access happen only inside functions, so importing this module touches nothing.
"""
from __future__ import annotations

import base64
import os
import threading
import time
from urllib.parse import urlsplit

import requests

API = "https://www.webmerge.me/api/"
_SECRET_PROJECT = os.environ.get("GCP_PROJECT", "cfm-qbo-mcp")
_SECRET_NAMES = (("formstack-documents-key", "formstack-documents-secret"),
                 ("formstack-sign-client-id", "formstack-sign-client-secret"))
_TIMEOUT = 60
_auth_cache: tuple[str, str] | None = None
_lock = threading.Lock()


class FormstackError(RuntimeError):
    """A Formstack problem in words an admin can act on. Never contains a credential."""


def _secret(name: str) -> str:
    from google.cloud import secretmanager
    client = secretmanager.SecretManagerServiceClient()
    path = f"projects/{_SECRET_PROJECT}/secrets/{name}/versions/latest"
    return client.access_secret_version(name=path).payload.data.decode("utf-8").strip()


def _credentials() -> tuple[str, str]:
    global _auth_cache
    env_key, env_secret = os.environ.get("FORMSTACK_DOCS_KEY", "").strip(), os.environ.get("FORMSTACK_DOCS_SECRET", "").strip()
    if env_key and env_secret:
        return env_key, env_secret
    with _lock:
        if _auth_cache is None:
            last = None
            for key_name, secret_name in _SECRET_NAMES:
                try:
                    _auth_cache = (_secret(key_name), _secret(secret_name))
                    break
                except Exception as exc:   # not there, or this runtime may not read it
                    last = type(exc).__name__
            if _auth_cache is None:
                raise FormstackError("Beacon cannot read the Formstack Documents credentials from Secret Manager "
                                     f"({last}). The runtime account needs access to them.")
        return _auth_cache


def reset_credentials_cache() -> None:
    global _auth_cache
    with _lock:
        _auth_cache = None


def _request(method: str, path: str, **kw):
    try:
        resp = requests.request(method, API + path, auth=_credentials(), timeout=_TIMEOUT, **kw)
    except requests.RequestException as exc:
        raise FormstackError(f"Could not reach Formstack Documents ({type(exc).__name__}).")
    if resp.status_code == 401:
        reset_credentials_cache()          # a rotated key must be re-read next time, not kept for the life of the instance
        raise FormstackError("Formstack Documents rejected Beacon's credentials.")
    if resp.status_code >= 400:
        raise FormstackError(f"Formstack Documents answered {resp.status_code} for {method} {path.split('/')[0]}.")
    try:
        return resp.json()
    except ValueError:
        return resp.text


# ---------------------------------------------------------------------------------------------
# Allowance
# ---------------------------------------------------------------------------------------------
def account() -> dict:
    """The account's own counters: real and test merges used, their limits, the reset time, document limits."""
    a = _request("GET", "account")
    if not isinstance(a, dict) or "max_merges" not in a:
        raise FormstackError("Formstack Documents returned an unexpected account response.")
    return a


def allowance(test: bool) -> dict:
    a = account()
    used = int(a["test_merge_count" if test else "merge_count"])
    limit = int(a["max_test_merges" if test else "max_merges"])
    return {"used": used, "limit": limit, "remaining": max(limit - used, 0),
            "resets_at_epoch": int(a.get("merge_next_reset") or 0)}


def ensure_allowance(needed: int, test: bool) -> dict:
    """Raise unless `needed` more merges fit in the month's allowance. Returns the allowance."""
    al = allowance(test)
    if needed > al["remaining"]:
        kind = "test" if test else "real"
        raise FormstackError(f"This needs {needed} {kind} merges but only {al['remaining']} are left this month "
                             f"({al['used']} of {al['limit']} used).")
    return al


# ---------------------------------------------------------------------------------------------
# Documents (the Word templates)
# ---------------------------------------------------------------------------------------------
def _b64(docx: bytes) -> str:
    return base64.b64encode(docx).decode("ascii")


def create_document(name: str, docx: bytes, output_name: str) -> dict:
    """Upload a Word template as a new document that merges to PDF. -> {"id": str}"""
    d = _request("POST", "documents", json={"name": name, "type": "docx", "output": "pdf",
                                            "output_name": output_name, "file_contents": _b64(docx)})
    if not isinstance(d, dict) or not d.get("id"):
        raise FormstackError("Formstack Documents did not return the new document.")
    return {"id": str(d["id"])}


def update_document(doc_id: str, docx: bytes, name: str | None = None, output_name: str | None = None) -> None:
    body = {"file_contents": _b64(docx)}
    if name:
        body["name"] = name
    if output_name:
        body["output_name"] = output_name
    _request("PUT", f"documents/{doc_id}", json=body)


def get_document(doc_id: str) -> dict:
    d = _request("GET", f"documents/{doc_id}")
    if not isinstance(d, dict) or not d.get("id"):
        raise FormstackError("Formstack Documents does not have that document.")
    return d


def field_names(doc_id: str) -> list[str]:
    """The {$Name} merge fields Formstack found in the template."""
    rows = _request("GET", f"documents/{doc_id}/fields")
    return [str(r.get("name")) for r in rows] if isinstance(rows, list) else []


def delete_document(doc_id: str) -> None:
    _request("DELETE", f"documents/{doc_id}")


# ---------------------------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------------------------
_MERGE_HOSTS = {"www.webmerge.me", "webmerge.me"}


def _is_formstack_url(url) -> bool:
    """The merge address Formstack's API hands back must be https on webmerge.me itself. A look-alike host
    (webmerge.me.example.com, or the name in a path) would receive a parish's data."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return False
    return parts.scheme == "https" and (parts.hostname or "").lower() in _MERGE_HOSTS and not parts.username


def merge(doc_id: str, data: dict[str, str], *, test: bool) -> bytes:
    """Merge one parish's data into the document and return the PDF. A test merge is free of the real
    allowance. Retries once on a transient 5xx or 429. Raises FormstackError otherwise."""
    d = get_document(doc_id)
    url = d.get("url")
    if not _is_formstack_url(url):
        raise FormstackError("The document has no merge address.")
    q = "?download=1" + ("&test=1" if test else "")
    last = None
    for attempt in (1, 2):
        try:
            resp = requests.post(url + q, data=data, timeout=_TIMEOUT, allow_redirects=False)
        except requests.ReadTimeout:
            # The request was sent, so the merge may have run (and used allowance). Never send it twice on a guess.
            raise FormstackError("Formstack Documents did not answer in time. The letter may or may not have been merged: "
                                 "check the allowance on the run page, then build again.")
        except requests.RequestException as exc:
            last = type(exc).__name__          # could not connect: nothing was sent, so one retry is safe
            time.sleep(2)
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and attempt == 1:
            last = str(resp.status_code)
            time.sleep(3)
            continue
        if resp.status_code >= 400:
            raise FormstackError(f"Formstack Documents could not merge the letter (HTTP {resp.status_code}).")
        if not resp.content.startswith(b"%PDF"):
            raise FormstackError("Formstack Documents did not return a PDF for the merge.")
        return resp.content
    raise FormstackError(f"Formstack Documents did not answer the merge ({last}).")
