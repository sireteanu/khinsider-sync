"""The engine behind the web interface: one job at a time (scan, album pages, sync), progress,
log, schedule."""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import shutil
import threading
import time
import zipfile
from collections import deque
from typing import Optional
from urllib.parse import unquote, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .core import browser, solver
from .core.config import Config
from .core.constants import RETRY_STATUSES
from .core.download import DONE_WORD, Downloader, ResolveError, run_item
from .core.limits import DownloadGate, RateLimit, _retry_after
from .core.sync import MANIFEST_NAME, FileInfo, Manifest, size_range, stamp_file, stamp_folders
from .core.util import CHALLENGE_MESSAGE, ChallengeError, extension, fmt_bytes, is_challenge, parse_size, safe_segment

from . import __version__, parse
from . import account as account_mod
from .catalog import (ALBUM, PLATFORM, SYNCED_WORDS, Catalog, album_plan, album_rank, album_wants,
                      parse_format_setting, parse_formats)
from .core.constants import AUDIO_EXTENSIONS, BEST_ORDER
from .pacing import PageRate, Window
from .scanner import Scanner

log = logging.getLogger("khinsider_sync")
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


_OWN_NUMBER = re.compile(r"\s*(\d{1,3})(\s*[.)\-_]\s*|\s+)")


