"""Page-load budget, jittered pacing and cooldowns for LinkedIn reads.

With a `BudgetFile` the budget and any cooldown survive restarts and are shared by every
process using the same data directory (the long-running bot and the CLI alike).
"""

import asyncio
import contextlib
import fcntl
import json
import logging
import math
import os
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

from linkedin_bot.adapters.linkedin.models import LinkedInError
from linkedin_bot.runtime.privacy import private_directory, safe_file

HOURLY = 30
DAILY = 150
QUIET_GAP = 20.0  # loads further apart than this are not part of a burst


def default_delay() -> float:
    return random.uniform(4.0, 10.0)


class BudgetFile:
    """A small private JSON file, read and rewritten under an exclusive kernel lock."""

    def __init__(self, root: Path) -> None:
        private_directory(root)
        self.path = safe_file(root, "linkedin-budget.json")

    @contextlib.contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = b""
            while chunk := os.read(fd, 65536):
                raw += chunk
            state: dict[str, Any] = {"loads": [], "cooldown_until": 0.0}
            try:
                loaded = json.loads(raw) if raw else {}
                loads = loaded.get("loads", [])
                state["loads"] = [float(t) for t in loads if type(t) in (int, float)][-DAILY:]
                state["cooldown_until"] = float(loaded.get("cooldown_until", 0.0))
            except ValueError, TypeError, AttributeError:
                logging.warning("", extra={"event": "linkedin_budget_unreadable"})
            yield state
            payload = json.dumps(
                {"loads": state["loads"][-DAILY:], "cooldown_until": state["cooldown_until"]}
            ).encode()
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def snapshot(self) -> dict[str, Any]:
        with self.locked() as state:
            return {"loads": list(state["loads"]), "cooldown_until": state["cooldown_until"]}


class Limiter:
    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        delay: Callable[[], float] = default_delay,
        budget: BudgetFile | None = None,
    ) -> None:
        self.clock, self.sleep, self.delay = clock, sleep, delay
        self.budget = budget
        self.loads: deque[float] = deque()
        self.cooldown_until = 0.0

    @contextlib.contextmanager
    def _shared(self) -> Iterator[None]:
        """Take the persisted view first and write ours back after, under one lock."""
        if self.budget is None:
            yield
            return
        with self.budget.locked() as state:
            self.loads = deque(sorted(state["loads"]))
            self.cooldown_until = max(self.cooldown_until, state["cooldown_until"])
            yield
            state["loads"] = list(self.loads)
            state["cooldown_until"] = self.cooldown_until

    def trip(self, hours: int) -> None:
        with self._shared():
            self.cooldown_until = max(self.cooldown_until, self.clock() + hours * 3600)

    def usage(self) -> dict[str, float | int]:
        """Loads in the last hour and day, and seconds of cooldown left (for status screens)."""
        if self.budget is not None:
            snapshot = self.budget.snapshot()
            loads = deque(sorted(snapshot["loads"]))
            cooldown = max(self.cooldown_until, snapshot["cooldown_until"])
        else:
            loads, cooldown = self.loads, self.cooldown_until
        now = self.clock()
        return {
            "hour": sum(1 for t in loads if t > now - 3600),
            "day": sum(1 for t in loads if t > now - 86_400),
            "hourly_limit": HOURLY,
            "daily_limit": DAILY,
            "cooldown_seconds": max(0, math.ceil(cooldown - now)),
        }

    async def acquire(self) -> None:
        with self._shared():
            now = self.clock()
            if now < self.cooldown_until:
                raise LinkedInError("cooldown", math.ceil(self.cooldown_until - now))
            while self.loads and self.loads[0] <= now - 86_400:
                self.loads.popleft()
            if len(self.loads) >= DAILY:
                raise LinkedInError("rate_limited", math.ceil(self.loads[0] + 86_400 - now))
            recent = [t for t in self.loads if t > now - 3600]
            if len(recent) >= HOURLY:
                raise LinkedInError("rate_limited", math.ceil(recent[0] + 3600 - now))
            previous = self.loads[-1] if self.loads else None
            self.loads.append(now)
        if previous is not None and now - previous < QUIET_GAP:
            await self.sleep(self.delay())  # pace only back-to-back loads
