"""Small in-memory TTL cache. Nothing here is written to disk."""

import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any


class TTLCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic, max_entries: int = 64) -> None:
        self.clock = clock
        self.max_entries = max_entries
        self.entries: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()

    def get(self, key: Hashable) -> tuple[bool, Any]:
        found = self.entries.get(key)
        if found is None or found[0] <= self.clock():
            self.entries.pop(key, None)
            return False, None
        return True, found[1]

    def set(self, key: Hashable, value: Any, ttl: float) -> None:
        self.entries[key] = (self.clock() + ttl, value)
        self.entries.move_to_end(key)
        while len(self.entries) > self.max_entries:
            self.entries.popitem(last=False)

    def clear(self) -> None:
        self.entries.clear()
