# Khinsider Sync (khinsider-sync)

Web app for keeping local copies of the downloads.khinsider.com soundtracks you pick. It reads the
site's album lists into a catalog, shows every album with a platform list on the left, syncs the
ticked albums (and whole ticked platforms, including albums added later), and cleans up files that
aren't part of the sync any more. Built on the Rom Sync framework (sireteanu/rom-sync): `core/` is
its sync engine (downloader, manifest, parallel-download gate, outage handling) with one addition,
the download-address resolver, and `core/browser.py` (requests look like Chrome through curl_cffi,
learned from the khi-explorer downloaders by madLinux7 and whxjwksjs). Python 3.9+, async (httpx), FastAPI.
It runs on **Windows** (`start.bat`) and in **Docker on Unraid** - keep both working: Windows paths
and console, and PUID/PGID/UMASK plus `/config` and `/data` in Docker.

## Commands
- Install (dev): `pip install -e ".[dev]"`
- Tests: `python -m pytest` (~90 s; all traffic goes to the in-process `tests/mock_site.py`);
  the `Tests` workflow runs them and pyflakes on every push (Python 3.9 and 3.12)
- Lint: `python -m pyflakes src tests`
- Web app: `python run.py [--data DIR] [--port 8485] [--host 0.0.0.0] [--password X]`
  (Windows users: `start.bat`; installed: `khinsider-sync`)
- Docker: `docker build -t khinsider-sync .` / `docker compose up -d --build`

## Layout
```
src/khinsider_sync/
  __main__.py   options / env vars (KHINSIDER_DATA, _CONFIG, _HOST, _PORT, _PASSWORD, _DATA_LOCKED)
  web.py        FastAPI routes (status, platforms, albums, album, select, scan, read, sync, settings ...);
                /api/albums slices a cached view (album_view) - the page's list scrolls through all of it
  engine.py     one job at a time (scan | quick | read | sync | scan+sync | quick+sync | check), Resolver, where albums go
                (album_dir), moving saved files, progress, LogBuffer, scheduler
  catalog.py    SQLite <config>/catalog.db: albums, platforms, files, selection, settings, scans,
                history, changes; wanted_exts() decides which formats/images a sync saves
  scanner.py    scan_site() reads /console-list + every browse page; scan_latest() the home page's newest
                albums (quick scan, falls back to a full scan); read_albums() reads album pages
  parse.py      every page reader (browse list, latest list, album page, track page, platform list)
  extras.py     optional playlists (.m3u8) and folder.jpg per downloaded album, tracked in `extras`
  covers.py     optional cover and missing tags put into music files (mutagen); files.cover / files.tags,
                Manifest.resized; one pass at the end of a sync (`Engine._tag_files`, `_finished_albums`)
  notify.py     notifications after jobs (ntfy / Gotify / Discord / JSON webhooks), never blocking a job
  cleanup.py    Clean up: walk the download folder, judge files (gone / part / unticked), delete
  static/       index.html, app.css, app.js - no build step, no CDN (works offline on a LAN)
  core/         the sync engine from Rom Sync
    download.py Downloader: plan() + download(); `resolve` hook (ResolveError); run_item()
    sync.py     FileInfo, Manifest (<data>/.khinsider-sync.db, by_url()/move()), plan()
    limits.py   Limiter (page pacing), DownloadGate (auto parallel downloads), RateLimit
    browser.py  ChromeTransport (httpx transport over curl_cffi impersonating Chrome; maps curl errors
                to httpx exceptions, drops content-encoding because curl already decoded)
    util.py / constants.py (formats, BEST_ORDER, MAGIC) / config.py
tests/  conftest (app_factory + API helpers), mock_site, fixtures/ (real markup samples),
        test_parse, test_app, test_engine, test_cleanup, test_changes, test_outage
```

## How khinsider works (verified in a browser, October 2026 - see README "Facts")
- The home page `/?page=N` lists the newest albums (8 days a page under `h3.latestSoundtrackHeading`,
  one `table.albumList` each) - the quick scan reads it.
