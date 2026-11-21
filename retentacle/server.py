"""Web server for browsing and scheduling Shelly backups."""

import asyncio
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import ipaddress
import json
import logging
from pathlib import Path
import re
from urllib.parse import parse_qs, quote, urlparse, urlsplit

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
import uvicorn

from .auth import (
    current_user,
    register_auth,
    require_admin,
    start_session,
    user_management_context,
    with_session_cookie,
)
from .backup import Backup
from .configuration import Configuration, parse_interval
from .consts import APPLICATION_NAME
from .discovery import (
    DiscoveredDevice,
    discover_shelly_device_details,
    iter_shelly_device_details,
)
from .oidc import (
    DEFAULT_SCOPES,
    OIDCError,
    clear_discovery_cache,
    discover_endpoints,
    oidc_config_from_settings,
)
from .recovery import backup_highlights, recovery_for
from .retention import RETENTION_FIELDS, RetentionPolicy, retention_policy_from_settings
from .scheduler import BackupScheduler
from .storage import BackupStore, device_display_name
from .templating import templates
from .users import UserRole, UserStore

_LOGGER = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_DISCOVERY_TIMEOUT = 5.0
_INTERVAL_PRESETS = (
    ("1h", "Every hour"),
    ("3h", "Every 3 hours"),
    ("6h", "Every 6 hours"),
    ("12h", "Every 12 hours"),
    ("1d", "Every day"),
    ("2d", "Every 2 days"),
    ("7d", "Every week"),
)
_INTERVAL_PRESET_VALUES = {value for value, _ in _INTERVAL_PRESETS}
_RETENTION_MAX = 1000


