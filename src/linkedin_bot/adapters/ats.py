import html
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from linkedin_bot.adapters.public_http import AdapterError, PublicHTTP
from linkedin_bot.domain.models import CollectionPage, SourceObservation, digest, now

Provider = Literal["greenhouse", "lever", "ashby"]
# Greenhouse and Ashby answer with the whole board in one response; Lever pages.
MAX_LIMIT: dict[str, int] = {"greenhouse": 500, "ashby": 500, "lever": 100}
SCHEDULED_PAGES = 5  # Lever pages walked in one scheduled read
DESCRIPTION_LIMIT = 20_000
CONTENT_BYTES = 8_000_000  # a whole Greenhouse board with descriptions can be large


class TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("p", "br", "li", "div", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")


def plain_text(markup: str | None) -> str | None:
    """Greenhouse sends descriptions as escaped HTML; keep only readable text."""
    if not markup:
        return None
    parser = TextOnly()
    parser.feed(html.unescape(markup))
    lines = (
        re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in "".join(parser.parts).split("\n")
    )
    text = "\n".join(line for line in lines if line)
    return text[:DESCRIPTION_LIMIT] or None


def epoch_millis(value: int | None) -> str | None:
    if value is None or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, UTC).isoformat()


class Board(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    provider: Provider
    slug: str

    def endpoint(self, limit: int, cursor: str | None, *, content: bool = False) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", self.slug):
            raise AdapterError("invalid_board")
        if not 1 <= limit <= MAX_LIMIT[self.provider]:
            raise AdapterError("invalid_limit")
        if cursor is not None and (self.provider != "lever" or not cursor.isdecimal()):
            raise AdapterError("invalid_cursor")
        if self.provider == "greenhouse":
            suffix = "?content=true" if content else ""
            return f"https://boards-api.greenhouse.io/v1/boards/{self.slug}/jobs{suffix}"
        if self.provider == "lever":
            offset = int(cursor or "0")
            if offset > 10_000:
                raise AdapterError("cursor_budget")
            return f"https://api.lever.co/v0/postings/{self.slug}?mode=json&limit={limit}&skip={offset}"
        return f"https://api.ashbyhq.com/posting-api/job-board/{self.slug}"


class Wire(BaseModel):
    # Provider additions are allowed; required fields and their types stay validated.
    model_config = ConfigDict(strict=True, extra="ignore")


class Location(Wire):
    name: str | None = None


class GreenhouseJob(Wire):
    id: int
    title: str
    absolute_url: str
    location: Location
    content: str | None = None
    updated_at: str | None = None
    first_published: str | None = None


class GreenhousePage(Wire):
    jobs: list[GreenhouseJob]


class LeverCategories(Wire):
    location: str | None = None


class LeverJob(Wire):
    id: str
    text: str
    hostedUrl: str
    categories: LeverCategories
    descriptionPlain: str | None = None
    createdAt: int | None = None


class AshbyJob(Wire):
    id: str
    title: str
    jobUrl: str
    location: str | None = None
    descriptionPlain: str | None = None
    publishedAt: str | None = None
    isListed: bool = True


class AshbyPage(Wire):
    jobs: list[AshbyJob]


class ATSSource:
    def __init__(self, http: PublicHTTP, board: Board) -> None:
        self.http = http
        self.board = board

    async def collect(self, limit: int = 20, cursor: str | None = None) -> CollectionPage:
        endpoint = self.board.endpoint(limit, cursor)
        content = await self.http.get(endpoint)
        return self.normalize(content, limit, cursor)

    async def collect_board(self, cursor: str | None = None) -> CollectionPage:
        """A scheduled read: the whole board (bounded), with descriptions where offered.

        Greenhouse is asked for descriptions first and falls back to the plain list when the
        board is too large; Lever is walked page by page from `cursor`, at most a few pages.
        """
        limit = MAX_LIMIT[self.board.provider]
        if self.board.provider == "greenhouse":
            try:
                body = await self.http.get(
                    self.board.endpoint(limit, None, content=True), max_bytes=CONTENT_BYTES
                )
            except AdapterError as exc:
                if exc.code != "response_too_large":
                    raise
                body = await self.http.get(self.board.endpoint(limit, None))
            return self.normalize(body, limit)
        if self.board.provider != "lever":
            return await self.collect(limit)
        items: list[SourceObservation] = []
        page = await self.collect(limit, cursor)
        items.extend(page.items)
        for _ in range(SCHEDULED_PAGES - 1):
            if page.next_cursor is None:
                break
            page = await self.collect(limit, page.next_cursor)
            items.extend(page.items)
        return CollectionPage(
            items=tuple(items),
            coverage=page.coverage,
            next_cursor=page.next_cursor,
            observed_at=page.observed_at,
        )

    def normalize(self, content: bytes, limit: int, cursor: str | None = None) -> CollectionPage:
        self.board.endpoint(limit, cursor)
        observed = now()
        items: list[SourceObservation] = []
        try:
            if self.board.provider == "greenhouse":
                jobs = GreenhousePage.model_validate_json(content).jobs
                for job in jobs[:limit]:
                    items.append(
                        SourceObservation(
                            source="greenhouse",
                            company=self.board.slug,
                            external_id=str(job.id),
                            url=job.absolute_url,
                            title=job.title,
                            location=job.location.name,
                            description=plain_text(job.content),
                            published_at=job.first_published or job.updated_at,
                            content_hash=digest(job.model_dump_json().encode()),
                            observed_at=observed,
                        )
                    )
                more = len(jobs) > limit
                next_cursor = None
            elif self.board.provider == "lever":
                raw = json.loads(content)
                if not isinstance(raw, list):
                    raise AdapterError("invalid_response")
                lever_jobs = [LeverJob.model_validate(job) for job in raw]
                for lever in lever_jobs[:limit]:
                    items.append(
                        SourceObservation(
                            source="lever",
                            company=self.board.slug,
                            external_id=lever.id,
                            url=lever.hostedUrl,
                            title=lever.text,
                            location=lever.categories.location,
                            description=lever.descriptionPlain,
                            published_at=epoch_millis(lever.createdAt),
                            content_hash=digest(lever.model_dump_json().encode()),
                            observed_at=observed,
                        )
                    )
                more = len(lever_jobs) >= limit
                next_cursor = str(int(cursor or "0") + limit) if more else None
            else:
                ashby_jobs = [
                    job for job in AshbyPage.model_validate_json(content).jobs if job.isListed
                ]
                for ashby in ashby_jobs[:limit]:
                    items.append(
                        SourceObservation(
                            source="ashby",
                            company=self.board.slug,
                            external_id=ashby.id,
                            url=ashby.jobUrl,
                            title=ashby.title,
                            location=ashby.location,
                            description=ashby.descriptionPlain,
                            published_at=ashby.publishedAt,
                            content_hash=digest(ashby.model_dump_json().encode()),
                            observed_at=observed,
                        )
                    )
                more = len(ashby_jobs) > limit
                next_cursor = None
        except (ValidationError, ValueError, TypeError) as exc:
            raise AdapterError("invalid_response") from exc
        return CollectionPage(
            items=tuple(items),
            coverage="partial" if more else "complete",
            next_cursor=next_cursor,
            observed_at=observed,
        )
