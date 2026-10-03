# ruff: noqa: E501  (fixture text)
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


# ---- settings-driven ranking -------------------------------------------------------------

from linkedin_bot.domain.language import detect_language  # noqa: E402


def prefs(**changes):
    base = {"roles": ("data scientist",), "locations": ("Berlin",)}
    return SearchPreferences.model_validate_json(__import__("json").dumps({**base, **changes}))


def test_old_preference_rows_still_load_and_new_fields_default_safely():
    old = SearchPreferences.model_validate_json('{"revision": 1, "roles": ["a"]}')
    assert old.priorities == ("role", "location", "firm", "recency", "language")
    assert old.must_keywords == () and old.max_age_days is None and old.interests == ""


def test_priorities_decide_which_criterion_wins():
    role_strong = posting("r", "Data Scientist", "Paris")  # role fits, location does not
    place_strong = posting("p", "Analytics Helper", "Berlin", description="data scientist tasks")
    by_role = rank([role_strong, place_strong], prefs(priorities=("role", "location")))
    by_place = rank([role_strong, place_strong], prefs(priorities=("location", "role")))
    assert by_role[0].external_id == "r"
    assert by_place[0].external_id == "p"


def test_must_keywords_blocked_firms_and_exclusions_are_hard_filters():
    items = [
        posting("a", "Data Scientist", "Berlin", description="python and security work"),
        posting("b", "Data Scientist", "Berlin", description="excel only"),
        posting(
            "c", "Data Scientist", "Berlin", description="python security", company="Shady Corp"
        ),
        posting("d", "Data Scientist", "Berlin", description="python security gambling"),
    ]
    matches = rank(
        items, prefs(must_keywords=("python",), blocked_firms=("shady",), exclusions=("gambling",))
    )
    assert [m.external_id for m in matches] == ["a"]


def test_pinned_firms_get_a_visible_boost():
    items = [
        posting("a", "Data Scientist", "Berlin", company="Acme"),
        posting("b", "Data Scientist", "Berlin", company="Beta"),
    ]
    plain = rank(items, prefs())
    boosted = rank(items, prefs(), pinned=("beta",))
    assert boosted[0].external_id == "b" and "pinned firm" in boosted[0].reasons
    assert plain[0].score == plain[1].score


def test_nice_keywords_and_seniority_shape_the_score_and_gaps():
    items = [
        posting("a", "Senior Data Scientist", "Berlin", description="python"),
        posting("b", "Junior Data Scientist", "Berlin", description="python rust"),
    ]
    matches = rank(items, prefs(seniority=("junior",), nice_keywords=("python", "rust")))
    assert matches[0].external_id == "b"
    assert any("seniority" in gap for gap in matches[1].gaps)


def test_recency_filters_old_postings_and_favours_fresh_ones():
    fresh = posting("f", "Data Scientist", "Berlin")
    old = posting("o", "Data Scientist", "Berlin", observed_at=now() - timedelta(days=40))
    assert [m.external_id for m in rank([fresh, old], prefs(max_age_days=30))] == ["f"]
    both = rank([old, fresh], prefs())
    assert both[0].external_id == "f"


def test_language_filter_only_rejects_confident_detections():
    english = posting(
        "e",
        "Data Scientist",
        "Berlin",
        description="We are looking for a person with experience in the field and the team",
    )
    german = posting(
        "g",
        "Data Scientist",
        "Berlin",
        description="Wir suchen eine Person mit Erfahrung und der Fähigkeit im Team zu arbeiten",
    )
    short = posting("s", "Data Scientist", "Berlin", description="ML")
    matches = rank([english, german, short], prefs(languages=("en",)))
    assert {m.external_id for m in matches} == {"e", "s"}  # too little text to judge passes


def test_detect_language_handles_each_supported_language_and_unknowns():
    samples = {
        "en": "the team is looking for a person who has experience with the tools",
        "fr": "nous recherchons une personne avec de l'expérience dans le domaine et les outils",
        "de": "wir suchen eine Person mit Erfahrung und der Fähigkeit im Team zu arbeiten",
        "es": "buscamos una persona con experiencia en el área y las herramientas del equipo",
        "it": "cerchiamo una persona con esperienza nel settore e nelle competenze del gruppo",
        "pl": "szukamy osoby z doświadczeniem w tej dziedzinie oraz w pracy w zespole",
    }
    for code, text in samples.items():
        assert detect_language(text) == code, code
    assert detect_language("ML") is None and detect_language("") is None
