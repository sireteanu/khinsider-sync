"""Clean up: find files in the download folder that are no longer part of the sync, and delete
the ones the user confirmed.

A file on disk is a leftover when
  * "gone"     - Khinsider Sync downloaded it (it is in the download record), but the site no longer
                 lists it: its album page was read without it, or the album itself is gone and a
                 full scan has finished since;
  * "unticked" - the site still lists it, but its album isn't ticked any more, or its format (or the
                 album art) isn't synced;
  * "part"     - an unfinished download (.part) of a file that isn't wanted any more.
Files Khinsider Sync didn't download and doesn't know are never listed - other music you keep in
the same folder is safe. Nothing is deleted without an explicit list of paths, and only paths that
are still leftovers when the delete runs are removed. Symlinks are never followed or deleted; the
download record and other dot-files are ignored.
"""
from __future__ import annotations

import logging
import os
import sqlite3

from .core.sync import MANIFEST_NAME, Manifest
from .core.util import fmt_bytes, fs_path
from .catalog import wanted_exts

log = logging.getLogger("dircrawl")

REASONS = {
    "gone": "No longer on the site",
    "unticked": "Not ticked any more",
    "part": "Unfinished download that isn't needed",
}
IGNORED = {"thumbs.db", "desktop.ini", ".ds_store"}


def walk(root: str) -> list:
    """[(rel dir, name, size)] for every regular file under root (runs in a thread)."""
    out = []
    root = fs_path(root)
    if not os.path.isdir(root):
        return out

    def scan(path: str, rel: str):
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            return
        for e in entries:
            if e.name.startswith(".") or e.name.lower() in IGNORED:
                continue
            try:
                if e.is_symlink():
                    continue
                if e.is_dir():
                    scan(e.path, f"{rel}/{e.name}" if rel else e.name)
                elif e.is_file():
                    out.append((rel, e.name, e.stat().st_size))
            except OSError:
                continue

    scan(root, "")
    return out


def downloaded(root: str) -> set:
    """Paths the download record knows (what Khinsider Sync saved)."""
    path = os.path.join(root, MANIFEST_NAME)
    if not os.path.exists(path):
        return set()
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return {r[0] for r in db.execute("SELECT path FROM files")}
        finally:
            db.close()
    except sqlite3.Error:
        return set()


def find(engine, settings: dict, disk: list) -> list:
    """Judge the files found on disk -> [{path, size, reason[, url]}] (runs in a worker thread)."""
    catalog = engine.catalog
    listed = catalog.files_by_rel()
    selected = set(catalog.selected_albums(None, settings.get("platform_types") or []))
    full = catalog.last_scan("full")
    full_done = bool(full and full.get("status") == "done")
    mine = downloaded(settings["data_dir"])
    albums = catalog.albums_by_slug({a for _, a, _, _ in listed.values()})
    by_dir: dict = {}                     # album folder on disk -> album slug (read albums only)
    for slug, a in albums.items():
        if a["read"]:
            by_dir.setdefault(engine.album_dir(a, settings), []).append(slug)
    want_cache: dict = {}

    def wanted(hit) -> bool:
        _url, album, ext, kind = hit
        if album not in selected:
            return False
        if album not in want_cache:
            a = albums.get(album)
            want_cache[album] = wanted_exts(settings.get("formats"), a["formats"] if a else [],
                                            bool(settings.get("album_art")))
        return ("art" if kind == "art" else ext) in want_cache[album]

    def judged_gone(d: str) -> bool:
        """Can we tell this file isn't on the site? Its album was read without errors - or no
        known album lives in its folder any more and a full scan has finished since."""
        slugs = by_dir.get(d)
        if slugs:
            return all(albums[s]["read"] and not albums[s]["error"] for s in slugs)
        return full_done

    out = []
    for d, name, size in disk:
        rel = f"{d}/{name}" if d else name
        if not d and name.startswith(MANIFEST_NAME):
            continue
        if name.endswith(".part"):
            base = rel[:-5]
            hit = listed.get(base)
            if hit and wanted(hit):
                continue                                   # still wanted: the next sync resumes it
            if hit or base in mine or d in by_dir:
                out.append({"path": rel, "size": size, "reason": "part"})
            continue
        hit = listed.get(rel)
        if hit:
            if not wanted(hit):
                out.append({"path": rel, "size": size, "reason": "unticked", "url": hit[0]})
            continue
        if rel in mine and judged_gone(d):
            out.append({"path": rel, "size": size, "reason": "gone"})
    out.sort(key=lambda f: f["path"].casefold())
    return out


def summary(files: list) -> dict:
    by = {k: {"files": 0, "bytes": 0} for k in REASONS}
    for f in files:
        by[f["reason"]]["files"] += 1
        by[f["reason"]]["bytes"] += f["size"]
    return {"files": len(files), "bytes": sum(f["size"] for f in files), "by_reason": by}


def remove_empty_dirs(root: str, dirs) -> None:
    """Remove these folders (deepest first) and their parents once empty; never the root."""
    root_abs = os.path.abspath(root)
    for d in sorted({os.path.abspath(x) for x in dirs}, key=len, reverse=True):
        while d.startswith(root_abs + os.sep):
            try:
                os.rmdir(fs_path(d))
            except OSError:
                break
            d = os.path.dirname(d)


def delete(root: str, files: list) -> dict:
    """Delete these leftovers (runs in a thread) and remove folders left empty by it.
    -> {deleted: [paths], bytes, errors: [text]}"""
    res = {"deleted": [], "bytes": 0, "errors": []}
    root_abs = os.path.abspath(root)
    dirs = set()
    for f in files:
        rel = f["path"]
        path = os.path.abspath(os.path.join(root_abs, *rel.split("/")))
        if not path.startswith(root_abs + os.sep) or os.path.islink(path):
            res["errors"].append(f"{rel}: not inside the download folder")
            continue
        try:
            os.remove(fs_path(path))
            res["deleted"].append(rel)
            res["bytes"] += f["size"]
            dirs.add(os.path.dirname(path))
        except FileNotFoundError:
            res["deleted"].append(rel)
        except OSError as e:
            res["errors"].append(f"{rel}: {e.strerror or e}")
    remove_empty_dirs(root, dirs)
    if res["deleted"]:
        m = Manifest(root)
        try:
            m.forget(res["deleted"])
        finally:
            m.close()
    return res


def describe(res: dict) -> str:
    out = f"Clean up: deleted {len(res['deleted'])} files ({fmt_bytes(res['bytes'])})"
    if res["errors"]:
        out += f"; {len(res['errors'])} could not be deleted: " + "; ".join(res["errors"][:5])
    return out
