"""Keeping a download folder in sync with the site.

An album page shows each track's size per format ("5.64 MB") and the album's "Date Added". When a
file is downloaded, what the page said is remembered in a manifest inside the download folder
(<folder>/.khinsider-sync.db) together with the server's Last-Modified / ETag. On the next run each
file is compared against the manifest and the disk, so only new, incomplete or changed files are
fetched - unchanged files cost no request at all.
"""
from __future__ import annotations

import calendar
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from email.utils import formatdate, parsedate_to_datetime
from typing import Optional

log = logging.getLogger("dircrawl")
_stamp_warned = False

MANIFEST_NAME = ".khinsider-sync.db"

_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?(?!\d)")
_SIZE_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(b|bytes?|k|m|g|t|kb|mb|gb|tb|kib|mib|gib|tib)\s*$", re.I)
_UNITS = {"": 0, "b": 0, "byte": 0, "bytes": 0, "k": 1, "kb": 1, "kib": 1, "m": 2, "mb": 2, "mib": 2,
          "g": 3, "gb": 3, "gib": 3, "t": 4, "tb": 4, "tib": 4}

# plan() actions
NEW, RESUME, UPDATE, CHECK, SKIP, ADOPT = "new", "resume", "update", "check", "skip", "adopt"
FETCH_ACTIONS = (NEW, RESUME, UPDATE, CHECK)


@dataclass(frozen=True)
class FileInfo:
    """What the listing page says about one file."""
    ext: str
    date: Optional[str] = None      # normalised listing date, e.g. "2025-11-17" or "2025-11-17 08:30"
    size: Optional[str] = None      # listing size text, e.g. "3.54 KB"


# --------------------------------------------------------------------------- listing values
def parse_date(text: str) -> Optional[str]:
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    y, mo, d, hh, mm, ss = m.groups()
    out = f"{y}-{mo}-{d}"
    if hh:
        out += f" {hh}:{mm}" + (f":{ss}" if ss else "")
    return out


def is_size(text: str) -> bool:
    return bool(_SIZE_RE.match(text or ""))


_SIZE_SEARCH = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s?(bytes?|b|kib|mib|gib|tib|kb|mb|gb|tb|k|m|g|t)(?![\w])", re.I)


def find_size(text: str) -> Optional[str]:
    """First size in a piece of listing text: '2025-11-17 • 3.54 KB' -> '3.54 KB'."""
    for m in _SIZE_SEARCH.finditer(text or ""):
        unit = m.group(2)
        if unit.lower() in ("b", "byte", "bytes") or len(unit) > 1 or "." in m.group(1) or "," in m.group(1):
            return f"{m.group(1)} {unit}"
    return None


def size_range(text: str) -> Optional[tuple]:
    """Byte range a rounded listing size can stand for ("1.50 KB" -> (1531, 1541)).
    Units may be 1000- or 1024-based; both are accepted."""
    m = _SIZE_RE.match(text or "")
    if not m:
        return None
    num = m.group(1).replace(",", ".")
    power = _UNITS[m.group(2).lower()]
    value = float(num)
    decimals = len(num.split(".")[1]) if "." in num else 0
    half = 0.5 * 10 ** -decimals
    if power == 0:
        return int(value), int(value)
    lo = (value - half) * min(1000, 1024) ** power
    hi = (value + half) * max(1000, 1024) ** power
    return int(lo), int(hi + 1)


def size_matches(local: int, text: Optional[str]) -> Optional[bool]:
    rng = size_range(text) if text else None
    if rng is None:
        return None
    return rng[0] <= local <= rng[1]


def date_epoch(date: str) -> float:
    """Listing date as a UTC timestamp (start of the day when no time is shown)."""
    fmt = "%Y-%m-%d %H:%M:%S" if date.count(":") == 2 else "%Y-%m-%d %H:%M" if ":" in date else "%Y-%m-%d"
    return float(calendar.timegm(time.strptime(date, fmt)))


