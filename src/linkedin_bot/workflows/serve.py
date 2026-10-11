"""Scheduled work for the long-running process: the daily digest and opt-in alerts."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from linkedin_bot.adapters.telegram import TelegramAPI
from linkedin_bot.domain.matching import rank
from linkedin_bot.domain.models import now
from linkedin_bot.domain.workspace import DigestSettings
from linkedin_bot.storage.database import Database
from linkedin_bot.storage.kv import KV
from linkedin_bot.storage.telegram import TelegramStore
from linkedin_bot.storage.workspace import WorkspaceStore
from linkedin_bot.workflows.digest import DigestService, hash_key
from linkedin_bot.workflows.linkedin_commands import LinkedInCommands

SEEN_LIMIT = 200


def in_quiet_hours(hhmm: str, start: str, end: str) -> bool:
    if start == end:
        return False  # an empty window is never quiet
    if start < end:
        return start <= hhmm < end
    return hhmm >= start or hhmm < end  # wraps midnight


class Ticker(Protocol):
    async def tick(self) -> list[str]: ...


class Notifier(Protocol):
    """Somewhere a digest or an alert can be delivered (Telegram, the terminal UI)."""

    async def ready(self) -> bool: ...

    async def send(self, text: str, kind: str) -> None: ...


class TelegramNotifier:
    """Delivers to the paired private chat; silent while unpaired."""

    def __init__(self, api: TelegramAPI, store: TelegramStore) -> None:
        self.api, self.store = api, store

    async def ready(self) -> bool:
        return await self.store.binding() is not None

    async def send(self, text: str, kind: str) -> None:
        binding = await self.store.binding()
        if binding is not None:
            await self.api.send_message(binding[1], text)


class Scheduler:
    def __init__(
        self,
        *,
        db: Database,
        digest: DigestService,
        linkedin: LinkedInCommands | None,
        timezone: str,
        message_interval_minutes: int,
        api: TelegramAPI | None = None,
        notifiers: Sequence[Notifier] = (),
        clock: Callable[[], datetime] = now,
    ) -> None:
        self.db, self.digest, self.linkedin = db, digest, linkedin
        self.zone = ZoneInfo(timezone)
        self.interval = timedelta(minutes=message_interval_minutes)
        self.clock = clock
        self.store = WorkspaceStore(db)
        self.kv = KV(db)
        self.last_message_check: datetime | None = None
        self.notifiers: list[Notifier] = list(notifiers)
        if api is not None:
            self.notifiers.append(TelegramNotifier(api, TelegramStore(db)))

    async def ready(self) -> list[Notifier]:
        return [n for n in self.notifiers if await n.ready()]

    async def deliver(self, text: str, kind: str) -> None:
        for notifier in await self.ready():
            try:
                await notifier.send(text, kind)
            except Exception:
                logging.warning("", extra={"event": "notify_failed"})

    async def tick(self) -> list[str]:
        if not await self.ready():
            return []  # nobody to tell: no digest, no alerts, no LinkedIn visits
        settings = await self.store.digest()
        at = self.clock()
        local = at.astimezone(self.zone)
        hhmm = local.strftime("%H:%M")
        done: list[str] = []
        paused = (await self.db.policy()).paused
        if (
            settings.enabled
            and hhmm >= settings.local_time
            and settings.last_digest_date != local.date().isoformat()
        ):
            # While paused the digest still arrives, built from local data only.
            done += await self.guard(
                self.send_digest(local.date().isoformat(), linkedin=not paused)
            )
        if paused:
            return done  # no alerts and no LinkedIn visits while paused
        if not in_quiet_hours(hhmm, settings.quiet_start, settings.quiet_end):
            if settings.alerts_boards:
                done += await self.guard(self.board_alerts(settings, at))
            if settings.alerts_messages:
                done += await self.guard(self.message_alerts(at))
        return done

    async def guard(self, work: Coroutine[Any, Any, list[str]]) -> list[str]:
        try:
            return await work
        except Exception:
            logging.warning("", extra={"event": "scheduler_failed"})
            return []

    async def send_digest(self, day: str, *, linkedin: bool = True) -> list[str]:
        await self.deliver(await self.digest.build(linkedin=linkedin), "digest")
        await self.store.update_digest(last_digest_date=day)
        return ["digest"]

    async def board_alerts(self, settings: DigestSettings, at: datetime) -> list[str]:
        if settings.last_alert_at is None:
            await self.store.update_digest(last_alert_at=at.isoformat())
            return []  # alerts begin from the moment they are switched on
        cutoff = datetime.fromisoformat(settings.last_alert_at)
        # First sighting, so an edited posting is not announced again as a new opening.
        postings = await self.db.opportunities(since=None, first_seen_after=cutoff)
        matches = [
            m
            for m in rank(postings, await self.db.preferences(), await self.store.pinned_tokens())
            if "pinned firm" in m.reasons
        ]
        await self.store.update_digest(last_alert_at=at.isoformat())
        if not matches:
            return []
        lines = [
            f"• {m.title} — {m.company} · {m.location or 'location unknown'}\n  {m.url}"
            for m in matches[:8]
        ]
        await self.deliver("🔔 New openings at pinned firms\n" + "\n".join(lines), "board_alert")
        return ["board_alert"]

    async def message_alerts(self, at: datetime) -> list[str]:
        if self.linkedin is None:
            return []
        if self.last_message_check is not None and at - self.last_message_check < self.interval:
            return []
        self.last_message_check = at
        result, error = await self.linkedin.fetch(lambda c: c.messages(10, refresh=True))
        if error is not None or result is None:
            if await self.kv.get("linkedin_notice") == error:
                return []  # already told the user about this problem
            await self.kv.set("linkedin_notice", error or "")
            await self.deliver(f"⚠️ {error}", "auth_notice")
            return ["auth_notice"]
        await self.kv.set("linkedin_notice", "")
        # A conversation is announced when it becomes unread, and again only after it was
        # read in between. Relative times ("2:15 PM", then "Yesterday") are not part of the key.
        unread = {hash_key(c.title): c for c in result.items if c.unread}
        seen = set(json.loads(await self.kv.get("alerted_unread") or "[]"))
        fresh = [c for key, c in unread.items() if key not in seen]
        await self.kv.set("alerted_unread", json.dumps(sorted(unread)[:SEEN_LIMIT]))
        if not fresh:
            return []
        names = ", ".join(c.title for c in fresh)
        await self.deliver(f"📬 {len(fresh)} unread conversation(s): {names}", "message_alert")
        return ["message_alert"]


async def run_scheduler(scheduler: Ticker, stop: asyncio.Event, interval: float = 60.0) -> None:
    while not stop.is_set():
        try:
            await scheduler.tick()
        except Exception:
            logging.warning("", extra={"event": "scheduler_failed"})
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


async def monitor_loop(
    run_one: Callable[[], Awaitable[bool]], stop: asyncio.Event, idle_seconds: float = 5.0
) -> None:
    """Process due board collections; wait a little whenever there is nothing to do."""
    while not stop.is_set():
        try:
            worked = await run_one()
        except Exception:
            logging.warning("", extra={"event": "monitor_failed"})
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=idle_seconds)
            except TimeoutError:
                pass
