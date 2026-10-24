"""The engine behind the web interface: one job at a time (scan, album pages, sync), progress,
log, schedule."""
from __future__ import annotations

import asyncio
import collections
import importlib.util
import logging
import os
import time
from collections import deque
from typing import Optional
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .core import browser
from .core.config import Config
from .core.constants import RETRY_STATUSES
from .core.download import Downloader, ResolveError, run_item
from .core.limits import DownloadGate, RateLimit, _retry_after
from .core.sync import MANIFEST_NAME, FileInfo, Manifest, size_range, stamp_folders
from .core.util import fmt_bytes, parse_size, safe_segment

from . import __version__, parse
from .catalog import ALBUM, PLATFORM, Catalog, album_wants, parse_formats
from .scanner import Scanner

log = logging.getLogger("dircrawl")
USER_AGENT = f"Mozilla/5.0 (compatible; khinsider-sync/{__version__})"
SITE = "https://downloads.khinsider.com/"
LAYOUTS = ("album", "platform", "type", "year")


def browser_headers(base: str) -> dict:
    """Headers sent with every request: what a browser sends for a page, and khinsider as the
    Referer - its download servers expect it (the khi-explorer downloaders send the same)."""
    return {"User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": base}


class LogBuffer(logging.Handler):
    """Keeps the last lines of the log for the web interface."""

    def __init__(self, size: int = 400):
        super().__init__(logging.INFO)
        self.lines: deque = deque(maxlen=size)
        self.counter = 0

    def emit(self, record):
        try:
            self.counter += 1
            self.lines.append({"id": self.counter, "t": record.created, "level": record.levelname.lower(),
                               "msg": record.getMessage()})
        except Exception:  # pragma: no cover
            pass

    def since(self, last_id: int) -> list:
        return [l for l in self.lines if l["id"] > last_id]


def parse_mode(value) -> str:
    """'777', '0777', '00777' or '' -> normalised '777' / '' (keep the system default)."""
    v = str(value or "").strip()
    if not v:
        return ""
    digits = v.lstrip("0") or "0"
    if not (len(digits) <= 3 and all(c in "01234567" for c in digits)):
        raise ValueError(f"file permissions must be octal like 777 or 775, not {v!r}")
    return digits.rjust(3, "0")


