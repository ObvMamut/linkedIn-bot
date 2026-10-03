# ruff: noqa: E501  (URL literals)
import asyncio
import logging
import os
import re
from pathlib import Path

import pytest

import linkedin_bot.adapters.linkedin as package
from linkedin_bot.adapters.linkedin.cache import TTLCache
from linkedin_bot.adapters.linkedin.connector import LinkedInConnector
from linkedin_bot.adapters.linkedin.limits import Limiter
from linkedin_bot.adapters.linkedin.models import LinkedInError
from linkedin_bot.adapters.linkedin.session import (
    WarmSession,
    profile_in_use,
    restrict_resources,
    should_block,
)
from tests.test_linkedin_connector import FakePage, no_sleep, run
from tests.test_linkedin_pages import MESSAGES, PEOPLE, PROFILE


class Time:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t


MESSAGE_PAGES = {
    "https://www.linkedin.com/messaging": ("https://www.linkedin.com/messaging/", MESSAGES),
    "https://www.linkedin.com/search/results/people": (
        "https://www.linkedin.com/search/results/people/",
        PEOPLE,
    ),
    "https://www.linkedin.com/in/jane-doe": ("https://www.linkedin.com/in/jane-doe", PROFILE),
}


def cached_connector(clock):
    page = FakePage(MESSAGE_PAGES)
    limiter = Limiter(sleep=no_sleep, delay=lambda: 0.0)
    return LinkedInConnector(page, limiter, TTLCache(clock=clock.now)), page


def test_ttl_cache_expires_and_distinguishes_keys():
    clock = Time()
    cache = TTLCache(clock=clock.now)
    cache.set(("a",), (1, 2), ttl=10)
    assert cache.get(("a",)) == (True, (1, 2)) and cache.get(("b",)) == (False, None)
    clock.t = 11
    assert cache.get(("a",)) == (False, None)
    cache.set(("empty",), (), ttl=10)
    assert cache.get(("empty",)) == (True, ())  # an empty result is still a hit


def test_repeat_reads_are_served_from_cache_until_ttl_or_refresh():
    clock = Time()
    conn, page = cached_connector(clock)
    first = run(conn.messages(5))
    assert run(conn.messages(1)).items == first.items[:1]  # sliced from the cached full list
    assert len(page.visited) == 1
    clock.t = 61  # messages expire after 60 s
    run(conn.messages(5))
    assert len(page.visited) == 2
    run(conn.messages(5, refresh=True))
    assert len(page.visited) == 3
    run(conn.search_people("Data Scientist"))
    run(conn.search_people("data scientist"))  # same query, different case
    assert len(page.visited) == 4


def test_failures_are_never_cached():
    clock = Time()
    page = FakePage(
        {"https://www.linkedin.com/messaging": ("https://www.linkedin.com/login", "<form></form>")}
    )
    conn = LinkedInConnector(
        page, Limiter(sleep=no_sleep, delay=lambda: 0.0), TTLCache(clock=clock.now)
    )
    for _ in range(2):
        with pytest.raises(LinkedInError):
            run(conn.messages(5))
    assert len(page.visited) == 2


def test_each_load_logs_pacing_and_page_durations_without_content(caplog):
    clock = Time()
    conn, _ = cached_connector(clock)
    with caplog.at_level(logging.INFO):
        run(conn.messages(5))
    events = {r.event: r.duration_ms for r in caplog.records if hasattr(r, "event")}
    assert {"linkedin_pacing", "linkedin_page"} <= set(events)
    assert all(isinstance(v, int) and v >= 0 for v in events.values())
    assert "Jane Doe" not in caplog.text


@pytest.mark.parametrize(
    ("kind", "url", "blocked"),
    [
        ("image", "https://media.licdn.com/x.jpg", True),
        ("media", "https://media.licdn.com/v.mp4", True),
        ("font", "https://static.licdn.com/f.woff2", True),
        ("script", "https://px.ads.linkedin.com/collect", True),
        ("xhr", "https://www.googletagmanager.com/gtm.js", True),
        ("document", "https://www.linkedin.com/feed/", False),
        ("script", "https://static.licdn.com/app.js", False),
        ("xhr", "https://www.linkedin.com/voyager/api/x", False),
    ],
)
def test_resource_blocking_rules(kind, url, blocked):
    assert should_block(kind, url) is blocked


