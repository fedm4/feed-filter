"""Application settings.

Every scalar knob is readable from an ``FF_``-prefixed environment variable, so a
deployment can be reconfigured without touching the image or the config file.

Lists and free text (sources, the interest profile, the classifier criteria) live in
``config.yaml`` instead: they are edited from the UI and do not fit sanely in a variable.
"""

from datetime import time
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Relevance(StrEnum):
    """Relevance ladder, lowest first."""

    IRRELEVANT = "irrelevant"
    MARGINAL = "marginal"
    INTERESTING = "interesting"
    ESSENTIAL = "essential"


class Sensationalism(StrEnum):
    """Sensationalism ladder, soberest first."""

    FACTUAL = "factual"
    SLANTED = "slanted"
    CLICKBAIT = "clickbait"
    TABLOID = "tabloid"


class OpinionPolicy(StrEnum):
    """What to do with items the classifier marks as opinion.

    The badge always renders; this only decides whether the item is additionally
    collapsed or hidden. Opinion is never dropped silently.
    """

    TAG = "tag"
    COLLAPSE = "collapse"
    HIDE = "hide"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FF_", extra="ignore")

    tz: str = "Europe/Madrid"
    ui_lang: str = "es"

    laya_base_url: str = "http://host.docker.internal:8000"
    # Empty means auto-route: the server picks a checkpoint from the detected language,
    # which measured better than pinning. Set it only to force one checkpoint.
    laya_model: str = ""
    laya_api_key: str | None = None
    laya_timeout_s: int = Field(default=60, gt=0)

    poll_minutes: int = Field(default=30, gt=0)
    fetch_full_text: bool = False
    retention_days: int = Field(default=30, gt=0)

    min_relevance: Relevance = Relevance.MARGINAL
    max_sensationalism: Sensationalism = Sensationalism.CLICKBAIT
    opinion_policy: OpinionPolicy = OpinionPolicy.TAG
    hide_promotion: bool = True

    digest_times: str = "08:30"
    digest_max_per_topic: int = Field(default=8, gt=0)

    db_path: Path = Path("data/feedfilter.db")
    config_path: Path = Path("data/config.yaml")
    # The curated feed list. Yours, so it lives with your data and not in the package;
    # catalog.example.yaml ships alongside the code as the starting point to copy.
    catalog_path: Path = Path("data/catalog.yaml")

    host: str = "0.0.0.0"
    port: int = Field(default=8080, gt=0, le=65535)
    log_level: str = "info"

    @field_validator("tz")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value!r}") from exc
        return value

    @field_validator("digest_times")
    @classmethod
    def _parsable_times(cls, value: str) -> str:
        for entry in value.split(","):
            time.fromisoformat(entry.strip())
        return value

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def digest_schedule(self) -> list[time]:
        """`FF_DIGEST_TIMES` as times, e.g. ``"08:30,20:00"`` -> two entries."""
        return [time.fromisoformat(entry.strip()) for entry in self.digest_times.split(",")]
