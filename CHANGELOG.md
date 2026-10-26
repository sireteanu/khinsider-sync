# Changelog

## [1.0.9] - 2026-10-25

### New
- **Put the cover into each music file** (Settings): khinsider's cover for the album goes into its
  MP3, FLAC, M4A and OGG/Opus files that have no picture yet. Pictures already inside are kept. Works
  without syncing images, and each file is tagged once, keeping its date. Needs the mutagen package
  (installed with this version; Windows: start.bat installs it).

## [1.0.8] - 2026-10-25

### Fixed
- folder.jpg is khinsider's own cover for the album - the first image on its page, the one the site
  shows in its lists - instead of a guess by file name (which could pick a back cover or a scan).
  Albums that already have a folder.jpg get the right one at the next sync.
- folder.jpg also works when images aren't synced: the cover alone is fetched once.

## [1.0.7] - 2026-10-25

### New
- An A–Z strip beside the album list jumps to the first album of a letter (`#` for digits and
  symbols). Letters without albums in the current view are dimmed; the letter you're at is marked.
  With another sort, a click sorts by name first.

## [1.0.6] - 2026-10-24

### Fixed
- A Cloudflare browser check ("Just a moment...") from khinsider no longer fails every file of a sync:
  a scan stops at once and removes nothing, a sync pauses with everything still queued, and the log
  says what happened in one line. Albums aren't marked as unreadable because of it.
- khinsider's robots.txt is respected again (the site sends it as a web page, so it was skipped).

### Checked on the real site
- A real file downloads complete through the Chrome-like client (MP3 header and size as listed).

## [1.0.5] - 2026-10-24

### New
- **Scan for new albums** (☰ menu) reads only khinsider's newest additions from its home page -
  usually one page instead of about 230. Scheduled syncs do this between full scans, so new albums of
  ticked platforms arrive without waiting for the next full scan.
- **Sync now** in an album's window syncs just that album.
- **Replace old formats** (Settings): when you change the formats, each ticked album's old files are
  deleted once its new ones have all arrived - move a whole collection from MP3 to FLAC.
- Optional playlist (`Album.m3u8`) and `folder.jpg` in each downloaded album's folder.
- **Before a scheduled sync, check the download folder**: never (default - the disks stay asleep),
  daily, weekly or monthly.

### Improved
- Check download folder and Check files also find files that were cut short.
- The type filter button shows how many albums the list holds; the "Sync every album of …" line says
  which album types a ticked platform brings in.

### Fixed
- An album khinsider moves to a new address keeps its Saved as, its own formats and a waiting upgrade.

## [1.0.4] - 2026-10-24

### New
- Pick several album types at once in the type filter (e.g. Gamerip and Arrangement), or "No type".
  The platform counts follow the picked types.
- **Check download folder** (☰ menu) looks for downloaded files you deleted in every album at once;
  the next sync downloads them again. It only reads your disk.
- **Upgrade to FLAC** (or another better format) in an album's window: the next sync downloads the
  better format, then deletes the album's files of the old format. Clean up leaves the old files
  alone meanwhile; **Cancel upgrade** puts the album's formats back.

## [1.0.3] - 2026-10-24

### Improved
- The album list shows every album: scroll through all of them, no more "Show more". Only the rows
  on screen are drawn and fetched, so 100,000 albums scroll as smoothly as 100.
- With a type filter (Gamerip, Soundtrack …) the platform list on the left counts only albums of that
  type, and "All albums" shows how many there are.

## [1.0.2] - 2026-10-24

### Changed
- Downloaded albums are done: khinsider doesn't change albums, so a sync no longer reads their pages
  again or looks through their files. Album pages are read once (the first sync or when you open
  the album). The "Re-read ticked album pages" and "Ask the server about every file" settings are gone.

### New
- The album list marks albums as Downloaded or Partly downloaded, the filter can show Downloaded /
  Partly downloaded / Not downloaded albums, and the top strip counts downloaded albums.
- "Check files" in an album's window finds files you deleted, so the next sync gets them again.
- An album you downloaded that khinsider moved to a new address keeps its tick and folder after a scan.

## [1.0.1] - 2026-10-24

### Changed
- Removed what was left over from Rom Sync: the old folder-tree styles, page-reading helpers for
  lolroms listings, ignore rules for its files; the log is now named after Khinsider Sync.
- The download record calls the album page's date and size by their names; records from 1.0.0
  are taken over at the first start.

## [1.0.0] - 2026-10-23

### New
- First release: keep local copies of the downloads.khinsider.com soundtracks you pick, built on the
  Rom Sync framework.
- Scan site: reads khinsider's album lists (about 230 pages for 106,000+ albums) and its platform
  list, without reading album pages or downloading music.
- Album browser: platforms on the left with album counts, search, type and "ticked / synced /
  problems" filters, sorting by name, year, size and synced.
- Tick single albums, or whole platforms to also get albums added later; optional album types for
  platform ticks; single albums can be left out.
- Album window: tracks per disc with every format and size, images, sync state, Saved as folder;
  the album's page is read when you first open it.
- Formats: MP3, FLAC where offered, best quality, every format, or a list; album images optional.
- Folders: one per album (clashing names get type and year), or grouped by platform, type or year;
  changing them moves the files you have instead of downloading them again.
- Sync: reads the ticked albums' pages, then downloads only new, incomplete or changed files, with
  resume, automatic parallel downloads, waiting out site outages, download-limit pause, free-space
  guard, file dates from the album's Date Added and permissions.
- Albums moved to a new address keep their tick and folder.
- Clean up for files no longer part of the sync (only files it downloaded are ever listed),
  Activity log, schedule, resume after restarts, password, dark and light theme.
- Docker image for Unraid (Compose Manager builds it from GitHub), Windows start.bat.
- From the khi-explorer downloaders (madLinux7, whxjwksjs): requests look like Chrome (curl_cffi) and
  name khinsider as Referer, so Cloudflare lets them through; "best quality" uses their order
  (FLAC, WAV, … M4A, OPUS, OGG, MP3); album art is also found in other galleries, never in comments
  or other albums.
- Track names numbered from the disc and track columns (`1-01 - Title.flac`), as a setting.
- Album images in an Art folder, as a setting.
- Formats for a single album (e.g. FLAC for favourites), in the album's window.
- Formats and download order: tick the formats you want (and the album images), put them in your
  order, and choose the first one an album has or all of them (e.g. only FLAC and MP3). Files
  download in that order across the whole sync.
