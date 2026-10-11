"""Logs: the live event stream (event names and counts only, never content)."""

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, RichLog, Select, Static

from linkedin_bot.tui.logbridge import Event
from linkedin_bot.tui.views.base import View
from linkedin_bot.tui.widgets import plain

LEVELS = [("Everything", "all"), ("Warnings only", "warnings")]


def line(event: Event) -> str:
    extra = []
    if event.count is not None:
        extra.append(f"count={event.count}")
    if event.duration_ms is not None:
        extra.append(f"{event.duration_ms} ms")
    return f"{event.at:%H:%M:%S} {event.level:<7} {event.event} {' '.join(extra)}".rstrip()


class Logs(View):
    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.serial = 0

    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Select(LEVELS, value="all", allow_blank=False, id="level")
            yield Button("Clear", id="clear")
            yield Static("Also written to data_dir/logs/events.log.", classes="hint")
        yield RichLog(id="log", markup=False, wrap=False, max_lines=2000)

    def on_mount(self) -> None:
        self.set_interval(1.0, self.pull)

    def pull(self) -> None:
        self.serial, events = self.cockpit.events.since(self.serial)
        warnings_only = self.query_one("#level", Select).value == "warnings"
        log = self.query_one("#log", RichLog)
        for event in events:
            if warnings_only and event.level not in ("WARNING", "ERROR", "CRITICAL"):
                continue
            log.write(plain(line(event)))

    @on(Select.Changed, "#level")
    def level_changed(self) -> None:
        self.query_one("#log", RichLog).clear()
        self.serial = 0
        self.pull()

    @on(Button.Pressed, "#clear")
    def clear(self) -> None:
        self.query_one("#log", RichLog).clear()
