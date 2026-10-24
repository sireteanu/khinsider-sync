"""HTTP API + the single-page interface."""
from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import os
import logging
import secrets
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .catalog import ALBUM, PLATFORM, album_wants, split_platforms
from .engine import Engine

log = logging.getLogger("khinsider_sync")

STATIC = os.path.join(os.path.dirname(__file__), "static")
SORTS = ("name", "year", "added", "new", "size", "synced")
SHOWS = ("all", "ticked", "synced", "failed", "unread")


def album_json(e: Engine, a: dict, rules: dict, selected: set, settings: dict, names: dict) -> dict:
    t = e.catalog.album_totals(a["slug"], a["formats"], settings) if a["read"] else \
        {"files": 0, "bytes": 0, "synced": 0, "synced_bytes": 0, "failed": 0}
    own = rules.get(ALBUM + a["slug"])
    return {"slug": a["slug"], "name": a["name"], "title": a.get("title") or "", "type": a["type"] or "",
            "year": a["year"] or "", "added": a.get("added") or "", "read": a["read"], "error": a["error"],
            "missing": bool(a["missing"]), "formats": a["formats"], "tracks": a.get("tracks"),
            "platforms": [names.get(p, p) for p in a["platforms"]], "platform_slugs": a["platforms"],
            "state": "on" if a["slug"] in selected else "off", "own": own,
            "via": own is None and a["slug"] in selected,
            "own_formats": (settings.get("album_formats") or {}).get(a["slug"], ""), **t}


