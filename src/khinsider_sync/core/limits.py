"""Adaptive request pacing, bandwidth cap and HTTP header helpers."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

log = logging.getLogger("dircrawl")


class Limiter:
    """Concurrency + pacing for listing pages, with additive-increase / multiplicative-decrease."""

    def __init__(self, max_conc: int, delay: float, adaptive: bool, slow_secs: float):
        self.max = max(1, max_conc)
        self.limit = self.max
        self.base_delay = self.delay = max(0.0, delay)
        self.adaptive = adaptive
        self.slow_secs = slow_secs
        self.inflight = 0
        self._cond = asyncio.Condition()
        self._next_at = 0.0
        self._ok = 0
        self._last_cut = -1e9

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._cond:
            await self._cond.wait_for(lambda: self.inflight < self.limit)
            self.inflight += 1
            now = loop.time()
            start = max(now, self._next_at)
            self._next_at = start + self.delay
        if start > now:
            try:
                await asyncio.sleep(start - now)
            except asyncio.CancelledError:
                await self.release()
                raise

    async def release(self) -> None:
        async with self._cond:
            self.inflight -= 1
            self._cond.notify_all()

    async def _wake(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    def ok(self, latency: float) -> None:
        if not self.adaptive:
            return
        if latency > self.slow_secs:
            self.cut(f"{latency:.1f}s response")
            return
        self._ok += 1
        if self._ok < max(10, self.limit * 2):
            return
        self._ok = 0
        if self.delay > self.base_delay:
            self.delay = max(self.base_delay, self.delay * 0.6)
            if self.delay < self.base_delay * 1.1:
                self.delay = self.base_delay
            log.info("server healthy again - delay now %.2fs", self.delay)
        elif self.limit < self.max:
            self.limit += 1
            log.info("server healthy again - parallel requests now %d", self.limit)
            asyncio.ensure_future(self._wake())

    def cut(self, why: str) -> None:
        if not self.adaptive:
            return
        now = time.monotonic()
        if now - self._last_cut < 3:          # react at most once per 3 s
            return
        self._last_cut, self._ok = now, 0
        old_l, old_d = self.limit, self.delay
        self.limit = max(1, self.limit // 2)
        self.delay = min(max(self.delay * 2, 0.25), 10.0)
        log.warning("server struggling (%s) - easing off: parallel %d -> %d, delay %.2fs -> %.2fs",
                    why, old_l, self.limit, old_d, self.delay)


class RateLimit:
    """Shared download bandwidth cap (token bucket)."""

    def __init__(self, rate: int):
        self.rate = rate
        self.tokens = float(rate)
        self.t = time.monotonic()
        self.lock = asyncio.Lock()

    async def consume(self, n: int) -> None:
        if self.rate <= 0:
            return
        async with self.lock:
            now = time.monotonic()
            self.tokens = min(self.rate, self.tokens + (now - self.t) * self.rate)
            self.t = now
            self.tokens -= n
            if self.tokens < 0:
                await asyncio.sleep(-self.tokens / self.rate)


def _retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return min(max(float(value), 0.5), 60.0)
    except ValueError:
        return None


def _total_from_headers(headers, status: int, have: int) -> Optional[int]:
    if status == 206:
        m = re.search(r"/(\d+)\s*$", headers.get("content-range", ""))
        if m:
            return int(m.group(1))
    cl = headers.get("content-length")
    if cl and cl.isdigit():
        return int(cl) + (have if status == 206 else 0)
    return None


class DownloadGate:
    """How many files download at once - automatically as many as the server allows.

    auto mode:
      * starts at the last known server maximum (or 2) and adds one parallel download every
        `ramp_secs` while every slot is busy and more files are waiting;
      * when the server pushes back (429/503, refused connections, a web page instead of the
        file) it records "the server allows N" (one less than what was running), drops to N and
        stays there - repeated push-back at that level halves it (rate-based limits);
      * every `probe_secs` it tries N+1 again. If that runs cleanly for `confirm_secs` the
        ceiling is lifted and ramping resumes until the next push-back.
    fixed mode: exactly `limit` downloads, never changed.
    """

    def __init__(self, *, auto: bool, start: int, hard_max: int, known_max: Optional[int] = None,
                 ramp_secs: float = 3.0, probe_secs: float = 120.0, confirm_secs: float = 30.0,
                 cooldown_secs: float = 15.0):
        self.auto = auto
        self.hard_max = max(1, hard_max)
        self.ceiling = min(known_max, self.hard_max) if (auto and known_max) else None
        self.limit = (min(self.hard_max, self.ceiling or max(1, start)) if auto
                      else max(1, min(start, self.hard_max)))
        self.ramp_secs, self.probe_secs = ramp_secs, probe_secs
        self.confirm_secs = confirm_secs
        self.cooldown_secs = min(cooldown_secs, probe_secs)
        self.inflight = 0
        self.peak = 0                        # most downloads seen running at once
        self.best = self.ceiling             # highest level known to run cleanly (remembered for next run)
        self._strike = -1e9
        self.probing_since: Optional[float] = None
        now = time.monotonic()
        self._last_change = now
        self._last_throttle = -1e9
        self._last_probe = now
        self._cond = asyncio.Condition()

    # -- slots --
    async def acquire(self) -> None:
        async with self._cond:
            await self._cond.wait_for(lambda: self.inflight < self.limit)
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)

    async def release(self) -> None:
        async with self._cond:
            self.inflight -= 1
            self._cond.notify_all()

    async def _set(self, new: int, why: str, level=logging.INFO) -> None:
        new = max(1, min(self.hard_max, new))
        if new != self.limit:
            log.log(level, "parallel downloads %d -> %d (%s)", self.limit, new, why)
            self.limit = new
            self._last_change = time.monotonic()
            async with self._cond:
                self._cond.notify_all()

    def describe(self) -> str:
        if not self.auto:
            return "fixed"
        if self.probing_since is not None:
            return f"testing {self.limit}"
        if self.ceiling is not None:
            return f"server max {self.ceiling}"
        return "ramping up" if self.limit < self.hard_max else "at your upper limit"

    # -- feedback --
    async def tick(self, backlog: int) -> None:
        """Called about once a second by the crawler."""
        if not self.auto:
            return
        now = time.monotonic()
        busy = self.inflight >= self.limit and backlog > 0
        if self.inflight >= self.limit and self.inflight > 0:
            if self.best is None or self.limit > self.best:
                if now - self._last_throttle > self.confirm_secs and now - self._last_change >= self.confirm_secs:
                    self.best = self.limit
        if self.probing_since is not None:
            if now - self.probing_since >= self.confirm_secs:
                log.info("server accepted %d parallel downloads - looking for its new limit", self.limit)
                self.best = max(self.best or 0, self.limit)
                self.ceiling = None
                self.probing_since = None
            return
        if now - self._last_throttle < self.cooldown_secs:
            return
        if self.ceiling is None:
            if busy and self.limit < self.hard_max and now - self._last_change >= self.ramp_secs:
                await self._set(self.limit + 1, "ramping up")
        elif self.limit < self.ceiling:
            if busy and now - self._last_change >= self.ramp_secs:
                await self._set(self.limit + 1, "recovering")
        elif busy and self.ceiling < self.hard_max and now - self._last_probe >= self.probe_secs:
            self._last_probe = now
            self.probing_since = now
            await self._set(self.ceiling + 1, "checking whether the server now allows more")

    async def throttled(self, why: str) -> None:
        """The server pushed back on a download."""
        if not self.auto:
            return
        now = time.monotonic()
        if now - self._last_throttle < 3:               # one reaction per burst
            return
        if (self.probing_since is None and self.best and self.limit <= self.best
                and now - self._strike > 10):
            self._strike = now                          # a level that worked before: needs a 2nd strike
            log.debug("one push-back at a proven level (%d) - ignoring unless it repeats: %s",
                      self.limit, why)
            return
        repeated = now - self._last_throttle < 60
        self._last_throttle = now
        self._last_probe = now
        if self.probing_since is not None:
            self.probing_since = None
            new = self.ceiling or max(1, self.limit - 1)
            await self._set(new, f"server still allows only {new}: {why}", logging.WARNING)
            return
        if repeated and self.ceiling is not None and self.limit <= self.ceiling:
            new = max(1, self.limit // 2)                # not a concurrency limit - back off harder
            self.ceiling = new
            await self._set(new, f"server keeps pushing back: {why}", logging.WARNING)
            return
        running = max(1, min(self.limit, self.inflight))
        self.ceiling = max(1, running - 1)
        await self._set(self.ceiling, f"server max looks like {self.ceiling}: {why}", logging.WARNING)

    def remembered_max(self) -> Optional[int]:
        """What to start from next time."""
        if not self.auto:
            return None
        return self.ceiling or self.best
