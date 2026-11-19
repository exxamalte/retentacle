"""Shared Jinja2 template environment."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from .consts import APPLICATION_NAME, APPLICATION_TAGLINE

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["application_name"] = APPLICATION_NAME
templates.env.globals["application_tagline"] = APPLICATION_TAGLINE
