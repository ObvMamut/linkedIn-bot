import fcntl
import os
from pathlib import Path
from types import TracebackType

from linkedin_bot.runtime.privacy import private_directory, safe_file


class AlreadyRunning(RuntimeError):
    pass


class RuntimeLock:
    """The inode stays in place; only the kernel decides whether it is locked."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.fd: int | None = None

    def __enter__(self) -> RuntimeLock:
        private_directory(self.root)
        path = safe_file(self.root, "runtime.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise AlreadyRunning("runtime_already_locked") from exc
        self.fd = fd
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
