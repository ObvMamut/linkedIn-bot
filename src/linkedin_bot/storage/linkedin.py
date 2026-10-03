"""Session health only. No LinkedIn content (messages, profiles) is ever stored."""

from datetime import datetime

from linkedin_bot.storage.database import Database


class LinkedInStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def record(self, state: str, at: datetime) -> None:
        await self.database.call(
            lambda c: c.execute(
                "UPDATE linkedin_state SET state=?,"
                "last_success=CASE WHEN ?='ready' THEN ? ELSE last_success END",
                (state, state, at.isoformat()),
            )
        )

    async def state(self) -> tuple[str, str | None]:
        row = await self.database.call(
            lambda c: c.execute("SELECT state,last_success FROM linkedin_state").fetchone()
        )
        return row[0], row[1]