- ~106,000 albums. `/game-soundtracks/browse/<0-9|A..Z>?page=N` lists 500 albums a page in
  `table.albumList` (cover | name | platforms | type | year) - about 230 pages in all. A full scan
  reads only these plus `/console-list`; it never reads album pages or files.
- An album page (`/game-soundtracks/album/<slug>`) has the details paragraph (Platforms, Year,
  Date Added "Dec 17th, 2025", Album type), `.albumImage a` full-size images, and `table#songlist`:
  header `#songlist_header` (optional CD and # columns, `Song Name` colspan 2, one column per
  format: MP3, FLAC, OGG, M4A ...), one row per track, footer `#songlist_footer`. Row cells line up
  with the header expanded by colspan. A missing album answers 200 with `<h2>Ooops!</h2>`.
  Renamed albums redirect to their new slug.
- A track's page is `.../album/<slug>/<file name encoded twice>.mp3` (the same page for every
  format). Its `.songDownloadLink` links point at the file on a vgmtreasurechest.com server
  (host and a per-album token vary), so **the download address is only known from the track page**:
  `engine.Resolver` reads it right before the download (one request per track, cached for its other
  formats). File names are the page's name decoded twice with the format's extension.
- Cloudflare guards the pages: data-center addresses (this cloud workspace) get the "Just a moment..."
  browser check (403, `cf-mitigated: challenge`) for every page whatever the fingerprint; the file
  servers still answer. `util.is_challenge` -> `ChallengeError` stops scans, `ResolveError(pause=...)`
  pauses syncs; nothing is marked failed. Never try to solve or get around the check.
  robots.txt comes as text/html.
- Sizes are shown in MB with two decimals ("5.64 MB"); totals use thousands commas ("1,438 MB").
  No per-file dates: files get the album's Date Added (12:00 local time).

## Rules (keep these)
1. **Scans never touch album pages or files** (the quick scan reads only the home page); reading album pages never downloads files. Only a
   sync requests track pages and files.
2. **Selection**: keys `a:<slug>` (one album, include or exclude) and `p:<platform>` (every album
   of a platform, also ones added later; limited by setting `platform_types`). An album's own rule
   wins; else any ticked platform brings it in. Ticking/unticking a platform clears the album
   rules below it (like ticking a folder in Rom Sync). `Catalog.select()` drops an album rule equal
   to what it would inherit.
3. **Album folders** (`albums.folder`) are assigned once (`assign_folders`): the name, then
   "name (Type, Year)", then "name [slug]" on a clash - never changed afterwards, so new albums never
   move old ones. `engine.album_dir()` = Saved as (`album_places`) or layout folder
   (`folder_layout`: album | platform | type | year) + folder. `Engine.rel_for()` names each file:
   khinsider's name, or `track_name()` ("1-01 - Title.flac") with `track_names: numbered`; images
   go in `Art/` with `art_folder`. Per-album formats: `album_formats` via `catalog.album_wants()` -
   use it wherever a sync decides what to save (engine, cleanup, web). Formats settings
   (`catalog.parse_format_setting`): presets `mp3|flac|best|all` (images per `album_art`), or ordered
   lists `first:`/`all:` with `art` for images; `format_plan()` gives an album's files in order,
   `format_rank()` the download order across albums (the sync sorts its queue by it).
   **Upgrades** (`Engine.upgrade_album`, setting `album_upgrades`): an album gets `first:<better>[,art]`
   as its own formats; `_finish_upgrades` (end of a sync, before the leftover check) deletes its old-format
   files only once every new file is in; cleanup never lists them meanwhile. `replace_formats` does the
   same for every ticked album after a formats change. Renamed albums take their `album_*` settings along.
4. **Moving, not re-downloading**: when an album's place changes (layout, Saved as, a renamed
   album), the next sync moves files the manifest says it saved (`Engine._relocate`,
   `Manifest.by_url/move`) and removes the folders left empty.
5. Downloads go through Rom Sync's Downloader unchanged apart from `resolve`: `.part` + rename,
   Range/If-Range resume, HTML-instead-of-file detection, MAGIC check (mp3/flac/ogg/jpg/png),
   DownloadGate, outage waiting (site errors also from the track page, `ResolveError.status`),
   daily-limit pause. A track page without the format's link fails that file for good (`permanent`).
