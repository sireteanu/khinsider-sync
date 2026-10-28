"""HTTP API + the single-page interface."""
from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import os
import logging
import re
import secrets
import urllib.parse
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .catalog import ALBUM, PLATFORM, split_platforms
from .parse import letter_of
from .engine import Engine

log = logging.getLogger("khinsider_sync")

STATIC = os.path.join(os.path.dirname(__file__), "static")
SORTS = ("name", "year", "added", "new", "size", "synced")
SHOWS = ("all", "ticked", "downloaded", "partly", "notdownloaded", "failed", "unread")


def public_settings(s: dict) -> dict:
    """Settings as the page gets them: never the khinsider password or session cookies."""
    out = dict(s)
    out["account_password_set"] = bool(out.pop("account_password", ""))
    out.pop("account_cookies", None)
    return out


def parse_types(text: str) -> tuple:
    """The type filter: "Gamerip,Soundtrack" -> ("Gamerip", "Soundtrack"); "Other" = albums without a type."""
    return tuple(sorted({t.strip() for t in str(text or "").split(",") if t.strip()}))


def album_json(e: Engine, a: dict, rules: dict, selected: set, settings: dict, names: dict) -> dict:
    t = e.catalog.album_totals(a["slug"], a["formats"], settings) if a["read"] else \
        {"files": 0, "bytes": 0, "synced": 0, "synced_bytes": 0, "failed": 0, "downloaded": False,
         "downloaded_at": None}
    own = rules.get(ALBUM + a["slug"])
    return {"slug": a["slug"], "name": a["name"], "title": a.get("title") or "", "type": a["type"] or "",
            "year": a["year"] or "", "added": a.get("added") or "", "read": a["read"], "error": a["error"],
            "missing": bool(a["missing"]), "formats": a["formats"], "tracks": a.get("tracks"),
            "platforms": [names.get(p, p) for p in a["platforms"]], "platform_slugs": a["platforms"],
            "state": "on" if a["slug"] in selected else "off", "own": own,
            "via": own is None and a["slug"] in selected,
            "own_formats": (settings.get("album_formats") or {}).get(a["slug"], ""),
            "changed": a.get("changed") or None, **t}


