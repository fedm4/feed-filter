"""The Jinja environment, wired to the translation catalogue.

`t` and `tl` are globals so a template never has to thread a translator through, and so
there is no excuse for writing a literal string in one.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from .i18n import Translator
from .settings import Settings

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

#: Content types that earn a glyph, because these are the two the reader most wants to
#: spot without reading. The other two get the badge alone.
BADGE_ICONS = {"opinion": "🗣️", "promotion": "📣"}


def badge_icon(content_type: str) -> str:
    return BADGE_ICONS.get(content_type, "")


def build_templates(settings: Settings | None = None) -> Jinja2Templates:
    translator = Translator(settings=(settings or Settings()))
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals.update(
        t=translator,
        tl=translator.label,
        translator=translator,
        lang=translator.lang,
        badge_icon=badge_icon,
    )
    return templates


def render(templates: Jinja2Templates, request: Request, name: str, **context: object):
    """Render with the bits every page needs already in place."""
    context.setdefault("flash", None)
    return templates.TemplateResponse(request=request, name=name, context=context)
