"""Telegram Bot API over long polling. The token comes from the environment only."""

import os
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from linkedin_bot.runtime.privacy import BoundaryError

API = "https://api.telegram.org"
TOKEN_ENV = "LINKEDIN_AUTOMATION_BOT_TOKEN"


class Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Sender(Wire):
    id: int


class Chat(Wire):
    id: int
    type: str


class Message(Wire):
    message_id: int = 0
    sender: Sender = Field(alias="from")
    chat: Chat
    text: str | None = None


class CallbackMessage(Wire):
    message_id: int
    chat: Chat


class CallbackQuery(Wire):
    id: str
    sender: Sender = Field(alias="from")
    data: str | None = None
    message: CallbackMessage | None = None


class Update(Wire):
    update_id: int
    message: Message | None = None
    callback_query: CallbackQuery | None = None


Markup = dict[str, Any]


class TelegramAPI(Protocol):
    async def send_message(self, chat_id: int, text: str, markup: Markup | None = None) -> int: ...

    async def edit_message(
        self, chat_id: int, message_id: int, text: str, markup: Markup | None = None
    ) -> None: ...

    async def answer_callback(self, callback_id: str, text: str = "") -> None: ...

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None: ...


class TelegramPoller(TelegramAPI, Protocol):
    async def get_updates(self, offset: int, poll_seconds: int = 30) -> list[Update]: ...


MESSAGE_LIMIT = 4000  # Telegram allows 4096 characters per message


def chunks(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split on line boundaries so long replies arrive whole instead of cut off."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single overlong line is cut hard
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def parse_updates(payload: Any) -> list[Update]:
    """Keep every update ID so offsets advance; unusable messages become `message=None`."""
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise BoundaryError("telegram_response")
    result = payload.get("result")
    if not isinstance(result, list):
        raise BoundaryError("telegram_response")
    updates = []
    for raw in result:
        if not isinstance(raw, dict) or type(raw.get("update_id")) is not int:
            raise BoundaryError("telegram_response")
        try:
            updates.append(Update.model_validate(raw))
        except ValidationError:
            updates.append(Update(update_id=raw["update_id"]))
    return updates


class HttpTelegram:
    def __init__(
        self, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        token = token if token is not None else os.environ.get(TOKEN_ENV)
        if not token:
            raise BoundaryError("telegram_token_missing")
        self._base = f"{API}/bot{token}"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(70.0, connect=10.0), transport=transport
        )

    async def _call(self, method: str, body: dict[str, Any]) -> httpx.Response:
        return await self._client.post(f"{self._base}/{method}", json=body)

    async def get_updates(self, offset: int, poll_seconds: int = 30) -> list[Update]:
        response = await self._call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": poll_seconds,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        if response.status_code != 200:
            raise BoundaryError("telegram_http_status")
        return parse_updates(response.json())

    async def send_message(self, chat_id: int, text: str, markup: Markup | None = None) -> int:
        """Long text goes out as several messages; buttons ride on the last one."""
        parts = chunks(text)
        message_id = 0
        for index, part in enumerate(parts):
            body: dict[str, Any] = {"chat_id": chat_id, "text": part}
            if markup is not None and index == len(parts) - 1:
                body["reply_markup"] = markup
            response = await self._call("sendMessage", body)
            if response.status_code != 200:
                raise BoundaryError("telegram_http_status")
            message_id = int(response.json()["result"]["message_id"])
        return message_id

    async def edit_message(
        self, chat_id: int, message_id: int, text: str, markup: Markup | None = None
    ) -> None:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:MESSAGE_LIMIT],
        }
        if markup is not None:
            body["reply_markup"] = markup
        response = await self._call("editMessageText", body)
        if response.status_code == 200:
            return
        if "message is not modified" in response.text:
            return  # the screen is already showing exactly this
        raise BoundaryError("telegram_edit_failed")

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        await self._call("sendChatAction", {"chat_id": chat_id, "action": action})

    async def close(self) -> None:
        await self._client.aclose()
