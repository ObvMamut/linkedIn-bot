"""Read-only LinkedIn connector. It can only navigate and read page content."""

import asyncio
import re
from typing import Protocol
from urllib.parse import quote, urlsplit

from linkedin_bot.adapters.linkedin.limits import Limiter
from linkedin_bot.adapters.linkedin.models import (
    Company,
    Conversation,
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
    parse_messages,
    parse_people,
    parse_profile,
)

BASE = "https://www.linkedin.com"
CHALLENGE_COOLDOWN_HOURS = 6
RATE_LIMIT_COOLDOWN_HOURS = 1


class Page(Protocol):
    url: str

    async def goto(self, url: str) -> None: ...

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
    def __init__(self, page: Page, limiter: Limiter) -> None:
        self.page = page
        self.limiter = limiter
        self.lock = asyncio.Lock()  # reads share one page; never interleave navigations

    async def load(self, url: str) -> tuple[SessionState, str]:
        async with self.lock:
            await self.limiter.acquire()
            await self.page.goto(url)
            html = await self.page.content()
            if "too many requests" in html.lower():
                self.limiter.trip(RATE_LIMIT_COOLDOWN_HOURS)
                raise LinkedInError("rate_limited", RATE_LIMIT_COOLDOWN_HOURS * 3600)
            state = detect_state(self.page.url, html)
            if state == "challenge_required":
                self.limiter.trip(CHALLENGE_COOLDOWN_HOURS)
            return state, html

    async def read(self, url: str) -> str:
        state, html = await self.load(url)
        if state != "ready":
            raise LinkedInError(state)
        return html

    async def status(self) -> SessionState:
        state, _ = await self.load(f"{BASE}/feed/")
        return state

    async def messages(self, count: int) -> Parsed[Conversation]:
        parsed = parse_messages(await self.read(f"{BASE}/messaging/"))
        return Parsed(parsed.items[: max(1, count)], parsed.skipped)

    async def search_people(self, query: str) -> Parsed[Person]:
        keywords = quote(check_query(query), safe="")
        return parse_people(await self.read(f"{BASE}/search/results/people/?keywords={keywords}"))

    async def search_companies(self, query: str) -> Parsed[Company]:
        keywords = quote(check_query(query), safe="")
        return parse_companies(
            await self.read(f"{BASE}/search/results/companies/?keywords={keywords}")
        )

    async def profile(self, target: str | None) -> Profile:
        url = f"{BASE}/in/me/" if target is None else validate_target(target)
        if not re.fullmatch(rf"{re.escape(BASE)}/in/[^/]+/?", url):
            raise LinkedInError("invalid_target")
        return parse_profile(await self.read(url), url)