def _format_timestamp(value: datetime | str | None) -> str:
    """Format a timestamp for display."""
    if value is None:
        return "Never"
    if isinstance(value, datetime):
        timestamp = value
    else:
        try:
            timestamp = datetime.fromisoformat(value)
        except TypeError, ValueError:
            return str(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _format_interval(seconds: float) -> str:
    """Format an interval in seconds for display."""
    remaining = int(seconds)
    if remaining <= 0:
        return "disabled"
    parts: list[str] = []
    for unit_seconds, unit_name in (
        (24 * 60 * 60, "day"),
        (60 * 60, "hour"),
        (60, "minute"),
        (1, "second"),
    ):
        value, remaining = divmod(remaining, unit_seconds)
        if value:
            suffix = "" if value == 1 else "s"
            parts.append(f"{value} {unit_name}{suffix}")
    return " ".join(parts)


def _format_interval_value(seconds: float) -> str:
    """Format an interval as a compact value for a form field."""
    remaining = int(seconds)
    if remaining <= 0:
        return "0"
    for unit_seconds, suffix in (
        (24 * 60 * 60, "d"),
        (60 * 60, "h"),
        (60, "m"),
        (1, "s"),
    ):
        if remaining % unit_seconds == 0:
            return f"{remaining // unit_seconds}{suffix}"
    return str(remaining)


def _format_datetime_local(value: datetime | None) -> str:
    """Format a datetime for an HTML datetime-local input in local time."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%Y-%m-%dT%H:%M")


def _parse_datetime_local(value: str) -> datetime:
    """Parse an HTML datetime-local value as local time and return UTC."""
    try:
        timestamp = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid date and time: {value!r}") from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.astimezone()
    return timestamp.astimezone(UTC)


def _interval_form_values(interval: float) -> tuple[str, str]:
    """Return the interval preset selection and custom value for the form."""
    if interval <= 0:
        return "1h", ""
    value = _format_interval_value(interval)
    if value in _INTERVAL_PRESET_VALUES:
        return value, ""
    return "custom", value


def _saved_next_run(store: BackupStore) -> datetime | None:
    """Return the next backup time saved via the web UI, if any."""
    value = store.get_settings().get("next_run_at")
    if not isinstance(value, str) or not value:
        return None
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        _LOGGER.warning("Ignoring invalid saved next backup time: %r", value)
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _effective_schedule(
    configuration: Configuration, store: BackupStore
) -> tuple[bool, int, datetime | None]:
    """Return the effective automatic backup setting, interval, and next run."""
    interval = _effective_interval(configuration, store)
    saved_enabled = store.get_settings().get("automatic_backups")
    enabled = saved_enabled if isinstance(saved_enabled, bool) else interval > 0
    enabled = enabled and interval > 0
    return enabled, interval, _saved_next_run(store) if enabled else None


def _save_next_run(store: BackupStore, next_run: datetime | None) -> None:
    """Persist the next scheduled backup time."""
    settings = store.get_settings()
    settings["next_run_at"] = next_run.isoformat() if next_run else None
    store.save_settings(settings)


def _effective_retention(store: BackupStore) -> RetentionPolicy:
    """Return the retention policy, using defaults when none is saved."""
    return retention_policy_from_settings(store.get_settings())


def _parse_retention(values: dict[str, str]) -> RetentionPolicy:
    """Parse and validate retention values submitted by the settings form."""
    parsed: dict[str, int] = {}
    for field in RETENTION_FIELDS:
        raw_value = values.get(field, "").strip()
        if not raw_value:
            raise ValueError("Enter a whole number for every retention period.")
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ValueError("Retention periods must be whole numbers.") from exc
        if value < 0:
            raise ValueError("Retention periods cannot be negative.")
        if value > _RETENTION_MAX:
            raise ValueError(f"Retention periods cannot exceed {_RETENTION_MAX}.")
        parsed[field] = value
    policy = RetentionPolicy(**parsed)
    if not policy.enabled:
        raise ValueError(
            "Keep at least one backup by setting one of the periods above zero."
        )
    return policy


def _effective_interval(configuration: Configuration, store: BackupStore) -> int:
    """Return the backup interval, preferring settings saved via the web UI."""
    saved_interval = store.get_settings().get("interval")
    if saved_interval is not None:
        try:
            return parse_interval(saved_interval)
        except TypeError, ValueError:
            _LOGGER.warning(
                "Ignoring invalid saved backup interval: %r", saved_interval
            )
    return configuration.interval()


def _parse_ip_addresses(value: str) -> list[str]:
    """Parse and validate a whitespace or comma separated list of IP addresses."""
    candidates = value.replace(",", " ").split()
    if not candidates:
        raise ValueError("Enter at least one IP address.")
    return [str(ipaddress.ip_address(candidate)) for candidate in candidates]


def _normalize_discovered_device(
    device: DiscoveredDevice, configured: set[str]
) -> DiscoveredDevice | None:
    """Normalize a discovered device and drop invalid or configured entries."""
    try:
        ip_address = str(ipaddress.ip_address(device.ip_address))
    except ValueError:
        _LOGGER.warning("Ignoring invalid discovered IP address: %r", device.ip_address)
        return None
    if ip_address in configured:
        return None
    if ip_address == device.ip_address:
        return device
    return replace(device, ip_address=ip_address)


def _new_discovered_devices(
    discovered: list[DiscoveredDevice], configured: list[str]
) -> list[DiscoveredDevice]:
    """Return discovered devices that are not configured yet, sorted by IP."""
    configured_addresses = set(configured)
    new_devices: list[DiscoveredDevice] = []
    seen: set[str] = set()
    for device in discovered:
        normalized = _normalize_discovered_device(device, configured_addresses)
        if normalized is None or normalized.ip_address in seen:
            continue
        seen.add(normalized.ip_address)
        new_devices.append(normalized)
    return sorted(new_devices, key=lambda device: device.sort_key)


def _discovery_event(device: DiscoveredDevice) -> str:
    """Serialize a discovered device as a newline-delimited JSON event."""
    return (
        json.dumps(
            {
                "ip_address": device.ip_address,
                "name": device.name,
                "details": device.details,
                "sort_key": device.sort_key_text,
            }
        )
        + "\n"
    )


def _discover_stream(configured: list[str]) -> Iterator[str]:
    """Stream newly discovered devices as newline-delimited JSON."""
    configured_addresses = set(configured)
    seen: set[str] = set()
    try:
        for device in iter_shelly_device_details(timeout=_DISCOVERY_TIMEOUT):
            normalized = _normalize_discovered_device(device, configured_addresses)
            if normalized is None or normalized.ip_address in seen:
                continue
            seen.add(normalized.ip_address)
            yield _discovery_event(normalized)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Device discovery failed")
        yield (
            json.dumps(
                {
                    "error": (
                        "Unable to discover devices. Check the server logs and try again."
                    )
                }
            )
            + "\n"
        )


def _saved_ip_addresses(store: BackupStore) -> list[str]:
    """Return valid device IP addresses saved via the web UI."""
    saved = store.get_settings().get("ip_addresses")
    if not isinstance(saved, list):
        return []
    ip_addresses: list[str] = []
    for value in saved:
        try:
            ip_addresses.append(str(ipaddress.ip_address(str(value))))
        except ValueError:
            _LOGGER.warning("Ignoring invalid saved device IP address: %r", value)
    return ip_addresses


def _save_device_ip_addresses(store: BackupStore, raw_values: list[str]) -> str | None:
    """Add submitted device addresses and return an error message if invalid."""
    if not raw_values:
        return "Select at least one device to add."
    try:
        new_ip_addresses = _parse_ip_addresses("\n".join(raw_values))
    except ValueError:
        return (
            "Enter valid IPv4 or IPv6 addresses, one per line or separated by commas."
        )
    settings = store.get_settings()
    ip_addresses = _saved_ip_addresses(store)
    for ip_address in new_ip_addresses:
        if ip_address not in ip_addresses:
            ip_addresses.append(ip_address)
    settings["ip_addresses"] = ip_addresses
    store.save_settings(settings)
    return None


def _effective_ip_addresses(
    configuration: Configuration, store: BackupStore
) -> list[str] | None:
    """Return the devices to use for scheduled backups."""
    saved = _saved_ip_addresses(store)
    if saved:
        return saved
    return configuration.ip_addresses()


def _normalized_ip_address(value: object) -> str:
    """Return a canonical IP address string, or the original value."""
    try:
        return str(ipaddress.ip_address(str(value)))
    except ValueError:
        return str(value)


def _device_web_url(value: object) -> str | None:
    """Return the device's HTTP web UI URL, if its address is valid."""
    try:
        ip_address = ipaddress.ip_address(str(value))
    except ValueError:
        return None
    host = f"[{ip_address}]" if ip_address.version == 6 else str(ip_address)
    return f"http://{host}"


def _record_timestamp(record: dict[str, object]) -> datetime | None:
    """Return a backup record's timestamp in UTC, if valid."""
    value = record.get("timestamp")
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _recent_backup_cutoff(scheduler: BackupScheduler, now: datetime) -> datetime:
    """Return the earliest timestamp considered a recent backup."""
    if scheduler.enabled and scheduler.interval > 0:
        window = timedelta(seconds=scheduler.interval) + timedelta(minutes=5)
    else:
        window = timedelta(hours=24)
    return now - window


def _device_overviews(
    configuration: Configuration,
    store: BackupStore,
    scheduler: BackupScheduler,
    now: datetime,
) -> list[dict[str, object]]:
    """Return device summaries with automatic backup and recency indicators."""
    automatic_ip_addresses = {
        _normalized_ip_address(value)
        for value in (_effective_ip_addresses(configuration, store) or [])
    }
    cutoff = _recent_backup_cutoff(scheduler, now)
    overviews: list[dict[str, object]] = []
    for device in store.devices():
        overview = dict(device)
        ip_address = _normalized_ip_address(device.get("ip_address", ""))
        overview["automatic"] = ip_address in automatic_ip_addresses
        overview["web_url"] = _device_web_url(device.get("ip_address"))
        latest = device.get("latest")
        timestamp = _record_timestamp(latest) if isinstance(latest, dict) else None
        overview["recent"] = None if timestamp is None else timestamp >= cutoff
        overviews.append(overview)
    return overviews


templates.env.filters["format_timestamp"] = _format_timestamp
templates.env.filters["format_interval"] = _format_interval


def _scheduler_status(scheduler: BackupScheduler, store: BackupStore) -> dict:
    """Return scheduler and storage status information."""
    return {
        "status": "ok",
        "enabled": scheduler.enabled,
        "started": scheduler.started,
        "running": scheduler.running,
        "queued": scheduler.queued,
        "active": scheduler.active,
        "progress": scheduler.progress,
        "started_at": (
            scheduler.started_at.isoformat() if scheduler.started_at else None
        ),
        "last_run": scheduler.last_run.isoformat() if scheduler.last_run else None,
        "last_error": scheduler.last_error,
        "next_run": scheduler.next_run.isoformat() if scheduler.next_run else None,
        "run_count": scheduler.run_count,
        "database": str(store.path),
        "settings": str(store.settings_path),
        "devices": len(store.devices()),
    }


def _registered_ip_addresses(store: BackupStore) -> list[str] | None:
    """Return the IP addresses of registered devices, or None when empty."""
    return [str(device["ip_address"]) for device in store.devices()] or None


def _manual_backup_ip_addresses(store: BackupStore) -> list[str] | None:
    """Return the devices to use for a global manual backup."""
    saved = _saved_ip_addresses(store)
    if saved:
        return saved
    return _registered_ip_addresses(store)


_DEVICE_REDIRECT_PATTERN = re.compile(r"/devices/(?P<ip_address>[^/]+)")
_BACKUP_REDIRECT_PATTERN = re.compile(r"/backups/(?P<doc_id>[0-9]+)")


def _safe_redirect_path(value: str, default: str = "/") -> str:
    """Return an allowlisted in-application redirect path or the default."""
    parsed_redirect = urlsplit(value)
    if parsed_redirect.scheme or parsed_redirect.netloc:
        return default
    path = parsed_redirect.path
    if path == "/":
        return "/"
    if path == "/settings":
        return "/settings"
    device_match = _DEVICE_REDIRECT_PATTERN.fullmatch(path)
    if device_match:
        return "/devices/" + quote(device_match.group("ip_address"), safe="")
    backup_match = _BACKUP_REDIRECT_PATTERN.fullmatch(path)
    if backup_match:
        return "/backups/" + quote(backup_match.group("doc_id"), safe="")
    return default


def _register_device_backup_route(
    app: FastAPI, store: BackupStore, scheduler: BackupScheduler
) -> None:
    """Register the manual per-device backup route."""

    @app.post("/devices/{ip_address}/backup")
    async def backup_device(request: Request, ip_address: str, redirect: str = ""):
        """Back up a single device and return to the requesting page."""
        require_admin(request)
        records = store.by_device(ip_address)
        if not records:
            raise HTTPException(status_code=404, detail="Device not found")
        success = await scheduler.run_device(ip_address)
        result = "backup_done" if success else "backup_failed"
        stored_ip_address = str(records[0].get("ip_address", ip_address))
        target = _safe_redirect_path(
            redirect, "/devices/" + quote(stored_ip_address, safe="")
        )
        return RedirectResponse(url=f"{target}?{result}=1", status_code=303)


def _register_backup_routes(
    app: FastAPI, store: BackupStore, scheduler: BackupScheduler
) -> None:
    """Register the global backup and status routes."""

    @app.post("/backup")
    async def trigger_backup(
        request: Request, background_tasks: BackgroundTasks, redirect: str = "/"
    ):
        """Back up all registered devices in the background."""
        require_admin(request)
        scheduler.queue()
        background_tasks.add_task(
            scheduler.run_once, _manual_backup_ip_addresses(store)
        )
        target = _safe_redirect_path(redirect)
        return RedirectResponse(url=f"{target}?backup_started=1", status_code=303)

    @app.get("/api/status")
    async def status():
        """Return scheduler status and backup progress as JSON."""
        return _scheduler_status(scheduler, store)

    @app.get("/health")
    async def health():
        """Return basic server and scheduler status."""
        return _scheduler_status(scheduler, store)


def _oidc_form_context(store: BackupStore) -> dict[str, object]:
    """Return the OIDC settings for the settings form, keeping the secret."""
    config = oidc_config_from_settings(store.get_settings())
    return {
        "enabled": config.enabled,
        "issuer": config.issuer,
        "client_id": config.client_id,
        "callback_url": config.callback_url,
        "scopes": config.scopes,
        "provider_name": config.provider_name,
        "auto_create_users": config.auto_create_users,
        "disable_password_login": config.disable_password_login,
        "has_client_secret": bool(config.client_secret),
    }


def _settings_context(
    configuration: Configuration,
    store: BackupStore,
    scheduler: BackupScheduler,
) -> dict[str, object]:
    """Build the base template context for the settings page."""
    interval = scheduler.interval
    next_run = scheduler.next_run
    next_run_value = next_run
    if next_run_value is None:
        next_run_value = datetime.now(UTC) + timedelta(
            seconds=interval if interval > 0 else 3600
        )
    interval_choice, interval_custom = _interval_form_values(interval)
    context: dict[str, object] = {
        "interval": interval,
        "interval_value": _format_interval_value(interval),
        "interval_presets": _INTERVAL_PRESETS,
        "interval_choice": interval_choice,
        "interval_custom": interval_custom,
        "automatic_backups": scheduler.enabled,
        "next_run": next_run,
        "next_run_value": _format_datetime_local(next_run_value),
        "retention_values": _effective_retention(store).as_dict(),
        "ip_addresses": _saved_ip_addresses(store),
        "ip_addresses_value": "",
        "fallback_ip_addresses": configuration.ip_addresses() or [],
        "discover": configuration.discover(),
        "discovered_devices": [],
        "discovery_ran": False,
        "discovery_error": None,
        "saved": False,
        "oidc": _oidc_form_context(store),
    }
    return context


def _render_settings(
    request: Request,
    configuration: Configuration,
    store: BackupStore,
    scheduler: BackupScheduler,
    *,
    status_code: int = 200,
    **overrides: object,
):
    """Render the settings page with the current settings and user accounts."""
    context = _settings_context(configuration, store, scheduler)
    context.update(
        user_management_context(request.app.state.users, current_user(request))
    )
    context.update(overrides)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        status_code=status_code,
        context=context,
    )


