# Khinsider Sync

Khinsider Sync keeps a local copy of the [khinsider](https://downloads.khinsider.com/) video game
soundtracks you choose, and keeps it up to date. It reads the site's album lists into a catalog
(without downloading any music), shows every album with the platforms on the left, and each sync
downloads only what is new, incomplete or changed. Tick single albums, or a whole platform to get
every album khinsider has for it - including albums added later.

It runs as a web page: in Docker (made for Unraid) or on Windows. It is built on the same framework
as [Rom Sync](https://github.com/sireteanu/rom-sync) and looks and works the same way.

- **New here?** Read the [user guide](docs/user-guide.md).
- **What changed?** See the [changelog](CHANGELOG.md) and the
  [releases](https://github.com/sireteanu/khinsider-sync/releases). The running version is shown
  next to the title in the web page.
- **Working on the code (people or AI assistants)?** This README is the full reference. Coding rules
  and how the site works are in [CLAUDE.md](CLAUDE.md).

Please be kind to khinsider: it is run by one person on donations. Keep the parallel downloads
modest, don't sync the whole site, and consider [donating](https://downloads.khinsider.com/forums/index.php?account/upgrades).

---

## Contents

1. [Features](#features)
2. [Install](#install)
3. [How it works](#how-it-works)
4. [Settings reference](#settings-reference)
5. [Command-line options and environment variables](#command-line-options-and-environment-variables)
6. [Files on disk](#files-on-disk)
7. [HTTP API](#http-api)
8. [Code map](#code-map)
9. [Development, tests and releases](#development-tests-and-releases)
10. [Facts about khinsider this relies on](#facts-about-khinsider-this-relies-on)

---

## Features

| Area | What it does |
|---|---|
| Catalog | **Scan site** reads khinsider's album lists (about 230 pages for 106,000+ albums: name, platforms, type, year) and its platform list. No album pages and no music are requested while scanning. |
| Browsing | Platforms on the left with album counts; the album list with search, type filter (Gamerip, Soundtrack, Arrangement …), "Ticked / With synced files / With problems" filters, and sorting by name, year, size or how much is synced. |
| Selection | Tick an album, or tick a platform for **every album of it, including ones added later** (optionally only some album types, e.g. gamerips). Untick single albums of a ticked platform as exceptions. Ticks are saved immediately. |
| Album details | Click an album: its tracks per disc with every format and size, its images, what is synced, where it is saved. The album's page is read the first time you open it. |
| Formats | MP3 (default), FLAC where offered, best quality (lossless where offered), every format, or a list such as `.mp3, .ogg`. Album images (covers, scans) are saved too unless you turn that off. |
| Folders | One folder per album, named after the album (clashing names get the type and year added). Optionally grouped by platform, type or year. **Saved as** puts one album anywhere you like. Changing either **moves** what you already have instead of downloading it again. |
| Sync | Reads the ticked albums' pages (new ones always, others when older than "Re-read after N days"), then downloads only what's missing, half-finished (`.part`, resumed) or changed (its size on the album page differs). Unchanged files cost no request. |
| Dates | Each saved file gets the album's "Date Added" (12:00 local time); folders get the date of their newest file. |
| Renamed albums | When khinsider moves an album to a new address, its tick, Saved as and folder follow it. |
| Permissions | Downloaded files and new folders get mode **777** by default (setting). |
| Speed | Parallel downloads ramp up until the server pushes back, settle one below, re-probe every 2 minutes and remember the limit. Optional speed cap and free-space guard. |
| Site outages | When khinsider stops answering, downloads **wait** for it (checking at 30 s, doubling to every 10 min) instead of failing; after "When the site is down, wait up to" (12 h) the sync pauses and the next one continues. |
| Download limits | If the site starts answering with web pages instead of files, downloads pause and continue at the next sync. |
| Progress | Activity panel (counts, speed, parallel downloads; click New, Updated, Resumed, Up to date or Failed to see those files), **This sync** box (size, files left, time remaining), **Downloading** box (one progress bar per file), live log. |
| Activity log | Every file added, updated, resumed, deleted or failed, by day, kept for a year. |
| Clean up | Lists files that aren't part of the sync any more (removed from their album, in albums you unticked, formats or images you no longer sync, unneeded `.part` files). Only files Khinsider Sync downloaded or knows are listed - other music in the folder is never touched. Nothing is deleted without confirming. |
| Schedule | Optional automatic sync every N hours and a site scan every N days (finds new albums for ticked platforms). |
| Reliability | A sync cut off by a restart, crash or reboot continues at the next start. Menu: **Restart** / **Shut down**. Starting a second copy just opens the running one. |
| Interface | Dark (default) or light theme, works on a phone, optional password (HTTP Basic), no external resources. |

---

## Install

### Unraid (Docker Compose Manager) - recommended

The image is built on the server from this GitHub repository; no image registry is involved.

1. Install **Docker Compose Manager** from Apps.
2. Docker tab → Compose → **Add New Stack** `khinsider-sync` → Edit Stack → Compose File → paste
   [`unraid/docker-compose.yml`](unraid/docker-compose.yml) and adjust the two paths:
   ```yaml
   services:
     khinsider-sync:
       build:
         context: https://github.com/sireteanu/khinsider-sync.git#main
       pull_policy: build            # every "Compose Up" rebuilds from the latest code
       image: khinsider-sync:latest
       container_name: khinsider-sync
       ports:
         - "8485:8485"
       volumes:
         - /path/to/appdata/khinsider-sync/config:/config   # catalog, settings, ticks, log
         - /path/to/music/game-soundtracks:/data            # where the albums go
       environment:
         - PUID=99
         - PGID=100
         - UMASK=000
         - TZ=America/New_York
         # - KHINSIDER_PASSWORD=change-me
       labels:
         net.unraid.docker.webui: "http://[IP]:[PORT:8485]/"
       restart: on-failure
   ```
3. **Compose Up**, then turn on **Autostart** for the stack. Open `http://SERVER:8485/`.
4. To update: click **Compose Up** again. Your catalog, ticks and downloads are kept.

Port 8485 lets it run next to Rom Sync (8484). Alternatives: [`docker/unraid-update.sh`](docker/unraid-update.sh)
(plain `docker build` / `docker run`), [`docker-compose.yml`](docker-compose.yml) (local checkout),
[`unraid/khinsider-sync.xml`](unraid/khinsider-sync.xml) (Docker-tab template for a published image;
the `Docker image` workflow can publish one to ghcr.io when started by hand).

### Windows

1. Install Python 3.9+ (tick "Add python.exe to PATH").
2. Double-click **`start.bat`**. The first start creates `.venv` and installs the dependencies; the
   browser then opens `http://localhost:8485/`.
3. Set the download folder in Settings, or start with `start.bat --data D:\path\to\music`.

Catalog and settings live in `config\` next to `start.bat`; the log is `config\khinsider-sync.log`.

---

## How it works

### Scanning (the catalog)

- **Scan site** reads `/console-list` (platform names) and every browse page,
  `/game-soundtracks/browse/<0-9|A…Z>?page=N` (500 albums a page), 4 pages at a time with pacing
  (halving parallelism and doubling the delay on 429/503/timeouts or slow answers). It respects
  `robots.txt`.
- Each row gives the album's address (its slug), name, platforms, type and year. New albums get a
  folder name once (see [Folders](#folders)).
- After a scan that read every page, albums the site no longer lists are removed from the catalog
  (your files stay; Clean up lists them). A scan with failed pages removes nothing.

### Album pages

An album's tracks are only known from its own page (`/game-soundtracks/album/<slug>`): the
details (Date Added, formats), the track list with a size for every format, and its images. Pages
are read:

- the first time you open the album in the list (and with **Read album page** in its window);
- by **Read ticked album pages** in the menu;
- before every sync, for ticked albums never read and those read longer ago than "Re-read ticked
  album pages after" (default 7 days; 0 = every sync).

Sizes in the storage strip only count albums whose pages were read; it says how many aren't yet.

### Selection rules

- `a:<album>` ticks or unticks one album; `p:<platform>` ticks every album of a platform, including
  albums added to it later. An album's own tick wins; otherwise it is synced when any of its
  platforms is ticked. Unticking an album of a ticked platform is an exception; ticking or unticking
  the platform again clears those exceptions.
- "A ticked platform brings in these album types" (setting) limits platform ticks to some types,
  e.g. only gamerips. Albums ticked one by one are always synced.
- A platform's box shows `-` when some of its albums differ from it.

### Which files a sync saves

| Formats setting | Per album |
|---|---|
| `mp3` | MP3 (an album without MP3: its first format, so nothing is left out) |
| `flac` | FLAC where the album has it, else MP3 |
| `best` | the first lossless format the album has (FLAC, WAV …), else MP3 |
| `all` | every format the album offers |
| `.mp3, .ogg` | exactly these, where offered |

Plus the album's images when "Also save each album's images" is on.

### Folders

- Each album has a folder named after it, assigned the first time it is seen and never changed:
  its name, or "name (Type, Year)" when another album already has that folder, or "name [slug]".
  Characters Windows doesn't allow become `_`.
- **Folders** setting: one folder per album (default), or `Platform/Album` (first platform),
  `Type/Album`, `Year/Album`.
- **Saved as** (album window): any folder inside the download folder for that album, e.g.
  `Soundtracks/Persona 5`; empty = back to its default.
- File names are the site's (`1-01. Wake Up, Get Up, Get Out There.mp3`), images keep theirs.
- When a place changes (layout, Saved as, an album renamed on the site), the next sync **moves** the
  files it saved before to the new place and removes the folders left empty; it doesn't download them
  again.

### Syncing

For every file of the ticked albums (formats as above), `Downloader.plan()` compares the album page
with the disk and the download record:

| On disk | Action |
|---|---|
| Missing | **New**: download |
| `.part` file, or smaller than recorded | **Resume** (HTTP Range) |
| Size on the album page differs from when it was saved | **Update**: download again; the old copy is replaced only when the new one is complete |
| Matches | **Skip**, no request |
| Present but not in the record (copied in by hand) | **Adopt** if its size fits, otherwise resume/update |

- Downloading a track takes two requests: its track page (for the download address, which carries a
  per-album token and server name) and the file. The track page is read right before the download
  and kept for the track's other formats. Images are downloaded directly.
- Files are written as `<name>.part` and renamed when complete. A web page received instead of a
  file is rejected, and MP3/FLAC/OGG/JPEG/PNG headers are checked.
- **Parallel downloads** (`DownloadGate`): start at 2 (or the limit remembered for the site), add one
  while every slot is busy, and on push-back (429/503, refused connections) set the ceiling one
  below; every 2 minutes it tries one more. Upper limit 8 by default. "Fixed number" disables this.
- **Site outages**: errors caused by the site (5xx/52x, timeouts, refused connections - on the track
  page or the file) count per file; after 3 the site's home page is asked. If it doesn't answer,
  downloads wait for it (one checks, the others wait), checking at 30 s, then doubling to every 10 min,
  and pause after `outage_wait_hours`. Nothing is marked failed because the site is down.
- **Download limit**: when the site keeps answering with an HTML page instead of files while nothing
  else gets through, downloads pause ("Paused: …") and stay queued for the next sync.
- **Free space**: downloads pause before the disk would drop below "Keep free on disk".
- **Interrupted syncs** continue at the next start (`interrupted_job`), resuming `.part` files.
- **Schedule**: "Sync automatically every N hours"; it becomes a scan+sync when the last scan is
  older than "Scan the site every N days".

### Clean up

`cleanup.py`, after every sync that finishes (not stopped or paused), and whenever the **Clean up**
dialog opens:

| Group | Meaning | Ticked by default |
|---|---|---|
| **No longer on the site** (`gone`) | Saved by Khinsider Sync, but its album page (read without errors) no longer lists it, or the album is gone and a scan has finished since | yes |
| **Unfinished downloads that aren't needed** (`part`) | A `.part` file of something you no longer sync | yes |
| **Not ticked any more** (`unticked`) | Still on the site, but its album isn't ticked, or its format / the images aren't synced | no |

Files Khinsider Sync didn't save and doesn't know (your other music) are never listed, nor are
dot-files, `Thumbs.db`/`desktop.ini`/`.DS_Store` or symlinks. Deleting needs two clicks; the API
refuses without `confirm: true` and while a job runs (409); only paths still leftovers are removed;
folders left empty are removed.

---

## Settings reference

Stored in the catalog (`settings` table); changed in the Settings dialog or `POST /api/settings`.

| Key | Default | Meaning |
|---|---|---|
| `data_dir` | from `--data` | Download folder (locked to `KHINSIDER_DATA` in Docker) |
| `formats` | `mp3` | `mp3`, `flac`, `best`, `all`, or a list like `.mp3,.ogg` |
| `album_art` | `true` | Also save each album's images |
| `platform_types` | `[]` | Album types a ticked platform brings in (`[]` = all) |
| `folder_layout` | `album` | `album`, `platform`, `type` or `year` |
| `album_places` | `{}` | Saved as: `{album slug: folder}`; changed only with `POST /api/album-place` |
| `album_refresh_days` | `7` | Re-read ticked album pages before a sync when older (0 = every sync) |
| `parallel` | `auto` | `auto` or a fixed number of parallel downloads |
| `parallel_max` | `8` | Upper limit for `auto` |
| `max_rate` | `""` | Overall speed limit, e.g. `10M` (empty = unlimited) |
| `min_free` | `2G` | Pause downloads before free disk space drops below this |
| `file_mode` | `777` | Permissions for downloaded files and new folders (octal; empty = system default) |
| `sync_every_hours` | `0` | Automatic sync interval (0 = off) |
| `full_scan_every_days` | `7` | With the schedule on: scan the site when the last scan is older |
| `resume_interrupted` | `true` | Continue an unfinished sync when the app starts |
| `check_remote` | `false` | Ask the server about every existing file (one request per file) |
| `outage_wait_hours` | `12` | How long a sync waits for the site when it's down (0 = don't wait) |
| `interrupted_job` | `""` | Internal: set while a sync runs |

---

## Command-line options and environment variables

`python -m khinsider_sync` (also `start.bat`, `run.py`, or `khinsider-sync` when installed):

| Option | Environment variable | Default | Meaning |
|---|---|---|---|
| `--data DIR` | `KHINSIDER_DATA` | `downloads` (`/data` in Docker) | Download folder |
| `--config DIR` | `KHINSIDER_CONFIG` | `config` (`/config` in Docker) | Catalog, settings, log |
| `--host` | `KHINSIDER_HOST` | `127.0.0.1` (`0.0.0.0` in Docker) | Listen address |
| `--port` | `KHINSIDER_PORT` | `8485` | Port |
| `--password` | `KHINSIDER_PASSWORD` | none | Ask for this password (HTTP Basic, any user name); `/api/health` stays open |
| `--lock-data` | `KHINSIDER_DATA_LOCKED=1` | off (on in Docker) | Download folder can't be changed in the page |
| `--no-browser` | `KHINSIDER_NO_BROWSER=1` | off (on in Docker) | Don't open a browser tab |
| `--site URL` | `KHINSIDER_SITE` | `https://downloads.khinsider.com/` | Hidden; for tests |

Docker only: `PUID`/`PGID` (default 99/100 = Unraid nobody/users), `UMASK` (default `000`), `TZ`
(file dates and schedule).

---

## Files on disk

| Where | What |
|---|---|
| `<config>/catalog.db` | SQLite (WAL): `albums` (name, platforms, type, year, folder, when its page was read, date added, formats), `platforms`, `files` (one row per track and format, and per image: size, place on disk, sync state), `selection`, `settings`, `scans`, `history`, `changes` (kept 365 days) |
| `<config>/khinsider-sync.log` | Log (rotating, 2 MB × 3); each start logs the catalog and the download folder it uses |
| `<data>/<album>/…` | Synced albums (or `<data>/<platform|type|year>/<album>/…`, or their Saved as folder) |
| `<data>/.khinsider-sync.db` | Download record: what the album page said for each saved file, Last-Modified/ETag, per-site parallel limit |
| `<data>/…/*.part` | Unfinished downloads (resumed later) |

---

## HTTP API

All JSON; the page uses nothing else.

| Method & path | Purpose |
|---|---|
| `GET /api/status?log_since=N` | Version, busy, job, catalog counts, selection totals (`albums`, `unread`, `files`, `bytes`, `synced`, `synced_bytes`), scan / album-page progress, sync progress (incl. `bytes_left`, `eta`, `active`), `leftovers`, next run, new log lines |
| `GET /api/platforms` | `{platforms: [{slug, name, albums, state}], types: [[type, n]], no_platform, platform_types}` |
| `GET /api/albums?platform=&q=&type=&show=&sort=&dir=&offset=&limit=` | Albums (`platform` `-` = without platform; `show` `all`/`ticked`/`synced`/`failed`/`unread`; `sort` `name`/`year`/`added`/`new`/`size`/`synced`) -> `{total, albums}` |
| `GET /api/album?slug=` | One album, where it is saved (`dir`, `place`), its files with `wanted` and sync state |
| `POST /api/album/read` `{slug}` | Read that album's page now -> `{read, files, renamed, slug}` |
| `POST /api/select` `{key, include}` | Tick/untick: `a:<slug>` or `p:<platform>` |
| `POST /api/selection/clear` | Untick everything |
| `POST /api/scan` · `POST /api/read` `{albums?}` · `POST /api/sync` · `POST /api/stop` | Jobs (409 when busy); `read` without albums reads every ticked album's page |
| `GET/POST /api/settings` | Read / change settings (400 with a message on invalid values) |
| `POST /api/album-place` `{slug, place}` | Saved as (`""` = default) -> `{slug, place, dir, moved}`; 400 on a bad place, 409 while busy |
| `GET /api/history` | Last jobs |
| `GET /api/sync/files?kind=` | Files behind an Activity counter (`new`, `updated`, `resumed`, `uptodate`, `failed`) |
| `GET /api/changes/days` · `GET /api/changes?day=&kind=` | Activity log |
| `GET /api/cleanup` · `POST /api/cleanup/delete` `{paths, confirm: true}` | Clean up |
| `POST /api/restart` · `POST /api/shutdown` | Restart / end the program |
| `GET /api/health` | `{"ok": true}` (no password) |

---

## Code map

```
src/khinsider_sync/
  __main__.py   options / env vars, port check, log file, uvicorn, restart / shutdown
  web.py        FastAPI routes and Basic auth
  engine.py     jobs (scan | read | sync | scan+sync), Resolver (track page -> download address),
                album_dir() (where albums go), moving saved files, Clean up, schedule, status
  catalog.py    SQLite catalog; selection (select / selected_albums / platform_states); wanted_exts
  scanner.py    scan_site() (platform list + browse pages), read_albums() (album pages)
  parse.py      page readers: parse_browse, parse_album, parse_track, parse_platforms
  cleanup.py    Clean up: walk the download folder, judge files, delete confirmed ones
  static/       index.html, app.css, app.js - no build step, no CDN
  core/         the sync engine from Rom Sync (download.py with the `resolve` hook, sync.py, limits.py ...)
tests/          pytest against tests/mock_site.py (a copy of khinsider's markup; samples of the real
                pages in tests/fixtures); never the real site
docker/, unraid/, .github/workflows/ (tests, release, docker image), start.bat, run.py, publish.bat
```

---

## Development, tests and releases

```
pip install -e ".[dev]"
python -m pytest            # ~75 tests, ~1.5 min, all against the in-process mock site
python -m pyflakes src tests
python run.py --data downloads --config config
```

- Tests and development never contact the real khinsider. Real-site markup is kept in
  `tests/fixtures/` and copied by `tests/mock_site.py`.
- Every user-visible change gets a test, a line in `CHANGELOG.md`, and updates to this README and the
  [user guide](docs/user-guide.md).
- **Versions**: every change to the app is its own version (1.0.0 → 1.0.1; the middle number for
  significant changes). Bump `pyproject.toml` and `src/khinsider_sync/__init__.py`, add
  `## [x.y.z] - date` to the changelog, push to `main`: the `Release` workflow publishes GitHub
  release `vx.y.z` with that section as its text.

---

## Facts about khinsider this relies on

Verified in a browser (October 2026). If the site changes, these are the first things to check
(the `investigate-site` skill walks through it).

- About 106,000 albums, 3.3 million songs, 37 TB (home page, October 2026).
- Browse pages: `/game-soundtracks/browse/<0-9|A…Z>?page=N&orderby=name&orderdirection=ASC`, 500 albums
  a page in `table.albumList` (cover cell `td.albumIcon`, name, platform links
  `/game-soundtracks/<platform>`, type, year), `.pagination .counter` "Page 2 of 12", "Found 5905
  albums!". A "Top 12" grid above the list is not part of it. `0-9` also holds names starting with
  quotes and other symbols.
- Platform list: `/console-list`, `<a href="/game-soundtracks/<slug>">Name</a> (count)`.
- Album pages: details `<p align="left">` (Platforms, Year, Date Added `Dec 17th, 2025`, Album type);
  images `.albumImage a`; `table#songlist` with `#songlist_header` (optional `CD` and `#` columns,
  `Song Name` over two columns, one column per format) and `#songlist_footer`. Missing albums answer
  200 with `<h2>Ooops!</h2>`; moved albums redirect.
- Track pages: `/game-soundtracks/album/<slug>/<file name encoded twice>.mp3`, links
  `.songDownloadLink` per format to `https://<server>.vgmtreasurechest.com/soundtracks/<slug>/<token>/<file>`.
- Track sizes in MB with two decimals; totals with thousands commas.
- `robots.txt` only disallows `/unavailable`.
