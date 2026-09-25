"""Question building, and that the criteria really do come from config."""

import pytest
import yaml

from feedfilter.config_file import (
    DEFAULT_PATH,
    ConfigError,
    load_config,
    parse_config,
    save_config,
)
from feedfilter.questions import (
    LADDERS,
    build_questions,
    relevance_question,
    rung_label,
)
from feedfilter.settings import Settings


@pytest.fixture
def config():
    return load_config(DEFAULT_PATH)


def test_the_shipped_defaults_are_valid(config) -> None:
    assert config.interests
    assert "technology" in config.topics
    assert set(config.criteria) == {"relevance", "sensationalism", "content_type"}


def test_all_four_questions_are_built(config) -> None:
    questions = build_questions(config)

    assert set(questions) == {"relevance", "sensationalism", "content_type", "topic"}
    assert questions["relevance"]["type"] == "score"
    assert questions["sensationalism"]["type"] == "score"
    assert questions["content_type"]["type"] == "choice"
    assert questions["topic"]["type"] == "choice"


def test_ladder_criteria_are_a_list_in_ladder_order(config) -> None:
    """Laya answers a score by position, so the order is the schema."""
    criteria = build_questions(config)["relevance"]["criteria"]

    assert isinstance(criteria, list)
    assert len(criteria) == len(LADDERS["relevance"])
    assert criteria[0] == config.criteria["relevance"]["irrelevant"]
    assert criteria[-1] == config.criteria["relevance"]["essential"]


def test_choice_criteria_are_keyed_by_identifier(config) -> None:
    """Never by a display string: the key is what ends up in the database."""
    criteria = build_questions(config)["content_type"]["criteria"]

    assert list(criteria) == ["news", "analysis", "opinion", "promotion"]


def test_changing_interests_changes_the_relevance_question(config) -> None:
    """The acceptance criterion: this file is the tuning knob."""
    edited = parse_config(
        {
            "interests": "Only bread baking.",
            "topics": config.topics,
            "criteria": config.criteria,
        }
    )

    assert "Only bread baking." in relevance_question(edited)["instructions"]
    assert "bread baking" not in relevance_question(config)["instructions"]


def test_topics_come_from_config(config) -> None:
    edited = parse_config(
        {
            "interests": config.interests,
            "topics": {"bread": "Baking and fermentation."},
            "criteria": config.criteria,
        }
    )

    assert build_questions(edited)["topic"]["criteria"] == {"bread": "Baking and fermentation."}


def test_no_criterion_text_is_hardcoded() -> None:
    """Grep the module: a criterion in Python would need a deploy to tune."""
    source = (DEFAULT_PATH.parent / "questions.py").read_text(encoding="utf-8")
    shipped = load_config(DEFAULT_PATH)

    for descriptions in shipped.criteria.values():
        for text in descriptions.values():
            assert text not in source


# ------------------------------------------------------------------ mapping back


def test_a_score_position_maps_to_an_identifier() -> None:
    assert rung_label("relevance", 0) == "irrelevant"
    assert rung_label("relevance", "3") == "essential"
    assert rung_label("sensationalism", 0) == "factual"


@pytest.mark.parametrize("bad", [-1, 4, 99])
def test_a_position_off_the_ladder_is_an_error(bad) -> None:
    with pytest.raises(ConfigError):
        rung_label("relevance", bad)


def test_a_choice_question_has_no_ladder() -> None:
    with pytest.raises(ConfigError, match="not a ladder"):
        rung_label("content_type", 0)


# ------------------------------------------------------------------ config loading


def test_a_missing_rung_is_refused(config) -> None:
    """Skipping one would shift every rung above it, changing what a stored score means."""
    criteria = {**config.criteria}
    criteria["relevance"] = {k: v for k, v in criteria["relevance"].items() if k != "marginal"}
    edited = parse_config(
        {"interests": config.interests, "topics": config.topics, "criteria": criteria}
    )

    with pytest.raises(ConfigError, match="missing marginal"):
        build_questions(edited)


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"interests": "", "topics": {"a": "b"}, "criteria": {}},
        {"interests": "x", "topics": {}, "criteria": {}},
        {"interests": "x", "topics": {"a": ""}, "criteria": {}},
        {"interests": "x", "topics": {"a": "b"}},
        "not a mapping",
    ],
    ids=["empty", "no interests", "no topics", "blank topic", "no criteria", "not a mapping"],
)
def test_malformed_config_is_refused(document) -> None:
    with pytest.raises(ConfigError):
        parse_config(document)


def test_a_missing_file_falls_back_to_the_defaults(tmp_path) -> None:
    """Unlike the catalogue: these are defaults meant to work, not a curation decision."""
    settings = Settings(config_path=tmp_path / "nothing" / "config.yaml")

    assert load_config(settings=settings).interests == load_config(DEFAULT_PATH).interests


def test_saving_then_loading_round_trips(tmp_path, config) -> None:
    target = tmp_path / "data" / "config.yaml"

    save_config(config, target)

    assert load_config(target) == config
    assert "interests" in yaml.safe_load(target.read_text())
