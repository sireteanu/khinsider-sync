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
    "track_names": "site",          # site ("1-01. Title.mp3" as khinsider names it) | numbered ("1-01 - Title.mp3")
    "art_folder": False,            # images in an "Art" folder inside the album's folder
    "impersonate": True,            # requests look like Chrome (needs curl_cffi)
    "album_refresh_days": 7,        # re-read a ticked album's page before a sync when older (0 = always)
    "parallel": "auto",             # auto | number
    "parallel_max": 8,
    "max_rate": "",                 # "" = unlimited, e.g. "5M"
    "min_free": "2G",
    "file_mode": "777",             # permissions for downloaded files and new folders; "" = system default
    "resume_interrupted": True,     # restart a sync that was cut off (crash, reboot, restart)
    "interrupted_job": "",          # set while a sync runs; cleared when it ends normally
    "check_remote": False,
    "outage_wait_hours": 12,        # how long a sync waits for the site when it's down
    "sync_every_hours": 0,          # 0 = off
    "full_scan_every_days": 7,      # with the schedule on: full scan when the last one is older
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
        """)
        self.db.commit()
        self._index: Optional[list] = None       # [(slug, (platforms), type)] for the selection
        self._stats: Optional[dict] = None       # {album: {ext|"art": [n, bytes, synced, synced_bytes, failed]}}
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
            "title", "formats", "tracks", "images", "first_seen")

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
                                 t["title"], t["duration"], rel_of(album, f), size, size_bytes(size), f["pos"]))
            for pos, img in enumerate(page["images"]):
                name = name_of_file(unquote(urlsplit(img).path.rsplit("/", 1)[-1]))
                ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
                f = {"name": name, "kind": "art", "ext": ext, "disc": "", "num": "", "title": "",
                     "pos": 100_000_000 + pos}
                rows.append((img, slug, None, ext, "art", "", "", name, "", "", rel_of(album, f), None, 0, f["pos"]))
            keep = {r[0] for r in rows}
            old = {r[0] for r in self.db.execute("SELECT url FROM files WHERE album=?", (slug,))}
            gone = old - keep
            if gone:
                self.db.executemany("DELETE FROM files WHERE url=?", [(u,) for u in gone])
            self.db.executemany(
                "INSERT INTO files(url, album, page, ext, kind, disc, num, name, title, duration, rel, size, "
                "bytes, pos) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
                "album=excluded.album, page=excluded.page, ext=excluded.ext, kind=excluded.kind, "
                "disc=excluded.disc, num=excluded.num, name=excluded.name, title=excluded.title, "
                "duration=excluded.duration, rel=excluded.rel, size=excluded.size, bytes=excluded.bytes, "
                "pos=excluded.pos", rows)
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

    def set_local(self, url: str, word: Optional[str], size: Optional[int], error: Optional[str]) -> None:
        with self.lock:
            self.db.execute("UPDATE files SET local=?, local_size=COALESCE(?, local_size), synced=?, "
                            "error=? WHERE url=?", (word, size, time.time(), error, url))
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
        """{album: {ext or "art": [files, bytes, synced, synced_bytes, failed]}} (cached)."""
        if self._stats is None:
            words = ",".join("'%s'" % w for w in SYNCED_WORDS)
            out: dict = {}
            with self.lock:
                for album, ext, kind, n, b, sn, sb, fl in self.db.execute(
                        # a synced file counts with its real size, the others with the listed one,
                        # so a fully synced selection shows nothing left to download
                        f"SELECT album, ext, kind, COUNT(*), "
                        f"SUM(CASE WHEN local IN ({words}) THEN COALESCE(local_size, bytes) ELSE bytes END), "
                        f"SUM(local IN ({words})), "
                        f"SUM(CASE WHEN local IN ({words}) THEN COALESCE(local_size, bytes) ELSE 0 END), "
                        f"SUM(error IS NOT NULL AND local IS NULL) FROM files GROUP BY album, ext, kind"):
                    key = "art" if kind == "art" else ext
                    cur = out.setdefault(album, {}).setdefault(key, [0, 0, 0, 0, 0])
                    for i, v in enumerate((n, b or 0, sn or 0, sb or 0, fl or 0)):
                        cur[i] += v
            self._stats = out
        return self._stats

    def album_totals(self, slug: str, formats: list, settings: dict) -> dict:
        """Files, size and how much is synced of what a sync of this album saves."""
        want = album_wants(settings, slug, formats)
        st = self.stats().get(slug, {})
        t = [0, 0, 0, 0, 0]
        for key in want:
            for i, v in enumerate(st.get(key, (0, 0, 0, 0, 0))):
                t[i] += v
        return {"files": t[0], "bytes": t[1], "synced": t[2], "synced_bytes": t[3], "failed": t[4]}

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
