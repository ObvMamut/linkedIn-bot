"""Daily upkeep while a long-running front end is open: snapshot, rotate, prune, compact.

Everything here is local: no network, no LinkedIn, nothing sent anywhere.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from linkedin_bot.domain.models import now
from linkedin_bot.storage.database import AUTO_BACKUP_PREFIX, Database, prune_backups
from linkedin_bot.storage.kv import KV

KEEP_BACKUPS = 7
VACUUM_EVERY = timedelta(days=30)


class Maintenance:
    def __init__(
        self,
        database: Database,
        timezone: str = "UTC",
        *,
        keep_backups: int = KEEP_BACKUPS,
        clock: Callable[[], datetime] = now,
    ) -> None:
        self.db, self.zone, self.keep, self.clock = (
            database,
            ZoneInfo(timezone),
            keep_backups,
            clock,
        )
        self.kv = KV(database)

    async def tick(self) -> dict[str, int] | None:
        """Once per local day; returns what was done, or None when today is already done."""
        at = self.clock()
        today = at.astimezone(self.zone).date().isoformat()
        if await self.kv.get("maintenance_date") == today:
            return None
        return await self.run(at, today)

    async def run(self, at: datetime | None = None, today: str | None = None) -> dict[str, int]:
        at = at or self.clock()
        await self.db.backup(prefix=AUTO_BACKUP_PREFIX)
        removed = await asyncio.to_thread(prune_backups, self.db.root, self.keep)
        pruned = await self.db.prune(at)
        vacuumed = 0
        last = await self.kv.get("last_vacuum")
        if last is None or at - datetime.fromisoformat(last) >= VACUUM_EVERY:
            await self.db.vacuum()
            await self.kv.set("last_vacuum", at.isoformat())
            vacuumed = 1
        await self.kv.set("maintenance_date", today or at.astimezone(self.zone).date().isoformat())
        result = {"backups_removed": removed, "vacuumed": vacuumed, **pruned}
        logging.info("", extra={"event": "maintenance_done", "count": sum(result.values())})
        return result


async def maintenance_loop(
    maintenance: Maintenance, stop: asyncio.Event, interval: float = 3600.0
) -> None:
    while not stop.is_set():
        try:
            await maintenance.tick()
        except Exception:
            logging.warning("", extra={"event": "maintenance_failed"})
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
