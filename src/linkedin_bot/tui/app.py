"""The cockpit: one process that owns the database, the browser and the LinkedIn budget.

It replaces `serve`: board collection, the digest, alerts, daily upkeep and (when a token is
set) Telegram polling all run inside it. Alerts appear as notifications here and in Telegram.
"""

import asyncio
import os
import signal
from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import ClassVar

from textual import on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from linkedin_bot.adapters.telegram import TOKEN_ENV
from linkedin_bot.domain.models import now
from linkedin_bot.runtime.config import Config
from linkedin_bot.runtime.services import Services
from linkedin_bot.storage.database import Database
from linkedin_bot.storage.linkedin import LinkedInStore
from linkedin_bot.tui.logbridge import EventBuffer, redirected
from linkedin_bot.tui.views.approvals import Approvals
from linkedin_bot.tui.views.base import View
from linkedin_bot.tui.views.boards import Boards
from linkedin_bot.tui.views.dashboard import Dashboard
from linkedin_bot.tui.views.drafts import Drafts
from linkedin_bot.tui.views.jobs import Jobs
from linkedin_bot.tui.views.logs import Logs
from linkedin_bot.tui.views.messages import Messages
from linkedin_bot.tui.views.pins import Pins
from linkedin_bot.tui.views.profile import Profile
from linkedin_bot.tui.views.saved import Saved
from linkedin_bot.tui.views.search import Search
from linkedin_bot.tui.views.settings import Settings
from linkedin_bot.tui.views.system import System
from linkedin_bot.tui.widgets import ConfirmModal, HelpModal, TextModal, clip

TABS: tuple[tuple[str, str, type[View], str], ...] = (
    ("dashboard", "1 Dashboard", Dashboard, "1"),
    ("jobs", "2 Jobs", Jobs, "2"),
    ("search", "3 Search", Search, "3"),
    ("messages", "4 Messages", Messages, "4"),
    ("pins", "5 Pinned", Pins, "5"),
    ("saved", "6 Saved", Saved, "6"),
    ("boards", "7 Boards", Boards, "7"),
    ("settings", "8 Settings", Settings, "8"),
    ("approvals", "9 Approvals", Approvals, "9"),
    ("profile", "0 Profile", Profile, "0"),
    ("drafts", "F2 Drafts", Drafts, "f2"),
    ("logs", "F3 Logs", Logs, "f3"),
    ("system", "F4 System", System, "f4"),
)
HELP = """Views
  1 Dashboard   state at a glance, pause/resume, digest preview
  2 Jobs        ranked postings and your tracker
  3 Search      LinkedIn people, firms and jobs (read only)
  4 Messages    LinkedIn inbox (read only, never stored)
  5 Pinned      firms and people, notes, tags, job boards
  6 Saved       saved searches; rerun to see only new results
  7 Boards      public job board collection queue
  8 Settings    ranking rules, priorities, interests, digest and alerts
  9 Approvals   policy, approvals and unknown outcomes
  0 Profile     onboarding, facts, CV, Telegram pairing
  F2 Drafts     cover letters and CVs
  F3 Logs       live event names (never content)
  F4 System     health report, backups, LinkedIn sign in

Everywhere
  ?  or F1      this help
  Ctrl+P        command palette (every action by name)
  Ctrl+R        refresh the current view
  P             pause or resume all background work
  q / Ctrl+Q    quit (asks first while a read or draft is running)

Jobs (2)
  /  filter   o open   c copy URL   i interested   a applied   v interviewing
  x  dismiss  u unmark  n note      p pin the firm  w draft a cover letter
  Click a column header to sort.

Search (3)
  /  query    o open   c copy URL   p pin   s save search

Pinned (5)   o open  n note  t tags  b find job board  g collect now  Delete unpin
Saved (6)    g run (new only)  e rename  Delete remove  o open a new result
Boards (7)   g run now  i interval  Delete stop collecting

Nothing in this program sends applications or messages. LinkedIn is only read, inside a
budget of 30 pages an hour and 150 a day, and it stops at any sign-in or verification page.
"""


