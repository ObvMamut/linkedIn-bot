"""Reusable pieces: modals that return a value, and text that is never read as markup.

Anything that comes from LinkedIn, a job board or the user is shown through `plain()` or a
widget created with `markup=False`, so square brackets in a job title cannot restyle the screen.
"""

from collections.abc import Iterable, Sequence

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static, TextArea


def plain(value: object) -> Text:
    return Text("" if value is None else str(value))


def clip(text: str, size: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= size else text[: size - 1] + "…"


def fill_table(
    table: DataTable[Text],
    rows: Iterable[tuple[str, Sequence[object]]],
    *,
    keep_cursor: bool = True,
) -> None:
    """Replace every row, keeping the cursor on the same row key when it still exists."""
    selected = selected_key(table) if keep_cursor else None
    table.clear()
    position = 0
    for index, (key, cells) in enumerate(rows):
        table.add_row(*(plain(cell) for cell in cells), key=key)
        if key == selected:
            position = index
    if table.row_count:
        table.move_cursor(row=position)


def selected_key(table: DataTable[Text]) -> str | None:
    if not table.row_count:
        return None
    try:
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
    except Exception:
        return None
    return None if row_key.value is None else str(row_key.value)


class ConfirmModal(ModalScreen[bool]):
    """A yes/no question; Enter confirms, Escape cancels."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("y", "confirm", "Yes")]

    def __init__(self, question: str, *, confirm_label: str = "Confirm", danger: bool = False):
        super().__init__()
        self.question, self.confirm_label, self.danger = question, confirm_label, danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Static(self.question, markup=False, classes="modal-text")
            with Horizontal(classes="modal-buttons"):
                yield Button(
                    self.confirm_label,
                    variant="error" if self.danger else "primary",
                    id="confirm",
                )
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#confirm", Button).focus()

    @on(Button.Pressed, "#confirm")
    def action_confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)


class PromptModal(ModalScreen[str | None]):
    """Ask for one line (or a block of text with `multiline=True`). None means cancelled."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "save", "Save")]

    def __init__(
        self,
        question: str,
        value: str = "",
        *,
        multiline: bool = False,
        placeholder: str = "",
        password: bool = False,
    ) -> None:
        super().__init__()
        self.question, self.value, self.multiline = question, value, multiline
        self.placeholder, self.password = placeholder, password

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal wide" if self.multiline else "modal"):
            yield Static(self.question, markup=False, classes="modal-text")
            if self.multiline:
                yield TextArea(self.value, id="answer", soft_wrap=True)
                yield Static("Ctrl+S saves, Escape cancels.", classes="hint")
            else:
                yield Input(
                    self.value, placeholder=self.placeholder, id="answer", password=self.password
                )
            with Horizontal(classes="modal-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#answer").focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        if self.multiline:
            self.dismiss(self.query_one("#answer", TextArea).text)
        else:
            self.dismiss(self.query_one("#answer", Input).value)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ChoiceModal(ModalScreen[str | None]):
    """Pick one option, with an optional free text note (returned as "value|note")."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self, question: str, options: Sequence[tuple[str, str]], *, note: str | None = None
    ) -> None:
        super().__init__()
        self.question, self.options, self.note = question, options, note

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Static(self.question, markup=False, classes="modal-text")
            yield Select(list(self.options), allow_blank=False, id="choice")
            if self.note is not None:
                yield Input(placeholder=self.note, id="note")
            with Horizontal(classes="modal-buttons"):
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#ok")
    def ok(self) -> None:
        value = str(self.query_one("#choice", Select).value)
        if self.note is not None:
            value = f"{value}|{self.query_one('#note', Input).value}"
        self.dismiss(value)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class TextModal(ModalScreen[None]):
    """Read-only text (a digest, a draft, a report) that you can scroll and copy."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close", show=False),
        Binding("c", "copy", "Copy"),
    ]

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self.title_text, self.text = title, text

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal wide tall"):
            yield Label(self.title_text, markup=False, classes="modal-title")
            with VerticalScroll():
                yield Static(self.text, markup=False, classes="document")
            with Horizontal(classes="modal-buttons"):
                yield Button("Copy", id="copy")
                yield Button("Close", variant="primary", id="close")

    @on(Button.Pressed, "#copy")
    def action_copy(self) -> None:
        self.app.copy_to_clipboard(self.text)
        self.app.notify("Copied to the clipboard.")

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        self.dismiss(None)


class HelpModal(TextModal):
    def __init__(self, text: str) -> None:
        super().__init__("Keys and views", text)
