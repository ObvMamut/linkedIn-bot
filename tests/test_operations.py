import asyncio
from contextlib import asynccontextmanager

import pytest

from linkedin_bot.domain.models import SourceObservation, digest, now
from linkedin_bot.runtime.config import Config
from linkedin_bot.runtime.services import Services
from linkedin_bot.storage.database import Database
from linkedin_bot.storage.telegram import TelegramStore
from linkedin_bot.workflows.onboarding import Onboarding, baseline_cv, save_cv
from linkedin_bot.workflows.operations import Operations, split_items
from tests.test_telegram import CHAT, USER, FakeAPI
from tests.test_ui import UIConnector


def posting(external_id, title, company="beta", location="Berlin"):
    return SourceObservation(
        source="greenhouse",
        external_id=external_id,
        url=f"https://boards.greenhouse.io/{company}/jobs/{external_id}",
        title=title,
        company=company,
        location=location,
        content_hash=digest(f"{external_id}{title}".encode()),
    )


def env(tmp_path, scenario, connector=None, finder=None):
    async def main():
        db = Database(tmp_path)
        try:
            await db.open()
            conn = connector or UIConnector()

            @asynccontextmanager
            async def opener():
                yield conn

            config = Config.model_validate({"data_dir": str(tmp_path)})
            services = Services(config, db, opener=opener, finder=finder)
            try:
                return await scenario(db, services, conn)
            finally:
                await services.aclose()
        finally:
            await db.close()

    return asyncio.run(main())


def test_ranked_jobs_and_outside_postings(tmp_path):
    async def scenario(db, services, conn):
        ops: Operations = services.ops
        await ops.edit_list("roles", add=["security engineer"])
        await db.save_observations([posting("1", "Security Engineer"), posting("2", "Gardener")])
        ranked = await ops.ranked_jobs()
        assert [item.external_id for item in ranked] == ["1"]
        everything = await ops.ranked_jobs(include_outside=True)
        assert [(i.external_id, i.matched) for i in everything] == [("1", True), ("2", False)]
        assert await ops.matching_count() == 1

    env(tmp_path, scenario)


def test_search_saves_linkedin_jobs_and_saved_search_reports_only_new(tmp_path):
    async def scenario(db, services, conn):
        ops = services.ops
        await ops.edit_list("roles", add=["security engineer"])
        items, error = await ops.search("jobs", "security in Berlin")
        assert error is None and [i.matched for i in items] == [True, True, False]
        assert items[-1].title.endswith("Gardener")  # outside your settings comes last
        assert ("jobs", "security", "Berlin") in conn.calls
        assert (await db.status())["postings"] == 3
        saved_id = await ops.save_search("jobs", "security in Berlin", [i.key for i in items])
        run = await ops.run_saved(saved_id)
        assert run.error is None and run.new_items == []
        await ops.rename_saved(saved_id, "Berlin security")
        assert (await ops.saved(saved_id)).name == "Berlin security"
        with pytest.raises(ValueError):
            await ops.save_search("jobs", "  ", [])
        people, _ = await ops.search("people", "jane")
        pin = await ops.pin(people[0])
        assert pin.kind == "person" and pin.name == "Person 1"

    env(tmp_path, scenario)


def test_linkedin_errors_come_back_as_short_messages(tmp_path):
    from linkedin_bot.adapters.linkedin.models import LinkedInError

    async def scenario(db, services, conn):
        items, error = await services.ops.search("people", "x")
        assert items is None and "sign in" in error.lower()

    env(tmp_path, scenario, connector=UIConnector(error=LinkedInError("auth_required")))


def test_preference_mutations_validate_and_persist(tmp_path):
    async def scenario(db, services, conn):
        ops = services.ops
        await ops.set_list("locations", ["Berlin", "berlin", " ", "Paris"])
        assert (await ops.preferences()).locations == ("Berlin", "Paris")
        await ops.edit_list("locations", delete=0)
        await ops.toggle_language("de")
        await ops.change_priority("up", "location")
        await ops.set_max_age(30)
        await ops.cycle_remote()
        await ops.toggle_internship()
        await ops.set_interests("x" * 3000)
        preferences = await ops.preferences()
        assert preferences.locations == ("Paris",) and preferences.languages == ("de",)
        assert preferences.priorities[0] == "location" and preferences.max_age_days == 30
        assert preferences.remote is True and preferences.internship is True
        assert len(preferences.interests) == 2000
        for bad in (
            ops.edit_list("nope", add=["x"]),
            ops.toggle_language("xx"),
            ops.change_priority("up", "nope"),
            ops.set_max_age(0),
        ):
            with pytest.raises(ValueError):
                await bad

    env(tmp_path, scenario)


