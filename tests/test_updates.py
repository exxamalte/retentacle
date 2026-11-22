"""Tests for the GitHub update check."""

from unittest.mock import Mock, patch

import httpx
import pytest

from retentacle import updates
from retentacle.updates import UpdateInfo, _is_newer, cached_update, refresh_update_info


def _release_response(tag, status_code=200, prerelease=False, draft=False):
    """Create a mock GitHub releases API response."""
    response = Mock()
    response.status_code = status_code
    response.json.return_value = {
        "tag_name": tag,
        "html_url": f"https://github.com/exxamalte/retentacle/releases/tag/{tag}",
        "prerelease": prerelease,
        "draft": draft,
    }
    return response


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch):
    """Enable update checks with a fresh cache for every test."""
    updates.clear_cache()
    monkeypatch.setenv("RETENTACLE_UPDATE_CHECK", "1")
    yield
    updates.clear_cache()


class TestVersionComparison:
    """Tests for the release-versus-current version logic."""

    @pytest.mark.parametrize(
        ("latest", "current", "expected"),
        [
            ("v2026.6.0", "2026.5.0", True),
            ("v2026.5.0", "2026.5.0.dev0", True),
            ("v2026.5.0", "2026.5.0.dev1", True),
            ("v2026.5.0", "2026.5.0", False),
            ("v2026.4.0", "2026.5.0.dev0", False),
            ("v2027.1.0", "2026.12.0", True),
            ("2026.5.1", "v2026.5.0", True),
            ("v2026.6.0b1", "2026.6.0", False),
            ("v2027.0.0-beta1", "2026.6.0", False),
            ("v2027.0.0-alpha.2", "2027.0.0.dev0", False),
            ("v2027.0.0-rc3", "2026.6.0", False),
        ],
    )
    def test_is_newer(self, latest, current, expected):
        """Test version comparisons including pre-releases and dev builds."""
        assert _is_newer(latest, current) is expected


class TestUpdateFetch:
    """Tests for the release fetch and caching behaviour."""

    def test_newer_release_is_reported(self):
        """Test that a newer tag produces an available update."""
        with patch(
            "httpx.get", return_value=_release_response("v9999.0.0")
        ) as mock_get:
            info = refresh_update_info()

        assert info is not None
        assert info.available is True
        assert info.latest == "v9999.0.0"
        assert "releases/tag/v9999.0.0" in info.html_url
        assert mock_get.call_args[0][0].endswith(
            "repos/exxamalte/retentacle/releases/latest"
        )
        assert mock_get.call_args.kwargs["headers"]["User-Agent"]

    def test_current_release_is_not_available(self):
        """Test an ancient tag yields cached info without an update."""
        with patch("httpx.get", return_value=_release_response("v0.0.1")):
            info = refresh_update_info()

        assert info is not None
        assert info.available is False

    def test_results_are_cached(self):
        """Test that the second call within the TTL skips the network."""
        with patch(
            "httpx.get", return_value=_release_response("v9999.0.0")
        ) as mock_get:
            refresh_update_info()
            refresh_update_info()

        assert mock_get.call_count == 1
        assert cached_update() is not None

    def test_clear_cache_forces_refetch(self):
        """Test that clearing the cache triggers a new request."""
        with patch(
            "httpx.get", return_value=_release_response("v9999.0.0")
        ) as mock_get:
            refresh_update_info()
            updates.clear_cache()
            refresh_update_info()

        assert mock_get.call_count == 2

    def test_network_errors_are_swallowed(self):
        """Test that connectivity problems yield no info and no raise."""
        with patch("httpx.get", side_effect=httpx.ConnectError("offline")):
            assert refresh_update_info() is None
        assert cached_update() is None

    def test_missing_release_returns_none(self):
        """Test repositories without GitHub releases stay silent."""
        with patch("httpx.get", return_value=_release_response("", status_code=404)):
            assert refresh_update_info() is None

    def test_prerelease_flag_is_ignored(self):
        """Test releases marked as pre-release never become the latest."""
        with patch(
            "httpx.get",
            return_value=_release_response("v9999.0.0", prerelease=True),
        ):
            assert refresh_update_info() is None

    def test_draft_flag_is_ignored(self):
        """Test draft releases are ignored."""
        with patch(
            "httpx.get",
            return_value=_release_response("v9999.0.0", draft=True),
        ):
            assert refresh_update_info() is None

    def test_prerelease_tag_without_flag_is_ignored(self):
        """Test beta-style tags are skipped even without the API flag."""
        with patch("httpx.get", return_value=_release_response("v9999.0.0-beta1")):
            assert refresh_update_info() is None

    def test_disabled_via_environment(self, monkeypatch):
        """Test the kill switch prevents any request."""
        monkeypatch.setenv("RETENTACLE_UPDATE_CHECK", "0")
        with patch("httpx.get") as mock_get:
            assert refresh_update_info() is None

        mock_get.assert_not_called()

    def test_token_and_repo_overrides(self, monkeypatch):
        """Test private repository and fork support via the environment."""
        monkeypatch.setenv("RETENTACLE_UPDATE_TOKEN", "ghp_test")
        monkeypatch.setenv("RETENTACLE_UPDATE_REPO", "acme/retentacle-fork")
        with patch("httpx.get", return_value=_release_response("v9999.0.0")) as get:
            refresh_update_info()

        assert "repos/acme/retentacle-fork/" in get.call_args[0][0]
        assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer ghp_test"


class TestUpdateInfo:
    """Tests for the UpdateInfo value object."""

    def test_available_property(self):
        """Test availability reflects the version comparison."""
        info = UpdateInfo(current="2026.5.0.dev0", latest="v2026.6.0", html_url="u")

        assert info.available is True
