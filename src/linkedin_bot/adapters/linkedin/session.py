"""Persistent browser profile owned by this app. Login is done by the user in a visible window."""

import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import Page, async_playwright

from linkedin_bot.adapters.linkedin.connector import LinkedInConnector
from linkedin_bot.adapters.linkedin.limits import Limiter
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
