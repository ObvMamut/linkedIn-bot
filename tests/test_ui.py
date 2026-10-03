# ruff: noqa: E501  (fixture text)
import asyncio
from contextlib import asynccontextmanager

from linkedin_bot.adapters.linkedin.models import Company, Job, LinkedInError, Person
from linkedin_bot.adapters.linkedin.pages import Parsed
from linkedin_bot.domain.models import CollectionPage, SourceObservation, digest, now
from linkedin_bot.storage.database import Database
from linkedin_bot.storage.linkedin import LinkedInStore
from linkedin_bot.storage.telegram import TelegramStore
from linkedin_bot.storage.workspace import WorkspaceStore
from linkedin_bot.workflows.linkedin_commands import LinkedInCommands
from linkedin_bot.workflows.telegram_bot import Bot
from tests.test_linkedin_commands import FakeConnector
from tests.test_telegram import FakeAPI, callback, update

PEOPLE = tuple(
    Person(url=f"https://www.linkedin.com/in/p{i}", name=f"Person {i}", headline=f"Headline {i}")
    for i in range(1, 4)
)
FIRMS = (
    Company(url="https://www.linkedin.com/company/acme", name="Acme Rockets", tagline="Aerospace"),
    Company(url="https://www.linkedin.com/company/beta", name="Beta Security", tagline="Cyber"),
)


JOBS = tuple(
    Job(
        id=str(i),
        url=f"https://www.linkedin.com/jobs/view/{i}",
        title=title,
        company=company,
        location=location,
    )
    for i, (title, company, location) in enumerate(
        (
            ("Gardener", "Green Co", "Berlin"),
            ("Security Engineer Intern", "Beta Security", "Berlin, Germany (On-site)"),
            ("Security Engineer", "Alpha Labs", "Paris"),
        ),
        start=1,
    )
)


class UIConnector(FakeConnector):
    async def search_jobs(self, query, location=None, **kw):
        self.calls.append(("jobs", query, location))
        self._maybe_fail()
        return Parsed(JOBS, 0)

    async def search_people(self, query, **kw):
        self.calls.append(("people", query))
        self._maybe_fail()
        return Parsed(PEOPLE, 0)

    async def search_companies(self, query, **kw):
        self.calls.append(("firms", query))
        self._maybe_fail()
        return Parsed(FIRMS, 0)


def flat(markup):
    """[(label, data-or-url)] for every button."""
    rows = (markup or {}).get("inline_keyboard", [])
    return [(b["text"], b.get("callback_data") or b.get("url")) for row in rows for b in row]


def find(markup, fragment):
    matches = [data for label, data in flat(markup) if fragment in label]
    assert matches, f"no button containing {fragment!r} in {flat(markup)}"
    return matches[0]


def ui_env(tmp_path, scenario, connector=None, board_finder=None):
    async def main():
        db = Database(tmp_path)
        try:
            await db.open()
            connector_ = connector or UIConnector()

            @asynccontextmanager
            async def opener():
                yield connector_

            api = FakeAPI()
            commands = LinkedInCommands(opener, LinkedInStore(db))
            bot = Bot(db, api, linkedin=commands, board_finder=board_finder)
            await bot.handle(update(1, f"/start {await TelegramStore(db).begin_pairing(now())}"))
            api.sent.clear()
            api.markups.clear()
            return await scenario(db, api, bot, connector_)
        finally:
            await db.close()

    return asyncio.run(main())


class Counter:
    n = 100

    @classmethod
    def next(cls):
        cls.n += 1
        return cls.n


def tap(bot, data, **kw):
    return bot.handle(callback(Counter.next(), data, **kw))


def say(bot, text, **kw):
    return bot.handle(update(Counter.next(), text, **kw))


def test_menu_lists_the_main_areas_and_every_callback_fits_telegrams_limit(tmp_path):
    async def scenario(db, api, bot, conn):
        await say(bot, "/menu")
        markup = api.markups[-1]
        labels = [label for label, _ in flat(markup)]
        for area in ("Messages", "Search", "Pinned", "Jobs", "Saved", "Settings", "Status"):
            assert any(area in label for label in labels), area
        assert all(len((data or "").encode()) <= 64 for _, data in flat(markup))

    ui_env(tmp_path, scenario)


