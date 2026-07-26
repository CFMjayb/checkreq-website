"""
auth_azure.py — Azure AD (Microsoft Entra ID) login for the 26-129 website.

Authorization Code flow via MSAL. Originally single-tenant (Cornerstone/
cfmins.org only) -- widened 2026-07-26 (Multi-Provider Authentication
Plan.md, Section 1) to the fixed, literal `/organizations` multi-tenant
authority so any Microsoft/Entra ID work-or-school tenant can sign in, once
that tenant's own IT grants a one-time admin-consent visit (see Plan.md
Section 1's walkthrough). This is Microsoft's own standard multi-tenant-web-
app endpoint -- it accepts any organizational tenant and rejects personal
Microsoft accounts (@outlook.com/@hotmail.com/Xbox) on its own, with zero
custom code required to enforce that exclusion. tenant_id is no longer read
from the secret for this purpose (client_id/client_secret are unchanged).

IMPORTANT -- real access control is NOT done here. A successful sign-in from
any tenant only proves WHO someone is; whether they're allowed to use Beacon
is decided entirely by main.py's _complete_login() against checkreq.app_users.
See that function's docstring for why (2026-07-17/18 duplicate-profile bug).

**Azure Portal change still needed, blocking multi-tenant sign-in from
working in practice** (confirmed 2026-07-26 this cannot be done
programmatically with the credentials this app has -- see CLAUDE.md's
Current State entry for this build): the app registration's own "Supported
account types" must be changed from single-tenant to
"Accounts in any organizational directory (Any Microsoft Entra ID tenant -
Multitenant)" (manifest signInAudience = AzureADMultipleOrgs) in the Azure
Portal, client id a7f89cbd-0d41-457d-a090-ee367fda4f65, tenant cfmins.org.
Only Jay (or someone with Application Administrator rights on that tenant)
can do this -- confirmed live that this app's own client-credentials token
carries no directory roles (`roles: None`), so it has no Graph API rights to
change its own registration's signInAudience. Until that Portal change lands,
this code change is harmless/inert for cfmins.org logins (which still work
exactly as before) but any other tenant's user will get a real AADSTS700016-
class error from Microsoft, not a clean Beacon-side rejection.

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
        # Fixed multi-tenant authority (Plan.md Section 1) -- NOT
        # f"https://login.microsoftonline.com/{c['tenant_id']}" anymore.
        # MSAL's acquire_token_by_authorization_code() fetches the correct
        # tenant-specific signing keys automatically against this authority
        # and returns id_token_claims including `tid` (which tenant the user
        # actually came from) -- no hand-written JWT validation needed.
        authority="https://login.microsoftonline.com/organizations",
    )


def get_auth_url(redirect_uri: str, state: str, login_hint: str | None = None) -> str:
    """Build the Microsoft login redirect URL. login_hint (optional) prefills
    the email the user already typed on Beacon's own email-first login page
    (Multi-Provider Authentication Plan.md, Section 3) -- purely a UX nicety,
    Microsoft still lets the user change it."""
    return _msal_app().get_authorization_request_url(
        scopes=_SCOPES, redirect_uri=redirect_uri, state=state, login_hint=login_hint,
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