def _register_device_settings_routes(
    app: FastAPI,
    configuration: Configuration,
    store: BackupStore,
    scheduler: BackupScheduler,
) -> None:
    """Register the device management routes on the settings page."""

    @app.post("/settings/discover")
    async def discover_devices(request: Request):
        """Discover Shelly devices and offer the ones not added yet."""
        require_admin(request)
        try:
            discovered = await asyncio.to_thread(
                discover_shelly_device_details, timeout=_DISCOVERY_TIMEOUT
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Device discovery failed")
            return _render_settings(
                request,
                configuration,
                store,
                scheduler,
                discovery_error=(
                    "Unable to discover devices. Check the server logs and try again."
                ),
            )
        discovered_devices = _new_discovered_devices(
            discovered, _saved_ip_addresses(store)
        )
        return _render_settings(
            request,
            configuration,
            store,
            scheduler,
            discovered_devices=discovered_devices,
            discovery_ran=True,
        )

    @app.post("/settings/discover/stream")
    async def discover_devices_stream(request: Request):
        """Stream newly discovered devices as newline-delimited JSON."""
        require_admin(request)
        return StreamingResponse(
            _discover_stream(_saved_ip_addresses(store)),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/settings/devices")
    async def add_devices(request: Request):
        """Add one or more device IP addresses to the settings."""
        require_admin(request)
        body = (await request.body()).decode("utf-8", errors="replace")
        raw_values = parse_qs(body).get("ip_addresses", [])
        error = _save_device_ip_addresses(store, raw_values)
        if error:
            return _render_settings(
                request,
                configuration,
                store,
                scheduler,
                status_code=400,
                ip_addresses_value="\n".join(raw_values),
                error=error,
            )
        return RedirectResponse(url="/settings?saved=1", status_code=303)

    @app.post("/settings/devices/remove")
    async def remove_device(request: Request):
        """Remove a device IP address from the settings."""
        require_admin(request)
        body = (await request.body()).decode("utf-8", errors="replace")
        raw_ip_address = parse_qs(body).get("ip_address", [""])[0]
        try:
            ip_address = str(ipaddress.ip_address(raw_ip_address.strip()))
        except ValueError:
            ip_address = raw_ip_address.strip()
        settings = store.get_settings()
        ip_addresses = _saved_ip_addresses(store)
        if ip_address in ip_addresses:
            ip_addresses.remove(ip_address)
        settings["ip_addresses"] = ip_addresses
        store.save_settings(settings)
        return RedirectResponse(url="/settings?saved=1", status_code=303)


def _register_retention_settings_routes(
    app: FastAPI,
    configuration: Configuration,
    store: BackupStore,
    scheduler: BackupScheduler,
) -> None:
    """Register the backup retention settings route."""

    @app.post("/settings/retention")
    async def save_retention(request: Request):
        """Validate and persist the backup retention policy."""
        require_admin(request)
        body = (await request.body()).decode("utf-8", errors="replace")
        form = parse_qs(body)
        values = {
            field: form.get(f"retention_{field}", [""])[0].strip()
            for field in RETENTION_FIELDS
        }
        try:
            policy = _parse_retention(values)
        except ValueError as exc:
            return _render_settings(
                request,
                configuration,
                store,
                scheduler,
                status_code=400,
                retention_values=values,
                error=str(exc),
            )
        settings = store.get_settings()
        settings["retention"] = policy.as_dict()
        store.save_settings(settings)
        return RedirectResponse(url="/settings?saved=1", status_code=303)


_SETTINGS_USERS_REDIRECT = "/settings?saved=1#users"


def _is_http_url(value: str) -> bool:
    """Whether the given value is an absolute http(s) URL."""
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _oidc_form_settings(
    form: dict[str, list[str]], stored_settings: dict
) -> tuple[dict, str | None]:
    """Parse and validate the OIDC settings form values.

    Returns the effective settings dict and a validation error message, if
    any. A blank client secret keeps the previously stored one.
    """
    enabled = "oidc_enabled" in form
    issuer = _parse_form_field(form, "oidc_issuer").strip().rstrip("/")
    client_id = _parse_form_field(form, "oidc_client_id").strip()
    client_secret = _parse_form_field(form, "oidc_client_secret")
    callback_url = _parse_form_field(form, "oidc_callback_url").strip()
    scopes = _parse_form_field(form, "oidc_scopes").strip()
    provider_name = _parse_form_field(form, "oidc_provider_name").strip()[:50]
    stored = oidc_config_from_settings(stored_settings)
    if not client_secret:
        client_secret = stored.client_secret
    values = {
        "enabled": enabled,
        "issuer": issuer,
        "client_id": client_id,
        "client_secret": client_secret,
        "callback_url": callback_url,
        "scopes": scopes or DEFAULT_SCOPES,
        "provider_name": provider_name,
        "auto_create_users": "oidc_auto_create_users" in form,
        "disable_password_login": "oidc_disable_password_login" in form and enabled,
    }
    error: str | None = None
    if enabled:
        if not _is_http_url(issuer):
            error = "Enter an OIDC issuer URL starting with http:// or https://."
        elif not client_id:
            error = "Enter the OIDC client ID."
        elif callback_url and not _is_http_url(callback_url):
            error = (
                "Enter the callback URL including http:// or https://,"
                " or leave it empty."
            )
        elif values["disable_password_login"] and not client_secret:
            error = "Enter the OIDC client secret before disabling password sign-in."
    return values, error


def _register_oidc_settings_routes(
    app: FastAPI,
    configuration: Configuration,
    store: BackupStore,
    scheduler: BackupScheduler,
) -> None:
    """Register the OpenID Connect settings route on the settings page."""

    @app.post("/settings/oidc")
    async def save_oidc(request: Request):
        """Validate and persist the OpenID Connect settings."""
        require_admin(request)
        body = (await request.body()).decode("utf-8", errors="replace")
        oidc, error = _oidc_form_settings(parse_qs(body), store.get_settings())
        if error is None and oidc["disable_password_login"]:
            try:
                await asyncio.to_thread(discover_endpoints, oidc["issuer"])
            except OIDCError as exc:
                error = (
                    "The OIDC provider could not be verified, so password"
                    f" sign-in stays enabled: {exc}"
                )
        if error is not None:
            return _render_settings(
                request,
                configuration,
                store,
                scheduler,
                status_code=400,
                error=error,
                oidc={
                    key: value for key, value in oidc.items() if key != "client_secret"
                }
                | {"has_client_secret": bool(oidc["client_secret"])},
            )

        settings = store.get_settings()
        settings["oidc"] = oidc
        store.save_settings(settings)
        clear_discovery_cache()
        return RedirectResponse(url="/settings?saved=1#oidc", status_code=303)


def _parse_form_field(form: dict[str, list[str]], name: str) -> str:
    """Return the last submitted value for a form field, or an empty string."""
    values = form.get(name, [])
    return values[-1] if values else ""


def _register_user_settings_routes(
    app: FastAPI,
    configuration: Configuration,
    store: BackupStore,
    scheduler: BackupScheduler,
) -> None:
    """Register the user management routes on the settings page."""

    @app.get("/users")
    async def users_redirect():
        """Redirect the legacy users page to the settings page."""
        return RedirectResponse(url="/settings", status_code=303)

    @app.post("/settings/users")
    async def add_user(request: Request):
        """Create a new user account."""
        require_admin(request)
        users = request.app.state.users
        body = (await request.body()).decode("utf-8", errors="replace")
        form = parse_qs(body)
        email = _parse_form_field(form, "email")
        password = _parse_form_field(form, "password")
        role = _parse_form_field(form, "role") or UserRole.VIEWER.value
        try:
            users.create_user(email, password, role)
        except ValueError as exc:
            return _render_settings(
                request,
                configuration,
                store,
                scheduler,
                status_code=400,
                error=str(exc),
                user_form={"email": email, "role": role},
            )
        return RedirectResponse(url=_SETTINGS_USERS_REDIRECT, status_code=303)


def _register_user_detail_settings_routes(
    app: FastAPI,
    configuration: Configuration,
    store: BackupStore,
    scheduler: BackupScheduler,
) -> None:
    """Register the per-user role, password, and delete routes."""

    @app.post("/settings/users/{user_id}/role")
    async def change_user_role(request: Request, user_id: int):
        """Change the role of a user account."""
        require_admin(request)
        users = request.app.state.users
        body = (await request.body()).decode("utf-8", errors="replace")
        role = _parse_form_field(parse_qs(body), "role")
        try:
            users.change_role(user_id, role)
        except ValueError as exc:
            return _render_settings(
                request,
                configuration,
                store,
                scheduler,
                status_code=400,
                error=str(exc),
            )
        return RedirectResponse(url=_SETTINGS_USERS_REDIRECT, status_code=303)

    @app.post("/settings/users/{user_id}/password")
    async def reset_user_password(request: Request, user_id: int):
        """Set a new password for a user account."""
        actor = require_admin(request)
        users = request.app.state.users
        body = (await request.body()).decode("utf-8", errors="replace")
        password = _parse_form_field(parse_qs(body), "password")
        try:
            users.change_password(user_id, password)
        except ValueError as exc:
            return _render_settings(
                request,
                configuration,
                store,
                scheduler,
                status_code=400,
                error=str(exc),
            )
        response = RedirectResponse(url=_SETTINGS_USERS_REDIRECT, status_code=303)
        if user_id == actor.user_id:
            # Changing your own password invalidates the current session,
            # so sign back in with a fresh one.
            current = users.get_user(user_id)
            if current is not None:
                token = start_session(users, current)
                with_session_cookie(response, token, users.session_max_age)
        return response

    @app.post("/settings/users/{user_id}/delete")
    async def delete_user(request: Request, user_id: int):
        """Delete a user account."""
        actor = require_admin(request)
        users = request.app.state.users
        try:
            users.delete_user(user_id, acting_user_id=actor.user_id)
        except ValueError as exc:
            return _render_settings(
                request,
                configuration,
                store,
                scheduler,
                status_code=400,
                error=str(exc),
            )
        return RedirectResponse(url=_SETTINGS_USERS_REDIRECT, status_code=303)


def _register_settings_routes(
    app: FastAPI,
    configuration: Configuration,
    store: BackupStore,
    scheduler: BackupScheduler,
) -> None:
    """Register the settings page routes."""

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        """Show the application settings form."""
        require_admin(request)
        return _render_settings(
            request,
            configuration,
            store,
            scheduler,
            saved=request.query_params.get("saved") == "1",
        )

    @app.post("/settings")
    async def save_settings(request: Request):
        """Validate and persist the automatic backup schedule."""
        require_admin(request)
        body = (await request.body()).decode("utf-8", errors="replace")
        form = parse_qs(body)
        automatic_backups = "automatic_backups" in form
        raw_preset = form.get("interval_preset", [""])[0].strip()
        raw_custom = form.get("interval_custom", [""])[0].strip()
        raw_interval = (
            raw_custom if raw_preset == "custom" else raw_preset or raw_custom
        )
        raw_next_run = form.get("next_run", [""])[0].strip()

        interval: int | None = None
        next_run_at: datetime | None = None
        error: str | None = None
        try:
            interval = parse_interval(raw_interval)
        except TypeError, ValueError:
            error = "Choose a valid interval such as 15m, 1h, or 1d."
        if error is None and automatic_backups:
            if interval is not None and interval <= 0:
                error = "Choose an interval greater than zero."
            elif not raw_next_run:
                error = "Choose the next backup date and time."
            else:
                try:
                    next_run_at = _parse_datetime_local(raw_next_run)
                except ValueError:
                    error = "Enter a valid next backup date and time."
        if error is not None or interval is None:
            return _render_settings(
                request,
                configuration,
                store,
                scheduler,
                status_code=400,
                automatic_backups=automatic_backups,
                interval_choice=(
                    raw_preset
                    if raw_preset in _INTERVAL_PRESET_VALUES or raw_preset == "custom"
                    else "custom"
                ),
                interval_custom=raw_custom or raw_interval,
                next_run_value=raw_next_run,
                error=error,
            )
        settings = store.get_settings()
        settings["interval"] = interval
        settings["automatic_backups"] = automatic_backups
        settings["next_run_at"] = next_run_at.isoformat() if next_run_at else None
        store.save_settings(settings)
        await scheduler.set_schedule(
            interval=interval,
            enabled=automatic_backups,
            next_run_at=next_run_at,
        )
        return RedirectResponse(url="/settings?saved=1", status_code=303)

    _register_retention_settings_routes(app, configuration, store, scheduler)
    _register_device_settings_routes(app, configuration, store, scheduler)
    _register_oidc_settings_routes(app, configuration, store, scheduler)
    _register_user_settings_routes(app, configuration, store, scheduler)
    _register_user_detail_settings_routes(app, configuration, store, scheduler)


def _backup_summaries(backups: list[dict]) -> list[dict]:
    """Build per-backup summary chips, collapsing rows identical to newer ones."""
    summaries: list[dict] = []
    previous: list[str] | None = None
    for record in backups:
        chips = backup_highlights(record)
        if previous is not None and chips == previous:
            summaries.append({"state": "unchanged", "chips": []})
        else:
            summaries.append({"state": "chips" if chips else "none", "chips": chips})
        previous = chips
    return summaries


def _register_backup_download_routes(app: FastAPI, store: BackupStore) -> None:
    """Register the raw snapshot and restore configuration download routes."""

    @app.get("/backups/{doc_id}/restore.json")
    async def download_restore_config(doc_id: int):
        """Download only the restorable configuration of a backup."""
        record = store.get(doc_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Backup not found")
        recovery = recovery_for(record)
        if not recovery.restorable:
            raise HTTPException(
                status_code=404,
                detail="This backup does not contain a restorable configuration",
            )
        return JSONResponse(
            content=recovery.config,
            headers={
                "Content-Disposition": f'attachment; filename="{recovery.restore_filename}"'
            },
        )

    @app.get("/backups/{doc_id}/download")
    async def download_backup(doc_id: int):
        """Download a single backup as JSON."""
        record = store.get(doc_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Backup not found")
        filename = f"retentacle-{record.get('ip_address', 'device')}-{doc_id}.json"
        return JSONResponse(
            content=record,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


def _register_routes(
    app: FastAPI,
    configuration: Configuration,
    store: BackupStore,
    scheduler: BackupScheduler,
) -> None:
    """Register the web routes on the application."""

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        """Show all devices with backups."""
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "devices": _device_overviews(
                    configuration, store, scheduler, datetime.now(UTC)
                ),
                "scheduler": scheduler,
                "last_backup": store.latest_backup_time(automatic_only=True),
            },
        )

    @app.get("/devices/{ip_address}", response_class=HTMLResponse)
    async def device(request: Request, ip_address: str):
        """Show all backups for a single device."""
        backups = store.by_device(ip_address)
        if not backups:
            raise HTTPException(status_code=404, detail="Device not found")
        latest = backups[0]
        return templates.TemplateResponse(
            request=request,
            name="device.html",
            context={
                "ip_address": ip_address,
                "display_name": device_display_name(latest),
                "backups": backups,
                "summaries": _backup_summaries(backups),
                "web_url": _device_web_url(ip_address),
            },
        )

    _register_device_backup_route(app, store, scheduler)

    @app.get("/backups/{doc_id}", response_class=HTMLResponse)
    async def backup(request: Request, doc_id: int):
        """Show a single backup."""
        record = store.get(doc_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Backup not found")
        return templates.TemplateResponse(
            request=request,
            name="backup.html",
            context={
                "backup": record,
                "display_name": device_display_name(record),
                "pretty_json": json.dumps(record, indent=2, sort_keys=True),
                "recovery": recovery_for(record),
            },
        )

    _register_backup_download_routes(app, store)
    _register_backup_routes(app, store, scheduler)
    _register_settings_routes(app, configuration, store, scheduler)


def create_app(
    configuration: Configuration | None = None,
    store: BackupStore | None = None,
    scheduler: BackupScheduler | None = None,
    users: UserStore | None = None,
) -> FastAPI:
    """Create the Retentacle web application."""
    configuration = configuration or Configuration()
    store = store or BackupStore(
        configuration.database_path(), configuration.settings_path()
    )
    users = users or UserStore(configuration.users_path())
    if scheduler is None:
        enabled, interval, next_run_at = _effective_schedule(configuration, store)
        scheduler = BackupScheduler(
            Backup(configuration, store=store),
            interval,
            enabled=enabled,
            next_run_at=next_run_at,
            ip_addresses_provider=lambda: _effective_ip_addresses(configuration, store),
            on_schedule_changed=lambda next_run: _save_next_run(store, next_run),
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()

    app = FastAPI(title=APPLICATION_NAME, lifespan=lifespan)
    app.state.configuration = configuration
    app.state.store = store
    app.state.scheduler = scheduler
    app.state.users = users
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    register_auth(app, users, store)
    _register_routes(app, configuration, store, scheduler)
    return app


def run_server(configuration: Configuration) -> None:
    """Run the Retentacle web server."""
    uvicorn.run(
        create_app(configuration),
        host=configuration.host(),
        port=configuration.port(),
        log_level="info",
    )