6. **Clean up** lists only files the download record knows (or the catalog lists): other music in
   the same folder is never listed. "gone" needs the album page read without errors (or the album
   gone after a finished full scan). Nothing is deleted without `confirm: true`; refused while busy.
7. A full scan that had failed pages removes nothing (status "incomplete").
8. **Getting through**: with `impersonate` (default) the client uses `browser.ChromeTransport` and only
   adds `Referer`; otherwise `browser_headers()` (browser-like Accept headers + Referer). Every
   request names khinsider as Referer - its download servers expect it.
9. **Downloaded albums are final** - unless khinsider replaces one with another version: it then
   reappears in the newest-albums list with a later Date Added; every scan reads that list
   (`Catalog.mark_replaced` sets `albums.changed`, page unread) and the next sync re-reads and re-plans
   it (`clear_changed`). Otherwise a sync reads an album page
   only if it was never read, and skips albums whose wanted files are all downloaded
   (`album_totals()["downloaded"]`) - no request, no disk access. "Read album page" (by hand) and
   "Check files" (`Engine.check_album`, `Catalog.forget_missing`) and the "check" job (Check download
   folder: `Engine._check_folder`, disk only) are the only ways back in. Before a
   full scan prunes, downloaded albums the lists no longer show are looked up once
   (`Scanner._follow_moved`) so a moved album keeps its tick and folder.
10. Politeness: page reads 4 at a time with pacing (`Limiter`) and at most `pages_per_minute` for the
   whole job (`pacing.PageRate`, also the Resolver's track pages); downloads start at 2 parallel and
   ramp; `files_per_hour` / `albums_per_day` (`pacing.Window`, catalog `quota`) stop a sync cleanly and
   `queue_resume_at` + `Engine.resume_queue` carry on later; a sync reads at most `albums_per_batch` (default
   `READ_BATCH` = 1) unread album pages, one at a time, the next batches follow in the same job (`_run` loop, `_add_runs`). Respect robots.txt
   (it only disallows /unavailable) - fetched once a day in a client of its own (`Engine._robots`), never in
   front of page requests: a session opening with robots.txt got the Cloudflare check.

## Publishing (GitHub: sireteanu/khinsider-sync)
- Same release process as Rom Sync: **every push that changes the app is its own version**. Bump the
  last number by default (1.0.0 -> 1.0.1); ask the owner before a minor/major bump. Add
  `## [x.y.z] - date` to `CHANGELOG.md` (New / Improved / Fixed / Changed), bump `version` in
  `pyproject.toml` and `src/khinsider_sync/__init__.py`, push to `main`; the `Release` workflow
  creates `v<version>` with that section as its text. Tooling/docs-only pushes need no version.
- Unraid builds from GitHub (`unraid/docker-compose.yml`, Compose Manager) - the user clicks Compose Up.
- **Docs stay current with every change** (same commit): README (complete reference), docs/user-guide.md,
  CHANGELOG.md.

## Conventions
- Dependencies: httpx[http2], selectolax, fastapi, uvicorn, curl_cffi, mutagen, Pillow (+ pytest/pyflakes). Ask before adding more.
  The app must keep working without curl_cffi (plain httpx, a warning in the log).
- Log through `logging.getLogger("khinsider_sync")`. Never `print`.
- Every user-visible change gets a test against `MockSite`. **MockSite copies the live markup**; when
  the site changes, update `tests/fixtures/` from the real page first (browser), then the mock and
  `parse.py`. Use the `investigate-site` skill.
- **Tests and development never touch the real khinsider.com.** Only run against the real site when
  the user asks, and never start a large download unasked (the whole site is ~38 TB).
- Keep saved settings working across versions: convert old keys instead of dropping a user's choices.
- Endpoints are `async def`: the catalog's SQLite connection and the engine live on the event loop.

## Privacy
- Never put the owner's real server paths, share names, time zone, IPs, user names or keys into the
  repository. Use `/path/to/music`, `/path/to/appdata/khinsider-sync`, `D:\path\to\music`,
  `America/New_York`.
