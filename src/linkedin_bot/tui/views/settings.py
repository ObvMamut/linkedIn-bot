"""Settings: every ranking rule, the priority order, interests, digest and alerts."""

import re

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    Select,
    Static,
    Switch,
    TextArea,
)

from linkedin_bot.domain.models import DEFAULT_PRIORITIES, SearchPreferences
from linkedin_bot.tui.views.base import View
from linkedin_bot.tui.widgets import fill_table, selected_key
from linkedin_bot.workflows.operations import LANGUAGES, LIST_FIELDS

LIST_LABELS = {
    "roles": "Roles you target (rank)",
    "must_keywords": "Must have words (hard filter)",
    "nice_keywords": "Nice to have words (rank)",
    "exclusions": "Excluded words (hard filter)",
    "locations": "Locations (rank)",
    "blocked_firms": "Blocked firms (hard filter)",
    "preferred_industries": "Preferred industries (rank)",
}
LANGUAGE_NAMES = {
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "pl": "Polish",
}
PRIORITY_LABELS = {
    "role": "Role fit",
    "location": "Location",
    "firm": "Pinned / preferred firms",
    "recency": "Freshness",
    "language": "Language",
}
REMOTE = [("Any", "any"), ("Remote preferred", "yes"), ("On-site only", "no")]
AGES = [("Any age", "any"), *((f"{d} days", str(d)) for d in (7, 14, 30, 60, 90, 180))]
TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def split_list(text: str) -> list[str]:
    return [part.strip() for part in text.replace("\n", ",").split(",") if part.strip()]


