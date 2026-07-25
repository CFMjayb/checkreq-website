"""
auth_azure.py — Azure AD (Microsoft Entra ID) login for the 26-129 website.

Authorization Code flow via MSAL, against the Cornerstone (cfmins.org) tenant
— NOT episcopalmaryland.org. See Plan.md's 2026-07-17 addendum and the
project_26129_custom_website memory note for why this tenant was chosen.

Credentials come from Secret Manager (`checkreq-azure-credentials`, project
cfm-qbo-mcp) — {"tenant_id", "client_id", "client_secret"}. Cached in-process
after first read.
"""
from __future__ import annotations

import json
import os

import msal

_SECRET_PROJECT = os.environ.get("FIRESTORE_PROJECT", "cfm-qbo-mcp")
_SECRET_NAME = "checkreq-azure-credentials"

_SCOPES = ["User.Read"]  # openid/profile/email/offline_access are added by MSAL automatically

_cached_creds: dict | None = None


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


def _msal_app() -> msal.ConfidentialClientApplication:
    c = _creds()
    return msal.ConfidentialClientApplication(
        client_id=c["client_id"],
        client_credential=c["client_secret"],
        authority=f"https://login.microsoftonline.com/{c['tenant_id']}",
    )


def get_auth_url(redirect_uri: str, state: str) -> str:
    """Build the Microsoft login redirect URL."""
    return _msal_app().get_authorization_request_url(
        scopes=_SCOPES, redirect_uri=redirect_uri, state=state,
    )


def acquire_token(code: str, redirect_uri: str) -> dict:
    """Exchange an authorization code for tokens + claims.

    Returns the MSAL result dict on success — includes 'id_token_claims'
    (email, name, oid) and 'access_token'. Raises ValueError with MSAL's
    error description on failure (e.g. bad code, AADSTS* errors)."""
    result = _msal_app().acquire_token_by_authorization_code(
        code, scopes=_SCOPES, redirect_uri=redirect_uri,
    )
    if "error" in result:
        raise ValueError(f"{result.get('error')}: {result.get('error_description', '')}")
    return result
