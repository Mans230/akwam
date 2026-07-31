"""كاش مؤقت في الذاكرة مع TTL (حسب SPEC 3.5)."""
from __future__ import annotations

import time
from typing import Any


class TTLCache:
    def __init__(self, ttl_seconds: int, max_size: int = 1000) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self._data: dict[str, tuple[Any, float]] = {}

    def get(self, key: str) -> Any:
        """None عند الانتهاء/الغياب."""
        item = self._data.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        if len(self._data) >= self.max_size and key not in self._data:
            self._evict()
        self._data[key] = (value, time.monotonic() + self.ttl_seconds)

    def _evict(self) -> None:
        """يمسح المنتهي أولاً، ولو مفيش يمسح الأقدم."""
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._data.items() if exp < now]
        for k in expired:
            self._data.pop(k, None)
        if len(self._data) >= self.max_size:
            oldest = min(self._data, key=lambda k: self._data[k][1])
            self._data.pop(oldest, None)
