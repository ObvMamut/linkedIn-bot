"""Safe M1 executor entry point. No live effect adapter is registered."""

from linkedin_bot.domain.models import ActionSnapshot
from linkedin_bot.domain.policy import block_reasons
from linkedin_bot.storage.database import Database


class DraftExecutor:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def propose(self, action: ActionSnapshot) -> tuple[str, ...]:
        await self.database.save_action(action)
        reasons = await self.evaluate(action)
        await self.database.block_action(action, reasons[0])
        return reasons

    async def evaluate(self, action: ActionSnapshot) -> tuple[str, ...]:
        policy = await self.database.policy()
        return block_reasons(action, policy)
