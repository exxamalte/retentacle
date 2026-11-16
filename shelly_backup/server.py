"""Web server for browsing and scheduling Shelly backups."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import ipaddress
import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import URL
import uvicorn

from .backup import Backup
from .configuration import Configuration, parse_interval
from .consts import APPLICATION_NAME
from .discovery import discover_shelly_devices
from .scheduler import BackupScheduler
from .storage import BackupStore, device_display_name

_LOGGER = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_DISCOVERY_TIMEOUT = 5.0
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.globals["application_name"] = APPLICATION_NAME


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


def _new_discovered_ip_addresses(
    discovered: list[str], configured: list[str]
) -> list[str]:
    """Return normalized discovered addresses that are not configured yet."""
    configured_addresses = set(configured)
    new_ip_addresses: list[str] = []
    for value in discovered:
        try:
            ip_address = str(ipaddress.ip_address(str(value)))
        except ValueError:
            _LOGGER.warning("Ignoring invalid discovered IP address: %r", value)
            continue
        if (
            ip_address not in configured_addresses
            and ip_address not in new_ip_addresses
        ):
            new_ip_addresses.append(ip_address)
    return new_ip_addresses


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


def _register_device_backup_route(
    app: FastAPI, store: BackupStore, scheduler: BackupScheduler
) -> None:
    """Register the manual per-device backup route."""

    @app.post("/devices/{ip_address}/backup")
    async def backup_device(ip_address: str):
        """Back up a single device and return to its detail page."""
        if not store.by_device(ip_address):
            raise HTTPException(status_code=404, detail="Device not found")
        success = await scheduler.run_device(ip_address)
        result = "backup_done" if success else "backup_failed"
        safe_ip_address = quote(ip_address, safe="")
        return RedirectResponse(
            url=f"/devices/{safe_ip_address}?{result}=1", status_code=303
        )


def _register_backup_routes(
    app: FastAPI, store: BackupStore, scheduler: BackupScheduler
) -> None:
    """Register the global backup and status routes."""

    @app.post("/backup")
    async def trigger_backup(background_tasks: BackgroundTasks, redirect: str = "/"):
        """Back up all registered devices in the background."""
        scheduler.queue()
        background_tasks.add_task(
            scheduler.run_once, _manual_backup_ip_addresses(store)
        )
        parsed_redirect = URL(redirect)
        path = parsed_redirect.path
        target = (
            path
            if not parsed_redirect.scheme
            and not parsed_redirect.netloc
            and path.startswith("/")
            and not path.startswith("//")
            and "\\" not in path
            and "\r" not in path
            and "\n" not in path
            else "/"
        )
        return RedirectResponse(url=f"{target}?backup_started=1", status_code=303)

    @app.get("/api/status")
    async def status():
        """Return scheduler status and backup progress as JSON."""
        return _scheduler_status(scheduler, store)

    @app.get("/health")
    async def health():
        """Return basic server and scheduler status."""
        return _scheduler_status(scheduler, store)


def _settings_context(
    configuration: Configuration,
    store: BackupStore,
    scheduler: BackupScheduler,
    **overrides: object,
) -> dict[str, object]:
    """Build the template context for the settings page."""
    context: dict[str, object] = {
        "interval": scheduler.interval,
        "interval_value": _format_interval_value(scheduler.interval),
        "next_run": scheduler.next_run,
        "ip_addresses": _saved_ip_addresses(store),
        "ip_addresses_value": "",
        "fallback_ip_addresses": configuration.ip_addresses() or [],
        "discover": configuration.discover(),
        "discovered_ip_addresses": [],
        "discovery_ran": False,
        "discovery_error": None,
        "saved": False,
    }
    context.update(overrides)
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
    """Render the settings page with the current settings."""
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        status_code=status_code,
        context=_settings_context(configuration, store, scheduler, **overrides),
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
        try:
            discovered = await asyncio.to_thread(
                discover_shelly_devices, timeout=_DISCOVERY_TIMEOUT
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
        discovered_ip_addresses = _new_discovered_ip_addresses(
            discovered, _saved_ip_addresses(store)
        )
        return _render_settings(
            request,
            configuration,
            store,
            scheduler,
            discovered_ip_addresses=discovered_ip_addresses,
            discovery_ran=True,
        )

    @app.post("/settings/devices")
    async def add_devices(request: Request):
        """Add one or more device IP addresses to the settings."""
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
        return _render_settings(
            request,
            configuration,
            store,
            scheduler,
            saved=request.query_params.get("saved") == "1",
        )

    @app.post("/settings")
    async def save_settings(request: Request):
        """Validate and persist the application settings."""
        body = (await request.body()).decode("utf-8", errors="replace")
        raw_interval = parse_qs(body).get("interval", [""])[0]
        try:
            interval = parse_interval(raw_interval)
        except TypeError, ValueError:
            return _render_settings(
                request,
                configuration,
                store,
                scheduler,
                status_code=400,
                interval_value=raw_interval,
                error=(
                    "Enter a valid interval such as 30m, 1h, or 1d. "
                    "Use 0 to disable automatic backups."
                ),
            )
        settings = store.get_settings()
        settings["interval"] = interval
        store.save_settings(settings)
        await scheduler.set_interval(interval)
        return RedirectResponse(url="/settings?saved=1", status_code=303)

    _register_device_settings_routes(app, configuration, store, scheduler)


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
                "devices": store.devices(),
                "scheduler": scheduler,
                "last_backup": store.latest_backup_time(),
                "database_path": store.path,
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
            },
        )

    @app.get("/backups/{doc_id}/download")
    async def download_backup(doc_id: int):
        """Download a single backup as JSON."""
        record = store.get(doc_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Backup not found")
        filename = f"shelly-backup-{record.get('ip_address', 'device')}-{doc_id}.json"
        return JSONResponse(
            content=record,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    _register_backup_routes(app, store, scheduler)
    _register_settings_routes(app, configuration, store, scheduler)


def create_app(
    configuration: Configuration | None = None,
    store: BackupStore | None = None,
    scheduler: BackupScheduler | None = None,
) -> FastAPI:
    """Create the Shelly Backup web application."""
    configuration = configuration or Configuration()
    store = store or BackupStore(configuration.database_path())
    scheduler = scheduler or BackupScheduler(
        Backup(configuration, store=store),
        _effective_interval(configuration, store),
        ip_addresses_provider=lambda: _effective_ip_addresses(configuration, store),
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
    _register_routes(app, configuration, store, scheduler)
    return app


def run_server(configuration: Configuration) -> None:
    """Run the Shelly Backup web server."""
    uvicorn.run(
        create_app(configuration),
        host=configuration.host(),
        port=configuration.port(),
        log_level="info",
    )
