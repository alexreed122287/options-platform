"""In-memory TTL cache and provider rate-budget tracking.

The cache deliberately keeps expired entries around: when a provider call
fails, the last known value is served with stale=True so the app degrades
gracefully instead of failing silently or crashing.
"""
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

log = logging.getLogger("data.cache")


@dataclass
class Fetched:
    """A provider result plus freshness metadata, surfaced all the way to the UI."""
    data: Any
    stale: bool
    as_of: float
    error: Optional[str] = None

    @property
    def as_of_iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.as_of))


class _Entry:
    __slots__ = ("value", "stored_at", "expires_at")

    def __init__(self, value: Any, stored_at: float, expires_at: float):
        self.value = value
        self.stored_at = stored_at
        self.expires_at = expires_at


class TTLCache:
    def __init__(self) -> None:
        self._entries: Dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.stale_serves = 0

    def _get_entry(self, key: str) -> Optional[_Entry]:
        with self._lock:
            return self._entries.get(key)

    def set(self, key: str, value: Any, ttl: float) -> None:
        now = time.time()
        with self._lock:
            self._entries[key] = _Entry(value, now, now + ttl)

    def peek(self, key: str) -> Optional[Any]:
        """Return the cached value regardless of freshness, else None."""
        entry = self._get_entry(key)
        return entry.value if entry else None

    async def get_or_fetch(
        self, key: str, ttl: float, fetch: Callable[[], Awaitable[Any]]
    ) -> Fetched:
        """Return fresh cached data, else fetch. On fetch failure, fall back to
        the last known value (stale=True) when one exists; otherwise re-raise."""
        now = time.time()
        entry = self._get_entry(key)
        if entry is not None and entry.expires_at > now:
            self.hits += 1
            return Fetched(entry.value, stale=False, as_of=entry.stored_at)
        self.misses += 1
        try:
            value = await fetch()
        except Exception as exc:
            if entry is not None:
                self.stale_serves += 1
                log.warning("serving stale data for %s after fetch error: %s", key, exc)
                return Fetched(entry.value, stale=True, as_of=entry.stored_at, error=str(exc))
            raise
        self.set(key, value, ttl)
        return Fetched(value, stale=False, as_of=time.time())

    def stats(self) -> Dict[str, int]:
        with self._lock:
            size = len(self._entries)
        return {
            "entries": size,
            "hits": self.hits,
            "misses": self.misses,
            "stale_serves": self.stale_serves,
        }


class RateBudget:
    """Rolling-window request counter per provider.

    Logs the remaining budget so rate-limit pressure is visible, and warns
    loudly when under 10% of either window remains.
    """

    def __init__(self, name: str, per_minute: int, per_day: int, log_every: int = 20):
        self.name = name
        self.per_minute = per_minute
        self.per_day = per_day
        self.log_every = log_every
        self._minute: deque = deque()
        self._day: deque = deque()
        self._total = 0
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        while self._minute and now - self._minute[0] > 60:
            self._minute.popleft()
        while self._day and now - self._day[0] > 86400:
            self._day.popleft()

    def record(self) -> None:
        now = time.time()
        with self._lock:
            self._trim(now)
            self._minute.append(now)
            self._day.append(now)
            self._total += 1
            rem_minute = self.per_minute - len(self._minute)
            rem_day = self.per_day - len(self._day)
            total = self._total
        if rem_minute <= self.per_minute * 0.1 or rem_day <= self.per_day * 0.1:
            log.warning(
                "%s rate budget LOW: %d/%d per-minute remaining, %d/%d per-day remaining",
                self.name, rem_minute, self.per_minute, rem_day, self.per_day,
            )
        elif total % self.log_every == 0:
            log.info(
                "%s rate budget: %d/%d per-minute remaining, %d/%d per-day remaining",
                self.name, rem_minute, self.per_minute, rem_day, self.per_day,
            )
        else:
            log.debug(
                "%s rate budget: %d/min remaining, %d/day remaining",
                self.name, rem_minute, rem_day,
            )

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            self._trim(now)
            used_minute = len(self._minute)
            used_day = len(self._day)
        return {
            "name": self.name,
            "used_minute": used_minute,
            "remaining_minute": self.per_minute - used_minute,
            "limit_minute": self.per_minute,
            "used_day": used_day,
            "remaining_day": self.per_day - used_day,
            "limit_day": self.per_day,
        }
