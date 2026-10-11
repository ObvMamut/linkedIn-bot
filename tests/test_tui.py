import asyncio
import logging
from contextlib import asynccontextmanager

from textual.widgets import DataTable, Input, Static, TabbedContent

from linkedin_bot.domain.models import SourceObservation, digest, now
from linkedin_bot.runtime.config import Config
from linkedin_bot.runtime.services import Services
from linkedin_bot.storage.database import Database
from linkedin_bot.tui.app import CockpitApp
from linkedin_bot.tui.logbridge import EventBuffer
from linkedin_bot.tui.widgets import ConfirmModal, HelpModal
from tests.test_ui import UIConnector

SIZE = (160, 50)


def posting(external_id, title, company="beta", location="Berlin"):
    return SourceObservation(
        source="greenhouse",
        external_id=external_id,
        url=f"https://boards.greenhouse.io/{company}/jobs/{external_id}",
        title=title,
        company=company,
        location=location,
        content_hash=digest(f"{external_id}{title}".encode()),
        observed_at=now(),
    )


def cockpit(tmp_path, scenario, *, seed=True):
    async def main():
        db = Database(tmp_path)
        try:
            await db.open()
            connector = UIConnector()

            @asynccontextmanager
            async def opener():
                yield connector

            async def finder(name):
                return ("ashby", "beta") if name.lower().startswith("beta") else None

            config = Config.model_validate({"data_dir": str(tmp_path)})
            services = Services(config, db, opener=opener, finder=finder)
            if seed:
                await services.ops.edit_list("roles", add=["security engineer"])
                await db.save_observations(
                    [posting("1", "Security Engineer"), posting("2", "Security Engineer Intern")]
                )
            events = EventBuffer()
            logging.getLogger().addHandler(events)
            app = CockpitApp(services, events=events, background=False, refresh_seconds=3600)
            try:
                async with app.run_test(size=SIZE) as pilot:
                    await pilot.pause()
                    return await scenario(app, pilot, db, services, connector)
            finally:
                logging.getLogger().removeHandler(events)
                await services.aclose()
        finally:
            await db.close()

    return asyncio.run(main())


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()
        await asyncio.sleep(0.02)


def active(app):
    return app.query_one("#tabs", TabbedContent).active


def test_dashboard_shows_counts_and_number_keys_switch_views(tmp_path):
    async def scenario(app, pilot, db, services, connector):
        assert active(app) == "dashboard"
        assert str(app.query_one("#card-postings", Static).render()) == "2"
        assert str(app.query_one("#card-matches", Static).render()) == "2"
        for key, tab in (
            ("2", "jobs"),
            ("7", "boards"),
            ("0", "profile"),
            ("f3", "logs"),
            ("1", "dashboard"),
        ):
            await pilot.press(key)
            await settle(pilot)
            assert active(app) == tab
        assert "draft_only" in str(app.query_one("#statusbar", Static).render())

    cockpit(tmp_path, scenario)


def test_jobs_are_ranked_filtered_and_tracked(tmp_path):
    async def scenario(app, pilot, db, services, connector):
        await pilot.press("2")
        await settle(pilot)
        table = app.query_one("#jobs-table", DataTable)
        assert table.row_count == 2
        table.focus()
        await pilot.press("a")  # applied
        await settle(pilot)
        statuses = await db.status_counts()
        assert statuses == {"applied": 1}
        await pilot.press("down", "x")  # dismiss the second
        await settle(pilot)
        assert table.row_count == 1
        app.query_one("#filter", Input).value = "nothing matches this"
        await settle(pilot)
        assert table.row_count == 0
        app.query_one("#filter", Input).value = ""
        await settle(pilot)
        table.focus()
        await pilot.press("p")  # pin the firm behind the posting
        await settle(pilot)
        [pin] = await services.ops.store.pins("firm")
        assert pin.board_provider == "greenhouse" and pin.board_slug == "beta"

    cockpit(tmp_path, scenario)


def test_search_runs_pins_and_saves(tmp_path):
    async def scenario(app, pilot, db, services, connector):
        await pilot.press("3")
        await settle(pilot)
        query = app.query_one("#query", Input)
        query.focus()
        query.value = "security in Berlin"
        await pilot.press("enter")
        await settle(pilot, 8)
        assert ("jobs", "security", "Berlin") in connector.calls
        results = app.query_one("#results", DataTable)
        assert results.row_count == 3
        results.focus()
        await pilot.press("s")
        await settle(pilot)
        assert [s.query for s in await services.ops.store.saved()] == ["security in Berlin"]
        await pilot.press("p")
        await settle(pilot)
        assert len(await services.ops.store.pins("firm")) == 1

    cockpit(tmp_path, scenario)


def test_settings_lists_and_switches_save_immediately(tmp_path):
    async def scenario(app, pilot, db, services, connector):
        await pilot.press("8")
        await settle(pilot, 5)
        field = app.query_one("#list-locations", Input)
        field.focus()
        field.value = "Berlin, Paris"
        await pilot.press("enter")
        await settle(pilot, 5)
        assert (await db.preferences()).locations == ("Berlin", "Paris")
        app.query_one("#digest-enabled").value = True
        await settle(pilot, 5)
        assert (await services.ops.store.digest()).enabled is True
        assert "posting(s) match" in str(app.query_one("#preview", Static).render())

    cockpit(tmp_path, scenario)


def test_policy_change_needs_confirmation(tmp_path):
    async def scenario(app, pilot, db, services, connector):
        await pilot.press("9")
        await settle(pilot, 5)
        app.query_one("#mode").value = "reviewed"
        await settle(pilot)
        app.query_one("#apply-policy").press()
        await settle(pilot, 5)
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("escape")
        await settle(pilot, 5)
        assert (await db.policy()).mode == "draft_only"
        app.query_one("#mode").value = "reviewed"
        app.query_one("#apply-policy").press()
        await settle(pilot, 5)
        await pilot.press("y")
        await settle(pilot, 5)
        assert (await db.policy()).mode == "reviewed"

    cockpit(tmp_path, scenario)


def test_pause_help_logs_and_quit(tmp_path):
    async def scenario(app, pilot, db, services, connector):
        await pilot.press("P")
        await settle(pilot)
        assert (await db.policy()).paused is True
        await pilot.press("question_mark")
        await settle(pilot)
        assert isinstance(app.screen, HelpModal)
        await pilot.press("escape")
        await settle(pilot)
        logging.getLogger().warning("", extra={"event": "something_happened"})
        await pilot.press("f3")
        await settle(pilot, 30)
        log = app.query_one("#log")
        assert any("something_happened" in line.text for line in log.lines)
        await pilot.press("q")
        await settle(pilot)
        assert app.stop.is_set()

    cockpit(tmp_path, scenario)


def test_profile_onboarding_and_facts(tmp_path):
    async def scenario(app, pilot, db, services, connector):
        await pilot.press("0")
        await settle(pilot, 5)
        assert app.query_one("#onboarding").display is True
        for step, value in (
            ("roles", "Security intern"),
            ("locations", "Berlin"),
            ("exclusions", "none"),
            ("eligibility", "EU citizen"),
        ):
            app.query_one(f"#answer-{step}", Input).value = value
        app.query_one("#answer-cv").text = "Jane Doe\nPython"
        app.query_one("#finish").press()
        await settle(pilot, 8)
        assert await services.onboarding.done()
        assert app.query_one("#onboarding").display is False
        assert app.query_one("#facts", DataTable).row_count == 1

    cockpit(tmp_path, scenario, seed=False)
