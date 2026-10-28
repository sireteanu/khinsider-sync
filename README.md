# Khinsider Sync

Khinsider Sync keeps a local copy of the [khinsider](https://downloads.khinsider.com/) video game
soundtracks you choose, and keeps it up to date. It reads the site's album lists into a catalog
(without downloading any music), shows every album with the platforms on the left, and each sync
downloads only what is new, incomplete or changed. Tick single albums, or a whole platform to get
every album khinsider has for it - including albums added later.

It runs as a web page: in Docker (made for Unraid) or on Windows. It is built on the same framework
as [Rom Sync](https://github.com/sireteanu/rom-sync) and looks and works the same way. How it talks
to khinsider (looking like Chrome, the best-format order, finding album art, numbered track names)
follows [khi-explorer](https://github.com/madLinux7/khi-explorer) by madLinux7 and
[its fork](https://github.com/whxjwksjs/khi-explorer-a-khinsider-downloader-tool) by whxjwksjs -
thanks to both.

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
| Catalog | **Scan site** reads khinsider's album lists (about 230 pages for 106,000+ albums: name, platforms, type, year) and its platform list. No album pages and no music are requested while scanning. **Scan for new albums** (☰ menu, and every scheduled sync between full scans) reads only the home page's newest additions - usually one page. |
| Browsing | Platforms on the left with album counts (of the chosen album type when the type filter is set); the album list - every album of the view in one scrolling list, fetched a slice at a time as you scroll, with an A–Z strip beside it that jumps to a letter (sorting by name first if needed) - with search, type filter (tick one or several: Gamerip, Soundtrack, Arrangement …; "No type" for albums without one), "Ticked / With synced files / With problems" filters, and sorting by name, year, size or how much is synced (column heads, or the **Sort** menu: name, newest / oldest release - by year, then most recently added to khinsider -, largest, most synced). |
| Selection | Tick an album, or tick a platform for **every album of it, including ones added later** (optionally only some album types, e.g. gamerips). Untick single albums of a ticked platform as exceptions. Ticks are saved immediately. |
| Album details | Click an album: its tracks per disc with every format and size, its images, what is synced, where it is saved. The album's page is read the first time you open it. |
| Formats | **Your own list, in your order**: tick the formats you want (and the album images) and move them up or down; an album gets either only the first ticked format it has, or every ticked one (e.g. FLAC *and* MP3). Files download in that order - every FLAC file of the sync before the first MP3. Quick picks: MP3 (default), FLAC else MP3, FLAC and MP3, best quality (FLAC → WAV → … → M4A → OPUS → OGG → MP3), everything. Any album can have **its own formats** (e.g. FLAC for favourites). Album images (covers, scans) are saved too unless you turn that off, optionally in an `Art` folder. |
| Track names | As khinsider names them (`1-01. Title.mp3`), or numbered from the album page's disc and track columns (`01 - Title.mp3`, `1-01 - Title.flac`). |
| Getting through | Requests look like Chrome (its TLS fingerprint and headers, via curl_cffi) and name khinsider as the Referer, as khi-explorer does, so Cloudflare's bot protection lets them through. Can be turned off. |
| Folders | One folder per album, named after the album (clashing names get the type and year added). Optionally grouped by platform, type or year. **Saved as** puts one album anywhere you like. Changing either **moves** what you already have instead of downloading it again. |
| Sync | Reads each ticked album's page once (the first time it's synced or opened), then downloads what's missing or half-finished (`.part`, resumed). khinsider doesn't change albums once they're up, so **an album that is downloaded is done**: later syncs skip it without a request or a look at the disk. |
| Downloaded albums | The album list marks every album as **Downloaded** or **Partly downloaded** (with the date in its tooltip and window), the filter shows Downloaded / Partly downloaded / Not downloaded, and the top strip counts "12 of 13 albums downloaded". **Check files** in an album's window looks for files you deleted; **Check download folder** (☰ menu) does that for every downloaded album at once - disk only, no requests. The next sync downloads the missing files again. |
| Replaced albums | khinsider sometimes replaces an album with another version (its "Change Log" says "replaced by a different version"); the album then shows up again among the newest albums with a new Date Added. Every scan (quick or full) reads that list, marks such albums **New version**, and the next sync that includes them reads their page again and downloads the album again (a new version: every file). |
| Tags | Optional: fill in the tags khinsider's files usually lack - title, album, track and disc number with totals, year - from the album page. Only missing tags are added; anything already there is kept. Done in the same pass as the cover, once per file. |
| Notifications | Optional: a message to ntfy, Gotify, Pushbullet (`pbul://` + your access token), a Discord webhook or a Home Assistant webhook when a sync downloaded something (which albums), when new albums turn up for your ticked platforms, and when something needs a look (sync paused, job failed). **Send a test** in Settings. |
| Upgrading | An album's window offers **Upgrade to FLAC** (or another format the album has that is better than what you downloaded: FLAC, WAV, APE, WV, M4A, OPUS, OGG, AAC, MP3). The album gets that format as its own formats and is ticked; once a sync has every new file, it deletes the album's files of the old format (images stay) and logs them in the Activity log as "Replaced by FLAC". Until then the old files stay and Clean up leaves them alone. **Cancel upgrade** puts the album's formats back. **Replace old formats** (Settings) does the same for every ticked album whenever the formats setting changes - switch to FLAC and the MP3s go once the FLACs are in. |
| Going easy on khinsider | Settings → **Go easy on khinsider**: page reads a minute (album and track pages, default 60), files an hour, albums a day (both off by default). **Albums at a time** (default 1): a sync reads that many unread album pages, one at a time, downloads those albums, then goes straight on with the next in the same sync. Page reads a minute caps the album pages a minute, and the files a minute for formats other than MP3 (one track page per file; MP3 addresses come from the album page's player); changing it applies to a sync under way at once. **Pause between albums** (default none; or a fixed time, or automatic): after an album's files, before the next album page - on the owner's connection it didn't keep the browser check away (it came even after 5 minutes), the 1-minute retry did the job. After a Cloudflare browser check the sync waits and **tries again by itself** - after 1 minute, then 10, 20, 40, 80 and up to 2 hours between tries while it keeps happening. When a limit is reached, the sync stops cleanly and **carries on by itself** as soon as there's room again - the Activity panel says when. |
| Tick from a list | ☰ → **Tick albums from a list**: paste or open a list of khinsider album addresses (or slugs; CSV/TSV first column) and tick them all at once - e.g. albums matched from a folder of older downloads. |
| One album now | **Sync now** in an album's window syncs just that album (and ticks it). |
| Playlists and covers | Optional: `<album>.m3u8` (track order, lengths, titles) and `folder.jpg` (khinsider's cover for the album: the first image on its page - the one its own lists show - copied from the downloaded images, or fetched on its own when images aren't synced; a PNG cover is converted to JPEG - see-through parts on black - or saved as `folder.png` without Pillow) in each downloaded album's folder, for Plex, Jellyfin, Kodi and players. They are written for every ticked album with downloaded music (also when one of its files failed, and after a paused sync), move with the album, go when turned off, and Clean up lists them with an unticked album's music. One that was deleted comes back at the next sync that downloads files of its album, or after Check files / Check download folder. A `folder.jpg` of your own is never replaced. **Put the cover into each music file** (optional) adds the same cover to MP3, FLAC, M4A and OGG/Opus files that carry no picture yet - pictures already inside are kept. Each file is tagged once, on a copy that replaces it, keeping its date and permissions; the download record learns the new size, so checks don't take it for a damaged file. |
| Dates | Each saved file gets the album's "Date Added" (12:00 local time); folders get the date of their newest file. |
| Renamed albums | When khinsider moves an album to a new address, its tick, Saved as and folder follow it. |
| Permissions | Downloaded files and new folders get mode **777** by default (setting). |
| Speed | Parallel downloads ramp up until the server pushes back, settle one below, re-probe every 2 minutes and remember the limit. Optional speed cap and free-space guard. |
| Site outages | When khinsider stops answering, downloads **wait** for it (checking at 30 s, doubling to every 10 min) instead of failing; after "When the site is down, wait up to" (12 h) the sync pauses and the next one continues. |
| Download limits | If the site starts answering with web pages instead of files, downloads pause and continue at the next sync. |
| Progress | Activity panel (counts, speed, parallel downloads; click New, Updated, Resumed, Up to date or Failed to see those files), **This sync** box (size, files left, time remaining; called **This run** when more albums follow, with how many follow and how many wait for the day's room), **Downloading** box (one progress bar per file), live log. |
| Activity log | Every file added, updated, resumed, deleted or failed, by day, kept for a year. |
| Clean up | Lists files that aren't part of the sync any more (removed from their album, in albums you unticked, formats or images you no longer sync, unneeded `.part` files). Only files Khinsider Sync downloaded or knows are listed - other music in the folder is never touched. Nothing is deleted without confirming. |
| Schedule | Optional automatic sync every N hours: a full site scan every N days, otherwise a scan for new albums (one page), then the sync. Optionally a check of the download folder first (never, daily, weekly, monthly - off by default, as it wakes the disks). |
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
  `robots.txt`. Without "Look like Chrome" requests go out as HTTP/1.1, like a browser's plain request.
- Each row gives the album's address (its slug), name, platforms, type and year. New albums get a
  folder name once (see [Folders](#folders)).
- After a scan that read every page, albums you downloaded that the lists no longer show are looked
  up once: if khinsider moved one to a new address, its tick and folder follow it - and so do its
  Saved as, its own formats and a waiting upgrade.
- **Scan for new albums** reads the home page's "Latest Soundtracks" (`/?page=N`, newest first, one
  table per day) back to two days before the last finished scan - usually one page. It adds and updates
  albums but never removes any. Without a finished scan, or when 30 pages don't reach back far enough,
  it does a full scan instead.
- After a scan that read every page, albums the site no longer lists are removed from the catalog
  (your files stay; Clean up lists them). A scan with failed pages removes nothing.

### Album pages

An album's tracks are only known from its own page (`/game-soundtracks/album/<slug>`): the
details (Date Added, formats), the track list with a size for every format, and its images. Pages
are read:

- the first time you open the album in the list (and again whenever you click **Read album page**
  in its window - the way to pick up tracks khinsider added to an album later);
- by **Read ticked album pages** in the menu;
- before a sync, for ticked albums never read. Pages already read are never read again by a sync.

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
| `first:.flac,.mp3,art` | the first of the listed formats the album has (none of them: no music from that album); `art` = its images, where it stands in the list |
| `all:.flac,.mp3,art` | every listed format the album has |
| `mp3` | MP3 (an album without MP3: its first format, so nothing is left out) |
| `flac` | FLAC where the album has it, else MP3 |
| `best` | the first of FLAC, WAV, APE, WV, M4A, OPUS, OGG, AAC, MP3 the album has (khi-explorer's order) |
| `all` | every format the album offers |
| `.mp3, .ogg` | exactly these, where offered |

With `first:` and the presets `mp3`, `flac`, `best`, a **track** the album has without its chosen
format (an album in FLAC with a few tracks only as MP3) comes in the next format that applies: the
next one of a `first:` list (none after it: left out), for `flac` MP3 and then the `best` order, for
`best` the `best` order, for `mp3` the smallest other format. Such a file counts as part of the album
(complete, not a Clean up leftover, in the playlist) and is kept by upgrades and Replace old formats.

The two list forms are what the Settings editor saves (**Formats and download order**: tick, move
up/down, "Only the first ticked format an album has" or "Every ticked format"); the presets below
them are the older settings, where images follow `album_art`.

**Download order**: a sync queues files by their format's place in the list (presets: FLAC, WAV …
MP3, images last), then album by album and track by track - so with `FLAC, MP3` you get every FLAC
file of the sync before the first MP3. **Formats for this album** in an
album's window overrides the setting for that album (stored in `album_formats`); picking one by hand
cancels a waiting upgrade.

### Folders

- Each album has a folder named after it, assigned the first time it is seen and never changed:
  its name, or "name (Type, Year)" when another album already has that folder, or "name [slug]".
  Characters Windows doesn't allow become `_`.
- **Folders** setting: one folder per album (default), or `Platform/Album` (first platform),
  `Type/Album`, `Year/Album`.
- **Saved as** (album window): any folder inside the download folder for that album, e.g.
  `Soundtracks/Persona 5`; empty = back to its default.
- Track names: khinsider's (`1-01. Wake Up, Get Up, Get Out There.mp3`, default), or with
  "Track names: Disc and track number" `01 - Title.mp3` / `1-01 - Title.flac` built from the album
  page's CD and # columns (without a # column, the track's place in the list). Images keep their names,
  next to the music or in `<album>/Art/`.
- When a place changes (layout, track names, Art folder, Saved as, an album renamed on the site), the next sync **moves** the
  files it saved before to the new place and removes the folders left empty; it doesn't download them
  again.

### Syncing

Albums whose files are all downloaded are skipped first (no request, no disk access - khinsider
doesn't change albums). For every file of the other ticked albums (formats as above),
`Downloader.plan()` compares the album page with the disk and the download record:

| On disk | Action |
|---|---|
| Missing | **New**: download |
| `.part` file, or smaller than recorded | **Resume** (HTTP Range) |
| Size on the album page differs from when it was saved | **Update**: download again; the old copy is replaced only when the new one is complete |
| Matches | **Skip**, no request |
| Present but not in the record (copied in by hand) | **Adopt** if its size fits, otherwise resume/update |

- Every request looks like Chrome's (curl_cffi impersonating Chrome as an httpx transport,
  `core/browser.py`) and carries `Referer: https://downloads.khinsider.com/`. Without curl_cffi, or
  with the setting off, a plain client with browser-like headers is used.
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
- **Cloudflare browser check**: when khinsider answers with Cloudflare's "Just a moment..." check (403
  with `cf-mitigated: challenge`) - as it does for data-center addresses, while a home connection gets
  through - a scan stops at once (it is "incomplete", so it removes nothing) and a sync pauses; nothing is
  marked failed and the log says so in one line. Downloads themselves come from the vgmtreasurechest.com
  file servers, which didn't ask for the check in October 2026.
- **Download limit**: when the site keeps answering with an HTML page instead of files while nothing
  else gets through, downloads pause ("Paused: …") and stay queued for the next sync.
- **Free space**: downloads pause before the disk would drop below "Keep free on disk".
- **Interrupted syncs** continue at the next start (`interrupted_job`), resuming `.part` files.
- **Schedule**: "Sync automatically every N hours"; it becomes a scan+sync when the last full scan is
  older than "Scan the whole site every N days", otherwise a scan for new albums + sync. With "Before a
  scheduled sync, check the download folder" (`check_folder_days`) set, a check of the download folder
  runs first when the last one is that old.
- **Check download folder** compares every downloaded file with the size it was saved with: missing or
  cut-short files are marked not downloaded (a cut-short one resumes at the next sync). The Activity
  panel shows a progress bar while it runs: files looked at, time remaining, missing so far.

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
| `formats` | `mp3` | `first:<list>` or `all:<list>` in priority order (formats like `.flac`, and `art` for images), or a preset `mp3`, `flac`, `best`, `all`; older `.mp3,.ogg` = `all:` with images per `album_art` |
| `album_art` | `true` | Save each album's images with a preset; a `first:`/`all:` list sets it from whether `art` is in it |
| `platform_types` | `[]` | Album types a ticked platform brings in (`[]` = all) |
| `folder_layout` | `album` | `album`, `platform`, `type` or `year` |
| `album_places` | `{}` | Saved as: `{album slug: folder}`; changed only with `POST /api/album-place` |
| `album_formats` | `{}` | Formats for single albums: `{album slug: formats}`; changed only with `POST /api/album-formats` |
| `album_upgrades` | `{}` | Waiting upgrades: `{album slug: {to, from, formats, since}}` (`formats`: the album's formats before, put back by Cancel); changed only with `POST /api/album/upgrade` |
| `track_names` | `site` | `site` (khinsider's file names) or `numbered` (`1-01 - Title.flac`) |
| `art_folder` | `false` | Images in `<album>/Art/` |
| `replace_formats` | `false` | Once a ticked album is downloaded in the formats now wanted, delete its music files of formats no longer wanted |
| `playlists` | `false` | Write `<album folder>.m3u8` in each downloaded album's folder |
| `write_tags` | `false` | Fill in missing title / album / track / disc / year tags from the album page (needs mutagen) |
| `notify_url` | `""` | Where notifications go: ntfy topic, Gotify `/message?token=…`, `pbul://<Pushbullet access token>`, Discord webhook, Home Assistant webhook, any URL taking JSON `{title, message, body}`; empty = off |
| `notify_on` | all | Which: `downloads`, `new_albums`, `problems` |
| `embed_cover` | `false` | Put the album's cover into each downloaded music file that has no picture (MP3, FLAC, M4A, OGG/Opus; needs mutagen) |
| `folder_art` | `false` | Save each downloaded album's cover (its first image) as `folder.jpg` / `folder.png`, also when images aren't synced |
| `impersonate` | `true` | Look like Chrome (needs curl_cffi) |
| `parallel` | `auto` | `auto` or a fixed number of parallel downloads |
| `parallel_max` | `8` | Upper limit for `auto` |
| `max_rate` | `""` | Overall speed limit, e.g. `10M` (empty = unlimited) |
| `min_free` | `2G` | Pause downloads before free disk space drops below this |
| `file_mode` | `777` | Permissions for downloaded files and new folders (octal; empty = system default) |
| `sync_every_hours` | `0` | Automatic sync interval (0 = off) |
| `full_scan_every_days` | `7` | With the schedule on: scan the whole site when the last full scan is older (else only the newest albums) |
| `pages_per_minute` | `60` | khinsider page requests a minute (browse lists, album pages, track pages - one per track downloaded in a format other than MP3, or when the player has no address); 0 = no limit |
| `player_links` | `true` | Take MP3 addresses from the album page's music player (no track page for them); off: every file reads its track page |
| `album_pause` | `0` | Seconds to wait after an album's downloads before reading the next album's page (`0` = none), or `auto`: starts at 60 s, +50% (at least +15 s, at most 5 min) when a browser check comes anyway - none at all once a check comes even after 5 min -, -10 s (at least 20 s) after 20 albums without one |
| `album_pause_auto`, `album_pause_streak` | `60`, `0` | Set by the app: the learned pause and albums since the last check |
| `albums_per_batch` | `1` | Unread album pages a sync reads (one at a time) before downloading those albums; the rest follow by themselves |
| `files_per_hour` | `0` | Files downloaded in any 60 minutes; the rest waits; 0 = no limit |
| `albums_per_day` | `0` | Albums started in any 24 hours (albums under way always finish); 0 = no limit |
| `challenge_retries` | `0` | Set by the app: browser checks in a row; the wait before trying again doubles each time (1 min, then 10 min up to 2 h) |
| `queue_resume_at` | `0` | Set by the app when a limit or batch stopped a sync: when it carries on |
| `check_folder_days` | `0` | With the schedule on: check the download folder before a scheduled sync when the last check is this many days old (0 = never) |
| `last_folder_check` | `0` | When the download folder was last checked (set by the app) |
| `resume_interrupted` | `true` | Continue an unfinished sync when the app starts |
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
| `GET /api/status?log_since=N` | Version, busy, job, catalog counts, selection totals (`albums`, `unread`, `files`, `bytes`, `synced`, `synced_bytes`), scan / album-page progress, sync progress (incl. `bytes_left`, `eta`, `active`), while the download folder is checked `check: {total, done, missing, eta}`, `leftovers`, next run, new log lines |
| `GET /api/platforms?type=` | `{platforms: [{slug, name, albums, state}], types: [[type, n]], no_platform, total, platform_types}`; with `type` (one or more, comma separated; `Other` = no type), `albums`, `no_platform` and `total` count only albums of those types (`types` always counts every type) |
| `GET /api/albums?platform=&q=&type=&show=&sort=&dir=&offset=&limit=` | Albums (`platform` `-` = without platform; `type` as for platforms; `show` `all`/`ticked`/`downloaded`/`partly`/`notdownloaded`/`failed`/`unread`; `sort` `name`/`year`/`added`/`new`/`size`/`synced`) -> `{total, offset, albums}`; the filtered, sorted view is kept between requests (until the catalog, ticks or format settings change), so the page asks for one slice at a time while scrolling; sorted by name, also `letters: {"#": 0, "A": 412, ...}` - where each first letter starts in the whole view (`#` = digits and symbols) |
| `GET /api/album?slug=` | One album, where it is saved (`dir`, `place`), its files with `wanted` and sync state, `upgrade: {have, better, pending}` (downloaded audio formats, better ones the album has, a waiting upgrade) |
| `POST /api/album/read` `{slug}` | Read that album's page now -> `{read, files, renamed, slug}` |
| `POST /api/album/check` `{slug}` | Look on disk for that album's downloaded files; missing ones are downloaded by the next sync -> `{slug, missing}` |
| `POST /api/album/upgrade` `{slug, format}` | Upgrade a downloaded album to a better format it has (`flac`, ...); `format` `""` cancels a waiting upgrade -> `{slug, pending}` (400 for a format that isn't better, 404 for an unknown album) |
| `POST /api/check` | Job: look on disk for every downloaded file; missing ones are marked not downloaded (409 when busy) |
| `POST /api/select` `{key, include}` | Tick/untick: `a:<slug>` or `p:<platform>` |
| `POST /api/select/list` `{text, include?}` | Tick (or untick) many albums: one per line, a khinsider album address or its slug; first column of CSV/TSV; `#` lines skipped -> `{ticked, unknown, unknown_count}` |
| `POST /api/selection/clear` | Untick everything |
| `POST /api/scan` `{quick?}` · `POST /api/read` `{albums?}` · `POST /api/sync` `{albums?}` · `POST /api/stop` | Jobs (409 when busy); `scan` with `quick: true` reads only the newest albums; `read` without albums reads every ticked album's page; `sync` with albums syncs only those (ticking them) |
| `GET/POST /api/settings` | Read / change settings (400 with a message on invalid values) |
| `POST /api/album-formats` `{slug, formats}` | This album's own formats (`""` = the general setting); 400 on an unknown format |
| `POST /api/album-place` `{slug, place}` | Saved as (`""` = default) -> `{slug, place, dir, moved}`; 400 on a bad place, 409 while busy |
| `GET /api/history` | Last jobs |
| `GET /api/sync/files?kind=` | Files behind an Activity counter (`new`, `updated`, `resumed`, `uptodate`, `failed`) |
| `GET /api/changes/days` · `GET /api/changes?day=&kind=` | Activity log |
| `GET /api/cleanup` · `POST /api/cleanup/delete` `{paths, confirm: true}` | Clean up (while it looks through the folder, `/api/status` has `cleanup: {files, expected, step, eta}`) |
| `POST /api/notify/test` `{url}` | Send a test notification to that address (not saved); 400 with the reason when it fails |
| `POST /api/restart` · `POST /api/shutdown` | Restart / end the program |
| `GET /api/health` | `{"ok": true}` (no password) |

---

## Code map

```
src/khinsider_sync/
  __main__.py   options / env vars, port check, log file, uvicorn, restart / shutdown
  web.py        FastAPI routes and Basic auth
  engine.py     jobs (scan | quick | read | sync | scan+sync | quick+sync | check), Resolver (track page -> download address),
                album_dir() (where albums go), moving saved files, Clean up, schedule, status
  catalog.py    SQLite catalog; selection (select / selected_albums / platform_states); wanted_exts
  scanner.py    scan_site() (platform list + browse pages), scan_latest() (home page), read_albums()
  parse.py      page readers: parse_browse, parse_latest, parse_album, parse_track, parse_platforms
  extras.py     playlists and folder images next to downloaded albums
  covers.py     the cover and missing tags put into music files (mutagen)
  pacing.py     page reads a minute (PageRate), files an hour / albums a day (Window)
  notify.py     notifications (ntfy / Gotify / Discord / webhooks)
  cleanup.py    Clean up: walk the download folder, judge files, delete confirmed ones
  static/       index.html, app.css, app.js - no build step, no CDN
  core/         the sync engine from Rom Sync (download.py with the `resolve` hook, sync.py, limits.py ...)
    browser.py  ChromeTransport: httpx transport that sends requests through curl_cffi as Chrome
tests/          pytest against tests/mock_site.py (a copy of khinsider's markup; samples of the real
                pages in tests/fixtures); never the real site
docker/, unraid/, .github/workflows/ (tests, release, docker image), start.bat, run.py, publish.bat
```

---

## Development, tests and releases

```
pip install -e ".[dev]"
python -m pytest            # ~80 tests, ~1.5 min, all against the in-process mock site
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
- Home page (`/?page=N`, 617 pages): "Latest Soundtracks", newest first, 8 days a page (~235 albums),
  each day an `h3.latestSoundtrackHeading` ("October 24th, 2026") followed by a `table.albumList` with
  the browse pages' columns; a day can carry on at the top of the next page.
- Album pages: details `<p align="left">` (Platforms, Year, Date Added `Dec 17th, 2025`, Album type);
  images `.albumImage a`; `table#songlist` with `#songlist_header` (optional `CD` and `#` columns,
  `Song Name` over two columns, one column per format) and `#songlist_footer`. Missing albums answer
  200 with `<h2>Ooops!</h2>`; moved albums redirect.
- Track pages: `/game-soundtracks/album/<slug>/<file name encoded twice>.mp3`, links
  `.songDownloadLink` per format to `https://<server>.vgmtreasurechest.com/soundtracks/<slug>/<token>/<file>`.
- Track sizes in MB with two decimals; totals with thousands commas.
- Cloudflare sets a `__cf_bm` cookie on the first page; it has to go back once per request, as a browser sends it (two Cookie headers got every later page the browser check - fixed in 1.0.27).
- `robots.txt` only disallows `/unavailable`; it is sent as `text/html`, so it is read by its content.
- Cloudflare protects the pages: from a data-center address (a cloud server) every page answers with the
  "Just a moment..." browser check (403, `cf-mitigated: challenge`) whatever the client looks like,
  while the file servers still send files (checked October 24th, 2026). From a home connection the
  pages load.
