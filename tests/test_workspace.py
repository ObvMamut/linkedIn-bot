import asyncio

import pytest

from linkedin_bot.domain.models import SearchPreferences
from linkedin_bot.storage.database import Database
from linkedin_bot.storage.restore import restore_backup
from linkedin_bot.storage.workspace import WorkspaceStore

FIRM = "https://www.linkedin.com/company/acme"
PERSON = "https://www.linkedin.com/in/jane-doe"


def run(tmp_path, scenario):
    async def main():
        db = Database(tmp_path)
        try:
            await db.open()
            return await scenario(db, WorkspaceStore(db))
        finally:
            await db.close()

    return asyncio.run(main())


def test_pins_are_added_updated_listed_and_removed(tmp_path):
    async def scenario(db, store):
        await store.pin("firm", FIRM, "Acme")
        await store.pin("firm", FIRM, "Acme Corp")  # pinning again renames, never duplicates
        await store.pin("person", PERSON, "Jane Doe")
        assert [(p.name, p.kind) for p in await store.pins("firm")] == [("Acme Corp", "firm")]
        assert len(await store.pins()) == 2
        await store.set_note("firm", FIRM, "great security team")
        await store.set_tags("firm", FIRM, ("security", "defense"))
        [firm] = await store.pins("firm")
        assert firm.note == "great security team" and firm.tags == ("security", "defense")
        assert await store.unpin("firm", FIRM) is True
        assert await store.unpin("firm", FIRM) is False
        assert await store.pins("firm") == ()

    run(tmp_path, scenario)


def test_linking_a_job_board_feeds_pinned_tokens_for_ranking(tmp_path):
    async def scenario(db, store):
        await store.pin("firm", FIRM, "Acme Rockets")
        await store.link_board(FIRM, "greenhouse", "acmerockets")
        await store.pin("person", PERSON, "Jane Doe")  # people never become firm tokens
        assert await store.pinned_tokens() == {"acme rockets", "acmerockets"}
        [firm] = await store.pins("firm")
        assert (firm.board_provider, firm.board_slug) == ("greenhouse", "acmerockets")
        with pytest.raises(ValueError):
            await store.link_board(FIRM, "workday", "x")

    run(tmp_path, scenario)


def test_saved_searches_report_only_new_results_since_the_last_run(tmp_path):
    async def scenario(db, store):
        first = await store.save_search("people", "ml engineer", "ML people")
        assert await store.save_search("people", "ml engineer", "again") == first  # same query
        assert await store.record_run(first, ["a", "b"]) == ("a", "b")
        assert await store.record_run(first, ["a", "b", "c"]) == ("c",)
        assert await store.record_run(first, ["a"]) == ()
        [saved] = await store.saved()
        assert saved.name == "ML people" and saved.last_run is not None
        assert await store.delete_saved(first) is True
        assert await store.saved() == ()

    run(tmp_path, scenario)


def test_seen_keys_are_bounded(tmp_path):
    async def scenario(db, store):
        saved = await store.save_search("jobs", "intern", "x")
        await store.record_run(saved, [str(i) for i in range(600)])
        keys = await db.call(lambda c: c.execute("SELECT seen FROM saved_searches").fetchone()[0])
        assert len(__import__("json").loads(keys)) == 500

    run(tmp_path, scenario)


def test_digest_settings_default_off_and_validate_changes(tmp_path):
    async def scenario(db, store):
        digest = await store.digest()
        assert (digest.enabled, digest.local_time, digest.alerts_messages) == (
            False,
            "18:00",
            False,
        )
        updated = await store.update_digest(enabled=True, local_time="07:30")
        assert updated.enabled and updated.local_time == "07:30"
        with pytest.raises(ValueError):
            await store.update_digest(local_time="25:99")
        with pytest.raises(ValueError):
            await store.update_digest(not_a_field=1)
        assert (await store.digest()).local_time == "07:30"

    run(tmp_path, scenario)


def test_pending_input_state_roundtrips_and_clears(tmp_path):
    async def scenario(db, store):
        assert await store.awaiting() is None
        await store.set_awaiting("add_location", {"screen": "settings"})
        assert await store.awaiting() == ("add_location", {"screen": "settings"})
        await store.clear_awaiting()
        assert await store.awaiting() is None

    run(tmp_path, scenario)


def test_update_preferences_applies_a_change_as_a_new_revision(tmp_path):
    async def scenario(db, store):
        updated = await db.update_preferences(lambda p: p.model_copy(update={"remote": True}))
        assert updated.revision == 2 and updated.remote is True
        again = await db.update_preferences(
            lambda p: p.model_copy(update={"roles": (*p.roles, "analyst")})
        )
        assert again.revision == 3 and again.roles == ("analyst",) and again.remote is True
        assert isinstance(await db.preferences(), SearchPreferences)

    run(tmp_path, scenario)


def test_restore_keeps_pins_and_saved_searches(tmp_path):
    async def make():
        db = Database(tmp_path / "src")
        try:
            await db.open()
            store = WorkspaceStore(db)
            await store.pin("firm", FIRM, "Acme")
            await store.save_search("firms", "palantir", "Palantir")
            return await db.backup()
        finally:
            await db.close()

    snapshot = asyncio.run(make())
    restore_backup(snapshot, tmp_path / "restored")

    async def check():
        db = Database(tmp_path / "restored")
        try:
            await db.open()
            store = WorkspaceStore(db)
            return len(await store.pins()), len(await store.saved())
        finally:
            await db.close()

    assert asyncio.run(check()) == (1, 1)
