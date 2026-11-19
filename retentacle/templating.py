"""Shared Jinja2 template environment."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from .consts import APPLICATION_NAME

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["application_name"] = APPLICATION_NAME
