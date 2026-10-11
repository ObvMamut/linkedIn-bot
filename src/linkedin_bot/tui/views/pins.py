"""Pinned firms and people: notes, tags, job boards."""

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Static

from linkedin_bot.domain.workspace import Pin
from linkedin_bot.tui.views.base import View
from linkedin_bot.tui.widgets import ConfirmModal, PromptModal, clip, fill_table, selected_key
from linkedin_bot.workflows.operations import split_items


class Pins(View):
    BINDINGS = [
        Binding("o", "open", "Open"),
        Binding("n", "note", "Note"),
        Binding("t", "tags", "Tags"),
        Binding("b", "board", "Find board"),
        Binding("g", "collect", "Collect now"),
        Binding("delete", "unpin", "Unpin"),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.pins: tuple[Pin, ...] = ()
        self.work: dict[str, dict[str, object]] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Button("Note", id="note")
            yield Button("Tags", id="tags")
            yield Button("Find job board", id="board")
            yield Button("Collect now", id="collect")
            yield Button("Unpin", variant="error", id="unpin")
        with Horizontal(classes="split"):
            table: DataTable[Text] = DataTable(id="pins", cursor_type="row", zebra_stripes=True)
            table.add_columns("Kind", "Name", "Board", "Tags", "Note")
            yield table
            with VerticalScroll(classes="detail"):
                yield Static(
                    "Nothing pinned yet. Pin people or firms from Search (3), or a firm from "
                    "Jobs (2) with p.",
                    id="pin-detail",
                    markup=False,
                )

    async def reload(self) -> None:
        self.pins = await self.services.ops.store.pins()
        self.work = {str(w["workflow"]): w for w in await self.services.db.work_items()}
        fill_table(
            self.query_one("#pins", DataTable),
            (
                (
                    pin.url,
                    (
                        "firm" if pin.kind == "firm" else "person",
                        clip(pin.name, 40),
                        f"{pin.board_provider}/{pin.board_slug}" if pin.board_slug else "",
                        clip(", ".join(pin.tags), 30),
                        clip(pin.note, 40),
                    ),
                )
                for pin in self.pins
            ),
        )
        self.show_detail()

    def current(self) -> Pin | None:
        key = selected_key(self.query_one("#pins", DataTable))
        return next((pin for pin in self.pins if pin.url == key), None)

    @on(DataTable.RowHighlighted, "#pins")
    def show_detail(self) -> None:
        pin = self.current()
        if pin is None:
            return
        lines = [pin.name, pin.url, f"Pinned: {pin.pinned_at[:16]}"]
        lines.append(f"Tags: {', '.join(pin.tags) or 'none'}")
        lines.append(f"Note: {pin.note or 'none'}")
        if pin.kind == "firm":
            if pin.board_slug:
                work = self.work.get(f"ats:{pin.board_provider}:{pin.board_slug}")
                lines.append(f"Job board: {pin.board_provider}/{pin.board_slug}")
                if work:
                    lines.append(
                        f"Collection: {work['state']}, coverage {work['coverage'] or 'not yet'}, "
                        f"next {str(work['due_at'])[:16]}"
                        + (f", last problem {work['reason']}" if work["reason"] else "")
                    )
            else:
                lines.append("Job board: not linked (b finds a public Greenhouse/Lever/Ashby one)")
        self.query_one("#pin-detail", Static).update("\n".join(lines))

    def action_open(self) -> None:
        if pin := self.current():
            self.app.open_url(pin.url)

    @on(Button.Pressed, "#note")
    def _pressed_note(self) -> None:
        self.action_note()

    @work(group="pin-edit")
    async def action_note(self) -> None:
        pin = self.current()
        if pin is None:
            return
        note = await self.app.push_screen_wait(
            PromptModal(f"Note for {pin.name} (max 500 characters)", pin.note, multiline=True)
        )
        if note is not None:
            await self.services.ops.store.set_note(pin.kind, pin.url, note.strip()[:500])
            await self.reload()

    @on(Button.Pressed, "#tags")
    def _pressed_tags(self) -> None:
        self.action_tags()

    @work(group="pin-edit")
    async def action_tags(self) -> None:
        pin = self.current()
        if pin is None:
            return
        tags = await self.app.push_screen_wait(
            PromptModal(f"Tags for {pin.name}, separated by commas", ", ".join(pin.tags))
        )
        if tags is not None:
            await self.services.ops.store.set_tags(pin.kind, pin.url, tuple(split_items(tags)))
            await self.reload()

    @on(Button.Pressed, "#board")
    def _pressed_board(self) -> None:
        self.action_board()

    @work(exclusive=True, group="pin-board")
    async def action_board(self) -> None:
        pin = self.current()
        if pin is None or pin.kind != "firm":
            self.report("Choose a pinned firm first.", error=True)
            return
        self.report(f"Looking for a public job board for {pin.name}…")
        with self.cockpit.busy("board"):
            found = await self.services.ops.link_board(pin)
        if found is None:
            self.report("No public Greenhouse, Lever or Ashby board found.", error=True)
        else:
            self.report(f"Linked {found[0]}/{found[1]}; collection is scheduled.")
        await self.reload()

    @on(Button.Pressed, "#collect")
    async def action_collect(self) -> None:
        pin = self.current()
        if pin is None or not pin.board_slug:
            self.report("Link a job board first (b).", error=True)
            return
        workflow = f"ats:{pin.board_provider}:{pin.board_slug}"
        if workflow not in self.work:
            assert pin.board_provider is not None
            await self.services.ops.schedule_board(pin.board_provider, pin.board_slug)
        else:
            await self.services.db.run_work_now(workflow, self.cockpit.now())
        self.report("Collection queued; new postings appear in Jobs (2).")
        await self.reload()

    @on(Button.Pressed, "#unpin")
    def _pressed_unpin(self) -> None:
        self.action_unpin()

    @work(group="pin-edit")
    async def action_unpin(self) -> None:
        pin = self.current()
        if pin is None:
            return
        if await self.app.push_screen_wait(
            ConfirmModal(f"Unpin {pin.name}?", confirm_label="Unpin", danger=True)
        ):
            await self.services.ops.store.unpin(pin.kind, pin.url)
            self.report(f"Unpinned {pin.name}.")
            await self.reload()