def track_name(f: dict) -> str:
    """'01 - Title.mp3', or '1-01 - Title.flac' on a multi-disc album (khi-explorer's naming), from
    the album page's CD and # columns; without a # column the track's place in the list counts.
    Falls back to khinsider's file name when the track has no title."""
    title = (f.get("title") or "").strip()
    # some khinsider titles end in the file's extension ("01 Title.mp3"): don't add it twice
    while extension(title) in AUDIO_EXTENSIONS:
        title = title[:-len(extension(title))].rstrip()
    if not title:
        return f["name"]
    num = (f.get("num") or "").strip().rstrip(".")
    if not num:
        num = str((f.get("pos") or 0) // 100 + 1)
    num = num.zfill(2) if num.isdigit() else num
    # many titles start with their own track number ("01 Title", "3. Title"): don't write it twice.
    # A single digit followed only by a space stays ("2 Become 1" is a title, not a number)
    m = _OWN_NUMBER.match(title)
    if m and num.isdigit() and int(m.group(1)) == int(num) and title[m.end():].strip() \
            and (len(m.group(1)) == 2 or m.group(2).strip()):
        title = title[m.end():].strip()
    disc = (f.get("disc") or "").strip()
    number = f"{disc}-{num}" if disc else num
    return safe_segment(f"{number} - {title}{f['ext']}")


SUMMARY_EVERY = 1800     # a long queue sums up its rounds in the log this often (and at its end)
LEFTOVER_EVERY = 1800    # during a long queue, the download folder is checked for leftovers this often
TAG_FILE_WARN = 60       # seconds on one music file's cover / tags before the log says so
TAG_FILE_LIMIT = 300     # ... before the sync gives up on that file and carries on
COVER_TIMEOUT = 60       # seconds to load an album's cover, all in
COVER_RETRY = 86400      # an album whose cover couldn't be loaded: the next try a day later
READ_BATCH = 10          # default of albums_per_batch: album pages a sync reads before downloading them


def file_intact(root: str, rel: str, size: Optional[int]) -> bool:
    """Is a downloaded file still in the folder, with the size it was saved with (when known)?"""
    try:
        st = os.stat(os.path.join(root, *rel.split("/")))
    except OSError:
        return False
    return st.st_size == size if size else True


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

    def __init__(self, client: httpx.AsyncClient, pages: dict, keep: int = 4000, page_rate=None,
                 direct: Optional[dict] = None):
        self.client = client
        self.page_rate = page_rate
        self.pages = pages                     # catalog address -> track page (None: download as is)
        # catalog address -> MP3 address from the album page's player: no track page needed; if it
        # doesn't deliver the file (source_failed), the track page is read after all
        self.direct = dict(direct or {})
        self.direct_used = 0
        self.direct_failed = 0
        self.cache: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
        self.locks: dict = {}
        self.keep = keep
        self.requests = 0
        self.challenged = False                # a track page got Cloudflare's browser check

    async def __call__(self, url: str) -> str:
        if url in self.direct:
            self.direct_used += 1
            return self.direct[url]
        page = self.pages.get(url)
        if not page:
            return url
        ext = url.rsplit("#", 1)[-1]
        links = await self._links(page)
        if ext not in links:
            raise ResolveError(f"the track page has no {ext[1:].upper()} link", permanent=True)
        return links[ext]

    def source_failed(self, url: str, src: str) -> bool:
        """The downloader got no file from the player's address: use the track page from now on."""
        if self.direct.get(url) != src:
            return False
        del self.direct[url]
        self.direct_failed += 1
        log.info("The album page's address for %s didn't work - reading its track page instead",
                 unquote(src.rsplit("/", 1)[-1]))
        return True

    async def _links(self, page: str) -> dict:
        if page in self.cache:
            self.cache.move_to_end(page)
            return self.cache[page]
        lock = self.locks.setdefault(page, asyncio.Lock())
        async with lock:
            if page in self.cache:
                return self.cache[page]
            self.requests += 1
            if self.page_rate is not None:
                await self.page_rate.wait()
            r = await self.client.get(page)
            if is_challenge(r.status_code, r.headers):
                self.challenged = True
                raise ResolveError(f"track page: HTTP {r.status_code} (Cloudflare browser check)", status=r.status_code,
                                   pause=CHALLENGE_MESSAGE.format(status=r.status_code))
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
        self.solve_state = solver.SolveState()   # FlareSolverr's cookie + User-Agent, kept between jobs
        self._leftovers_at = 0.0                 # the last leftover check during a sync
        self._round = 0                          # rounds of the queue in this job (albums_per_batch)
        self._pace: deque = deque(maxlen=400)    # (time, albums not read yet) at each round: the queue's pace
        self._cover_failed: dict = {}
        self._tag_stuck: set = set()
        self._fetch_failed: dict = {}            # folder image -> when its cover couldn't be fetched (retry daily)             # files whose tagging hung (TAG_FILE_LIMIT): left alone            # album slug -> when its cover couldn't be loaded (retry daily)
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
        self.account = None                            # account.Account of the running sync (album zips)
        self._job_list: Optional[dict] = None          # the account's list, read once a job
        self._from_list = False                        # the job is "Download my khinsider list"
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
        self.check_progress: Optional[dict] = None    # Check download folder: {total, done, missing}
        self.cleanup_progress: Optional[dict] = None  # Clean up's look through the folder: {files, expected}
        self._notifying: set = set()                  # notifications being sent
        self.page_rate = PageRate(0)                   # pacing.py: set per job from pages_per_minute
        self._robots_cache: Optional[tuple] = None     # (when, RobotFileParser or None): robots.txt for a day
        self.limit_hit: Optional[str] = None           # a files-per-hour / albums-per-day limit stopped the sync
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
        ups = dict(self.catalog.settings().get("album_upgrades") or {})
        if ups.pop(slug, None):                 # picked by hand: no files are deleted for it any more
            log.info("Upgrade of %s cancelled (its formats were changed)", a["name"])
        self.catalog.save_settings({"album_formats": own, "album_upgrades": ups})
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
            if k in ("interrupted_job", "album_places", "album_formats", "album_upgrades", "last_folder_check",
                     "queue_resume_at", "challenge_retries", "account_tidy",
                     "account_cookies", "account_status"):
                continue
            if k in ("parallel_max", "sync_every_hours", "full_scan_every_days", "check_folder_days",
                     "pages_per_minute", "files_per_hour", "albums_per_day", "albums_per_batch"):
                v = max(0, int(float(v or 0)))
            if k == "outage_wait_hours":
                v = max(0.0, float(v or 0))
                v = int(v) if v == int(v) else v
            if k in ("account_user", "account_password"):
                v = str(v or "")
                if k == "account_password" and not v:
                    continue                     # empty = keep the saved one (the page never sees it)
            if k in ("resume_interrupted", "album_art", "art_folder", "impersonate", "replace_formats", "player_links",
                     "account_zips",
                     "playlists", "folder_art", "embed_cover", "write_tags"):
                v = bool(v)
            if k == "notify_url":
                v = str(v or "").strip()
                from .notify import valid
                if v and not valid(v):
                    raise ValueError("the notification address must start with http:// or https:// "
                                     "(or pbul:// for Pushbullet)")
            if k == "notify_on":
                from .notify import EVENTS
                v = [e for e in EVENTS if e in (v or [])]
            if k == "flaresolverr_url":
                v = solver.normalize_url(str(v or ""))    # raises ValueError for nonsense
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
        if "formats" in clean:
            _mode, items, _preset = parse_format_setting(clean["formats"])
            if items is not None and ":" in clean["formats"]:   # such a list says itself whether images are saved
                clean["album_art"] = "art" in items
        if "account_user" in clean and clean["account_user"].strip() != (s.get("account_user") or "").strip():
            # another account (or none): forget the old one's password unless a new one came, and its session
            clean["account_user"] = clean["account_user"].strip()
            if not clean["account_user"]:
                clean["account_password"] = ""
            clean["account_cookies"] = {}
            clean["account_status"] = {}
        elif "account_password" in clean:
            clean["account_cookies"] = {}
            clean["account_status"] = {}
        moving = [k for k in ("folder_layout", "track_names", "art_folder") if k in clean and clean[k] != s.get(k)]
        self.catalog.save_settings(clean)
        if "flaresolverr_url" in clean and clean["flaresolverr_url"] != s.get("flaresolverr_url"):
            self.solve_state.clear()             # another FlareSolverr (or none): its cookie goes too
        if "pages_per_minute" in clean and clean["pages_per_minute"] != s.get("pages_per_minute"):
            self.page_rate.set(float(clean["pages_per_minute"] or 0))   # a sync under way speeds up / slows down now
            if self.busy:
                log.info("Page reads a minute: %s from now on", clean["pages_per_minute"] or "no limit")
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
        sync (`albums`: only those, else every ticked one) | scan+sync | quick (only the newest albums,
        from the home page; a full scan when that can't cover the time since the last scan) |
        quick+sync | check (the download folder, for downloaded files deleted since)."""
        if self.busy:
            return False
        self.stop_requested = False
        self.job = {"kind": kind, "phase": "starting", "started": time.time(), "scheduled": scheduled,
                    "message": ""}
        self._job_list = None
        self._from_list = kind == "list"
        if kind in ("sync", "scan+sync", "quick+sync") and not albums:
            # remembered until the sync ends on its own or is stopped, so a sync cut off by a
            # crash, reboot or app restart carries on (and resumes its .part files) at next start
            self.catalog.save_settings({"interrupted_job": kind})
        what = {"scan": "scan of the album lists", "read": "reading album pages", "sync": "sync",
                "scan+sync": "scan + sync", "check": "check of the download folder",
                "quick": "scan for new albums", "quick+sync": "scan for new albums + sync",
                "list": "download of your khinsider list"}.get(kind, kind)
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

    def flaresolverr_url(self) -> str:
        """The FlareSolverr address from Settings, else the FLARESOLVERR_URL environment variable ("" = off)."""
        url = self.catalog.settings().get("flaresolverr_url") or os.environ.get("FLARESOLVERR_URL", "")
        try:
            return solver.normalize_url(url)
        except ValueError:
            return ""

    def _client(self, flaresolverr: bool = True) -> httpx.AsyncClient:
        n = self.scan_workers + int(self.catalog.settings().get("parallel_max") or 8) * 2 + 4
        transport = browser.make_transport(bool(self.catalog.settings().get("impersonate", True)), n)
        limits = httpx.Limits(max_connections=n, max_keepalive_connections=n)
        url = self.flaresolverr_url() if flaresolverr else ""
        host = urlsplit(self.base).hostname or ""
        if transport is not None:
            # Chrome's own User-Agent and headers go out (curl_cffi); only the Referer is ours
            return httpx.AsyncClient(transport=solver.wrap(transport, url, host, self.solve_state),
                                     follow_redirects=True, max_redirects=8,
                                     timeout=httpx.Timeout(20.0, connect=10.0), headers={"Referer": self.base})
        # HTTP/1.1 like a plain browser request: Cloudflare singles out HTTP/2 from clients that aren't
        # browsers (seen by the ghost-dl downloader)
        return httpx.AsyncClient(http2=False, follow_redirects=True, max_redirects=8,
                                 transport=solver.wrap(None, url, host, self.solve_state, http2=False, limits=limits),
                                 timeout=httpx.Timeout(20.0, connect=10.0),
                                 limits=limits, headers=browser_headers(self.base))

    async def _robots(self, client=None) -> Optional[RobotFileParser]:
        """khinsider's robots.txt - kept for a day, and fetched in a session of its own: a session
        that opens with robots.txt looks like a bot to Cloudflare, and the album page asked for next
        in it got the browser check (seen on the owner's server; opening an album, without it, didn't)."""
        if self.ignore_robots:
            return None
        cached = self._robots_cache
        if cached and time.time() - cached[0] < 86400:
            return cached[1]
        p = urlsplit(self.base)
        rp = None
        try:
            async with self._client(flaresolverr=False) as own:
                r = await own.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=10)
            # khinsider serves robots.txt as text/html: judge by the content, not the type
            if r.status_code == 200 and not r.text.lstrip().startswith("<"):
                rp = RobotFileParser()
                rp.parse(r.text.splitlines())
        except httpx.HTTPError:
            return None                          # try again next job
        self._robots_cache = (time.time(), rp)
        return rp

    def _scanner(self, client, robots) -> Scanner:
        self.scanner = Scanner(self.catalog, self.base, client, workers=self.scan_workers,
                               delay=self.scan_delay, user_agent=USER_AGENT, robots=robots,
                               store=self.store_album)
        self.scanner.page_rate = self.page_rate
        return self.scanner

    async def _run(self, kind: str, albums: Optional[list]) -> None:
        started = time.time()
        summary: dict = {}
        self._round = 0
        try:
            s = self.catalog.settings()
            self.page_rate = PageRate(float(s.get("pages_per_minute") or 0))
            self.limit_hit = None
            days = float(s.get("check_folder_days") or 0)
            if kind == "check":                  # only the disk: no requests
                summary["check"] = await self._check_folder()
            elif self.job.get("scheduled") and days > 0 and time.time() - float(s.get("last_folder_check") or 0) >= days * 86400 - 3600:
                log.info("Scheduled check of the download folder (every %g days) before the sync", days)
                summary["check"] = await self._check_folder()
            if kind != "check" and not self.stop_requested:
                async with self._client() as client:
                    robots = await self._robots(client)
                    full = kind in ("scan", "scan+sync")
                    since = self.catalog.last_good_scan()
                    if full and since is not None:
                        # the newest-albums list also shows albums khinsider replaced with another
                        # version; the browse pages don't - one page more catches them
                        day = time.strftime("%Y-%m-%d", time.localtime(since - 2 * 86400))
                        summary["latest"] = await self._scanner(client, robots).scan_latest(self.folder_name, day)
                        if self.scanner.challenged:
                            full = False                 # the browse pages would get the same check
                    if kind in ("quick", "quick+sync"):
                        since = self.catalog.last_good_scan()
                        if since is None:
                            log.info("No finished scan yet - reading all of khinsider's album lists instead")
                            full = True
                        else:
                            self.job["phase"] = "reading the newest albums"
                            day = time.strftime("%Y-%m-%d", time.localtime(since - 2 * 86400))   # 2 days slack
                            res = summary["scan"] = await self._scanner(client, robots).scan_latest(self.folder_name, day)
                            full = res["status"] == "incomplete" and not res["errors"] and not self.stop_requested
                    if full and not self.stop_requested:
                        self.job["phase"] = "reading the album lists"
                        summary["scan"] = await self._scanner(client, robots).scan_site(self.folder_name)
                    if kind == "read" and not self.stop_requested:
                        slugs = albums if albums else self.catalog.selected_albums()
                        self.job["phase"] = "reading album pages"
                        summary["read"] = await self._scanner(client, robots).read_albums(slugs)
                    if kind == "list" and not self.stop_requested:
                        summary["sync"] = await self._from_account_list(client, robots)
                    if kind in ("sync", "scan+sync", "quick+sync") and not self.stop_requested:
                        self._round = 1
                        summary["sync"] = await self._sync(client, robots, albums if kind == "sync" else None)
                        said = time.time()
                        # a long list goes a batch at a time (albums_per_batch): go straight on with the
                        # next one in this job - no gap between albums, one notification at the end
                        while (not self.stop_requested and (summary["sync"].get("next_batch") or 0)
                               and 0 < (summary["sync"].get("resume_at") or 0) <= time.time() + 1):
                            self._round += 1
                            more = await self._sync(client, robots, None)
                            summary["sync"] = _add_runs(summary["sync"], more)
                            if time.time() - said >= SUMMARY_EVERY:
                                self._queue_summary(summary["sync"], started)
                                said = time.time()
                        if self._round > 1:
                            self._queue_summary(summary["sync"], started,
                                                final=not summary["sync"].get("next_batch"))
                        # a browser check: wait and try again by itself, longer each time in a row
                        if summary["sync"].get("challenged") and not self.stop_requested:
                            got = any(summary["sync"].get(k) for k in ("new", "updated", "resumed"))
                            self._challenge_backoff(fresh=got)
                        elif int(self.catalog.settings().get("challenge_retries") or 0):
                            self.catalog.save_settings({"challenge_retries": 0})
                            log.info("khinsider lets this network read its pages again")
                    # the queue is through (nothing waits, no check): take saved albums off the khinsider
                    # list now - during the queue those page reads would slow every album down
                    if (kind in ("sync", "scan+sync", "quick+sync", "list") and not self.stop_requested
                            and not (summary.get("sync") or {}).get("challenged")
                            and not (summary.get("sync") or {}).get("next_batch")
                            and not float(self.catalog.settings().get("queue_resume_at") or 0)):
                        await self._tidy_list(client)
            self.job["phase"] = "stopped" if self.stop_requested else "done"
            if self.scanner is not None and self.scanner.challenged and "browser check" not in (self.job.get("message") or ""):
                self.job["message"] = "khinsider asks for a browser check (Cloudflare) - see the log"
        except Exception as e:
            log.exception("job failed: %r", e)
            self.job["phase"] = "failed"
            self.job["message"] = str(e)
            summary["error"] = repr(e)
        finally:
            self.job["finished"] = time.time()
            if kind in ("sync", "scan+sync", "quick+sync") and not albums and not self.shutting_down:
                self.catalog.save_settings({"interrupted_job": ""})
            self.catalog.add_history(kind, started, summary)
            self.catalog.prune_changes()
            self.scanner = None
            self.catalog.commit()
            self.catalog.changed(albums=True)       # final numbers show right away
            self._sel_cache = None
            self._schedule_next()
            if not self.shutting_down:              # sent on the side: never holds up the next job
                task = asyncio.create_task(self._notify(kind, started, summary))
                self._notifying.add(task)
                task.add_done_callback(self._notifying.discard)

    async def _notify(self, kind: str, started: float, summary: dict) -> None:
        """Tell the notification address what happened (notify.py)."""
        from . import notify
        s = self.catalog.settings()
        if not (s.get("notify_url") or "").strip():
            return
        try:
            sy = summary.get("sync") or {}
            got = sy.get("got_albums") or []
            if got or sy.get("failed"):
                n = sum(sy.get(k, 0) for k in ("new", "updated", "resumed"))
                parts = []
                if got:
                    parts.append(f"{fmt_bytes(sy.get('bytes', 0))} for {notify.album_list(got)}.")
                if sy.get("failed"):
                    parts.append(f"{sy['failed']} files failed - see the Activity log.")
                await notify.send_quietly(s, "downloads", f"Khinsider Sync: {n} new files" if n else
                                          f"Khinsider Sync: {sy['failed']} files failed", " ".join(parts))
            if kind in ("scan", "quick", "scan+sync", "quick+sync") and "error" not in summary:
                with self.catalog.lock:
                    fresh = dict(self.catalog.db.execute("SELECT slug, name FROM albums WHERE first_seen>=?",
                                                         (started,)).fetchall())
                if fresh and len(fresh) < 5000:      # not the very first scan of the whole site
                    mine = [fresh[a] for a in self.catalog.selected_albums() if a in fresh]
                    if mine:
                        await notify.send_quietly(
                            s, "new_albums", f"Khinsider Sync: {len(mine)} new albums for you",
                            f"New on khinsider for your ticked platforms: {notify.album_list(mine)}."
                            + ("" if kind.endswith("+sync") else " The next sync downloads them."))
            problem = None
            if sy.get("paused") and not self.limit_hit:
                problem = f"The sync paused: {sy['paused']}"
            elif summary.get("error"):
                problem = f"The {kind} failed: {self.job.get('message') or summary['error']}"
            elif "browser check" in (self.job.get("message") or ""):
                problem = "khinsider's Cloudflare protection asked for a browser check - nothing could be read."
            if problem:
                await notify.send_quietly(s, "problems", "Khinsider Sync needs a look", problem)
        except Exception as e:
            log.warning("Could not send notifications: %r", e)

    async def _sync(self, client, robots, only: Optional[list] = None) -> dict:
        s = self.catalog.settings()
        data_dir = s["data_dir"]
        os.makedirs(data_dir, exist_ok=True)
        slugs = self.catalog.selected_albums()
        if only:
            slugs = [a for a in only if a in set(slugs)]
        if not slugs:
            log.warning("Nothing is ticked - tick the albums or platforms you want, then sync.")
            return {"files": 0}
        albums = self.catalog.albums_by_slug(slugs)
        # albums per day (pacing.py): albums not started yet beyond the day's room wait for a later run -
        # their pages aren't even read now. Albums already under way always continue.
        held: list = []
        day = Window(int(s.get("albums_per_day") or 0), 86400, self.catalog.quota_times("album", 86400))
        if day.limit and not only:
            begun = self.catalog.quota_keys("album", 86400)
            fresh = []
            for a in sorted(albums, key=lambda a: (albums[a]["name"] or a).casefold()):
                d = albums[a]
                t = self.catalog.album_totals(a, d["formats"], s) if d["read"] else None
                if a in begun or (t and t["synced"]) or (t and t["downloaded"] and not d["changed"]):
                    continue                     # under way, or done (skipped further down)
                fresh.append(a)
            for a in fresh:
                if day.take():
                    self.catalog.quota_add("album", a)
                else:
                    held.append(a)
            if held:
                waiting = set(held)
                slugs = [a for a in slugs if a not in waiting]
                albums = {a: d for a, d in albums.items() if a not in waiting}
                log.info("Albums per day: %d albums wait for later (limit %d a day) - the app carries on "
                         "by itself at %s", len(held), day.limit, time.strftime("%a %H:%M", time.localtime(day.free_at())))
        self.catalog.commit()
        # khinsider doesn't change albums once they're up: a page is read once, the first time its
        # album is synced (or opened), and never again
        unread = [a for a in slugs if a in albums and not albums[a]["missing"] and not albums[a]["read"]]
        # a long list is done in batches: read a few album pages, download those albums, carry on -
        # never thousands of pages up front (that's what gets a connection challenged)
        later: list = []
        if unread and not only:
            self._pace.append((time.time(), len(unread)))
        batch = max(1, int(s.get("albums_per_batch") or READ_BATCH))
        if len(unread) > batch and not only:
            unread.sort(key=lambda a: (albums[a]["name"] or a).casefold())
            later = unread[batch:]
            unread = unread[:batch]
            skip = set(later)
            slugs = [a for a in slugs if a not in skip]
            albums = {a: d for a, d in albums.items() if a not in skip}
            self._note("%d ticked albums haven't been read yet: this sync does %d of them, then carries on "
                     "with the next ones by itself", len(unread) + len(later), len(unread))
        if unread:
            self.job["phase"] = "reading album pages"
            self._note("Reading %d album pages first", len(unread))
            sc = self._scanner(client, robots)
            sc.workers = 1                       # one page at a time, never a burst
            sc.quiet = self._round > 1
            res = await sc.read_albums(unread, "album pages")
            if self.stop_requested:
                return {}
            if self.scanner.challenged:          # track pages would get the same: no downloads now
                self.job["message"] = "khinsider asks for a browser check (Cloudflare) - see the log"
                return {"challenged": True}
            if res["renamed"]:
                slugs = [res["renamed"].get(a, a) for a in slugs]
            albums = self.catalog.albums_by_slug(slugs)
        self.scanner = None
        self.job["phase"] = "checking what needs downloading"
        # albums already downloaded are done for good: no requests and no disk checks for them
        done = {a for a, d in albums.items()
                if d["read"] and not d["changed"] and self.catalog.album_totals(a, d["formats"], s)["downloaded"]}
        if done:
            self._note("%d ticked albums are already downloaded - skipped", len(done))
        albums = {a: d for a, d in albums.items() if a not in done}
        # each album's files in the order of its formats setting: with "FLAC, then MP3", every
        # FLAC file of the sync is queued before the first MP3 file, album by album within that
        plan = {a: album_plan(s, a, d["formats"]) for a, d in albums.items()}
        rank = {a: {k: album_rank(s, a, k) for k in p} for a, p in plan.items()}
        wants = {a: self.catalog.wants(s, a, d["formats"]) for a, d in albums.items()}
        # a track missing in the album's format comes in another one (format_fallback), queued with it
        main = {a: next((k for k in p if k != "art"), None) for a, p in plan.items()}

        def rank_of(r):
            key = "art" if r[4] == "art" else r[3]
            return rank[r[2]][key] if key in rank[r[2]] else rank[r[2]].get(main[r[2]], 0)
        rows = [r for r in self.catalog.files_of(albums) if r[2] in wants and wants[r[2]].has(r[0], r[3], r[4])]
        order = {r[0]: i for i, r in enumerate(rows)}
        rows.sort(key=lambda r: (rank_of(r), order[r[0]]))
        unread = [a for a in slugs if a in albums and not albums[a]["read"]]
        if unread:
            log.warning("%d ticked albums couldn't be read and are left out of this sync", len(unread))
        cfg = Config(start_url=self.base, download_dir=data_dir,
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
        st = self.sync_stats = {"total": len(rows), "albums": len(albums), "albums_done": len(done),
                                "planned": 0, "to_fetch": 0,
                                "new": 0, "updated": 0, "resumed": 0, "up to date": 0, "already in folder": 0,
                                "failed": 0, "bytes_planned": 0, "bytes_settled": 0, "files_left": 0,
                                # what comes after this run (the Activity panel says so): ticked albums
                                # whose pages the next runs read, and albums waiting for the day's room
                                "albums_later": len(later), "albums_held": len(held),
                                "held_until": day.free_at() if held else 0}

        def settle(url):
            if url in sizes:
                st["bytes_settled"] += sizes.pop(url)
                st["files_left"] -= 1

        job_id = self.job["started"]

        album_of = {r[0]: r[2] for r in rows}
        got: set = set()                             # albums that got files (for notifications)

        def on_done(url, rel, word, size):
            st[word] = st.get(word, 0) + 1
            settle(url)
            self.catalog.set_local(url, word, size, None)
            if word in ("new", "updated", "resumed"):
                self.catalog.add_change(job_id, word, rel, size)
                self.catalog.quota_add("file", rel)
                got.add(album_of.get(url))

        def on_failed(url, rel, err):
            st["failed"] += 1
            settle(url)
            self.catalog.set_local(url, None, None, err)
            self.catalog.add_change(job_id, "failed", rel, None, err)

        allowed = (lambda u: robots.can_fetch(USER_AGENT, u)) if robots else None
        direct = self.catalog.direct_links(albums) if s.get("player_links", True) else {}
        resolver = Resolver(client, {r[0]: r[6] for r in rows if r[6]}, page_rate=self.page_rate,
                            direct={r[0]: direct[r[0]] for r in rows if r[0] in direct})
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

        todo = await asyncio.to_thread(plan_all)
        if account_mod.configured(s) and todo and not self.stop_requested:
            # a paid account: whole albums as one zip each; whatever a zip doesn't cover goes file by file
            kinds = {r[0]: (r[2], r[4]) for r in rows}
            todo = await self._album_zips(client, s, todo, wants, albums, kinds, dl, manifest, on_done)
        for item in todo:
            queue.put_nowait(item)
        replaced = [a for a, d in albums.items() if d["changed"] and d["read"]]
        if replaced and not self.stop_requested:
            self.catalog.clear_changed(replaced)    # their files were compared with the new page
        manifest.commit()
        self.catalog.commit()
        if moved[0]:
            log.info("Moved %d files to their new place in the download folder", moved[0])
        self._note("Sync: %d albums, %d files, %d to download (about %s), %d already up to date",
                 len(albums), st["total"], st["to_fetch"], fmt_bytes(st["bytes_planned"]),
                 st["up to date"] + st["already in folder"])
        self.job["phase"] = "downloading"
        self._samples.clear()
        self._eta_samples.clear()
        self.eta_speed = 0.0

        # files per hour (pacing.py): when reached, the sync stops cleanly and carries on later
        hour = Window(int(s.get("files_per_hour") or 0), 3600, self.catalog.quota_times("file", 3600))
        counted: set = set()                     # files already counted (the gate can put one back in the queue)

        async def worker():
            while True:
                item = await queue.get()
                claimed[0] += 1
                try:
                    if item[0] not in counted and (self.limit_hit or (not dl.paused and not dl.stop_requested
                                                                      and not hour.take())):
                        if not self.limit_hit:   # files under way finish; no new one starts
                            self.limit_hit = (f"the limit of {hour.limit} files an hour is reached - the app carries "
                                              f"on by itself at {time.strftime('%H:%M', time.localtime(hour.free_at()))}")
                            log.info("Files per hour: %s", self.limit_hit)
                        continue                 # stays queued for the next run
                    counted.add(item[0])
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
                if self._sample_speed(dl.bytes):
                    manifest.commit()
                    self.catalog.commit()

        n = self.gate.hard_max if auto else self.gate.limit
        tasks = [asyncio.create_task(worker()) for _ in range(n)] + [asyncio.create_task(ticker())]
        best = None
        try:
            await queue.join()
            if self.limit_hit and not dl.paused:
                dl.paused = self.limit_hit       # shown as "Paused: ..." - the sync carries on by itself
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if dl.touched_dirs:
                n = await asyncio.to_thread(stamp_folders, list(dl.touched_dirs), data_dir)
                if n:
                    self._note("Dated %d folders to their newest file", n)
            if not self.stop_requested:
                manifest.commit()
                self.catalog.commit()
                if not dl.paused:
                    await self._finish_upgrades()
                # also after a pause (download limit, browser check): what did arrive gets its extras
                finished = self._finished_albums()      # worked out once for the steps below
                await self._write_extras(client, finished, recheck={a for a in got if a})
                await self._tag_files(client, manifest, finished)
                # the walk over the whole download folder: at the end of the queue, or every half hour
                # during a long one - not after every round (it gets slower as the folder grows)
                if not dl.paused and (not later or time.time() - self._leftovers_at >= LEFTOVER_EVERY):
                    await self._check_leftovers()
                    self._leftovers_at = time.time()
            best = self.gate.remembered_max()
            if best:
                manifest.set_meta(key, best)
            manifest.close()
            self.catalog.commit()
            self.speed = self.eta_speed = 0.0
        summary = {k: st[k] for k in ("total", "albums", "albums_done", "to_fetch", "new", "updated", "resumed", "up to date",
                                      "already in folder", "failed")}
        summary["bytes"] = dl.bytes
        summary["paused"] = dl.paused
        summary["server_max"] = best
        summary["track_pages"] = resolver.requests
        summary["player_files"] = resolver.direct_used - resolver.direct_failed
        if resolver.direct_used:
            self._note("%d files came with the address from their album page's player (no track page needed)%s",
                     resolver.direct_used - resolver.direct_failed,
                     f"; {resolver.direct_failed} needed their track page after all" if resolver.direct_failed else "")
        # carry on by itself when a limit stopped this sync (not when you pressed Stop)
        resume = 0.0
        if not self.stop_requested:
            if self.limit_hit:
                resume = hour.free_at()
            if held:
                resume = max(resume, day.free_at()) if self.limit_hit else day.free_at()
            if later and not resume and not resolver.challenged:
                resume = time.time()             # the next batch right away
        self.catalog.save_settings({"queue_resume_at": resume})
        summary["waiting_albums"] = len(held)
        summary["next_batch"] = len(later)
        if resolver.challenged:
            summary["challenged"] = True
        summary["resume_at"] = resume
        names = self.catalog.albums_by_slug({a for a in got if a})
        summary["got_albums"] = sorted(names[a]["name"] for a in names)
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
    def check_album(self, slug: str) -> dict:
        """Look on disk for an album's downloaded files (they are never checked by a sync once the
        album is downloaded). Missing ones are downloaded again by the next sync."""
        if self.catalog.album(slug) is None:
            raise ValueError("no such album")
        root = self.data_dir
        gone = self.catalog.forget_missing(slug, lambda rel, size: file_intact(root, rel, size))
        from . import extras
        made = {r: v for r, v in self.catalog.extras().items() if v[0] == slug}
        lost = extras.missing(root, made, made, {slug})
        if lost:
            self.catalog.drop_extras(lost)
            gone = gone + lost
        self._sel_cache = None
        if gone:
            log.info("%d files of %s are missing or cut short in the download folder - the next sync gets them again",
                     len(gone), slug)
        return {"slug": slug, "missing": gone}

    async def _check_folder(self) -> dict:
        """Every downloaded file is looked for on disk; the ones deleted since are marked not
        downloaded, so their albums show it and the next sync gets them again."""
        self.job["phase"] = "looking at every downloaded file"
        root = self.data_dir
        rows = self.catalog.saved_files()
        prog = self.check_progress = {"total": len(rows), "done": 0, "missing": 0, "started": time.time()}

        def look() -> list:
            gone = []
            for url, rel, album, _ext, _kind, _size, saved in rows:
                if self.stop_requested:
                    break
                if not file_intact(root, rel, saved):
                    gone.append((url, rel, album))
                    prog["missing"] += 1
                prog["done"] += 1
            return gone

        gone = await asyncio.to_thread(look)
        if not self.stop_requested:
            self.catalog.save_settings({"last_folder_check": time.time()})
            # playlists and folder images the app wrote: written again by the next sync when gone
            from . import extras
            made = self.catalog.extras()
            lost = await asyncio.to_thread(extras.missing, root, made, made, {a for a, _k, _s in made.values()})
            if lost:
                self.catalog.drop_extras(lost)
                log.info("%d playlists / folder images are missing - the next sync writes them again", len(lost))
        if gone:
            self.catalog.forget_local(u for u, _, _ in gone)
        albums = sorted({a for _, _, a in gone})
        self._sel_cache = None
        self.job["message"] = (f"{len(gone):,} of {len(rows):,} downloaded files are missing or cut short ({len(albums):,} "
                               f"{'album' if len(albums) == 1 else 'albums'}) - the next sync downloads them again"
                               if gone else f"all {len(rows):,} downloaded files are there")
        if self.stop_requested:
            log.info("Check of the download folder stopped")
        elif gone:
            log.info("Check of the download folder: %d of %d downloaded files are missing or cut short (%d albums) - "
                     "the next sync downloads them again", len(gone), len(rows), len(albums))
            named = self.catalog.albums_by_slug(albums)
            for a in albums[:20]:
                n = sum(1 for _, _, x in gone if x == a)
                log.info("  %s: %d %s missing", named[a]["name"] if a in named else a, n, "file" if n == 1 else "files")
            if len(albums) > 20:
                log.info("  ... and %d more albums", len(albums) - 20)
        else:
            log.info("Check of the download folder: all %d downloaded files are there", len(rows))
        return {"files": len(rows), "missing": len(gone), "albums": len(albums)}

    # ------------------------------------------------------------------ upgrading an album
    @staticmethod
    def _quality(ext: str) -> int:
        """Lower is better (BEST_ORDER: FLAC, WAV ... MP3)."""
        return BEST_ORDER.index(ext) if ext in BEST_ORDER else len(BEST_ORDER)

    def upgrade_info(self, slug: str, settings: Optional[dict] = None) -> dict:
        """What an album's window offers: the audio formats it has that are better than the best one
        downloaded (`better`), and a waiting upgrade (`pending`)."""
        s = settings or self.catalog.settings()
        a = self.catalog.album(slug)
        pending = (s.get("album_upgrades") or {}).get(slug)
        have = {r[3] for r in self.catalog.saved_files(slug) if r[4] != "art" and r[3] in AUDIO_EXTENSIONS}
        better = []
        if a and have:
            best = min(self._quality(e) for e in have)
            better = sorted((f for f in a["formats"] if f in AUDIO_EXTENSIONS and self._quality(f) < best),
                            key=self._quality)
        return {"have": sorted(have, key=self._quality), "better": better,
                "pending": pending or None}

    def upgrade_album(self, slug: str, fmt: str) -> dict:
        """Get an album in a better format and delete the old one: the album gets its own formats
        setting (just `fmt`, plus its images if it had them) and is ticked; once a sync has downloaded
        every new file, the files of the old formats are deleted (_finish_upgrades). fmt "" cancels a
        waiting upgrade and puts the album's formats back."""
        a = self.catalog.album(slug)
        if a is None:
            raise KeyError("no such album")
        s = self.catalog.settings()
        ups = dict(s.get("album_upgrades") or {})
        own = dict(s.get("album_formats") or {})
        fmt = str(fmt or "").strip().lower()
        if not fmt:
            u = ups.pop(slug, None)
            if u is None:
                return {"slug": slug, "pending": None}
            own.pop(slug, None)
            if u.get("formats"):
                own[slug] = u["formats"]
            self.catalog.save_settings({"album_upgrades": ups, "album_formats": own})
            self._sel_cache = None
            log.info("Upgrade of %s cancelled - its formats are as before", a["name"])
            return {"slug": slug, "pending": None}
        fmt = "." + fmt.lstrip(".")
        info = self.upgrade_info(slug, s)
        if not info["have"]:
            raise ValueError("nothing of this album is downloaded yet - pick its formats instead")
        if fmt not in info["better"]:
            raise ValueError(f"{fmt[1:].upper()} isn't a better format this album has")
        art = "art" in album_wants(s, slug, a["formats"])
        before = ups.get(slug, {}).get("formats", own.get(slug, ""))
        # the old formats after it: tracks the album doesn't have in the new one keep theirs
        own[slug] = f"first:{fmt}" + "".join(f",{e}" for e in info["have"] if e != fmt) + (",art" if art else "")
        ups[slug] = {"to": fmt, "from": [e for e in info["have"] if e != fmt], "formats": before,
                     "since": time.time()}
        self.catalog.save_settings({"album_upgrades": ups, "album_formats": own})
        self.catalog.select(ALBUM + slug, True)
        self._sel_cache = None
        log.info("Upgrade: %s gets %s; its %s files are deleted once the next sync has them all",
                 a["name"], fmt[1:].upper(), ", ".join(e[1:].upper() for e in ups[slug]["from"]))
        return {"slug": slug, "pending": ups[slug]}

    def _replacements(self, s: dict) -> dict:
        """Albums whose old music format can go now -> {slug: (old formats, why, upgrade or None)}:
        upgrades (upgrade_album) and, with `replace_formats`, every ticked album with saved music files
        of formats it no longer syncs - in both cases only once every wanted file is downloaded."""
        ups = s.get("album_upgrades") or {}
        out = {}
        stale: dict = {}
        if s.get("replace_formats"):
            selected = set(self.catalog.selected_albums(None, s.get("platform_types") or []))
            for _url, _rel, slug, ext, kind, *_ in self.catalog.saved_files():
                if kind != "art" and ext in AUDIO_EXTENSIONS and slug in selected:
                    stale.setdefault(slug, set()).add(ext)
        albums = self.catalog.albums_by_slug(set(stale) | set(ups))
        for slug in set(stale) | set(ups):
            a = albums.get(slug)
            if a is None:
                continue
            want = self.catalog.wants(s, slug, a["formats"])
            old = (stale.get(slug, set()) | set((ups.get(slug) or {}).get("from") or ())) - want
            if not (old or slug in ups):
                continue
            if not self.catalog.album_totals(slug, a["formats"], s)["downloaded"]:
                continue
            new = sorted((e for e in want if e != "art"), key=self._quality)
            why = "Replaced by " + "/".join(e[1:].upper() for e in new) if new else "Format no longer synced"
            out[slug] = (old, why, ups.get(slug))
        return out

    async def _finish_upgrades(self) -> None:
        """After a sync: albums whose upgrade (or, with `replace_formats`, whose new formats) are fully
        downloaded lose their music files of the old formats. Images are never touched here."""
        from . import cleanup
        s = self.catalog.settings()
        ups = dict(s.get("album_upgrades") or {})
        changed = False
        for slug in [k for k in ups if self.catalog.album(k) is None]:
            ups.pop(slug)
            changed = True
        todo = self._replacements(s)
        names = self.catalog.albums_by_slug(todo)
        for slug, (old, why, up) in sorted(todo.items()):
            a = self.catalog.album(slug)
            keep = self.catalog.wants(s, slug, a["formats"]).stand_ins if a else frozenset()
            files = [{"path": rel, "url": url, "size": size or 0, "reason": "replaced"}
                     for url, rel, _al, ext, kind, size, _n in self.catalog.saved_files(slug)
                     if kind != "art" and ext in old and url not in keep]   # stand-ins stay
            res = await asyncio.to_thread(cleanup.delete, self.data_dir, files)
            gone = set(res["deleted"])
            for f in files:
                if f["path"] in gone:
                    self.catalog.add_change(None, "deleted", f["path"], f["size"], why)
            self.catalog.forget_local([f["url"] for f in files if f["path"] in gone])
            name = names[slug]["name"] if slug in names else slug
            if res["errors"]:
                log.warning("%s: %d old files could not be deleted: %s", name, len(res["errors"]),
                            "; ".join(res["errors"][:5]))
                continue                        # tried again after the next sync
            if files:
                log.info("%s of %s: deleted %d %s files (%s)", "Upgrade" if up else "New format", name,
                         len(gone), "/".join(e[1:].upper() for e in sorted(old)), fmt_bytes(res["bytes"]))
            if up:
                log.info("Upgrade of %s done: %s", name, up["to"][1:].upper())
                ups.pop(slug, None)
                changed = True
        if changed:
            self.catalog.save_settings({"album_upgrades": ups})
        self.catalog.commit()
        self._sel_cache = None

    def _finished_albums(self) -> tuple:
        """(settings, ticked albums, ticked albums with downloaded files) - for the steps at the end of
        a sync, worked out once instead of by each of them. Not only complete albums: one file that
        failed (a broken scan on khinsider) mustn't keep the rest from its cover, tags and folder.jpg."""
        s = self.catalog.settings()
        if not (s.get("playlists") or s.get("folder_art") or s.get("embed_cover") or s.get("write_tags")
                or self.catalog.extras()):
            return s, set(), []
        selected = set(self.catalog.selected_albums(None, s.get("platform_types") or []))
        albums = self.catalog.albums_by_slug(selected)
        done = [a for a, d in albums.items()
                if d["read"] and self.catalog.album_totals(a, d["formats"], s)["synced"]]
        return s, selected, done

    async def _write_extras(self, client=None, finished=None, recheck=frozenset()) -> None:
        """Playlists and folder images of the ticked, downloaded albums (extras.py). Never fails a sync."""
        from . import extras
        from .cleanup import remove_empty_dirs
        try:
            s, selected, done = finished or self._finished_albums()
            have = self.catalog.extras()
            if not (s.get("playlists") or s.get("folder_art") or have):
                return
            want = extras.plan(self, s, done)
            mode = int(parse_mode(s.get("file_mode", "")), 8) if parse_mode(s.get("file_mode", "")) else None
            # albums that got files this sync: their playlist / folder image is looked for on disk too
            # (written before, then deleted with the album's files?) - the others aren't touched
            gone = await asyncio.to_thread(extras.missing, self.data_dir, have, want, set(recheck))
            for rel in gone:
                have.pop(rel, None)
            if gone:
                self.catalog.drop_extras(gone)
            fetched = await extras.fetch_covers(client, want, have, self._fetch_failed) if client is not None else {}
            written, removed, dirs = await asyncio.to_thread(extras.apply, self.data_dir, want, have, selected, mode,
                                                             fetched)
            for rel, (album, kind, sig) in written.items():
                self.catalog.set_extra(rel, album, kind, sig)
            self.catalog.drop_extras(removed)
            if dirs:
                await asyncio.to_thread(remove_empty_dirs, self.data_dir, dirs)
            if written or removed:
                kinds = collections.Counter(want[r][1] for r in written)
                self._note("Playlists and folder images: %d playlists and %d folder images written, %d removed",
                         kinds.get("playlist", 0), kinds.get("folder_art", 0), len(removed))
        except Exception as e:
            log.warning("Could not write the playlists / folder images: %r", e)

    async def _tag_files(self, client, manifest, finished=None) -> None:
        """Into each ticked, downloaded album's music files: its cover (`embed_cover`) and the tags
        they lack (`write_tags`) - covers.py. Each file once; never fails a sync."""
        from . import covers
        s = self.catalog.settings()
        want_cover, want_tags = bool(s.get("embed_cover")), bool(s.get("write_tags"))
        if not (want_cover or want_tags):
            return
        if not covers.available():
            log.warning("Covers and tags can't be put into the music files: the mutagen package is missing "
                        "(pip install mutagen)")
            return
        try:
            s, _selected, done = finished or self._finished_albums()
            todo = self.catalog.untagged(done, want_cover, want_tags)
            if not todo:
                return
            root = self.data_dir
            with self.catalog.lock:
                firsts = {a: (url, rel, local) for a, url, rel, local in self.catalog.db.execute(
                    "SELECT album, url, rel, local FROM files WHERE kind='art' ORDER BY album, pos DESC")}
            albums = self.catalog.albums_by_slug({f["album"] for f in todo})
            counts = collections.Counter()
            self.job["phase"] = "putting covers and tags into the music files"
            by_album: dict = {}
            for f in todo:
                by_album.setdefault(f["album"], []).append(f)
            for slug, files in by_album.items():
                if self.stop_requested:
                    break
                a = albums.get(slug) or {}
                cover = None
                if want_cover and any(f["cover"] is None for f in files):
                    if time.time() - self._cover_failed.get(slug, 0) < COVER_RETRY:
                        if not want_tags or all(f["tags"] is not None for f in files):
                            continue                 # tried not long ago: once a day, not every sync
                    else:
                        try:
                            cover = await asyncio.wait_for(self._cover_bytes(client, firsts.get(slug), root),
                                                           COVER_TIMEOUT)
                        except asyncio.TimeoutError:
                            cover = None
                        if cover is None and firsts.get(slug):
                            self._cover_failed[slug] = time.time()
                        elif cover is not None:
                            self._cover_failed.pop(slug, None)
                        if cover is None:
                            counts["in albums without images" if not firsts.get(slug)
                                   else "waiting for their cover (couldn't load it - tried again in a day)"] += len(files)
                layout = self.catalog.track_layout(slug) if want_tags else {}
                discs = sorted({d for (_e, d) in layout if d})

                def tags_of(f) -> dict:
                    tracks = layout.get((f["ext"], f["disc"] or ""), [])
                    num = (f["num"] or "").strip().rstrip(".")
                    if not num.isdigit():
                        num = str(next((i for i, (_n, p) in enumerate(tracks, 1) if p == f["pos"]), 0) or "")
                    disc = (f["disc"] or "").strip()
                    year = a.get("year") or ""
                    return {"title": (f["title"] or "").strip() or os.path.splitext(f["name"] or "")[0],
                            "album": a.get("title") or a.get("name") or "",
                            "track": int(num) if num.isdigit() else None, "tracks": len(tracks) or None,
                            "disc": int(disc) if disc.isdigit() else None,
                            "discs": len(discs) if disc.isdigit() and discs else None,
                            "year": year if len(year) == 4 and year.isdigit() else None}

                no_cover = not firsts.get(slug)        # the album has no images at all

                cur = {"rel": None, "since": 0.0}            # the file being tagged now (the watchdog below)
                out: list = []
                halt = threading.Event()

                def tag_all(files=files, cover=cover, no_cover=no_cover, cur=cur, out=out, halt=halt) -> None:
                    for f in files:
                        if self.stop_requested or halt.is_set():
                            break                        # Stop works file by file, not only between albums
                        if f["rel"] in self._tag_stuck:
                            continue                     # hung before: left as it is
                        cur["rel"], cur["since"] = f["rel"], time.monotonic()
                        path = os.path.join(root, *f["rel"].split("/"))
                        c = cover if (f["cover"] is None and want_cover) else None
                        t = tags_of(f) if (f["tags"] is None and want_tags) else None
                        if c is None and t is None:      # nothing to do now - no need to open the file
                            out.append((f, "skipped" if (no_cover and want_cover and f["cover"] is None) else None,
                                        None, None))
                            continue
                        try:
                            rc, rt = covers.update(path, f["ext"], c, t)
                        except Exception as e:  # a file mutagen can't read stays as it was
                            log.warning("could not tag %s: %s", f["rel"], e)
                            rc, rt = ("skipped" if c else None), ("skipped" if t is not None else None)
                        changed = "added" in (rc, rt)
                        out.append((f, rc, rt, os.path.getsize(path) if changed else None))
                    cur["rel"] = None

                # a thread can't be killed: when one file hangs (a stalled disk or share, a file mutagen
                # can't get through), the sync stops waiting for it, names it in the log and carries on
                task = asyncio.ensure_future(asyncio.to_thread(tag_all))
                warned = None
                while not task.done():
                    await asyncio.wait({task}, timeout=2)
                    rel, took = cur["rel"], time.monotonic() - cur["since"]
                    if task.done():
                        break
                    if self.stop_requested:
                        halt.set()
                        await asyncio.wait({task}, timeout=1)   # a file under way gets a moment to finish
                        break
                    if rel and took >= TAG_FILE_WARN and warned != rel:
                        warned = rel
                        log.warning("Still putting the cover / tags into %s after %d s - the download folder "
                                    "may be very slow", rel, took)
                    if rel and took >= TAG_FILE_LIMIT:
                        self._tag_stuck.add(rel)
                        halt.set()
                        log.warning("Gave up on %s after %d minutes - it stays as it is (not tagged); the sync "
                                    "carries on. If the download folder is on a disk or share that stopped "
                                    "answering, check it.", rel, took // 60)
                        break
                for f, rc, rt, size in list(out):
                    self.catalog.set_tagged(f["url"], rc, rt, size)
                    if size is not None:
                        manifest.resized(f["rel"], size)
                    if rc:
                        counts["cover " + rc] += 1
                    if rt:
                        counts["tags " + rt] += 1
            manifest.commit()
            self.catalog.commit()
            self._sel_cache = None
            parts = []
            if want_cover:
                parts.append(f"cover put into {counts['cover added']} files ({counts['cover had']} already had one)")
            if want_tags:
                parts.append(f"missing tags filled in for {counts['tags added']} files "
                             f"({counts['tags had']} were complete)")
            extra = "".join(f", {n} {k}" for k, n in counts.items()
                            if k not in ("cover added", "cover had", "tags added", "tags had") and n)
            self._note("Music files: %s%s", "; ".join(parts), extra)
        except Exception as e:
            log.warning("Could not put covers / tags into the music files: %r", e)

    async def _cover_bytes(self, client, first, root: str) -> Optional[tuple]:
        """The album's cover (its first image) as (data, mime): from the download folder when it
        was synced, else fetched. None when there's none or it couldn't be loaded."""
        from . import covers
        if not first:
            return None
        data = None
        if first[2] in SYNCED_WORDS:
            try:
                with open(os.path.join(root, *first[1].split("/")), "rb") as f:
                    data = f.read()
            except OSError:
                data = None
        if data is None and client is not None:
            try:
                r = await client.get(first[0])
                data = r.content if r.status_code == 200 else None
            except Exception:
                data = None
        mime = covers.mime_of(data or b"")
        return (data, mime) if mime else None

    async def check_account(self) -> dict:
        """Sign in to khinsider now with the account from Settings (the Check button) -> account status."""
        async with self._client() as client:
            acct = account_mod.Account(self.catalog, self.base, client)
            try:
                await acct.sign_in()
                st = self.catalog.settings().get("account_status") or {}
                if st.get("state") not in ("ok", "no_mass"):
                    acct._status("signed_in", "signed in - whether it may download whole albums shows at the next sync")
            except ChallengeError:
                acct._status("failed", "khinsider asked for a browser check - try again in a minute")
            except (account_mod.AccountError, httpx.HTTPError) as e:
                acct._status("failed", str(e))
        s = self.catalog.settings()
        return dict(s.get("account_status") or {}, configured=account_mod.configured(s),
                    user=(s.get("account_user") or "").strip())

    async def check_flaresolverr(self) -> dict:
        """Is the FlareSolverr from Settings there? (the Test button) -> {ok, version, message, url}"""
        url = self.flaresolverr_url()
        if not url:
            return {"ok": False, "version": "", "message": "no FlareSolverr address set", "url": ""}
        res = await solver.FlareSolverr(url).health()
        return dict(res, url=url, solves=self.solve_state.solves, pages=self.solve_state.pages)

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
        # how many files to expect: what the app saved (other files in the folder add to it)
        expected = len(self.catalog.saved_files()) + len(self.catalog.extras())
        prog = self.cleanup_progress = {"files": 0, "folders": 0, "expected": expected, "started": time.time(),
                                        "step": "walking"}

        def run():
            disk = cleanup.walk(s["data_dir"], prog)
            prog["step"] = "judging"
            return cleanup.find(self, s, disk)
        try:
            files = await asyncio.to_thread(run)
        finally:
            self.cleanup_progress = None
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
        self.catalog.drop_extras([f["path"] for f in files if f.get("extra") and f["path"] in gone])
        log.info(cleanup.describe(res))
        await self.find_leftovers()
        return res

    async def delete_album(self, slug: str, confirm: bool = False) -> dict:
        """Delete an album from the download folder and the queue: it's unticked (an album rule out, so a
        ticked platform doesn't bring it back) and the files this app saved for it go - its music and
        images (the download record's or the catalog's), unfinished downloads of them, its playlist and
        folder image, and the folders left empty. Other files in its folder are never touched.
        Without `confirm` it only says what would go. -> {slug, files, bytes, deleted, errors}"""
        from . import cleanup
        a = self.catalog.album(slug)
        if a is None:
            raise KeyError(slug)
        root = self.data_dir
        listed = self.catalog.files_of([slug])
        urls = {r[0] for r in listed}
        rels = {r[1] for r in self.catalog.saved_files(slug)}
        if os.path.exists(os.path.join(root, MANIFEST_NAME)):   # the download record, when there is one
            m = Manifest(root)
            try:
                rels |= {path for url, path in m.by_url().items() if url in urls}
            finally:
                m.close()
        extras = [rel for rel, (album, _k, _s) in self.catalog.extras().items() if album == slug]

        def look() -> list:
            out = []
            # what it saved (and its unfinished downloads); of files never finished, only the .part
            paths = rels | set(extras) | {r + ".part" for r in rels} | {r[1] + ".part" for r in listed}
            for path in sorted(paths):
                try:
                    size = os.stat(os.path.join(root, *path.split("/"))).st_size
                except OSError:
                    continue
                out.append({"path": path, "size": size})
            return out
        files = await asyncio.to_thread(look)
        out = {"slug": slug, "name": a["name"], "files": len(files), "bytes": sum(f["size"] for f in files),
               "deleted": 0, "errors": []}
        if not confirm:
            return out
        res = await asyncio.to_thread(cleanup.delete, root, files)
        gone = set(res["deleted"])
        for f in files:
            if f["path"] in gone and not f["path"].endswith(".part"):
                self.catalog.add_change(None, "deleted", f["path"], f["size"], "Album deleted")
        self.catalog.select(ALBUM + slug, False)          # off the queue: not synced again
        self.catalog.forget_local(urls)
        self.catalog.drop_extras([r for r in extras if r in gone])
        self.catalog.commit()
        self._sel_cache = None
        self.catalog.changed(albums=True)
        log.info("Deleted the album %s: %d files (%s) from the download folder; unticked%s", a["name"],
                 len(gone), fmt_bytes(res["bytes"]),
                 f" - {len(res['errors'])} could not be deleted: " + "; ".join(res["errors"][:3]) if res["errors"] else "")
        await self.find_leftovers()
        return dict(out, deleted=len(gone), bytes=res["bytes"], errors=res["errors"])

    # ------------------------------------------------------------------ schedule
    def _schedule_next(self) -> None:
        s = self.catalog.settings()
        hours = float(s.get("sync_every_hours") or 0)
        if hours <= 0:
            self.next_run = None
            return
        last = next((h for h in self.catalog.history(50) if h["kind"] in ("sync", "scan+sync", "quick+sync")), None)
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

    def _sample_speed(self, total: int) -> bool:
        """Speed and time left from the bytes downloaded so far (at most once a second) -> sampled?"""
        now = time.monotonic()
        if self._samples and now - self._samples[-1][0] < 1:
            return False
        self._samples.append((now, total))
        t0, b0 = self._samples[0]
        self.speed = (total - b0) / (now - t0) if now > t0 else 0.0
        self._eta_samples.append((now, total))
        t1, b1 = self._eta_samples[0]
        self.eta_speed = (total - b1) / (now - t1) if now - t1 >= 3 else 0.0
        return True

    async def _from_account_list(self, client, robots) -> dict:
        """"Download my khinsider list": every album on the account's list is ticked and synced - its zip
        straight from the list (no add_album), then taken off the list."""
        s = self.catalog.settings()
        if not account_mod.configured(s):
            log.warning("No khinsider account with album zips in Settings - nothing to take from a list")
            return {"files": 0}
        acct = account_mod.Account(self.catalog, self.base, client, self.page_rate)
        self.job["phase"] = "reading your khinsider list"
        try:
            listed = await acct.mass_list()
        except ChallengeError:
            self.job["message"] = "khinsider asks for a browser check - see the log"
            return {"challenged": True}
        except (account_mod.AccountError, httpx.HTTPError) as e:
            log.warning("Couldn't read your khinsider list: %s", e)
            return {"files": 0}
        self._job_list = listed
        known = self.catalog.albums_by_slug(list(listed))
        missing = [sl for sl in listed if sl not in known]
        if missing:
            log.warning("%d albums on your khinsider list aren't in the catalog yet (scan the site first): %s",
                        len(missing), ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else ""))
        slugs = [sl for sl in listed if sl in known]
        selected = set(self.catalog.selected_albums())
        for sl in slugs:
            if sl not in selected:
                self.catalog.select(ALBUM + sl, True)    # on the list = wanted: ticked like any other
        self._sel_cache = None
        log.info("Your khinsider list: %d albums", len(slugs))
        if not slugs:
            return {"files": 0}
        return await self._sync(client, robots, slugs)

    async def _album_zips(self, client, s, todo, wants, albums, kinds, dl, manifest, on_done) -> list:
        """Albums whose files to fetch are all of one format (MP3 or FLAC, plus images) come as the
        album's zip from a paid khinsider account (account.py), one album after another: download,
        unpack under the app's own names; taking it off the account's list waits for the end of the
        queue (`_tidy_list`). -> the items left
        for the file-by-file downloads (other formats, files a zip lacks, or no zips any more)."""
        acct = self.account = account_mod.Account(self.catalog, self.base, client, self.page_rate,
                                                  listed=self._job_list)
        try:
            self._job_list = await acct.mass_list()      # once a job: albums on it need no add_album
        except ChallengeError:
            acct.off = "khinsider asked for a browser check"
        except (account_mod.AccountError, httpx.HTTPError) as e:
            acct.off = str(e)
            log.warning("Couldn't read your khinsider list (%s) - downloading file by file", e)
        by_album: dict = {}
        for it in todo:
            by_album.setdefault(kinds[it[0]][0], []).append(it)
        ids = self.catalog.album_ids(by_album)

        def is_art(it):
            return kinds[it[0]][1] == "art"
        names = self.catalog.file_names(it[0] for it in todo)
        left: list = []
        done_albums = 0
        for slug, items in by_album.items():
            want = wants.get(slug)
            audio = sorted({it[2].ext for it in items if not is_art(it)} &
                           (set(want or ()) - {"art"}))
            fmt = audio[0] if len(audio) == 1 and audio[0] in (".mp3", ".flac") else None
            if acct.off or self.stop_requested or not fmt or slug not in ids:
                left += items
                continue
            mine = [it for it in items if is_art(it) or it[2].ext == fmt]
            rest = [it for it in items if it not in mine]
            try:
                self.job["phase"] = "downloading album zips"
                got = await self._one_zip(acct, slug, ids[slug], fmt, mine, names, dl, manifest, on_done)
            except ChallengeError:
                acct.off = "khinsider asked for a browser check"
                got = set()
            except account_mod.NoMassDownloads as e:
                acct.off = str(e)
                log.warning("Your khinsider account can't download whole albums now (%s) - downloading file "
                            "by file", e)
                got = set()
            except (account_mod.AccountError, httpx.HTTPError, OSError, zipfile.BadZipFile) as e:
                log.warning("Album zip of %s didn't work (%s) - its files come one by one", albums[slug]["name"], e)
                got = set()
            if got:
                done_albums += 1
            left += [it for it in mine if it[0] not in got] + rest
        if done_albums:
            self._note("%d albums came as zips from your khinsider account (%d page requests for them)",
                     done_albums, acct.requests)
        self.job["phase"] = "downloading"
        return left

    async def _one_zip(self, acct, slug, albumid, fmt, items, names, dl, manifest, on_done) -> set:
        """Download one album zip and unpack the files `items` asks for -> the catalog addresses saved."""
        zip_url = await acct.album_zip(albumid, fmt, slug)
        tmp = os.path.join(self.data_dir, ".khinsider-sync-zips")
        os.makedirs(tmp, exist_ok=True)
        part = os.path.join(tmp, f"{slug}{'.flac' if fmt == '.flac' else ''}.zip.part")
        a = self.catalog.album(slug) or {}
        # shown in the Downloading box like a file: the album's folder, then what's coming
        label = f"{self.album_dir(a, self.catalog.settings()) if a else slug}/Album zip ({fmt[1:].upper()})"
        have = os.path.getsize(part) if os.path.exists(part) else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        async with self.client_stream(dl.client, zip_url, headers) as r:
            if r.status_code == 416:
                pass                                     # already complete
            elif r.status_code not in (200, 206) or "html" in r.headers.get("content-type", ""):
                raise account_mod.AccountError(f"the zip answered HTTP {r.status_code}")
            else:
                total = int(r.headers.get("content-length") or 0) + (have if r.status_code == 206 else 0)
                # the zip and its unpacked files side by side for a moment, and the free space to keep
                need = 2 * total + int(parse_size(str(self.catalog.settings().get("min_free") or "0")))
                if total and shutil.disk_usage(tmp).free < need:
                    raise account_mod.AccountError(f"not enough free space for its zip ({fmt_bytes(total)})")
                dl.active[label] = [have if r.status_code == 206 else 0, total]
                with open(part, "ab" if r.status_code == 206 else "wb") as f:
                    async for chunk in r.aiter_bytes(256 * 1024):
                        f.write(chunk)
                        dl.bytes += len(chunk)
                        dl.active[label][0] += len(chunk)
                        self._sample_speed(dl.bytes)
                        if self.stop_requested:
                            break
        if self.stop_requested:
            dl.active.pop(label, None)
            return set()                                 # the .part stays: resumed next time
        self.job["phase"] = "downloading album zips - unpacking"

        def unpack() -> list:
            saved = []
            with zipfile.ZipFile(part) as z:
                members = {}
                for m in z.infolist():
                    if m.is_dir():
                        continue
                    base = m.filename.replace("\\", "/").rsplit("/", 1)[-1]
                    members.setdefault(base, m)
                    members.setdefault(safe_segment(base), m)
                for url, rel, info, action, _reason in items:
                    m = members.get(names.get(url) or "")
                    if m is None:
                        continue
                    dest = dl.dest_of(rel)
                    dl._make_dirs(os.path.dirname(dest))
                    with z.open(m) as src, open(dest + ".part", "wb") as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
                    os.replace(dest + ".part", dest)
                    stamp_file(dest, info, None)
                    dl._set_mode(dest)
                    manifest.done(rel, url, m.file_size, None, None, info)
                    saved.append((url, rel, DONE_WORD.get(action, "new"), m.file_size))
            return saved
        try:
            saved = await asyncio.to_thread(unpack)
        finally:
            dl.active.pop(label, None)
            self.job["phase"] = "downloading album zips"
        manifest.commit()
        for url, rel, word, size in saved:
            on_done(url, rel, word, size)
        os.remove(part)
        # taken off the khinsider list later (`_tidy_list`): one page read each, not in the busy queue
        tidy = list(self.catalog.settings().get("account_tidy") or [])
        if albumid not in tidy:
            self.catalog.save_settings({"account_tidy": tidy + [albumid]})
        return {u for u, *_ in saved}

    async def _tidy_list(self, client) -> int:
        """Take the albums saved from zips off the account's "My Mass Downloads" list - after the queue,
        paced like every page read (khinsider has no way to remove several at once)."""
        s = self.catalog.settings()
        ids = [int(a) for a in (s.get("account_tidy") or [])]
        if not ids or not account_mod.configured(s):
            return 0
        acct = account_mod.Account(self.catalog, self.base, client, self.page_rate, listed=self._job_list)
        self.job["phase"] = "tidying up your khinsider list"
        done = 0
        try:
            for aid in ids:
                if self.stop_requested:
                    break
                await acct.remove(aid)
                done += 1
        except ChallengeError:
            log.info("khinsider asked for a browser check while tidying up your list - the rest waits for "
                     "the next sync")
        finally:
            left = [a for a in (self.catalog.settings().get("account_tidy") or []) if int(a) not in set(ids[:done])]
            self.catalog.save_settings({"account_tidy": left})
        if done:
            log.info("Took %d downloaded albums off your khinsider list%s", done,
                     f" ({len(left)} left for the next sync)" if left else "")
        return done

    @staticmethod
    def client_stream(client, url, headers):
        return client.stream("GET", url, headers=headers, timeout=httpx.Timeout(60.0, connect=15.0))

    def _challenge_backoff(self, fresh: bool = False) -> None:
        """khinsider's Cloudflare asked for a browser check: no more requests for a while, then the
        sync tries again by itself - 1 min, then 10, 20, 40 ... up to 2 h while it keeps happening.
        Never anything to get around the check: waiting is all it does."""
        # a sync that got files before the check isn't "another check in a row": start from 1 minute
        n = 0 if fresh else int(self.catalog.settings().get("challenge_retries") or 0)
        wait = CHALLENGE_FIRST if n == 0 else min(CHALLENGE_WAIT * 2 ** (n - 1), CHALLENGE_WAIT_MAX)
        at = time.time() + wait
        self.catalog.save_settings({"queue_resume_at": at, "challenge_retries": n + 1})
        if self.job is not None:
            self.job["message"] = "khinsider asks for a browser check - trying again at " + time.strftime("%H:%M", time.localtime(at))
        log.warning("Paused for %s because of khinsider's browser check: the sync tries again by itself at %s%s",
                    _span(wait), time.strftime("%a %H:%M", time.localtime(at)),
                    "" if n == 0 else f" (check {n + 1} in a row - the wait doubles each time, up to {_span(CHALLENGE_WAIT_MAX)})")

    def resume_queue(self) -> bool:
        """A files-per-hour / albums-per-day limit or a browser check stopped the last sync and it's
        time again: carry on (called by the scheduler every 30 s)."""
        s = self.catalog.settings()
        resume = float(s.get("queue_resume_at") or 0)
        if not resume or time.time() < resume or self.busy:
            return False
        self.catalog.save_settings({"queue_resume_at": 0})
        log.info("Trying again after khinsider's browser check" if int(s.get("challenge_retries") or 0)
                 else "Carrying on with the queue (a limit had stopped the last sync)")
        return self.start("sync", scheduled=True)

    async def scheduler_loop(self, resume_delay: float = 3.0) -> None:
        self._schedule_next()
        await asyncio.sleep(resume_delay)
        self.resume_interrupted()
        while True:
            await asyncio.sleep(30)
            if self.resume_queue():
                continue
            if self.next_run and time.time() >= self.next_run and not self.busy:
                s = self.catalog.settings()
                days = float(s.get("full_scan_every_days") or 0)
                full = self.catalog.last_scan("full")
                need_full = days > 0 and (not full or time.time() - (full["finished"] or 0) > days * 86400)
                log.info("Scheduled run: %s", "album list scan + sync" if need_full else "newest albums + sync")
                self.start("scan+sync" if need_full else "quick+sync", scheduled=True)

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
        sel = {"albums": len(slugs), "unread": 0, "downloaded": 0, "files": 0, "bytes": 0, "synced": 0,
               "synced_bytes": 0, "failed": 0}
        for slug, a in albums.items():
            if not a["read"]:
                sel["unread"] += 1
                continue
            t = self.catalog.album_totals(slug, a["formats"], s)
            for k in ("files", "bytes", "synced", "synced_bytes", "failed"):
                sel[k] += t[k]
            sel["downloaded"] += t["downloaded"]
        self._sel_cache = (key, dict(sel))
        return sel

    def _queue_status(self) -> Optional[dict]:
        """The ticked albums still to download and, while the queue runs, roughly how long they'll take."""
        sel = self.selection()
        left = max(0, sel["albums"] - sel["downloaded"])
        if not left:
            return None
        pace = self.queue_pace()
        going = self.busy or bool(float(self.catalog.settings().get("queue_resume_at") or 0))
        return {"albums_left": left, "per_album": round(1 / pace, 1) if pace else None,
                "eta": round(left / pace) if pace and going else None}

    def _note(self, msg: str, *args) -> None:
        """Housekeeping lines of a round (albums read, folders dated, covers ...): in the log for the first
        round of a job; later rounds of a long queue put them at debug level - `_queue_summary` sums up."""
        log.log(logging.DEBUG if self._round > 1 else logging.INFO, msg, *args)

    def _queue_summary(self, sync: dict, started: float, final: bool = False) -> None:
        q = self._queue_status() or {}
        log.info("Queue %s: %d rounds, %d albums, %d files new, %d updated, %d failed, %s in %s%s",
                 "done" if final else "so far", sync.get("batches") or 1, len(sync.get("got_albums") or []),
                 sync.get("new") or 0, sync.get("updated") or 0, sync.get("failed") or 0,
                 fmt_bytes(sync.get("bytes") or 0), _span(time.time() - started),
                 f" - {q['albums_left']:,} albums to go" + (f", about {_span(q['eta'])} at this pace" if q.get("eta") else "")
                 if q.get("albums_left") else "")

    def queue_pace(self) -> Optional[float]:
        """Albums a second the queue has been going at lately: its rounds over the last hour, as long as
        they followed one another (a wait of more than 10 minutes - a browser check, a limit - starts over).
        None until there are three rounds to go by."""
        pts = list(self._pace)
        if len(pts) < 3:
            return None
        tail = [pts[-1]]
        for p in reversed(pts[:-1]):
            if tail[0][0] - p[0] > 600 or pts[-1][0] - p[0] > 3600 or p[1] < tail[0][1]:
                break
            tail.insert(0, p)
        if len(tail) < 3:
            return None
        dt, dn = tail[-1][0] - tail[0][0], tail[0][1] - tail[-1][1]
        return dn / dt if dt > 0 and dn > 0 else None

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
            "queue_resume_at": float(s.get("queue_resume_at") or 0) or None,
            "challenge_retries": int(s.get("challenge_retries") or 0),
            "account": dict(s.get("account_status") or {}, configured=account_mod.configured(s),
                            user=(s.get("account_user") or "").strip()),
            "limits": {k: int(s.get(k) or 0) for k in ("pages_per_minute", "files_per_hour", "albums_per_day")},
            "leftovers": self.leftovers,
            "queue": self._queue_status(),
            "data_dir": s["data_dir"],
            "version_key": self.catalog.version,
            "log": self.logbuf.since(log_since),
        }
        if self.scanner is not None:
            out["scan"] = self.scanner.progress()
        if self.cleanup_progress is not None:
            p = dict(self.cleanup_progress)
            spent = time.time() - p["started"]
            rate = p["files"] / spent if spent > 1 and p["files"] else 0
            p["eta"] = round(max(0, p["expected"] - p["files"]) / rate) if rate and p["step"] == "walking" else None
            out["cleanup"] = p
        if self.check_progress is not None and self.busy:
            p = dict(self.check_progress)
            spent = time.time() - p["started"]
            rate = p["done"] / spent if spent > 1 and p["done"] else 0
            p["eta"] = round((p["total"] - p["done"]) / rate) if rate else None
            out["check"] = p
        phase = (self.job or {}).get("phase") or ""
        if self.downloader is not None and (phase.startswith("downloading") or phase in ("done", "stopped")):
            dl, g = self.downloader, self.gate
            st = self.sync_stats
            active = list(dl.active.items())
            # what this sync still has to transfer: planned sizes of unfinished files minus what the
            # files in progress already have on disk (page sizes are rounded, so it's an estimate)
            left = max(0, st["bytes_planned"] - st["bytes_settled"] - sum(h for _, (h, _t) in active))
            running = self.busy and phase.startswith("downloading")
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


