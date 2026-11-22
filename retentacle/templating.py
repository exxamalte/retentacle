"""Shared Jinja2 template environment."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi.templating import Jinja2Templates

from .consts import APPLICATION_NAME, APPLICATION_TAGLINE, PROJECT_URL
from .updates import cached_update

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _application_version() -> str:
    """Return the installed version, or a fallback for source checkouts."""
    try:
        return version("retentacle")
    except PackageNotFoundError:
        return "dev"


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["application_name"] = APPLICATION_NAME
templates.env.globals["application_tagline"] = APPLICATION_TAGLINE
templates.env.globals["application_version"] = _application_version()
templates.env.globals["project_url"] = PROJECT_URL
templates.env.globals["update_info"] = cached_update
