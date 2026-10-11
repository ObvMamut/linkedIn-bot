"""Search LinkedIn for people, firms or jobs (read only, inside the page budget)."""

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Checkbox, DataTable, Input, LoadingIndicator, Select, Static

from linkedin_bot.tui.views.base import View
from linkedin_bot.tui.widgets import clip, fill_table, selected_key
from linkedin_bot.workflows.operations import ResultItem, SearchKind, search_kind

KINDS = [("Jobs", "jobs"), ("People", "people"), ("Firms", "firms")]


class Search(View):
    AUTO_REFRESH = False
    BINDINGS = [
        Binding("o", "open", "Open"),
        Binding("c", "copy", "Copy URL"),
        Binding("p", "pin", "Pin"),
        Binding("s", "save", "Save search"),
        Binding("slash", "focus_query", "Query", show=False),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.items: list[ResultItem] = []
        self.kind: SearchKind = "jobs"
        self.query_text = ""

    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Select(KINDS, value="jobs", allow_blank=False, id="kind")
            yield Input(
                placeholder="security intern in Berlin   (jobs: words, then 'in <place>')",
                id="query",
            )
            yield Checkbox("Bypass cache", id="refresh")
            yield Button("Search", variant="primary", id="go")
        yield Static("", id="budget", classes="hint")
        yield LoadingIndicator(id="searching")
        with Horizontal(classes="split"):
            table: DataTable[Text] = DataTable(id="results", cursor_type="row", zebra_stripes=True)
            table.add_columns("Name", "Details", "Score")
            yield table
            with VerticalScroll(classes="detail"):
                yield Static("Results appear here.", id="result-detail", markup=False)

    def on_mount(self) -> None:
        self.query_one("#searching").display = False

    async def reload(self) -> None:
        usage = self.services.limiter.usage()
        cooldown = int(usage["cooldown_seconds"])
        self.query_one("#budget", Static).update(
            f"LinkedIn page budget: {usage['hour']}/{usage['hourly_limit']} this hour, "
            f"{usage['day']}/{usage['daily_limit']} today"
            + (f" · paused for {cooldown // 60} min" if cooldown else "")
            + ". Searches are cached for 5 minutes."
        )

    def action_focus_query(self) -> None:
        self.query_one("#query", Input).focus()

    @on(Input.Submitted, "#query")
    @on(Button.Pressed, "#go")
    def submit(self) -> None:
        query = self.query_one("#query", Input).value.strip()
        kind = search_kind(str(self.query_one("#kind", Select).value)) or "jobs"
        if not 1 <= len(query) <= 100:
            self.report("Give search words of 1 to 100 characters.", error=True)
            return
        self.run_search(kind, query, self.query_one("#refresh", Checkbox).value)

    @work(exclusive=True, group="search")
    async def run_search(self, kind: SearchKind, query: str, refresh: bool) -> None:
        spinner = self.query_one("#searching")
        spinner.display = True
        try:
            with self.cockpit.busy("search"):
                items, error = await self.services.ops.search(kind, query, refresh=refresh)
        finally:
            spinner.display = False
        await self.reload()
        if error is not None or items is None:
            self.report(error or "Search failed.", error=True)
            return
        self.items, self.kind, self.query_text = items, kind, query
        fill_table(
            self.query_one("#results", DataTable),
            (
                (
                    item.key,
                    (
                        clip(item.title[2:], 50),
                        clip(" · ".join(line for line in item.lines if line), 80),
                        "" if item.score is None else item.score,
                    ),
                )
                for item in items
            ),
            keep_cursor=False,
        )
        self.show_detail()
        self.query_one("#results", DataTable).focus()  # so o, c, p and s work at once
        self.report(f"{len(items)} result(s).")
        if not items:
            self.query_one("#result-detail", Static).update(f"No {kind} found for “{query}”.")

    def current(self) -> ResultItem | None:
        key = selected_key(self.query_one("#results", DataTable))
        return next((item for item in self.items if item.key == key), None)

    @on(DataTable.RowHighlighted, "#results")
    def show_detail(self) -> None:
        item = self.current()
        if item is None:
            return
        lines = [item.title[2:], *[line for line in item.lines if line], item.url]
        if item.kind == "jobs" and item.matched:
            lines.append("Saved to Jobs (2) and ranked by your settings.")
        self.query_one("#result-detail", Static).update("\n".join(lines))

    def action_open(self) -> None:
        if item := self.current():
            self.app.open_url(item.url)

    def action_copy(self) -> None:
        if item := self.current():
            self.app.copy_to_clipboard(item.url)
            self.report("URL copied.")

    async def action_pin(self) -> None:
        item = self.current()
        if item is None:
            return
        if item.kind == "jobs":
            pin = await self.services.ops.pin_company(item)
        else:
            pin = await self.services.ops.pin(item)
        self.report(f"Pinned {pin.name}.")

    async def action_save(self) -> None:
        if not self.items or not self.query_text:
            self.report("Run a search first.", error=True)
            return
        await self.services.ops.save_search(
            self.kind, self.query_text, [item.key for item in self.items]
        )
        self.report("Saved. Rerun it from Saved (6) to see only new results.")
