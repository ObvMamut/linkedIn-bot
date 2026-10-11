"""While the cockpit runs, log events go to the Logs view and a private rotating file.

The same redacting formatter as everywhere else is used, so only event names and counts are
ever written; stderr is not touched (it would corrupt the screen).
"""

import json
import logging
import logging.handlers
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from linkedin_bot.runtime.privacy import RedactedFormatter, private_directory, safe_file

MEMORY = 2000


@dataclass(frozen=True)
class Event:
    at: datetime
    level: str
    event: str
    count: int | float | None = None
    duration_ms: int | float | None = None


class EventBuffer(logging.Handler):
    """Keeps the most recent events in memory; safe to call from any thread."""

    def __init__(self, size: int = MEMORY) -> None:
        super().__init__(level=logging.INFO)
        self.setFormatter(RedactedFormatter())
        self.events: deque[Event] = deque(maxlen=size)
        self.serial = 0  # grows with every event, so readers can tell what is new
        self.warnings = 0
        self.guard = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            data = json.loads(self.format(record))
        except ValueError:
            return
        with self.guard:
            self.events.append(
                Event(
                    at=datetime.fromtimestamp(record.created).astimezone(),
                    level=str(data.get("level", "INFO")),
                    event=str(data.get("event", "event")),
                    count=data.get("count"),
                    duration_ms=data.get("duration_ms"),
                )
            )
            self.serial += 1
            if record.levelno >= logging.WARNING:
                self.warnings += 1

    def since(self, serial: int) -> tuple[int, list[Event]]:
        """Events added after `serial` (at most the buffer size) and the new serial."""
        with self.guard:
            fresh = min(self.serial - serial, len(self.events))
            return self.serial, list(self.events)[len(self.events) - fresh :] if fresh else []


def file_handler(root: Path) -> logging.Handler:
    directory = safe_file(root, "logs")
    private_directory(directory)
    handler = logging.handlers.RotatingFileHandler(
        safe_file(root, "logs/events.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(RedactedFormatter())
    handler.setLevel(logging.INFO)
    return handler


@contextmanager
def redirected(root: Path, buffer: EventBuffer) -> Iterator[None]:
    """Swap the root handlers for the buffer and the file; restore them afterwards."""
    logger = logging.getLogger()
    previous = list(logger.handlers)
    target = file_handler(root)
    for handler in previous:
        logger.removeHandler(handler)
    logger.addHandler(buffer)
    logger.addHandler(target)
    try:
        yield
    finally:
        logger.removeHandler(buffer)
        logger.removeHandler(target)
        target.close()
        for handler in previous:
            logger.addHandler(handler)
