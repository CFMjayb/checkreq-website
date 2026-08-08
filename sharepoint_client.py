"""
sharepoint_client.py — Microsoft Graph helper for uploading check-request
attachments to each entity's permanent SharePoint archive.

Adapted from the proven `sharepoint_auth.py` pattern already live in
26-101/26-102/26-130 (Azure AD client-credentials grant, raw `requests`, not
MSAL). No shared Python module folder exists in this codebase (every
Cloud Run project is a self-contained repo -- confirmed via 26-130's
Dockerfile explicit per-file COPY list), so this is this project's own copy,
trimmed to only what attachment upload needs (a single in-memory PUT -- no
download/list/chunked-session code, since these files are small).

Credentials: GCP Secret Manager secret `sharepoint-credentials` (project
cfm-qbo-mcp) -- the SAME secret 26-101/26-102/26-130 already use, already
has Sites.ReadWrite.All admin-consented. Do NOT confuse this with
`checkreq-azure-credentials` (auth_azure.py) -- that's 26-129's own separate
interactive user-login app registration, a different credential set for a
different purpose.
"""
from __future__ import annotations

import json
import os
import time

import requests

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPE = "https://graph.microsoft.com/.default"

_SECRET_PROJECT = os.environ.get("FIRESTORE_PROJECT", "cfm-qbo-mcp")
_SECRET_NAME = "sharepoint-credentials"

_cached_creds: dict | None = None
_token_cache = {"token": None, "expires_at": 0}


def _read_secret(name: str) -> str:
    from google.cloud import secretmanager
    client = secretmanager.SecretManagerServiceClient()
    path = f"projects/{_SECRET_PROJECT}/secrets/{name}/versions/latest"
    return client.access_secret_version(name=path).payload.data.decode("utf-8")


def _creds() -> dict:
    global _cached_creds
    if _cached_creds is None:
        _cached_creds = json.loads(_read_secret(_SECRET_NAME))
    return _cached_creds


def get_access_token() -> str:
    """Fetch (and cache) a Graph access token via client_credentials grant."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    c = _creds()
    url = f"https://login.microsoftonline.com/{c['tenant_id']}/oauth2/v2.0/token"
    resp = requests.post(url, data={
        "grant_type": "client_credentials",
        "client_id": c["client_id"],
        "client_secret": c["client_secret"],
        "scope": SCOPE,
    })
    if not resp.ok:
        raise RuntimeError(f"Microsoft token request failed ({resp.status_code}): {resp.text[:500]}")
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    return _token_cache["token"]


def _headers(token: str, content_type: str | None = "application/json") -> dict:
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if content_type:
        h["Content-Type"] = content_type
    return h


def _check(resp: requests.Response, what: str) -> requests.Response:
    if not resp.ok:
        raise RuntimeError(f"Graph {what} failed ({resp.status_code}): {resp.text[:500]}")
    return resp


def get_site_id(token: str, hostname: str, site_path: str) -> str:
    """hostname='cornerstonefranciscan.sharepoint.com', site_path='/sites/servicesteam'."""
    url = f"{GRAPH_BASE}/sites/{hostname}:{site_path}"
    resp = _check(requests.get(url, headers=_headers(token)), f"site resolution for {hostname}:{site_path}")
    return resp.json()["id"]


def upload_bytes(token: str, site_id: str, folder_path: str, filename: str, data: bytes, content_type: str) -> dict:
    """Single-PUT upload (files here are always small -- no chunked-session
    path needed). Overwrites unconditionally on filename collision -- this is
    why the caller's naming convention always appends a unique request number."""
    seg = f"{folder_path.strip('/')}/{filename}"
    url = f"{GRAPH_BASE}/sites/{site_id}/drive/root:/{seg}:/content"
    resp = _check(
        requests.put(url, headers=_headers(token, content_type="application/octet-stream"), data=data),
        f'upload "{seg}"',
    )
    return resp.json()


