"""Approving, rejecting and resolving proposed actions, and changing the policy.

Shared by Telegram and the terminal UI so both enforce the same rules: approvals are single
use, a policy change invalidates every pending approval, and caps never exceed the ceilings.
"""

from dataclasses import dataclass

from linkedin_bot.domain.models import (
    MAX_DAILY_APPLICATIONS,
    MAX_DAILY_COMMUNICATIONS,
    ActionSnapshot,
    Policy,
)
from linkedin_bot.storage.database import Database
from linkedin_bot.workflows.executor import Executor


@dataclass(frozen=True)
class Pending:
    action: ActionSnapshot
    approval_id: str | None


def describe(policy: Policy) -> str:
    return (
        f"mode={policy.mode} applications/day={policy.application_cap} "
        f"messages/day={policy.communication_cap}"
    )


async def pending(database: Database) -> list[Pending]:
    return [
        Pending(action, await database.approval_for(action.id))
        for action in await database.actions_in_state("awaiting_approval")
    ]


async def unknown(database: Database) -> list[ActionSnapshot]:
    return list(await database.actions_in_state("unknown"))


async def decide(database: Database, executor: Executor, approval_id: str, approve: bool) -> str:
    outcome = await executor.decide(approval_id, approve=approve)
    if outcome == "stale":
        return "That approval is no longer valid (used, expired, edited or policy changed)."
    if outcome == "rejected":
        return "Rejected. Nothing will be sent."
    action_id = await database.call(
        lambda c: c.execute(
            "SELECT action_id FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()[0]
    )
    reasons = await executor.run(action_id)
    if reasons:
        return f"Approved, but blocked: {', '.join(reasons)}. Nothing was sent."
    _, state = await database.action(action_id)
    return f"Dispatched once. Outcome: {state}."


def propose(
    current: Policy,
    *,
    mode: str | None = None,
    application_cap: int | None = None,
    communication_cap: int | None = None,
) -> Policy:
    """The next policy revision; raises ValueError past a hard ceiling or for a bad mode."""
    update: dict[str, object] = {"revision": current.revision + 1}
    if mode is not None:
        if mode not in ("draft_only", "reviewed"):
            raise ValueError("unknown_mode")
        update["mode"] = mode
    if application_cap is not None:
        if not 0 <= application_cap <= MAX_DAILY_APPLICATIONS:
            raise ValueError(
                f"The hard ceiling for application is {MAX_DAILY_APPLICATIONS} per day."
            )
        update["application_cap"] = application_cap
    if communication_cap is not None:
        if not 0 <= communication_cap <= MAX_DAILY_COMMUNICATIONS:
            raise ValueError(
                f"The hard ceiling for communication is {MAX_DAILY_COMMUNICATIONS} per day."
            )
        update["communication_cap"] = communication_cap
    return current.model_copy(update=update)


async def apply(database: Database, proposed: Policy, current: Policy) -> None:
    """Save `proposed` as the next revision. Runtime flags (paused, audit) are not policy."""
    await database.save_policy(
        proposed.model_copy(update={"paused": False, "restore_audit_required": False}),
        current.revision,
    )


async def resolve(database: Database, action_id: str, happened: bool, note: str) -> str:
    if not note.strip():
        return "An evidence note is required: say what you inspected."
    done = await database.resolve_unknown(action_id, happened, note.strip())
    return "Recorded as your attestation." if done else "No unknown action with that id."
