import argparse
import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from linkedin_bot.adapters.ats import ATSSource, Board, Provider
from linkedin_bot.adapters.browser_probe import browser_smoke
from linkedin_bot.adapters.linkedin.connector import LinkedInConnector
from linkedin_bot.adapters.linkedin.limits import BudgetFile, Limiter
from linkedin_bot.adapters.linkedin.session import (
    WarmSession,
    manual_login_command,
    open_connector,
    playwright_launcher,
    profile_in_use,
    system_chromium,
)
from linkedin_bot.adapters.ollama import OllamaProbe
from linkedin_bot.adapters.public_http import AdapterError, PublicHTTP
from linkedin_bot.adapters.telegram import TOKEN_ENV
from linkedin_bot.domain.models import identifier, now
from linkedin_bot.runtime.config import Config, load_config
from linkedin_bot.runtime.locking import AlreadyRunning, RuntimeLock
from linkedin_bot.runtime.privacy import BoundaryError, configure_logging
from linkedin_bot.runtime.services import Services
from linkedin_bot.storage.database import Database, RevisionConflict, SchemaError
from linkedin_bot.storage.linkedin import LinkedInStore
from linkedin_bot.storage.restore import restore_backup
from linkedin_bot.storage.telegram import TelegramStore
from linkedin_bot.storage.workspace import WorkspaceStore
from linkedin_bot.workflows.maintenance import Maintenance
from linkedin_bot.workflows.monitor import run_one
from linkedin_bot.workflows.telegram_bot import poll_forever


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
            command.add_argument(
                "--interval-seconds",
                type=int,
                help="Defaults to linkedin.jobs_interval_minutes from the config",
            )
    run = commands.add_parser("run", help="Run explicitly configured read work in the foreground")
    run.add_argument("--once", action="store_true", help="Process at most one due work item")
    commands.add_parser("pair", help="Print a one-use Telegram pairing code (valid ten minutes)")
    commands.add_parser("unpair", help="Forget the paired Telegram chat")
    commands.add_parser(
        "prune", help="Snapshot, rotate automatic backups and drop old posting versions now"
    )
    telegram = commands.add_parser("telegram", help="Poll Telegram in the foreground")
    telegram.add_argument("--once", action="store_true", help="Handle one polling batch")
    commands.add_parser(
        "serve",
        help="Foreground process: Telegram, board collection, and the opt-in digest and alerts",
    )
    linkedin = commands.add_parser("linkedin", help="Read-only LinkedIn session tools")
    linkedin.add_argument("action", choices=("login", "status", "timing"))
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


async def timing(config: Config) -> int:
    """Per-step wall time and result counts only (never content), cold then warm then cached.

    Burst pacing is switched off here: a lone command after a quiet period has none, so this
    shows what one command costs. Hourly and daily limits still apply.
    """
    session = WarmSession(
        playwright_launcher(
            config.root, config.linkedin.browser_executable, config.linkedin.block_resources
        ),
        Limiter(delay=lambda: 0.0, budget=BudgetFile(config.root)),
    )
    steps: list[tuple[str, Callable[[LinkedInConnector], Awaitable[Any]]]] = [
        ("status", lambda c: c.status()),
        ("messages", lambda c: c.messages(5, refresh=True)),
        ("people", lambda c: c.search_people("data scientist", refresh=True)),
        ("firms", lambda c: c.search_companies("palantir", refresh=True)),
        ("profile", lambda c: c.profile(None, refresh=True)),
    ]
    rows = []
    try:
        for label, refresh in (("cold", True), ("warm", True), ("cached", False)):
            for name, call in steps if refresh else [("messages", lambda c: c.messages(5))]:
                started = time.perf_counter()
                async with session.use() as connector:
                    result = await call(connector)
                rows.append(
                    {
                        "run": label,
                        "step": name,
                        "ms": int((time.perf_counter() - started) * 1000),
                        "items": len(getattr(result, "items", ())) or None,
                    }
                )
    finally:
        await session.close()
    emit(rows)
    return 0


