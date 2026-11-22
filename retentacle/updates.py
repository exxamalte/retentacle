"""Check GitHub for newer releases of Retentacle.

The check calls the public releases API of the project repository and
caches the result for several hours, so anonymous usage stays far below
the GitHub rate limit. Set the following environment variables to adjust:

- ``RETENTACLE_UPDATE_CHECK``: "0" disables all update checks.
- ``RETENTACLE_UPDATE_REPO``: alternate "owner/repo" (e.g. for a fork).
- ``RETENTACLE_UPDATE_TOKEN``: GitHub token used for private repositories.
"""

from dataclasses import dataclass
import importlib.metadata
import logging
import os
import re
import threading
import time

import httpx

_LOGGER = logging.getLogger(__name__)

DEFAULT_REPO = "exxamalte/retentacle"
CHECK_ENV_VAR = "RETENTACLE_UPDATE_CHECK"
TOKEN_ENV_VAR = "RETENTACLE_UPDATE_TOKEN"
REPO_ENV_VAR = "RETENTACLE_UPDATE_REPO"

CACHE_TTL = 6 * 3600.0
_TIMEOUT = 5.0
_USER_AGENT = "retentacle-update-check"


@dataclass(frozen=True)
class UpdateInfo:
    """The result of a release check against GitHub."""

    current: str
    latest: str
    html_url: str

    @property
    def available(self) -> bool:
        """Whether the latest release is newer than the running version."""
        return _is_newer(self.latest, self.current)


def _split_version(value: str) -> tuple[tuple[int, ...], bool]:
    """Return the numeric parts and the development flag of a version."""
    text = value.strip().lower().removeprefix("v")
    numbers = tuple(int(part) for part in re.findall(r"\d+", text))
    return numbers, "dev" in text


def _is_newer(candidate: str, current: str) -> bool:
    """Compare CalVer versions, treating development builds as pre-release."""
    candidate_parts, candidate_dev = _split_version(candidate)
    current_parts, current_dev = _split_version(current)
    size = max(len(candidate_parts), len(current_parts))
    normalized_candidate = candidate_parts + (0,) * (size - len(candidate_parts))
    normalized_current = current_parts + (0,) * (size - len(current_parts))
    if normalized_candidate != normalized_current:
        return normalized_candidate > normalized_current
    # Same numbers: the stable release is newer than a development build.
    return current_dev and not candidate_dev


def current_version() -> str:
    """Return the installed application version, or a fallback."""
    try:
        return importlib.metadata.version("retentacle")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


def enabled() -> bool:
    """Whether update checks are turned on via the environment."""
    value = os.environ.get(CHECK_ENV_VAR, "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


_lock = threading.Lock()
_state: dict = {"fetched_at": 0.0, "info": None}


def clear_cache() -> None:
    """Forget the cached release information."""
    with _lock:
        _state["fetched_at"] = 0.0
        _state["info"] = None


def cached_update() -> UpdateInfo | None:
    """Return the cached release information without any network access."""
    with _lock:
        return _state["info"]


def refresh_update_info() -> UpdateInfo | None:
    """Fetch the latest release when the cache is stale; never raises."""
    if not enabled():
        return None
    now = time.monotonic()
    with _lock:
        if _state["fetched_at"] and now - _state["fetched_at"] < CACHE_TTL:
            return _state["info"]

    info: UpdateInfo | None = None
    repo = os.environ.get(REPO_ENV_VAR, DEFAULT_REPO).strip("/")
    try:
        headers = {"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"}
        token = os.environ.get(TOKEN_ENV_VAR, "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = httpx.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers=headers,
            timeout=_TIMEOUT,
        )
        if response.status_code == httpx.codes.OK:
            release = response.json()
            tag = release.get("tag_name") or release.get("name") or ""
            url = release.get("html_url") or f"https://github.com/{repo}/releases"
            if isinstance(tag, str) and tag.strip():
                info = UpdateInfo(
                    current=current_version(),
                    latest=tag.strip(),
                    html_url=url if isinstance(url, str) else "",
                )
    except httpx.HTTPError, ValueError:
        _LOGGER.debug("Unable to check for Retentacle updates", exc_info=True)

    with _lock:
        _state["fetched_at"] = time.monotonic()
        _state["info"] = info
    return info