# waits after a browser check: 1 min, then 10 min doubling while it keeps happening, at most 2 h. The
# owner's log (October 2026): mostly a single page gets the check - opening an album 18 s later worked;
# after a heavy night it lasted hours - 2 h between tries costs one page request each
CHALLENGE_FIRST = 60
CHALLENGE_WAIT = 600
CHALLENGE_WAIT_MAX = 2 * 3600


def _span(secs: float) -> str:
    if secs < 120:
        return f"{secs / 60:g} minute"
    return f"{secs / 3600:g} hours" if secs >= 3600 * 2 else f"{secs / 3600:g} hour" if secs >= 3600 else f"{secs / 60:g} minutes"


_RUN_SUMS = ("total", "albums", "to_fetch", "new", "updated", "resumed", "up to date", "already in folder",
             "failed", "bytes", "track_pages")


def _add_runs(a: dict, b: dict) -> dict:
    """One job's sync summary over several batches: counts add up, the last batch says what's next."""
    out = dict(b)
    for k in _RUN_SUMS:
        if isinstance(a.get(k), (int, float)) or isinstance(b.get(k), (int, float)):
            out[k] = (a.get(k) or 0) + (b.get(k) or 0)
    out["got_albums"] = sorted(set(a.get("got_albums") or []) | set(b.get("got_albums") or []))
    out["batches"] = (a.get("batches") or 1) + 1
    return out


__all__ = ["Engine", "ALBUM", "PLATFORM", "clean_place", "parse_mode", "LAYOUTS"]