def test_strangers_and_wrong_chats_cannot_press_buttons(tmp_path):
    async def scenario(db, api, bot, conn):
        await tap(bot, "m:msgs", user=999)
        await tap(bot, "m:msgs", chat=333)
        await tap(bot, "m:msgs", chat_type="group")
        assert api.edits == [] and api.answers == [] and conn.calls == []

    ui_env(tmp_path, scenario)


def test_unknown_or_expired_buttons_are_refused_politely(tmp_path):
    async def scenario(db, api, bot, conn):
        await tap(bot, "r:nonexistent:0")
        await tap(bot, "zzz:what")
        assert all(
            "expired" in text.lower() or "unknown" in text.lower() for _, text in api.answers
        )
        assert len(api.answers) == 2 and api.edits == []

    ui_env(tmp_path, scenario)


def test_messages_screen_shows_loading_then_content_and_refresh_bypasses_the_cache(tmp_path):
    async def scenario(db, api, bot, conn):
        await tap(bot, "m:msgs")
        assert api.edits[0][2].startswith("⏳")  # instant feedback while LinkedIn loads
        text, markup = api.edits[-1][2], api.edits[-1][3]
        assert "Jane Doe" in text and "unread" in text
        assert conn.refreshes[-1] is False
        await tap(bot, find(markup, "Refresh"))
        assert conn.refreshes[-1] is True
        assert api.answers  # every press is acknowledged so the button stops spinning

    ui_env(tmp_path, scenario)


def test_linkedin_errors_become_a_screen_with_a_way_back(tmp_path):
    async def scenario(db, api, bot, conn):
        await tap(bot, "m:msgs")
        text, markup = api.edits[-1][2], api.edits[-1][3]
        assert "sign in" in text.lower() and any(data == "m:main" for _, data in flat(markup))

    ui_env(tmp_path, scenario, connector=UIConnector(error=LinkedInError("auth_required")))


def test_search_flow_input_results_paging_pin_open_and_save(tmp_path):
    async def scenario(db, api, bot, conn):
        await tap(bot, "m:search")
        await tap(bot, find(api.edits[-1][3], "People"))
        assert "search words" in api.edits[-1][2].lower()
        await say(bot, "machine learning")
        assert ("people", "machine learning") in conn.calls
        assert api.actions and api.actions[-1][1] == "typing"
        text, markup = api.edits[-1][2], api.edits[-1][3]
        assert "Person 1" in text and "1/3" in text
        assert any(label == "🔗 Open" and data.endswith("/in/p1") for label, data in flat(markup))
        await tap(bot, find(markup, "Next"))
        text, markup = api.edits[-1][2], api.edits[-1][3]
        assert "Person 2" in text and "2/3" in text
        await tap(bot, find(markup, "Pin"))
        assert api.answers[-1][1].startswith("Pinned")
        assert [p.name for p in await WorkspaceStore(db).pins("person")] == ["Person 2"]
        await tap(bot, find(markup, "Save search"))
        assert [s.query for s in await WorkspaceStore(db).saved()] == ["machine learning"]
        await tap(bot, find(markup, "Prev"))
        assert "Person 1" in api.edits[-1][2]

    ui_env(tmp_path, scenario)


def test_firm_search_pins_firms_and_pinned_screen_manages_notes_and_unpin(tmp_path):
    async def scenario(db, api, bot, conn):
        await tap(bot, "m:search")
        await tap(bot, find(api.edits[-1][3], "Firms"))
        await say(bot, "security")
        await tap(bot, find(api.edits[-1][3], "Pin"))
        await tap(bot, "m:pins")
        markup = api.edits[-1][3]
        assert "1 firm" in api.edits[-1][2]
        await tap(bot, find(markup, "Acme Rockets"))
        detail_markup = api.edits[-1][3]
        await tap(bot, find(detail_markup, "note"))
        await say(bot, "great security team")
        pins = await WorkspaceStore(db).pins("firm")
        assert pins[0].note == "great security team"
        assert "great security team" in api.edits[-1][2]
        await tap(bot, find(api.edits[-1][3], "Unpin"))
        assert await WorkspaceStore(db).pins("firm") == ()

    ui_env(tmp_path, scenario)


