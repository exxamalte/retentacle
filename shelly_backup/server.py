"""Web server for browsing and scheduling Shelly backups."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import uvicorn

from .backup import Backup
from .configuration import Configuration
from .consts import APPLICATION_NAME
from .scheduler import BackupScheduler
from .storage import BackupStore, device_display_name

_TEMPLATES_DIR = Path(__file__).parent / "templates"
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


templates.env.filters["format_timestamp"] = _format_timestamp
templates.env.filters["format_interval"] = _format_interval


def _scheduler_status(scheduler: BackupScheduler, store: BackupStore) -> dict:
    """Return scheduler and storage status information."""
    return {
        "status": "ok",
        "started": scheduler.started,
        "running": scheduler.running,
        "last_run": scheduler.last_run.isoformat() if scheduler.last_run else None,
        "last_error": scheduler.last_error,
        "next_run": scheduler.next_run.isoformat() if scheduler.next_run else None,
        "run_count": scheduler.run_count,
        "database": str(store.path),
        "devices": len(store.devices()),
    }


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
        return RedirectResponse(
            url=f"/devices/{ip_address}?{result}=1", status_code=303
        )


def _register_routes(
    app: FastAPI, store: BackupStore, scheduler: BackupScheduler
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

    @app.post("/backup")
    async def trigger_backup(request: Request, background_tasks: BackgroundTasks):
        """Trigger a backup run in the background."""
        background_tasks.add_task(request.app.state.scheduler.run_once)
        return RedirectResponse(url="/?backup_started=1", status_code=303)

    @app.get("/health")
    async def health():
        """Return basic server and scheduler status."""
        return _scheduler_status(scheduler, store)


def create_app(
    configuration: Configuration | None = None,
    store: BackupStore | None = None,
    scheduler: BackupScheduler | None = None,
) -> FastAPI:
    """Create the Shelly Backup web application."""
    configuration = configuration or Configuration()
    store = store or BackupStore(configuration.database_path())
    scheduler = scheduler or BackupScheduler(
        Backup(configuration, store=store), configuration.interval()
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
    _register_routes(app, store, scheduler)
    return app


def run_server(configuration: Configuration) -> None:
    """Run the Shelly Backup web server."""
    uvicorn.run(
        create_app(configuration),
        host=configuration.host(),
        port=configuration.port(),
        log_level="info",
    )
