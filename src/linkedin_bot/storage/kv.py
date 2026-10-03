"""Tiny key-value store for small bookkeeping (alert de-duplication and similar)."""

from linkedin_bot.storage.database import Database


class KV:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def get(self, key: str) -> str | None:
        row = await self.database.call(
            lambda c: c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        )
        return None if row is None else str(row[0])

    async def set(self, key: str, value: str) -> None:
        await self.database.call(
            lambda c: c.execute(
                "INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        )
