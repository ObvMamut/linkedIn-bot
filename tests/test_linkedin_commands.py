# ruff: noqa: E501  (long fixture lines)
import asyncio
import logging
from contextlib import asynccontextmanager

from linkedin_bot.adapters.linkedin.models import (
    Company,
    Conversation,
    LinkedInError,
    Person,
    Profile,
)
from linkedin_bot.adapters.linkedin.pages import Parsed
from linkedin_bot.domain.models import now
from linkedin_bot.storage.database import Database
from linkedin_bot.storage.linkedin import LinkedInStore
from linkedin_bot.storage.restore import restore_backup
from linkedin_bot.storage.telegram import TelegramStore
from linkedin_bot.workflows.linkedin_commands import LinkedInCommands
from linkedin_bot.workflows.telegram_bot import Bot
from tests.test_telegram import FakeAPI, update

SECRET = "Are you free Tuesday for the confidential offer?"


class FakeConnector:
    def __init__(self, error=None, state="ready", conversations=None):
        self.error, self.state, self.calls, self.refreshes = error, state, [], []
        self.conversations = conversations or (
            Conversation(id="0", title="Jane Doe", snippet=SECRET, unread=True, when="Sep 30"),
            Conversation(id="2", title="Acme", snippet="Thanks", unread=False),
        )

    def _maybe_fail(self):
        if self.error:
            raise self.error

    async def status(self):
        self.calls.append("status")
        self._maybe_fail()
        return self.state

    async def messages(self, count, **kw):
        self.refreshes.append(kw.get("refresh", False))
        self.calls.append(("messages", count))
        self._maybe_fail()
        return Parsed(self.conversations[:count], 1)

    async def search_people(self, query, **kw):
        self.calls.append(("people", query))
        self._maybe_fail()
        return Parsed(
            (Person(url="https://www.linkedin.com/in/jane", name="Jane", headline="ML"),), 0
        )

    async def search_companies(self, query, **kw):
        self.calls.append(("firms", query))
        self._maybe_fail()
        return Parsed((Company(url="https://www.linkedin.com/company/acme", name="Acme"),), 0)

    async def profile(self, target, **kw):
        self.calls.append(("profile", target))
        self._maybe_fail()
        return Profile(
            url=target or "https://www.linkedin.com/in/me",
            name="Jane Doe",
            headline="ML Engineer",
            about="I build models.",
            experience=("Acme, 2022",),
        )


def setup(tmp_path, connector, configured=True):
    async def main(scenario):
        db = Database(tmp_path)
        try:
            await db.open()

            @asynccontextmanager
            async def opener():
                yield connector

            commands = LinkedInCommands(opener, LinkedInStore(db)) if configured else None
            api = FakeAPI()
            bot = Bot(db, api, linkedin=commands)
            store = TelegramStore(db)
            await bot.handle(update(1, f"/start {await store.begin_pairing(now())}"))
            return await scenario(db, api, bot)
        finally:
            await db.close()

    return main


def ask(tmp_path, connector, *texts, configured=True):
    async def scenario(db, api, bot):
        for number, text in enumerate(texts, start=10):
            await bot.handle(update(number, text))
        return api, db

    return asyncio.run(setup(tmp_path, connector, configured)(scenario))


def last(api):
    return api.sent[-1][1]


def test_status_reports_state_and_when_the_layout_was_last_verified(tmp_path):
    connector = FakeConnector()
    api, db = ask(tmp_path, connector, "/li_status")
    assert "ready" in last(api) and "verified against live pages on 2026-10-03" in last(api)
    state = asyncio.run(_state(tmp_path))
    assert state[0] == "ready" and state[1] is not None


async def _state(tmp_path):
    db = Database(tmp_path)
    try:
        await db.open()
        return await db.call(
            lambda c: c.execute("SELECT state,last_success FROM linkedin_state").fetchone()
        )
    finally:
        await db.close()


def test_messages_are_listed_bounded_and_clamped(tmp_path):
    connector = FakeConnector()
    api, _ = ask(tmp_path, connector, "/li_messages 99")
    assert ("messages", 10) in connector.calls
    reply = last(api)
    assert "Jane Doe" in reply and "unread" in reply and "1 row skipped" in reply


