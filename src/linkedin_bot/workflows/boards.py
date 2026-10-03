"""Find a firm's public job board (Greenhouse, Lever or Ashby) from its name. No LinkedIn access."""

import re
from collections.abc import Awaitable, Callable

from linkedin_bot.adapters.ats import ATSSource, Board
from linkedin_bot.adapters.public_http import AdapterError, PublicHTTP

PROVIDERS = ("greenhouse", "lever", "ashby")
SUFFIXES = {"inc", "llc", "ltd", "gmbh", "sa", "sas", "ag", "plc", "corp", "co", "bv", "srl", "spa"}
Probe = Callable[[str, str], Awaitable[bool]]


def slug_candidates(name: str) -> list[str]:
    """At most three likely board slugs: joined, hyphenated and first word."""
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in SUFFIXES]
    if not words:
        return []
    ordered = ["".join(words), "-".join(words), words[0]]
    return [slug for index, slug in enumerate(ordered) if slug not in ordered[:index]]


async def find_board(name: str, probe: Probe) -> tuple[str, str] | None:
    for slug in slug_candidates(name):
        for provider in PROVIDERS:
            if await probe(provider, slug):
                return provider, slug
    return None


def public_probe(http: PublicHTTP) -> Probe:
    async def probe(provider: str, slug: str) -> bool:
        try:
            board = Board.model_validate({"provider": provider, "slug": slug})
            page = await ATSSource(http, board).collect(1, None)
        except AdapterError, ValueError:
            return False
        return page.coverage != "unavailable"

    return probe
