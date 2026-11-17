"""Tests for the OpenID Connect client module."""

from unittest.mock import Mock, patch

import httpx
import pytest

from shelly_backup.oidc import (
    DEFAULT_SCOPES,
    OIDCConfig,
    OIDCError,
    authorization_url,
    clear_discovery_cache,
    discover_endpoints,
    exchange_code,
    fetch_userinfo,
    oidc_config_from_settings,
)

_ISSUER = "https://id.example.com/realms/main"

_DISCOVERY = {
    "authorization_endpoint": f"{_ISSUER}/protocol/openid-connect/auth",
    "token_endpoint": f"{_ISSUER}/protocol/openid-connect/token",
    "userinfo_endpoint": f"{_ISSUER}/protocol/openid-connect/userinfo",
}


def _response(payload, status_code=200):
    """Create a mock httpx response with a JSON payload."""
    response = Mock()
    response.status_code = status_code
    response.json.return_value = payload
    response.raise_for_status = Mock()
    return response


@pytest.fixture(autouse=True)
def _clear_cache():
    """Keep discovery caching from leaking between tests."""
    clear_discovery_cache()
    yield
    clear_discovery_cache()


class TestConfig:
    """Tests for the OIDC settings parsing."""

    def test_defaults_without_settings(self):
        """Test that missing or broken settings fall back to defaults."""
        for settings in ({}, {"oidc": "nonsense"}, {"oidc": []}):
            config = oidc_config_from_settings(settings)
            assert config == OIDCConfig()
            assert config.enabled is False
            assert config.scopes == DEFAULT_SCOPES
            assert config.ready is False

    def test_parses_and_normalizes_values(self):
        """Test that stored values are trimmed and validated."""
        config = oidc_config_from_settings(
            {
                "oidc": {
                    "enabled": True,
                    "issuer": " https://id.example.com/ ",
                    "client_id": " shelly-backup ",
                    "client_secret": "secret",
                    "callback_url": "https://backup.example.com/oidc/callback",
                    "scopes": " openid email ",
                    "provider_name": " Acme ID ",
                    "auto_create_users": True,
                    "disable_password_login": True,
                }
            }
        )

        assert config.issuer == "https://id.example.com"
        assert config.client_id == "shelly-backup"
        assert config.callback_url == "https://backup.example.com/oidc/callback"
        assert config.provider_name == "Acme ID"
        assert config.scopes == "openid email"
        assert config.auto_create_users is True
        assert config.disable_password_login is True
        assert config.ready is True

    def test_scopes_default_when_blank(self):
        """Test that an empty scope string falls back to the default."""
        config = oidc_config_from_settings({"oidc": {"enabled": True, "scopes": " "}})

        assert config.scopes == DEFAULT_SCOPES

    def test_ready_requires_issuer_and_client(self):
        """Test that incomplete configurations are not considered ready."""
        assert OIDCConfig(enabled=True, issuer="https://x").ready is False
        assert OIDCConfig(enabled=True, client_id="c").ready is False
        assert OIDCConfig(enabled=True, issuer="https://x", client_id="c").ready is True


class TestEndpoints:
    """Tests for discovery and the request helpers."""

    def test_discovery_returns_endpoints_and_caches(self):
        """Test that discovery succeeds and results are cached."""
        with patch("httpx.get", return_value=_response(dict(_DISCOVERY))) as get:
            endpoints = discover_endpoints(_ISSUER)
            cached = discover_endpoints(_ISSUER)

            assert endpoints == _DISCOVERY
            assert cached == endpoints
            assert get.call_count == 1
            assert get.call_args[0][0] == _ISSUER + "/.well-known/openid-configuration"

    def test_discovery_rejects_missing_endpoints(self):
        """Test that incomplete discovery documents fail with an error."""
        payload = dict(_DISCOVERY)
        del payload["token_endpoint"]
        with (
            patch("httpx.get", return_value=_response(payload)),
            pytest.raises(OIDCError, match="token_endpoint"),
        ):
            discover_endpoints(_ISSUER)

    def test_discovery_reports_http_errors(self):
        """Test that transport errors are wrapped."""
        with (
            patch("httpx.get", side_effect=httpx.ConnectError("boom")),
            pytest.raises(OIDCError, match="discovery document"),
        ):
            discover_endpoints(_ISSUER)

    def test_discovery_requires_issuer(self):
        """Test that an empty issuer raises immediately."""
        with pytest.raises(OIDCError, match="not configured"):
            discover_endpoints("  ")

    def test_clear_discovery_cache(self):
        """Test that the cache can be invalidated."""
        with patch("httpx.get", return_value=_response(dict(_DISCOVERY))) as get:
            discover_endpoints(_ISSUER)
            clear_discovery_cache()
            discover_endpoints(_ISSUER)

            assert get.call_count == 2


