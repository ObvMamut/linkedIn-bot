import argparse
import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from linkedin_bot.adapters.ats import ATSSource, Board, Provider
from linkedin_bot.adapters.browser_probe import browser_smoke
from linkedin_bot.adapters.linkedin.limits import Limiter
from linkedin_bot.adapters.linkedin.session import open_connector, open_page, wait_for_login
from linkedin_bot.adapters.ollama import OllamaGenerator, OllamaProbe
from linkedin_bot.adapters.public_http import AdapterError, PublicHTTP
from linkedin_bot.adapters.telegram import HttpTelegram
from linkedin_bot.domain.models import identifier, now
from linkedin_bot.runtime.config import Config, load_config
from linkedin_bot.runtime.locking import AlreadyRunning, RuntimeLock
from linkedin_bot.runtime.privacy import BoundaryError, configure_logging
from linkedin_bot.storage.database import Database, RevisionConflict, SchemaError
from linkedin_bot.storage.linkedin import LinkedInStore
from linkedin_bot.storage.restore import restore_backup
from linkedin_bot.storage.telegram import TelegramStore
from linkedin_bot.workflows.executor import Executor
from linkedin_bot.workflows.linkedin_commands import LinkedInCommands
from linkedin_bot.workflows.monitor import run_one
from linkedin_bot.workflows.telegram_bot import Bot, poll_once


def emit(content: object) -> None:
    print(json.dumps(content, indent=2, default=str))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Draft-only local job-search foundations")
    result.add_argument("--config", type=Path)
    result.add_argument("--data-dir", type=Path)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize private local storage without network access")
    commands.add_parser("doctor", help="Report local persistence and unreleased capabilities")
    commands.add_parser("backup", help="Create a private consistent local snapshot")
    restore = commands.add_parser("restore", help="Restore into a fresh data directory, paused")
    restore.add_argument("snapshot", type=Path)
    commands.add_parser("pause")
    commands.add_parser("resume", help="Resume reads; draft-only restrictions remain")
    for name in ("collect", "schedule-board"):
        command = commands.add_parser(name)
        command.add_argument("provider", choices=("greenhouse", "lever", "ashby"))
        command.add_argument("slug")
        if name == "collect":
            command.add_argument("--limit", type=int, default=20)
        else:
            command.add_argument("--interval-seconds", type=int, default=21600)
    run = commands.add_parser("run", help="Run explicitly configured read work in the foreground")
    run.add_argument("--once", action="store_true", help="Process at most one due work item")
    commands.add_parser("pair", help="Print a one-use Telegram pairing code (valid ten minutes)")
    telegram = commands.add_parser("telegram", help="Poll Telegram in the foreground")
    telegram.add_argument("--once", action="store_true", help="Handle one polling batch")
    linkedin = commands.add_parser("linkedin", help="Read-only LinkedIn session tools")
    linkedin.add_argument("action", choices=("login", "status"))
    probe = commands.add_parser("probe", help="Explicit bounded feasibility checks")
    probe.add_argument("target", choices=("ollama", "browser"))
    probe.add_argument("--model", help="Installed model for a synthetic probe; never downloads")
    probe.add_argument("--browser-executable", help="Use installed Chromium for a smoke check")
    return result


async def probe(args: argparse.Namespace, config: Config) -> int:
    if args.target == "browser":
        emit(await browser_smoke(args.browser_executable))
    else:
        adapter = OllamaProbe(config.inference)
        try:
            if args.model:
                emit(await adapter.synthetic(args.model))
            else:
                emit({"models": [model.model_dump() for model in await adapter.inventory()]})
        finally:
            await adapter.close()
    return 0


async def linkedin_cli(args: argparse.Namespace, config: Config) -> int:
    executable = config.linkedin.browser_executable
    if args.action == "login":
        async with open_page(config.root, headless=False, executable=executable) as page:
            await page.goto("https://www.linkedin.com/login")
            emit({"instruction": "Sign in in the browser window (10 minutes). Do not close it."})
            state = await wait_for_login(page)
        emit({"session": state})
        return 0 if state == "ready" else 2
    async with open_connector(config.root, Limiter(), executable) as connector:
        emit({"session": await connector.status()})
    return 0


