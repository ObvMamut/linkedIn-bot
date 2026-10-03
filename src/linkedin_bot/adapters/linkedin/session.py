"""Persistent browser profile owned by this app. Login is done by the user in a plain window."""

import asyncio
import os
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Route, async_playwright

from linkedin_bot.adapters.linkedin.connector import LinkedInConnector
from linkedin_bot.adapters.linkedin.connector import Page as ReadablePage
from linkedin_bot.adapters.linkedin.limits import Limiter
from linkedin_bot.adapters.linkedin.models import LinkedInError
from linkedin_bot.runtime.privacy import private_directory

BLOCKED_TYPES = frozenset({"image", "media", "font"})
BLOCKED_HOSTS = (
    "doubleclick.net",
    "googletagmanager.com",
    "google-analytics.com",
    "px.ads.linkedin.com",
    "snap.licdn.com",
)


def profile_dir(root: Path) -> Path:
    path = root / "browser"
    private_directory(path)
    return path


def profile_in_use(root: Path) -> bool:
    """True while a live Chromium holds the profile (its SingletonLock names a running pid)."""
    lock = root / "browser" / "SingletonLock"
    try:
        target = os.readlink(lock)
        pid = int(target.rsplit("-", 1)[1])
        os.kill(pid, 0)
    except OSError, ValueError, IndexError:
        return False  # no lock, an unreadable one, or a stale one from a crashed browser
    return True


def system_chromium(configured: str | None) -> str | None:
    return configured or shutil.which("chromium") or shutil.which("chromium-browser")


def should_block(resource_type: str, url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return resource_type in BLOCKED_TYPES or any(
        host == blocked or host.endswith("." + blocked) for blocked in BLOCKED_HOSTS
    )


async def restrict_resources(route: Route) -> None:
    """Skip images, media, fonts and ad/analytics hosts. The only things this can do to a
    request are abort it or let it through unchanged."""
    request = route.request
    if should_block(request.resource_type, request.url):
        await route.abort()
    else:
        await route.continue_()


class ReadOnlyPage:
    """The only page object the connector sees: navigate, wait for render, read. Nothing else."""

    def __init__(self, page: Page, settle_seconds: float = 1.5, quick_seconds: float = 0.3):
        self._page = page
        self._settle = settle_seconds
        self._quick = quick_seconds

    @property
    def url(self) -> str:
        return self._page.url

    async def goto(self, url: str, wait_for: str | None = None) -> None:
        await self._page.goto(url, wait_until="domcontentloaded")
        # These pages are built in the browser after load: <main> marks the app shell and
        # `wait_for` names the content the caller expects. Pages that never show either
        # (login, challenge, empty results) are read as they are after the timeout.
        for selector in ("main", wait_for) if wait_for else ("main",):
            try:
                await self._page.wait_for_selector(selector, state="attached", timeout=12_000)
            except PlaywrightError:
                break
        if wait_for:
            await asyncio.sleep(self._quick)  # the expected element is there; let siblings land
            return
        # Nothing specific to wait for: wait until the page stops growing.
        previous = -1
        for _ in range(10):
            await asyncio.sleep(self._settle)
            size = len(await self._page.content())
            if size == previous:
                break
            previous = size

    async def content(self) -> str:
        return await self._page.content()


@asynccontextmanager
async def open_page(
    root: Path, *, headless: bool, executable: str | None = None, block_resources: bool = False
) -> AsyncIterator[Page]:
    """One persistent context; cookies stay in the profile directory and are never exported."""
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir(root)),
            headless=headless,
            executable_path=system_chromium(executable),
            args=["--no-first-run", "--no-default-browser-check"],
        )
        try:
            if block_resources:
                await context.route("**/*", restrict_resources)
            yield context.pages[0] if context.pages else await context.new_page()
        finally:
            await context.close()


@asynccontextmanager
async def open_connector(
    root: Path, limiter: Limiter, executable: str | None = None
) -> AsyncIterator[LinkedInConnector]:
    """One-shot, for the CLI: opens and closes a browser around a single use."""
    async with open_page(root, headless=True, executable=executable) as page:
        yield LinkedInConnector(ReadOnlyPage(page), limiter)


class Launched(Protocol):
    @property
    def page(self) -> ReadablePage: ...

    async def close(self) -> None: ...


Launcher = Callable[[], Awaitable[Launched]]


class PlaywrightLaunch:
    """A running headless browser kept open between commands."""

    def __init__(self, playwright: Any, context: Any, page: ReadOnlyPage) -> None:
        self._playwright, self._context, self.page = playwright, context, page

    async def close(self) -> None:
        try:
            await self._context.close()
        finally:
            await self._playwright.stop()


def playwright_launcher(
    root: Path, executable: str | None = None, block_resources: bool = True
) -> Launcher:
    async def launch() -> Launched:
        playwright = await async_playwright().start()
        try:
            context = await playwright.chromium.launch_persistent_context(
                str(profile_dir(root)),
                headless=True,
                executable_path=system_chromium(executable),
                args=["--no-first-run", "--no-default-browser-check"],
            )
            if block_resources:
                await context.route("**/*", restrict_resources)
            page = context.pages[0] if context.pages else await context.new_page()
        except BaseException:
            await playwright.stop()
            raise
        return PlaywrightLaunch(playwright, context, ReadOnlyPage(page))

    return launch


class WarmSession:
    """Opens the browser on first use, keeps one connector (and its cache) alive, and releases
    the profile after `idle_seconds` without use or on an unexpected browser error."""

    def __init__(self, launcher: Launcher, limiter: Limiter, idle_seconds: float = 600.0) -> None:
        self.launcher, self.limiter, self.idle_seconds = launcher, limiter, idle_seconds
        self.launched: Launched | None = None
        self.connector: LinkedInConnector | None = None
        self.lock = asyncio.Lock()
        self.timer: asyncio.TimerHandle | None = None
        self.closing: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def use(self) -> AsyncIterator[LinkedInConnector]:
        async with self.lock:
            self._cancel_timer()
            if self.launched is None:
                self.launched = await self.launcher()
                self.connector = LinkedInConnector(self.launched.page, self.limiter)
            assert self.connector is not None
            connector = self.connector
            try:
                yield connector
            except LinkedInError:
                raise  # an expected outcome (sign-in needed, rate limit); the browser is fine
            except BaseException:
                await self._close_locked()  # unknown state: start fresh next time
                raise
            finally:
                if self.launched is not None:
                    self._arm_timer()

    async def close(self) -> None:
        async with self.lock:
            self._cancel_timer()
            await self._close_locked()

    async def _close_locked(self) -> None:
        launched, self.launched, self.connector = self.launched, None, None
        if launched is not None:
            await launched.close()

    def _cancel_timer(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None

    def _arm_timer(self) -> None:
        self._cancel_timer()
        loop = asyncio.get_running_loop()
        self.timer = loop.call_later(self.idle_seconds, self._on_idle)

    def _on_idle(self) -> None:
        self.timer = None
        task = asyncio.ensure_future(self.close())
        self.closing.add(task)
        task.add_done_callback(self.closing.discard)


def manual_login_command(executable: str, root: Path) -> list[str]:
    """A normal browser with nothing attached. Sign-in providers refuse automated browsers.

    `--password-store=basic` matches what Playwright uses, so the reader can open the cookies.
    """
    return [
        executable,
        f"--user-data-dir={profile_dir(root)}",
        "--password-store=basic",
        "--no-first-run",
        "--no-default-browser-check",
        "https://www.linkedin.com/login",
    ]