def site_timestamp(info: "FileInfo", last_modified: Optional[str] = None) -> Optional[float]:
    """The date to give a saved file so it matches what the website shows.

    The listing's date is the source of truth. The server's Last-Modified is only used when it
    falls on that same day (it adds the time of day). A date without a time becomes 12:00 local
    time, so Explorer / Finder show the same day as the website in any time zone."""
    lm = http_date_epoch(last_modified)
    if not info.date:
        return lm
    day = info.date[:10]
    if lm is not None and day in (time.strftime("%Y-%m-%d", time.gmtime(lm)),
                                  time.strftime("%Y-%m-%d", time.localtime(lm))):
        return lm
    if ":" in info.date:
        return date_epoch(info.date)
    return time.mktime(time.strptime(day + " 12:00", "%Y-%m-%d %H:%M"))


def stamp_file(path: str, info: "FileInfo", last_modified: Optional[str] = None, st=None) -> bool:
    """Set the file's modified date to the website's date. True if it was changed.
    `st`: a current os.stat of the file, if the caller has one (saves asking the disk again)."""
    stamp = site_timestamp(info, last_modified)
    if stamp is None:
        return False
    try:
        if abs((st or os.stat(path)).st_mtime - stamp) < 2:
            return False
        os.utime(path, (stamp, stamp))
        return True
    except OSError as e:
        global _stamp_warned
        if not _stamp_warned:            # say it once: the cause is the same for every file
            _stamp_warned = True
            log.warning("Could not set file dates to the website's dates (%s: %s). Check that the "
                        "download folder allows changing dates (owner/permissions of the share).",
                        path, e)
        return False


def stamp_folders(folders, root: str) -> int:
    """Give each folder (and its parents below root) the date of the newest thing inside it, so
    folders show when something in them last changed on the website, not when it was downloaded."""
    root = os.path.abspath(root)
    todo = set()
    for f in folders:
        f = os.path.abspath(f)
        while f.startswith(root + os.sep):
            todo.add(f)
            f = os.path.dirname(f)
    changed = 0
    for d in sorted(todo, key=lambda p: p.count(os.sep), reverse=True):   # deepest first
        newest = None
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.endswith(".part") or e.name.startswith(MANIFEST_NAME):
                        continue
                    try:
                        m = e.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        continue
                    newest = m if newest is None or m > newest else newest
            if newest is not None and abs(os.stat(d).st_mtime - newest) >= 2:
                os.utime(d, (newest, newest))
                changed += 1
        except OSError as e:
            log.debug("could not date folder %s: %s", d, e)
    return changed


