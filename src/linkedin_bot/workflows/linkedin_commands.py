"""Telegram `/li_*` commands. Read-only; replies carry only what the user asked for."""

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol, TypeVar

from linkedin_bot.adapters.linkedin.models import (
    Company,
    Conversation,
    Job,
    LinkedInError,
    Person,
    Profile,
    SessionState,
)
from linkedin_bot.adapters.linkedin.pages import Parsed
from linkedin_bot.domain.models import now
from linkedin_bot.storage.linkedin import LinkedInStore

T = TypeVar("T")
LAYOUT_VERIFIED = "2026-10-03"  # last live check of the page parsers; None if unchecked
LIMIT = 3900
LOGIN_HINT = "Run `linkedin-bot linkedin login` on the machine and sign in once."
USAGE = {
    "/li_messages": "Usage: /li_messages [1-10]",
    "/li_search": "Usage: /li_search people|firms <query>",
    "/li_profile": "Usage: /li_profile [https://www.linkedin.com/in/<name>]",
}


class Connector(Protocol):
    async def status(self) -> SessionState: ...
    async def messages(self, count: int, *, refresh: bool = False) -> Parsed[Conversation]: ...
    async def search_people(self, query: str, *, refresh: bool = False) -> Parsed[Person]: ...
    async def search_companies(self, query: str, *, refresh: bool = False) -> Parsed[Company]: ...
    async def search_jobs(
        self, query: str, location: str | None = None, *, refresh: bool = False
    ) -> Parsed[Job]: ...
    async def profile(self, target: str | None, *, refresh: bool = False) -> Profile: ...


Opener = Callable[[], AbstractAsyncContextManager[Connector]]


def wait(seconds: int | None) -> str:
    seconds = seconds or 0
    if seconds >= 3600:
        hours = round(seconds / 3600)
        return f"{hours} hour{'s' if hours != 1 else ''}"
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def explain(error: LinkedInError) -> str:
    match error.code:
        case "auth_required":
            return f"LinkedIn needs you to sign in. {LOGIN_HINT}"
        case "challenge_required":
            return (
                "LinkedIn is asking for a verification step. I stopped and will not touch it "
                f"for 6 hours. Complete it yourself, then: {LOGIN_HINT}"
            )
        case "rate_limited" | "cooldown":
            return (
                f"Paused to stay under LinkedIn's limits. Try again in {wait(error.retry_after)}."
            )
        case "layout_changed":
            return "LinkedIn's page layout is not what I expect, so I stopped instead of guessing."
        case "invalid_target":
            return "I only open a public profile or company URL on www.linkedin.com."
        case "invalid_query":
            return "Give a search query of 1 to 100 characters."
        case _:
            return "The LinkedIn browser session is unavailable (is another window using it?)."


def clip(text: str, size: int) -> str:
    return text if len(text) <= size else text[: size - 1] + "…"


def skipped(count: int) -> str:
    return f"\n({count} row{'s' if count != 1 else ''} skipped: unrecognised)" if count else ""


class LinkedInCommands:
    def __init__(
        self,
        opener: Opener,
        store: LinkedInStore,
        release: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.opener = opener
        self.store = store
        self.release = release

    async def handle(self, text: str) -> str:
        command, _, rest = text.partition(" ")
        rest = rest.strip()
        if command == "/li_release":
            if self.release is not None:
                await self.release()
            return "Browser released. You can run `linkedin-bot linkedin login` now."
        result, error = await self.fetch(lambda connector: self.run(connector, command, rest))
        return clip(result, LIMIT) if result is not None else (error or "")

    async def fetch(self, call: Callable[[Connector], Awaitable[T]]) -> tuple[T | None, str | None]:
        """Run `call` on the connector. Returns (result, None) or (None, short user message)."""
        try:
            async with self.opener() as connector:
                result = await call(connector)
            await self.store.record("ready", now())
            return result, None
        except LinkedInError as error:
            if error.code in ("auth_required", "challenge_required", "unavailable"):
                await self.store.record(error.code, now())
            logging.warning("", extra={"event": f"linkedin_{error.code}"})
            return None, explain(error)
        except Exception:
            logging.warning("", extra={"event": "linkedin_failed"})
            return None, explain(LinkedInError("unavailable"))

    async def run(self, connector: Connector, command: str, rest: str) -> str:
        if command == "/li_status":
            state = await connector.status()
            note = "" if state == "ready" else f"\n{LOGIN_HINT}"
            layout = (
                f"verified against live pages on {LAYOUT_VERIFIED}"
                if LAYOUT_VERIFIED
                else "unverified (not yet checked)"
            )
            return f"LinkedIn session: {state}.\nPage layout: {layout}.{note}"
        if command == "/li_messages":
            if rest and not rest.isdecimal():
                return USAGE[command]
            parsed = await connector.messages(min(10, max(1, int(rest or "5"))))
            if not parsed.items:
                return "No conversations found."
            lines = [
                f"• {c.title}{' [unread]' if c.unread else ''}\n  {clip(c.snippet, 140)}"
                for c in parsed.items
            ]
            return "\n".join(lines) + skipped(parsed.skipped)
        if command == "/li_search":
            kind, _, query = rest.partition(" ")
            if kind not in ("people", "firms") or not query.strip():
                return USAGE[command]
            if kind == "people":
                people = await connector.search_people(query)
                body = [
                    f"• {p.name} — {clip(p.headline, 100)}\n  {p.url}" for p in people.items[:10]
                ]
                return ("\n".join(body) or "No people found.") + skipped(people.skipped)
            firms = await connector.search_companies(query)
            body = [f"• {c.name} — {clip(c.tagline, 100)}\n  {c.url}" for c in firms.items[:10]]
            return ("\n".join(body) or "No firms found.") + skipped(firms.skipped)
        if command == "/li_profile":
            if rest and " " in rest:
                return USAGE[command]
            profile = await connector.profile(rest or None)
            parts = [profile.name, profile.headline, profile.location or "", profile.url]
            text = "\n".join(p for p in parts if p)
            if profile.about:
                text += f"\n\nAbout: {clip(profile.about, 600)}"
            if profile.experience:
                text += "\n\nExperience:\n" + "\n".join(f"• {e}" for e in profile.experience[:8])
            return text
        return "Unknown LinkedIn command."
