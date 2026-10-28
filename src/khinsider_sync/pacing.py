"""Going easy on khinsider: limits the user sets so a big queue is spread over hours or days.

  pages_per_minute   khinsider page requests (browse lists, album pages, track pages - the part
                     Cloudflare guards; a track page per file not taken from the album page's player) - PageRate,
                     shared by everything a job does
  files_per_hour     files downloaded in any 60 minutes      } a sync stops when one is reached and
  albums_per_day     albums started in any 24 hours          } the app carries on by itself when the
                                                               window has room again (queue_resume_at)
0 = no limit. What was downloaded is kept in the catalog's `quota` table (time, kind).
"""
from __future__ import annotations

import asyncio
import time
from collections import deque


class PageRate:
    """At most `per_minute` page requests a minute (evenly spaced); 0 = no limit."""

    def __init__(self, per_minute: float = 0):
        self.interval = 60.0 / per_minute if per_minute and per_minute > 0 else 0.0
        self._next = 0.0
        self._lock = None

    def set(self, per_minute: float) -> None:
        """A new rate for a job under way (Settings changed): the next requests use it - no waiting
        on slots booked at the old, slower pace beyond one new interval."""
        self.interval = 60.0 / per_minute if per_minute and per_minute > 0 else 0.0
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            return
        self._next = min(self._next, now + self.interval)

    async def wait(self) -> None:
        if not self.interval:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            await asyncio.sleep(start - now)


class Window:
    """A rolling window (e.g. files in the last hour): times of what was started in it."""

    def __init__(self, limit: int, seconds: float, earlier=()):
        self.limit = int(limit or 0)
        self.seconds = seconds
        self.times = deque(sorted(earlier))

    def _trim(self, now: float) -> None:
        while self.times and self.times[0] <= now - self.seconds:
            self.times.popleft()

    def take(self, now: float = None) -> bool:
        """Room for one more? Then it counts."""
        if not self.limit:
            return True
        now = time.time() if now is None else now
        self._trim(now)
        if len(self.times) >= self.limit:
            return False
        self.times.append(now)
        return True

    def room(self, now: float = None) -> int:
        if not self.limit:
            return 1 << 30
        now = time.time() if now is None else now
        self._trim(now)
        return max(0, self.limit - len(self.times))

    def free_at(self, now: float = None) -> float:
        """When the next one fits."""
        now = time.time() if now is None else now
        self._trim(now)
        if not self.limit or len(self.times) < self.limit:
            return now
        return self.times[0] + self.seconds