def http_date_epoch(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


def http_date(epoch: float) -> str:
    return formatdate(epoch, usegmt=True)


# --------------------------------------------------------------------------- manifest
@dataclass
class Record:
    path: str
    url: str
    size: Optional[int]
    last_modified: Optional[str]
    etag: Optional[str]
    listing_date: Optional[str]
    listing_size: Optional[str]
    part_validator: Optional[str]     # Last-Modified / ETag of an unfinished .part, for If-Range


class Manifest:
    """What is in the download folder and what the site said about it when it was saved."""

    def __init__(self, folder: str):
        self.path = os.path.join(folder, MANIFEST_NAME)
        # check_same_thread off: a sync plans its files in a worker thread before any download
        # starts (never both at once)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, url TEXT, size INTEGER, last_modified TEXT, etag TEXT,
                listing_date TEXT, listing_size TEXT, part_validator TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        """)
        self.rows = {r[0]: Record(*r) for r in self.db.execute(
            "SELECT path, url, size, last_modified, etag, listing_date, listing_size, part_validator "
            "FROM files")}

    def get(self, path: str) -> Optional[Record]:
        return self.rows.get(path)

    def _save(self, rec: Record) -> None:
        self.rows[rec.path] = rec
        self.db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                        (rec.path, rec.url, rec.size, rec.last_modified, rec.etag, rec.listing_date,
                         rec.listing_size, rec.part_validator, time.time()))

    def done(self, path, url, size, last_modified, etag, info: FileInfo) -> None:
        self._save(Record(path, url, size, last_modified, etag, info.date, info.size, None))

    def part_started(self, path, url, validator: Optional[str]) -> None:
        old = self.rows.get(path)
        rec = Record(path, url, old.size if old else None, old.last_modified if old else None,
                     old.etag if old else None, old.listing_date if old else None,
                     old.listing_size if old else None, validator)
        self._save(rec)

    def seen_unchanged(self, path, info: FileInfo) -> None:
        """Server said 304 / listing confirmed: refresh what the listing shows now."""
        old = self.rows.get(path)
        if old and (old.listing_date != info.date or old.listing_size != info.size):
            old.listing_date, old.listing_size = info.date, info.size
            self._save(old)

    def by_url(self) -> dict:
        """{url: path} of every saved file (to find files whose place in the folder changed)."""
        return {r.url: r.path for r in self.rows.values() if r.url}

    def move(self, old: str, new: str) -> None:
        """A saved file was moved to another place in the download folder."""
        rec = self.rows.pop(old, None)
        if rec is None:
            return
        self.db.execute("DELETE FROM files WHERE path=?", (old,))
        rec.path = new
        self._save(rec)

    def forget(self, paths) -> None:
        """Files that were deleted on purpose (Clean up)."""
        for path in paths:
            if self.rows.pop(path, None) is not None:
                self.db.execute("DELETE FROM files WHERE path=?", (path,))

    def get_meta(self, k):
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def set_meta(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))

    def commit(self):
        self.db.commit()

    def close(self):
        try:
            self.db.commit()
            self.db.close()
        except sqlite3.Error:
            pass


# --------------------------------------------------------------------------- decisions
def plan(dest: str, rec: Optional[Record], info: FileInfo, *, check_remote: bool = False,
         overwrite: bool = False, st=...) -> tuple:
    """Decide what to do with one listed file -> (action, reason). `st`: os.stat(dest) if the
    caller already has it (None = the file doesn't exist), so the disk is asked only once."""
    part = dest + ".part"
    if overwrite:
        return UPDATE, "download again requested"
    if os.path.exists(part):
        return RESUME, "unfinished download"
    if st is ...:
        try:
            st = os.stat(dest)
        except FileNotFoundError:
            st = None
    if st is None:
        return NEW, "new" if rec is None else "missing from the folder"
    size = st.st_size

    if rec is not None:                                  # we downloaded it before
        if rec.size is not None and size != rec.size:
            if size < rec.size and info.date == rec.listing_date and info.size == rec.listing_size:
                return RESUME, f"incomplete ({size} of {rec.size} bytes)"
            return UPDATE, "local copy differs from what was downloaded"
        if info.date and rec.listing_date and info.date != rec.listing_date:
            return UPDATE, f"changed on the site ({rec.listing_date} -> {info.date})"
        if info.size and rec.listing_size and info.size != rec.listing_size:
            return UPDATE, f"size changed on the site ({rec.listing_size} -> {info.size})"
        if info.size and size_matches(size, info.size) is False:
            return UPDATE, f"size on the site is {info.size}"
        if check_remote or (not info.date and not info.size and (rec.last_modified or rec.etag)):
            return CHECK, "asking the server whether it changed"
        return SKIP, "up to date"

    # Already in the folder but not downloaded by this tool (or the manifest was deleted).
    newer = info.date is not None and date_epoch(info.date) > st.st_mtime + 86400   # 1 day slack: no time zone shown
    if info.size and size_matches(size, info.size) is False:
        lo = size_range(info.size)[0]
        if size < lo and not newer:
            return RESUME, f"incomplete ({size} bytes, site shows {info.size})"
        return UPDATE, f"size on the site is {info.size}"
    if newer:
        return UPDATE, f"newer on the site ({info.date})"
    if check_remote or (not info.date and not info.size):
        return CHECK, "asking the server whether it changed"
    return ADOPT, "already in the folder"
