"""Inline keyboards: small value types and conversion to Telegram's markup."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Button:
    text: str
    data: str | None = None  # callback data, at most 64 bytes
    url: str | None = None  # opens a link instead of calling back


Keyboard = list[list[Button]]


@dataclass(frozen=True)
class Screen:
    text: str
    keyboard: Keyboard = field(default_factory=list)


def to_markup(keyboard: Keyboard) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    for row in keyboard:
        out: list[dict[str, str]] = []
        for button in row:
            if button.url is not None:
                out.append({"text": button.text, "url": button.url})
            elif button.data is not None and len(button.data.encode()) <= 64:
                out.append({"text": button.text, "callback_data": button.data})
            else:
                raise ValueError("callback_data_too_long_or_missing")
        rows.append(out)
    return {"inline_keyboard": rows}
