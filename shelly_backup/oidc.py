"""OpenID Connect configuration parsing and provider client.

The identity of a user is verified by calling the provider's userinfo
endpoint with the exchanged access token. This delegates token validation
to the provider and keeps the app free of extra cryptographic dependencies.
"""

from dataclasses import dataclass
import threading
import time
from urllib.parse import urlencode

import httpx

DEFAULT_SCOPES = "openid email profile"
DISCOVERY_SUFFIX = "/.well-known/openid-configuration"
DISCOVERY_KEYS = (
    "authorization_endpoint",
    "token_endpoint",
    "userinfo_endpoint",
)

_TIMEOUT = 10.0
_DISCOVERY_TTL = 3600.0


class OIDCError(Exception):
    """Raised when an OpenID Connect step cannot be completed."""


@dataclass(frozen=True)
class OIDCConfig:
    """Stored OpenID Connect settings."""

    enabled: bool = False
    issuer: str = ""
    client_id: str = ""
    client_secret: str = ""
    base_url: str = ""
    scopes: str = DEFAULT_SCOPES
    auto_create_users: bool = False
    disable_password_login: bool = False

    @property
    def ready(self) -> bool:
        """Whether the configuration is complete enough to start a login."""
        return bool(self.enabled and self.issuer and self.client_id)


def oidc_config_from_settings(settings: dict) -> OIDCConfig:
    """Return the OIDC configuration stored in the application settings."""
    raw = settings.get("oidc") if isinstance(settings, dict) else None
    if not isinstance(raw, dict):
        return OIDCConfig()

    def text(key: str) -> str:
        value = raw.get(key)
        return str(value).strip() if isinstance(value, str) else ""

    return OIDCConfig(
        enabled=bool(raw.get("enabled")),
        issuer=text("issuer").rstrip("/"),
        client_id=text("client_id"),
        client_secret=text("client_secret"),
        base_url=text("base_url").rstrip("/"),
        scopes=text("scopes") or DEFAULT_SCOPES,
        auto_create_users=bool(raw.get("auto_create_users")),
        disable_password_login=bool(raw.get("disable_password_login")),
    )


_endpoints_cache: dict[str, tuple[float, dict[str, str]]] = {}
_cache_lock = threading.Lock()


def clear_discovery_cache() -> None:
    """Forget all cached provider discovery results."""
    with _cache_lock:
        _endpoints_cache.clear()


def discover_endpoints(issuer: str) -> dict[str, str]:
    """Return the provider endpoints from its OIDC discovery document."""
    base = issuer.strip().rstrip("/")
    if not base:
        raise OIDCError("The OIDC issuer URL is not configured.")
    with _cache_lock:
        cached = _endpoints_cache.get(base)
        if cached is not None and cached[0] > time.monotonic():
            return dict(cached[1])
    try:
        response = httpx.get(base + DISCOVERY_SUFFIX, timeout=_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise OIDCError(f"Unable to reach the OIDC discovery document: {exc}") from exc
    except ValueError as exc:
        raise OIDCError("The OIDC discovery document is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise OIDCError("The OIDC discovery document is not an object.")
    endpoints: dict[str, str] = {}
    for key in DISCOVERY_KEYS:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise OIDCError(f"The OIDC discovery document is missing {key}.")
        endpoints[key] = value
    with _cache_lock:
        _endpoints_cache[base] = (time.monotonic() + _DISCOVERY_TTL, dict(endpoints))
    return endpoints


def authorization_url(
    config: OIDCConfig,
    endpoints: dict[str, str],
    redirect_uri: str,
    state: str,
    nonce: str,
) -> str:
    """Return the provider URL to send the user to for sign-in."""
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "scope": config.scopes,
        "state": state,
        "nonce": nonce,
    }
    separator = "&" if "?" in endpoints["authorization_endpoint"] else "?"
    return f"{endpoints['authorization_endpoint']}{separator}{urlencode(params)}"


def exchange_code(
    config: OIDCConfig,
    endpoints: dict[str, str],
    code: str,
    redirect_uri: str,
) -> str:
    """Exchange an authorization code and return the provider access token."""
    try:
        response = httpx.post(
            endpoints["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
            },
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise OIDCError(f"Unable to reach the OIDC token endpoint: {exc}") from exc
    if response.status_code != httpx.codes.OK:
        raise OIDCError(
            f"The OIDC token endpoint returned HTTP {response.status_code}."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise OIDCError("The OIDC token response is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise OIDCError("The OIDC token response is not an object.")
    error = payload.get("error")
    if error:
        raise OIDCError(f"The OIDC provider rejected the login: {error}.")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OIDCError("The OIDC token response did not include an access token.")
    return access_token


def fetch_userinfo(endpoints: dict[str, str], access_token: str) -> dict:
    """Return the verified user profile from the provider userinfo endpoint."""
    try:
        response = httpx.get(
            endpoints["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise OIDCError(f"Unable to reach the OIDC userinfo endpoint: {exc}") from exc
    if response.status_code != httpx.codes.OK:
        raise OIDCError(
            f"The OIDC userinfo endpoint returned HTTP {response.status_code}."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise OIDCError("The OIDC userinfo response is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise OIDCError("The OIDC userinfo response is not an object.")
    return payload