def create_app(engine_factory, password: Optional[str] = None, on_restart=None, on_shutdown=None) -> FastAPI:
    """on_restart / on_shutdown: called by POST /api/restart and /api/shutdown to restart or end the
    whole program (see __main__). Both keep an unfinished sync, which continues at the next start."""
    holder: dict = {}

    @contextlib.asynccontextmanager
    async def lifespan(app):
        engine = engine_factory()
        holder["engine"] = engine
        app.state.engine = engine
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
    async def platforms(type: str = ""):
        """Platforms with their album counts - of the album types in `type` (comma separated) when
        given, so the list matches what the album filter shows."""
        types = parse_types(type)
        e = eng()
        s = e.catalog.settings()
        rules = e.catalog.rules()
        states = e.catalog.platform_states(rules)
        count = collections.Counter()
        kinds = collections.Counter()
        none = 0
        for _slug, plats, kind in e.catalog.index():
            kinds[kind or "Other"] += 1
            if types and (kind or "Other") not in types:
                continue
            count.update(plats)
            none += not plats
        names = e.catalog.platform_names()
        out = [{"slug": p, "name": names.get(p, p), "albums": count.get(p, 0), "state": states.get(p, "off")}
               for p in names if count.get(p, 0) or states.get(p)]
        out.sort(key=lambda x: x["name"].casefold())
        total = sum(1 for _s, _p, k in e.catalog.index() if not types or (k or "Other") in types)
        return {"platforms": out, "types": sorted(kinds.items(), key=lambda kv: -kv[1]),
                "no_platform": none, "total": total, "platform_types": s.get("platform_types") or []}

    # the page scrolls through every album of a view, asking for a slice at a time: the view's
    # filtered and sorted list is kept for the next slices until anything in the catalog changes
    views: "collections.OrderedDict[tuple, list]" = collections.OrderedDict()

    def album_view(e: Engine, key: tuple, platform, q, type, show, sort, dir) -> list:
        """Album dicts of one view, filtered and sorted (cached)."""
        hit = views.get(key)
        if hit is not None:
            views.move_to_end(key)
            return hit
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
        if type:                                  # a tuple of types (parse_types)
            cond = []
            named = [t for t in type if t != "Other"]
            if named:
                cond.append("type IN (%s)" % ",".join("?" * len(named)))
                args += named
            if "Other" in type:
                cond.append("(type IS NULL OR type='')")
            where.append("(" + " OR ".join(cond) + ")")
        w = (" WHERE " + " AND ".join(where)) if where else ""
        with e.catalog.lock:
            rows = e.catalog.db.execute(f"SELECT {', '.join(e.catalog.COLS)} FROM albums{w}", args).fetchall()
        items = [e.catalog._album_dict(r) for r in rows]
        if show == "ticked":
            items = [a for a in items if a["slug"] in selected]
        elif show == "unread":
            items = [a for a in items if a["slug"] in selected and not a["read"]]
        # sizes and download state only where the filter or the sort needs them (they cost time
        # for 100,000 albums); otherwise only for the albums sent
        if show in ("downloaded", "partly", "notdownloaded", "failed") or sort in ("size", "synced"):
            for a in items:
                a["_t"] = e.catalog.album_totals(a["slug"], a["formats"], s) if a["read"] else None
            t = lambda a: a["_t"] or {"files": 0, "bytes": 0, "synced": 0, "synced_bytes": 0,  # noqa: E731
                                      "failed": 0, "downloaded": False}
            if show == "downloaded":
                items = [a for a in items if t(a)["downloaded"]]
            elif show == "partly":
                items = [a for a in items if t(a)["synced"] and not t(a)["downloaded"]]
            elif show == "notdownloaded":
                items = [a for a in items if not t(a)["downloaded"]]
            elif show == "failed":
                items = [a for a in items if t(a)["failed"] or a["error"]]
        else:
            t = None
        key_fn = {"name": lambda a: (a["name"].casefold(), a["slug"]),
                  "year": lambda a: (a["year"] or "0000", a["name"].casefold()),
                  "added": lambda a: (a["added"] or "0000", a["name"].casefold()),
                  "new": lambda a: (a["first_seen"] or 0, a["name"].casefold()),
                  "size": lambda a: (t(a)["bytes"], a["name"].casefold()),
                  "synced": lambda a: (t(a)["synced_bytes"] / (t(a)["bytes"] or 1) if t(a)["files"] else -1,
                                       a["name"].casefold()),
                  }[sort]
        if sort == "year":
            # newest (or oldest) release first; within a year the latest added to khinsider first,
            # then by name - and albums without a year at the end either way
            items.sort(key=lambda a: (a["name"].casefold(), a["slug"]))
            items.sort(key=lambda a: a["added"] or "", reverse=True)
            items.sort(key=lambda a: a["year"] or "", reverse=dir < 0)
            if dir > 0:
                items.sort(key=lambda a: not a["year"])
        else:
            items.sort(key=key_fn, reverse=dir < 0)
        views[key] = items
        while len(views) > 8:
            views.popitem(last=False)
        return items

    @app.get("/api/albums")
    async def albums(platform: str = "", q: str = "", type: str = "", show: str = "all", sort: str = "name",
                     dir: int = 1, offset: int = 0, limit: int = 200):
        e = eng()
        if sort not in SORTS:
            raise HTTPException(400, "sort must be one of " + ", ".join(SORTS))
        if show == "synced":                     # the name before 1.0.2
            show = "downloaded"
        if show not in SHOWS:
            raise HTTPException(400, "show must be one of " + ", ".join(SHOWS))
        rules = e.catalog.rules()
        s = e.catalog.settings()
        type = parse_types(type)
        key = (e.catalog.version, tuple(sorted(rules.items())), str(s.get("formats")), str(s.get("album_formats")),
               bool(s.get("album_art")), tuple(s.get("platform_types") or ()), platform, q, type, show, sort, dir)
        items = album_view(e, key, platform, q, type, show, sort, dir)
        offset = max(0, offset)
        limit = max(1, min(1000, limit))
        selected = set(e.catalog.selected_albums(rules, s.get("platform_types") or []))
        names = e.catalog.platform_names()
        page = [album_json(e, a, rules, selected, s, names) for a in items[offset:offset + limit]]
        out = {"total": len(items), "offset": offset, "albums": page}
        if sort == "name":
            out["letters"] = letter_index(key, items)
        return out

    # where each letter starts in a view sorted by name ("#" = digits and symbols), for the A-Z strip
    jumps: "collections.OrderedDict[tuple, dict]" = collections.OrderedDict()

    def letter_index(key: tuple, items: list) -> dict:
        hit = jumps.get(key)
        if hit is None:
            hit = {}
            for i, a in enumerate(items):
                c = letter_of(a["name"])
                hit.setdefault("#" if c == "0-9" else c, i)
            jumps[key] = hit
            while len(jumps) > 8:
                jumps.popitem(last=False)
        return hit

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
        want = e.catalog.wants(s, slug, a["formats"])
        files = []
        for r in rows:
            f = dict(zip(keys, r))
            f["wanted"] = want.has(f["url"], f["ext"], f["kind"])
            f["stand_in"] = f["url"] in want.stand_ins
            files.append(f)
        places = s.get("album_places") or {}
        return {"album": album_json(e, a, rules, selected, s, e.catalog.platform_names()),
                "dir": e.album_dir(a, s), "place": places.get(slug, ""), "files": files,
                "formats": (s.get("album_formats") or {}).get(slug, ""), "default_formats": s.get("formats"),
                "upgrade": e.upgrade_info(slug, s),
                "page": f"{e.base}game-soundtracks/album/{slug}"}

    @app.post("/api/account/list")
    async def account_list():
        """Download my khinsider list: every album on the account's "My Mass Downloads"."""
        if not eng().start("list"):
            raise HTTPException(409, "already busy")
        return {"started": True}

    @app.post("/api/account/check")
    async def account_check():
        return await eng().check_account()

    @app.post("/api/flaresolverr/check")
    async def flaresolverr_check():
        return await eng().check_flaresolverr()

    @app.post("/api/album/read")
    async def album_read(slug: str = Body(..., embed=True)):
        e = eng()
        if e.catalog.album(slug) is None:
            raise HTTPException(404, "no such album")
        res = await e.read_album_now(slug)
        new = (res.get("renamed") or {}).get(slug, slug)
        return dict(res, slug=new)

    @app.post("/api/album/check")
    async def album_check(slug: str = Body(..., embed=True)):
        try:
            return eng().check_album(slug)
        except ValueError as exc:
            raise HTTPException(404, str(exc))

    @app.post("/api/album/delete")
    async def album_delete(slug: str = Body(...), confirm: bool = Body(False)):
        """Delete an album's files from the download folder and untick it. Without confirm: what would go."""
        e = eng()
        if confirm and e.busy:
            raise HTTPException(409, "wait until the running job has finished")
        try:
            return await e.delete_album(slug, confirm)
        except KeyError:
            raise HTTPException(404, "no such album")

    @app.post("/api/album/upgrade")
    async def album_upgrade(slug: str = Body(...), format: str = Body("")):
        """Replace an album's files with a better format ("" = cancel a waiting upgrade)."""
        try:
            return eng().upgrade_album(slug, format)
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0]))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/api/notify/test")
    async def notify_test(url: str = Body(..., embed=True)):
        """Send a test notification to this address (not saved)."""
        from . import notify
        try:
            await notify.send(url, "Khinsider Sync", "Test notification - notifications work.")
        except Exception as exc:
            return JSONResponse({"error": str(exc) or type(exc).__name__}, status_code=400)
        return {"ok": True}

    @app.post("/api/check")
    async def check_folder():
        """Look through the download folder for downloaded files that were deleted."""
        if not eng().start("check"):
            raise HTTPException(409, "already busy")
        return {"ok": True}

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

    @app.post("/api/select/list")
    async def select_list(text: str = Body(..., embed=True), include: bool = Body(True, embed=True)):
        """Tick the albums of a pasted / uploaded list: one per line - khinsider album addresses or
        their last part (the slug); the first column of a CSV / tab-separated line counts; lines
        starting with # are skipped. -> {ticked, unknown: [lines]}"""
        e = eng()
        slugs, unknown, seen = [], [], set()
        for line in str(text or "").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            first = re.split(r"[\t,;]", raw, maxsplit=1)[0].strip().strip('"')
            m = re.search(r"/game-soundtracks/album/([^/?#\s]+)", first)
            slug = urllib.parse.unquote(m.group(1)) if m else first
            if slug in seen:
                continue
            seen.add(slug)
            if e.catalog.album(slug) is None:
                unknown.append(raw)
            else:
                slugs.append(slug)
        n = e.catalog.select_albums(slugs, include)
        log.info("%s %d albums from a list%s", "Ticked" if include else "Unticked", n,
                 f" ({len(unknown)} lines not found in the catalog)" if unknown else "")
        return {"ticked": n, "unknown": unknown[:200], "unknown_count": len(unknown)}

    @app.post("/api/selection/clear")
    async def clear_selection():
        eng().catalog.clear_selection()
        return {"ok": True}

    # ---------------------------------------------------------------- jobs
    @app.post("/api/scan")
    async def scan(quick: bool = Body(False, embed=True)):
        """Read khinsider's album lists: all of them, or (quick) only the newest albums."""
        if not eng().start("quick" if quick else "scan"):
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
    async def sync(albums: list = Body(None, embed=True)):
        """Sync every ticked album, or only the given ones (they are ticked if they aren't)."""
        e = eng()
        slugs = [str(a) for a in albums] if albums else None
        if slugs:
            if e.busy:
                raise HTTPException(409, "already busy")
            for slug in slugs:
                if e.catalog.album(slug) is None:
                    raise HTTPException(404, f"no such album: {slug}")
            chosen = set(e.catalog.selected_albums())
            for slug in slugs:
                if slug not in chosen:
                    e.catalog.select(ALBUM + slug, True)
        if not e.start("sync", slugs):
            raise HTTPException(409, "already busy")
        return {"ok": True}

    @app.post("/api/stop")
    async def stop():
        eng().stop()
        return {"ok": True}

    @app.get("/api/settings")
    async def get_settings():
        e = eng()
        return {"settings": public_settings(e.catalog.settings()), "data_locked": e.data_locked}

    @app.post("/api/settings")
    async def set_settings(values: dict = Body(...)):
        try:
            return {"settings": public_settings(eng().update_settings(values)), "data_locked": eng().data_locked}
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
        job = e.job if e.job and e.job.get("kind") in ("sync", "scan+sync", "quick+sync") else None
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
