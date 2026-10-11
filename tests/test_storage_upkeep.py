import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

from linkedin_bot.domain.models import SourceObservation, digest, now
from linkedin_bot.storage.database import Database, prune_backups
from linkedin_bot.storage.telegram import TelegramStore
from linkedin_bot.workflows.maintenance import Maintenance
from linkedin_bot.workflows.operations import Operations


def posting(external_id, title="Security Engineer", observed=None, company="beta"):
    return SourceObservation(
        source="greenhouse",
        external_id=external_id,
        url=f"https://boards.greenhouse.io/{company}/jobs/{external_id}",
        title=title,
        company=company,
        location="Berlin",
        content_hash=digest(f"{external_id}{title}".encode()),
        observed_at=observed or now(),
    )


def with_db(tmp_path, scenario):
    async def main():
        db = Database(tmp_path)
        try:
            await db.open()
            return await scenario(db)
        finally:
            await db.close()

    return asyncio.run(main())


def test_tracker_status_hides_dismissed_and_keeps_notes(tmp_path):
    async def scenario(db):
        ops = Operations(db)
        await ops.edit_list("roles", add=["security engineer"])
        await db.save_observations([posting("1"), posting("2")])
        await ops.set_status("greenhouse", "1", "applied", "sent via site")
        await ops.set_status("greenhouse", "2", "dismissed")
        ranked = await ops.ranked_jobs()
        assert [(i.external_id, i.status, i.note) for i in ranked] == [
            ("1", "applied", "sent via site")
        ]
        everything = await ops.ranked_jobs(include_dismissed=True)
        assert {i.external_id for i in everything} == {"1", "2"}
        assert [i.external_id for i in await ops.tracked()][0] in {"1", "2"}
        await ops.set_status("greenhouse", "1", "interviewing")  # note survives a status change
        record = await db.posting("greenhouse", "1")
        assert (record.status, record.note) == ("interviewing", "sent via site")
        await ops.set_status("greenhouse", "2", "new")
        assert len(await db.opportunities(since=None)) == 2
        assert await db.status_counts() == {"interviewing": 1}
        with pytest.raises(LookupError):
            await ops.set_status("greenhouse", "nope", "applied")
        with pytest.raises(ValueError):
            await ops.set_status("greenhouse", "1", "hired")

    with_db(tmp_path, scenario)


def test_prune_drops_old_versions_but_keeps_first_sighting_and_tracked_postings(tmp_path):
    async def scenario(db):
        at = now()
        year_ago = at - timedelta(days=400)
        await db.save_observations(
            [
                posting("1", "Old title", observed=at - timedelta(days=300)),
                posting("1", "New title", observed=at - timedelta(days=1)),
                posting("2", observed=year_ago),
                posting("3", observed=year_ago),
                posting("4", observed=at - timedelta(days=100)),
            ]
        )
        await db.set_status("greenhouse", "3", "applied")
        await db.set_status("greenhouse", "4", "dismissed")
        result = await db.prune(at)
        assert result == {"old_versions": 1, "old_postings": 2}
        rows = {
            r.observation.external_id: r for r in await db.postings(None, include_dismissed=True)
        }
        assert set(rows) == {"1", "3"}
        assert rows["1"].observation.title == "New title"
        assert rows["1"].first_seen < at - timedelta(days=299)  # still the first sighting

    with_db(tmp_path, scenario)


def test_daily_maintenance_snapshots_rotates_and_runs_once_a_day(tmp_path):
    class Clock:
        at = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

        def __call__(self):
            return self.at

    async def scenario(db):
        clock = Clock()
        maintenance = Maintenance(db, "UTC", keep_backups=2, clock=clock)
        manual = await db.backup()
        for day in range(4):
            clock.at = datetime(2026, 10, 5 + day, 12, 0, tzinfo=UTC)
            assert await maintenance.tick() is not None
            assert await maintenance.tick() is None  # once per day
            await asyncio.sleep(0.01)  # distinct snapshot times
        names = sorted(p.name for p in (tmp_path / "backups").iterdir())
        assert manual.name in names  # manual snapshots are never rotated away
        assert sum(name.startswith("auto-") for name in names) == 2
        assert all(os.stat(tmp_path / "backups" / n).st_mode & 0o077 == 0 for n in names)
        assert prune_backups(tmp_path, keep=1) == 1

    with_db(tmp_path, scenario)


def test_unpair_frees_the_binding_for_a_new_pairing(tmp_path):
    async def scenario(db):
        store = TelegramStore(db)
        assert await store.unpair() is False
        code = await store.begin_pairing(now())
        assert await store.pair(code, 1, 2, now())
        await store.record_update(42)
        assert await store.unpair() is True
        assert await store.binding() is None and await store.last_update_id() == 42
        await store.begin_pairing(now())  # allowed again

    with_db(tmp_path, scenario)
