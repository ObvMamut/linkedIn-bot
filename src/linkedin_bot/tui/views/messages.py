"""LinkedIn inbox, read only. Message text stays in memory and is never stored or logged."""

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, LoadingIndicator, Static

from linkedin_bot.adapters.linkedin.models import Conversation
from linkedin_bot.tui.views.base import View
from linkedin_bot.tui.widgets import clip, fill_table, selected_key


class Messages(View):
    AUTO_REFRESH = False
    BINDINGS = [Binding("r", "load(True)", "Refresh")]

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.conversations: tuple[Conversation, ...] = ()

    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Button("Load inbox", variant="primary", id="load")
            yield Button("Refresh (bypass cache)", id="refresh")
            yield Static(
                "Opens LinkedIn's messaging page once (cached 60 s). Nothing is sent.",
                classes="hint",
            )
        yield LoadingIndicator(id="loading-messages")
        with Horizontal(classes="split"):
            table: DataTable[Text] = DataTable(id="inbox", cursor_type="row", zebra_stripes=True)
            table.add_columns("From", "Unread", "When")
            yield table
            with VerticalScroll(classes="detail"):
                yield Static("", id="snippet", markup=False)

    def on_mount(self) -> None:
        self.query_one("#loading-messages").display = False

    @on(Button.Pressed, "#load")
    def load_button(self) -> None:
        self.action_load(False)

    @on(Button.Pressed, "#refresh")
    def refresh_button(self) -> None:
        self.action_load(True)

    @work(exclusive=True, group="messages")
    async def action_load(self, refresh: bool = False) -> None:
        spinner = self.query_one("#loading-messages")
        spinner.display = True
        try:
            with self.cockpit.busy("messages"):
                result, error = await self.services.ops.messages(refresh=refresh)
        finally:
            spinner.display = False
        if error is not None or result is None:
            self.report(error or "LinkedIn is unavailable.", error=True)
            return
        self.conversations = tuple(result.items)
        fill_table(
            self.query_one("#inbox", DataTable),
            (
                (str(index), (clip(c.title, 40), "●" if c.unread else "", c.when))
                for index, c in enumerate(self.conversations)
            ),
            keep_cursor=False,
        )
        if result.skipped:
            self.report(f"{result.skipped} unrecognised row(s) skipped.")
        self.show_snippet()

    @on(DataTable.RowHighlighted, "#inbox")
    def show_snippet(self) -> None:
        key = selected_key(self.query_one("#inbox", DataTable))
        if key is None or not key.isdecimal() or int(key) >= len(self.conversations):
            return
        conversation = self.conversations[int(key)]
        self.query_one("#snippet", Static).update(
            f"{conversation.title}\n{conversation.when}\n\n{conversation.snippet}\n\n"
            "Open LinkedIn to read or reply; this bot never sends messages."
        )