class TestAuthorizationUrl:
    """Tests for the authorization redirect URL."""

    def test_builds_query(self):
        """Test that the URL carries the flow parameters."""
        config = OIDCConfig(
            enabled=True,
            issuer=_ISSUER,
            client_id="shelly-backup",
            scopes="openid email",
        )
        url = authorization_url(
            config,
            _DISCOVERY,
            "https://app.example/oidc/callback",
            "state-1",
            "nonce-1",
        )

        assert url.startswith(_DISCOVERY["authorization_endpoint"] + "?")
        assert "response_type=code" in url
        assert "client_id=shelly-backup" in url
        assert "scope=openid+email" in url
        assert "state=state-1" in url
        assert "nonce=nonce-1" in url
        assert "redirect_uri=https%3A%2F%2Fapp.example%2Foidc%2Fcallback" in url

    def test_appends_to_existing_query(self):
        """Test that endpoints with a query string are extended correctly."""
        endpoints = dict(_DISCOVERY)
        endpoints["authorization_endpoint"] = (
            _DISCOVERY["authorization_endpoint"] + "?mode=query"
        )
        url = authorization_url(
            OIDCConfig(issuer=_ISSUER, client_id="c"),
            endpoints,
            "https://app/oidc/callback",
            "s",
            "n",
        )

        assert url.startswith(endpoints["authorization_endpoint"] + "&")


class TestTokenExchange:
    """Tests for the code exchange and userinfo retrieval."""

    _config = OIDCConfig(
        enabled=True,
        issuer=_ISSUER,
        client_id="client",
        client_secret="secret",
    )

    def test_exchange_returns_access_token(self):
        """Test the successful code exchange."""
        with patch(
            "httpx.post", return_value=_response({"access_token": "token-1"})
        ) as post:
            token = exchange_code(self._config, _DISCOVERY, "code-1", "https://app/cb")

            assert token == "token-1"
            sent = post.call_args.kwargs["data"]
            assert sent["grant_type"] == "authorization_code"
            assert sent["code"] == "code-1"
            assert sent["client_secret"] == "secret"

    def test_exchange_reports_provider_errors(self):
        """Test that error responses raise with the provider error."""
        with (
            patch("httpx.post", return_value=_response({"error": "invalid_grant"})),
            pytest.raises(OIDCError, match="invalid_grant"),
        ):
            exchange_code(self._config, _DISCOVERY, "code-1", "https://app/cb")

    def test_exchange_requires_access_token(self):
        """Test that a token response without access token fails."""
        with (
            patch("httpx.post", return_value=_response({"id_token": "x"})),
            pytest.raises(OIDCError, match="access token"),
        ):
            exchange_code(self._config, _DISCOVERY, "code-1", "https://app/cb")

    def test_exchange_handles_http_status(self):
        """Test that non-OK statuses are reported."""
        with (
            patch("httpx.post", return_value=_response({}, status_code=500)),
            pytest.raises(OIDCError, match="HTTP 500"),
        ):
            exchange_code(self._config, _DISCOVERY, "code-1", "https://app/cb")

    def test_fetch_userinfo(self):
        """Test that the bearer token is sent to the userinfo endpoint."""
        with patch(
            "httpx.get", return_value=_response({"email": "user@example.com"})
        ) as get:
            profile = fetch_userinfo(_DISCOVERY, "token-1")

            assert profile == {"email": "user@example.com"}
            assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer token-1"

    def test_fetch_userinfo_rejects_errors(self):
        """Test that userinfo failures are wrapped."""
        with (
            patch("httpx.get", return_value=_response({}, status_code=401)),
            pytest.raises(OIDCError, match="HTTP 401"),
        ):
            fetch_userinfo(_DISCOVERY, "token-1")

    def test_fetch_userinfo_requires_object(self):
        """Test that non-object payloads are rejected."""
        with (
            patch("httpx.get", return_value=_response([])),
            pytest.raises(OIDCError, match="not an object"),
        ):
            fetch_userinfo(_DISCOVERY, "token-1")
