"""Typed LinkedIn records. All connector output is read-only data."""

from typing import Literal

from pydantic import Field

from linkedin_bot.domain.models import Contract

SessionState = Literal["ready", "auth_required", "challenge_required", "unavailable"]


class LinkedInError(RuntimeError):
    """`code` is the only text that may reach logs or Telegram."""

    def __init__(self, code: str, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class Conversation(Contract):
    id: str
    title: str
    snippet: str
    unread: bool = False
    when: str = ""


class Person(Contract):
    url: str
    name: str
    headline: str = ""
    location: str | None = None


class Company(Contract):
    url: str
    name: str
    tagline: str = ""


class Job(Contract):
    id: str
    url: str
    title: str
    company: str
    location: str | None = None


class Profile(Contract):
    url: str
    name: str
    headline: str = ""
    location: str | None = None
    about: str = ""
    experience: tuple[str, ...] = Field(default=(), max_length=20)