def track_name(f: dict) -> str:
    """'01 - Title.mp3', or '1-01 - Title.flac' on a multi-disc album (khi-explorer's naming), from
    the album page's CD and # columns; without a # column the track's place in the list counts.
    Falls back to khinsider's file name when the track has no title."""
    title = (f.get("title") or "").strip()
    if not title:
        return f["name"]
    num = (f.get("num") or "").strip().rstrip(".")
    if not num:
        num = str((f.get("pos") or 0) // 100 + 1)
    num = num.zfill(2) if num.isdigit() else num
    disc = (f.get("disc") or "").strip()
    number = f"{disc}-{num}" if disc else num
    return safe_segment(f"{number} - {title}{f['ext']}")


def clean_place(value) -> str:
    """A "Saved as" folder typed in the page -> 'Folder/Sub' ('' = where it goes by default).
    Each part is made safe for Windows, macOS and Linux; '..' is refused."""
    text = str(value or "").strip().replace("\\", "/")
    parts = [p.strip() for p in text.split("/") if p.strip() not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("a folder can't go above the download folder (..)")
    return "/".join(safe_segment(p) for p in parts)


class Resolver:
    """A track's catalog address ("<track page>#.flac") -> the file's download address, read from
    the track page right before the download (the page holds every format's link, so it is read
    once per track and kept for its other formats)."""

    def __init__(self, client: httpx.AsyncClient, pages: dict, keep: int = 4000):
        self.client = client
        self.pages = pages                     # catalog address -> track page (None: download as is)
        self.cache: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
        self.locks: dict = {}
        self.keep = keep
        self.requests = 0

    async def __call__(self, url: str) -> str:
        page = self.pages.get(url)
        if not page:
            return url
        ext = url.rsplit("#", 1)[-1]
        links = await self._links(page)
        if ext not in links:
            raise ResolveError(f"the track page has no {ext[1:].upper()} link", permanent=True)
        return links[ext]

    async def _links(self, page: str) -> dict:
        if page in self.cache:
            self.cache.move_to_end(page)
            return self.cache[page]
        lock = self.locks.setdefault(page, asyncio.Lock())
        async with lock:
            if page in self.cache:
                return self.cache[page]
            self.requests += 1
            r = await self.client.get(page)
            if r.status_code != 200:
                status = r.status_code
                raise ResolveError(f"track page: HTTP {status}", status=status,
                                   permanent=status not in RETRY_STATUSES,
                                   retry_after=_retry_after(r.headers.get("retry-after")))
            if "html" not in r.headers.get("content-type", "html").lower():
                raise ResolveError("track page: not a web page", permanent=True)
            links = parse.parse_track(r.content, str(r.url))
            if not links:
                # khinsider's own "Ooops" page, or one without links: that track is gone
                raise ResolveError("the track page has no download links (the track may be gone)",
                                   permanent=True)
            self.cache[page] = links
            while len(self.cache) > self.keep:
                self.cache.popitem(last=False)
        self.locks.pop(page, None)
        return links


class Engine:
    def __init__(self, config_dir: str, data_dir: str, base_url: str = SITE, *,
                 data_locked: bool = False, scan_workers: int = 4, scan_delay: float = 0.25,
                 ignore_robots: bool = False, tuning: Optional[dict] = None):
        os.makedirs(config_dir, exist_ok=True)
        self.config_dir = config_dir
        self.base = base_url if base_url.endswith("/") else base_url + "/"
        self.catalog = Catalog(os.path.join(config_dir, "catalog.db"))
        self.data_locked = data_locked
        s = self.catalog.settings()
        if data_locked or not s.get("data_dir"):
            self.catalog.save_settings({"data_dir": data_dir})
        self.scan_workers, self.scan_delay = scan_workers, scan_delay
        self.ignore_robots = ignore_robots
        self.tuning = tuning or {}
        self.logbuf = LogBuffer()
        log.addHandler(self.logbuf)
        log.setLevel(logging.DEBUG)
        self._place_cache = None
        self._remap(at_start=True)
        self._log_startup()
        self.job: Optional[dict] = None
        self.task: Optional[asyncio.Task] = None
        self.scanner: Optional[Scanner] = None
        self.downloader: Optional[Downloader] = None
        self.gate: Optional[DownloadGate] = None
        self.sync_stats: dict = {}
        self.speed = 0.0
        self._samples: deque = deque(maxlen=6)          # ~5 s window: the speed shown
        self._eta_samples: deque = deque(maxlen=121)    # ~2 min window: steadier, for the ETA
        self.eta_speed = 0.0
        self.stop_requested = False
        self.scheduler: Optional[asyncio.Task] = None
        self.next_run: Optional[float] = None
        self.shutting_down = False
        self.leftovers: Optional[dict] = None   # Clean up summary from the last check
        self._sel_cache = None

    # ------------------------------------------------------------------ where albums are saved
    def album_dir(self, album: dict, settings: Optional[dict] = None) -> str:
        """An album's folder inside the download folder: its "Saved as" setting, or the layout
        setting's folder (platform / type / year, or none) plus the album's own folder name."""
        s = settings or self.catalog.settings()
        place = (s.get("album_places") or {}).get(album["slug"])
        if place:
            return place
        folder = album.get("folder") or safe_segment(album.get("name") or album["slug"])
        layout = s.get("folder_layout") or "album"
        top = ""
        if layout == "platform":
            plats = album.get("platforms") or []
            names = self._platform_names()
            top = names.get(plats[0], plats[0]) if plats else "Other"
        elif layout == "type":
            top = album.get("type") or "Other"
        elif layout == "year":
            top = album.get("year") or "Unknown year"
        return f"{safe_segment(top)}/{folder}" if top else folder

    def _platform_names(self) -> dict:
        if self._place_cache is None or self._place_cache[0] != self.catalog.version:
            self._place_cache = (self.catalog.version, self.catalog.platform_names())
        return self._place_cache[1]

    def rel_for(self, album: dict, f: dict, settings: Optional[dict] = None) -> str:
        """Where one file of an album goes, relative to the download folder. `f`: name (khinsider's
        file name), kind (track | art), ext, disc, num, title, pos (track order)."""
        s = settings or self.catalog.settings()
        folder = self.album_dir(album, s)
        if f["kind"] == "art":
            return f"{folder}/Art/{f['name']}" if s.get("art_folder") else f"{folder}/{f['name']}"
        return f"{folder}/{track_name(f) if s.get('track_names') == 'numbered' else f['name']}"

    @staticmethod
    def folder_name(name: str, slug: str = "") -> str:
        return safe_segment(name or slug)

    def store_album(self, slug: str, page: dict) -> int:
        """A freshly read album page into the catalog (the scanner calls this)."""
        s = self.catalog.settings()
        a = self.catalog.album(slug)
        if a is not None and not a.get("folder"):
            self.catalog.assign_folders(self.folder_name)
        return self.catalog.album_read(slug, page, safe_segment, lambda album, f: self.rel_for(album, f, s))

    def _remap(self, at_start: bool = False) -> int:
        s = self.catalog.settings()
        n = self.catalog.remap(lambda album, f: self.rel_for(album, f, s))
        if n:
            log.info("%d files now have a different place in the download folder%s", n,
                     "" if at_start else " - the next sync puts them there")
        return n

    def set_place(self, slug: str, place) -> dict:
        """The album's "Saved as" folder. "" = where it goes by default."""
        if self.busy:
            raise RuntimeError("busy")
        a = self.catalog.album(slug)
        if a is None:
            raise ValueError("no such album")
        value = clean_place(place)
        places = dict(self.catalog.settings().get("album_places") or {})
        places.pop(slug, None)
        if value and value != self.album_dir(a, dict(self.catalog.settings(), album_places={})):
            places[slug] = value
        self.catalog.save_settings({"album_places": places})
        log.info("Saved as: %s %s", a["name"], f"goes to {value}" if slug in places else "back to its own folder")
        moved = self._remap()
        return {"slug": slug, "place": places.get(slug, ""), "dir": self.album_dir(self.catalog.album(slug)),
                "moved": moved}

    def set_album_formats(self, slug: str, formats) -> dict:
        """One album's own formats ("" = the general setting)."""
        a = self.catalog.album(slug)
        if a is None:
            raise ValueError("no such album")
        value = str(formats or "").strip().lower()
        if value:
            parse_formats(value)
        own = dict(self.catalog.settings().get("album_formats") or {})
        own.pop(slug, None)
        if value:
            own[slug] = value
        self.catalog.save_settings({"album_formats": own})
        self._sel_cache = None
        log.info("Formats: %s %s", a["name"], f"uses {value}" if value else "back to the general setting")
        return {"slug": slug, "formats": value}

    def _log_startup(self) -> None:
        """Say which catalog and download folder this start uses and what's in them, so a wrong
        folder mapping (an empty catalog after an update) is easy to spot in the log."""
        cat = self.catalog
        try:
            st = os.stat(cat.path)
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            size = fmt_bytes(st.st_size)
        except OSError:
            when, size = "?", "?"
        c = cat.counts()
        ticks = sum(1 for v in cat.rules().values() if v)
        full = cat.last_scan("full")
        scanned = ("last full scan %s (%s)" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(full.get("started") or 0)),
                                               full.get("status"))) if full else "no full scan yet"
        if cat.existed:
            log.info("Catalog: %s (%s, last changed %s)", cat.path, size, when)
        else:
            log.warning("Catalog: %s did not exist - started a new, empty catalog", cat.path)
        log.info("Catalog has %d albums (%d read), %d ticks; %s", c["albums"], c["read"], ticks, scanned)
        data = self.data_dir
        manifest = os.path.join(data, MANIFEST_NAME)
        try:
            with os.scandir(data) as it:
                entries = [e.name for e in it if not e.name.startswith(".")]
            log.info("Download folder: %s (%d items at the top%s; download record %s)", data, len(entries),
                     ": " + ", ".join(sorted(entries)[:8]) + (" ..." if len(entries) > 8 else "") if entries else "",
                     "found" if os.path.exists(manifest) else "not found")
        except FileNotFoundError:
            entries = []
            log.warning("Download folder: %s does not exist yet", data)
        except OSError as e:
            entries = []
            log.warning("Download folder: %s can't be read (%s)", data, e)
        if not c["albums"] and (entries or os.path.exists(manifest)):
            log.warning("The catalog is empty but the download folder already has files. If Khinsider Sync "
                        "ran before, check that the config folder mapping (%s) points at the folder that "
                        "holds your old catalog.db - your ticks are stored there.", self.config_dir)

    # ------------------------------------------------------------------ settings
    @property
    def data_dir(self) -> str:
        return self.catalog.settings()["data_dir"]

    def update_settings(self, values: dict) -> dict:
        clean = {}
        s = self.catalog.settings()
        for k, v in values.items():
            if k not in s:
                continue
            if k == "data_dir" and self.data_locked:
                continue
            if k in ("interrupted_job", "album_places", "album_formats"):
                continue
            if k in ("parallel_max", "sync_every_hours", "full_scan_every_days", "album_refresh_days"):
                v = max(0, int(float(v or 0)))
            if k == "outage_wait_hours":
                v = max(0.0, float(v or 0))
                v = int(v) if v == int(v) else v
            if k in ("check_remote", "resume_interrupted", "album_art", "art_folder", "impersonate"):
                v = bool(v)
            if k == "track_names" and v not in ("site", "numbered"):
                raise ValueError("track names must be site or numbered")
            if k == "file_mode":
                v = parse_mode(v)
            if k in ("max_rate", "min_free") and v:
                parse_size(str(v))               # validates, raises on nonsense
            if k == "parallel" and str(v).strip().lower() != "auto":
                v = str(max(1, int(v)))
            if k == "formats":
                v = str(v or "mp3").strip().lower()
                parse_formats(v)
            if k == "folder_layout":
                if v not in LAYOUTS:
                    raise ValueError("folder layout must be one of " + ", ".join(LAYOUTS))
            if k == "platform_types":
                if isinstance(v, str):
                    v = [t.strip() for t in v.split(",")]
                v = sorted({str(t).strip() for t in (v or []) if str(t).strip()})
            if k == "data_dir":
                v = str(v or "").strip()
                if not v:
                    raise ValueError("the download folder can't be empty")
            clean[k] = v
        moving = [k for k in ("folder_layout", "track_names", "art_folder") if k in clean and clean[k] != s.get(k)]
        self.catalog.save_settings(clean)
        if moving:
            log.info("Changed %s", ", ".join(f"{k.replace('_', ' ')} to {clean[k]}" for k in moving))
            self._remap()
        self._sel_cache = None
        self._schedule_next()
        return self.catalog.settings()

    # ------------------------------------------------------------------ jobs
    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, kind: str, albums: Optional[list] = None, scheduled: bool = False) -> bool:
        """kind: scan (the album lists) | read (album pages: `albums`, or the ticked ones) |
        sync | scan+sync."""
        if self.busy:
            return False
        self.stop_requested = False
        self.job = {"kind": kind, "phase": "starting", "started": time.time(), "scheduled": scheduled,
                    "message": ""}
        if kind in ("sync", "scan+sync"):
            # remembered until the sync ends on its own or is stopped, so a sync cut off by a
            # crash, reboot or app restart carries on (and resumes its .part files) at next start
            self.catalog.save_settings({"interrupted_job": kind})
        what = {"scan": "scan of the album lists", "read": "reading album pages", "sync": "sync",
                "scan+sync": "scan + sync"}.get(kind, kind)
        log.info("Starting %s (%s)", what, "scheduled" if scheduled else "requested")
        self.task = asyncio.create_task(self._run(kind, albums))
        return True

    def shutdown(self) -> None:
        """The app is closing: stop the job but remember an unfinished sync."""
        self.shutting_down = True
        self.stop()

    def stop(self) -> None:
        self.stop_requested = True
        if self.scanner:
            self.scanner.stop()
        if self.downloader:
            self.downloader.stop_requested = True
        log.info("Stopping...")

    def _client(self) -> httpx.AsyncClient:
        http2 = importlib.util.find_spec("h2") is not None
        n = self.scan_workers + int(self.catalog.settings().get("parallel_max") or 8) * 2 + 4
        transport = browser.make_transport(bool(self.catalog.settings().get("impersonate", True)), n)
        if transport is not None:
            # Chrome's own User-Agent and headers go out (curl_cffi); only the Referer is ours
            return httpx.AsyncClient(transport=transport, follow_redirects=True, max_redirects=8,
                                     timeout=httpx.Timeout(20.0, connect=10.0), headers={"Referer": self.base})
        return httpx.AsyncClient(http2=http2, follow_redirects=True, max_redirects=8,
                                 timeout=httpx.Timeout(20.0, connect=10.0),
                                 limits=httpx.Limits(max_connections=n, max_keepalive_connections=n),
                                 headers=browser_headers(self.base))

    async def _robots(self, client) -> Optional[RobotFileParser]:
        if self.ignore_robots:
            return None
        p = urlsplit(self.base)
        try:
            r = await client.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=10)
            if r.status_code == 200 and "html" not in r.headers.get("content-type", ""):
                rp = RobotFileParser()
                rp.parse(r.text.splitlines())
                return rp
        except httpx.HTTPError:
            pass
        return None

    def _scanner(self, client, robots) -> Scanner:
        self.scanner = Scanner(self.catalog, self.base, client, workers=self.scan_workers,
                               delay=self.scan_delay, user_agent=USER_AGENT, robots=robots,
                               store=self.store_album)
        return self.scanner

    async def _run(self, kind: str, albums: Optional[list]) -> None:
        started = time.time()
        summary: dict = {}
        try:
            async with self._client() as client:
                robots = await self._robots(client)
                if kind in ("scan", "scan+sync"):
                    self.job["phase"] = "reading the album lists"
                    summary["scan"] = await self._scanner(client, robots).scan_site(self.folder_name)
                if kind == "read" and not self.stop_requested:
                    slugs = albums if albums else self.catalog.selected_albums()
                    self.job["phase"] = "reading album pages"
                    summary["read"] = await self._scanner(client, robots).read_albums(slugs)
                if kind in ("sync", "scan+sync") and not self.stop_requested:
                    summary["sync"] = await self._sync(client, robots)
            self.job["phase"] = "stopped" if self.stop_requested else "done"
        except Exception as e:
            log.exception("job failed: %r", e)
            self.job["phase"] = "failed"
            self.job["message"] = str(e)
            summary["error"] = repr(e)
        finally:
            self.job["finished"] = time.time()
            if kind in ("sync", "scan+sync") and not self.shutting_down:
                self.catalog.save_settings({"interrupted_job": ""})
            self.catalog.add_history(kind, started, summary)
            self.catalog.prune_changes()
            self.scanner = None
            self.catalog.commit()
            self.catalog.changed(albums=True)       # final numbers show right away
            self._sel_cache = None
            self._schedule_next()

    async def _sync(self, client, robots) -> dict:
        s = self.catalog.settings()
        data_dir = s["data_dir"]
        os.makedirs(data_dir, exist_ok=True)
        slugs = self.catalog.selected_albums()
        if not slugs:
            log.warning("Nothing is ticked - tick the albums or platforms you want, then sync.")
            return {"files": 0}
        albums = self.catalog.albums_by_slug(slugs)
        # album pages: read the ones never read, and the ones read longer ago than the setting says
        days = float(s.get("album_refresh_days") or 0)
        cutoff = time.time() - days * 86400
        stale = [a for a in slugs if a in albums and not albums[a]["missing"]
                 and (not albums[a]["read"] or days <= 0 or albums[a]["read"] < cutoff)]
        if stale:
            self.job["phase"] = "reading album pages"
            log.info("Reading %d album pages first (%d never read)", len(stale),
                     sum(1 for a in stale if not albums[a]["read"]))
            res = await self._scanner(client, robots).read_albums(stale, "album pages")
            if self.stop_requested:
                return {}
            if res["renamed"]:
                slugs = self.catalog.selected_albums()
            albums = self.catalog.albums_by_slug(slugs)
        self.scanner = None
        self.job["phase"] = "checking what needs downloading"
        want = {a: album_wants(s, a, d["formats"]) for a, d in albums.items()}
        rows = [r for r in self.catalog.files_of(albums)
                if ("art" if r[4] == "art" else r[3]) in want.get(r[2], ())]
        unread = [a for a in slugs if a in albums and not albums[a]["read"]]
        if unread:
            log.warning("%d ticked albums couldn't be read and are left out of this sync", len(unread))
        cfg = Config(start_url=self.base, download_dir=data_dir, check_remote=bool(s["check_remote"]),
                     min_free=parse_size(str(s["min_free"] or "0")),
                     file_mode=int(parse_mode(s.get("file_mode", "")), 8) if parse_mode(s.get("file_mode", "")) else None,
                     user_agent=USER_AGENT, retry_wait=self.tuning.get("retry", 2.0),
                     outage_wait=float(s.get("outage_wait_hours", 12) or 0) * 3600,
                     outage_check=self.tuning.get("outage_check", 30.0))
        manifest = Manifest(data_dir)
        host = urlsplit(self.base).hostname or ""
        key = f"max_parallel:{host}"
        known = manifest.get_meta(key)
        auto = str(s["parallel"]).strip().lower() == "auto"
        t = self.tuning
        self.gate = DownloadGate(auto=auto, start=2 if auto else int(s["parallel"]),
                                 hard_max=int(s["parallel_max"] or 8) if auto else int(s["parallel"]),
                                 known_max=int(known) if known and known.isdigit() else None,
                                 ramp_secs=t.get("ramp", 3.0), probe_secs=t.get("probe", 120.0),
                                 confirm_secs=t.get("confirm", 30.0))
        rate = parse_size(str(s["max_rate"])) if s["max_rate"] else 0
        st = self.sync_stats = {"total": len(rows), "albums": len(albums), "planned": 0, "to_fetch": 0,
                                "new": 0, "updated": 0, "resumed": 0, "up to date": 0, "already in folder": 0,
                                "failed": 0, "bytes_planned": 0, "bytes_settled": 0, "files_left": 0}

        def settle(url):
            if url in sizes:
                st["bytes_settled"] += sizes.pop(url)
                st["files_left"] -= 1

        job_id = self.job["started"]

        def on_done(url, rel, word, size):
            st[word] = st.get(word, 0) + 1
            settle(url)
            self.catalog.set_local(url, word, size, None)
            if word in ("new", "updated", "resumed"):
                self.catalog.add_change(job_id, word, rel, size)

        def on_failed(url, rel, err):
            st["failed"] += 1
            settle(url)
            self.catalog.set_local(url, None, None, err)
            self.catalog.add_change(job_id, "failed", rel, None, err)

        allowed = (lambda u: robots.can_fetch(USER_AGENT, u)) if robots else None
        resolver = Resolver(client, {r[0]: r[6] for r in rows if r[6]})
        self.downloader = dl = Downloader(cfg, client, manifest, self.gate, root=data_dir,
                                          bucket=RateLimit(rate) if rate else None, allowed=allowed,
                                          on_done=on_done, on_failed=on_failed, resolve=resolver)
        queue: asyncio.Queue = asyncio.Queue()
        claimed = [0]                            # taken from the queue, not finished yet
        dl.pending = lambda: queue.qsize() + claimed[0]
        sizes = {}

        moved = [0]

        def plan_all() -> list:
            """Compare every file with the disk. Runs in a worker thread: on a network share or an
            array this is thousands of disk lookups, and the page must stay responsive meanwhile.
            Nothing downloads yet, so nothing else uses the manifest.
            A file this tool saved before at another place (the folder layout or an album's Saved
            as changed) is moved to its new place instead of being downloaded again."""
            todo = []
            saved_at = manifest.by_url()
            left_dirs = set()
            for url, rel, album, ext, kind, size, page in rows:
                if self.stop_requested:
                    break
                old = saved_at.get(url)
                if old and old != rel:
                    left_dirs |= self._relocate(dl, manifest, old, rel, moved)
                info = FileInfo(ext, albums[album]["added"], size)
                action, reason = dl.plan(url, rel, info)
                st["planned"] += 1
                if action:
                    todo.append((url, rel, info, action, reason))
                    st["to_fetch"] += 1
                    st["files_left"] += 1
                    rng = size_range(size) if size else None
                    sizes[url] = (rng[0] + rng[1]) // 2 if rng else 0
                    st["bytes_planned"] += sizes[url]
            if left_dirs:
                from .cleanup import remove_empty_dirs
                remove_empty_dirs(data_dir, left_dirs)
            return todo

        for item in await asyncio.to_thread(plan_all):
            queue.put_nowait(item)
        manifest.commit()
        self.catalog.commit()
        if moved[0]:
            log.info("Moved %d files to their new place in the download folder", moved[0])
        log.info("Sync: %d albums, %d files, %d to download (about %s), %d already up to date",
                 len(albums), st["total"], st["to_fetch"], fmt_bytes(st["bytes_planned"]),
                 st["up to date"] + st["already in folder"])
        self.job["phase"] = "downloading"
        self._samples.clear()
        self._eta_samples.clear()
        self.eta_speed = 0.0

        async def worker():
            while True:
                item = await queue.get()
                claimed[0] += 1
                try:
                    await run_item(dl, self.gate, queue, item)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    on_failed(item[0], item[1], f"internal error: {e!r}")
                finally:
                    claimed[0] -= 1
                    queue.task_done()

        async def ticker():
            while True:
                await asyncio.sleep(0.25)
                if not dl.paused and not dl.outage:
                    await self.gate.tick(queue.qsize())
                now = time.monotonic()
                if not self._samples or now - self._samples[-1][0] >= 1:
                    self._samples.append((now, dl.bytes))
                    t0, b0 = self._samples[0]
                    self.speed = (dl.bytes - b0) / (now - t0) if now > t0 else 0.0
                    self._eta_samples.append((now, dl.bytes))
                    t1, b1 = self._eta_samples[0]
                    self.eta_speed = (dl.bytes - b1) / (now - t1) if now - t1 >= 3 else 0.0
                    manifest.commit()
                    self.catalog.commit()

        n = self.gate.hard_max if auto else self.gate.limit
        tasks = [asyncio.create_task(worker()) for _ in range(n)] + [asyncio.create_task(ticker())]
        best = None
        try:
            await queue.join()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if dl.touched_dirs:
                n = await asyncio.to_thread(stamp_folders, list(dl.touched_dirs), data_dir)
                if n:
                    log.info("Dated %d folders to their newest file", n)
            if not self.stop_requested and not dl.paused:
                await self._check_leftovers()
            best = self.gate.remembered_max()
            if best:
                manifest.set_meta(key, best)
            manifest.close()
            self.catalog.commit()
            self.speed = self.eta_speed = 0.0
        summary = {k: st[k] for k in ("total", "albums", "to_fetch", "new", "updated", "resumed", "up to date",
                                      "already in folder", "failed")}
        summary["bytes"] = dl.bytes
        summary["paused"] = dl.paused
        summary["server_max"] = best
        summary["track_pages"] = resolver.requests
        log.info("Sync %s: %d new, %d updated, %d resumed, %d failed, %s downloaded%s",
                 "stopped" if self.stop_requested else ("paused" if dl.paused else "finished"),
                 st["new"], st["updated"], st["resumed"], st["failed"], fmt_bytes(dl.bytes),
                 f" - paused: {dl.paused}" if dl.paused else "")
        return summary

    @staticmethod
    def _relocate(dl: Downloader, manifest: Manifest, old: str, new: str, moved: list) -> set:
        """Move a saved file from `old` to `new` (both relative to the download folder) when it is
        still at the old place and nothing is at the new one. -> folders it left."""
        src, dest = dl.dest_of(old), dl.dest_of(new)
        if not os.path.isfile(src) or os.path.exists(dest) or os.path.exists(dest + ".part"):
            return set()
        try:
            dl._make_dirs(os.path.dirname(dest))
            os.replace(src, dest)
        except OSError as e:
            log.warning("could not move %s to %s (%s) - it is downloaded again instead", old, new, e)
            return set()
        manifest.move(old, new)
        moved[0] += 1
        return {os.path.dirname(src)}

    # ------------------------------------------------------------------ one album, now
    async def read_album_now(self, slug: str) -> dict:
        """Read one album's page right away (opening an album in the page), outside the job queue."""
        async with self._client() as client:
            sc = Scanner(self.catalog, self.base, client, workers=1, delay=0, user_agent=USER_AGENT,
                         robots=None, store=self.store_album)
            res = await sc.read_albums([slug], "album")
        self.catalog.commit()
        self._sel_cache = None
        return res

    # ------------------------------------------------------------------ clean up
    async def find_leftovers(self) -> list:
        """Files in the download folder that aren't part of the sync (see cleanup.py)."""
        from . import cleanup
        s = self.catalog.settings()
        self.catalog.commit()
        files = await asyncio.to_thread(lambda: cleanup.find(self, s, cleanup.walk(s["data_dir"])))
        self.leftovers = dict(cleanup.summary(files), checked=time.time())
        return files

    async def _check_leftovers(self) -> None:
        try:
            files = await self.find_leftovers()
        except Exception as e:  # never let the check fail a sync
            log.warning("Could not check the download folder for leftover files: %s", e)
            return
        if files:
            log.info("%d files (%s) in the download folder aren't part of the sync any more - "
                     "review them with Clean up", len(files), fmt_bytes(self.leftovers["bytes"]))

    async def delete_leftovers(self, paths) -> dict:
        """Delete the given files, but only those that are still leftovers right now."""
        from . import cleanup
        wanted = set(paths)
        files = [f for f in await self.find_leftovers() if f["path"] in wanted]
        res = await asyncio.to_thread(cleanup.delete, self.data_dir, files)
        res["skipped"] = len(wanted) - len(files)
        gone = set(res["deleted"])
        for f in files:
            if f["path"] in gone:
                self.catalog.add_change(None, "deleted", f["path"], f["size"], cleanup.REASONS[f["reason"]])
        self.catalog.commit()
        self.catalog.forget_local([f["url"] for f in files if f.get("url") and f["path"] in gone])
        log.info(cleanup.describe(res))
        await self.find_leftovers()
        return res

    # ------------------------------------------------------------------ schedule
    def _schedule_next(self) -> None:
        s = self.catalog.settings()
        hours = float(s.get("sync_every_hours") or 0)
        if hours <= 0:
            self.next_run = None
            return
        last = next((h for h in self.catalog.history(50) if h["kind"] in ("sync", "scan+sync")), None)
        base = last["finished"] if last and last.get("finished") else time.time()
        self.next_run = max(base + hours * 3600, time.time() + 60)

    def resume_interrupted(self) -> bool:
        s = self.catalog.settings()
        if not s.get("interrupted_job") or self.busy:
            return False
        if not s.get("resume_interrupted", True):
            self.catalog.save_settings({"interrupted_job": ""})
            return False
        log.info("The last sync didn't finish (the app was closed or stopped) - continuing it")
        return self.start("sync")

    async def scheduler_loop(self, resume_delay: float = 3.0) -> None:
        self._schedule_next()
        await asyncio.sleep(resume_delay)
        self.resume_interrupted()
        while True:
            await asyncio.sleep(30)
            if self.next_run and time.time() >= self.next_run and not self.busy:
                s = self.catalog.settings()
                days = float(s.get("full_scan_every_days") or 0)
                full = self.catalog.last_scan("full")
                need_full = days > 0 and (not full or time.time() - (full["finished"] or 0) > days * 86400)
                log.info("Scheduled run: %s", "album list scan + sync" if need_full else "sync")
                self.start("scan+sync" if need_full else "sync", scheduled=True)

    # ------------------------------------------------------------------ status for the UI
    def selection(self) -> dict:
        """Size of the ticked albums (what a sync saves of them); worked out again only when the
        catalog or the ticks change, not on every status poll."""
        rules = self.catalog.rules()
        s = self.catalog.settings()
        key = (self.catalog.version, tuple(sorted(rules.items())), s.get("formats"), s.get("album_art"),
               tuple(s.get("platform_types") or ()))
        if self._sel_cache and self._sel_cache[0] == key:
            return dict(self._sel_cache[1])
        slugs = self.catalog.selected_albums(rules, s.get("platform_types") or [])
        albums = self.catalog.albums_by_slug(slugs)
        sel = {"albums": len(slugs), "unread": 0, "files": 0, "bytes": 0, "synced": 0, "synced_bytes": 0,
               "failed": 0}
        for slug, a in albums.items():
            if not a["read"]:
                sel["unread"] += 1
                continue
            t = self.catalog.album_totals(slug, a["formats"], s)
            for k in ("files", "bytes", "synced", "synced_bytes", "failed"):
                sel[k] += t[k]
        self._sel_cache = (key, dict(sel))
        return sel

    def status(self, log_since: int = 0) -> dict:
        s = self.catalog.settings()
        full = self.catalog.last_scan("full")
        c = self.catalog.counts()
        out = {
            "version": __version__,
            "busy": self.busy,
            "job": dict(self.job) if self.job else None,
            "catalog": dict(c, last_full_scan=full),
            "selection": self.selection(),
            "next_run": self.next_run,
            "leftovers": self.leftovers,
            "data_dir": s["data_dir"],
            "version_key": self.catalog.version,
            "log": self.logbuf.since(log_since),
        }
        if self.scanner is not None:
            out["scan"] = self.scanner.progress()
        if self.downloader is not None and self.job and self.job.get("phase") in ("downloading", "done", "stopped"):
            dl, g = self.downloader, self.gate
            st = self.sync_stats
            active = list(dl.active.items())
            # what this sync still has to transfer: planned sizes of unfinished files minus what the
            # files in progress already have on disk (listing sizes are rounded, so it's an estimate)
            left = max(0, st["bytes_planned"] - st["bytes_settled"] - sum(h for _, (h, _t) in active))
            running = self.busy and self.job.get("phase") == "downloading"
            eta = (left / self.eta_speed if running and self.eta_speed > 0 and not dl.paused and not dl.outage
                   else None)
            out["sync"] = dict(st, bytes=dl.bytes, speed=self.speed, paused=dl.paused,
                               outage=dict(dl.outage, gives_up=dl.outage["since"] + dl.cfg.outage_wait)
                               if dl.outage and running else None,
                               bytes_left=left, eta=round(eta) if eta is not None else None,
                               running=g.inflight if g else 0, limit=g.limit if g else 0,
                               gate=g.describe() if g else "",
                               active=[{"rel": r, "have": h, "total": t} for r, (h, t) in active[:100]])
        return out


__all__ = ["Engine", "ALBUM", "PLATFORM", "clean_place", "parse_mode", "LAYOUTS"]
