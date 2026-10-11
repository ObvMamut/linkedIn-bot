"""Public job boards: the collection queue, adding boards, intervals and manual runs."""

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Input, Select, Static

from linkedin_bot.adapters.public_http import AdapterError
from linkedin_bot.tui.views.base import View
from linkedin_bot.tui.views.dashboard import when
from linkedin_bot.tui.widgets import ConfirmModal, PromptModal, fill_table, selected_key

PROVIDERS = [("Greenhouse", "greenhouse"), ("Lever", "lever"), ("Ashby", "ashby")]


class Boards(View):
    BINDINGS = [
        Binding("g", "run_now", "Run now"),
        Binding("i", "interval", "Interval"),
        Binding("delete", "delete", "Delete"),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.work: list[dict[str, object]] = []

    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Select(PROVIDERS, value="greenhouse", allow_blank=False, id="provider")
            yield Input(placeholder="board slug, e.g. astranis", id="slug")
            yield Button("Add board", variant="primary", id="add")
        with Horizontal(classes="toolbar"):
            yield Button("Run now", id="run-now")
            yield Button("Interval", id="interval")
            yield Button("Delete", variant="error", id="delete")
            yield Button("Collect everything due", id="collect-due")
        table: DataTable[Text] = DataTable(id="work", cursor_type="row", zebra_stripes=True)
        table.add_columns("Board", "State", "Next", "Every", "Coverage", "Reads", "Problem")
        yield table
        yield Static(
            "Boards are public Greenhouse, Lever and Ashby listings read without LinkedIn. "
            "A board name is a lead until its firm's own careers page links to it.",
            classes="hint",
        )

    async def reload(self) -> None:
        self.work = [
            w
            for w in await self.services.db.work_items()
            if str(w["workflow"]).startswith(("ats:", "adhoc:"))
        ]
        fill_table(
            self.query_one("#work", DataTable),
            (
                (
                    str(w["workflow"]),
                    (
                        str(w["workflow"]).split(":", 1)[1].rsplit(":", 1)[0]
                        if str(w["workflow"]).startswith("adhoc:")
                        else str(w["workflow"]).split(":", 1)[1],
                        w["state"],
                        when(w["due_at"]) if w["state"] != "completed" else "done",
                        f"{int(str(w['interval_seconds'])) // 3600} h"
                        if w["recurring"]
                        else "once",
                        w["coverage"] or "",
                        w["attempts"],
                        w["reason"] or "",
                    ),
                )
                for w in self.work
            ),
        )

    def current(self) -> str | None:
        return selected_key(self.query_one("#work", DataTable))

    @on(Input.Submitted, "#slug")
    @on(Button.Pressed, "#add")
    async def add(self) -> None:
        slug = self.query_one("#slug", Input).value.strip()
        provider = str(self.query_one("#provider", Select).value)
        try:
            board = await self.services.ops.schedule_board(provider, slug)
        except AdapterError, ValueError:
            self.report("That is not a valid board slug (letters, digits, - and _).", error=True)
            return
        self.query_one("#slug", Input).value = ""
        self.report(f"Scheduled {board.provider}/{board.slug}; the first read runs shortly.")
        await self.reload()

    @on(Button.Pressed, "#run-now")
    async def action_run_now(self) -> None:
        workflow = self.current()
        if workflow and await self.services.db.run_work_now(workflow, self.cockpit.now()):
            self.report("Queued.")
        await self.reload()

    @on(Button.Pressed, "#collect-due")
    def collect_due(self) -> None:
        self.cockpit.action_collect()

    @on(Button.Pressed, "#interval")
    def interval_pressed(self) -> None:
        self.action_interval()

    @work(group="boards-edit")
    async def action_interval(self) -> None:
        workflow = self.current()
        if workflow is None:
            return
        answer = await self.app.push_screen_wait(
            PromptModal("Read this board every how many hours? (at least 0.5)", "6")
        )
        if answer is None:
            return
        try:
            hours = float(answer)
            await self.services.db.set_work_interval(workflow, int(hours * 3600))
        except ValueError:
            self.report("Give a number of hours, at least 0.5.", error=True)
            return
        await self.reload()

    @on(Button.Pressed, "#delete")
    def delete_pressed(self) -> None:
        self.action_delete()

    @work(group="boards-edit")
    async def action_delete(self) -> None:
        workflow = self.current()
        if workflow is None:
            return
        if await self.app.push_screen_wait(
            ConfirmModal(
                f"Stop collecting {workflow}? Postings already collected are kept.",
                confirm_label="Delete",
                danger=True,
            )
        ):
            if not await self.services.db.delete_work(workflow):
                self.report("It is being read right now; try again in a moment.", error=True)
            await self.reload()
