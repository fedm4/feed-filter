"""Loading and saving `config.yaml`: interests, topics, and criteria wording.

Two files, like the catalogue. `default_config.yaml` ships beside the code;
`FF_CONFIG_PATH` (default `data/config.yaml`) is yours and untracked.

Unlike the catalogue, a missing copy is *not* an error -- it falls back to the shipped
defaults. The difference is deliberate: a feed list is a curation decision only you can
make, while these are defaults meant to work as they are. F5 will write the copy from the
UI, so most installs never create one by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .settings import Settings

DEFAULT_PATH = Path(__file__).parent / "default_config.yaml"


class ConfigError(ValueError):
    """The config file is malformed, naming what is wrong with it."""


@dataclass(frozen=True, slots=True)
class Config:
    #: Free text describing what to read, dropped into the relevance question.
    interests: str
    #: Identifier -> description, for the topic question.
    topics: dict[str, str]
    #: Question key -> {identifier -> description}. The descriptions are what the model
    #: reads; the identifiers are what gets stored.
    criteria: dict[str, dict[str, str]]


def _strings(value: object, where: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ConfigError(f"{where}: expected a non-empty mapping")
    out = {}
    for key, description in value.items():
        if not isinstance(description, str) or not description.strip():
            raise ConfigError(f"{where}.{key}: description must be non-empty text")
        out[str(key)] = " ".join(description.split())
    return out


def parse_config(document: object) -> Config:
    if not isinstance(document, dict):
        raise ConfigError("config must be a mapping")

    interests = document.get("interests")
    if not isinstance(interests, str) or not interests.strip():
        raise ConfigError("interests: expected non-empty text")

    criteria_raw = document.get("criteria")
    if not isinstance(criteria_raw, dict):
        raise ConfigError("criteria: expected a mapping")
    criteria = {str(key): _strings(value, f"criteria.{key}") for key, value in criteria_raw.items()}

    return Config(
        interests=interests.strip(),
        topics=_strings(document.get("topics"), "topics"),
        criteria=criteria,
    )


def load_config(path: Path | None = None, *, settings: Settings | None = None) -> Config:
    """The user's config if there is one, otherwise the shipped defaults."""
    target = path if path is not None else (settings or Settings()).config_path
    source = target if target.exists() else DEFAULT_PATH
    return parse_config(yaml.safe_load(source.read_text(encoding="utf-8")))


def save_config(
    config: Config, path: Path | None = None, *, settings: Settings | None = None
) -> Path:
    """Write the config out, creating the directory if needed. Returns where it went."""
    target = path if path is not None else (settings or Settings()).config_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(
            {
                "interests": config.interests,
                "topics": config.topics,
                "criteria": config.criteria,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return target