async def linkedin_cli(args: argparse.Namespace, config: Config) -> int:
    executable = config.linkedin.browser_executable
    if profile_in_use(config.root):
        raise BoundaryError("browser_in_use_send_li_release_in_telegram_or_stop_the_bot")
    if args.action == "timing":
        return await timing(config)
    if args.action == "login":
        browser = system_chromium(executable)
        if browser is None:
            raise BoundaryError("chromium_not_found_set_linkedin_browser_executable")
        emit({"instruction": "Sign in, then close the browser window. Nothing is automated."})
        process = await asyncio.create_subprocess_exec(*manual_login_command(browser, config.root))
        await process.wait()
        async with open_connector(
            config.root, Limiter(budget=BudgetFile(config.root)), executable
        ) as connector:
            state = await connector.status()
        emit({"session": state})
        return 0 if state == "ready" else 2
    async with open_connector(
        config.root, Limiter(budget=BudgetFile(config.root)), executable
    ) as connector:
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
                    getattr(args, "interval_seconds", None) or config.board_interval_seconds,
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
            elif args.command == "unpair":
                emit({"unpaired": await TelegramStore(database).unpair()})
            elif args.command == "prune":
                emit(await Maintenance(database, config.timezone).run())
            elif args.command == "serve":
                await telegram_foreground(database, config, once=False, background=True)
                emit({"stopped": True})
            elif args.command == "telegram":
                await telegram_foreground(database, config, once=args.once)
                emit({"stopped": True})
            elif args.command == "run":
                await foreground(database, once=args.once)
                emit({"stopped": True})
            else:
                emit(await doctor(database, config, recovery, artifact_health))
        finally:
            await database.close()
            logging.info("", extra={"event": "runtime_stopped"})
    return 0


async def doctor(
    database: Database, config: Config, recovery: dict[str, int], artifacts: dict[str, int]
) -> dict[str, object]:
    """What is configured and what state it is in. Names and counts only, never content."""
    status = await database.status()
    linkedin_state, last_ok = await LinkedInStore(database).state()
    digest = await WorkspaceStore(database).digest()
    work = cast(list[dict[str, object]], status["work"])
    states: dict[str, int] = {}
    for item in work:
        states[str(item["state"])] = states.get(str(item["state"]), 0) + 1
    budget = Limiter(budget=BudgetFile(config.root)).usage()
    return {
        **status,
        "work_states": states,
        "policy": (await database.policy()).model_dump(),
        "recovery": recovery,
        **artifacts,
        "capabilities": {
            "linkedin": {"session": linkedin_state, "last_ok": last_ok, "budget": budget},
            "telegram": "paired" if await TelegramStore(database).binding() else "unpaired",
            "telegram_token": "set" if os.environ.get(TOKEN_ENV) else "missing",
            "inference": config.inference.model or "not_configured",
            "digest": {
                "enabled": digest.enabled,
                "time": digest.local_time,
                "board_alerts": digest.alerts_boards,
                "message_alerts": digest.alerts_messages,
            },
            "employer_effects": "disabled",
            "deepseek": "disabled",
        },
    }


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


async def telegram_foreground(
    database: Database, config: Config, *, once: bool, background: bool = False
) -> None:
    # No effect adapter is registered, so approved actions stay blocked until one is added.
    services = Services.from_config(config, database, telegram=True)
    assert services.bot is not None and services.api is not None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        if not await services.bot.store.polling_allowed(now()):
            raise BoundaryError("not_paired_run_pair_first")
        if background:
            # Opt-in work: board collection every time something is due, plus the digest,
            # alerts and daily upkeep (which do nothing until switched on in Settings).
            services.start_background(stop)
        await poll_forever(services.bot, services.api, stop, once=once)
    finally:
        stop.set()
        await services.aclose()
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
