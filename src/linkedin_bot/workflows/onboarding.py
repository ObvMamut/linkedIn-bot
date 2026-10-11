"""The resumable onboarding journey (roles, locations, exclusions, eligibility, CV).

Telegram asks one step at a time; the terminal UI fills every answer in one form. Both finish
the same way: answers are merged into the existing settings, eligibility becomes a confirmed
fact and the CV is stored as a checked artifact.
"""

import logging
from datetime import datetime

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
MAX_CV_CHARACTERS = 50_000


def split(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.replace("\n", ",").split(",") if part.strip())


def union(old: tuple[str, ...], new: tuple[str, ...]) -> tuple[str, ...]:
    seen = {value.lower() for value in old}
    return old + tuple(value for value in new if value.lower() not in seen)


class Onboarding:
    def __init__(self, database: Database) -> None:
        self.db = database
        self.store = TelegramStore(database)

    async def state(self) -> tuple[str, dict[str, str]]:
        return await self.store.onboarding()

    async def done(self) -> bool:
        return (await self.store.onboarding())[0] == "done"

    async def prompt(self) -> str:
        step, _ = await self.store.onboarding()
        return "Onboarding is complete. Use /status or /help." if step == "done" else PROMPTS[step]

    async def answer(self, text: str, at: datetime | None = None) -> str:
        """Store one answer and return the step that follows (`done` at the end)."""
        step, answers = await self.store.onboarding()
        if step == "done":
            return "done"
        answers[step] = text
        following = STEPS[STEPS.index(step) + 1] if step != STEPS[-1] else "done"
        if following == "done":
            await self.complete(answers, at or now())
            # The CV now lives in its own checked artifact; keep no second copy here.
            answers = {key: value for key, value in answers.items() if key != "cv"}
        await self.store.set_onboarding(following, answers)
        return following

    async def finish_all(self, answers: dict[str, str], at: datetime | None = None) -> None:
        """Every answer at once (the terminal UI form)."""
        missing = [step for step in STEPS if not answers.get(step, "").strip()]
        if missing:
            raise ValueError(f"missing_{missing[0]}")
        await self.complete(answers, at or now())
        await self.store.set_onboarding(
            "done", {key: value for key, value in answers.items() if key != "cv"}
        )

    async def complete(self, answers: dict[str, str], at: datetime) -> None:
        remote = "remote" in answers["locations"].lower() or None
        places = tuple(
            p for p in split(answers["locations"]) if p.lower() not in ("remote ok", "remote")
        )
        roles = split(answers["roles"])
        exclusions = () if answers["exclusions"].lower() == "none" else split(answers["exclusions"])

        def merge(current: SearchPreferences) -> SearchPreferences:
            # Settings changed before onboarding ended are kept; answers add to them.
            return current.model_copy(
                update={
                    "roles": union(current.roles, roles),
                    "locations": union(current.locations, places),
                    "remote": current.remote if remote is None else remote,
                    "internship": current.internship
                    or any("intern" in role.lower() for role in roles)
                    or None,
                    "exclusions": union(current.exclusions, exclusions),
                }
            )

        await self.db.update_preferences(merge)
        await self.db.save_fact(
            CandidateFact(
                kind="work_eligibility",
                value=answers["eligibility"],
                source_reference="onboarding",
                status="confirmed",
                confirmed_at=at,
            )
        )
        await save_cv(self.db, answers["cv"])
        logging.info("", extra={"event": "onboarding_completed"})


async def save_cv(database: Database, text: str) -> Artifact:
    """Store CV text as the newest baseline CV artifact (earlier ones stay for history)."""
    text = text.strip()
    if not text:
        raise ValueError("empty_cv")
    content = text[:MAX_CV_CHARACTERS].encode()
    artifact_id = identifier()
    profile = await database.profile()
    return await database.save_artifact(
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


async def baseline_cv(database: Database) -> str | None:
    """The newest baseline CV text, or None."""
    profile = await database.profile()
    if profile.baseline_artifact_id is None:
        return None
    path = database.root / "artifacts" / profile.baseline_artifact_id
    return path.read_text() if path.is_file() else None
