"""Persistent browser profile owned by this app. Login is done by the user in a visible window."""

import asyncio
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

from linkedin_bot.adapters.linkedin.connector import LinkedInConnector
from linkedin_bot.adapters.linkedin.connector import Page as ReadablePage
from linkedin_bot.adapters.linkedin.limits import Limiter
from linkedin_bot.adapters.linkedin.models import SessionState
from linkedin_bot.adapters.linkedin.pages import detect_state
from linkedin_bot.runtime.privacy import private_directory


def profile_dir(root: Path) -> Path:
    path = root / "browser"
    private_directory(path)
    return path


def system_chromium(configured: str | None) -> str | None:
    return configured or shutil.which("chromium") or shutil.which("chromium-browser")


@asynccontextmanager
async def open_page(
    root: Path, *, headless: bool, executable: str | None = None
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
            yield context.pages[0] if context.pages else await context.new_page()
        finally:
            await context.close()


@asynccontextmanager
async def open_connector(
    root: Path, limiter: Limiter, executable: str | None = None
) -> AsyncIterator[LinkedInConnector]:
    """Headless, per command: no browser stays running between Telegram requests."""
    async with open_page(root, headless=True, executable=executable) as page:
        yield LinkedInConnector(page, limiter)


async def wait_for_login(
    page: ReadablePage,
    *,
    give_up_after: float = 600.0,
    interval: float = 3.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> SessionState:
    """Watch, never act: the user signs in and handles any challenge in the visible window."""
    deadline = clock() + give_up_after
    state: SessionState = "unavailable"
    while True:
        try:
            html = await page.content()
            state = detect_state(page.url, html)
        except PlaywrightError:
            pass  # the page is mid-navigation; look again
        if state == "ready" or clock() >= deadline:
            return state
        await sleep(interval)
