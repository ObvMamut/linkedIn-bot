"""Read-only LinkedIn connector. It can only navigate and read page content."""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar
from urllib.parse import quote, urlsplit

from linkedin_bot.adapters.linkedin.cache import TTLCache
from linkedin_bot.adapters.linkedin.limits import Limiter
from linkedin_bot.adapters.linkedin.models import (
    Company,
    Conversation,
    Job,
    LinkedInError,
    Person,
    Profile,
    SessionState,
)
from linkedin_bot.adapters.linkedin.pages import (
    Parsed,
    canonical,
    detect_state,
    parse_companies,
    parse_jobs,
    parse_messages,
    parse_people,
    parse_profile,
)

T = TypeVar("T")
BASE = "https://www.linkedin.com"
TTL = {"messages": 60.0, "people": 300.0, "companies": 300.0, "profile": 600.0, "jobs": 300.0}
CHALLENGE_COOLDOWN_HOURS = 6
RATE_LIMIT_COOLDOWN_HOURS = 1


WAIT = {
    "messages": 'ul[aria-label="Conversation List"]',
    "people": 'main [role="listitem"] a[href*="/in/"]',
    "companies": 'main [role="listitem"] a[href*="/company/"]',
    "profile": "main h2",
    "jobs": 'main a[href*="/jobs/view/"]',
}


class Page(Protocol):
    @property
    def url(self) -> str: ...

    async def goto(self, url: str, wait_for: str | None = None) -> object: ...

    async def content(self) -> str: ...


def validate_target(target: str) -> str:
    """Only public profile and company pages on the exact host; returns the canonical URL."""
    parts = urlsplit(target.strip())
    if parts.username or parts.password or parts.port:
        raise LinkedInError("invalid_target")
    for kind in ("in", "company"):
        url = canonical(target.strip(), kind)
        if url is not None:
            return url
    raise LinkedInError("invalid_target")


def check_query(query: str) -> str:
    query = query.strip()
    if not 1 <= len(query) <= 100:
        raise LinkedInError("invalid_query")
    return query


class LinkedInConnector:
    def __init__(self, page: Page, limiter: Limiter, cache: TTLCache | None = None) -> None:
        self.page = page
        self.limiter = limiter
        self.cache = cache or TTLCache()
        self.lock = asyncio.Lock()  # reads share one page; never interleave navigations

    async def load(self, url: str, wait_for: str | None = None) -> tuple[SessionState, str]:
        async with self.lock:
            started = time.perf_counter()
            await self.limiter.acquire()
            paced = time.perf_counter()
            await self.page.goto(url, wait_for)
            html = await self.page.content()
            finished = time.perf_counter()
            logging.info(
                "", extra={"event": "linkedin_pacing", "duration_ms": int((paced - started) * 1000)}
            )
            logging.info(
                "", extra={"event": "linkedin_page", "duration_ms": int((finished - paced) * 1000)}
            )
            if "too many requests" in html.lower():
                self.limiter.trip(RATE_LIMIT_COOLDOWN_HOURS)
                raise LinkedInError("rate_limited", RATE_LIMIT_COOLDOWN_HOURS * 3600)
            state = detect_state(self.page.url, html)
            if state == "challenge_required":
                self.limiter.trip(CHALLENGE_COOLDOWN_HOURS)
            return state, html

    async def read(self, url: str, wait_for: str | None = None) -> str:
        state, html = await self.load(url, wait_for)
        if state != "ready":
            raise LinkedInError(state)
        return html

    async def cached(
        self, key: tuple[str, ...], ttl: float, refresh: bool, produce: Callable[[], Awaitable[T]]
    ) -> T:
        """Successful results only; errors propagate and are never stored."""
        if not refresh:
            hit, value = self.cache.get(key)
            if hit:
                return value  # type: ignore[no-any-return]
        value = await produce()
        self.cache.set(key, value, ttl)
        return value

    async def status(self) -> SessionState:
        state, _ = await self.load(f"{BASE}/feed/", "main")
        return state

    async def messages(self, count: int, *, refresh: bool = False) -> Parsed[Conversation]:
        async def produce() -> Parsed[Conversation]:
            return parse_messages(await self.read(f"{BASE}/messaging/", WAIT["messages"]))

        parsed = await self.cached(("messages",), TTL["messages"], refresh, produce)
        return Parsed(parsed.items[: max(1, count)], parsed.skipped)

    async def search_people(self, query: str, *, refresh: bool = False) -> Parsed[Person]:
        query = check_query(query)
        url = f"{BASE}/search/results/people/?keywords={quote(query, safe='')}"

        async def produce() -> Parsed[Person]:
            return parse_people(await self.read(url, WAIT["people"]))

        return await self.cached(("people", query.lower()), TTL["people"], refresh, produce)

    async def search_companies(self, query: str, *, refresh: bool = False) -> Parsed[Company]:
        query = check_query(query)
        url = f"{BASE}/search/results/companies/?keywords={quote(query, safe='')}"

        async def produce() -> Parsed[Company]:
            return parse_companies(await self.read(url, WAIT["companies"]))

        return await self.cached(("companies", query.lower()), TTL["companies"], refresh, produce)

    async def search_jobs(
        self, query: str, location: str | None = None, *, refresh: bool = False
    ) -> Parsed[Job]:
        query = check_query(query)
        place = (location or "").strip() or None
        if place is not None and len(place) > 60:
            raise LinkedInError("invalid_query")
        url = f"{BASE}/jobs/search/?keywords={quote(query, safe='')}"
        if place:
            url += f"&location={quote(place, safe='')}"

        async def produce() -> Parsed[Job]:
            return parse_jobs(await self.read(url, WAIT["jobs"]))

        key = ("jobs", query.lower(), (place or "").lower())
        return await self.cached(key, TTL["jobs"], refresh, produce)

    async def profile(self, target: str | None, *, refresh: bool = False) -> Profile:
        url = f"{BASE}/in/me/" if target is None else validate_target(target)
        if not re.fullmatch(rf"{re.escape(BASE)}/in/[^/]+/?", url):
            raise LinkedInError("invalid_target")

        async def produce() -> Profile:
            return parse_profile(await self.read(url, WAIT["profile"]), url)

        return await self.cached(("profile", url), TTL["profile"], refresh, produce)
