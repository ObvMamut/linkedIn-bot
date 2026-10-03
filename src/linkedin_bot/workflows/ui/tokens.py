"""Opaque short tokens for button payloads too big for Telegram's 64-byte callback data."""

import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any


class TokenMap:
    """In-memory and expiring: after a restart old buttons answer "expired" instead of acting."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = 24 * 3600,
        limit: int = 300,
    ) -> None:
        self.clock, self.ttl, self.limit = clock, ttl, limit
        self.entries: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    def issue(self, payload: dict[str, Any]) -> str:
        token = secrets.token_urlsafe(6)
        self.entries[token] = (self.clock() + self.ttl, payload)
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)
        return token

    def get(self, token: str) -> dict[str, Any] | None:
        entry = self.entries.get(token)
        if entry is None or entry[0] <= self.clock():
            self.entries.pop(token, None)
            return None
        return entry[1]
