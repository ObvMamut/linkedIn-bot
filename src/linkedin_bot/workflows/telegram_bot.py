"""Authorize every update, then run commands and the resumable onboarding journey."""

import logging
from datetime import datetime, timedelta

from linkedin_bot.adapters.public_http import AdapterError
from linkedin_bot.adapters.telegram import (
    CallbackQuery,
    Chat,
    TelegramAPI,
    TelegramPoller,
    Update,
)
from linkedin_bot.domain.matching import rank
from linkedin_bot.domain.models import (
    MAX_DAILY_APPLICATIONS,
    MAX_DAILY_COMMUNICATIONS,
    Artifact,
    CandidateFact,
    SearchPreferences,
    digest,
    identifier,
    now,
)
from linkedin_bot.storage.database import Database
from linkedin_bot.storage.telegram import TelegramStore
from linkedin_bot.workflows.executor import Executor
from linkedin_bot.workflows.linkedin_commands import LinkedInCommands
from linkedin_bot.workflows.materials import Generator, draft_cover_letter
from linkedin_bot.workflows.ui.router import UI, BoardFinder

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
    "/prepare <id> draft a cover letter for review\n"
    "/pending actions waiting for you or with unknown outcome\n"
    "/approve <id>, /reject <id> decide one proposed action\n"
    "/policy show or change mode and daily caps (changes need 'confirm')\n"
    "/resolve <action> success|none <note> record what you saw for an unknown outcome\n"
    "/menu open the button menu (messages, search, pinned, jobs, saved, settings)\n"
    "/li_status /li_messages /li_search /li_profile read-only LinkedIn access\n"
    "/li_release free the browser profile (before `linkedin login`)\n"
    "/pause pause work\n/resume resume reads (not approval to send anything)\n/help this text"
)


def split(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.replace("\n", ",").split(",") if part.strip())


