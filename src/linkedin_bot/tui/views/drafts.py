"""Drafts and documents: cover letters and CVs stored as checked artifacts."""

import asyncio
import os
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Static

from linkedin_bot.domain.models import Artifact
from linkedin_bot.runtime.privacy import BoundaryError
from linkedin_bot.tui.views.base import View
from linkedin_bot.tui.widgets import PromptModal, fill_table, selected_key


def write_new_file(answer: str, text: str) -> str | None:
    """Create a new private file (never overwrite, never follow a link). None if refused."""
    path = Path(answer).expanduser()
    if not path.is_absolute() or path.exists() or not path.parent.is_dir():
        return None
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(text)
    return str(path)


KIND_LABELS = {
    "baseline_cv": "CV",
    "tailored_cv": "Tailored CV",
    "cover_letter": "Cover letter",
    "message_draft": "Message draft",
}


class Drafts(View):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.artifacts: tuple[Artifact, ...] = ()
        self.text = ""

    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Button("Copy", id="copy")
            yield Button("Export to file", id="export")
            yield Static("Drafts are for your review; nothing here is ever sent.", classes="hint")
        with Horizontal(classes="split"):
            table: DataTable[Text] = DataTable(
                id="artifacts", cursor_type="row", zebra_stripes=True
            )
            table.add_columns("Kind", "For", "Generator")
            yield table
            with VerticalScroll(classes="detail"):
                yield Static("", id="artifact-text", markup=False, classes="document")

    async def reload(self) -> None:
        self.artifacts = await self.services.db.artifacts()
        fill_table(
            self.query_one("#artifacts", DataTable),
            (
                (
                    a.id,
                    (KIND_LABELS.get(a.kind, a.kind), a.opportunity_id or "", a.generator_version),
                )
                for a in self.artifacts
            ),
        )
        await self.show()

    @on(DataTable.RowHighlighted, "#artifacts")
    async def show(self) -> None:
        key = selected_key(self.query_one("#artifacts", DataTable))
        artifact = next((a for a in self.artifacts if a.id == key), None)
        if artifact is None:
            self.text = ""
            self.query_one("#artifact-text", Static).update(
                "No drafts yet. With a local model configured, press w on a posting in Jobs (2)."
            )
            return
        try:
            self.text = await self.services.db.artifact_text(artifact)
        except BoundaryError, OSError, UnicodeDecodeError:
            self.text = ""
            self.query_one("#artifact-text", Static).update(
                "This file no longer matches its checksum; run doctor."
            )
            return
        self.query_one("#artifact-text", Static).update(self.text)

    @on(Button.Pressed, "#copy")
    def copy(self) -> None:
        if self.text:
            self.app.copy_to_clipboard(self.text)
            self.report("Copied to the clipboard.")

    @on(Button.Pressed, "#export")
    def export_pressed(self) -> None:
        self.export()

    @work(group="export")
    async def export(self) -> None:
        if not self.text:
            return
        answer = await self.app.push_screen_wait(
            PromptModal(
                "Export to which new file? (absolute path)", placeholder="/home/you/letter.txt"
            )
        )
        if not answer:
            return
        written = await asyncio.to_thread(write_new_file, answer, self.text)
        if written is None:
            self.report("Give an absolute path to a new file in an existing folder.", error=True)
            return
        self.report(f"Exported to {written}.")
