"""Authorize every update, then run commands and the resumable onboarding journey."""

import logging
from datetime import datetime, timedelta

from linkedin_bot.adapters.telegram import TelegramAPI, TelegramPoller, Update
from linkedin_bot.domain.matching import rank
from linkedin_bot.domain.models import (
    Artifact,
    CandidateFact,
    SearchPreferences,
    digest,
    identifier,
    now,
)
from linkedin_bot.storage.database import Database
from linkedin_bot.storage.telegram import TelegramStore

STEPS = ("roles", "locations", "exclusions", "eligibility", "cv")
PROMPTS = {
    "roles": "Which roles or internships are you targeting? (comma separated)",
    "locations": "Which locations, and is remote acceptable? Say 'remote ok' if it is.",
    "exclusions": "Any companies, sectors or role types to exclude? (comma separated, or 'none')",
    "eligibility": "What is your work eligibility (citizenship, visa needs)? I will not guess it.",
    "cv": "Paste your CV as text. I never ask for LinkedIn passwords, codes or cookies here.",
}
MODE_LINE = "Mode: draft_only. Nothing is sent to employers; application and message caps are 0."
HELP = (
    "/start resume onboarding\n/status show mode and progress\n"
    "/opportunities show ranked matches from collected postings\n"
    "/pause pause work\n/resume resume reads (not approval to send anything)\n/help this text"
)


def split(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.replace("\n", ",").split(",") if part.strip())


class Bot:
    def __init__(self, database: Database, api: TelegramAPI) -> None:
        self.db = database
        self.api = api
        self.store = TelegramStore(database)

    async def handle(self, update: Update) -> None:
        if update.update_id <= await self.store.last_update_id():
            return
        message = update.message
        try:
            if message is not None and message.chat.type == "private" and message.text:
                await self._dispatch(message.sender.id, message.chat.id, message.text.strip())
        finally:
            await self.store.record_update(update.update_id)

    async def _dispatch(self, user_id: int, chat_id: int, text: str) -> None:
        binding = await self.store.binding()
        if binding is None:
            code = text.removeprefix("/start ").strip() if text.startswith("/start ") else None
            if code and await self.store.pair(code, user_id, chat_id, now()):
                await self._reply(chat_id, f"Paired. {MODE_LINE}")
                await self._reply(chat_id, await self._prompt())
            return
        if binding != (user_id, chat_id):
            return  # Strangers get no reply and cannot learn that the bot is paired.
        command = text.split()[0] if text.startswith("/") else None
        if command == "/start":
            await self._reply(chat_id, await self._prompt())
        elif command == "/status":
            step, _ = await self.store.onboarding()
            status = await self.db.status()
            await self._reply(
                chat_id,
                f"{MODE_LINE}\nPaused: {status['paused']}\nOnboarding: {step}\n"
                f"Facts: {status['facts']}  Opportunities seen: {status['observations']}",
            )
        elif command == "/opportunities":
            await self._reply(chat_id, await self._opportunities())
        elif command == "/pause":
            await self.db.pause(True)
            await self._reply(chat_id, "Paused.")
        elif command == "/resume":
            await self.db.pause(False)
            await self._reply(chat_id, f"Resumed. {MODE_LINE}")
        elif command == "/help":
            await self._reply(chat_id, HELP)
        elif command is not None:
            await self._reply(chat_id, f"Unknown or not yet released command.\n{HELP}")
        else:
            await self._answer(chat_id, text)

    async def _opportunities(self) -> str:
        recent = await self.db.opportunities(since=now() - timedelta(days=14))
        matches = rank(recent, await self.db.preferences())[:5]
        if not matches:
            return "No matches yet. Collect boards and finish onboarding first."
        return "\n\n".join(
            f"{m.title} ({m.location or 'location unknown'}) score {m.score}\n"
            f"Why: {', '.join(m.reasons)}"
            + (f"\nGaps: {', '.join(m.gaps)}" if m.gaps else "")
            + f"\n{m.url}"
            for m in matches
        )

    async def _prompt(self) -> str:
        step, _ = await self.store.onboarding()
        return "Onboarding is complete. Use /status or /help." if step == "done" else PROMPTS[step]

    async def _answer(self, chat_id: int, text: str) -> None:
        step, answers = await self.store.onboarding()
        if step == "done":
            await self._reply(chat_id, "Onboarding is complete. Use /help.")
            return
        answers[step] = text
        following = STEPS[STEPS.index(step) + 1] if step != STEPS[-1] else "done"
        if following == "done":
            await self._finish(answers, now())
        await self.store.set_onboarding(following, answers)
        await self._reply(
            chat_id,
            "Saved. Onboarding is complete; I will not act until later milestones release it."
            if following == "done"
            else f"Saved. {PROMPTS[following]}",
        )

    async def _finish(self, answers: dict[str, str], at: datetime) -> None:
        remote = "remote" in answers["locations"].lower() or None
        places = tuple(
            p for p in split(answers["locations"]) if p.lower() not in ("remote ok", "remote")
        )
        roles = split(answers["roles"])
        current = await self.db.call(
            lambda c: c.execute("SELECT MAX(revision) FROM preferences").fetchone()[0]
        )
        exclusions = () if answers["exclusions"].lower() == "none" else split(answers["exclusions"])
        await self.db.save_preferences(
            SearchPreferences(
                revision=current + 1,
                roles=roles,
                locations=places,
                remote=remote,
                internship=any("intern" in role.lower() for role in roles) or None,
                exclusions=exclusions,
            ),
            current,
        )
        await self.db.save_fact(
            CandidateFact(
                kind="work_eligibility",
                value=answers["eligibility"],
                source_reference="telegram:onboarding",
                status="confirmed",
                confirmed_at=at,
            )
        )
        content = answers["cv"].encode()
        artifact_id = identifier()
        profile = await self.db.profile()
        await self.db.save_artifact(
            content,
            Artifact(
                id=artifact_id,
                kind="baseline_cv",
                relative_path=f"artifacts/{artifact_id}",
                sha256=digest(content),
                profile_revision=profile.revision,
                generator_version="user-supplied",
            ),
        )
        logging.info("", extra={"event": "onboarding_completed"})

    async def _reply(self, chat_id: int, text: str) -> None:
        await self.api.send_message(chat_id, text)


async def poll_once(bot: Bot, api: TelegramPoller, poll_seconds: int = 30) -> int:
    """Fetch and handle one batch. Update IDs are recorded as each one is handled."""
    offset = await bot.store.last_update_id() + 1
    updates = await api.get_updates(offset, poll_seconds)
    for update in updates:
        await bot.handle(update)
    return len(updates)
