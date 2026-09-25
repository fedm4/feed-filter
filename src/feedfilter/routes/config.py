"""Editing interests, topics and criteria from the browser, plus export and import.

Environment variables still win over the file, so the thresholds are shown read-only with
a note saying why. An editable-looking field that silently does nothing is worse than one
that says it is locked.

A malformed save or upload leaves the running config untouched: it is validated, and only
written if it parses.
"""

from __future__ import annotations

import io

import yaml
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse

from ..config_file import Config, ConfigError, load_config, parse_config, save_config
from ..i18n import Translator
from ..settings import Settings
from ..templating import render

router = APIRouter(tags=["config"])


def _redirect(message: str | None = None) -> RedirectResponse:
    return RedirectResponse("/config" + (f"?flash={message}" if message else ""), 303)


def as_yaml(config: Config) -> str:
    return yaml.safe_dump(
        {"interests": config.interests, "topics": config.topics, "criteria": config.criteria},
        allow_unicode=True,
        sort_keys=False,
    )


@router.get("/config")
def config_page(request: Request, flash: str | None = None):
    settings: Settings = request.app.state.settings
    config = load_config(settings=settings)

    return render(
        request.app.state.templates,
        request,
        "config.html",
        page="config",
        config=config,
        document=as_yaml(config),
        # Read-only, because an env var beats the file and pretending otherwise looks
        # like the editor is broken.
        thresholds={
            "FF_MIN_RELEVANCE": settings.min_relevance.value,
            "FF_MAX_SENSATIONALISM": settings.max_sensationalism.value,
            "FF_OPINION_POLICY": settings.opinion_policy.value,
            "FF_HIDE_PROMOTION": settings.hide_promotion,
            "FF_POLL_MINUTES": settings.poll_minutes,
            "FF_RETENTION_DAYS": settings.retention_days,
        },
        flash=flash,
    )


@router.post("/config")
def save(request: Request, document: str = Form(...)):
    """Validate, then write. Never the other way round."""
    translator: Translator = request.app.state.templates.env.globals["t"]
    settings: Settings = request.app.state.settings
    try:
        config = parse_config(yaml.safe_load(document))
    except (ConfigError, yaml.YAMLError) as exc:
        return _redirect(translator("config.invalid", error=str(exc)[:200]))

    save_config(config, settings=settings)
    return _redirect(translator("config.saved"))


@router.get("/config/export")
def export(request: Request) -> StreamingResponse:
    """The file itself, so a clean install can be brought up identically."""
    document = as_yaml(load_config(settings=request.app.state.settings))
    return StreamingResponse(
        io.BytesIO(document.encode("utf-8")),
        media_type="application/x-yaml",
        headers={"content-disposition": 'attachment; filename="config.yaml"'},
    )


@router.post("/config/import")
async def import_config(request: Request, file: UploadFile = File(...)):
    translator: Translator = request.app.state.templates.env.globals["t"]
    settings: Settings = request.app.state.settings
    try:
        config = parse_config(yaml.safe_load(await file.read()))
    except (ConfigError, yaml.YAMLError, UnicodeDecodeError) as exc:
        return _redirect(translator("config.import_failed", error=str(exc)[:200]))

    save_config(config, settings=settings)
    return _redirect(translator("config.imported"))
