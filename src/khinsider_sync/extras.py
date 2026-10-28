"""Files the app makes next to a downloaded album's music, for media servers and players:

  playlists   "<album folder>.m3u8" - the album's music in track order (#EXTINF with length and
              title, paths relative to the playlist), one format: the first the album's formats
              setting puts first
  folder_art  "folder.jpg" - the album's cover (a PNG cover converted to JPEG with Pillow; without
              Pillow it stays folder.png): its FIRST image, which is
              the one khinsider shows for the album in its own lists (checked on the real site,
              October 2026 - the names say nothing: "BTD3.png", "SLPS-03515.jpg", "106282_front.jpg").
              Copied from the downloaded image, or - when images aren't synced - fetched on its own
              (one request), so it works with any formats setting.

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
import shutil

from .catalog import AUDIO_EXTENSIONS, SYNCED_WORDS, album_plan, format_fallback, formats_for
from .core.util import fs_path

log = logging.getLogger("khinsider_sync")

COVER_EXTS = (".jpg", ".jpeg", ".png")


def can_convert() -> bool:
    """Pillow installed: PNG covers become a real folder.jpg (else folder.png)."""
    import importlib.util
    return importlib.util.find_spec("PIL") is not None


def to_jpeg(data: bytes) -> bytes:
    """A PNG (or any image) as JPEG; see-through parts on black. Pillow refuses absurdly large images."""
    import io
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            back = Image.new("RGB", im.size, (0, 0, 0))
            back.paste(im, mask=im.getchannel("A"))
            im = back
        elif im.mode != "RGB":
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, "JPEG", quality=92, optimize=True)
        return out.getvalue()


def seconds(duration: str) -> int:
    """'2:47' / '1:02:03' -> seconds (-1 when unknown, as #EXTINF wants)."""
    try:
        n = 0
        for part in (duration or "").split(":"):
            n = n * 60 + int(part)
        return n if duration else -1
    except ValueError:
        return -1


def cover_kind(data: bytes) -> str:
    """'.jpg' / '.png' by the file's first bytes ('' for anything else)."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"\x89PNG":
        return ".png"
    return ""


def plan(engine, s: dict, slugs) -> dict:
    """{rel: (album, kind, sig, what)} the given (ticked, downloaded) albums should have now.
    `what`: the playlist's text; for a folder image ("copy", saved rel) or ("fetch", image url)."""
    want = {}
    if not (s.get("playlists") or s.get("folder_art")):
        return want
    albums = engine.catalog.albums_by_slug(slugs)
    words = ",".join("'%s'" % w for w in SYNCED_WORDS)
    with engine.catalog.lock:
        rows = engine.catalog.db.execute(
            f"SELECT album, ext, kind, rel, title, name, duration, local_size, pos FROM files "
            f"WHERE local IN ({words}) ORDER BY album, pos, ext").fetchall()
        covers = {a: (url, ext, rel, local) for a, url, ext, rel, local in engine.catalog.db.execute(
            "SELECT album, url, ext, rel, local FROM files WHERE kind='art' ORDER BY album, pos DESC")
            if a in albums}                          # pos DESC: the first image wins
    by_album: dict = {}
    for r in rows:
        if r[0] in albums:
            by_album.setdefault(r[0], []).append(r)
    for slug in covers:
        by_album.setdefault(slug, [])
    for slug, files in by_album.items():
        a = albums[slug]
        folder = engine.album_dir(a, s)
        if s.get("playlists"):
            order = [k for k in album_plan(s, slug, a["formats"]) if k in AUDIO_EXTENSIONS]
            fmt = next((k for k in order if any(f[1] == k and f[2] != "art" for f in files)), None)
            # one line per track: in the album's format, or the one standing in for it (format_fallback)
            _pick, rest = format_fallback(formats_for(s, slug), a["formats"])
            prefer = ([fmt] if fmt else []) + [e for e in rest if e != fmt]
            per_track: dict = {}
            for f in files:
                if f[2] != "art" and f[1] in prefer:
                    per_track.setdefault(f[8] // 100, {})[f[1]] = f
            tracks = [next(have[e] for e in prefer if e in have) for _p, have in sorted(per_track.items())]
            if tracks and fmt:
                lines = ["#EXTM3U", f"#PLAYLIST:{a['name']}"]
                for _al, _ext, _kind, rel, title, name, dur, _n, _pos in tracks:
                    path = rel[len(folder) + 1:] if rel.startswith(folder + "/") else os.path.basename(rel)
                    lines.append(f"#EXTINF:{seconds(dur)},{title or name}")
                    lines.append(path)
                text = "\n".join(lines) + "\n"
                base = folder.rsplit("/", 1)[-1]
                want[f"{folder}/{base}.m3u8"] = (slug, "playlist", hashlib.sha1(text.encode()).hexdigest(), text)
        cover = covers.get(slug)
        if s.get("folder_art") and cover and cover[1] in COVER_EXTS:
            url, ext, src, local = cover
            convert = ext == ".png" and can_convert()
            rel = f"{folder}/folder{'.png' if ext == '.png' and not convert else '.jpg'}"
            if src.lower() == rel.lower():
                continue                         # the cover is already named folder.jpg
            mark = "jpeg:" if convert else ""
            if local in SYNCED_WORDS:
                want[rel] = (slug, "folder_art", f"{mark}copy:{src}", ("copy", src, convert))
            else:
                want[rel] = (slug, "folder_art", f"{mark}fetch:{url}", ("fetch", url, convert))
    return want


def missing(root: str, have: dict, want: dict, albums: set) -> list:
    """Recorded playlists / folder images of these albums that aren't on disk any more (so they are
    written again). Only these albums are looked at - one lookup each."""
    out = []
    for rel, (album, _kind, _sig) in have.items():
        if album in albums and rel in want and not os.path.isfile(fs_path(os.path.join(root, *rel.split("/")))):
            out.append(rel)
    return out


async def fetch_covers(client, want: dict, have: dict) -> dict:
    """Download the covers that aren't synced as images ("fetch") and aren't written yet -> {rel: bytes}.
    Anything that isn't a JPEG/PNG image (an error page, a challenge) is skipped and tried next time."""
    got = {}
    for rel, (album, kind, sig, what) in want.items():
        if kind != "folder_art" or what[0] != "fetch" or (have.get(rel) or (None, None, None))[2] == sig:
            continue
        try:
            r = await client.get(what[1])
        except Exception as e:  # never fail a sync over a cover
            log.warning("could not fetch the cover for %s: %s", rel, e)
            continue
        if r.status_code != 200 or not cover_kind(r.content):
            log.warning("could not fetch the cover for %s (HTTP %s, not an image)", rel, r.status_code)
            continue
        got[rel] = r.content
    return got


def apply(root: str, want: dict, have: dict, own_albums: set, file_mode=None, fetched=None) -> tuple:
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
            elif what[0] == "copy" and not what[2]:
                shutil.copyfile(path_of(what[1]), dest)
            else:
                if what[0] == "copy":
                    with open(path_of(what[1]), "rb") as f:
                        data = f.read()
                elif rel in (fetched or {}):
                    data = fetched[rel]
                else:
                    continue                    # its download failed: next sync
                if what[2]:
                    try:
                        data = to_jpeg(data)
                    except Exception as e:      # not an image Pillow can read: try again next sync
                        log.warning("could not turn the cover of %s into a JPEG: %s", rel, e)
                        continue
                with open(dest, "wb") as f:
                    f.write(data)
            if os.path.exists(dest + ".part"):
                os.remove(dest + ".part")
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
