"""Tags of downloaded music files (mutagen): the album cover (setting `embed_cover`) and the basic
tags khinsider's files usually lack (setting `write_tags`) - title, album, track and disc number
(with totals) and year, all from the album page. A real khinsider MP3 (October 2026) carried no
tags but the encoder's name, so players showed "unknown album".

Nothing already in a file is changed: a picture inside stays, a tag that has a value stays; only
what is missing is added. Into MP3 (ID3), FLAC and OGG Vorbis / Opus (Vorbis comments, FLAC/OGG
pictures) and M4A; other formats are left alone. The cover is khinsider's own one: the album's
first image (see extras.py). Each file is handled once (files.cover / files.tags: "added" | "had" |
"skipped"); a file downloaded again is handled again.

Files are changed on a copy that then replaces the original, with the original's date and
permissions. The download record and the catalog get the new size, so the size checks (Check
download folder, resuming) don't take a tagged file for a damaged one. Without mutagen nothing is
changed (a warning in the log).
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
from typing import Optional

log = logging.getLogger("khinsider_sync")

FORMATS = (".mp3", ".flac", ".m4a", ".ogg", ".opus")
FIELDS = ("title", "album", "track", "tracks", "disc", "discs", "year")


def available() -> bool:
    import importlib.util
    return importlib.util.find_spec("mutagen") is not None


def mime_of(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"\x89PNG":
        return "image/png"
    return ""


def _flac_picture(data: bytes, mime: str):
    from mutagen.flac import Picture
    pic = Picture()
    pic.type = 3                                   # front cover
    pic.mime = mime
    pic.desc = "Cover"
    pic.data = data
    return pic


def _open(path: str, ext: str):
    if ext == ".mp3":
        from mutagen.id3 import ID3, ID3NoHeaderError
        try:
            return ID3(path)
        except ID3NoHeaderError:
            return None
    if ext == ".flac":
        from mutagen.flac import FLAC
        return FLAC(path)
    if ext == ".m4a":
        from mutagen.mp4 import MP4
        return MP4(path)
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    return (OggOpus if ext == ".opus" else OggVorbis)(path)


def _has_picture(f, ext: str) -> bool:
    if f is None:
        return False
    if ext == ".mp3":
        return bool(f.getall("APIC"))
    if ext == ".flac":
        return bool(f.pictures)
    if ext == ".m4a":
        return bool(f.tags and f.tags.get("covr"))
    return bool(f.tags and f.tags.get("metadata_block_picture"))


def has_picture(path: str, ext: str) -> bool:
    return _has_picture(_open(path, ext), ext)


def _num(n, total) -> str:
    return f"{n}/{total}" if n and total else str(n or "")


def _missing_tags(f, ext: str, tags: dict) -> dict:
    """The fields of `tags` the file has no value for."""
    want = {k: v for k, v in tags.items() if v not in (None, "", 0)}
    if f is None:
        return want
    if ext == ".mp3":
        have = {"title": "TIT2", "album": "TALB", "track": "TRCK", "disc": "TPOS", "year": "TDRC"}
        out = {k: v for k, v in want.items() if k in have and not str(f.get(have[k]) or "").strip()}
        if "track" in out and "tracks" in want:
            out["tracks"] = want["tracks"]
        if "disc" in out and "discs" in want:
            out["discs"] = want["discs"]
        return out
    if ext == ".m4a":
        t = f.tags or {}
        have = {"title": "\xa9nam", "album": "\xa9alb", "track": "trkn", "disc": "disk", "year": "\xa9day"}
        out = {k: v for k, v in want.items() if k in have and not t.get(have[k])}
        for a, b in (("track", "tracks"), ("disc", "discs")):
            if a in out and b in want:
                out[b] = want[b]
        return out
    t = f.tags or {}
    names = {"title": "title", "album": "album", "track": "tracknumber", "tracks": "tracktotal",
             "disc": "discnumber", "discs": "disctotal", "year": "date"}
    return {k: v for k, v in want.items() if not any(str(x).strip() for x in (t.get(names[k]) or []))}


def _write(path: str, ext: str, cover: Optional[tuple], tags: dict) -> None:
    """Add the cover (data, mime) and these tag fields to the file at `path`."""
    if ext == ".mp3":
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TDRC, TIT2, TPOS, TRCK
        try:
            f = ID3(path)
            version = 4 if f.version[1] >= 4 else 3
        except ID3NoHeaderError:
            f, version = ID3(), 3
        if "title" in tags:
            f.add(TIT2(encoding=3, text=tags["title"]))
        if "album" in tags:
            f.add(TALB(encoding=3, text=tags["album"]))
        if "track" in tags:
            f.add(TRCK(encoding=3, text=_num(tags["track"], tags.get("tracks"))))
        if "disc" in tags:
            f.add(TPOS(encoding=3, text=_num(tags["disc"], tags.get("discs"))))
        if "year" in tags:
            f.add(TDRC(encoding=3, text=str(tags["year"])))
        if cover:
            f.add(APIC(encoding=3, mime=cover[1], type=3, desc="Cover", data=cover[0]))
        f.save(path, v2_version=version)
        return
    f = _open(path, ext)
    if ext == ".m4a":
        from mutagen.mp4 import MP4Cover
        if f.tags is None:
            f.add_tags()
        t = f.tags
        if "title" in tags:
            t["\xa9nam"] = [tags["title"]]
        if "album" in tags:
            t["\xa9alb"] = [tags["album"]]
        if "track" in tags:
            t["trkn"] = [(int(tags["track"]), int(tags.get("tracks") or 0))]
        if "disc" in tags:
            t["disk"] = [(int(tags["disc"]), int(tags.get("discs") or 0))]
        if "year" in tags:
            t["\xa9day"] = [str(tags["year"])]
        if cover:
            fmt = MP4Cover.FORMAT_PNG if cover[1] == "image/png" else MP4Cover.FORMAT_JPEG
            t["covr"] = [MP4Cover(cover[0], imageformat=fmt)]
        f.save()
        return
    # FLAC and OGG: Vorbis comments
    if f.tags is None:
        f.add_tags()
    names = {"title": "title", "album": "album", "track": "tracknumber", "tracks": "tracktotal",
             "disc": "discnumber", "discs": "disctotal", "year": "date"}
    for k, v in tags.items():
        f.tags[names[k]] = [str(v)]
    if cover:
        if ext == ".flac":
            f.add_picture(_flac_picture(*cover))
        else:
            block = base64.b64encode(_flac_picture(*cover).write()).decode("ascii")
            f.tags["metadata_block_picture"] = [block]
    f.save()


def update(path: str, ext: str, cover: Optional[tuple] = None, tags: Optional[dict] = None) -> tuple:
    """Add what the file lacks: the cover (data, mime) and/or these tags (FIELDS).
    -> (cover result, tags result), each "added" | "had" | "skipped" | None (not asked).
    Raises on files mutagen can't read (they are left as they were)."""
    if ext not in FORMATS:
        return ("skipped" if cover else None), ("skipped" if tags is not None else None)
    f = _open(path, ext)
    add_cover = bool(cover) and not _has_picture(f, ext)
    add_tags = _missing_tags(f, ext, tags) if tags is not None else {}
    res = (("added" if add_cover else "had") if cover else None,
           ("added" if add_tags else "had") if tags is not None else None)
    if not (add_cover or add_tags):
        return res
    tmp = path + ".tagtmp"
    st = os.stat(path)
    shutil.copy2(path, tmp)
    try:
        _write(tmp, ext, cover if add_cover else None, add_tags)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    os.utime(path, (st.st_atime, st.st_mtime))       # the album's date stays
    try:
        os.chmod(path, st.st_mode & 0o7777)
    except OSError:
        pass
    return res


def embed(path: str, ext: str, data: bytes, mime: str) -> str:
    """Only the cover -> "added" | "had" | "skipped"."""
    return update(path, ext, cover=(data, mime))[0]
