"""Duplicate and near-miss headlines, all taken from real feeds.

Every pair below was fetched from the catalogue on 2026-09-25, not invented. That matters:
the false positives are the ones nobody would think to write down, and two of them --
Clarin's dollar-quote series and Perfil's blue-rate series -- are what shaped the design.
"""

from datetime import timedelta

import pytest
from rapidfuzz import fuzz
from sqlalchemy import select

from conftest import make_item, make_source
from feedfilter.db import session_scope
from feedfilter.dedup import find_duplicate, record_alias
from feedfilter.models import Item, ItemAlias, utcnow
from feedfilter.settings import Settings

# Real cross-outlet pairs that must collapse, with the score each scored.
DUPLICATES = [
    (
        "OpenAI dejó vía libre a su IA para hackear a gobiernos y universidades",
        "OpenAI dejó vía libre a su IA para hackear a gobiernos y universidades",
    ),
    (
        "Tailandia desestima los recursos y mantiene la cadena perpetua para Daniel Sancho",
        "Tailandia mantiene la cadena perpetua a Daniel Sancho",
    ),
    (
        "Cazas de Finlandia y Suecia interceptan a un escuadrón de aviones rusos en el Báltico",
        "Cazas de Finlandia y Suecia interceptan aviones de guerra rusos sobre el Báltico",
    ),
    (
        "El Senado aprobó la reforma de la Carta Orgánica del Banco Central",
        "El Senado aprobó los cambios a la Carta Orgánica del Banco Central",
    ),
    (
        "Abás interviene por video en la ONU porque EE.UU. le negó el visado",
        "Abás interviene por vídeo en la ONU porque EEUU le ha negado el visado",
    ),
    (
        "Una vecina de Palma, desahuciada por un fondo buitre: “He pasado el verano en el coche”",
        'Una vecina de Palma, desahuciada por un fondo buitre: "Con 40 años cotizados he pasado '
        'el verano en el coche"',
    ),
]

# Real pairs that must NOT collapse. These are the expensive mistakes: a wrong merge
# hides an article, and nobody goes looking for what they cannot see.
NEAR_MISSES = [
    # Different exchange rates, published daily by every Argentine outlet. Clarin's MEP
    # against Perfil's blue: across outlets the threshold is what holds them apart.
    (
        "Dólar MEP hoy: a cuánto cotiza este viernes 25 de septiembre",
        "A cuánto cotiza el dólar blue hoy, viernes 25 de septiembre",
    ),
    # Same topic, different events.
    (
        "Detenido un joven de 20 años por matar a una mujer que ejercía la prostitución",
        "Detenido un joven por matar a puñaladas a una mujer en un hotel en Bormujos",
    ),
    (
        "El INE rebaja al 2,6% el crecimiento del PIB en el segundo trimestre",
        "El paro baja en 24.000 personas en el tercer trimestre",
    ),
]

THRESHOLD = Settings().dedup_threshold


@pytest.mark.parametrize(("left", "right"), DUPLICATES, ids=[d[0][:44] for d in DUPLICATES])
def test_real_duplicates_score_above_the_threshold(left, right) -> None:
    assert fuzz.token_set_ratio(left, right) >= THRESHOLD


@pytest.mark.parametrize(("left", "right"), NEAR_MISSES, ids=[n[0][:44] for n in NEAR_MISSES])
def test_real_near_misses_score_below_the_threshold(left, right) -> None:
    """The threshold has to hold these apart or it hides articles instead of tidying them."""
    assert fuzz.token_set_ratio(left, right) < THRESHOLD


def test_finds_a_duplicate_from_another_source(factory) -> None:
    original, copy = DUPLICATES[1]

    with session_scope(factory) as session:
        first = make_source(session, name="El Pais")
        make_item(session, first, title=original)
        second = make_source(session, name="El Mundo")

        match = find_duplicate(session, source_id=second.id, title=copy)

    assert match is not None
    assert match.item.title == original
    assert match.score >= THRESHOLD


def test_the_same_source_is_never_matched_against_itself(factory) -> None:
    """Clarin's own 'Dolar MEP' and 'Dolar cripto' score 96.6 and are different articles.

    Excluding same-source pairs removes that whole class of false positive, and costs
    nothing: one outlet republishing itself is what the exact URL matcher is for.
    """
    mep = "Dólar MEP hoy: a cuánto cotiza este viernes 25 de septiembre"
    cripto = "Dólar cripto hoy: a cuánto cotiza este viernes 25 de septiembre"
    # 96.6 in the real data, well over the threshold. The threshold cannot save this one;
    # only the source exclusion can. Perfil's own "dolar blue" vs "euro blue" is 95.5.
    assert fuzz.token_set_ratio(mep, cripto) >= THRESHOLD, "they really do score high"

    with session_scope(factory) as session:
        clarin = make_source(session, name="Clarin")
        make_item(session, clarin, title=mep)

        assert find_duplicate(session, source_id=clarin.id, title=cripto) is None


