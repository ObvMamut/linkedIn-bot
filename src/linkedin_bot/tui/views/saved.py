"""Saved searches: rerun to see only what is new since the last run."""

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Static

from linkedin_bot.domain.workspace import SavedSearch
from linkedin_bot.tui.views.base import View
from linkedin_bot.tui.widgets import ConfirmModal, PromptModal, clip, fill_table, selected_key
from linkedin_bot.workflows.operations import ResultItem


class Saved(View):
    BINDINGS = [
        Binding("enter", "run", "Run", show=False),
        Binding("g", "run", "Run"),
        Binding("e", "rename", "Rename"),
        Binding("delete", "delete", "Delete"),
        Binding("o", "open", "Open result"),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.saved: tuple[SavedSearch, ...] = ()
        self.new_items: list[ResultItem] = []

    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Button("Run (new only)", variant="primary", id="run")
            yield Button("Rename", id="rename")
            yield Button("Delete", variant="error", id="delete")
        with Vertical():
            table: DataTable[Text] = DataTable(id="saved", cursor_type="row", zebra_stripes=True)
            table.add_columns("Name", "Kind", "Query", "Last run")
            yield table
            yield Static("New results", classes="panel-title")
            fresh: DataTable[Text] = DataTable(id="fresh", cursor_type="row", zebra_stripes=True)
            fresh.add_columns("Result", "Details")
            yield fresh

    async def reload(self) -> None:
        self.saved = await self.services.ops.store.saved()
        fill_table(
            self.query_one("#saved", DataTable),
            (
                (s.id, (clip(s.name, 40), s.kind, clip(s.query, 40), (s.last_run or "")[:16]))
                for s in self.saved
            ),
        )

    def current(self) -> SavedSearch | None:
        key = selected_key(self.query_one("#saved", DataTable))
        return next((s for s in self.saved if s.id == key), None)

    @on(Button.Pressed, "#run")
    def _pressed_run(self) -> None:
        self.action_run()

    @work(exclusive=True, group="saved")
    async def action_run(self) -> None:
        saved = self.current()
        if saved is None:
            self.report("No saved searches yet. Save one from Search (3).", error=True)
            return
        self.report(f"Running {saved.name}…")
        with self.cockpit.busy("saved"):
            run = await self.services.ops.run_saved(saved.id)
        if run.error is not None:
            self.report(run.error, error=True)
            return
        self.new_items = run.new_items
        fill_table(
            self.query_one("#fresh", DataTable),
            (
                (
                    item.key,
                    (clip(item.title[2:], 50), clip(" · ".join(filter(None, item.lines)), 80)),
                )
                for item in run.new_items
            ),
            keep_cursor=False,
        )
        self.report(f"{len(run.new_items)} new since the last run.")
        await self.reload()

    @on(Button.Pressed, "#rename")
    def _pressed_rename(self) -> None:
        self.action_rename()

    @work(group="saved-edit")
    async def action_rename(self) -> None:
        saved = self.current()
        if saved is None:
            return
        name = await self.app.push_screen_wait(PromptModal("New name", saved.name))
        if name:
            await self.services.ops.rename_saved(saved.id, name)
            await self.reload()

    @on(Button.Pressed, "#delete")
    def _pressed_delete(self) -> None:
        self.action_delete()

    @work(group="saved-edit")
    async def action_delete(self) -> None:
        saved = self.current()
        if saved is None:
            return
        if await self.app.push_screen_wait(
            ConfirmModal(
                f"Delete the saved search {saved.name}?", confirm_label="Delete", danger=True
            )
        ):
            await self.services.ops.store.delete_saved(saved.id)
            await self.reload()

    def action_open(self) -> None:
        key = selected_key(self.query_one("#fresh", DataTable))
        if key:
            self.app.open_url(key)
