"""Building the four questions Laya is asked about every item.

No criterion text appears in this module. It all comes from `config.yaml`, because the
wording is the main tuning knob until a fine-tuned checkpoint exists and tuning it should
not need a code change.

Two shapes. A **ladder** (`score`) sends its rungs as an ordered list and Laya answers
with an expected value plus a distribution over positions; the position maps back to an
identifier through `LADDERS`. A **choice** sends `{identifier: description}` and Laya
answers with the winning identifier. So a ladder's identifiers live here, in a fixed
order, while a choice's come straight from config.
"""

from __future__ import annotations

from typing import Any

from .config_file import Config, ConfigError
from .settings import Relevance, Sensationalism

CONTENT_TYPES = ("news", "analysis", "opinion", "promotion")

#: Ladder order, lowest rung first. This is the schema: config supplies the wording for
#: each rung, never the rungs themselves, so a reordered config file cannot silently
#: change what a stored score of 2 means.
LADDERS: dict[str, tuple[str, ...]] = {
    "relevance": tuple(step.value for step in Relevance),
    "sensationalism": tuple(step.value for step in Sensationalism),
}


def _descriptions(config: Config, question: str, expected: tuple[str, ...]) -> list[str]:
    """Rung descriptions in ladder order, failing loudly on a missing one.

    A missing rung cannot be skipped: it would shift every rung above it down a position,
    so a stored score would quietly start meaning something else.
    """
    criteria = config.criteria.get(question)
    if not criteria:
        raise ConfigError(f"criteria.{question}: missing")
    missing = [name for name in expected if name not in criteria]
    if missing:
        raise ConfigError(f"criteria.{question}: missing {', '.join(missing)}")
    return [criteria[name] for name in expected]


def relevance_question(config: Config) -> dict[str, Any]:
    return {
        "type": "score",
        "instructions": (
            "How relevant is this article to a reader with the following interests?\n\n"
            f"{config.interests}"
        ),
        "criteria": _descriptions(config, "relevance", LADDERS["relevance"]),
    }


def sensationalism_question(config: Config) -> dict[str, Any]:
    return {
        "type": "score",
        "instructions": "How sensational is the tone of this article?",
        "criteria": _descriptions(config, "sensationalism", LADDERS["sensationalism"]),
    }


def content_type_question(config: Config) -> dict[str, Any]:
    criteria = config.criteria.get("content_type")
    if not criteria:
        raise ConfigError("criteria.content_type: missing")
    missing = [name for name in CONTENT_TYPES if name not in criteria]
    if missing:
        raise ConfigError(f"criteria.content_type: missing {', '.join(missing)}")
    return {
        "type": "choice",
        "instructions": "What kind of piece is this?",
        "criteria": {name: criteria[name] for name in CONTENT_TYPES},
    }


def topic_question(config: Config) -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": "Which topic does this article belong to?",
        "criteria": dict(config.topics),
    }


def build_questions(config: Config) -> dict[str, dict[str, Any]]:
    """All four, keyed the way a Verdict row is keyed."""
    return {
        "relevance": relevance_question(config),
        "sensationalism": sensationalism_question(config),
        "content_type": content_type_question(config),
        "topic": topic_question(config),
    }


def rung_label(question: str, index: int | str) -> str:
    """The identifier for a ladder position, for storing what a score meant.

    Laya keys a score's distribution by position (``"0"``, ``"1"``, ...). Storing those
    would leave a row unreadable the moment a ladder changed, so they are translated here.
    """
    ladder = LADDERS.get(question)
    if ladder is None:
        raise ConfigError(f"{question} is not a ladder")
    position = int(index)
    if not 0 <= position < len(ladder):
        raise ConfigError(f"{question}: no rung at position {position}")
    return ladder[position]


def ladder_score(question: str, distribution: dict[str, float]) -> float:
    """The expected position on a ladder, from a distribution keyed by identifier.

    Laya returns this as `score`, but a stored Verdict keeps only the distribution -- so
    that moving a threshold is a read rather than a reclassification. This recovers the
    number from what was stored.
    """
    ladder = LADDERS.get(question)
    if ladder is None:
        raise ConfigError(f"{question} is not a ladder")
    return sum(position * distribution.get(name, 0.0) for position, name in enumerate(ladder))
