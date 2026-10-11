"""What the user can do, independent of the screen they do it from (Telegram or terminal).

Front ends render; everything that reads or changes state goes through here so both behave
the same way.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal

from linkedin_bot.adapters.ats import Board
from linkedin_bot.adapters.linkedin.pages import Parsed
from linkedin_bot.domain.matching import Match, rank
from linkedin_bot.domain.models import (
    DEFAULT_PRIORITIES,
    SearchPreferences,
    SourceObservation,
    digest,
    now,
)
from linkedin_bot.domain.workspace import Pin, SavedSearch
from linkedin_bot.storage.database import Database, PostingRecord, TrackerStatus
from linkedin_bot.storage.workspace import WorkspaceStore
from linkedin_bot.workflows.linkedin_commands import Connector, LinkedInCommands
from linkedin_bot.workflows.searching import split_job_query

SearchKind = Literal["people", "firms", "jobs"]
SEARCH_KINDS: tuple[SearchKind, ...] = ("people", "firms", "jobs")
LIST_FIELDS = (
    "locations",
    "roles",
    "must_keywords",
    "nice_keywords",
    "exclusions",
    "blocked_firms",
    "preferred_industries",
)
LANGUAGES = ("en", "fr", "de", "es", "it", "pl")
JOB_DAYS = 60
NOT_ENABLED = "LinkedIn is not enabled on this installation."
BoardFinder = Callable[[str], Awaitable[tuple[str, str] | None]]


@dataclass(frozen=True)
class ResultItem:
    """One search result or ranked posting, ready to show as a card or a table row."""

    kind: SearchKind
    title: str
    url: str
    name: str = ""
    lines: tuple[str, ...] = ()
    score: int | None = None
    source: str | None = None
    external_id: str | None = None
    company: str | None = None
    location: str | None = None
    reasons: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    matched: bool = True
    status: str = "new"
    note: str = ""
    first_seen: datetime | None = None
    description: str | None = None
    published_at: str | None = None

    @property
    def key(self) -> str:
        return self.url

    def card(self) -> dict[str, Any]:
        """The shape the Telegram card screens and tokens use."""
        return {
            "title": self.title,
            "lines": list(self.lines),
            "url": self.url,
            "key": self.key,
            "name": self.name,
        }


@dataclass(frozen=True)
class SavedRun:
    saved: SavedSearch | None
    new_items: list[ResultItem] = field(default_factory=list)
    error: str | None = None


def search_kind(value: str) -> SearchKind | None:
    for kind in SEARCH_KINDS:
        if kind == value:
            return kind
    return None


def split_items(text: str) -> list[str]:
    parts = [part.strip() for part in text.replace("\n", ",").split(",")]
    return [part for part in parts if 0 < len(part) <= 60][:20]


def tracked(item: ResultItem, record: PostingRecord | None) -> ResultItem:
    """Add what storage knows about the posting: your status, first sighting, description."""
    if record is None:
        return item
    return replace(
        item,
        status=record.status,
        note=record.note,
        first_seen=record.first_seen,
        description=record.observation.description,
        published_at=record.observation.published_at,
    )


def job_item(match: Match, record: PostingRecord | None = None) -> ResultItem:
    return tracked(
        ResultItem(
            kind="jobs",
            title=f"💼 {match.title}",
            url=match.url,
            name=match.company or "",
            lines=(
                f"{match.company or match.source} · {match.location or 'location unknown'}",
                f"score {match.score} — {', '.join(match.reasons)}",
                f"Gaps: {', '.join(match.gaps)}" if match.gaps else "",
            ),
            score=match.score,
            source=match.source,
            external_id=match.external_id,
            company=match.company,
            location=match.location,
            reasons=match.reasons,
            gaps=match.gaps,
        ),
        record,
    )


def outside_item(posting: SourceObservation, record: PostingRecord | None = None) -> ResultItem:
    return tracked(
        ResultItem(
            kind="jobs",
            title=f"💼 {posting.title}",
            url=posting.url,
            name=posting.company or "",
            lines=(
                f"{posting.company} · {posting.location or 'location unknown'}",
                "outside your settings",
            ),
            source=posting.source,
            external_id=posting.external_id,
            company=posting.company,
            location=posting.location,
            matched=False,
        ),
        record,
    )


def result_items(kind: SearchKind, parsed: Parsed[Any]) -> list[ResultItem]:
    if kind == "people":
        return [
            ResultItem(
                kind="people",
                title=f"👤 {p.name}",
                url=p.url,
                name=p.name,
                lines=(p.headline, p.location or ""),
                location=p.location,
            )
            for p in parsed.items
        ]
    return [
        ResultItem(kind="firms", title=f"🏢 {c.name}", url=c.url, name=c.name, lines=(c.tagline,))
        for c in parsed.items
    ]


class Operations:
    def __init__(
        self,
        database: Database,
        linkedin: LinkedInCommands | None = None,
        finder: BoardFinder | None = None,
        board_interval_seconds: int = 6 * 3600,
    ) -> None:
        self.db, self.linkedin, self.finder = database, linkedin, finder
        self.board_interval_seconds = board_interval_seconds
        self.store = WorkspaceStore(database)

    # ---- postings --------------------------------------------------------------------
    async def job_matches(self, days: int = JOB_DAYS) -> list[Match]:
        recent = await self.db.opportunities(since=now() - timedelta(days=days))
        return rank(recent, await self.db.preferences(), await self.store.pinned_tokens())

    async def ranked_jobs(
        self,
        days: int = JOB_DAYS,
        *,
        include_outside: bool = False,
        include_dismissed: bool = False,
    ) -> list[ResultItem]:
        """Best fit first, with your tracker status; optionally everything else after it."""
        rows = await self.db.postings(
            now() - timedelta(days=days), include_dismissed=include_dismissed
        )
        records: dict[tuple[str, str], PostingRecord] = {
            (r.observation.source, r.observation.external_id): r for r in rows
        }
        observations = [r.observation for r in rows]
        matches = rank(observations, await self.db.preferences(), await self.store.pinned_tokens())
        items = [job_item(m, records.get((m.source, m.external_id))) for m in matches]
        if include_outside:
            matched = {(m.source, m.external_id) for m in matches}
            items += [
                outside_item(o, records.get((o.source, o.external_id)))
                for o in observations
                if (o.source, o.external_id) not in matched
            ]
        return items

    async def set_status(
        self, source: str, external_id: str, status: TrackerStatus, note: str | None = None
    ) -> None:
        await self.db.set_status(source, external_id, status, note)

    async def tracked(self) -> list[ResultItem]:
        """Postings you marked (anything but new), most recently changed first."""
        rows = [
            r for r in await self.db.postings(None, include_dismissed=True) if r.status != "new"
        ]
        rows.sort(key=lambda r: r.status_at or r.first_seen, reverse=True)
        return [outside_item(r.observation, r) for r in rows]

    async def save_linkedin_jobs(self, parsed: Parsed[Any]) -> list[ResultItem]:
        """Store the public postings, then order them by your settings (best fit first)."""
        stamp = now()
        observations = [
            SourceObservation(
                source="linkedin",
                external_id=job.id,
                url=job.url,
                title=job.title,
                location=job.location,
                company=job.company,
                content_hash=digest(f"{job.id}|{job.title}|{job.company}|{job.location}".encode()),
                observed_at=stamp,
            )
            for job in parsed.items
        ]
        await self.db.save_observations(observations)
        matches = rank(observations, await self.db.preferences(), await self.store.pinned_tokens())
        matched = {m.external_id for m in matches}
        return [job_item(m) for m in matches] + [
            outside_item(o) for o in observations if o.external_id not in matched
        ]

    # ---- LinkedIn search -------------------------------------------------------------
    async def fetch(self, call: Callable[[Connector], Awaitable[Any]]) -> tuple[Any, str | None]:
        if self.linkedin is None:
            return None, NOT_ENABLED
        return await self.linkedin.fetch(call)

    async def search(
        self, kind: SearchKind, query: str, *, refresh: bool = False
    ) -> tuple[list[ResultItem] | None, str | None]:
        if kind == "jobs":
            words, place = split_job_query(query)
            result, error = await self.fetch(lambda c: c.search_jobs(words, place, refresh=refresh))
            if error is not None:
                return None, error
            return await self.save_linkedin_jobs(result), None
        if kind == "people":
            result, error = await self.fetch(lambda c: c.search_people(query, refresh=refresh))
        else:
            result, error = await self.fetch(lambda c: c.search_companies(query, refresh=refresh))
        if error is not None:
            return None, error
        return result_items(kind, result), None

    async def messages(self, *, refresh: bool, count: int = 10) -> tuple[Any, str | None]:
        return await self.fetch(lambda c: c.messages(count, refresh=refresh))

    # ---- saved searches --------------------------------------------------------------
    async def save_search(self, kind: SearchKind, query: str, keys: list[str]) -> str:
        if not query.strip():
            raise ValueError("empty_query")
        saved_id = await self.store.save_search(kind, query, f"{kind}: {query}")
        await self.store.record_run(saved_id, keys)
        return saved_id

    async def saved(self, saved_id: str) -> SavedSearch | None:
        return next((s for s in await self.store.saved() if s.id == saved_id), None)

    async def run_saved(self, saved_id: str) -> SavedRun:
        saved = await self.saved(saved_id)
        if saved is None:
            return SavedRun(None, error="That saved search no longer exists.")
        items, error = await self.search(saved.kind, saved.query, refresh=True)
        if error is not None or items is None:
            return SavedRun(saved, error=error or "Search failed.")
        fresh = set(await self.store.record_run(saved_id, [item.key for item in items]))
        return SavedRun(saved, [item for item in items if item.key in fresh])

    async def rename_saved(self, saved_id: str, name: str) -> None:
        name = name.strip()[:80]
        if not name:
            raise ValueError("empty_name")
        await self.db.call(
            lambda c: c.execute("UPDATE saved_searches SET name=? WHERE id=?", (name, saved_id))
        )

    # ---- pins and boards -------------------------------------------------------------
    async def pin(self, item: ResultItem) -> Pin:
        kind: Literal["firm", "person"] = "person" if item.kind == "people" else "firm"
        name = item.name or item.title
        await self.store.pin(kind, item.url, name)
        return next(p for p in await self.store.pins(kind) if p.url == item.url)

    async def schedule_board(self, provider: str, slug: str, *, due_now: bool = True) -> Board:
        board = Board.model_validate({"provider": provider, "slug": slug})
        board.endpoint(1, None)  # validates the slug before anything is stored
        await self.db.schedule(
            f"ats:{board.provider}:{board.slug}",
            board.model_dump_json(),
            now() if due_now else now() + timedelta(seconds=self.board_interval_seconds),
            self.board_interval_seconds,
        )
        return board

    async def link_board(self, pin: Pin) -> tuple[str, str] | None:
        """Find the firm's public board, remember it on the pin and schedule collection."""
        if self.finder is None:
            raise LookupError("board_finder_not_configured")
        found = await self.finder(pin.name)
        if found is None:
            return None
        provider, slug = found
        await self.store.link_board(pin.url, provider, slug)
        await self.schedule_board(provider, slug)
        return found

    # ---- settings --------------------------------------------------------------------
    async def preferences(self) -> SearchPreferences:
        return await self.db.preferences()

    async def change(
        self, change: Callable[[SearchPreferences], SearchPreferences]
    ) -> SearchPreferences:
        return await self.db.update_preferences(change)

    async def edit_list(
        self, field_name: str, *, add: list[str] | None = None, delete: int | None = None
    ) -> SearchPreferences:
        if field_name not in LIST_FIELDS:
            raise ValueError("unknown_list_field")

        def change(p: SearchPreferences) -> SearchPreferences:
            values = list(getattr(p, field_name))
            if delete is not None and 0 <= delete < len(values):
                values.pop(delete)
            lowered = {value.lower() for value in values}
            for item in add or []:
                if item.lower() not in lowered:
                    values.append(item)
                    lowered.add(item.lower())
            return p.model_copy(update={field_name: tuple(values)})

        return await self.change(change)

    async def set_list(self, field_name: str, values: list[str]) -> SearchPreferences:
        if field_name not in LIST_FIELDS:
            raise ValueError("unknown_list_field")
        cleaned: list[str] = []
        for value in values:
            value = value.strip()
            if 0 < len(value) <= 60 and value.lower() not in {v.lower() for v in cleaned}:
                cleaned.append(value)
        return await self.change(lambda p: p.model_copy(update={field_name: tuple(cleaned[:50])}))

    async def toggle_language(self, code: str) -> SearchPreferences:
        if code not in LANGUAGES:
            raise ValueError("unknown_language")

        def change(p: SearchPreferences) -> SearchPreferences:
            chosen = list(p.languages)
            if code in chosen:
                chosen.remove(code)
            else:
                chosen.append(code)
            return p.model_copy(update={"languages": tuple(chosen)})

        return await self.change(change)

    async def change_priority(self, operation: str, name: str) -> SearchPreferences:
        if name not in DEFAULT_PRIORITIES:
            raise ValueError("unknown_priority")

        def change(p: SearchPreferences) -> SearchPreferences:
            order = list(p.priorities)
            if operation == "add" and name not in order:
                order.append(name)
            elif operation == "rm" and name in order and len(order) > 1:
                order.remove(name)
            elif operation in ("up", "dn") and name in order:
                index = order.index(name)
                target = index - 1 if operation == "up" else index + 1
                if 0 <= target < len(order):
                    order[index], order[target] = order[target], order[index]
            return p.model_copy(update={"priorities": tuple(order)})

        return await self.change(change)

    async def set_remote(self, value: bool | None) -> SearchPreferences:
        return await self.change(lambda p: p.model_copy(update={"remote": value}))

    async def cycle_remote(self) -> SearchPreferences:
        cycle: dict[bool | None, bool | None] = {None: True, True: False, False: None}
        return await self.change(lambda p: p.model_copy(update={"remote": cycle[p.remote]}))

    async def set_internship(self, value: bool | None) -> SearchPreferences:
        return await self.change(lambda p: p.model_copy(update={"internship": value}))

    async def toggle_internship(self) -> SearchPreferences:
        return await self.change(
            lambda p: p.model_copy(update={"internship": None if p.internship else True})
        )

    async def set_max_age(self, days: int | None) -> SearchPreferences:
        if days is not None and not 1 <= days <= 365:
            raise ValueError("invalid_age")
        return await self.change(lambda p: p.model_copy(update={"max_age_days": days}))

    async def set_interests(self, text: str) -> SearchPreferences:
        return await self.change(lambda p: p.model_copy(update={"interests": text[:2000]}))

    async def matching_count(self, preferences: SearchPreferences | None = None) -> int:
        """How many recent postings pass the given settings (a live preview while editing)."""
        recent = await self.db.opportunities(since=now() - timedelta(days=JOB_DAYS))
        chosen = preferences or await self.db.preferences()
        return len(rank(recent, chosen, await self.store.pinned_tokens()))