class Bot:
    def __init__(
        self,
        database: Database,
        api: TelegramAPI,
        generator: Generator | None = None,
        executor: Executor | None = None,
        linkedin: LinkedInCommands | None = None,
        board_finder: BoardFinder | None = None,
    ) -> None:
        self.db = database
        self.api = api
        self.generator = generator
        self.executor = executor or Executor(database)
        self.linkedin = linkedin
        self.store = TelegramStore(database)
        self.ui = UI(database, api, linkedin, board_finder, llm_enabled=generator is not None)

    async def handle(self, update: Update) -> None:
        if update.update_id <= await self.store.last_update_id():
            return
        message = update.message
        query = update.callback_query
        try:
            if message is not None and message.chat.type == "private" and message.text:
                await self._dispatch(message.sender.id, message.chat.id, message.text.strip())
            elif query is not None and query.data and query.message is not None:
                await self._press(query.sender.id, query.message.chat, query)
        finally:
            await self.store.record_update(update.update_id)

    async def _press(self, user_id: int, chat: Chat, query: CallbackQuery) -> None:
        """Buttons get the same authorisation as messages: the paired user in the paired chat."""
        if chat.type != "private" or await self.store.binding() != (user_id, chat.id):
            return
        assert query.message is not None and query.data is not None
        await self.ui.on_callback(chat.id, query.message.message_id, query.id, query.data)

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
        if command == "/menu":
            await self.ui.open_menu(chat_id)
            return
        if command is None and await self.ui.handle_text(chat_id, text):
            return
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
        elif command == "/prepare":
            await self._reply(chat_id, await self._prepare(text.split()[1:]))
        elif command == "/pending":
            await self._reply(chat_id, await self._pending())
        elif command in ("/approve", "/reject"):
            await self._reply(chat_id, await self._decide(command == "/approve", text))
        elif command == "/policy":
            await self._reply(chat_id, await self._policy(text.split()[1:]))
        elif command == "/resolve":
            await self._reply(chat_id, await self._resolve(text.split(maxsplit=3)[1:]))
        elif command is not None and command.startswith("/li_"):
            await self._reply(
                chat_id,
                "LinkedIn is not enabled on this installation."
                if self.linkedin is None
                else await self.linkedin.handle(text),
            )
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
        pinned = await self.ui.store.pinned_tokens()
        matches = rank(recent, await self.db.preferences(), pinned)[:5]
        if not matches:
            return "No matches yet. Collect boards and finish onboarding first."
        return "\n\n".join(
            f"{m.title} ({m.location or 'location unknown'}) score {m.score}\n"
            f"Id: {m.source}:{m.external_id}\n"
            f"Why: {', '.join(m.reasons)}"
            + (f"\nGaps: {', '.join(m.gaps)}" if m.gaps else "")
            + f"\n{m.url}"
            for m in matches
        )

    async def _prepare(self, arguments: list[str]) -> str:
        if self.generator is None:
            return "No local model is configured (inference.model). Nothing was drafted."
        if len(arguments) != 1:
            return "Usage: /prepare <source>:<id>, as shown by /opportunities."
        postings = await self.db.opportunities(since=None)
        posting = next((p for p in postings if f"{p.source}:{p.external_id}" == arguments[0]), None)
        if posting is None:
            return "Unknown opportunity."
        try:
            artifact = await draft_cover_letter(self.db, self.generator, posting)
        except AdapterError as exc:
            return f"Draft not produced: {exc.code}. Nothing was saved."
        text = (self.db.root / artifact.relative_path).read_text()
        return f"Draft for review (not sent anywhere):\n\n{text}"

    async def _pending(self) -> str:
        lines = []
        for item in await self.db.actions_in_state("awaiting_approval"):
            approval = await self.db.approval_for(item.id)
            lines.append(
                f"Approve? {item.kind} to {item.target}\n{item.content}\n"
                f"/approve {approval}   /reject {approval}"
            )
        for item in await self.db.actions_in_state("unknown"):
            lines.append(
                f"Unknown outcome: {item.kind} to {item.target} ({item.id})\n"
                "Inspect it yourself, then /resolve <id> success|none <evidence note>. "
                "It will not be retried automatically."
            )
        return "\n\n".join(lines) if lines else "Nothing waiting."

    async def _decide(self, approve: bool, text: str) -> str:
        parts = text.split()
        if len(parts) != 2:
            return f"Usage: {parts[0]} <approval id>"
        outcome = await self.executor.decide(parts[1], approve=approve)
        if outcome == "stale":
            return "That approval is no longer valid (used, expired, edited or policy changed)."
        if outcome == "rejected":
            return "Rejected. Nothing will be sent."
        action_id = await self.db.call(
            lambda c: c.execute(
                "SELECT action_id FROM approvals WHERE id=?", (parts[1],)
            ).fetchone()[0]
        )
        reasons = await self.executor.run(action_id)
        if reasons:
            return f"Approved, but blocked: {', '.join(reasons)}. Nothing was sent."
        _, state = await self.db.action(action_id)
        return f"Dispatched once. Outcome: {state}."

    async def _policy(self, arguments: list[str]) -> str:
        current = await self.db.policy()
        summary = (
            f"mode={current.mode} applications/day={current.application_cap} "
            f"messages/day={current.communication_cap}"
        )
        if not arguments:
            return summary
        confirmed = arguments[-1] == "confirm"
        change = arguments[:-1] if confirmed else arguments
        update: dict[str, object]
        if len(change) == 2 and change[0] == "mode" and change[1] in ("draft_only", "reviewed"):
            update = {"mode": change[1]}
        elif (
            len(change) == 3
            and change[0] == "cap"
            and change[1] in ("application", "communication")
            and change[2].isdecimal()
        ):
            ceiling = (
                MAX_DAILY_APPLICATIONS if change[1] == "application" else MAX_DAILY_COMMUNICATIONS
            )
            if int(change[2]) > ceiling:
                return f"The hard ceiling for {change[1]} is {ceiling} per day."
            update = {f"{change[1]}_cap": int(change[2])}
        else:
            return "Usage: /policy [mode draft_only|reviewed | cap application|communication N]"
        proposed = current.model_copy(update={**update, "revision": current.revision + 1})
        if not confirmed:
            return (
                f"Before: {summary}\nAfter: mode={proposed.mode} "
                f"applications/day={proposed.application_cap} "
                f"messages/day={proposed.communication_cap}\n"
                "Pending actions will need fresh approval. Repeat the command with 'confirm'."
            )
        await self.db.save_policy(
            proposed.model_copy(update={"paused": False, "restore_audit_required": False}),
            current.revision,
        )
        return (
            f"Policy updated to revision {proposed.revision}. Pending approvals were invalidated."
        )

    async def _resolve(self, arguments: list[str]) -> str:
        if len(arguments) < 2 or arguments[1] not in ("success", "none"):
            return "Usage: /resolve <action id> success|none <evidence note>"
        if len(arguments) < 3 or not arguments[2].strip():
            return "An evidence note is required: say what you inspected."
        done = await self.db.resolve_unknown(arguments[0], arguments[1] == "success", arguments[2])
        return "Recorded as your attestation." if done else "No unknown action with that id."

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