def test_boards_are_validated_linked_and_scheduled(tmp_path):
    async def finder(name):
        return ("ashby", "beta") if name == "Beta" else None

    async def scenario(db, services, conn):
        ops = services.ops
        await ops.store.pin("firm", "https://www.linkedin.com/company/beta", "Beta")
        [pin] = await ops.store.pins("firm")
        assert await ops.link_board(pin) == ("ashby", "beta")
        work = (await db.status())["work"]
        assert work[0]["workflow"] == "ats:ashby:beta"
        [linked] = await ops.store.pins("firm")
        assert linked.board_slug == "beta"
        from linkedin_bot.adapters.public_http import AdapterError

        with pytest.raises(AdapterError):
            await ops.schedule_board("greenhouse", "../etc")

    env(tmp_path, scenario, finder=finder)


def test_split_items_bounds_length_and_count():
    assert split_items("a, b\nc,," + ",".join(["x"] * 30))[:3] == ["a", "b", "c"]
    assert len(split_items(",".join(str(i) for i in range(40)))) == 20
    assert split_items("y" * 61) == []


def test_terminal_onboarding_form_finishes_in_one_step(tmp_path):
    async def scenario(db, services, conn):
        onboarding: Onboarding = services.onboarding
        with pytest.raises(ValueError, match="missing_cv"):
            await onboarding.finish_all(
                {"roles": "a", "locations": "b", "exclusions": "none", "eligibility": "EU"}
            )
        await onboarding.finish_all(
            {
                "roles": "Security intern",
                "locations": "Berlin, remote ok",
                "exclusions": "none",
                "eligibility": "EU citizen",
                "cv": "Jane Doe\nPython",
            }
        )
        assert await onboarding.done()
        preferences = await db.preferences()
        assert preferences.remote is True and preferences.internship is True
        assert await baseline_cv(db) == "Jane Doe\nPython"
        await save_cv(db, "Jane Doe\nPython, Rust")
        assert await baseline_cv(db) == "Jane Doe\nPython, Rust"
        with pytest.raises(ValueError):
            await save_cv(db, "   ")

    env(tmp_path, scenario)


def test_services_deliver_alerts_to_extra_notifiers_without_telegram(tmp_path):
    class Sink:
        def __init__(self):
            self.messages = []

        async def ready(self):
            return True

        async def send(self, text, kind):
            self.messages.append((kind, text))

    async def scenario(db, services, conn):
        sink = Sink()
        services.scheduler.notifiers.append(sink)
        await services.ops.store.update_digest(alerts_messages=True)
        assert await services.scheduler.tick() == ["message_alert"]
        assert sink.messages[0][0] == "message_alert"

    env(tmp_path, scenario)


def test_services_with_telegram_share_one_operations_object(tmp_path):
    async def main():
        db = Database(tmp_path)
        try:
            await db.open()
            conn = UIConnector()

            @asynccontextmanager
            async def opener():
                yield conn

            api = FakeAPI()
            config = Config.model_validate({"data_dir": str(tmp_path)})
            services = Services(config, db, telegram=api, opener=opener)
            assert services.bot.ui.ops is services.ops
            assert not await services.telegram_ready()
            await TelegramStore(db).begin_pairing(now())
            await db.call(
                lambda c: c.execute("UPDATE telegram_binding SET user_id=?,chat_id=?", (USER, CHAT))
            )
            assert await services.telegram_ready()
            stop = asyncio.Event()
            services.start_background(stop)
            await asyncio.sleep(0.05)
            stop.set()
            await services.aclose()
            assert services.tasks == []
        finally:
            await db.close()

    asyncio.run(main())
