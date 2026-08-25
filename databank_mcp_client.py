"""
databank_mcp_client.py — thin REST client for 26-120 databank-mcp-server's
clergy-directory endpoint. New file (Parish Portal S2 — registry.py's
sync_from_databank() is its first and only caller).

Mirrors qbo_mcp_client.py's exact shape (same tuple-return contract, same
Secret Manager auth pattern) so the two external-service clients read as one
family, not two unrelated ones.

Auth: X-API-Key header, GCP Secret Manager secret `databank-mcp-api-key`
(project cfm-qbo-mcp — same project this app already reads
checkreq-azure-credentials/checkreq-google-credentials from, no new IAM
grant expected to be needed).

get_congregations() returns (list_or_None, error_str_or_None) — never
raises. As of 2026-08-07 this will reliably return an error: the Databank
vendor's real ClergyDirectory URL path is still unconfirmed (see 26-120's
CLAUDE.md) — registry.py's sync must treat that as "nothing to sync yet,"
never as a reason to fabricate rows.
"""
from __future__ import annotations

import os

import requests

DATABANK_MCP_URL = os.environ.get(
    "DATABANK_MCP_URL", "https://databank-mcp-server-xltaug3m6q-ue.a.run.app"
).rstrip("/")

_SECRET_PROJECT = os.environ.get("FIRESTORE_PROJECT", "cfm-qbo-mcp")
_cached_api_key: str | None = None


def _get_api_key() -> str:
    global _cached_api_key
    env = os.environ.get("DATABANK_MCP_API_KEY", "").strip()
    if env:
        return env
    if _cached_api_key is None:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{_SECRET_PROJECT}/secrets/databank-mcp-api-key/versions/latest"
        _cached_api_key = client.access_secret_version(name=name).payload.data.decode("utf-8").strip()
    return _cached_api_key


def get_contact(contact_id: str, timeout: int = 15) -> tuple[dict | None, str | None]:
    """GET /api/contacts?contactid=X -- one church/congregation record, keyed
    by the same Databank ContactID stored on portal.parishes.databank_contact_id
    (2026-08-08 batch of parish matching). Returns (contact_dict_or_None,
    error_str_or_None) -- never raises, same contract as get_congregations()."""
    url = f"{DATABANK_MCP_URL}/api/contacts"
    try:
        resp = requests.get(
            url,
            headers={"X-API-Key": _get_api_key()},
            params={"contactid": contact_id},
            timeout=timeout,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if not resp.ok:
            return None, data.get("error") or f"HTTP {resp.status_code}: {resp.text[:300]}"
        if "error" in data:
            return None, data["error"]
        contacts = data.get("contacts", [])
        return (contacts[0] if contacts else None), None
    except Exception as exc:
        return None, str(exc)


def get_clergy_for_church(churchwebacct, timeout: int = 30) -> tuple[list[dict] | None, str | None]:
    """GET /api/clergy-directory?churchwebacct=X -- clergy assigned to one
    church (name/role/email/mobile). churchwebacct here is Databank's
    "internal_contact_id" -- NOT the small "contactid" get_contact() takes.
    A caller who only has a parish's databank_contact_id (a contactid)
    gets internal_contact_id for free: it's already in get_contact()'s own
    response dict. Returns (rows_or_None, error_str_or_None) -- never
    raises, same contract as get_congregations()."""
    url = f"{DATABANK_MCP_URL}/api/clergy-directory"
    try:
        resp = requests.get(
            url,
            headers={"X-API-Key": _get_api_key()},
            params={"churchwebacct": churchwebacct},
            timeout=timeout,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if not resp.ok:
            return None, data.get("error") or f"HTTP {resp.status_code}: {resp.text[:300]}"
        if "error" in data:
            return None, data["error"]
        return data.get("clergy", []), None
    except Exception as exc:
        return None, str(exc)


def get_congregations(timeout: int = 30) -> tuple[list[dict] | None, str | None]:
    """GET /api/clergy-directory?congregations=true — the registry-seed shape
    (clergy_directory.congregations_from()): distinct churches keyed by
    databank_churchwebacct, plus leadership-only name variants with
    churchwebacct=None (need human matching, R3 — see Parish Portal Plan.md
    Section 9). Returns (rows, None) on success, (None, error) otherwise —
    same contract as qbo_mcp_client.py, never raises."""
    url = f"{DATABANK_MCP_URL}/api/clergy-directory"
    try:
        resp = requests.get(
            url,
            headers={"X-API-Key": _get_api_key()},
            params={"congregations": "true"},
            timeout=timeout,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if not resp.ok:
            return None, data.get("error") or f"HTTP {resp.status_code}: {resp.text[:300]}"
        if "error" in data:
            return None, data["error"]
        return data.get("congregations", []), None
    except Exception as exc:
        return None, str(exc)
