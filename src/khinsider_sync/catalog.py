"""The catalog: every album khinsider lists, the tracks of the albums that were read, the selection,
the settings and what has been synced - one SQLite file in the config folder.

A full scan reads the site's browse pages (A-Z, 0-9; 500 albums a page) into `albums`: name,
platforms, type and year. An album's tracks are only known once its own page was read (`read`),
which happens before it is synced, or when you open it. Each track is one row in `files` per format
(MP3, FLAC, ...), plus one row per album image.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Callable, Iterable, Optional
from urllib.parse import unquote, urlsplit

from .core.constants import AUDIO_EXTENSIONS, BEST_ORDER
from .core.sync import size_range

SYNCED_WORDS = ("new", "updated", "resumed", "up to date", "already in folder")

DEFAULT_SETTINGS = {
    "formats": "mp3",               # mp3 | flac | best | all | "first:.flac,.mp3,art" | "all:.flac,.mp3"
    "album_art": True,              # also save the album's images (covers, scans)
    "platform_types": [],           # album types a ticked platform brings in ([] = every type)
    "folder_layout": "album",       # album | platform | type | year  (folder above each album)
    "album_places": {},             # {album slug: folder in the download folder} - "Saved as"
    "album_formats": {},            # {album slug: formats} - this album's own formats setting
    "album_upgrades": {},           # {album slug: {to, from, formats, since}} - waiting upgrades (Engine.upgrade_album)
    "track_names": "site",          # site ("1-01. Title.mp3" as khinsider names it) | numbered ("1-01 - Title.mp3")
    "art_folder": False,            # images in an "Art" folder inside the album's folder
    "playlists": False,             # write "<album>.m3u8" next to a downloaded album's music (extras.py)
    "folder_art": False,
    "embed_cover": False,           # put the album's cover into its music files that have none (covers.py)
    "write_tags": False,            # fill in missing title / album / track / disc / year tags (covers.py)            # copy a downloaded album's cover to folder.jpg / folder.png (extras.py)
    "notify_url": "",               # ntfy / Gotify / Discord / Home Assistant webhook ... (notify.py); "" = off
    "notify_on": ["downloads", "new_albums", "problems"],
    "replace_formats": False,       # once a ticked album is downloaded in the formats now wanted, delete its
                                    # music files of formats no longer wanted (Engine._finish_upgrades)
    "impersonate": True,            # requests look like Chrome (needs curl_cffi)
    "parallel": "auto",             # auto | number
    "parallel_max": 8,
    "max_rate": "",                 # "" = unlimited, e.g. "5M"
    "min_free": "2G",
    "file_mode": "777",             # permissions for downloaded files and new folders; "" = system default
    "resume_interrupted": True,     # restart a sync that was cut off (crash, reboot, restart)
    "interrupted_job": "",          # set while a sync runs; cleared when it ends normally
    "outage_wait_hours": 12,        # how long a sync waits for the site when it's down
    "sync_every_hours": 0,          # 0 = off
    "full_scan_every_days": 7,      # with the schedule on: full scan when the last one is older (else only the newest albums)
    "pages_per_minute": 60,         # khinsider page requests a minute (album + track pages; 0 = no limit) - pacing.py
    "files_per_hour": 0,            # files downloaded in any hour (0 = no limit); the rest waits
    "albums_per_day": 0,            # albums started in any 24 hours (0 = no limit); the rest waits
    "albums_per_batch": 1,          # album pages a sync reads (one at a time) before downloading them; then the next batch
    "player_links": True,           # MP3 addresses from the album page's player: no track page for them
    "challenge_retries": 0,         # set by the app: browser checks in a row (each doubles the wait)
    "queue_resume_at": 0,           # set by the app: a limit stopped a sync - it carries on at this time
    "check_folder_days": 0,         # with the schedule on: check the download folder for deleted files before a
                                    # scheduled sync when the last check is this many days old (0 = never: disks stay asleep)
    "last_folder_check": 0,         # when the download folder was last checked (set by the app)
    "data_dir": "",                 # set at start-up from --data unless changed in the UI
}

ALBUM = "a:"          # selection keys: "a:<album slug>" ticks one album,
PLATFORM = "p:"       # "p:<platform slug>" every album of that platform (also ones added later)


def split_platforms(value: Optional[str]) -> list:
    return [p for p in (value or "").split("|") if p]


def join_platforms(slugs: Iterable[str]) -> str:
    slugs = [s for s in slugs if s]
    return "|" + "|".join(slugs) + "|" if slugs else ""


def size_bytes(text: Optional[str]) -> int:
    """Best estimate of a size shown on an album page, like '5.64 MB'."""
    rng = size_range(text) if text else None
    return (rng[0] + rng[1]) // 2 if rng else 0


PRESETS = ("mp3", "flac", "best", "all")
MODES = ("first", "all")


def parse_format_setting(text) -> tuple:
    """A formats setting -> (mode, [items in priority order] or None for a preset, preset or None).
      mp3 | flac | best | all          presets (see format_plan)
      first:.flac,.mp3,art             the first listed format an album has, in this order
      all:.flac,.mp3,art               every listed format an album has, downloaded in this order
      .mp3,.ogg                        (older form) the same as all:.mp3,.ogg
    "art" stands for the album's images. Raises ValueError for anything else."""
    s = str(text or "").strip().lower()
    if s in PRESETS:
        return None, None, s
    mode = "all"
    if ":" in s:
        mode, _, s = s.partition(":")
        mode = mode.strip()
        if mode not in MODES:
            raise ValueError("a format list starts with first: or all:")
    items = []
    for e in s.split(","):
        e = e.strip()
        if not e:
            continue
        e = "art" if e in ("art", "images", "image") else "." + e.lstrip(".")
        if e != "art" and e not in AUDIO_EXTENSIONS:
            raise ValueError(f"unknown format {e} (known: {', '.join(sorted(AUDIO_EXTENSIONS))}, art)")
        if e not in items:
            items.append(e)
    if not any(i != "art" for i in items):
        raise ValueError("pick at least one audio format")
    return mode, items, None


