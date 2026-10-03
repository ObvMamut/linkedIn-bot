import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from linkedin_bot.adapters.public_http import AdapterError, PublicHTTP
from linkedin_bot.domain.models import CollectionPage, SourceObservation, digest, now

Provider = Literal["greenhouse", "lever", "ashby"]


class Board(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    provider: Provider
    slug: str

    def endpoint(self, limit: int, cursor: str | None) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", self.slug):
            raise AdapterError("invalid_board")
        if not 1 <= limit <= 100:
            raise AdapterError("invalid_limit")
        if cursor is not None and (self.provider != "lever" or not cursor.isdecimal()):
            raise AdapterError("invalid_cursor")
        if self.provider == "greenhouse":
            return f"https://boards-api.greenhouse.io/v1/boards/{self.slug}/jobs"
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
                            external_id=str(job.id),
                            url=job.absolute_url,
                            title=job.title,
                            location=job.location.name,
                            description=job.content,
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
                            external_id=lever.id,
                            url=lever.hostedUrl,
                            title=lever.text,
                            location=lever.categories.location,
                            description=lever.descriptionPlain,
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
