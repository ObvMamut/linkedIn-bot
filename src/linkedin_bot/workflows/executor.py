"""Policy-gated action lifecycle: propose, approve, reserve, dispatch once, record the outcome.

No effect adapter is registered by the application, so approved actions stay blocked with
`capability_not_released` until an adapter is deliberately added.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from linkedin_bot.domain.models import ActionResult, ActionSnapshot, now
from linkedin_bot.domain.policy import block_reasons, category
from linkedin_bot.storage.database import Database

APPROVAL_LIFETIME = timedelta(hours=24)


class EffectAdapter(Protocol):
    async def perform(self, action: ActionSnapshot) -> ActionResult: ...


class Executor:
    def __init__(
        self,
        database: Database,
        adapters: Mapping[str, EffectAdapter] | None = None,
        timezone: str = "UTC",
    ) -> None:
        self.database = database
        self.adapters = dict(adapters or {})
        self.zone = ZoneInfo(timezone)

    def local_date(self, at: datetime) -> str:
        return at.astimezone(self.zone).date().isoformat()

    async def evaluate(self, action: ActionSnapshot, at: datetime | None = None) -> tuple[str, ...]:
        at = at or now()
        used = (await self.database.used_today(self.local_date(at)))[category(action)]
        return block_reasons(
            action,
            await self.database.policy(),
            used_today=used,
            has_adapter=action.kind in self.adapters,
        )

    async def propose(self, action: ActionSnapshot) -> tuple[str, ...]:
        """Blocked reasons, or an empty tuple when the action now awaits the user's approval."""
        await self.database.save_action(action)
        reasons = await self.evaluate(action)
        if reasons:
            await self.database.block_action(action, reasons[0])
        else:
            await self.database.await_approval(action, now() + APPROVAL_LIFETIME)
        return reasons

    async def state(self, logical_key: str) -> str:
        row = await self.database.call(
            lambda c: c.execute(
                "SELECT state FROM actions WHERE logical_key=? ORDER BY rowid DESC LIMIT 1",
                (logical_key,),
            ).fetchone()
        )
        return str(row[0])

    async def approval_id(self, action_id: str) -> str:
        approval = await self.database.approval_for(action_id)
        if approval is None:
            raise LookupError("no_live_approval")
        return approval

    async def decide(self, approval_id: str, *, approve: bool, at: datetime | None = None) -> str:
        return await self.database.decide_approval(approval_id, approve, at or now())

    async def run(self, action_id: str) -> tuple[str, ...]:
        """Dispatch an approved action. Returns block reasons if a hard constraint now applies."""
        action, _ = await self.database.action(action_id)
        started = now()
        reasons = await self.evaluate(action, started)
        if reasons:
            await self.database.block_ready(action, reasons[0])
            return reasons
        local_date = self.local_date(started)
        policy = await self.database.policy()
        kind = category(action)
        cap = policy.application_cap if kind == "application" else policy.communication_cap
        attempt = await self.database.begin_attempt(action, local_date, kind, cap, started)
        await self.database.mark_dispatch(attempt, now())
        try:
            result = await self.adapters[action.kind].perform(action)
        except Exception:
            # The effect may have happened; never guess, and keep the slot reserved.
            result = ActionResult(outcome="unknown", reason_code="adapter_exception")
        await self.database.finish_attempt(attempt, result)
        return ()


DraftExecutor = Executor