def format_plan(setting, album_formats: Iterable[str], art: bool) -> list:
    """Which of an album's files a sync saves, in the order they are downloaded: audio formats
    (".flac", ...) and "art" for its images.
      mp3   MP3 (or, when an album has no MP3, its first format, so nothing is left out)
      flac  FLAC (or, without it, MP3 / the first format)
      best  the best format the album has: FLAC, WAV, ... M4A, OPUS, OGG, AAC, then MP3
      all   every format the album offers
      first:<list>  the first format of the list the album has (nothing if it has none of them)
      all:<list>    every format of the list the album has
    Presets add the images (last) when `art` is on; a list has them where "art" stands in it."""
    have = [f for f in album_formats if f]
    mode, items, preset = parse_format_setting(setting or "mp3")
    if items is not None and ":" not in str(setting):
        # the older ".mp3,.ogg" form: these formats, the images as the album_art setting says
        out = [i for i in items if i in have and i != "art"]
        return out + (["art"] if art else [])
    if preset == "all":
        out = list(have)
    elif preset:
        pick = None
        if preset == "flac" and ".flac" in have:
            pick = ".flac"
        elif preset == "best":
            pick = next((f for f in BEST_ORDER if f in have), None)
        if pick is None:
            pick = ".mp3" if ".mp3" in have else (have[0] if have else None)
        out = [pick] if pick else []
    else:
        out = []
        picked = False
        for i in items:
            if i == "art":
                out.append("art")
            elif i in have and not (mode == "first" and picked):
                out.append(i)
                picked = True
        return out
    if art:
        out.append("art")
    return out


def format_fallback(setting, album_formats: Iterable[str]) -> tuple:
    """(the album's format, [formats to use instead, in order]) for settings that save one format of
    an album: a track the album has in other formats only - FLAC for most tracks, a few as MP3 - gets
    the first of these it has, so it isn't left out. (None, []) when every format is saved anyway."""
    have = [f for f in album_formats if f]
    mode, items, preset = parse_format_setting(setting or "mp3")
    if (items is not None and ":" not in str(setting)) or preset == "all" or mode == "all":
        return None, []
    plan = [k for k in format_plan(setting, have, False) if k != "art"]
    if not plan:
        return None, []
    pick = plan[0]
    if preset == "mp3":
        order = [".mp3"] + list(reversed(BEST_ORDER))   # the smaller formats first
    elif preset == "flac":
        order = [".flac", ".mp3"] + list(BEST_ORDER)
    elif preset:
        order = list(BEST_ORDER)
    else:                                              # first:<list> - only what the list names
        audio = [i for i in items if i != "art"]
        order = audio[audio.index(pick) + 1:] if pick in audio else []
    rest = []
    for f in order:
        if f in have and f != pick and f not in rest:
            rest.append(f)
    return pick, rest


class Want(frozenset):
    """What a sync saves of one album: its formats (and "art" for the images), plus `stand_ins` -
    the files of tracks the album's format is missing for (format_fallback). `"art" in want` and
    `ext in want` work as for a set; whether one file is wanted: want.has(url, ext, kind)."""

    def __new__(cls, keys=(), stand_ins=()):
        self = super().__new__(cls, keys)
        self.rows = dict(stand_ins) if isinstance(stand_ins, dict) else {u: None for u in stand_ins}
        self.stand_ins = frozenset(self.rows)
        return self

    def has(self, url: str, ext: str, kind: str) -> bool:
        return ("art" if kind == "art" else ext) in self or url in self.stand_ins


def format_rank(setting, key: str) -> int:
    """Where a format (or "art") stands in a formats setting's order: the download order across
    albums. Presets follow BEST_ORDER, their images come last."""
    _mode, items, preset = parse_format_setting(setting or "mp3")
    order = list(items) if items is not None else list(BEST_ORDER)
    if items is None or ":" not in str(setting):
        order.append("art")
    return order.index(key) if key in order else len(order) + 1


def wanted_exts(setting, album_formats: Iterable[str], art: bool) -> frozenset:
    return frozenset(format_plan(setting, album_formats, art))


def formats_for(settings: dict, slug: str) -> str:
    """The formats setting that applies to one album: its own, else the general one."""
    return (settings.get("album_formats") or {}).get(slug) or settings.get("formats") or "mp3"


def album_plan(settings: dict, slug: str, album_formats) -> list:
    return format_plan(formats_for(settings, slug), album_formats, bool(settings.get("album_art")))


def album_rank(settings: dict, slug: str, key: str) -> int:
    return format_rank(formats_for(settings, slug), key)


def album_wants(settings: dict, slug: str, album_formats) -> frozenset:
    return frozenset(album_plan(settings, slug, album_formats))


def parse_formats(text) -> None:
    """Raises ValueError for a formats setting that can't work."""
    parse_format_setting(text)


