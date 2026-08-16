"""
cfm_sharepoint_client.py -- Graph client AUTH ONLY for CFM's own "Services
Team" SharePoint site (cornerstonefranciscan.sharepoint.com/sites/
servicesteam), used by cornerstone_documents.py's "From Cornerstone"
document section (Cornerstone Mode, 2026-08-16).

Separate tenant, separate app registration, separate credentials from
sharepoint_client.py's EDOM-tenant (episcopalmaryland.org) client -- the
same lesson 26-132's knowledge_base_client.py already learned the hard way
("the two cannot share a token cache or a credentials env var"). Confirmed
directly before writing any calling code: sharepoint_client.py's own
`sharepoint-credentials` secret is scoped to the EDOM tenant and hard-fails
(400) against cornerstonefranciscan.sharepoint.com (per 26-133/26-135's own
CLAUDE.md entries) -- reusing it here would have been a real, live-breaking
mistake, not just an inconsistency.

This module owns ONLY the credential fetch + token cache. Every GENERIC
Graph-mechanics function (get_site_id / list_folder / upload_bytes /
download_bytes / ensure_folder / delete_file) is reused UNCHANGED from
sharepoint_client.py -- none of them read credentials internally, they all
just take an already-fetched token + site_id, so there's nothing tenant-
specific to duplicate there.

Credentials: GCP Secret Manager secret `cfm-sharepoint-credentials`
(project cfm-qbo-mcp) -- the SAME secret 26-132's Gabriel bot already uses
for this exact tenant/site (Sites.Selected, already covers the whole
Services Team site -- confirmed no new Graph permission grant needed).
IAM: `qbo-mcp-sa` (this service's own runtime identity) and
`cfm-daily-jobs-sa` (this machine's local ADC identity, needed for
`run_dev_bypass.py`-based verification) were both granted
`roles/secretmanager.secretAccessor` on this specific secret 2026-08-16 --
neither had it before (confirmed live, a real PERMISSION_DENIED before the
grant, not assumed).
"""
from __future__ import annotations

import json
import os
import time

import requests

SCOPE = "https://graph.microsoft.com/.default"

_SECRET_PROJECT = os.environ.get("FIRESTORE_PROJECT", "cfm-qbo-mcp")
_SECRET_NAME = "cfm-sharepoint-credentials"

# Confirmed 2026-08-16 (Cornerstone Served Parishes Plan.md's own SharePoint
# destination note, and 26-132's knowledge_base_client.py already using
# this exact site for a different library on the same site).
HOSTNAME = "cornerstonefranciscan.sharepoint.com"
SITE_PATH = "/sites/servicesteam"

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
    """Fetch (and cache) a Graph access token for the cfmins.org tenant --
    a SEPARATE cache from sharepoint_client.py's own (different tenant,
    different app registration, must never be conflated)."""
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
