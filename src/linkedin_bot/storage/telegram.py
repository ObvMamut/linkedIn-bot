"""Telegram pairing and onboarding state, kept in SQLite so both survive restarts."""

import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta

from linkedin_bot.domain.models import digest
from linkedin_bot.runtime.privacy import BoundaryError
from linkedin_bot.storage.database import Database, transaction

CODE_LIFETIME = timedelta(minutes=10)
MAX_CODE_ATTEMPTS = 5


class TelegramStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def begin_pairing(self, at: datetime) -> str:
        code = secrets.token_urlsafe(9)

        def write(connection: sqlite3.Connection) -> None:
            if connection.execute("SELECT user_id FROM telegram_binding").fetchone()[0] is not None:
                raise BoundaryError("already_paired")
            connection.execute(
                "UPDATE telegram_binding SET code_hash=?,code_expires_at=?,code_attempts=0",
                (digest(code.encode()), (at + CODE_LIFETIME).isoformat()),
            )

        await self.database.call(write)
        return code

    async def pair(self, code: str, user_id: int, chat_id: int, at: datetime) -> bool:
        """One-use, expiring, attempt-limited. Callers pass private chats only."""

        def write(connection: sqlite3.Connection) -> bool:
            with transaction(connection):
                bound, stored, expires, attempts = connection.execute(
                    "SELECT user_id,code_hash,code_expires_at,code_attempts FROM telegram_binding"
                ).fetchone()
                if bound is not None or stored is None:
                    return False
                if attempts >= MAX_CODE_ATTEMPTS or at > datetime.fromisoformat(expires):
                    return False
                if not hmac.compare_digest(stored, digest(code.encode())):
                    connection.execute("UPDATE telegram_binding SET code_attempts=code_attempts+1")
                    return False
                connection.execute(
                    "UPDATE telegram_binding SET user_id=?,chat_id=?,paired_at=?,"
                    "code_hash=NULL,code_expires_at=NULL,code_attempts=0",
                    (user_id, chat_id, at.isoformat()),
                )
                return True

        return await self.database.call(write)

    async def unpair(self) -> bool:
        """Forget the paired chat so `pair` can bind a new one. Update IDs stay recorded."""
        cursor = await self.database.call(
            lambda c: c.execute(
                "UPDATE telegram_binding SET user_id=NULL,chat_id=NULL,paired_at=NULL,"
                "code_hash=NULL,code_expires_at=NULL,code_attempts=0 WHERE user_id IS NOT NULL"
            )
        )
        return bool(cursor.rowcount)

    async def binding(self) -> tuple[int, int] | None:
        row = await self.database.call(
            lambda c: c.execute("SELECT user_id,chat_id FROM telegram_binding").fetchone()
        )
        return None if row[0] is None else (row[0], row[1])

    async def polling_allowed(self, at: datetime) -> bool:
        """Listen only when paired or while an unexpired pairing code is waiting."""
        user, expires = await self.database.call(
            lambda c: c.execute("SELECT user_id,code_expires_at FROM telegram_binding").fetchone()
        )
        return user is not None or (expires is not None and at <= datetime.fromisoformat(expires))

    async def last_update_id(self) -> int:
        return await self.database.call(
            lambda c: c.execute("SELECT last_update_id FROM telegram_binding").fetchone()[0]
        )

    async def record_update(self, update_id: int) -> None:
        await self.database.call(
            lambda c: c.execute(
                "UPDATE telegram_binding SET last_update_id=MAX(last_update_id,?)", (update_id,)
            )
        )

    async def onboarding(self) -> tuple[str, dict[str, str]]:
        step, answers = await self.database.call(
            lambda c: c.execute("SELECT step,answers FROM onboarding").fetchone()
        )
        return step, json.loads(answers)

    async def set_onboarding(self, step: str, answers: dict[str, str]) -> None:
        await self.database.call(
            lambda c: c.execute(
                "UPDATE onboarding SET step=?,answers=?", (step, json.dumps(answers))
            )
        )