class Catalog:
    def __init__(self, path: str):
        self.path = path
        self.existed = os.path.exists(path)      # False: a new, empty catalog was created
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE IF NOT EXISTS albums (
                slug TEXT PRIMARY KEY, name TEXT, platforms TEXT, type TEXT, year TEXT, letter TEXT,
                seen_scan INTEGER, first_seen REAL, folder TEXT, read REAL, error TEXT,
                missing INTEGER DEFAULT 0, added TEXT, title TEXT, formats TEXT, tracks INTEGER,
                images INTEGER);
            CREATE INDEX IF NOT EXISTS albums_folder ON albums(folder COLLATE NOCASE);
            CREATE TABLE IF NOT EXISTS platforms (slug TEXT PRIMARY KEY, name TEXT, count INTEGER);
            CREATE TABLE IF NOT EXISTS files (
                url TEXT PRIMARY KEY, album TEXT, page TEXT, ext TEXT, kind TEXT, disc TEXT, num TEXT,
                name TEXT, title TEXT, duration TEXT, rel TEXT, size TEXT, bytes INTEGER, pos INTEGER,
                local TEXT, local_size INTEGER, synced REAL, error TEXT);
            CREATE INDEX IF NOT EXISTS files_album ON files(album);
            CREATE TABLE IF NOT EXISTS selection (key TEXT PRIMARY KEY, include INTEGER);
            CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, root TEXT, started REAL,
                finished REAL, pages INTEGER, files INTEGER, status TEXT);
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, started REAL, finished REAL,
                summary TEXT);
            CREATE TABLE IF NOT EXISTS changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, day TEXT, job REAL, kind TEXT,
                path TEXT, size INTEGER, note TEXT);
            CREATE INDEX IF NOT EXISTS changes_day ON changes(day);
            CREATE INDEX IF NOT EXISTS changes_job ON changes(job, kind);
            CREATE TABLE IF NOT EXISTS extras (rel TEXT PRIMARY KEY, album TEXT, kind TEXT, sig TEXT);
            CREATE TABLE IF NOT EXISTS quota (t REAL, kind TEXT, key TEXT);
            CREATE INDEX IF NOT EXISTS quota_t ON quota(kind, t);
        """)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(files)")}
        if "cover" not in cols:                  # 1.0.9: covers embedded into music files (covers.py)
            self.db.execute("ALTER TABLE files ADD COLUMN cover TEXT")
        if "tags" not in cols:                   # 1.0.11: missing tags filled in (covers.py)
            self.db.execute("ALTER TABLE files ADD COLUMN tags TEXT")
        if "direct" not in cols:                 # 1.0.22: the MP3 address from the album page's player
            self.db.execute("ALTER TABLE files ADD COLUMN direct TEXT")
        if "changed" not in {r[1] for r in self.db.execute("PRAGMA table_info(albums)")}:
            # 1.0.11: when khinsider replaced the album with another version (seen in its newest list)
            self.db.execute("ALTER TABLE albums ADD COLUMN changed TEXT")
        self.db.commit()
        self._index: Optional[list] = None       # [(slug, (platforms), type)] for the selection
        self._stats: Optional[dict] = None       # {album: {ext|"art": [n, bytes, synced, synced_bytes, failed]}}
        self._pages: dict = {}                   # {album: tracks} (with stats)
        self._stand: dict = {}                   # stand_ins() cache (with stats)
        self.version = 0                          # +1 on every change the page should show

    def changed(self, albums: bool = False) -> None:
        """Something the page shows changed; albums=True: the album list itself (a scan)."""
        self.version += 1
        self._stats = None
        if albums:
            self._index = None

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict:
        out = dict(DEFAULT_SETTINGS)
        with self.lock:
            for k, v in self.db.execute("SELECT k, v FROM settings"):
                try:
                    out[k] = json.loads(v)
                except ValueError:
                    out[k] = v
        return out

    def save_settings(self, values: dict) -> None:
        with self.lock:
            for k, v in values.items():
                if k in DEFAULT_SETTINGS:
                    self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (k, json.dumps(v)))
            self.db.commit()
        self.changed()

    # ------------------------------------------------------------------ scans
    def start_scan(self, kind: str, root: str = "") -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO scans(kind, root, started, status) VALUES (?,?,?,?)",
                                  (kind, root, time.time(), "running"))
            self.db.commit()
            return cur.lastrowid

    def finish_scan(self, scan_id: int, pages: int, files: int, status: str) -> None:
        with self.lock:
            self.db.execute("UPDATE scans SET finished=?, pages=?, files=?, status=? WHERE id=?",
                            (time.time(), pages, files, status, scan_id))
            self.db.commit()
        self.changed(albums=True)

    def last_scan(self, kind: Optional[str] = None):
        q = "SELECT id, kind, root, started, finished, pages, files, status FROM scans"
        q += " WHERE kind=?" if kind else ""
        q += " ORDER BY id DESC LIMIT 1"
        with self.lock:
            row = self.db.execute(q, (kind,) if kind else ()).fetchone()
        if not row:
            return None
        return dict(zip(("id", "kind", "root", "started", "finished", "pages", "files", "status"), row))

    def last_good_scan(self) -> Optional[float]:
        """When the last album-list scan (full or newest albums) that finished cleanly started."""
        with self.lock:
            row = self.db.execute("SELECT MAX(started) FROM scans WHERE kind IN ('full','quick') AND status='done'").fetchone()
        return row[0] if row and row[0] else None

    def mark_replaced(self, seen: Iterable[tuple]) -> list:
        """(slug, day) pairs from khinsider's newest-albums list. An album whose page was read
        before and now shows up there with a later day was replaced by another version: its page is
        read again and its files are checked by the next sync that includes it. -> those slugs."""
        out = []
        with self.lock:
            for slug, day in seen:
                row = self.db.execute("SELECT read, added FROM albums WHERE slug=?", (slug,)).fetchone()
                if row and row[0] and row[1] and row[1] < day:
                    self.db.execute("UPDATE albums SET read=NULL, changed=? WHERE slug=?", (day, slug))
                    out.append(slug)
            if out:
                self.db.commit()
        if out:
            self.changed(albums=True)
        return out

    def clear_changed(self, slugs: Iterable[str]) -> None:
        with self.lock:
            self.db.executemany("UPDATE albums SET changed=NULL WHERE slug=?", [(s,) for s in slugs])

    def set_platforms(self, rows: Iterable[tuple]) -> None:
        """[(slug, name, albums)] from the site's platform list."""
        with self.lock:
            for slug, name, count in rows:
                self.db.execute("INSERT INTO platforms(slug, name, count) VALUES (?,?,?) ON CONFLICT(slug) "
                                "DO UPDATE SET name=excluded.name, count=excluded.count", (slug, name, count))

    def add_albums(self, rows: list, letter: str, scan_id: int) -> int:
        """Albums from a browse page -> how many were new."""
        now = time.time()
        new = 0
        with self.lock:
            for a in rows:
                plats = join_platforms(p for p, _ in a["platforms"])
                for p, name in a["platforms"]:
                    self.db.execute("INSERT OR IGNORE INTO platforms(slug, name, count) VALUES (?,?,0)", (p, name))
                cur = self.db.execute(
                    "UPDATE albums SET name=?, platforms=?, type=?, year=?, letter=?, seen_scan=?, missing=0 "
                    "WHERE slug=?", (a["name"], plats, a["type"], a["year"], letter, scan_id, a["slug"]))
                if cur.rowcount == 0:
                    self.db.execute("INSERT INTO albums(slug, name, platforms, type, year, letter, seen_scan, "
                                    "first_seen) VALUES (?,?,?,?,?,?,?,?)",
                                    (a["slug"], a["name"], plats, a["type"], a["year"], letter, scan_id, now))
                    new += 1
        return new

    def assign_folders(self, name_of: Callable[[str, str], str]) -> int:
        """Give every album without one its folder name, once: its name; when another album
        already has that folder, "name (Type, Year)"; failing that "name [slug]". Kept for good,
        so albums the site adds later never move folders you already have.
        name_of(name, slug) makes a name safe for the disk."""
        with self.lock:
            used = {r[0].casefold() for r in self.db.execute(
                "SELECT folder FROM albums WHERE folder IS NOT NULL")}
            todo = self.db.execute("SELECT slug, name, type, year FROM albums WHERE folder IS NULL "
                                   "ORDER BY first_seen, slug").fetchall()
            for slug, name, kind, year in todo:
                name = name or slug
                extra = ", ".join(x for x in (kind, year) if x)
                for cand in (name, f"{name} ({extra})" if extra else None, f"{name} [{slug}]"):
                    if cand is None:
                        continue
                    folder = name_of(cand, slug)
                    if folder.casefold() not in used:
                        break
                used.add(folder.casefold())
                self.db.execute("UPDATE albums SET folder=? WHERE slug=?", (folder, slug))
            self.db.commit()
        return len(todo)

    def prune(self, scan_id: int) -> list:
        """After a complete full scan: forget albums the site no longer lists -> their slugs."""
        with self.lock:
            gone = [r[0] for r in self.db.execute(
                "SELECT slug FROM albums WHERE seen_scan IS NULL OR seen_scan<>?", (scan_id,))]
            for i in range(0, len(gone), 500):
                chunk = gone[i:i + 500]
                marks = ",".join("?" * len(chunk))
                self.db.execute(f"DELETE FROM files WHERE album IN ({marks})", chunk)
                self.db.execute(f"DELETE FROM albums WHERE slug IN ({marks})", chunk)
                self.db.execute(f"DELETE FROM selection WHERE key IN ({marks})", [ALBUM + s for s in chunk])
            self.db.commit()
        self.changed(albums=True)
        return gone

    def commit(self):
        with self.lock:
            self.db.commit()

    # ------------------------------------------------------------------ albums
    COLS = ("slug", "name", "platforms", "type", "year", "folder", "read", "error", "missing", "added",
            "title", "formats", "tracks", "images", "first_seen", "changed")

    def album(self, slug: str) -> Optional[dict]:
        with self.lock:
            row = self.db.execute(f"SELECT {', '.join(self.COLS)} FROM albums WHERE slug=?", (slug,)).fetchone()
        return self._album_dict(row) if row else None

    def _album_dict(self, row) -> dict:
        d = dict(zip(self.COLS, row))
        d["platforms"] = split_platforms(d["platforms"])
        d["formats"] = [f for f in (d["formats"] or "").split(",") if f]
        return d

    def albums_by_slug(self, slugs: Iterable[str]) -> dict:
        slugs = list(slugs)
        out = {}
        with self.lock:
            for i in range(0, len(slugs), 500):
                chunk = slugs[i:i + 500]
                q = f"SELECT {', '.join(self.COLS)} FROM albums WHERE slug IN ({','.join('?' * len(chunk))})"
                for row in self.db.execute(q, chunk):
                    out[row[0]] = self._album_dict(row)
        return out

    def platform_names(self) -> dict:
        with self.lock:
            return {s: n for s, n in self.db.execute("SELECT slug, name FROM platforms")}

    def album_read(self, slug: str, page: dict, name_of_file: Callable[[str], str],
                   rel_of: Callable[[dict, dict], str]) -> int:
        """Store what an album's page shows (parse.parse_album) -> number of files.
        Files the page no longer lists are dropped from the catalog (Clean up finds them on disk)."""
        now = time.time()
        with self.lock:
            row = self.db.execute("SELECT platforms, type, year FROM albums WHERE slug=?", (slug,)).fetchone()
            plats = join_platforms(p for p, _ in page["platforms"])
            for p, name in page["platforms"]:
                self.db.execute("INSERT OR IGNORE INTO platforms(slug, name, count) VALUES (?,?,0)", (p, name))
            if row is None:                       # not in a scan yet (a renamed album): add it
                self.db.execute("INSERT INTO albums(slug, name, platforms, type, year, first_seen) "
                                "VALUES (?,?,?,?,?,?)", (slug, page["title"] or slug, plats, page["type"],
                                                         page["year"], now))
            else:
                # the browse list is the source of platforms/type/year; fill in only what it lacks
                self.db.execute("UPDATE albums SET platforms=COALESCE(NULLIF(platforms, ''), ?), "
                                "type=COALESCE(NULLIF(type, ''), ?), year=COALESCE(NULLIF(year, ''), ?) "
                                "WHERE slug=?", (plats, page["type"], page["year"], slug))
            self.db.execute("UPDATE albums SET read=?, error=NULL, missing=0, added=?, title=?, formats=?, "
                            "tracks=?, images=? WHERE slug=?",
                            (now, page["added"], page["title"], ",".join(page["formats"]), len(page["tracks"]),
                             len(page["images"]), slug))
            album = self.album(slug)
            rows = []
            for pos, t in enumerate(page["tracks"]):
                stem = t["file"].rsplit(".", 1)[0] if "." in t["file"] else t["file"]
                for k, ext in enumerate(page["formats"]):
                    size = t["sizes"].get(ext)
                    if not size:
                        continue                      # this track isn't offered in this format
                    name = name_of_file(stem + ext)
                    f = {"name": name, "kind": "track", "ext": ext, "disc": t["disc"], "num": t["num"],
                         "title": t["title"], "pos": pos * 100 + k}
                    rows.append((f"{t['page']}#{ext}", slug, t["page"], ext, "track", t["disc"], t["num"], name,
                                 t["title"], t["duration"], rel_of(album, f), size, size_bytes(size), f["pos"],
                                 t.get("mp3") if ext == ".mp3" else None))
            for pos, img in enumerate(page["images"]):
                name = name_of_file(unquote(urlsplit(img).path.rsplit("/", 1)[-1]))
                ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
                f = {"name": name, "kind": "art", "ext": ext, "disc": "", "num": "", "title": "",
                     "pos": 100_000_000 + pos}
                rows.append((img, slug, None, ext, "art", "", "", name, "", "", rel_of(album, f), None, 0, f["pos"],
                             None))
            keep = {r[0] for r in rows}
            old = {r[0] for r in self.db.execute("SELECT url FROM files WHERE album=?", (slug,))}
            gone = old - keep
            if gone:
                self.db.executemany("DELETE FROM files WHERE url=?", [(u,) for u in gone])
            self.db.executemany(
                "INSERT INTO files(url, album, page, ext, kind, disc, num, name, title, duration, rel, size, "
                "bytes, pos, direct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
                "album=excluded.album, page=excluded.page, ext=excluded.ext, kind=excluded.kind, "
                "disc=excluded.disc, num=excluded.num, name=excluded.name, title=excluded.title, "
                "duration=excluded.duration, rel=excluded.rel, size=excluded.size, bytes=excluded.bytes, "
                "pos=excluded.pos, direct=excluded.direct", rows)
        self.changed()
        return len(rows)

    def album_failed(self, slug: str, error: str, missing: bool = False) -> None:
        with self.lock:
            self.db.execute("UPDATE albums SET error=?, missing=? WHERE slug=?", (error, int(missing), slug))
        self.changed()

    def rename_album(self, old: str, new: str) -> None:
        """The site moved an album to a new address (its page redirects). Ticks, Saved as and the
        folder on disk follow it, so nothing is downloaded twice."""
        with self.lock:
            have_new = self.db.execute("SELECT folder FROM albums WHERE slug=?", (new,)).fetchone()
            row = self.db.execute("SELECT folder FROM albums WHERE slug=?", (old,)).fetchone()
            if row is None:
                return
            if have_new is None:
                self.db.execute("UPDATE albums SET slug=? WHERE slug=?", (new, old))
            else:
                # the new address is known already (a scan saw it): it takes over the old folder
                self.db.execute("UPDATE albums SET folder=NULL WHERE slug=?", (old,))
                self.db.execute("UPDATE albums SET folder=? WHERE slug=?", (row[0], new))
                self.db.execute("DELETE FROM albums WHERE slug=?", (old,))
            self.db.execute("DELETE FROM files WHERE album=?", (old,))
            inc = self.db.execute("SELECT include FROM selection WHERE key=?", (ALBUM + old,)).fetchone()
            if inc is not None:
                self.db.execute("DELETE FROM selection WHERE key=?", (ALBUM + old,))
                self.db.execute("INSERT OR REPLACE INTO selection VALUES (?,?)", (ALBUM + new, inc[0]))
            self.db.commit()
        # the album's own settings follow it too
        s = self.settings()
        moved = {}
        for key in ("album_places", "album_formats", "album_upgrades"):
            own = dict(s.get(key) or {})
            if old in own:
                own.setdefault(new, own[old])
                del own[old]
                moved[key] = own
        if moved:
            self.save_settings(moved)
        self.changed(albums=True)

    def remap(self, rel_of: Callable[[dict, dict], str]) -> int:
        """Where files go changed (layout, names, Art folder or Saved as): work out every file's
        place again. The next sync moves files it saved before to their new place."""
        with self.lock:
            albums = {r[0]: self._album_dict(r) for r in self.db.execute(
                f"SELECT {', '.join(self.COLS)} FROM albums WHERE read IS NOT NULL")}
            fixes = []
            for url, album, name, kind, ext, disc, num, title, pos, rel in self.db.execute(
                    "SELECT url, album, name, kind, ext, disc, num, title, pos, rel FROM files"):
                a = albums.get(album)
                if a is None:
                    continue
                new = rel_of(a, {"name": name, "kind": kind, "ext": ext, "disc": disc, "num": num,
                                 "title": title, "pos": pos})
                if new != rel:
                    fixes.append((new, url))
            if fixes:
                self.db.executemany("UPDATE files SET rel=?, local=NULL, local_size=NULL, synced=NULL "
                                    "WHERE url=?", fixes)
                self.db.commit()
        if fixes:
            self.changed()
        return len(fixes)

    # ------------------------------------------------------------------ files for syncing
    def files_of(self, albums: Iterable[str]) -> list:
        """[(url, rel, album, ext, kind, size, page)] of the given albums, in track order."""
        out = []
        albums = list(albums)
        with self.lock:
            for i in range(0, len(albums), 500):
                chunk = albums[i:i + 500]
                q = ("SELECT url, rel, album, ext, kind, size, page FROM files WHERE album IN (%s) "
                     "ORDER BY album, pos, ext" % ",".join("?" * len(chunk)))
                out += self.db.execute(q, chunk).fetchall()
        return out

    def direct_links(self, albums: Iterable[str]) -> dict:
        """{catalog address: MP3 address from the album page's player} of the given albums."""
        out = {}
        albums = list(albums)
        with self.lock:
            for i in range(0, len(albums), 500):
                chunk = albums[i:i + 500]
                out.update(self.db.execute("SELECT url, direct FROM files WHERE direct IS NOT NULL AND album IN (%s)"
                                           % ",".join("?" * len(chunk)), chunk).fetchall())
        return out

    def forget_missing(self, slug: str, intact: Callable[[str, Optional[int]], bool]) -> list:
        """Check an album's saved files on disk (`intact(rel, size when saved)`): the ones gone or
        cut short are marked not downloaded, so the next sync fetches them again. -> their paths."""
        words = ",".join("'%s'" % w for w in SYNCED_WORDS)
        with self.lock:
            rows = self.db.execute(f"SELECT url, rel, local_size FROM files WHERE album=? AND local IN ({words})",
                                   (slug,)).fetchall()
        gone = [(u, r) for u, r, n in rows if not intact(r, n)]
        if gone:
            with self.lock:
                self.db.executemany("UPDATE files SET local=NULL, local_size=NULL WHERE url=?", [(u,) for u, _ in gone])
                self.db.commit()
            self.changed()
        return [r for _, r in gone]

    def saved_files(self, album: Optional[str] = None) -> list:
        """[(url, rel, album, ext, kind, size, size when saved or None)] of the downloaded files
        (of one album, or all)."""
        words = ",".join("'%s'" % w for w in SYNCED_WORDS)
        q = (f"SELECT url, rel, album, ext, kind, COALESCE(local_size, bytes, 0), local_size FROM files "
             f"WHERE local IN ({words})")
        with self.lock:
            if album is None:
                return self.db.execute(q).fetchall()
            return self.db.execute(q + " AND album=?", (album,)).fetchall()

    # ------------------------------------------------------------------ files the app made (extras.py)
    # ------------------------------------------------------------------ limits (pacing.py)
    def quota_add(self, kind: str, key: str, t: Optional[float] = None) -> None:
        with self.lock:
            self.db.execute("INSERT INTO quota VALUES (?,?,?)", (t or time.time(), kind, key))

    def quota_times(self, kind: str, seconds: float) -> list:
        """When each `kind` ("file" / "album") of the last `seconds` was started."""
        with self.lock:
            self.db.execute("DELETE FROM quota WHERE t<?", (time.time() - 3 * 86400,))
            return [r[0] for r in self.db.execute("SELECT t FROM quota WHERE kind=? AND t>? ORDER BY t",
                                                  (kind, time.time() - seconds))]

    def quota_keys(self, kind: str, seconds: float) -> set:
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT key FROM quota WHERE kind=? AND t>?",
                                                  (kind, time.time() - seconds))}

    def extras(self) -> dict:
        """{rel: (album, kind, sig)} of the playlists and folder images this app wrote."""
        with self.lock:
            return {r: (a, k, s) for r, a, k, s in self.db.execute("SELECT rel, album, kind, sig FROM extras")}

    def set_extra(self, rel: str, album: str, kind: str, sig: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO extras VALUES (?,?,?,?)", (rel, album, kind, sig))

    def untagged(self, slugs, cover: bool, tags: bool) -> list:
        """Downloaded music files of these albums covers.py hasn't handled yet (for the cover and/or
        the tags) -> [dict(url, rel, album, ext, disc, num, pos, title, name, cover, tags)]."""
        words = ",".join("'%s'" % w for w in SYNCED_WORDS)
        todo = " OR ".join(c for c, on in (("cover IS NULL", cover), ("tags IS NULL", tags)) if on)
        if not todo:
            return []
        keys = ("url", "rel", "album", "ext", "disc", "num", "pos", "title", "name", "cover", "tags")
        out = []
        slugs = list(slugs)
        with self.lock:
            for i in range(0, len(slugs), 500):
                chunk = slugs[i:i + 500]
                out += [dict(zip(keys, r)) for r in self.db.execute(
                    f"SELECT {', '.join(keys)} FROM files WHERE kind!='art' AND ({todo}) AND local IN ({words}) "
                    f"AND album IN ({','.join('?' * len(chunk))}) ORDER BY album, pos", chunk)]
        return out

    def track_layout(self, album: str) -> dict:
        """{(ext, disc): [num or pos, ...] in track order} of an album's music files - for track totals."""
        out: dict = {}
        with self.lock:
            for ext, disc, num, pos in self.db.execute(
                    "SELECT ext, disc, num, pos FROM files WHERE album=? AND kind!='art' ORDER BY pos", (album,)):
                out.setdefault((ext, disc or ""), []).append((num, pos))
        return out

    def set_tagged(self, url: str, cover: Optional[str], tags: Optional[str], size: Optional[int] = None) -> None:
        with self.lock:
            self.db.execute("UPDATE files SET cover=COALESCE(?, cover), tags=COALESCE(?, tags), "
                            "local_size=COALESCE(?, local_size) WHERE url=?", (cover, tags, size, url))

    def drop_extras(self, rels: Iterable[str]) -> None:
        with self.lock:
            self.db.executemany("DELETE FROM extras WHERE rel=?", [(r,) for r in rels])
            self.db.commit()

    def vanished_downloaded(self, scan_id: int) -> list:
        """Albums a full scan didn't see although something of them was downloaded."""
        words = ",".join("'%s'" % w for w in SYNCED_WORDS)
        with self.lock:
            return [r[0] for r in self.db.execute(
                f"SELECT DISTINCT a.slug FROM albums a JOIN files f ON f.album=a.slug "
                f"WHERE (a.seen_scan IS NULL OR a.seen_scan<>?) AND f.local IN ({words})", (scan_id,))]

    def mark_seen(self, slug: str, scan_id: int) -> None:
        with self.lock:
            self.db.execute("UPDATE albums SET seen_scan=? WHERE slug=?", (scan_id, slug))

    def set_local(self, url: str, word: Optional[str], size: Optional[int], error: Optional[str]) -> None:
        with self.lock:
            self.db.execute("UPDATE files SET local=?, local_size=COALESCE(?, local_size), synced=?, error=?, "
                            "cover=CASE WHEN ? IN ('new','updated','resumed') THEN NULL ELSE cover END, "
                            "tags=CASE WHEN ? IN ('new','updated','resumed') THEN NULL ELSE tags END "
                            "WHERE url=?", (word, size, time.time(), error, word, word, url))
        self._stats = None
        self.version += 1

    def files_by_rel(self) -> dict:
        """{saved path: (url, album, ext, kind)} for every file the catalog knows."""
        with self.lock:
            return {rel: (url, album, ext, kind) for rel, url, album, ext, kind in self.db.execute(
                "SELECT rel, url, album, ext, kind FROM files")}

    def forget_local(self, urls: Iterable[str]) -> None:
        """These files were deleted from the download folder."""
        with self.lock:
            self.db.executemany("UPDATE files SET local=NULL, local_size=NULL, error=NULL WHERE url=?",
                                [(u,) for u in urls])
            self.db.commit()
        self.changed()

    # ------------------------------------------------------------------ selection
    def rules(self) -> dict:
        with self.lock:
            return {k: bool(i) for k, i in self.db.execute("SELECT key, include FROM selection")}

    def index(self) -> list:
        """[(slug, platforms tuple, type)] for every album (cached until the next scan)."""
        if self._index is None:
            with self.lock:
                self._index = [(s, tuple(split_platforms(p)), t or "") for s, p, t in self.db.execute(
                    "SELECT slug, platforms, type FROM albums")]
        return self._index

    @staticmethod
    def by_platform(platforms, kind: str, rules: dict, types) -> bool:
        """Does a ticked platform bring this album in? (`types`: the album types it brings in.)"""
        if types and kind not in types:
            return False
        return any(rules.get(PLATFORM + p) for p in platforms)

    def is_selected(self, slug: str, platforms, kind: str, rules: dict, types) -> bool:
        own = rules.get(ALBUM + slug)
        if own is not None:
            return own
        return self.by_platform(platforms, kind, rules, types)

    def selected_albums(self, rules: Optional[dict] = None, types=None) -> list:
        rules = self.rules() if rules is None else rules
        if types is None:
            types = self.settings().get("platform_types") or []
        if not rules:
            return []
        if not any(k.startswith(PLATFORM) and v for k, v in rules.items()):   # only album ticks: quick
            return sorted(k[len(ALBUM):] for k, v in rules.items() if v and k.startswith(ALBUM))
        return [s for s, p, t in self.index() if self.is_selected(s, p, t, rules, types)]

    def select(self, key: str, include: bool) -> None:
        """Tick / untick an album ("a:<slug>") or a platform ("p:<slug>": every album of it,
        including albums the site adds later). Ticking or unticking a platform clears the
        exceptions made on its albums, like ticking a folder does."""
        types = self.settings().get("platform_types") or []
        with self.lock:
            if key.startswith(PLATFORM):
                plat = key[len(PLATFORM):]
                pat = f"%|{plat}|%"
                self.db.execute("DELETE FROM selection WHERE key IN (SELECT ? || slug FROM albums "
                                "WHERE platforms LIKE ?)", (ALBUM, pat))
                if include:
                    self.db.execute("INSERT OR REPLACE INTO selection VALUES (?,1)", (key,))
                else:
                    self.db.execute("DELETE FROM selection WHERE key=?", (key,))
            elif key.startswith(ALBUM):
                slug = key[len(ALBUM):]
                row = self.db.execute("SELECT platforms, type FROM albums WHERE slug=?", (slug,)).fetchone()
                rules = self.rules()
                inherited = self.by_platform(split_platforms(row[0]) if row else [], (row[1] or "") if row else "",
                                             rules, types)
                if include == inherited:
                    self.db.execute("DELETE FROM selection WHERE key=?", (key,))
                else:
                    self.db.execute("INSERT OR REPLACE INTO selection VALUES (?,?)", (key, int(include)))
            else:
                raise ValueError("unknown selection key")
            self.db.commit()
        self.version += 1

    def select_albums(self, slugs: Iterable[str], include: bool = True) -> int:
        """Tick (or untick) many albums at once - one transaction. -> how many rules changed."""
        types = self.settings().get("platform_types") or []
        n = 0
        with self.lock:
            rules = self.rules()
            for slug in slugs:
                row = self.db.execute("SELECT platforms, type FROM albums WHERE slug=?", (slug,)).fetchone()
                if row is None:
                    continue
                inherited = self.by_platform(split_platforms(row[0]), row[1] or "", rules, types)
                key = ALBUM + slug
                if include == inherited:
                    self.db.execute("DELETE FROM selection WHERE key=?", (key,))
                else:
                    self.db.execute("INSERT OR REPLACE INTO selection VALUES (?,?)", (key, int(include)))
                n += 1
            self.db.commit()
        self.version += 1
        return n

    def clear_selection(self) -> None:
        with self.lock:
            self.db.execute("DELETE FROM selection")
            self.db.commit()
        self.version += 1

    def platform_states(self, rules: dict) -> dict:
        """{platform: 'on' | 'partial'} ('off' is left out). Partial: ticked with some albums
        unticked, or not ticked with some albums ticked one by one."""
        out = {k[len(PLATFORM):]: "on" for k, v in rules.items() if k.startswith(PLATFORM) and v}
        own = {k[len(ALBUM):]: v for k, v in rules.items() if k.startswith(ALBUM)}
        if own:
            for slug, plats in self.albums_platforms(own).items():
                for p in plats:
                    if out.get(p) == "on" and not own[slug]:
                        out[p] = "partial"
                    elif p not in out and own[slug]:
                        out[p] = "partial"
        return out

    def albums_platforms(self, slugs) -> dict:
        slugs = list(slugs)
        out = {}
        with self.lock:
            for i in range(0, len(slugs), 500):
                chunk = slugs[i:i + 500]
                for s, p in self.db.execute("SELECT slug, platforms FROM albums WHERE slug IN (%s)"
                                            % ",".join("?" * len(chunk)), chunk):
                    out[s] = split_platforms(p)
        return out

    # ------------------------------------------------------------------ numbers for the page
    def stats(self) -> dict:
        """{album: {ext or "art": [files, bytes, synced, synced_bytes, failed, last synced]}} (cached)."""
        if self._stats is None:
            words = ",".join("'%s'" % w for w in SYNCED_WORDS)
            out: dict = {}
            with self.lock:
                for album, ext, kind, n, b, sn, sb, fl, last in self.db.execute(
                        # a synced file counts with its real size, the others with the listed one,
                        # so a fully synced selection shows nothing left to download
                        f"SELECT album, ext, kind, COUNT(*), "
                        f"SUM(CASE WHEN local IN ({words}) THEN COALESCE(local_size, bytes) ELSE bytes END), "
                        f"SUM(local IN ({words})), "
                        f"SUM(CASE WHEN local IN ({words}) THEN COALESCE(local_size, bytes) ELSE 0 END), "
                        f"SUM(error IS NOT NULL AND local IS NULL), "
                        f"MAX(CASE WHEN local IN ({words}) THEN synced END) FROM files GROUP BY album, ext, kind"):
                    key = "art" if kind == "art" else ext
                    cur = out.setdefault(album, {}).setdefault(key, [0, 0, 0, 0, 0, 0.0])
                    for i, v in enumerate((n, b or 0, sn or 0, sb or 0, fl or 0)):
                        cur[i] += v
                    cur[5] = max(cur[5], last or 0.0)
            self._stats = out
            with self.lock:
                self._pages = dict(self.db.execute(
                    "SELECT album, COUNT(DISTINCT page) FROM files WHERE kind='track' GROUP BY album"))
            self._stand = {}
        return self._stats

    def stand_ins(self, slug: str, pick: Optional[str], fallback: list) -> dict:
        """{url: [1, bytes, synced, synced_bytes, failed, last synced]} of the files that stand in for
        an album's tracks missing in its format `pick` (format_fallback), cached with stats()."""
        st = self.stats().get(slug, {})
        if not pick or not fallback or st.get(pick, [0])[0] >= self._pages.get(slug, 0):
            return {}                            # every track has the album's format
        key = (slug, pick, tuple(fallback))
        if key in self._stand:
            return self._stand[key]
        words = SYNCED_WORDS
        with self.lock:
            rows = self.db.execute("SELECT page, ext, url, bytes, local, local_size, error, synced FROM files "
                                   "WHERE album=? AND kind='track'", (slug,)).fetchall()
        by_page: dict = {}
        for page, ext, *rest in rows:
            by_page.setdefault(page, {})[ext] = rest
        out = {}
        for page, have in by_page.items():
            if pick in have:
                continue
            ext = next((f for f in fallback if f in have), None)
            if ext is None:
                continue
            url, b, local, lsize, err, synced = have[ext]
            ok = local in words
            out[url] = [1, (lsize or b or 0) if ok else (b or 0), int(ok), (lsize or b or 0) if ok else 0,
                        int(err is not None and local is None), (synced or 0.0) if ok else 0.0]
        self._stand[key] = out
        return out

    def wants(self, settings: dict, slug: str, album_formats) -> Want:
        """album_wants() plus the files standing in for tracks missing in the album's format."""
        keys = album_wants(settings, slug, album_formats)
        pick, rest = format_fallback(formats_for(settings, slug), album_formats)
        return Want(keys, self.stand_ins(slug, pick, rest))

    def album_totals(self, slug: str, formats: list, settings: dict) -> dict:
        """Files, size and how much is synced of what a sync of this album saves. `downloaded`:
        every one of them is in the download folder (`downloaded_at`: when the last one arrived) -
        khinsider doesn't change albums, so a downloaded album is never checked again."""
        want = self.wants(settings, slug, formats)
        st = self.stats().get(slug, {})
        t = [0, 0, 0, 0, 0]
        last = 0.0
        for row in [st.get(key, (0, 0, 0, 0, 0, 0.0)) for key in want] + [r for r in want.rows.values() if r]:
            for i in range(5):
                t[i] += row[i]
            last = max(last, row[5])
        done = bool(t[0]) and t[2] == t[0]
        return {"files": t[0], "bytes": t[1], "synced": t[2], "synced_bytes": t[3], "failed": t[4],
                "downloaded": done, "downloaded_at": last if done else None}

    def counts(self) -> dict:
        with self.lock:
            albums = self.db.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
            read = self.db.execute("SELECT COUNT(*) FROM albums WHERE read IS NOT NULL").fetchone()[0]
            plats = self.db.execute("SELECT COUNT(*) FROM platforms").fetchone()[0]
        return {"albums": albums, "read": read, "platforms": plats}

    # ------------------------------------------------------------------ history
    def add_history(self, kind: str, started: float, summary: dict) -> None:
        with self.lock:
            self.db.execute("INSERT INTO history(kind, started, finished, summary) VALUES (?,?,?,?)",
                            (kind, started, time.time(), json.dumps(summary)))
            self.db.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 50)")
            self.db.commit()

    def history(self, limit: int = 20) -> list:
        with self.lock:
            rows = self.db.execute("SELECT kind, started, finished, summary FROM history "
                                   "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"kind": k, "started": s, "finished": f, "summary": json.loads(j)} for k, s, f, j in rows]

    # ------------------------------------------------------------------ change history
    # What happened to files in the download folder: new / updated / resumed / failed (by a sync,
    # `job` = the sync's start time) and deleted (by Clean up). Kept for CHANGES_KEEP_DAYS.
    CHANGES_KEEP_DAYS = 365

    def add_change(self, job: Optional[float], kind: str, path: str, size: Optional[int] = None,
                   note: Optional[str] = None, t: Optional[float] = None) -> None:
        t = t or time.time()
        with self.lock:
            self.db.execute("INSERT INTO changes(t, day, job, kind, path, size, note) VALUES (?,?,?,?,?,?,?)",
                            (t, time.strftime("%Y-%m-%d", time.localtime(t)), job, kind, path, size, note))

    def prune_changes(self) -> None:
        with self.lock:
            self.db.execute("DELETE FROM changes WHERE t < ?", (time.time() - self.CHANGES_KEEP_DAYS * 86400,))
            self.db.commit()

    def change_days(self, limit: int = 120) -> list:
        """[{day, counts: {kind: n}, bytes: {kind: b}}], newest day first."""
        with self.lock:
            days = [r[0] for r in self.db.execute(
                "SELECT DISTINCT day FROM changes ORDER BY day DESC LIMIT ?", (limit,))]
            out = {d: {"day": d, "counts": {}, "bytes": {}} for d in days}
            if days:
                q = ("SELECT day, kind, COUNT(*), SUM(COALESCE(size, 0)) FROM changes WHERE day IN (%s) "
                     "GROUP BY day, kind" % ",".join("?" * len(days)))
                for d, k, n, b in self.db.execute(q, days):
                    out[d]["counts"][k] = n
                    out[d]["bytes"][k] = b or 0
        return [out[d] for d in days]

    def changes(self, day: Optional[str] = None, job: Optional[float] = None, kind: Optional[str] = None,
                limit: int = 2000) -> tuple:
        """(total, [{t, kind, path, size, note}]) newest first."""
        where, args = [], []
        if day:
            where.append("day=?")
            args.append(day)
        if job is not None:
            where.append("job=?")
            args.append(job)
        if kind:
            where.append("kind=?")
            args.append(kind)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        with self.lock:
            total = self.db.execute("SELECT COUNT(*) FROM changes" + w, args).fetchone()[0]
            rows = self.db.execute("SELECT t, kind, path, size, note FROM changes" + w +
                                   " ORDER BY t DESC, id DESC LIMIT ?", args + [limit]).fetchall()
        return total, [dict(zip(("t", "kind", "path", "size", "note"), r)) for r in rows]

    def unchanged_since(self, since: float, limit: int = 2000) -> tuple:
        """Files a sync found up to date (or adopted) since a time -> (total, rows)."""
        q = " FROM files WHERE synced >= ? AND local IN ('up to date', 'already in folder')"
        with self.lock:
            total = self.db.execute("SELECT COUNT(*)" + q, (since,)).fetchone()[0]
            rows = self.db.execute("SELECT synced, local, rel, COALESCE(local_size, bytes)" + q +
                                   " ORDER BY rel COLLATE NOCASE LIMIT ?", (since, limit)).fetchall()
        return total, [{"t": t, "kind": "uptodate", "path": r, "size": b,
                        "note": "already in the folder" if w == "already in folder" else None}
                       for t, w, r, b in rows]

    def close(self):
        with self.lock:
            self.db.commit()
            self.db.close()
