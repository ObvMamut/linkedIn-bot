"""The daily digest: what is new, at pinned firms, in saved searches, and unread messages."""

import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from linkedin_bot.adapters.linkedin.pages import Parsed
from linkedin_bot.adapters.public_http import AdapterError
from linkedin_bot.domain.matching import rank, type_of
from linkedin_bot.domain.models import SourceObservation, now
from linkedin_bot.storage.database import Database
from linkedin_bot.storage.workspace import WorkspaceStore
from linkedin_bot.workflows.assess import assess_posting
from linkedin_bot.workflows.linkedin_commands import Connector, LinkedInCommands
from linkedin_bot.workflows.materials import Generator
from linkedin_bot.workflows.searching import split_job_query

MAX_SAVED_SEARCHES = 4  # with the inbox this keeps a digest to at most 5 LinkedIn page loads
MAX_ASSESSED = 3


def opening_count(count: int) -> str:
    return f"{count} opening{'s' if count != 1 else ''}"


def saved_call(kind: str, text: str) -> Callable[[Connector], Awaitable[Parsed[Any]]]:
    async def call(connector: Connector) -> Parsed[Any]:
        if kind == "people":
            return await connector.search_people(text, refresh=True)
        if kind == "firms":
            return await connector.search_companies(text, refresh=True)
        words, place = split_job_query(text)
        return await connector.search_jobs(words, place, refresh=True)

    return call


class DigestService:
    def __init__(
        self,
        database: Database,
        linkedin: LinkedInCommands | None,
        generator: Generator | None = None,
        clock: Callable[[], datetime] = now,
    ) -> None:
        self.db, self.linkedin, self.generator, self.clock = database, linkedin, generator, clock
        self.store = WorkspaceStore(database)

    async def build(self) -> str:
        at = self.clock()
        preferences = await self.db.preferences()
        pinned = await self.store.pinned_tokens()
        everything = await self.db.opportunities(since=at - timedelta(days=60))
        fresh = [o for o in everything if o.observed_at >= at - timedelta(days=1)]
        sections = [f"📰 Daily digest — {at.date().isoformat()}"]

        matches = rank(fresh, preferences, pinned, at)[:5]
        if matches:
            sections.append(
                "🏆 New matches\n"
                + "\n".join(
                    f"• {m.title} — {m.company or m.source} · {m.location or 'location unknown'} "
                    f"(score {m.score}: {', '.join(m.reasons)})\n  {m.url}"
                    for m in matches
                )
            )
        else:
            sections.append("🏆 New matches\nNothing new that fits your settings.")

        firms = [p for p in await self.store.pins("firm") if p.board_slug]
        if firms:
            lines = []
            for firm in firms:
                slug = (firm.board_slug or "").lower()
                count = sum(1 for o in everything if (o.company or "").lower() == slug)
                lines.append(f"• {firm.name}: {opening_count(count)}")
            sections.append("⭐ Pinned firms\n" + "\n".join(lines))

        if self.linkedin is not None:
            saved = await self.saved_searches()
            if saved:
                sections.append("💾 Saved searches\n" + "\n".join(saved))
            if (await self.store.digest()).alerts_messages:
                sections.append(await self.unread())

        sections.append(await self.ai_check(fresh, pinned, preferences.interests))
        return "\n\n".join(sections)

    async def saved_searches(self) -> list[str]:
        assert self.linkedin is not None
        lines: list[str] = []
        for saved in (await self.store.saved())[:MAX_SAVED_SEARCHES]:
            result, error = await self.linkedin.fetch(saved_call(saved.kind, saved.query))
            if error is not None or result is None:
                lines.append(f"• {saved.name}: unavailable")
                continue
            keys = [getattr(item, "url", "") for item in result.items]
            fresh = await self.store.record_run(saved.id, keys)
            lines.append(f"• {saved.name}: {len(fresh)} new")
        return lines

    async def unread(self) -> str:
        assert self.linkedin is not None
        result, error = await self.linkedin.fetch(lambda c: c.messages(10, refresh=True))
        if error is not None or result is None:
            return "📬 Unread messages: unavailable"
        names = [c.title for c in result.items if c.unread]
        # Names only: message text never goes into a digest.
        return f"📬 Unread messages: {len(names)}" + (f" ({', '.join(names)})" if names else "")

    async def ai_check(
        self, fresh: list[SourceObservation], pinned: set[str], interests: str
    ) -> str:
        if self.generator is None:
            return "🤖 AI check: not enabled (no local model installed)"
        if not interests.strip():
            return "🤖 AI check: add your interests in Settings to use it"
        candidates = [
            o
            for o in fresh
            if (o.company or "").lower() in pinned and type_of(o.title) == "internship"
        ][:MAX_ASSESSED]
        if not candidates:
            return "🤖 AI check: no new internships at pinned firms"
        facts = [f.value for f in await self.db.facts(confirmed_only=True) if f.value]
        lines = []
        for posting in candidates:
            try:
                verdict = await assess_posting(self.generator, posting, interests, facts)
            except AdapterError:
                logging.warning("", extra={"event": "assess_failed"})
                continue
            mark = {True: "fits", False: "does not fit", None: "unknown"}[verdict.fits]
            lines.append(f"• {posting.title} @ {posting.company}: {mark} — {verdict.reason}")
        return "🤖 AI check\n" + "\n".join(lines) if lines else "🤖 AI check: unavailable"


def hash_key(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]
