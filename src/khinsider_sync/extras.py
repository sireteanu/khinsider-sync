"""Files the app makes next to a downloaded album's music, for media servers and players:

  playlists   "<album folder>.m3u8" - the album's music in track order (#EXTINF with length and
              title, paths relative to the playlist), one format: the first the album's formats
              setting puts first
  folder_art  "folder.jpg" (or .png) - a copy of the album's cover (an image named cover / front /
              folder, else its first image), for Plex, Jellyfin, Kodi, Windows Explorer

Only for ticked albums that are completely downloaded. Each one is recorded in the catalog
(`extras`, with a signature of its content), so a sync rewrites it only when the album's files
changed, moves it when the album's folder moves, and removes it when the setting is turned off -
without looking at the disk for albums that are done. Clean up lists those of unticked albums.
An existing folder.jpg the app didn't write is never replaced.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil

from .catalog import AUDIO_EXTENSIONS, SYNCED_WORDS, album_plan
from .core.util import fs_path

log = logging.getLogger("khinsider_sync")

COVER = re.compile(r"cover|front|folder", re.I)


def seconds(duration: str) -> int:
    """'2:47' / '1:02:03' -> seconds (-1 when unknown, as #EXTINF wants)."""
    try:
        n = 0
        for part in (duration or "").split(":"):
            n = n * 60 + int(part)
        return n if duration else -1
    except ValueError:
        return -1


def plan(engine, s: dict, slugs) -> dict:
    """{rel: (album, kind, sig, content or source rel)} the given (ticked, downloaded) albums
    should have now."""
    want = {}
    if not (s.get("playlists") or s.get("folder_art")):
        return want
    albums = engine.catalog.albums_by_slug(slugs)
    words = ",".join("'%s'" % w for w in SYNCED_WORDS)
    with engine.catalog.lock:
        rows = engine.catalog.db.execute(
            f"SELECT album, ext, kind, rel, title, name, duration, local_size FROM files "
            f"WHERE local IN ({words}) ORDER BY album, pos, ext").fetchall()
    by_album: dict = {}
    for r in rows:
        if r[0] in albums:
            by_album.setdefault(r[0], []).append(r)
    for slug, files in by_album.items():
        a = albums[slug]
        folder = engine.album_dir(a, s)
        if s.get("playlists"):
            order = [k for k in album_plan(s, slug, a["formats"]) if k in AUDIO_EXTENSIONS]
            fmt = next((k for k in order if any(f[1] == k and f[2] != "art" for f in files)), None)
            tracks = [f for f in files if f[2] != "art" and f[1] == fmt]
            if tracks:
                lines = ["#EXTM3U", f"#PLAYLIST:{a['name']}"]
                for _al, _ext, _kind, rel, title, name, dur, _n in tracks:
                    path = rel[len(folder) + 1:] if rel.startswith(folder + "/") else os.path.basename(rel)
                    lines.append(f"#EXTINF:{seconds(dur)},{title or name}")
                    lines.append(path)
                text = "\n".join(lines) + "\n"
                base = folder.rsplit("/", 1)[-1]
                want[f"{folder}/{base}.m3u8"] = (slug, "playlist", hashlib.sha1(text.encode()).hexdigest(), text)
        if s.get("folder_art"):
            images = [f for f in files if f[2] == "art" and f[1] in (".jpg", ".jpeg", ".png")]
            pick = next((f for f in images if COVER.search(os.path.basename(f[3]))), images[0] if images else None)
            if pick:
                ext = ".png" if pick[1] == ".png" else ".jpg"
                rel = f"{folder}/folder{ext}"
                if pick[3].lower() != rel.lower():
                    want[rel] = (slug, "folder_art", f"{pick[3]}:{pick[7]}", pick[3])
    return want


def apply(root: str, want: dict, have: dict, own_albums: set, file_mode=None) -> tuple:
    """Write what changed, remove what is no longer wanted (only for albums in `own_albums`: those
    of unticked albums wait for Clean up). Runs in a thread. -> (written {rel: (album, kind, sig)},
    removed [rel], dirs left)."""
    written, removed, dirs = {}, [], set()

    def path_of(rel):
        return fs_path(os.path.join(root, *rel.split("/")))

    for rel, (album, kind, _sig) in have.items():
        if rel in want or album not in own_albums:
            continue
        try:
            os.remove(path_of(rel))
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("could not remove %s: %s", rel, e)
            continue
        removed.append(rel)
        dirs.add(os.path.dirname(path_of(rel)))
    for rel, (album, kind, sig, what) in want.items():
        old = have.get(rel)
        if old and old[2] == sig:
            continue
        dest = path_of(rel)
        if old is None and os.path.exists(dest):
            continue                        # not ours (e.g. your own folder.jpg): left alone
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if kind == "playlist":
                with open(dest, "w", encoding="utf-8", newline="\n") as f:
                    f.write(what)
            else:
                shutil.copyfile(path_of(what), dest)
            if file_mode is not None:
                try:
                    os.chmod(dest, file_mode)
                except OSError:
                    pass
        except OSError as e:
            log.warning("could not write %s: %s", rel, e)
            continue
        written[rel] = (album, kind, sig)
    return written, removed, dirs
