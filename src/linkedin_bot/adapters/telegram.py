"""Telegram Bot API over long polling. The token comes from the environment only."""

import os
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from linkedin_bot.runtime.privacy import BoundaryError

API = "https://api.telegram.org"
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


class Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Sender(Wire):
    id: int


class Chat(Wire):
    id: int
    type: str


class Message(Wire):
    sender: Sender = Field(alias="from")
    chat: Chat
    text: str | None = None


class Update(Wire):
    update_id: int
    message: Message | None = None


class TelegramAPI(Protocol):
    async def send_message(self, chat_id: int, text: str) -> None: ...


class TelegramPoller(TelegramAPI, Protocol):
    async def get_updates(self, offset: int, poll_seconds: int = 30) -> list[Update]: ...


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
    def __init__(self, token: str | None = None) -> None:
        token = token if token is not None else os.environ.get(TOKEN_ENV)
        if not token:
            raise BoundaryError("telegram_token_missing")
        self._base = f"{API}/bot{token}"
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(70.0, connect=10.0))

    async def get_updates(self, offset: int, poll_seconds: int = 30) -> list[Update]:
        response = await self._client.post(
            f"{self._base}/getUpdates",
            json={"offset": offset, "timeout": poll_seconds, "allowed_updates": ["message"]},
        )
        if response.status_code != 200:
            raise BoundaryError("telegram_http_status")
        return parse_updates(response.json())

    async def send_message(self, chat_id: int, text: str) -> None:
        response = await self._client.post(
            f"{self._base}/sendMessage", json={"chat_id": chat_id, "text": text[:4000]}
        )
        if response.status_code != 200:
            raise BoundaryError("telegram_http_status")

    async def close(self) -> None:
        await self._client.aclose()
