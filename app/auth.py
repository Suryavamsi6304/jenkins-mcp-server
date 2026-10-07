import base64
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from app.config import ALLOWED_OAUTH_REDIRECT_URIS

ACCESS_TOKEN_EXPIRE_SECONDS = 604800
AUTHORIZATION_CODE_EXPIRE_SECONDS = 300
CLIENT_SECRET_LENGTH = 32
SUPPORTED_TOKEN_ENDPOINT_AUTH_METHODS = {
    "none",
    "client_secret_basic",
    "client_secret_post",
}

clients: Dict[str, Dict[str, Any]] = {}
tokens: Dict[str, Dict[str, Any]] = {}
authorization_codes: Dict[str, Dict[str, Any]] = {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(dt: datetime) -> int:
    return int(dt.timestamp())


def _is_secure_redirect_uri(redirect_uri: str) -> bool:
    parsed_uri = urlparse(redirect_uri)
    return parsed_uri.scheme == "https" and bool(parsed_uri.netloc)


def register_dynamic_client(
    client_name: str,
    redirect_uris: Optional[List[str]] = None,
    scope: str = "read",
    grant_types: Optional[List[str]] = None,
    token_endpoint_auth_method: str = "none",
) -> Dict[str, Any]:
    if grant_types is None:
        grant_types = ["authorization_code"]

    if (
        not isinstance(token_endpoint_auth_method, str)
        or token_endpoint_auth_method not in SUPPORTED_TOKEN_ENDPOINT_AUTH_METHODS
    ):
        raise ValueError("Unsupported token endpoint authentication method")

    registered_redirect_uris = redirect_uris or []
    if not registered_redirect_uris:
        raise ValueError("At least one HTTPS redirect URI is required")

    if not all(_is_secure_redirect_uri(redirect_uri) for redirect_uri in registered_redirect_uris):
        raise ValueError("Redirect URIs must use HTTPS")

    if not ALLOWED_OAUTH_REDIRECT_URIS:
        raise ValueError("ALLOWED_OAUTH_REDIRECT_URIS must be configured")

    if not all(
        redirect_uri in ALLOWED_OAUTH_REDIRECT_URIS
        for redirect_uri in registered_redirect_uris
    ):
        raise ValueError("Redirect URI is not approved")

    client_id = secrets.token_urlsafe(16)
    client_secret = (
        secrets.token_urlsafe(CLIENT_SECRET_LENGTH)
        if token_endpoint_auth_method != "none"
        else None
    )
    now = _utc_now()

    client = {
        "client_name": client_name,
        "client_id": client_id,
        "client_secret": client_secret,
        "token_endpoint_auth_method": token_endpoint_auth_method,
        "redirect_uris": registered_redirect_uris,
        "scope": scope,
        "grant_types": grant_types,
        "created_at": _timestamp(now),
        "secret_expires_at": 0,
    }

    clients[client_id] = client
    return client


def validate_client_credentials(client_id: str, client_secret: str) -> bool:
    client = clients.get(client_id)
    return bool(
        client
        and client["token_endpoint_auth_method"] != "none"
        and client["client_secret"]
        and hmac.compare_digest(client["client_secret"], client_secret)
    )


def get_client(client_id: str) -> Optional[Dict[str, Any]]:
    return clients.get(client_id)


def validate_redirect_uri(client_id: str, redirect_uri: str) -> bool:
    client = clients.get(client_id)
    return bool(
        client
        and any(
            hmac.compare_digest(registered_uri, redirect_uri)
            for registered_uri in client["redirect_uris"]
        )
    )


def issue_access_token(client_id: str, scope: Optional[str] = None) -> Optional[Dict[str, Any]]:
    client = clients.get(client_id)
    if not client:
        return None

    token = secrets.token_urlsafe(32)
    expires_at = _timestamp(_utc_now() + timedelta(seconds=ACCESS_TOKEN_EXPIRE_SECONDS))
    token_data = {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_SECONDS,
        "scope": scope or client["scope"],
        "client_id": client_id,
        "issued_at": _timestamp(_utc_now()),
        "expires_at": expires_at,
    }
    tokens[token] = token_data
    return token_data


def validate_access_token(access_token: str) -> Optional[Dict[str, Any]]:
    token_data = tokens.get(access_token)
    if not token_data:
        return None

    if token_data["expires_at"] < _timestamp(_utc_now()):
        tokens.pop(access_token, None)
        return None

    return token_data


def create_authorization_code(
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
) -> str:
    code = secrets.token_urlsafe(32)
    authorization_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "created_at": _timestamp(_utc_now()),
    }
    return code


def validate_authorization_code(code: str) -> Optional[Dict[str, Any]]:
    code_data = authorization_codes.get(code)
    if not code_data:
        return None

    expires_at = code_data["created_at"] + AUTHORIZATION_CODE_EXPIRE_SECONDS
    if expires_at < _timestamp(_utc_now()):
        authorization_codes.pop(code, None)
        return None

    return code_data


def consume_authorization_code(
    code: str,
    client_id: str,
    redirect_uri: str,
) -> Optional[Dict[str, Any]]:
    code_data = validate_authorization_code(code)
    if not code_data:
        return None

    if not hmac.compare_digest(code_data["client_id"], client_id):
        return None

    if not hmac.compare_digest(code_data["redirect_uri"], redirect_uri):
        return None

    return authorization_codes.pop(code, None)


def parse_basic_authorization(header_value: str) -> Optional[Dict[str, str]]:
    if not header_value:
        return None

    value = header_value.strip()
    if not value.lower().startswith("basic "):
        return None

    try:
        encoded = value[6:]
        decoded = base64.b64decode(encoded).decode("utf-8")
        client_id, client_secret = decoded.split(":", 1)
        return {"client_id": client_id, "client_secret": client_secret}
    except Exception:
        return None
