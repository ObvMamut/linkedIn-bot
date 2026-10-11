"""Everything a long-running front end needs, built once from the config.

`serve`, `telegram` and the terminal UI all run on one `Services` object, so one process owns
the database, the browser profile and the LinkedIn page budget. The caller opens the database
(under the runtime lock) and closes it after `aclose()`.
"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from linkedin_bot.adapters.linkedin.limits import BudgetFile, Limiter
from linkedin_bot.adapters.linkedin.session import WarmSession, playwright_launcher
from linkedin_bot.adapters.ollama import OllamaGenerator
from linkedin_bot.adapters.public_http import PublicHTTP
from linkedin_bot.adapters.telegram import HttpTelegram, TelegramPoller
from linkedin_bot.domain.models import now
from linkedin_bot.runtime.config import Config
from linkedin_bot.storage.database import Database
from linkedin_bot.storage.linkedin import LinkedInStore
from linkedin_bot.storage.telegram import TelegramStore
from linkedin_bot.workflows.boards import find_board, public_probe
from linkedin_bot.workflows.digest import DigestService
from linkedin_bot.workflows.executor import Executor
from linkedin_bot.workflows.linkedin_commands import Connector, LinkedInCommands
from linkedin_bot.workflows.maintenance import Maintenance, maintenance_loop
from linkedin_bot.workflows.materials import Generator
from linkedin_bot.workflows.monitor import run_one
from linkedin_bot.workflows.onboarding import Onboarding
from linkedin_bot.workflows.operations import BoardFinder, Operations
from linkedin_bot.workflows.serve import Notifier, Scheduler, monitor_loop, run_scheduler
from linkedin_bot.workflows.telegram_bot import COMMANDS, Bot, poll_forever

Opener = Callable[[], AbstractAsyncContextManager[Connector]]


class Services:
    def __init__(
        self,
        config: Config,
        database: Database,
        *,
        telegram: TelegramPoller | None = None,
        generator: Generator | None = None,
        opener: Opener | None = None,
        http: PublicHTTP | None = None,
        finder: BoardFinder | None = None,
        notifiers: list[Notifier] | None = None,
    ) -> None:
        """Real adapters by default; tests pass fakes for the network-facing parts."""
        self.config, self.db = config, database
        self.limiter = Limiter(budget=BudgetFile(config.root))
        self.session: WarmSession | None = None
        release = None
        if opener is None:
            self.session = WarmSession(
                playwright_launcher(
                    config.root, config.linkedin.browser_executable, config.linkedin.block_resources
                ),
                self.limiter,
                idle_seconds=config.linkedin.idle_close_seconds,
            )
            opener = self.session.use
            release = self.session.close
        self.linkedin = LinkedInCommands(opener, LinkedInStore(database), release=release)
        self.http = http or PublicHTTP()
        self.finder = finder or self._find_board
        self.generator = generator
        self.executor = Executor(database, timezone=config.timezone)
        self.digest = DigestService(database, self.linkedin, generator)
        self.ops = Operations(database, self.linkedin, self.finder, config.board_interval_seconds)
        self.onboarding = Onboarding(database)
        self.api = telegram
        self.bot: Bot | None = None
        if telegram is not None:
            self.bot = Bot(
                database,
                telegram,
                generator,
                self.executor,
                self.linkedin,
                self.finder,
                config.board_interval_seconds,
                operations=self.ops,
                digest_service=self.digest,
            )
        self.scheduler = Scheduler(
            db=database,
            digest=self.digest,
            linkedin=self.linkedin,
            timezone=config.timezone,
            message_interval_minutes=max(30, config.linkedin.inbox_interval_minutes),
            api=telegram,
            notifiers=notifiers or [],
        )
        self.maintenance = Maintenance(database, config.timezone)
        self.pairing_check_seconds = 15.0
        self.tasks: list[asyncio.Task[None]] = []

    @classmethod
    def from_config(
        cls, config: Config, database: Database, *, telegram: bool, **overrides: Any
    ) -> Services:
        """The production wiring: Telegram only when asked for (the token must be set)."""
        api = HttpTelegram() if telegram else None
        generator = OllamaGenerator(config.inference) if config.inference.model else None
        return cls(config, database, telegram=api, generator=generator, **overrides)

    async def _find_board(self, name: str) -> tuple[str, str] | None:
        return await find_board(name, public_probe(self.http))

    async def collect_once(self) -> bool:
        """Process one due board read, if any."""
        return await run_one(self.db, self.http)

    def start_background(self, stop: asyncio.Event, *, poll_telegram: bool = False) -> None:
        """Board collection, digest and alerts, and optionally Telegram polling."""
        self.tasks.append(asyncio.create_task(monitor_loop(self.collect_once, stop)))
        self.tasks.append(asyncio.create_task(run_scheduler(self.scheduler, stop)))
        self.tasks.append(asyncio.create_task(maintenance_loop(self.maintenance, stop)))
        if poll_telegram and self.bot is not None and self.api is not None:
            self.tasks.append(asyncio.create_task(self._poll(stop)))

    async def _poll(self, stop: asyncio.Event) -> None:
        """Poll while paired or while a pairing code waits; otherwise check back later."""
        assert self.bot is not None and self.api is not None
        store = TelegramStore(self.db)
        await self.register_commands()
        while not stop.is_set():
            if await store.polling_allowed(now()):
                await poll_forever(self.bot, self.api, stop, stop_when_unpaired=store)
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.pairing_check_seconds)
            except TimeoutError:
                pass

    async def register_commands(self) -> None:
        """Best effort: list the commands in Telegram's menu. Failure changes nothing else."""
        if not isinstance(self.api, HttpTelegram):
            return
        try:
            await self.api.set_commands(list(COMMANDS))
        except Exception:
            logging.warning("", extra={"event": "telegram_commands_failed"})

    async def telegram_ready(self) -> bool:
        return self.api is not None and await TelegramStore(self.db).polling_allowed(now())

    async def release_browser(self) -> None:
        if self.session is not None:
            await self.session.close()

    async def aclose(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        await self.release_browser()
        await self.http.close()
        if isinstance(self.api, HttpTelegram):
            await self.api.close()
        if isinstance(self.generator, OllamaGenerator):
            await self.generator.close()
