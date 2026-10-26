"""Embed the album cover into downloaded music files (setting `embed_cover`), so players that only
read tags show it too.

The cover is khinsider's own one: the album's first image (see extras.py). Into MP3 (ID3 APIC),
FLAC (PICTURE block), M4A (covr) and OGG Vorbis / Opus (METADATA_BLOCK_PICTURE); other formats are
left alone. A file that already carries a picture keeps it - nothing is replaced. Each file is
handled once (files.cover: "added" | "had" | "skipped"); a file downloaded again is handled again.

Files are tagged on a copy that then replaces the original, with the original's date and
permissions. The download record and the catalog get the new size, so the size checks (Check
download folder, resuming) don't take a tagged file for a damaged one. Needs mutagen; without it
nothing is embedded (a warning in the log).
"""
from __future__ import annotations

import base64
import logging
import os
import shutil

log = logging.getLogger("khinsider_sync")

FORMATS = (".mp3", ".flac", ".m4a", ".ogg", ".opus")


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


def has_picture(path: str, ext: str) -> bool:
    """Does this file carry a picture already?"""
    if ext == ".mp3":
        from mutagen.id3 import ID3, ID3NoHeaderError
        try:
            return bool(ID3(path).getall("APIC"))
        except ID3NoHeaderError:
            return False
    if ext == ".flac":
        from mutagen.flac import FLAC
        return bool(FLAC(path).pictures)
    if ext == ".m4a":
        from mutagen.mp4 import MP4
        tags = MP4(path).tags
        return bool(tags and tags.get("covr"))
    if ext in (".ogg", ".opus"):
        from mutagen.oggopus import OggOpus
        from mutagen.oggvorbis import OggVorbis
        f = (OggOpus if ext == ".opus" else OggVorbis)(path)
        return bool(f.tags and f.tags.get("metadata_block_picture"))
    return False


def _add(path: str, ext: str, data: bytes, mime: str) -> None:
    if ext == ".mp3":
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError
        try:
            tags = ID3(path)
            version = 4 if tags.version[1] >= 4 else 3
        except ID3NoHeaderError:
            tags, version = ID3(), 3
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        tags.save(path, v2_version=version)
    elif ext == ".flac":
        from mutagen.flac import FLAC
        f = FLAC(path)
        f.add_picture(_flac_picture(data, mime))
        f.save()
    elif ext == ".m4a":
        from mutagen.mp4 import MP4, MP4Cover
        f = MP4(path)
        if f.tags is None:
            f.add_tags()
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        f.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
        f.save()
    elif ext in (".ogg", ".opus"):
        from mutagen.oggopus import OggOpus
        from mutagen.oggvorbis import OggVorbis
        f = (OggOpus if ext == ".opus" else OggVorbis)(path)
        block = base64.b64encode(_flac_picture(data, mime).write()).decode("ascii")
        f["metadata_block_picture"] = [block]
        f.save()


def embed(path: str, ext: str, data: bytes, mime: str) -> str:
    """-> "added" | "had" (it has a picture already) | "skipped" (format without pictures).
    Raises on files mutagen can't read (they are left as they were)."""
    if ext not in FORMATS:
        return "skipped"
    if has_picture(path, ext):
        return "had"
    tmp = path + ".covertmp"
    st = os.stat(path)
    shutil.copy2(path, tmp)
    try:
        _add(tmp, ext, data, mime)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    os.utime(path, (st.st_atime, st.st_mtime))       # the album's date stays
    try:
        os.chmod(path, st.st_mode & 0o7777)
    except OSError:
        pass
    return "added"
