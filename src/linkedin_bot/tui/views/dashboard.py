"""Dashboard: the state of everything at a glance, plus the most used actions."""

from datetime import datetime, timedelta

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.widgets import Button, RichLog, Static

from linkedin_bot.domain.models import now
from linkedin_bot.storage.linkedin import LinkedInStore
from linkedin_bot.storage.telegram import TelegramStore
from linkedin_bot.tui.views.base import View
from linkedin_bot.tui.widgets import TextModal, plain
from linkedin_bot.workflows import approvals

CARDS = (
    ("postings", "Postings tracked"),
    ("new", "New in 24 h"),
    ("matches", "Matching your settings"),
    ("pinned", "Pinned"),
    ("saved", "Saved searches"),
    ("tracker", "Applied / interviewing"),
    ("approvals", "Waiting for approval"),
    ("unknown", "Unknown outcomes"),
)


def when(stamp: object) -> str:
    if not isinstance(stamp, str):
        return "—"
    try:
        moment = datetime.fromisoformat(stamp).astimezone()
    except ValueError:
        return "—"
    delta = moment - datetime.now().astimezone()
    minutes = int(delta.total_seconds() // 60)
    if minutes <= 0:
        return "due now"
    if minutes < 60:
        return f"in {minutes} min"
    if minutes < 48 * 60:
        return f"in {minutes // 60} h"
    return moment.strftime("%d %b %H:%M")


class Dashboard(View):
    def compose(self) -> ComposeResult:
        with Grid(id="cards"):
            for key, label in CARDS:
                with Vertical(classes="card"):
                    yield Static(label, classes="card-label")
                    yield Static("…", id=f"card-{key}", classes="card-value")
        with Horizontal(classes="actions"):
            yield Button("⏸ Pause", id="pause")
            yield Button("Collect due boards", id="collect")
            yield Button("Preview digest", id="digest")
            yield Button("Check LinkedIn", id="check")
        with Horizontal(id="dashboard-panels"):
            with Vertical(classes="panel"):
                yield Static("Status", classes="panel-title")
                yield Static("", id="health", markup=False)
            with Vertical(classes="panel"):
                yield Static("Alerts and digests", classes="panel-title")
                yield RichLog(id="alerts", wrap=True, markup=False, max_lines=300)

    async def reload(self) -> None:
        services = self.services
        db = services.db
        status = await db.status()
        policy = await db.policy()
        new = await db.opportunities(since=None, first_seen_after=now() - timedelta(days=1))
        counts = await db.status_counts()
        pins = await services.ops.store.pins()
        saved = await services.ops.store.saved()
        pending = await approvals.pending(db)
        values = {
            "postings": status["postings"],
            "new": len(new),
            "matches": await services.ops.matching_count(),
            "pinned": len(pins),
            "saved": len(saved),
            "tracker": counts.get("applied", 0) + counts.get("interviewing", 0),
            "approvals": len(pending),
            "unknown": status["unknown_actions"],
        }
        for key, value in values.items():
            self.query_one(f"#card-{key}", Static).update(plain(value))
        self.query_one("#pause", Button).label = "▶ Resume" if policy.paused else "⏸ Pause"

        state, last_ok = await LinkedInStore(db).state()
        usage = services.limiter.usage()
        digest = await services.ops.store.digest()
        binding = await TelegramStore(db).binding()
        work = await db.work_items()
        due = [w for w in work if w["state"] in ("pending", "blocked")]
        blocked = sum(1 for w in work if w["state"] == "blocked")
        cooldown = int(usage["cooldown_seconds"])
        lines = [
            f"Mode: {policy.mode} · {'PAUSED' if policy.paused else 'running'}"
            + (" · restore audit required" if policy.restore_audit_required else ""),
            f"LinkedIn: {state}" + (f" (last ok {last_ok[:16]})" if last_ok else ""),
            f"Page budget: {usage['hour']}/{usage['hourly_limit']} this hour, "
            f"{usage['day']}/{usage['daily_limit']} today"
            + (f" · cooldown {cooldown // 60} min" if cooldown else ""),
            "Telegram: "
            + (
                "not configured (no token)"
                if services.api is None
                else ("paired" if binding else "not paired")
            ),
            f"Local model: {services.config.inference.model or 'not configured'}",
            f"Digest: {'on at ' + digest.local_time if digest.enabled else 'off'} · "
            f"board alerts {'on' if digest.alerts_boards else 'off'} · "
            f"message alerts {'on' if digest.alerts_messages else 'off'}",
            f"Boards: {len(work)} scheduled, {blocked} blocked"
            + (f" · next {when(due[0]['due_at'])}" if due else ""),
            f"Events logged: {self.cockpit.events.serial} "
            f"({self.cockpit.events.warnings} warnings)",
        ]
        self.query_one("#health", Static).update("\n".join(lines))
        log = self.query_one("#alerts", RichLog)
        for stamp, kind, text in self.cockpit.drain_alerts():
            log.write(plain(f"{stamp:%H:%M} [{kind}] {text}"))

    @on(Button.Pressed, "#pause")
    async def toggle_pause(self) -> None:
        await self.cockpit.action_toggle_pause()

    @on(Button.Pressed, "#collect")
    def collect(self) -> None:
        self.cockpit.action_collect()

    @on(Button.Pressed, "#digest")
    def digest(self) -> None:
        self.preview_digest()

    @on(Button.Pressed, "#check")
    def check(self) -> None:
        self.cockpit.action_check_linkedin()

    @work(exclusive=True, group="digest")
    async def preview_digest(self) -> None:
        self.report("Building the digest…")
        paused = (await self.services.db.policy()).paused
        with self.cockpit.busy("digest"):
            text = await self.services.digest.build(linkedin=not paused)
        await self.app.push_screen(TextModal("Digest preview (not sent)", text))
