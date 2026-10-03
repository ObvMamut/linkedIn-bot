"""Private paths and structured logs that never format arbitrary diagnostic text."""

import json
import logging
import os
from pathlib import Path
from typing import Any


class BoundaryError(ValueError):
    pass


def private_directory(path: Path) -> None:
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise BoundaryError("symlink_directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise BoundaryError("directory_owner")
    path.chmod(0o700)


def safe_file(root: Path, relative: str) -> Path:
    candidate = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise BoundaryError("path_escape")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise BoundaryError("path_escape")
    for path in (candidate, *candidate.parents):
        if path.is_symlink():
            raise BoundaryError("symlink_file")
    return candidate


class RedactedFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Never emit msg, args, exception text, URLs, or arbitrary extra fields.
        event = getattr(record, "event", "event")
        if not isinstance(event, str) or not event.replace("_", "").isalnum():
            event = "event"
        result: dict[str, Any] = {"level": record.levelname, "event": event}
        for key in ("count", "duration_ms"):
            value = getattr(record, key, None)
            if type(value) in (int, float):
                result[key] = value
        return json.dumps(result)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactedFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