class TuiNotifier:
    """Delivers digests and alerts to the cockpit as notifications and the Dashboard feed."""

    def __init__(self, app: CockpitApp) -> None:
        self.app = app

    async def ready(self) -> bool:
        return True

    async def send(self, text: str, kind: str) -> None:
        self.app.alerts.append((datetime.now().astimezone(), kind, text))
        if kind == "digest":
            self.app.latest_digest = text
            self.app.notify("Your daily digest is ready (Dashboard).", title="Digest", markup=False)
        else:
            self.app.notify(
                clip(text, 300),
                title="Alert",
                severity="warning" if kind == "auth_notice" else "information",
                markup=False,
            )


class CockpitApp(App[None]):
    TITLE = "LinkedIn Bot"
    SUB_TITLE = "cockpit"
    CSS_PATH = "styles.tcss"
    BINDINGS: ClassVar[list[BindingType]] = [
        *(
            Binding(key, f"show('{tab}')", label.split(" ", 1)[1], show=False)
            for tab, label, _, key in TABS
        ),
        Binding("question_mark", "help", "Help"),
        Binding("f1", "help", "Help", show=False),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("P", "toggle_pause", "Pause/resume"),
        Binding("q", "quit_app", "Quit"),
        Binding("escape", "leave_input", "Leave field", show=False),
    ]

    def __init__(
        self,
        services: Services,
        *,
        events: EventBuffer | None = None,
        background: bool = True,
        poll_telegram: bool = True,
        refresh_seconds: float = 10.0,
    ) -> None:
        super().__init__()
        self.services = services
        self.events = events or EventBuffer()
        self.background = background
        self.poll_telegram = poll_telegram
        self.refresh_seconds = refresh_seconds
        self.stop = asyncio.Event()
        self.alerts: deque[tuple[datetime, str, str]] = deque(maxlen=200)
        self.latest_digest: str | None = None
        self.running: dict[str, int] = {}

    # ---- layout ----------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="statusbar", markup=False)
        with TabbedContent(initial="dashboard", id="tabs"):
            for tab, label, view, _ in TABS:
                with TabPane(label, id=tab):
                    yield view(id=f"view-{tab}")
        yield Footer()

    async def on_mount(self) -> None:
        self.services.scheduler.notifiers.append(TuiNotifier(self))
        if self.background:
            self.services.start_background(self.stop, poll_telegram=self.poll_telegram)
        self.set_interval(5.0, self.update_statusbar)
        self.set_interval(self.refresh_seconds, self.refresh_active)
        await self.update_statusbar()
        await self.reload_active()

    async def on_unmount(self) -> None:
        self.stop.set()

    def now(self) -> datetime:
        return now()

    # ---- refresh ---------------------------------------------------------------------
    def active_view(self) -> View | None:
        tab = self.query_one("#tabs", TabbedContent).active
        try:
            return self.query_one(f"#view-{tab}", View)
        except Exception:
            return None

    async def reload_active(self) -> None:
        view = self.active_view()
        if view is not None:
            try:
                await view.reload()
            except Exception:
                self.log.error("view reload failed")
                self.notify("Could not refresh this view.", severity="error")

    async def refresh_active(self) -> None:
        """Periodic refresh, so changes made from Telegram or by background work show up."""
        if isinstance(self.screen, ModalScreen):
            return  # never redraw under an open dialog
        view = self.active_view()
        if view is not None and view.AUTO_REFRESH:
            await self.reload_active()

    @on(TabbedContent.TabActivated, "#tabs")
    async def tab_changed(self) -> None:
        await self.reload_active()

    async def update_statusbar(self) -> None:
        services = self.services
        policy = await services.db.policy()
        state, _ = await LinkedInStore(services.db).state()
        usage = services.limiter.usage()
        telegram = "off" if services.api is None else ("on" if self.poll_telegram else "idle")
        busy = ", ".join(sorted(self.running)) or "idle"
        self.query_one("#statusbar", Static).update(
            f"{policy.mode} · {'PAUSED' if policy.paused else 'running'} · LinkedIn {state} "
            f"({usage['hour']}/{usage['hourly_limit']} h, {usage['day']}/{usage['daily_limit']} d) "
            f"· Telegram {telegram} · model {services.config.inference.model or 'off'} · {busy}"
        )
        self.sub_title = "paused" if policy.paused else "running"

    def drain_alerts(self) -> list[tuple[datetime, str, str]]:
        items = list(self.alerts)
        self.alerts.clear()
        return items

    @contextmanager
    def busy(self, name: str) -> Iterator[None]:
        """Marks a LinkedIn read, draft or similar as running (shown and checked on quit)."""
        self.running[name] = self.running.get(name, 0) + 1
        try:
            yield
        finally:
            self.running[name] -= 1
            if not self.running[name]:
                del self.running[name]

    # ---- actions ---------------------------------------------------------------------
    def action_show(self, tab: str) -> None:
        # Drop focus first: a focused input in the old pane would otherwise pull focus (and
        # the active tab) straight back when that pane is hidden.
        self.screen.set_focus(None)
        self.query_one("#tabs", TabbedContent).active = tab
        self.call_after_refresh(self.focus_view)

    def focus_view(self) -> None:
        """Focus the view's main table, so its keys work at once."""
        view = self.active_view()
        if view is None:
            return
        if view.FOCUS is not None:
            view.query_one(view.FOCUS).focus()
            return
        tables = [table for table in view.query(DataTable) if table.focusable]
        if tables:
            tables[0].focus()

    def action_leave_input(self) -> None:
        if isinstance(self.focused, Input | TextArea):
            self.focus_view()

    async def action_refresh(self) -> None:
        await self.reload_active()
        await self.update_statusbar()

    def action_help(self) -> None:
        self.push_screen(HelpModal(HELP))

    async def action_toggle_pause(self) -> None:
        paused = (await self.services.db.policy()).paused
        await self.services.db.pause(not paused)
        self.notify(
            "Resumed. Still draft only."
            if paused
            else "Paused: no board reads, alerts or LinkedIn visits."
        )
        await self.update_statusbar()
        await self.reload_active()

    @work(exclusive=True, group="collect")
    async def action_collect(self) -> None:
        count = 0
        with self.busy("boards"):
            while count < 20 and await self.services.collect_once():
                count += 1
        self.notify(f"Read {count} due board(s)." if count else "No board is due (or paused).")
        await self.reload_active()

    @work(exclusive=True, group="check")
    async def action_check_linkedin(self) -> None:
        with self.busy("linkedin"):
            state, error = await self.services.linkedin.check()
        if error is not None:
            self.notify(error, severity="error", markup=False)
        else:
            self.notify(f"LinkedIn session: {state}", markup=False)
        await self.update_statusbar()

    async def action_release_browser(self) -> None:
        await self.services.release_browser()
        self.notify("Browser released.")

    def action_show_digest(self) -> None:
        if self.latest_digest is None:
            self.notify("No digest has been delivered in this session yet.")
        else:
            self.push_screen(TextModal("Latest digest", self.latest_digest))

    @work(exclusive=True, group="quit")
    async def action_quit_app(self) -> None:
        if self.running and not await self.push_screen_wait(
            ConfirmModal(
                f"Still running: {', '.join(sorted(self.running))}. Quit anyway?",
                confirm_label="Quit",
                danger=True,
            )
        ):
            return
        self.stop.set()
        self.exit()

    def get_system_commands(self, screen: Screen[object]) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        for tab, label, _, _key in TABS:
            yield SystemCommand(
                f"Go to {label.split(' ', 1)[1]}",
                f"Open the {label.split(' ', 1)[1]} view",
                lambda tab=tab: self.action_show(tab),
            )
        yield SystemCommand(
            "Pause or resume", "Stop or restart all background work", self.action_toggle_pause
        )
        yield SystemCommand(
            "Collect due boards", "Read every public board that is due now", self.action_collect
        )
        yield SystemCommand(
            "Check LinkedIn session",
            "Load the feed once and record the state",
            self.action_check_linkedin,
        )
        yield SystemCommand(
            "Release browser", "Close the warm headless browser", self.action_release_browser
        )
        yield SystemCommand(
            "Show latest digest", "The last digest delivered here", self.action_show_digest
        )
        yield SystemCommand("Help", "Every key and view", self.action_help)


async def run_tui(
    database: Database, *, config: Config, telegram: bool = True, background: bool = True
) -> None:
    """Build the services, take over logging and run until the user quits."""
    use_telegram = telegram and bool(os.environ.get(TOKEN_ENV))
    services = Services.from_config(config, database, telegram=use_telegram)
    events = EventBuffer()
    app = CockpitApp(services, events=events, background=background, poll_telegram=use_telegram)
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, app.exit)
    try:
        with redirected(config.root, events):
            await app.run_async()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        app.stop.set()
        await services.aclose()
