"""Reads khinsider's pages (never the music files) into the catalog.

* scan_site(): the platform list and every browse page (A-Z and 0-9, 500 albums a page) - about
  230 pages for 100,000+ albums, so a full scan takes minutes, not hours.
* read_albums(): the pages of the given albums - their tracks, sizes per format, images.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Callable, Optional
from urllib.robotparser import RobotFileParser

import httpx

from .core.constants import MAX_BODY, RETRY_STATUSES, THROTTLE_STATUSES
from .core.limits import Limiter, _retry_after
from .core.util import FetchError

from . import parse
from .catalog import Catalog

log = logging.getLogger("khinsider_sync")


def browse_url(base: str, letter: str, page: int) -> str:
    return f"{base}game-soundtracks/browse/{letter}?page={page}&orderby=name&orderdirection=ASC"


def album_url(base: str, slug: str) -> str:
    from urllib.parse import quote
    return f"{base}game-soundtracks/album/{quote(slug, safe='-._~')}"


class Scanner:
    def __init__(self, catalog: Catalog, base_url: str, client: httpx.AsyncClient, *,
                 workers: int = 4, delay: float = 0.25, retries: int = 2,
                 user_agent: str = "", robots: Optional[RobotFileParser] = None,
                 store: Optional[Callable[[str, dict], int]] = None):
        self.catalog = catalog
        self.base = base_url if base_url.endswith("/") else base_url + "/"
        self.client = client
        self.workers = workers
        self.delay = delay
        self.retries = retries
        self.user_agent = user_agent
        self.robots = robots
        self.store = store                # store(slug, parsed album page) -> files (engine.store_album)
        self.stopping = False
        # progress
        self.what = ""
        self.pages = 0
        self.albums = 0                   # albums seen on browse pages
        self.new = 0
        self.read = 0                     # album pages read
        self.files = 0
        self.errors = 0
        self.total = 0                    # pages (or albums) this run will read, when known
        self.started = time.monotonic()
        self.queue: Optional[asyncio.Queue] = None
        self.lim: Optional[Limiter] = None
        self.current = ""

    def progress(self) -> dict:
        el = max(time.monotonic() - self.started, 1e-6)
        done = self.pages
        return {"what": self.what, "pages": self.pages, "albums": self.albums, "new": self.new,
                "read": self.read, "files": self.files, "errors": self.errors, "total": self.total,
                "queued": self.queue.qsize() if self.queue else 0, "rate": round(done / el, 1),
                "parallel": self.lim.limit if self.lim else 0,
                "delay": round(self.lim.delay, 2) if self.lim else 0, "current": self.current}

    def stop(self):
        self.stopping = True

    async def _fetch(self, url: str):
        """-> (final url, body). Pages only: anything that isn't HTML comes back with body None."""
        if self.robots is not None and not self.robots.can_fetch(self.user_agent, url):
            raise FetchError("robots.txt disallows it")
        err = "unknown error"
        for attempt in range(self.retries + 1):
            pause = None
            await self.lim.acquire()
            t0 = time.monotonic()
            try:
                async with self.client.stream("GET", url) as r:
                    status = r.status_code
                    if status == 200:
                        ctype = r.headers.get("content-type", "").lower()
                        body = None
                        if not ctype or "html" in ctype:
                            buf = bytearray()
                            async for chunk in r.aiter_bytes():
                                buf += chunk
                                if len(buf) > MAX_BODY:
                                    break
                            body = bytes(buf)
                        self.lim.ok(time.monotonic() - t0)
                        return str(r.url), body
                    if status not in RETRY_STATUSES:
                        raise FetchError(f"HTTP {status}")
                    err = f"HTTP {status}"
                    if status in THROTTLE_STATUSES:
                        self.lim.cut(err)
                        pause = _retry_after(r.headers.get("retry-after"))
            except httpx.TimeoutException:
                err = "timed out"
                self.lim.cut("timeout")
            except httpx.TransportError as e:
                err = f"{type(e).__name__}: {e}".rstrip(": ")
            finally:
                await self.lim.release()
            if self.stopping:
                break
            if attempt < self.retries:
                wait = pause if pause is not None else min(30.0, 2.0 ** attempt) * (0.75 + random.random() / 2)
                await asyncio.sleep(wait)
        raise FetchError(err)

    async def _pool(self, handle) -> None:
        async def worker():
            while True:
                item = await self.queue.get()
                try:
                    if not self.stopping:
                        await handle(item)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # one bad page must not stop the scan
                    self.errors += 1
                    log.error("unexpected error reading %s: %r", item, e)
                finally:
                    self.queue.task_done()

        tasks = [asyncio.create_task(worker()) for _ in range(self.workers)]
        try:
            await self.queue.join()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.catalog.commit()

    def _start(self, what: str) -> None:
        self.what = what
        self.pages = self.albums = self.new = self.read = self.files = self.errors = self.total = 0
        self.started = time.monotonic()
        self.lim = Limiter(self.workers, self.delay, True, 6.0)
        self.queue = asyncio.Queue()

    # ------------------------------------------------------------------ the whole site
    async def scan_site(self, name_of: Callable[[str, str], str]) -> dict:
        """Read the platform list and every browse page. Albums the site no longer lists are
        forgotten only when every page was read."""
        self._start("site")
        scan_id = self.catalog.start_scan("full")
        log.info("Reading khinsider's album lists")
        try:
            _, body = await self._fetch(self.base + "console-list")
            plats = parse.parse_platforms(body or b"")
            self.catalog.set_platforms(plats)
            self.pages += 1
        except FetchError as e:
            self.errors += 1
            log.warning("could not read the platform list (%s)", e)
        letters_found: dict = {}

        async def handle(item):
            letter, page = item
            self.current = f"{letter}, page {page}"
            url = browse_url(self.base, letter, page)
            try:
                _, body = await self._fetch(url)
            except FetchError as e:
                self.errors += 1
                log.warning("could not read %s (%s)", self.current, e)
                return
            albums, pages, found = parse.parse_browse(body or b"")
            self.pages += 1
            self.albums += len(albums)
            self.new += self.catalog.add_albums(albums, letter, scan_id)
            if page == 1:
                letters_found[letter] = found
                self.total += pages - 1
                for n in range(2, pages + 1):
                    self.queue.put_nowait((letter, n))
            if self.pages % 20 == 0:
                self.catalog.commit()

        self.total = len(parse.LETTERS) + 1
        for letter in parse.LETTERS:
            self.queue.put_nowait((letter, 1))
        await self._pool(handle)
        status = "stopped" if self.stopping else "done"
        gone: list = []
        if status == "done" and not self.errors and self.albums:
            gone = self.catalog.prune(scan_id)
        elif status == "done":
            status = "incomplete"
        named = self.catalog.assign_folders(name_of)
        self.catalog.finish_scan(scan_id, self.pages, self.albums, status)
        expected = sum(v for v in letters_found.values() if v)
        log.info("Album list %s: %d pages, %d albums (%d new)%s%s%s", status, self.pages, self.albums, self.new,
                 f", {self.errors} pages failed - nothing was removed from the catalog" if self.errors else "",
                 f", {len(gone)} no longer on the site" if gone else "",
                 f" (the site says {expected})" if expected and abs(expected - self.albums) > 0 else "")
        return {"pages": self.pages, "albums": self.albums, "new": self.new, "errors": self.errors,
                "removed": len(gone), "status": status, "named": named}

    # ------------------------------------------------------------------ album pages
    async def read_albums(self, slugs: list, what: str = "albums") -> dict:
        """Read these albums' pages into the catalog (tracks, sizes, images)."""
        self._start(what)
        self.total = len(slugs)
        renamed: dict = {}
        missing: list = []

        async def handle(slug):
            self.current = slug
            try:
                final, body = await self._fetch(album_url(self.base, slug))
            except FetchError as e:
                self.errors += 1
                self.catalog.album_failed(slug, str(e))
                log.warning("could not read album %s (%s)", slug, e)
                return
            page = parse.parse_album(body or b"", final)
            self.pages += 1
            new_slug = parse.album_slug(final) or slug
            if new_slug != slug:
                self.catalog.rename_album(slug, new_slug)
                renamed[slug] = new_slug
                log.info("The site moved album %s to %s - its tick and folder follow", slug, new_slug)
            if page["missing"]:
                missing.append(new_slug)
                self.catalog.album_failed(new_slug, "the site has no such album any more", missing=True)
                log.warning("album %s is no longer on the site", new_slug)
                return
            self.files += self.store(new_slug, page) if self.store else 0
            self.read += 1
            if self.read % 20 == 0:
                self.catalog.commit()

        for s in slugs:
            self.queue.put_nowait(s)
        await self._pool(handle)
        if self.read or self.errors:
            log.info("Read %d album pages (%d files)%s%s", self.read, self.files,
                     f", {self.errors} failed" if self.errors else "",
                     f", {len(missing)} gone from the site" if missing else "")
        return {"read": self.read, "files": self.files, "errors": self.errors, "renamed": renamed,
                "missing": missing, "status": "stopped" if self.stopping else "done"}
