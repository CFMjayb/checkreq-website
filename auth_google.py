"""
auth_google.py — Google OAuth/OIDC login for the 26-129 website.

Second, independent identity provider added per Multi-Provider Authentication
Plan.md (2026-07-26, Jay-approved). Deliberately mirrors auth_azure.py's exact
shape (get_auth_url / acquire_token) so the two providers read as one family
of code, not two unrelated ones -- see that file's docstring for the sibling
pattern.

Built on google-auth-oauthlib's Flow class (the direct equivalent of MSAL's
ConfidentialClientApplication) rather than hand-assembling the authorization
URL / token exchange -- keeps scope-encoding and token verification correct
without maintaining that logic ourselves.

Credentials come from Secret Manager (`checkreq-google-credentials`, project
cfm-qbo-mcp, reusing the existing cfm-qbo-mcp GCP project per Jay's decision
4 in the plan) -- {"client_id", "client_secret"}. Cached in-process after
first read, same pattern as auth_azure.py.

GCP setup still needed before this can be used for real (Plan.md Section 2,
decisions 4-5): the checkreq-google-credentials secret does not exist yet --
it requires a real OAuth 2.0 Client ID created in the Google Cloud Console
for project cfm-qbo-mcp (Console-only; confirmed live 2026-07-26 that no
gcloud/API path exists for this -- see CLAUDE.md's Current State entry for
this build). Until that secret exists, get_auth_url()/acquire_token() will
raise when _creds() tries to read it -- the /auth/google/start route lets
that exception surface as a clear 500 rather than silently degrading.
"""
from __future__ import annotations

import json
import os

from google_auth_oauthlib.flow import Flow
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

_SECRET_PROJECT = os.environ.get("FIRESTORE_PROJECT", "cfm-qbo-mcp")
_SECRET_NAME = "checkreq-google-credentials"

# openid/email/profile are Google's own documented "non-sensitive" scope
# category -- exempt from app-verification review, confirmed in the plan.
_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

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


def _client_config(redirect_uri: str) -> dict:
    c = _creds()
    return {
        "web": {
            "client_id": c["client_id"],
            "client_secret": c["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


def get_auth_url(redirect_uri: str, state: str, login_hint: str | None = None) -> str:
    """Build the Google login redirect URL. login_hint (optional) prefills
    the email the user already typed on Beacon's own email-first login page
    (Multi-Provider Authentication Plan.md, Section 3) -- purely a UX nicety,
    Google still lets the user change it."""
    flow = Flow.from_client_config(
        _client_config(redirect_uri), scopes=_SCOPES, redirect_uri=redirect_uri,
    )
    kwargs = {}
    if login_hint:
        kwargs["login_hint"] = login_hint
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=state,
        # No prompt="consent" -- Google's own login/consent UI handles a
        # returning user's already-granted-scope case correctly on its own,
        # matching how the Microsoft side never forces a re-consent either.
        **kwargs,
    )
    return auth_url


def acquire_token(code: str, redirect_uri: str, state: str) -> dict:
    """Exchange an authorization code for tokens + verified ID-token claims.

    Returns a claims dict with email/email_verified/name/sub (Google's
    durable per-account id, the equivalent of Azure's oid). Raises ValueError
    on any failure (bad code, signature/audience/expiry failure, etc.) --
    same contract as auth_azure.acquire_token()."""
    c = _creds()
    flow = Flow.from_client_config(
        _client_config(redirect_uri), scopes=_SCOPES, redirect_uri=redirect_uri, state=state,
    )
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001 -- surface any exchange failure uniformly
        raise ValueError(f"google_token_exchange_failed: {exc}") from exc

    credentials = flow.credentials
    if not credentials.id_token:
        raise ValueError("google_no_id_token: Google did not return an ID token")

    try:
        claims = google_id_token.verify_oauth2_token(
            credentials.id_token, google_requests.Request(), c["client_id"],
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"google_id_token_invalid: {exc}") from exc

    # Real Google-specific caveat (Plan.md Section 2): email_verified can
    # occasionally be False for custom-domain edge cases. Azure's UPN claims
    # never need this check -- Microsoft only issues already-verified UPNs.
    if not claims.get("email_verified"):
        raise ValueError("google_email_not_verified: Google reports this email address is not verified")

    return {
        "email": (claims.get("email") or "").strip().lower(),
        "name": claims.get("name", ""),
        "sub": claims.get("sub", ""),
    }
