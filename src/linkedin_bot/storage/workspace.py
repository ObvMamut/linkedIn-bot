"""Pins, saved searches, digest settings and pending text input (single-user, local)."""

import json
import sqlite3
from typing import Literal

from linkedin_bot.domain.models import identifier, now
from linkedin_bot.domain.workspace import DigestSettings, Pin, SavedSearch
from linkedin_bot.storage.database import Database, transaction

PROVIDERS = ("greenhouse", "lever", "ashby")
SEEN_LIMIT = 500


class WorkspaceStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    # --- pins ---------------------------------------------------------------------------
    async def pin(self, kind: Literal["firm", "person"], url: str, name: str) -> None:
        await self.database.call(
            lambda c: c.execute(
                "INSERT INTO pins(kind,url,name,pinned_at) VALUES (?,?,?,?) "
                "ON CONFLICT(kind,url) DO UPDATE SET name=excluded.name",
                (kind, url, name, now().isoformat()),
            )
        )

    async def unpin(self, kind: str, url: str) -> bool:
        cursor = await self.database.call(
            lambda c: c.execute("DELETE FROM pins WHERE kind=? AND url=?", (kind, url))
        )
        return bool(cursor.rowcount)

    async def pins(self, kind: str | None = None) -> tuple[Pin, ...]:
        def read(connection: sqlite3.Connection) -> tuple[Pin, ...]:
            query = (
                "SELECT kind,url,name,note,tags,board_provider,board_slug,pinned_at FROM pins"
                + (" WHERE kind=?" if kind else "")
                + " ORDER BY pinned_at,url"
            )
            rows = connection.execute(query, (kind,) if kind else ())
            return tuple(
                Pin(
                    kind=r[0],
                    url=r[1],
                    name=r[2],
                    note=r[3],
                    tags=tuple(json.loads(r[4])),
                    board_provider=r[5],
                    board_slug=r[6],
                    pinned_at=r[7],
                )
                for r in rows
            )

        return await self.database.call(read)

    async def set_note(self, kind: str, url: str, note: str) -> None:
        Pin(kind="firm", url=url, name="x", note=note, pinned_at="x")  # validates the length
        await self.database.call(
            lambda c: c.execute("UPDATE pins SET note=? WHERE kind=? AND url=?", (note, kind, url))
        )

    async def set_tags(self, kind: str, url: str, tags: tuple[str, ...]) -> None:
        await self.database.call(
            lambda c: c.execute(
                "UPDATE pins SET tags=? WHERE kind=? AND url=?", (json.dumps(list(tags)), kind, url)
            )
        )

    async def link_board(self, url: str, provider: str, slug: str) -> None:
        if provider not in PROVIDERS:
            raise ValueError("unknown_provider")
        await self.database.call(
            lambda c: c.execute(
                "UPDATE pins SET board_provider=?,board_slug=? WHERE kind='firm' AND url=?",
                (provider, slug, url),
            )
        )

    async def pinned_tokens(self) -> set[str]:
        """Lower-case names and board slugs of pinned firms, for ranking."""
        tokens: set[str] = set()
        for pin in await self.pins("firm"):
            tokens.add(pin.name.lower())
            if pin.board_slug:
                tokens.add(pin.board_slug.lower())
        return tokens

    # --- saved searches -----------------------------------------------------------------
    async def save_search(self, kind: str, query: str, name: str) -> str:
        def write(connection: sqlite3.Connection) -> str:
            row = connection.execute(
                "SELECT id FROM saved_searches WHERE kind=? AND query=?", (kind, query)
            ).fetchone()
            if row:
                return str(row[0])
            identity = identifier()
            connection.execute(
                "INSERT INTO saved_searches(id,name,kind,query) VALUES (?,?,?,?)",
                (identity, name, kind, query),
            )
            return identity

        return await self.database.call(write)

    async def saved(self) -> tuple[SavedSearch, ...]:
        rows = await self.database.call(
            lambda c: c.execute(
                "SELECT id,name,kind,query,last_run FROM saved_searches ORDER BY rowid"
            ).fetchall()
        )
        return tuple(
            SavedSearch(id=r[0], name=r[1], kind=r[2], query=r[3], last_run=r[4]) for r in rows
        )

    async def delete_saved(self, search_id: str) -> bool:
        cursor = await self.database.call(
            lambda c: c.execute("DELETE FROM saved_searches WHERE id=?", (search_id,))
        )
        return bool(cursor.rowcount)

    async def record_run(self, search_id: str, keys: list[str]) -> tuple[str, ...]:
        """Store this run and return the keys not seen in any earlier run."""

        def write(connection: sqlite3.Connection) -> tuple[str, ...]:
            with transaction(connection):
                row = connection.execute(
                    "SELECT seen FROM saved_searches WHERE id=?", (search_id,)
                ).fetchone()
                if row is None:
                    return ()
                seen = json.loads(row[0])
                fresh = tuple(key for key in keys if key not in seen)
                kept = (seen + list(fresh))[-SEEN_LIMIT:]
                connection.execute(
                    "UPDATE saved_searches SET seen=?,last_run=? WHERE id=?",
                    (json.dumps(kept), now().isoformat(), search_id),
                )
                return fresh

        return await self.database.call(write)

    # --- digest -------------------------------------------------------------------------
    async def digest(self) -> DigestSettings:
        row = await self.database.call(
            lambda c: c.execute(
                "SELECT enabled,local_time,alerts_boards,alerts_messages,quiet_start,quiet_end,"
                "last_digest_date,last_alert_at FROM digest_settings"
            ).fetchone()
        )
        return DigestSettings(
            enabled=bool(row[0]),
            local_time=row[1],
            alerts_boards=bool(row[2]),
            alerts_messages=bool(row[3]),
            quiet_start=row[4],
            quiet_end=row[5],
            last_digest_date=row[6],
            last_alert_at=row[7],
        )

    async def update_digest(self, **changes: object) -> DigestSettings:
        updated = DigestSettings.model_validate(
            {**(await self.digest()).model_dump(), **changes}, strict=False
        )
        await self.database.call(
            lambda c: c.execute(
                "UPDATE digest_settings SET enabled=?,local_time=?,alerts_boards=?,"
                "alerts_messages=?,quiet_start=?,quiet_end=?,last_digest_date=?,last_alert_at=?",
                (
                    int(updated.enabled),
                    updated.local_time,
                    int(updated.alerts_boards),
                    int(updated.alerts_messages),
                    updated.quiet_start,
                    updated.quiet_end,
                    updated.last_digest_date,
                    updated.last_alert_at,
                ),
            )
        )
        return updated

    # --- pending text input -------------------------------------------------------------
    async def awaiting(self) -> tuple[str, dict[str, object]] | None:
        row = await self.database.call(
            lambda c: c.execute("SELECT awaiting,context FROM ui_state").fetchone()
        )
        return None if row[0] is None else (row[0], json.loads(row[1]))

    async def set_awaiting(self, name: str, context: dict[str, object]) -> None:
        await self.database.call(
            lambda c: c.execute(
                "UPDATE ui_state SET awaiting=?,context=?", (name, json.dumps(context))
            )
        )

    async def clear_awaiting(self) -> None:
        await self.database.call(
            lambda c: c.execute("UPDATE ui_state SET awaiting=NULL,context='{}'")
        )