def test_find_job_board_links_the_firm_and_schedules_collection(tmp_path):
    async def finder(name):
        assert name == "Acme Rockets"
        return ("greenhouse", "acmerockets")

    async def scenario(db, api, bot, conn):
        await WorkspaceStore(db).pin(
            "firm", "https://www.linkedin.com/company/acme", "Acme Rockets"
        )
        await tap(bot, "m:pins")
        await tap(bot, find(api.edits[-1][3], "Acme Rockets"))
        await tap(bot, find(api.edits[-1][3], "job board"))
        [pin] = await WorkspaceStore(db).pins("firm")
        assert (pin.board_provider, pin.board_slug) == ("greenhouse", "acmerockets")
        work = (await db.status())["work"]
        assert [w["workflow"] for w in work] == ["ats:greenhouse:acmerockets"]
        assert "acmerockets" in api.edits[-1][2]

    ui_env(tmp_path, scenario, board_finder=finder)


def test_saved_searches_rerun_and_show_only_new_results(tmp_path):
    async def scenario(db, api, bot, conn):
        store = WorkspaceStore(db)
        saved = await store.save_search("people", "ml", "ML people")
        await store.record_run(saved, ["https://www.linkedin.com/in/p1"])
        await tap(bot, "m:saved")
        await tap(bot, find(api.edits[-1][3], "ML people"))
        text = api.edits[-1][2]
        assert "2 new" in text and "Person 2" in text and "Person 1" not in text
        await tap(bot, find(api.edits[-1][3], "Delete"))
        assert await store.saved() == ()

    ui_env(tmp_path, scenario)


def test_settings_list_fields_can_be_added_and_removed(tmp_path):
    async def scenario(db, api, bot, conn):
        await tap(bot, "m:set")
        await tap(bot, find(api.edits[-1][3], "Locations"))
        await tap(bot, find(api.edits[-1][3], "Add location"))
        await say(bot, "Berlin, Paris\nberlin")
        prefs = await db.preferences()
        assert prefs.locations == ("Berlin", "Paris")  # duplicates ignored case-insensitively
        await tap(bot, find(api.edits[-1][3], "Berlin"))  # shown as a remove button
        assert (await db.preferences()).locations == ("Paris",)

    ui_env(tmp_path, scenario)


def test_settings_toggles_priorities_age_languages_and_digest(tmp_path):
    async def scenario(db, api, bot, conn):
        await tap(bot, "set:remote")
        assert (await db.preferences()).remote is True
        await tap(bot, "set:remote")
        assert (await db.preferences()).remote is False
        await tap(bot, "set:remote")
        assert (await db.preferences()).remote is None  # off / on / only: cycles back
        await tap(bot, "set:intern")
        assert (await db.preferences()).internship is True
        await tap(bot, "set:prio:up:location")
        assert (await db.preferences()).priorities[:2] == ("location", "role")
        await tap(bot, "set:prio:rm:language")
        assert "language" not in (await db.preferences()).priorities
        await tap(bot, "set:prio:add:language")
        assert (await db.preferences()).priorities[-1] == "language"
        await tap(bot, "set:age:14")
        assert (await db.preferences()).max_age_days == 14
        await tap(bot, "set:age:any")
        assert (await db.preferences()).max_age_days is None
        await tap(bot, "set:lang:fr")
        await tap(bot, "set:lang:en")
        await tap(bot, "set:lang:fr")
        assert (await db.preferences()).languages == ("en",)
        await tap(bot, "dg:toggle")
        await tap(bot, "dg:time:+")
        digest_settings = await WorkspaceStore(db).digest()
        assert digest_settings.enabled and digest_settings.local_time == "18:30"
        await tap(bot, "dg:alerts_boards")
        assert (await WorkspaceStore(db).digest()).alerts_boards is True

    ui_env(tmp_path, scenario)


def test_interests_free_text_is_saved_and_the_llm_note_is_honest(tmp_path):
    async def scenario(db, api, bot, conn):
        await tap(bot, "set:interests")
        assert "not enabled" in api.edits[-1][2].lower()
        await tap(bot, find(api.edits[-1][3], "Edit"))
        await say(bot, "cybersecurity, LLMs, data analysis, finance, defense internships")
        assert "cybersecurity" in (await db.preferences()).interests

    ui_env(tmp_path, scenario)


