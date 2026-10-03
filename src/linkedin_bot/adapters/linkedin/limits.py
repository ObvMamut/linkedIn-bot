"""Page-load budget, jittered pacing and cooldowns for LinkedIn reads."""

import asyncio
import math
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable

from linkedin_bot.adapters.linkedin.models import LinkedInError

HOURLY = 30
DAILY = 150
QUIET_GAP = 20.0  # loads further apart than this are not part of a burst


def default_delay() -> float:
    return random.uniform(4.0, 10.0)


class Limiter:
    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        delay: Callable[[], float] = default_delay,
    ) -> None:
        self.clock, self.sleep, self.delay = clock, sleep, delay
        self.loads: deque[float] = deque()
        self.cooldown_until = 0.0

    def trip(self, hours: int) -> None:
        self.cooldown_until = max(self.cooldown_until, self.clock() + hours * 3600)

    async def acquire(self) -> None:
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