def test_a_distinct_story_finds_nothing(factory) -> None:
    with session_scope(factory) as session:
        first = make_source(session)
        make_item(session, first, title="El Senado aprobó la reforma de la Carta Orgánica")
        second = make_source(session)

        assert find_duplicate(session, source_id=second.id, title="Messi marcó dos goles") is None


def test_an_item_outside_the_window_is_not_matched(factory) -> None:
    """A cable is republished within hours; a wider window only adds coincidence."""
    original, copy = DUPLICATES[1]

    with session_scope(factory) as session:
        first = make_source(session)
        make_item(session, first, title=original, fetched_at=utcnow() - timedelta(hours=72))
        second = make_source(session)

        assert find_duplicate(session, source_id=second.id, title=copy) is None


def test_the_window_is_configurable(factory) -> None:
    original, copy = DUPLICATES[1]

    with session_scope(factory) as session:
        first = make_source(session)
        make_item(session, first, title=original, fetched_at=utcnow() - timedelta(hours=72))
        second = make_source(session)

        wide = Settings(dedup_window_hours=96)
        assert find_duplicate(session, source_id=second.id, title=copy, settings=wide) is not None


def test_the_best_match_wins_not_the_first(factory) -> None:
    """A story in five outlets should attach to the one it most resembles."""
    with session_scope(factory) as session:
        loose = make_source(session, name="Loose")
        make_item(session, loose, title="Tailandia mantiene la cadena perpetua a Daniel Sancho")
        exact = make_source(session, name="Exact")
        target = "Tailandia desestima los recursos y mantiene la cadena perpetua para Daniel Sancho"
        make_item(session, exact, title=target)

        third = make_source(session, name="Third")
        match = find_duplicate(session, source_id=third.id, title=target)

    assert match is not None
    assert match.item.title == target


def test_an_empty_title_matches_nothing(factory) -> None:
    with session_scope(factory) as session:
        first = make_source(session)
        make_item(session, first, title="Something real")
        second = make_source(session)

        assert find_duplicate(session, source_id=second.id, title="   ") is None


def test_recording_an_alias_keeps_the_other_outlet_intact(factory) -> None:
    """Nothing is discarded: their headline, their link, and the score that merged them."""
    original, copy = DUPLICATES[1]

    with session_scope(factory) as session:
        first = make_source(session, name="El Pais")
        make_item(session, first, title=original)
        second = make_source(session, name="El Mundo")
        match = find_duplicate(session, source_id=second.id, title=copy)
        record_alias(session, match, source_id=second.id, url="https://elmundo.es/a", title=copy)

    with session_scope(factory) as session:
        alias = session.scalars(select(ItemAlias)).one()
        assert alias.title == copy
        assert alias.url == "https://elmundo.es/a"
        assert alias.score >= THRESHOLD
        assert len(session.scalars(select(Item)).all()) == 1


def test_the_same_outlet_is_recorded_once(factory) -> None:
    original, copy = DUPLICATES[1]

    with session_scope(factory) as session:
        first = make_source(session)
        make_item(session, first, title=original)
        second = make_source(session)
        match = find_duplicate(session, source_id=second.id, title=copy)

        assert record_alias(session, match, source_id=second.id, url="https://a", title=copy)
        assert (
            record_alias(session, match, source_id=second.id, url="https://b", title=copy) is None
        )

    with session_scope(factory) as session:
        assert len(session.scalars(select(ItemAlias)).all()) == 1


def test_aliases_go_when_their_item_goes(factory) -> None:
    original, copy = DUPLICATES[1]

    with session_scope(factory) as session:
        first = make_source(session)
        item = make_item(session, first, title=original)
        second = make_source(session)
        match = find_duplicate(session, source_id=second.id, title=copy)
        record_alias(session, match, source_id=second.id, url="https://a", title=copy)

    with session_scope(factory) as session:
        session.delete(session.get(Item, item.id))

    with session_scope(factory) as session:
        assert session.scalars(select(ItemAlias)).all() == []
