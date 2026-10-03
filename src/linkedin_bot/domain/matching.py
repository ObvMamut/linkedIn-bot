"""Deterministic, explainable ranking of observed postings against confirmed preferences."""

import re
from collections.abc import Iterable

from linkedin_bot.domain.models import Contract, SearchPreferences, SourceObservation


class Match(Contract):
    source: str
    external_id: str
    url: str
    title: str
    location: str | None
    score: int
    reasons: tuple[str, ...]
    gaps: tuple[str, ...]


def words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9+#]+", text.lower()) if len(word) > 2}


def score_posting(posting: SourceObservation, preferences: SearchPreferences) -> Match | None:
    title = posting.title.lower()
    location = (posting.location or "").lower()
    haystack = " ".join((title, location, (posting.description or "").lower()))
    if any(term.lower() in haystack for term in preferences.exclusions):
        return None
    score = 0
    reasons: list[str] = []
    gaps: list[str] = []
    best_role = 0
    for role in preferences.roles:
        if role.lower() in title:
            best_role = max(best_role, 3)
        elif words(role) & words(title):
            best_role = max(best_role, 1)
    if best_role == 0:
        return None  # Without a role match nothing else is worth showing.
    score += best_role
    reasons.append("role matches your targets" if best_role == 3 else "role partly matches")
    if preferences.internship is True:
        if "intern" in title:
            score += 2
            reasons.append("internship")
        else:
            gaps.append("not an internship")
    if not location:
        gaps.append("location unknown")
    elif any(place.lower() in location for place in preferences.locations):
        score += 2
        reasons.append("location matches")
    elif preferences.remote and "remote" in location:
        score += 2
        reasons.append("remote")
    elif preferences.locations or preferences.remote:
        gaps.append("location does not match")
    return Match(
        source=posting.source,
        external_id=posting.external_id,
        url=posting.url,
        title=posting.title,
        location=posting.location,
        score=score,
        reasons=tuple(reasons),
        gaps=tuple(gaps),
    )


def rank(postings: Iterable[SourceObservation], preferences: SearchPreferences) -> list[Match]:
    matches = [m for p in postings if (m := score_posting(p, preferences)) is not None]
    return sorted(matches, key=lambda m: (-m.score, m.title, m.external_id))
