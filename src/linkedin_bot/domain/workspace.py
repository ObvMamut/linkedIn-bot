"""Records the user curates: pins, saved searches and digest preferences."""

from typing import Literal

from pydantic import Field

from linkedin_bot.domain.models import Contract

TIME = r"^([01]\d|2[0-3]):[0-5]\d$"
Provider = Literal["greenhouse", "lever", "ashby"]


class Pin(Contract):
    kind: Literal["firm", "person"]
    url: str
    name: str
    note: str = Field(default="", max_length=500)
    tags: tuple[str, ...] = ()
    board_provider: Provider | None = None
    board_slug: str | None = None
    pinned_at: str


class SavedSearch(Contract):
    id: str
    name: str
    kind: Literal["people", "firms", "jobs"]
    query: str
    last_run: str | None = None


class DigestSettings(Contract):
    enabled: bool = False
    local_time: str = Field(default="18:00", pattern=TIME)
    alerts_boards: bool = False
    alerts_messages: bool = False
    quiet_start: str = Field(default="22:00", pattern=TIME)
    quiet_end: str = Field(default="08:00", pattern=TIME)
    last_digest_date: str | None = None
    last_alert_at: str | None = None