def test_jobs_screen_ranks_collected_postings_with_settings_and_pins(tmp_path):
    async def scenario(db, api, bot, conn):
        await db.update_preferences(
            lambda p: p.model_copy(
                update={"roles": ("security engineer",), "locations": ("Berlin",)}
            )
        )
        await db.schedule("w", "{}", now(), 3600)
        work = await db.claim(now(), "w")
        items = tuple(
            SourceObservation(
                source="greenhouse",
                external_id=str(i),
                url=f"https://boards.greenhouse.io/{slug}/jobs/{i}",
                title="Security Engineer",
                location="Berlin",
                company=slug,
                content_hash=digest(f"{i}".encode()),
            )
            for i, slug in enumerate(("alpha", "beta"), start=1)
        )
        await db.persist_page(work, CollectionPage(items=items, coverage="complete"), now())
        await WorkspaceStore(db).pin("firm", "https://www.linkedin.com/company/beta", "Beta")
        await WorkspaceStore(db).link_board(
            "https://www.linkedin.com/company/beta", "greenhouse", "beta"
        )
        await tap(bot, "m:jobs")
        text = api.edits[-1][2]
        assert "beta" in text.lower().split("1/2")[0] or "pinned firm" in text  # pinned firm first
        assert "pinned firm" in text

    ui_env(tmp_path, scenario)


def test_status_screen_summarises_without_touching_linkedin(tmp_path):
    async def scenario(db, api, bot, conn):
        await tap(bot, "m:status")
        text = api.edits[-1][2]
        assert "draft_only" in text and "Pinned" in text and conn.calls == []

    ui_env(tmp_path, scenario)


def test_free_text_still_reaches_onboarding_when_no_input_is_pending(tmp_path):
    async def scenario(db, api, bot, conn):
        await say(bot, "ML engineer internship")
        assert "location" in api.sent[-1][1].lower()

    ui_env(tmp_path, scenario)


def test_menu_command_cancels_a_pending_input(tmp_path):
    async def scenario(db, api, bot, conn):
        await tap(bot, "m:search")
        await tap(bot, find(api.edits[-1][3], "People"))
        assert await WorkspaceStore(db).awaiting() is not None
        await say(bot, "/menu")
        assert await WorkspaceStore(db).awaiting() is None

    ui_env(tmp_path, scenario)


def test_jobs_search_input_accepts_a_location_ranks_by_settings_and_stores_public_jobs(tmp_path):
    async def scenario(db, api, bot, conn):
        await db.update_preferences(
            lambda p: p.model_copy(
                update={
                    "roles": ("security engineer",),
                    "locations": ("Berlin",),
                    "internship": True,
                }
            )
        )
        await tap(bot, "m:search")
        await tap(bot, find(api.edits[-1][3], "Jobs"))
        await say(bot, "security engineer in Berlin")
        assert ("jobs", "security engineer", "Berlin") in conn.calls
        text, markup = api.edits[-1][2], api.edits[-1][3]
        assert "Security Engineer Intern" in text and "internship" in text  # best match first
        assert "1/3" in text
        await tap(bot, find(markup, "Next"))
        assert "Security Engineer" in api.edits[-1][2]
        await tap(bot, find(api.edits[-1][3], "Next"))
        assert "outside your settings" in api.edits[-1][2] and "Gardener" in api.edits[-1][2]
        stored = await db.opportunities(since=None)
        assert {o.external_id for o in stored} == {"1", "2", "3"}
        assert {o.source for o in stored} == {"linkedin"}
        assert next(o for o in stored if o.external_id == "2").company == "Beta Security"

    ui_env(tmp_path, scenario)


def test_saved_job_searches_report_new_postings_only(tmp_path):
    async def scenario(db, api, bot, conn):
        store = WorkspaceStore(db)
        saved = await store.save_search("jobs", "security engineer in Berlin", "Berlin jobs")
        await store.record_run(saved, ["https://www.linkedin.com/jobs/view/1"])
        await tap(bot, "m:saved")
        await tap(bot, find(api.edits[-1][3], "Berlin jobs"))
        assert "2 new" in api.edits[-1][2] and ("jobs", "security engineer", "Berlin") in conn.calls

    ui_env(tmp_path, scenario)
