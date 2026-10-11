"""Jobs: every collected posting ranked by your settings, with your own tracker."""

from datetime import datetime

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Checkbox, DataTable, Input, Static

from linkedin_bot.adapters.public_http import AdapterError
from linkedin_bot.storage.database import TrackerStatus
from linkedin_bot.tui.views.base import View
from linkedin_bot.tui.widgets import PromptModal, TextModal, clip, fill_table, selected_key
from linkedin_bot.workflows.materials import draft_cover_letter
from linkedin_bot.workflows.operations import ResultItem

COLUMNS = ("Score", "Title", "Company", "Location", "Source", "Seen", "Status")
SORTS = {"Score": "score", "Title": "title", "Company": "company", "Seen": "seen"}


def age(stamp: datetime | None) -> str:
    if stamp is None:
        return ""
    days = (datetime.now(stamp.tzinfo) - stamp).days
    return "today" if days <= 0 else f"{days} d"


class Jobs(View):
    BINDINGS = [
        Binding("o", "open", "Open"),
        Binding("c", "copy", "Copy URL"),
        Binding("i", "mark('interested')", "Interested"),
        Binding("a", "mark('applied')", "Applied"),
        Binding("v", "mark('interviewing')", "Interviewing"),
        Binding("x", "mark('dismissed')", "Dismiss"),
        Binding("u", "mark('new')", "Unmark", show=False),
        Binding("n", "note", "Note"),
        Binding("p", "pin", "Pin firm"),
        Binding("w", "draft", "Draft letter"),
        Binding("slash", "filter", "Filter", show=False),
    ]

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.items: list[ResultItem] = []
        self.sort = "score"
        self.reverse = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Input(placeholder="Filter by title, company or location (/)", id="filter")
            yield Checkbox("Outside my settings", id="outside")
            yield Checkbox("Dismissed", id="dismissed")
        with Horizontal(classes="split"):
            table: DataTable[Text] = DataTable(
                id="jobs-table", cursor_type="row", zebra_stripes=True
            )
            table.add_columns(*COLUMNS)
            yield table
            with VerticalScroll(classes="detail"):
                yield Static("Select a posting.", id="job-detail", markup=False)

    async def reload(self) -> None:
        self.items = await self.services.ops.ranked_jobs(
            include_outside=self.query_one("#outside", Checkbox).value,
            include_dismissed=self.query_one("#dismissed", Checkbox).value,
        )
        self.render_rows()

    def shown_items(self) -> list[ResultItem]:
        words = self.query_one("#filter", Input).value.lower().split()
        chosen = [
            item
            for item in self.items
            if all(
                word in f"{item.title} {item.company} {item.location} {item.status}".lower()
                for word in words
            )
        ]
        keys = {
            "score": lambda i: (i.score is None, -(i.score or 0)),
            "title": lambda i: i.title.lower(),
            "company": lambda i: (i.company or "").lower(),
            "seen": lambda i: -(i.first_seen.timestamp() if i.first_seen else 0),
        }
        return sorted(chosen, key=keys[self.sort], reverse=self.reverse)

    def render_rows(self) -> None:
        table = self.query_one("#jobs-table", DataTable)
        fill_table(
            table,
            (
                (
                    item.key,
                    (
                        "" if item.score is None else item.score,
                        clip(item.title.removeprefix("💼 "), 60),
                        clip(item.company or "", 30),
                        clip(item.location or "", 30),
                        item.source or "",
                        age(item.first_seen),
                        item.status if item.status != "new" else "",
                    ),
                )
                for item in self.shown_items()
            ),
        )
        self.show_detail()

    def current(self) -> ResultItem | None:
        key = selected_key(self.query_one("#jobs-table", DataTable))
        return next((item for item in self.items if item.key == key), None)

    def show_detail(self) -> None:
        item = self.current()
        detail = self.query_one("#job-detail", Static)
        if item is None:
            detail.update("No postings yet. Pin firms with a job board, or search jobs (3).")
            return
        lines = [
            item.title.removeprefix("💼 "),
            f"{item.company or ''} · {item.location or 'location unknown'} · {item.source}",
            item.url,
            "",
            f"Score: {item.score}" if item.score is not None else "Outside your settings",
        ]
        if item.reasons:
            lines.append("Why: " + ", ".join(item.reasons))
        if item.gaps:
            lines.append("Gaps: " + ", ".join(item.gaps))
        if item.published_at:
            lines.append(f"Published: {item.published_at[:10]}")
        if item.first_seen:
            lines.append(f"First seen: {item.first_seen.astimezone():%Y-%m-%d %H:%M}")
        lines.append(f"Your status: {item.status}" + (f" — {item.note}" if item.note else ""))
        if item.description:
            lines += ["", clip(item.description, 3000)]
        detail.update("\n".join(lines))

    @on(DataTable.RowHighlighted, "#jobs-table")
    def highlighted(self) -> None:
        self.show_detail()

    @on(DataTable.HeaderSelected, "#jobs-table")
    def sort_by(self, event: DataTable.HeaderSelected) -> None:
        chosen = SORTS.get(str(event.label))
        if chosen is None:
            return
        self.reverse = not self.reverse if chosen == self.sort else False
        self.sort = chosen
        self.render_rows()

    @on(Input.Changed, "#filter")
    def filtered(self) -> None:
        self.render_rows()

    @on(Checkbox.Changed)
    async def toggled(self) -> None:
        await self.reload()

    def action_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_open(self) -> None:
        if item := self.current():
            self.app.open_url(item.url)

    def action_copy(self) -> None:
        if item := self.current():
            self.app.copy_to_clipboard(item.url)
            self.report("URL copied.")

    async def action_mark(self, status: TrackerStatus) -> None:
        item = self.current()
        if item is None or item.source is None or item.external_id is None:
            return
        await self.services.ops.set_status(item.source, item.external_id, status)
        self.report(f"Marked {status}." if status != "new" else "Status cleared.")
        await self.reload()

    @work(group="jobs-note")
    async def action_note(self) -> None:
        item = self.current()
        if item is None or item.source is None or item.external_id is None:
            return
        note = await self.app.push_screen_wait(
            PromptModal(f"Note for {item.title.removeprefix('💼 ')}", item.note)
        )
        if note is None:
            return
        status: TrackerStatus = "interested" if item.status == "new" else item.status  # type: ignore[assignment]
        await self.services.ops.set_status(item.source, item.external_id, status, note[:1000])
        await self.reload()

    async def action_pin(self) -> None:
        item = self.current()
        if item is None or not item.company:
            return
        pin = await self.services.ops.pin_company(item)
        self.report(
            f"Pinned {pin.name}" + (f" (board {pin.board_provider})" if pin.board_slug else "")
        )
        await self.reload()

    @work(exclusive=True, group="draft")
    async def action_draft(self) -> None:
        item = self.current()
        generator = self.services.generator
        if item is None or item.source is None or item.external_id is None:
            return
        if generator is None:
            self.report("No local model is configured (inference.model).", error=True)
            return
        record = await self.services.db.posting(item.source, item.external_id)
        if record is None:
            return
        self.report("Drafting a cover letter with the local model…")
        try:
            with self.cockpit.busy("draft"):
                artifact = await draft_cover_letter(self.services.db, generator, record.observation)
        except AdapterError as exc:
            self.report(f"Draft not produced: {exc.code}. Nothing was saved.", error=True)
            return
        text = await self.services.db.artifact_text(artifact)
        await self.app.push_screen(TextModal("Cover letter draft (not sent anywhere)", text))