def test_search_people_and_firms_and_usage(tmp_path):
    connector = FakeConnector()
    api, _ = ask(
        tmp_path,
        connector,
        "/li_search people ml engineer",
        "/li_search firms acme",
        "/li_search cats x",
    )
    assert ("people", "ml engineer") in connector.calls and ("firms", "acme") in connector.calls
    assert "Usage" in last(api)
    assert any("linkedin.com/in/jane" in text for _, text in api.sent)


def test_profile_own_and_by_url(tmp_path):
    connector = FakeConnector()
    api, _ = ask(
        tmp_path, connector, "/li_profile", "/li_profile https://www.linkedin.com/in/jane-doe"
    )
    assert [c for c in connector.calls if c[0] == "profile"] == [
        ("profile", None),
        ("profile", "https://www.linkedin.com/in/jane-doe"),
    ]
    assert "ML Engineer" in last(api) and "I build models." in last(api)


def test_errors_become_short_actionable_messages(tmp_path):
    cases = {
        "auth_required": "linkedin-bot linkedin login",
        "challenge_required": "6 hours",
        "layout_changed": "layout",
        "invalid_target": "profile or company",
        "unavailable": "browser",
    }
    for code, expected in cases.items():
        api, _ = ask(tmp_path / code, FakeConnector(error=LinkedInError(code)), "/li_messages")
        assert expected in last(api), code
    api, _ = ask(
        tmp_path / "rl", FakeConnector(error=LinkedInError("rate_limited", 1800)), "/li_messages"
    )
    assert "30 minutes" in last(api)
    api, _ = ask(
        tmp_path / "cd", FakeConnector(error=LinkedInError("cooldown", 7200)), "/li_messages"
    )
    assert "2 hours" in last(api)


def test_not_configured_and_strangers(tmp_path):
    api, _ = ask(tmp_path / "a", FakeConnector(), "/li_status", configured=False)
    assert "not enabled" in last(api)

    async def scenario(db, api, bot):
        sent = len(api.sent)
        await bot.handle(update(20, "/li_messages", user=999))
        assert len(api.sent) == sent

    asyncio.run(setup(tmp_path / "b", FakeConnector())(scenario))


def test_message_text_never_reaches_logs_and_replies_fit_telegram(tmp_path, caplog):
    long = tuple(
        Conversation(id=str(i), title="T" * 80, snippet="S" * 600, unread=False) for i in range(10)
    )
    with caplog.at_level(logging.DEBUG):
        api, _ = ask(
            tmp_path,
            FakeConnector(conversations=long + (Conversation(id="x", title="a", snippet=SECRET),)),
            "/li_messages 10",
        )
    assert SECRET not in caplog.text and len(last(api)) <= 4000


def test_restore_clears_linkedin_state(tmp_path):
    async def make():
        db = Database(tmp_path / "src")
        try:
            await db.open()
            await LinkedInStore(db).record("ready", now())
            return await db.backup()
        finally:
            await db.close()

    snapshot = asyncio.run(make())
    restore_backup(snapshot, tmp_path / "restored")
    state = asyncio.run(_state(tmp_path / "restored"))
    assert state == ("unknown", None)


def test_release_command_closes_the_browser_and_is_explained_without_one(tmp_path):
    released = []

    async def release():
        released.append(True)

    async def main(scenario):
        db = Database(tmp_path)
        try:
            await db.open()

            @asynccontextmanager
            async def opener():
                yield FakeConnector()

            api = FakeAPI()
            commands = LinkedInCommands(opener, LinkedInStore(db), release=release)
            bot = Bot(db, api, linkedin=commands)
            await bot.handle(update(1, f"/start {await TelegramStore(db).begin_pairing(now())}"))
            await bot.handle(update(2, "/li_release"))
            return api
        finally:
            await db.close()

    api = asyncio.run(main(None))
    assert released == [True] and "released" in last(api).lower()


def test_status_records_the_state_the_page_showed_not_just_that_it_answered(tmp_path):
    connector = FakeConnector(state="auth_required")
    api, db = ask(tmp_path, connector, "/li_status")
    assert "auth_required" in last(api) and "linkedin login" in last(api)
    state = asyncio.run(_state(tmp_path))
    assert state == ("auth_required", None)