def download_bytes(token: str, site_id: str, file_path: str) -> bytes:
    """Downloads a file's raw bytes via the same :/content addressing scheme
    as upload_bytes -- added 2026-07-25 for the Edit-page attachment "view"
    feature. Graph's own /content endpoint issues a 302 to a pre-authenticated
    short-lived download URL; `requests` follows redirects by default, so this
    returns the real file bytes in one call, no special-casing needed.

    Deliberate design choice over linking users straight to a stored
    sp_web_url: sp_web_url points at a *different* Azure AD tenant
    (episcopalmaryland.sharepoint.com) than the one this app's own users sign
    into (cfmins.org) -- proxying through this same service credential
    (already used for upload) works regardless of the viewer's own SharePoint
    tenant access, avoiding a second, unrelated login wall."""
    seg = file_path.strip("/")
    url = f"{GRAPH_BASE}/sites/{site_id}/drive/root:/{seg}:/content"
    resp = _check(requests.get(url, headers=_headers(token, content_type=None)), f'download "{seg}"')
    return resp.content


def list_folder(token: str, site_id: str, folder_path: str) -> list[dict]:
    """Lists a folder's immediate children (files and subfolders).
    folder_path='' lists the drive root. Returns [] if the folder doesn't
    exist yet (404) -- callers (Parish Portal S5, 2026-08-08) treat a
    not-yet-created area (e.g. a parish's own editable "Parish Files"
    subfolder before its first upload) as genuinely empty, not an error."""
    seg = folder_path.strip("/")
    if seg:
        url = f"{GRAPH_BASE}/sites/{site_id}/drive/root:/{seg}:/children?$top=500"
    else:
        url = f"{GRAPH_BASE}/sites/{site_id}/drive/root/children?$top=500"
    resp = requests.get(url, headers=_headers(token))
    if resp.status_code == 404:
        return []
    _check(resp, f'list "{seg or "/"}"')
    out = []
    for it in resp.json().get("value", []):
        out.append({
            "name": it["name"],
            "is_folder": "folder" in it,
            "size": it.get("size", 0),
            "id": it.get("id"),
            "web_url": it.get("webUrl"),
            "last_modified": it.get("lastModifiedDateTime"),
        })
    return out


def ensure_folder(token: str, site_id: str, parent_path: str, name: str) -> None:
    """Creates a subfolder `name` under parent_path if it doesn't already
    exist (Parish Portal S5). Deliberately does NOT use
    @microsoft.graph.conflictBehavior='replace' the way upload_bytes does --
    for a FOLDER, "replace" risks clobbering an already-existing folder's
    real contents, not just overwriting one file. Checks via a plain
    listing first and only POSTs a create when genuinely missing; a 409 on
    the create (another request won the same race) is treated as success,
    not an error."""
    existing = list_folder(token, site_id, parent_path)
    if any(it["is_folder"] and it["name"] == name for it in existing):
        return
    seg = parent_path.strip("/")
    url = (f"{GRAPH_BASE}/sites/{site_id}/drive/root:/{seg}:/children" if seg
           else f"{GRAPH_BASE}/sites/{site_id}/drive/root/children")
    resp = requests.post(url, headers=_headers(token), json={
        "name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail",
    })
    if resp.status_code not in (201, 409):
        raise RuntimeError(f'create folder "{name}" under "{parent_path}" failed '
                            f'({resp.status_code}): {resp.text[:300]!r}')


def delete_file(token: str, site_id: str, file_path: str) -> None:
    """Not currently called by any route (attachment removal in this app is a
    soft-delete only -- see payment_request_attachments.removed_at) -- added
    for parity/completeness since a prior session (2026-07-26) had to fall
    back to a raw requests.delete() call for test-cleanup with a comment
    flagging this gap. Kept here for any future genuinely-destructive cleanup
    need (e.g. a real GDPR-style purge), not wired to anything today.

    Deletes the driveItem itself -- addressed WITHOUT the :/content suffix
    (that suffix addresses an item's content stream, used by upload/download;
    a DELETE against .../content is a different, undocumented operation that
    was empirically confirmed here to return 200 with the file's own bytes
    echoed back rather than deleting anything -- caught live while testing
    this exact function, fixed before ever relying on it)."""
    seg = file_path.strip("/")
    url = f"{GRAPH_BASE}/sites/{site_id}/drive/root:/{seg}"
    resp = requests.delete(url, headers=_headers(token, content_type=None))
    if resp.status_code not in (204, 404):
        raise RuntimeError(f'Graph delete "{seg}" failed ({resp.status_code}): {resp.text[:300]!r}')