class Settings(View):
    AUTO_REFRESH = False
    FOCUS = "#settings-scroll"

    def compose(self) -> ComposeResult:
        yield Static("", id="preview", classes="hint")
        with VerticalScroll(id="settings-scroll"):
            yield Label("Keywords, places and firms", classes="section")
            yield Static(
                "Comma separated. Enter saves one field; the button saves them all.",
                classes="hint",
            )
            for field in (
                "roles",
                "must_keywords",
                "nice_keywords",
                "exclusions",
                "locations",
                "blocked_firms",
                "preferred_industries",
            ):
                with Horizontal(classes="field"):
                    yield Label(LIST_LABELS[field], classes="field-label")
                    yield Input(id=f"list-{field}")
            yield Button("Save all lists", variant="primary", id="save-lists")

            yield Label("Where and what", classes="section")
            with Horizontal(classes="field"):
                yield Label("Remote", classes="field-label")
                yield Select(REMOTE, allow_blank=False, id="remote")
            with Horizontal(classes="field"):
                yield Label("Internships only boost", classes="field-label")
                yield Switch(id="internship")
            with Horizontal(classes="field"):
                yield Label("Maximum posting age", classes="field-label")
                yield Select(AGES, allow_blank=False, id="age")
            with Horizontal(classes="field"):
                yield Label("Languages (none ticked = any)", classes="field-label")
                for code in LANGUAGES:
                    yield Checkbox(LANGUAGE_NAMES[code], id=f"lang-{code}")

            yield Label("Priority order (higher counts more)", classes="section")
            with Horizontal(classes="field"):
                table: DataTable[Text] = DataTable(id="priorities", cursor_type="row")
                table.add_columns("#", "Criterion")
                yield table
                with Vertical(classes="priority-buttons"):
                    yield Button("Up", id="prio-up")
                    yield Button("Down", id="prio-down")
                    yield Button("Remove", id="prio-remove")
                    yield Select([], prompt="Add back…", id="prio-add")

            yield Label("Interests (for the optional local model check)", classes="section")
            yield TextArea(id="interests")
            yield Button("Save interests", id="save-interests")

            yield Label(
                "Digest and alerts (only while the cockpit or serve runs)", classes="section"
            )
            with Horizontal(classes="field"):
                yield Label("Daily digest", classes="field-label")
                yield Switch(id="digest-enabled")
                yield Input(placeholder="18:00", id="digest-time", classes="short")
            with Horizontal(classes="field"):
                yield Label("New openings at pinned firms", classes="field-label")
                yield Switch(id="alerts-boards")
            with Horizontal(classes="field"):
                yield Label("Unread LinkedIn messages", classes="field-label")
                yield Switch(id="alerts-messages")
            with Horizontal(classes="field"):
                yield Label("Quiet hours", classes="field-label")
                yield Input(placeholder="22:00", id="quiet-start", classes="short")
                yield Input(placeholder="08:00", id="quiet-end", classes="short")
            yield Button("Save digest times", id="save-digest")

    async def reload(self) -> None:
        preferences = await self.services.ops.preferences()
        digest = await self.services.ops.store.digest()
        with self.prevent(Switch.Changed, Select.Changed, Checkbox.Changed):
            for field in LIST_FIELDS:
                self.query_one(f"#list-{field}", Input).value = ", ".join(
                    getattr(preferences, field)
                )
            self.query_one("#remote", Select).value = {None: "any", True: "yes", False: "no"}[
                preferences.remote
            ]
            self.query_one("#internship", Switch).value = bool(preferences.internship)
            self.query_one("#age", Select).value = (
                str(preferences.max_age_days)
                if str(preferences.max_age_days) in {v for _, v in AGES}
                else "any"
            )
            for code in LANGUAGES:
                self.query_one(f"#lang-{code}", Checkbox).value = code in preferences.languages
            self.query_one("#digest-enabled", Switch).value = digest.enabled
            self.query_one("#alerts-boards", Switch).value = digest.alerts_boards
            self.query_one("#alerts-messages", Switch).value = digest.alerts_messages
            missing = [
                (PRIORITY_LABELS[p], p)
                for p in DEFAULT_PRIORITIES
                if p not in preferences.priorities
            ]
            self.query_one("#prio-add", Select).set_options(missing)
        self.query_one("#interests", TextArea).text = preferences.interests
        self.query_one("#digest-time", Input).value = digest.local_time
        self.query_one("#quiet-start", Input).value = digest.quiet_start
        self.query_one("#quiet-end", Input).value = digest.quiet_end
        self.fill_priorities(preferences)
        await self.preview(preferences)

    def fill_priorities(self, preferences: SearchPreferences) -> None:
        fill_table(
            self.query_one("#priorities", DataTable),
            (
                (name, (index + 1, PRIORITY_LABELS[name]))
                for index, name in enumerate(preferences.priorities)
            ),
        )

    async def preview(self, preferences: SearchPreferences | None = None) -> None:
        count = await self.services.ops.matching_count(preferences)
        self.query_one("#preview", Static).update(
            f"{count} recent posting(s) match these settings. Changes save immediately and "
            "re-rank Jobs."
        )

    async def changed(self, preferences: SearchPreferences, message: str = "Saved.") -> None:
        await self.preview(preferences)
        self.report(message)

    # ---- lists ---------------------------------------------------------------------------
    @on(Input.Submitted)
    async def list_submitted(self, event: Input.Submitted) -> None:
        identifier = event.input.id or ""
        if identifier.startswith("list-"):
            field = identifier.removeprefix("list-")
            preferences = await self.services.ops.set_list(field, split_list(event.value))
            await self.changed(preferences, f"Saved {LIST_LABELS[field].split(' (')[0].lower()}.")
        elif identifier in ("digest-time", "quiet-start", "quiet-end"):
            await self.save_digest()

    @on(Button.Pressed, "#save-lists")
    async def save_lists(self) -> None:
        preferences = None
        for field in LIST_FIELDS:
            value = self.query_one(f"#list-{field}", Input).value
            preferences = await self.services.ops.set_list(field, split_list(value))
        assert preferences is not None
        await self.reload()
        await self.changed(preferences, "All lists saved.")

    # ---- switches and selects -----------------------------------------------------------
    @on(Select.Changed, "#remote")
    async def remote_changed(self, event: Select.Changed) -> None:
        value = {"any": None, "yes": True, "no": False}[str(event.value)]
        await self.changed(await self.services.ops.set_remote(value))

    @on(Switch.Changed, "#internship")
    async def internship_changed(self, event: Switch.Changed) -> None:
        await self.changed(await self.services.ops.set_internship(True if event.value else None))

    @on(Select.Changed, "#age")
    async def age_changed(self, event: Select.Changed) -> None:
        value = None if event.value == "any" else int(str(event.value))
        await self.changed(await self.services.ops.set_max_age(value))

    @on(Checkbox.Changed)
    async def language_changed(self, event: Checkbox.Changed) -> None:
        identifier = event.checkbox.id or ""
        if identifier.startswith("lang-"):
            await self.changed(
                await self.services.ops.toggle_language(identifier.removeprefix("lang-"))
            )

    # ---- priorities ---------------------------------------------------------------------
    async def priority(self, operation: str, name: str | None) -> None:
        if name is None:
            return
        preferences = await self.services.ops.change_priority(operation, name)
        self.fill_priorities(preferences)
        table = self.query_one("#priorities", DataTable)
        if name in preferences.priorities:
            table.move_cursor(row=preferences.priorities.index(name))
        missing = [
            (PRIORITY_LABELS[p], p) for p in DEFAULT_PRIORITIES if p not in preferences.priorities
        ]
        with self.prevent(Select.Changed):
            self.query_one("#prio-add", Select).set_options(missing)
        await self.changed(preferences)

    @on(Button.Pressed, "#prio-up")
    async def priority_up(self) -> None:
        await self.priority("up", selected_key(self.query_one("#priorities", DataTable)))

    @on(Button.Pressed, "#prio-down")
    async def priority_down(self) -> None:
        await self.priority("dn", selected_key(self.query_one("#priorities", DataTable)))

    @on(Button.Pressed, "#prio-remove")
    async def priority_remove(self) -> None:
        await self.priority("rm", selected_key(self.query_one("#priorities", DataTable)))

    @on(Select.Changed, "#prio-add")
    async def priority_add(self, event: Select.Changed) -> None:
        if event.value is not Select.NULL:
            await self.priority("add", str(event.value))

    # ---- interests and digest -----------------------------------------------------------
    @on(Button.Pressed, "#save-interests")
    async def save_interests(self) -> None:
        text = self.query_one("#interests", TextArea).text
        await self.changed(await self.services.ops.set_interests(text), "Interests saved.")

    @on(Switch.Changed, "#digest-enabled")
    async def digest_enabled(self, event: Switch.Changed) -> None:
        await self.services.ops.store.update_digest(enabled=event.value)
        self.report("Daily digest " + ("on." if event.value else "off."))

    @on(Switch.Changed, "#alerts-boards")
    async def alerts_boards(self, event: Switch.Changed) -> None:
        await self.services.ops.store.update_digest(alerts_boards=event.value)
        self.report("Pinned firm alerts " + ("on." if event.value else "off."))

    @on(Switch.Changed, "#alerts-messages")
    async def alerts_messages(self, event: Switch.Changed) -> None:
        await self.services.ops.store.update_digest(alerts_messages=event.value)
        self.report("Message alerts " + ("on." if event.value else "off."))

    @on(Button.Pressed, "#save-digest")
    async def save_digest(self) -> None:
        values = {
            "local_time": self.query_one("#digest-time", Input).value.strip(),
            "quiet_start": self.query_one("#quiet-start", Input).value.strip(),
            "quiet_end": self.query_one("#quiet-end", Input).value.strip(),
        }
        if not all(TIME.match(value) for value in values.values()):
            self.report("Times are 24 hour HH:MM, for example 18:00.", error=True)
            return
        await self.services.ops.store.update_digest(**values)
        self.report("Digest times saved.")
