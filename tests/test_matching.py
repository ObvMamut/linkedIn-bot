import asyncio
from datetime import timedelta

from linkedin_bot.domain.matching import rank
from linkedin_bot.domain.models import (
    CollectionPage,
    SearchPreferences,
    SourceObservation,
    digest,
    now,
)
from linkedin_bot.storage.database import Database


def posting(external_id, title, location=None, description=None, source="greenhouse", **extra):
    return SourceObservation(
        source=source,
        external_id=external_id,
        url=f"https://example.com/{external_id}",
        title=title,
        location=location,
        description=description,
        content_hash=digest(f"{external_id}{title}{location}".encode()),
        **extra,
    )


PREFS = SearchPreferences(
    roles=("machine learning engineer", "data scientist"),
    locations=("Warsaw", "Berlin"),
    remote=True,
    internship=True,
    exclusions=("crypto",),
)


def test_rank_orders_by_fit_and_explains_each_match():
    matches = rank(
        [
            posting("a", "Data Scientist Intern", "Warsaw, Poland"),
            posting("b", "Machine Learning Engineer", "Remote - EU"),
            posting("c", "Accountant", "Warsaw"),
        ],
        PREFS,
    )
    assert [m.external_id for m in matches] == ["a", "b"]
    assert matches[0].score > matches[1].score > 0
    assert any("role" in reason for reason in matches[0].reasons)
    assert any("not an internship" in gap for gap in matches[1].gaps)


def test_exclusions_remove_postings_even_when_the_role_matches():
    matches = rank(
        [
            posting("a", "Data Scientist", "Berlin", description="We build crypto exchanges"),
            posting("b", "Data Scientist", "Berlin"),
        ],
        PREFS,
    )
    assert [m.external_id for m in matches] == ["b"]


def test_unknown_location_is_a_gap_not_a_mismatch_and_empty_preferences_match_nothing():
    [match] = rank([posting("a", "Data Scientist", None)], PREFS)
    assert "location unknown" in match.gaps
    assert rank([posting("a", "Data Scientist")], SearchPreferences()) == []


def test_database_returns_latest_observation_per_posting_with_freshness(tmp_path):
    async def scenario():
        db = Database(tmp_path)
        try:
            await db.open()
            await db.schedule("w", "{}", now(), 3600)
            work = await db.claim(now(), "w")
            old = now() - timedelta(days=30)
            first = posting("a", "Data Scientist", "Berlin", observed_at=old)
            changed = posting("a", "Senior Data Scientist", "Berlin")
            stale = posting("z", "Data Scientist", "Berlin", observed_at=old)
            page = CollectionPage(items=(first, changed, stale), coverage="complete")
            await db.persist_page(work, page, now())
            current = await db.opportunities(since=now() - timedelta(days=14))
            assert [(o.external_id, o.title) for o in current] == [("a", "Senior Data Scientist")]
            everything = await db.opportunities(since=None)
            assert {o.external_id for o in everything} == {"a", "z"}
        finally:
            await db.close()

    asyncio.run(scenario())
