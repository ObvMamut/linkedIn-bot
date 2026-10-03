"""Deterministic, explainable ranking of observed postings against the user's hard settings."""

import re
from collections.abc import Collection, Iterable
from datetime import datetime, timedelta

from linkedin_bot.domain.language import detect_language
from linkedin_bot.domain.models import Contract, SearchPreferences, SourceObservation, now

FRESH_DAYS = 3
DEFAULT_HORIZON_DAYS = 30
SENIORITY_WORDS = {
    "intern": ("intern", "internship", "stage", "praktikum", "werkstudent"),
    "junior": ("junior", "jr", "graduate", "entry"),
    "senior": ("senior", "sr"),
    "lead": ("lead", "principal", "staff", "head", "director", "manager"),
}
TYPE_WORDS = {
    "internship": ("intern", "internship", "stage", "praktikum"),
    "part_time": ("part-time", "part time"),
    "contract": ("contract", "freelance", "contractor"),
}


class Match(Contract):
    source: str
    external_id: str
    url: str
    title: str
    location: str | None
    company: str | None = None
    score: int
    reasons: tuple[str, ...]
    gaps: tuple[str, ...]


def words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9+#]+", text.lower()) if len(word) > 2}


def has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def seniority_of(title: str) -> str:
    lowered = title.lower()
    for level, markers in SENIORITY_WORDS.items():
        if any(has_word(lowered, marker) for marker in markers):
            return level
    return "mid"


def type_of(title: str) -> str:
    lowered = title.lower()
    for kind, markers in TYPE_WORDS.items():
        if any(marker in lowered for marker in markers):
            return kind
    return "full_time"


def weights(preferences: SearchPreferences) -> dict[str, int]:
    count = len(preferences.priorities)
    return {name: count - index for index, name in enumerate(preferences.priorities)}


def age_days(posting: SourceObservation, at: datetime) -> float:
    stamp = posting.observed_at
    if posting.published_at:
        try:
            parsed = datetime.fromisoformat(posting.published_at.replace("Z", "+00:00"))
            stamp = parsed if parsed.utcoffset() is not None else parsed.astimezone()
        except ValueError:
            pass
    return max(0.0, (at - stamp) / timedelta(days=1))


def score_posting(
    posting: SourceObservation,
    preferences: SearchPreferences,
    pinned: Collection[str] = (),
    at: datetime | None = None,
) -> Match | None:
    at = at or now()
    title = posting.title.lower()
    location = (posting.location or "").lower()
    description = (posting.description or "").lower()
    company = (posting.company or "").lower()
    haystack = " ".join((title, location, description, company))
    # Hard filters: a posting failing any of these is never shown.
    if any(term.lower() in haystack for term in preferences.exclusions):
        return None
    if any(
        term.lower() in f"{company} {posting.url.lower()}" for term in preferences.blocked_firms
    ):
        return None
    if any(term.lower() not in haystack for term in preferences.must_keywords):
        return None
    age = age_days(posting, at)
    if preferences.max_age_days is not None and age > preferences.max_age_days:
        return None
    if preferences.languages:
        detected = detect_language(description)
        if detected is not None and detected not in preferences.languages:
            return None

    reasons: list[str] = []
    gaps: list[str] = []
    fractions: dict[str, float] = {}

    # Role: the title decides most; keywords and type/seniority refine it.
    role = 0.0
    for phrase in preferences.roles:
        if phrase.lower() in title:
            role = max(role, 1.0)
        elif words(phrase) & words(title):
            role = max(role, 0.4)
        elif phrase.lower() in description:
            role = max(role, 0.4)
    nice = [k for k in preferences.nice_keywords if k.lower() in haystack]
    if not preferences.roles and (preferences.must_keywords or nice):
        role = 1.0
    if role == 0.0:
        return None  # nothing in the posting relates to what you are looking for
    reasons.append("role matches your targets" if role == 1.0 else "role partly matches")
    if preferences.nice_keywords:
        bonus = 0.3 * len(nice) / len(preferences.nice_keywords)
        role = min(1.0, role + bonus)
        if nice:
            reasons.append(f"keywords: {', '.join(nice)}")
    wants_internship = preferences.internship is True or "internship" in preferences.job_types
    if wants_internship:
        if type_of(posting.title) == "internship":
            reasons.append("internship")
            role = 0.6 * role + 0.4
        else:
            gaps.append("not an internship")
            role = 0.6 * role
    elif preferences.job_types and type_of(posting.title) not in preferences.job_types:
        gaps.append(f"type {type_of(posting.title)}")
        role *= 0.6
    if preferences.seniority and seniority_of(posting.title) not in preferences.seniority:
        gaps.append(f"seniority {seniority_of(posting.title)}")
        role *= 0.5
    fractions["role"] = role

    # Location.
    if not (preferences.locations or preferences.remote):
        fractions["location"] = 1.0
    elif not location:
        gaps.append("location unknown")
        fractions["location"] = 0.0
    elif any(place.lower() in location for place in preferences.locations):
        reasons.append("location matches")
        fractions["location"] = 1.0
    elif preferences.remote and "remote" in location:
        reasons.append("remote")
        fractions["location"] = 1.0
    else:
        gaps.append("location does not match")
        fractions["location"] = 0.0

    # Firm: pinned firms and preferred industries.
    firm = 0.0
    if company and any(token.lower() == company or token.lower() in company for token in pinned):
        reasons.append("pinned firm")
        firm = 1.0
    elif any(term.lower() in f"{title} {description}" for term in preferences.preferred_industries):
        reasons.append("preferred industry")
        firm = 0.6
    fractions["firm"] = firm

    # Recency: full marks while fresh, fading to zero at the horizon.
    horizon = float(preferences.max_age_days or DEFAULT_HORIZON_DAYS)
    fresh = (
        1.0
        if age <= FRESH_DAYS
        else max(0.0, 1 - (age - FRESH_DAYS) / max(1.0, horizon - FRESH_DAYS))
    )
    if age <= FRESH_DAYS:
        reasons.append("fresh")
    fractions["recency"] = fresh
    fractions["language"] = 1.0

    weight = weights(preferences)
    total = sum(weight.get(name, 0) * fraction for name, fraction in fractions.items())
    return Match(
        source=posting.source,
        external_id=posting.external_id,
        url=posting.url,
        title=posting.title,
        location=posting.location,
        company=posting.company,
        score=round(10 * total),
        reasons=tuple(reasons),
        gaps=tuple(gaps),
    )


def rank(
    postings: Iterable[SourceObservation],
    preferences: SearchPreferences,
    pinned: Collection[str] = (),
    at: datetime | None = None,
) -> list[Match]:
    matches = [m for p in postings if (m := score_posting(p, preferences, pinned, at)) is not None]
    return sorted(matches, key=lambda m: (-m.score, m.title, m.external_id))
