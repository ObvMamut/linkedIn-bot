"""System: health report, backups, upkeep, and the LinkedIn browser session."""

import asyncio
import json

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Static

from linkedin_bot.adapters.linkedin.session import (
    manual_login_command,
    profile_in_use,
    system_chromium,
)
from linkedin_bot.runtime.doctor import doctor
from linkedin_bot.tui.views.base import View
from linkedin_bot.tui.widgets import ConfirmModal


class System(View):
    AUTO_REFRESH = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Button("Refresh report", id="report")
            yield Button("Back up now", id="backup")
            yield Button("Run daily upkeep now", id="upkeep")
        with Horizontal(classes="toolbar"):
            yield Button("Check LinkedIn session", id="check")
            yield Button("Release browser", id="release")
            yield Button("Sign in to LinkedIn…", variant="warning", id="login")
        with VerticalScroll():
            yield Static("", id="doctor", markup=False, classes="document")

    async def reload(self) -> None:
        db = self.services.db
        report = await doctor(db, self.services.config, {}, await db.inspect_artifacts())
        report.pop("work", None)  # the Boards view shows the queue
        self.query_one("#doctor", Static).update(json.dumps(report, indent=2, default=str))

    @on(Button.Pressed, "#report")
    async def refresh_report(self) -> None:
        await self.reload()

    @on(Button.Pressed, "#backup")
    async def backup(self) -> None:
        path = await self.services.db.backup()
        self.report(f"Snapshot written to {path}.")

    @on(Button.Pressed, "#upkeep")
    def upkeep_pressed(self) -> None:
        self.upkeep()

    @work(exclusive=True, group="upkeep")
    async def upkeep(self) -> None:
        result = await self.services.maintenance.run()
        self.report(
            f"Upkeep done: {result['old_versions']} old versions and {result['old_postings']} "
            f"old postings removed, {result['backups_removed']} old snapshots rotated."
        )
        await self.reload()

    @on(Button.Pressed, "#check")
    def check(self) -> None:
        self.cockpit.action_check_linkedin()

    @on(Button.Pressed, "#release")
    async def release(self) -> None:
        await self.services.release_browser()
        self.report("Browser released.")

    @on(Button.Pressed, "#login")
    def login_pressed(self) -> None:
        self.login()

    @work(exclusive=True, group="login")
    async def login(self) -> None:
        browser = system_chromium(self.services.config.linkedin.browser_executable)
        if browser is None:
            self.report("Chromium not found; set linkedin.browser_executable.", error=True)
            return
        if not await self.app.push_screen_wait(
            ConfirmModal(
                "A normal Chromium window opens on the bot's own profile. Sign in to LinkedIn "
                "yourself, then close that window. Nothing is automated while it is open.",
                confirm_label="Open window",
            )
        ):
            return
        await self.services.release_browser()
        if profile_in_use(self.services.config.root):
            self.report("Another browser is using the profile; close it first.", error=True)
            return
        process = await asyncio.create_subprocess_exec(
            *manual_login_command(browser, self.services.config.root),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.report("Waiting for the sign-in window to close…")
        await process.wait()
        self.cockpit.action_check_linkedin()