def test_route_handler_only_aborts_or_continues():
    class Request:
        def __init__(self, kind, url):
            self.resource_type, self.url = kind, url

    class Route:
        def __init__(self, kind, url):
            self.request, self.done = Request(kind, url), []

        async def abort(self):
            self.done.append("abort")

        async def continue_(self):
            self.done.append("continue")

    async def go():
        blocked, allowed = (
            Route("image", "https://x/a.png"),
            Route("document", "https://www.linkedin.com/"),
        )
        await restrict_resources(blocked)
        await restrict_resources(allowed)
        return blocked.done, allowed.done

    assert run(go()) == (["abort"], ["continue"])


def test_package_has_no_way_to_act_on_pages_and_route_is_confined_to_session():
    strict = re.compile(
        r"\.(click|dblclick|fill|type|press|check|select_option|set_input_files|"
        r"dispatch_event|evaluate|evaluate_handle|keyboard|mouse|route)\b|\bsend_keys\b"
    )
    for path in Path(package.__file__).parent.glob("*.py"):
        text = path.read_text()
        if path.name == "session.py":
            # The one allowed use: a request filter that can only abort or continue.
            routed = text.count("context.route(")
            assert routed >= 1
            assert routed == text.count('context.route("**/*", restrict_resources)')
            assert set(re.findall(r"\broute\.(\w+)\(", text)) <= {"abort", "continue_"}
            text = text.replace("context.route(", "")
        assert not strict.search(text), path.name


class Launch:
    def __init__(self):
        self.opened = self.closed = 0

    async def __call__(self):
        self.opened += 1
        owner = self

        class Launched:
            page = FakePage(MESSAGE_PAGES)

            async def close(self):
                owner.closed += 1

        return Launched()


def warm(launch, idle=60.0):
    return WarmSession(launch, Limiter(sleep=no_sleep, delay=lambda: 0.0), idle_seconds=idle)


def test_warm_session_opens_lazily_reuses_and_keeps_one_connector():
    launch = Launch()
    session = warm(launch)

    async def go():
        assert launch.opened == 0
        async with session.use() as first:
            pass
        async with session.use() as second:
            pass
        await session.close()
        await session.close()  # idempotent
        return first is second

    assert run(go()) is True
    assert (launch.opened, launch.closed) == (1, 1)


def test_warm_session_closes_when_idle_then_reopens_on_demand():
    launch = Launch()
    session = warm(launch, idle=0.05)

    async def go():
        async with session.use():
            pass
        await asyncio.sleep(0.2)
        assert launch.closed == 1
        async with session.use():
            pass
        await session.close()

    run(go())
    assert (launch.opened, launch.closed) == (2, 2)


def test_unexpected_errors_drop_the_browser_but_linkedin_errors_do_not():
    launch = Launch()
    session = warm(launch)

    async def go():
        with pytest.raises(LinkedInError):
            async with session.use():
                raise LinkedInError("auth_required")
        assert launch.closed == 0
        with pytest.raises(RuntimeError):
            async with session.use():
                raise RuntimeError("browser crashed")
        assert launch.closed == 1
        async with session.use():
            pass  # a fresh browser is opened
        await session.close()

    run(go())
    assert launch.opened == 2


def test_profile_in_use_detects_a_live_chromium_lock_but_not_a_stale_one(tmp_path):
    browser = tmp_path / "browser"
    browser.mkdir()
    assert profile_in_use(tmp_path) is False
    lock = browser / "SingletonLock"
    lock.symlink_to(f"host-{os.getpid()}")
    assert profile_in_use(tmp_path) is True
    lock.unlink()
    lock.symlink_to("host-999999999")
    assert profile_in_use(tmp_path) is False
