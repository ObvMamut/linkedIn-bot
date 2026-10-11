"""Profile: onboarding, the facts drafts may cite, your CV, and Telegram pairing."""

import asyncio
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Label, Static, TextArea

from linkedin_bot.adapters.public_http import AdapterError
from linkedin_bot.domain.models import now
from linkedin_bot.storage.telegram import TelegramStore
from linkedin_bot.tui.views.base import View
from linkedin_bot.tui.widgets import (
    ChoiceModal,
    ConfirmModal,
    PromptModal,
    TextModal,
    clip,
    fill_table,
    selected_key,
)
from linkedin_bot.workflows import facts
from linkedin_bot.workflows.onboarding import PROMPTS, STEPS, baseline_cv, save_cv

MAX_CV_FILE = 1_000_000


def read_text_file(answer: str) -> str | None:
    path = Path(answer).expanduser()
    try:
        if not path.is_file() or path.stat().st_size > MAX_CV_FILE:
            return None
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError, OSError:
        return None


class Profile(View):
    AUTO_REFRESH = False
    FOCUS = "#profile-scroll"

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="profile-scroll"):
            with Vertical(id="onboarding"):
                yield Label("Finish onboarding", classes="section")
                for step in STEPS:
                    yield Static(PROMPTS[step], classes="hint")
                    if step == "cv":
                        yield TextArea(id="answer-cv")
                    else:
                        yield Input(id=f"answer-{step}")
                yield Button("Finish onboarding", variant="primary", id="finish")

            yield Label("Facts drafts may cite", classes="section")
            yield Static("", id="facts-summary", classes="hint")
            with Horizontal(classes="toolbar"):
                yield Button("Add", id="fact-add")
                yield Button("Confirm", variant="success", id="fact-confirm")
                yield Button("Edit", id="fact-edit")
                yield Button("Reject", variant="error", id="fact-reject")
                yield Button("Propose from CV (local model)", id="fact-extract")
            table: DataTable[Text] = DataTable(id="facts", cursor_type="row", zebra_stripes=True)
            table.add_columns("Status", "Kind", "Fact", "Source")
            yield table

            yield Label("CV", classes="section")
            yield Static("", id="cv-summary", markup=False)
            with Horizontal(classes="toolbar"):
                yield Button("View", id="cv-view")
                yield Button("Replace (paste)", id="cv-paste")
                yield Button("Replace from file", id="cv-file")

            yield Label("Telegram", classes="section")
            yield Static("", id="telegram-state", markup=False)
            with Horizontal(classes="toolbar"):
                yield Button("New pairing code", id="pair")
                yield Button("Unpair", variant="error", id="unpair")

    async def reload(self) -> None:
        db = self.services.db
        done = await self.services.onboarding.done()
        self.query_one("#onboarding").display = not done
        items = await facts.facts(db)
        counts = facts.summary(items)
        self.query_one("#facts-summary", Static).update(
            f"{counts['confirmed']} confirmed, {counts['unconfirmed']} waiting for you, "
            f"{counts['rejected']} rejected. Only confirmed facts are ever used."
        )
        fill_table(
            self.query_one("#facts", DataTable),
            (
                (
                    item.id,
                    (
                        item.status,
                        item.kind,
                        clip(item.value or "", 70),
                        clip(item.source_reference, 20),
                    ),
                )
                for item in items
            ),
        )
        cv = await baseline_cv(db)
        self.query_one("#cv-summary", Static).update(
            "No CV stored yet."
            if cv is None
            else f"Stored CV: {len(cv)} characters, "
            f"{len(cv.splitlines())} lines. First line: {clip(cv.splitlines()[0] if cv else '', 60)}"
        )
        binding = await TelegramStore(db).binding()
        telegram = (
            "No bot token in LINKEDIN_AUTOMATION_BOT_TOKEN, so Telegram is off in this session."
            if self.services.api is None
            else "Paired with your private chat."
            if binding
            else "Not paired."
        )
        self.query_one("#telegram-state", Static).update(telegram)

    # ---- onboarding ----------------------------------------------------------------------
    @on(Button.Pressed, "#finish")
    async def finish(self) -> None:
        answers = {
            step: (
                self.query_one("#answer-cv", TextArea).text
                if step == "cv"
                else self.query_one(f"#answer-{step}", Input).value
            )
            for step in STEPS
        }
        try:
            await self.services.onboarding.finish_all(answers)
        except ValueError as exc:
            self.report(f"Please answer: {str(exc).removeprefix('missing_')}.", error=True)
            return
        self.report("Onboarding complete. Your settings, eligibility and CV are saved.")
        await self.reload()

    # ---- facts ---------------------------------------------------------------------------
    def current_fact(self) -> str | None:
        return selected_key(self.query_one("#facts", DataTable))

    @on(Button.Pressed, "#fact-add")
    def _pressed_add_fact(self) -> None:
        self.add_fact()

    @work(group="facts")
    async def add_fact(self) -> None:
        kind = await self.app.push_screen_wait(
            ChoiceModal(
                "Add a fact about yourself (it is confirmed as you type it)",
                [(kind.replace("_", " "), kind) for kind in facts.FACT_KINDS],
                note="The fact, e.g. 'Python, 3 years'",
            )
        )
        if kind is None:
            return
        chosen, _, value = kind.partition("|")
        try:
            await facts.add(self.services.db, chosen, value)
        except ValueError:
            self.report("A fact is 1 to 500 characters.", error=True)
            return
        await self.reload()

    @on(Button.Pressed, "#fact-confirm")
    async def confirm_fact(self) -> None:
        if fact_id := self.current_fact():
            await facts.confirm(self.services.db, fact_id)
            await self.reload()

    @on(Button.Pressed, "#fact-reject")
    async def reject_fact(self) -> None:
        if fact_id := self.current_fact():
            await facts.reject(self.services.db, fact_id)
            await self.reload()

    @on(Button.Pressed, "#fact-edit")
    def _pressed_edit_fact(self) -> None:
        self.edit_fact()

    @work(group="facts")
    async def edit_fact(self) -> None:
        fact_id = self.current_fact()
        if fact_id is None:
            return
        current = await facts.fact(self.services.db, fact_id)
        value = await self.app.push_screen_wait(PromptModal("Edit the fact", current.value or ""))
        if value is None:
            return
        try:
            await facts.edit(self.services.db, fact_id, value)
        except ValueError:
            self.report("A fact is 1 to 500 characters.", error=True)
            return
        self.report("Edited; confirm it again to let drafts use it.")
        await self.reload()

    @on(Button.Pressed, "#fact-extract")
    def _pressed_extract(self) -> None:
        self.extract()

    @work(exclusive=True, group="extract")
    async def extract(self) -> None:
        generator = self.services.generator
        if generator is None:
            self.report("No local model is configured (inference.model).", error=True)
            return
        profile = await self.services.db.profile()
        cv = await baseline_cv(self.services.db)
        if cv is None or profile.baseline_artifact_id is None:
            self.report("Store your CV first.", error=True)
            return
        self.report("Asking the local model for facts from your CV…")
        try:
            with self.cockpit.busy("extract"):
                added = await facts.extract(
                    self.services.db, generator, cv, profile.baseline_artifact_id
                )
        except AdapterError as exc:
            self.report(f"No facts proposed: {exc.code}.", error=True)
            return
        self.report(f"{len(added)} fact(s) proposed. Confirm the ones that are true.")
        await self.reload()

    # ---- CV ------------------------------------------------------------------------------
    @on(Button.Pressed, "#cv-view")
    async def view_cv(self) -> None:
        cv = await baseline_cv(self.services.db)
        await self.app.push_screen(TextModal("Your CV", cv or "No CV stored yet."))

    @on(Button.Pressed, "#cv-paste")
    def _pressed_paste_cv(self) -> None:
        self.paste_cv()

    @work(group="cv")
    async def paste_cv(self) -> None:
        text = await self.app.push_screen_wait(
            PromptModal(
                "Paste your CV as text", await baseline_cv(self.services.db) or "", multiline=True
            )
        )
        if text is not None:
            await self.store_cv(text)

    @on(Button.Pressed, "#cv-file")
    def _pressed_file_cv(self) -> None:
        self.file_cv()

    @work(group="cv")
    async def file_cv(self) -> None:
        answer = await self.app.push_screen_wait(
            PromptModal(
                "Path to a plain text CV file (.txt or .md)", placeholder="/home/you/cv.txt"
            )
        )
        if not answer:
            return
        text = await asyncio.to_thread(read_text_file, answer)
        if text is None:
            self.report("That is not a readable UTF-8 text file under 1 MB.", error=True)
            return
        await self.store_cv(text)

    async def store_cv(self, text: str) -> None:
        try:
            await save_cv(self.services.db, text)
        except ValueError:
            self.report("The CV is empty.", error=True)
            return
        self.report("CV stored. Facts you confirmed earlier stay; propose new ones if needed.")
        await self.reload()

    # ---- Telegram ------------------------------------------------------------------------
    @on(Button.Pressed, "#pair")
    async def pair(self) -> None:
        store = TelegramStore(self.services.db)
        if await store.binding() is not None:
            self.report("Already paired. Unpair first to pair another chat.", error=True)
            return
        code = await store.begin_pairing(now())
        await self.app.push_screen(
            TextModal(
                "Telegram pairing code (valid ten minutes)",
                f"Send this to your bot from a private chat:\n\n/start {code}\n\n"
                "The cockpit answers it while Telegram polling is on.",
            )
        )

    @on(Button.Pressed, "#unpair")
    def _pressed_unpair(self) -> None:
        self.unpair()

    @work(group="pairing")
    async def unpair(self) -> None:
        if await self.app.push_screen_wait(
            ConfirmModal("Forget the paired Telegram chat?", confirm_label="Unpair", danger=True)
        ):
            await TelegramStore(self.services.db).unpair()
            await self.reload()
