"""
entra_token.py -- checks a signed-in person's Microsoft (Entra ID) token for Beacon's Phoenix-workbook calls (26-158 / 26-129, 2026-10-22).

The Phoenix workbook already signs each person in with their own Microsoft account (the same device-code sign-in it uses for QuickBooks:
modAPI.GetAuthToken), so Beacon needs no shared key in a workbook. The token is issued for the same Entra app the QBO, Databank, BILL and
Email servers already accept (their tenant and client ids are public values, served by qbo-mcp-server's /api/auth-config); this module
checks it the same way bearer_auth.py does in the Cloud Email Server: signature against Entra's published keys, expiry, issuer (v1 or v2)
and audience (api://<client id> or the bare client id).

It only says WHO signed in. What that person may do in Beacon is decided by Beacon's own roles (phoenix_api.py).
"""
from __future__ import annotations

import jwt
from jwt import PyJWKClient

TENANT_ID = "b5c4a260-4cc3-42eb-9c8c-b356942e0b8c"      # the Cornerstone tenant that issues these sign-ins
CLIENT_ID = "f7a8fc4e-33e6-4558-9d12-be9f3c7cf7fa"      # the shared Entra app the workbooks and MCP servers use

_jwks_clients: dict = {}


def _jwks(tenant_id: str) -> PyJWKClient:
    if tenant_id not in _jwks_clients:
        _jwks_clients[tenant_id] = PyJWKClient(f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys", cache_keys=True)
    return _jwks_clients[tenant_id]


def validate_token(token: str, tenant_id: str = TENANT_ID, client_id: str = CLIENT_ID) -> dict:
    """The decoded token payload, or raises jwt.InvalidTokenError."""
    key = _jwks(tenant_id).get_signing_key_from_jwt(token)
    payload = jwt.decode(token, key.key, algorithms=["RS256"],
                         options={"verify_exp": True, "verify_aud": False, "verify_iss": False})
    if payload.get("iss", "") not in {f"https://login.microsoftonline.com/{tenant_id}/v2.0", f"https://sts.windows.net/{tenant_id}/"}:
        raise jwt.InvalidIssuerError(f"Invalid issuer: {payload.get('iss')}")
    aud = payload.get("aud", "")
    if not ({aud} if isinstance(aud, str) else set(aud)) & {f"api://{client_id}", client_id}:
        raise jwt.InvalidAudienceError(f"Invalid audience: {aud}")
    return payload


def candidate_emails(payload: dict) -> list[str]:
    """The addresses a token may be known by, lower case, in order. A guest account's user name looks like
    'name_domain.org#EXT#@tenant.onmicrosoft.com': the address it stands for is 'name@domain.org'."""
    out: list[str] = []
    for claim in ("email", "preferred_username", "upn", "unique_name"):
        v = str(payload.get(claim) or "").strip().lower()
        if not v:
            continue
        if "#ext#" in v:
            local = v.split("#ext#")[0]
            if "_" in local:
                name, dom = local.rsplit("_", 1)
                if name and "." in dom and f"{name}@{dom}" not in out:
                    out.append(f"{name}@{dom}")
            continue
        if v not in out:
            out.append(v)
    return out


def display_name(payload: dict) -> str:
    return payload.get("name") or payload.get("preferred_username") or payload.get("upn") or payload.get("sub", "?")