def create_app(engine_factory, password: Optional[str] = None, on_restart=None, on_shutdown=None) -> FastAPI:
    """on_restart / on_shutdown: called by POST /api/restart and /api/shutdown to restart or end the
    whole program (see __main__). Both keep an unfinished sync, which continues at the next start."""
    holder: dict = {}

    @contextlib.asynccontextmanager
    async def lifespan(app):
        engine = engine_factory()
        holder["engine"] = engine
        engine.scheduler = asyncio.create_task(engine.scheduler_loop())
        try:
            yield
        finally:
            engine.shutdown()
            engine.scheduler.cancel()
            if engine.task:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(engine.task, 10)
            engine.catalog.close()

    app = FastAPI(title="khinsider-sync", lifespan=lifespan, docs_url=None, redoc_url=None)

    def eng() -> Engine:
        return holder["engine"]

    # album lists and file lists are large JSON that compresses well
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    if password:
        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            if request.url.path == "/api/health":          # Docker health check
                return await call_next(request)
            header = request.headers.get("authorization", "")
            ok = False
            if header.lower().startswith("basic "):
                try:
                    _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                    ok = secrets.compare_digest(given, password)
                except Exception:
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="khinsider-sync"'})
            return await call_next(request)

    @app.get("/")
    async def index():
        # the page asks for its script and styles by version, so a browser never runs an old
        # cached app.js against a newer page after an update
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
            html = f.read()
        for name in ("app.js", "app.css"):
            html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={__version__}"')
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/status")
    async def status(log_since: int = 0):
        return eng().status(log_since)

    # ---------------------------------------------------------------- browsing
    @app.get("/api/platforms")
    async def platforms():
        e = eng()
        s = e.catalog.settings()
        rules = e.catalog.rules()
        states = e.catalog.platform_states(rules)
        count = collections.Counter()
        kinds = collections.Counter()
        none = 0
        for _slug, plats, kind in e.catalog.index():
            count.update(plats)
            kinds[kind or "Other"] += 1
            none += not plats
        names = e.catalog.platform_names()
        out = [{"slug": p, "name": names.get(p, p), "albums": count.get(p, 0), "state": states.get(p, "off")}
               for p in names if count.get(p, 0) or states.get(p)]
        out.sort(key=lambda x: x["name"].casefold())
        return {"platforms": out, "types": sorted(kinds.items(), key=lambda kv: -kv[1]),
                "no_platform": none, "platform_types": s.get("platform_types") or []}

    @app.get("/api/albums")
    async def albums(platform: str = "", q: str = "", type: str = "", show: str = "all", sort: str = "name",
                     dir: int = 1, offset: int = 0, limit: int = 200):
        e = eng()
        if sort not in SORTS:
            raise HTTPException(400, "sort must be one of " + ", ".join(SORTS))
        if show not in SHOWS:
            raise HTTPException(400, "show must be one of " + ", ".join(SHOWS))
        s = e.catalog.settings()
        rules = e.catalog.rules()
        selected = set(e.catalog.selected_albums(rules, s.get("platform_types") or []))
        where, args = [], []
        if platform == "-":
            where.append("(platforms IS NULL OR platforms='')")
        elif platform:
            where.append("platforms LIKE ?")
            args.append(f"%|{platform}|%")
        for word in q.split():
            where.append("(name LIKE ? OR title LIKE ? OR slug LIKE ?)")
            args += [f"%{word}%"] * 3
        if type:
            where.append("type=?")
            args.append(type)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        with e.catalog.lock:
            rows = e.catalog.db.execute(f"SELECT {', '.join(e.catalog.COLS)} FROM albums{w}", args).fetchall()
        items = [e.catalog._album_dict(r) for r in rows]
        if show == "ticked":
            items = [a for a in items if a["slug"] in selected]
        elif show == "unread":
            items = [a for a in items if a["slug"] in selected and not a["read"]]
        names = e.catalog.platform_names()
        out = [album_json(e, a, rules, selected, s, names) for a in items]
        if show == "synced":
            out = [a for a in out if a["synced"]]
        elif show == "failed":
            out = [a for a in out if a["failed"] or a["error"]]
        key = {"name": lambda a: (a["name"].casefold(), a["slug"]),
               "year": lambda a: (a["year"] or "0000", a["name"].casefold()),
               "added": lambda a: (a["added"] or "0000", a["name"].casefold()),
               "new": lambda a: (a.get("_first") or 0, a["name"].casefold()),
               "size": lambda a: (a["bytes"], a["name"].casefold()),
               "synced": lambda a: (a["synced_bytes"] / (a["bytes"] or 1) if a["files"] else -1, a["name"].casefold()),
               }[sort]
        if sort == "new":
            first = {a["slug"]: a["first_seen"] or 0 for a in items}
            for a in out:
                a["_first"] = first.get(a["slug"], 0)
        out.sort(key=key, reverse=dir < 0)
        for a in out:
            a.pop("_first", None)
        limit = max(1, min(1000, limit))
        return {"total": len(out), "offset": offset, "albums": out[offset:offset + limit]}

    @app.get("/api/album")
    async def album(slug: str):
        e = eng()
        a = e.catalog.album(slug)
        if a is None:
            raise HTTPException(404, "no such album")
        s = e.catalog.settings()
        rules = e.catalog.rules()
        selected = set(e.catalog.selected_albums(rules, s.get("platform_types") or []))
        with e.catalog.lock:
            rows = e.catalog.db.execute(
                "SELECT url, ext, kind, disc, num, name, title, duration, rel, size, local, local_size, error "
                "FROM files WHERE album=? ORDER BY pos, ext", (slug,)).fetchall()
        keys = ("url", "ext", "kind", "disc", "num", "name", "title", "duration", "rel", "size", "local",
                "local_size", "error")
        want = album_wants(s, slug, a["formats"])
        files = []
        for r in rows:
            f = dict(zip(keys, r))
            f["wanted"] = ("art" if f["kind"] == "art" else f["ext"]) in want
            files.append(f)
        places = s.get("album_places") or {}
        return {"album": album_json(e, a, rules, selected, s, e.catalog.platform_names()),
                "dir": e.album_dir(a, s), "place": places.get(slug, ""), "files": files,
                "formats": (s.get("album_formats") or {}).get(slug, ""), "default_formats": s.get("formats"),
                "page": f"{e.base}game-soundtracks/album/{slug}"}

    @app.post("/api/album/read")
    async def album_read(slug: str = Body(..., embed=True)):
        e = eng()
        if e.catalog.album(slug) is None:
            raise HTTPException(404, "no such album")
        res = await e.read_album_now(slug)
        new = (res.get("renamed") or {}).get(slug, slug)
        return dict(res, slug=new)

    @app.post("/api/select")
    async def select(key: str = Body(...), include: bool = Body(...)):
        e = eng()
        if key.startswith(ALBUM):
            if e.catalog.album(key[len(ALBUM):]) is None:
                raise HTTPException(404, "no such album")
        elif key.startswith(PLATFORM):
            if key[len(PLATFORM):] not in e.catalog.platform_names():
                raise HTTPException(404, "no such platform")
        else:
            raise HTTPException(400, "key must be a:<album> or p:<platform>")
        e.catalog.select(key, include)
        return {"ok": True}

    @app.post("/api/selection/clear")
    async def clear_selection():
        eng().catalog.clear_selection()
        return {"ok": True}

    # ---------------------------------------------------------------- jobs
    @app.post("/api/scan")
    async def scan():
        if not eng().start("scan"):
            raise HTTPException(409, "already busy")
        return {"ok": True}

    @app.post("/api/read")
    async def read(albums: list = Body(None, embed=True)):
        """Read album pages: the given ones, or every ticked album."""
        e = eng()
        slugs = [str(a) for a in albums] if albums else None
        if not (slugs or e.catalog.selected_albums()):
            raise HTTPException(400, "tick some albums first")
        if not e.start("read", slugs):
            raise HTTPException(409, "already busy")
        return {"ok": True}

    @app.post("/api/sync")
    async def sync():
        if not eng().start("sync"):
            raise HTTPException(409, "already busy")
        return {"ok": True}

    @app.post("/api/stop")
    async def stop():
        eng().stop()
        return {"ok": True}

    @app.get("/api/settings")
    async def get_settings():
        e = eng()
        return {"settings": e.catalog.settings(), "data_locked": e.data_locked}

    @app.post("/api/settings")
    async def set_settings(values: dict = Body(...)):
        try:
            return {"settings": eng().update_settings(values), "data_locked": eng().data_locked}
        except (ValueError, Exception) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/api/album-place")
    async def album_place(body: dict = Body(...)):
        try:
            return eng().set_place(body.get("slug") or "", body.get("place"))
        except RuntimeError:
            return JSONResponse({"error": "Wait until the scan or sync has finished, then try again."},
                                status_code=409)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/api/album-formats")
    async def album_formats(body: dict = Body(...)):
        try:
            return eng().set_album_formats(body.get("slug") or "", body.get("formats"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/api/history")
    async def history():
        return {"history": eng().catalog.history(20)}

    # ---------------------------------------------------------------- what changed
    SYNC_KINDS = ("new", "updated", "resumed", "uptodate", "failed")

    @app.get("/api/sync/files")
    async def sync_files(kind: str, limit: int = 2000):
        """The files behind one of the Activity counters, for the current or last sync."""
        e = eng()
        if kind not in SYNC_KINDS:
            raise HTTPException(400, "kind must be one of " + ", ".join(SYNC_KINDS))
        job = e.job if e.job and e.job.get("kind") in ("sync", "scan+sync") else None
        if job is None:
            return {"total": 0, "files": [], "job": None}
        if kind == "uptodate":
            total, rows = e.catalog.unchanged_since(job["started"], limit)
        else:
            total, rows = e.catalog.changes(job=job["started"], kind=kind, limit=limit)
        return {"total": total, "files": rows, "job": job["started"]}

    @app.get("/api/changes/days")
    async def change_days():
        return {"days": eng().catalog.change_days()}

    @app.get("/api/changes")
    async def changes(day: str = "", kind: str = "", limit: int = 2000):
        total, rows = eng().catalog.changes(day=day or None, kind=kind or None, limit=limit)
        return {"total": total, "files": rows}

    # ---------------------------------------------------------------- clean up
    @app.get("/api/cleanup")
    async def cleanup_list():
        e = eng()
        files = await e.find_leftovers()
        return {"files": files, "summary": e.leftovers, "busy": e.busy}

    @app.post("/api/cleanup/delete")
    async def cleanup_delete(paths: list = Body(..., embed=True), confirm: bool = Body(False, embed=True)):
        e = eng()
        if not confirm:
            raise HTTPException(400, "confirm the delete first")
        if e.busy:
            raise HTTPException(409, "wait until the scan or sync has finished, then try again")
        res = await e.delete_leftovers([str(p) for p in paths])
        return dict(res, summary=e.leftovers)

    # ---------------------------------------------------------------- restart
    @app.post("/api/restart")
    async def restart():
        if on_restart is None:
            raise HTTPException(501, "restart is not available when started this way")
        log.info("Restarting Khinsider Sync...")
        asyncio.get_running_loop().call_later(0.3, on_restart)
        return {"ok": True}

    @app.post("/api/shutdown")
    async def shutdown():
        if on_shutdown is None:
            raise HTTPException(501, "shutting down is not available when started this way")
        log.info("Shutting down Khinsider Sync (from the web page)...")
        asyncio.get_running_loop().call_later(0.3, on_shutdown)
        return {"ok": True, "docker": eng().data_locked}

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    return app


__all__ = ["create_app", "split_platforms"]
