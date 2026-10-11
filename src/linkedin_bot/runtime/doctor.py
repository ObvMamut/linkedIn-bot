"""The health report shared by `doctor` and the cockpit's System view."""

import os
from typing import cast

from linkedin_bot.adapters.linkedin.limits import BudgetFile, Limiter
from linkedin_bot.adapters.telegram import TOKEN_ENV
from linkedin_bot.runtime.config import Config
from linkedin_bot.storage.database import Database
from linkedin_bot.storage.linkedin import LinkedInStore
from linkedin_bot.storage.telegram import TelegramStore
from linkedin_bot.storage.workspace import WorkspaceStore


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