async def execute(args: argparse.Namespace, config: Config) -> int:
    if args.command == "probe":
        return await probe(args, config)
    if args.command == "linkedin":
        return await linkedin_cli(args, config)
    with RuntimeLock(config.root):
        if (config.root / "restore-incomplete").exists():
            raise BoundaryError("unfinished_restore_requires_recovery")
        if args.command == "restore":
            await asyncio.to_thread(restore_backup, args.snapshot.absolute(), config.root)
        database = Database(config.root)
        try:
            await database.open()
            recovery = await database.recover()
            artifact_health = await database.inspect_artifacts()
            if any(artifact_health.values()):
                logging.warning(
                    "",
                    extra={
                        "event": "artifact_recovery_needed",
                        "count": sum(artifact_health.values()),
                    },
                )
            logging.info("", extra={"event": "runtime_ready"})
            if args.command == "backup":
                emit({"snapshot": str(await database.backup())})
            elif args.command == "pause":
                await database.pause(True)
                emit({"paused": True})
            elif args.command == "resume":
                await database.pause(False)
                emit({"paused": False, "mode": "draft_only"})
            elif args.command in ("collect", "schedule-board"):
                board = Board(provider=cast(Provider, args.provider), slug=args.slug)
                board.endpoint(getattr(args, "limit", 20), None)
                workflow = f"ats:{board.provider}:{board.slug}"
                if args.command == "collect":
                    workflow = f"adhoc:{board.provider}:{board.slug}:{identifier()}"
                await database.schedule(
                    workflow,
                    board.model_dump_json(),
                    now(),
                    getattr(args, "interval_seconds", 21600),
                    recurring=args.command == "schedule-board",
                )
                if args.command == "collect":
                    http = PublicHTTP()
                    try:
                        work = await database.claim(now(), workflow)
                        if work is None:
                            raise BoundaryError("runtime_paused")
                        if work.workflow != workflow:
                            raise BoundaryError("another_work_item_due_use_run")
                        try:
                            page = await ATSSource(http, board).collect(args.limit, work.checkpoint)
                            await database.persist_page(work, page, now())
                        except AdapterError as exc:
                            await database.fail_work(work, exc.code, exc.transient, now())
                            raise
                        emit(
                            {
                                "count": len(page.items),
                                "coverage": page.coverage,
                                "next_cursor": page.next_cursor,
                                "qualification": "board observation; domain verification pending",
                            }
                        )
                    finally:
                        await http.close()
                else:
                    emit({"scheduled": board.model_dump(), "execution": "foreground run required"})
            elif args.command == "pair":
                emit(
                    {
                        "pairing_code": await TelegramStore(database).begin_pairing(now()),
                        "instruction": "Send /start <code> to your bot from a private chat",
                    }
                )
            elif args.command == "telegram":
                await telegram_foreground(database, config, once=args.once)
                emit({"stopped": True})
            elif args.command == "run":
                await foreground(database, once=args.once)
                emit({"stopped": True})
            else:
                emit(
                    {
                        **await database.status(),
                        "policy": (await database.policy()).model_dump(),
                        "recovery": recovery,
                        **artifact_health,
                        "capabilities": {
                            "linkedin": "pending_login_and_read_gates",
                            "telegram": "paired"
                            if await TelegramStore(database).binding()
                            else "unpaired",
                            "inference": "not_evaluated",
                            "employer_effects": "disabled",
                            "deepseek": "disabled",
                        },
                    }
                )
        finally:
            await database.close()
            logging.info("", extra={"event": "runtime_stopped"})
    return 0


async def foreground(database: Database, *, once: bool) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    http = PublicHTTP()
    try:
        while not stop.is_set():
            if once:
                await run_one(database, http)
                break
            if not await run_one(database, http):
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)
                except TimeoutError:
                    pass
    finally:
        await http.close()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


async def telegram_foreground(database: Database, config: Config, *, once: bool) -> None:
    api = HttpTelegram()
    generator = OllamaGenerator(config.inference) if config.inference.model else None
    # No effect adapter is registered, so approved actions stay blocked until one is added.
    limiter = Limiter()  # shared, so the page budget holds across Telegram commands
    commands = LinkedInCommands(
        lambda: open_connector(config.root, limiter, config.linkedin.browser_executable),
        LinkedInStore(database),
    )
    bot = Bot(database, api, generator, Executor(database, timezone=config.timezone), commands)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        if not await bot.store.polling_allowed(now()):
            raise BoundaryError("not_paired_run_pair_first")
        while not stop.is_set():
            polling = asyncio.ensure_future(poll_once(bot, api))
            waiting = asyncio.ensure_future(stop.wait())
            await asyncio.wait({polling, waiting}, return_when=asyncio.FIRST_COMPLETED)
            waiting.cancel()
            if not polling.done():
                polling.cancel()
                break
            polling.result()
            if once:
                break
    finally:
        await api.close()
        if generator is not None:
            await generator.close()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


def main() -> int:
    os.umask(0o077)
    configure_logging()
    args = parser().parse_args()
    try:
        config = load_config(args.config)
        if args.data_dir is not None:
            config = Config.model_validate({**config.model_dump(), "data_dir": str(args.data_dir)})
            config.validate_paths()
        return asyncio.run(execute(args, config))
    except ValidationError as exc:
        emit(
            {
                "error": "validation_error",
                "fields": [
                    {"location": list(error["loc"]), "type": error["type"]}
                    for error in exc.errors(include_input=False, include_context=False)
                ],
            }
        )
        return 2
    except (AdapterError, AlreadyRunning, BoundaryError, SchemaError, RevisionConflict) as exc:
        emit({"error": str(exc)})
        return 2
    except (OSError, sqlite3.Error, ValueError) as exc:
        emit({"error": type(exc).__name__})
        return 2


if __name__ == "__main__":
    sys.exit(main())
