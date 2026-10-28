# Changelog

## [1.0.22] - 2026-10-28

### New
- **MP3 files no longer need a track page each.** An album page's music player already lists every
  track's MP3 address; the app now takes them from there (read from the page, nothing run), so an
  MP3 album costs one page read however many tracks it has - up to dozens of times fewer requests to
  the part of khinsider Cloudflare guards, and MP3 downloads go as fast as your connection. FLAC and
  other formats still read their track pages. If an address from the player doesn't work, that file
  falls back to its track page. Settings → Go easy on khinsider → **Take MP3 addresses from the album
  page's music player** (on by default). Albums whose page was read before this version still use
  track pages until their page is read again.

## [1.0.21] - 2026-10-28

### New
- **Sort** menu next to the album filters: **Newest release** first (by year; within a year the
  most recently added to khinsider first), oldest release, name, largest, most synced.

### Fixed
- A track an album doesn't have in its chosen format - most tracks in FLAC, a few only as MP3 - was
  left out. It now comes in the next format the setting allows (with FLAC: MP3), counts as part of the
  album, appears in its playlist, and is kept by upgrades and Replace old formats. An upgrade to FLAC
  keeps the MP3 of such a track.

### Changed
- With "Look like Chrome" off, requests go out as HTTP/1.1 (Cloudflare singles out HTTP/2 from
  clients that aren't browsers).

## [1.0.20] - 2026-10-27

### New
- After a Cloudflare browser check, the sync pauses and **tries again by itself**: after 30 minutes,
  then 1, 2, 4 and up to 8 hours while the check keeps coming, and back to 30 minutes once pages come
  through again. The Activity panel says when. Before, it stopped and waited for you to click Sync.

### Fixed
- A browser check on a track page let a long list go straight on to the next album, which got the
  same check.

## [1.0.19] - 2026-10-27

### Fixed
- Changing **Page reads a minute** only took effect at the next sync, so a running sync kept its
  old pace (with 10, one file every 6 seconds) - now that the whole list goes in one sync, that
  could be days. The new pace now applies at once.

## [1.0.18] - 2026-10-27

### Improved
- A long list no longer stops between albums: the next album's page is read and downloaded right
  away in the same sync (it used to end the sync and wait up to 30 s for the next one), and you get
  one notification at the end, not one per album.

### Fixed
- The speed shown after each file in the log counted the wait for its track page, so files looked
  much slower than they were. It is now the file's own download speed.

### Changed
- Settings explains that **Page reads a minute** is also the most files a minute (one track page per
  file).

## [1.0.17] - 2026-10-27

### Improved
- When a long list is done one run at a time, the Activity panel says so: the box is called
  **This run**, and a line under it says how many albums still follow (and how many wait for room
  under Albums a day). The time remaining no longer looks like the time for the whole list.

## [1.0.16] - 2026-10-27

### Fixed
- Syncing an album whose page hadn't been read yet got khinsider's Cloudflare browser check, while
  opening the same album worked. The sync asked for robots.txt first in the same connection, which
  looks like a bot; robots.txt is now fetched on its own, once a day.

### Changed
- **Albums at a time** (Settings → Go easy on khinsider, 1 by default): a sync reads one new album
  page, downloads that album, then reads the next - one page at a time, never a burst (was 25 pages,
  4 at a time).

## [1.0.15] - 2026-10-27

### New
- **Go easy on khinsider** (Settings): page reads a minute (60 by default), files an hour and albums a
  day. When a limit is reached the sync stops cleanly - files under way finish - and carries on by
  itself as soon as there's room again; the Activity panel says when.

### Changed
- A sync with many unread albums no longer reads all their pages first: it reads 25, downloads those
  albums, and carries on with the next 25 by itself. (Reading thousands of pages in one go is what gets a
  connection a Cloudflare browser check.)

## [1.0.14] - 2026-10-27

### New
- **Tick albums from a list** (☰ menu): paste or open a list of khinsider album addresses (or their
  last part; the first column of a CSV) and tick them all at once. Lines it can't find are listed.

## [1.0.13] - 2026-10-25

### Fixed
- A folder.jpg (or playlist) that was deleted - e.g. with the album's folder - never came back: the
  app trusted its record of having written it. Now an album that gets files again gets them back, and
  Check download folder / Check files find missing ones for the next sync.
- folder.jpg, the embedded cover and tags were skipped for an album when any of its files failed
  (e.g. a broken scan image on khinsider) and after a paused sync. Now every downloaded file gets them.

## [1.0.12] - 2026-10-25

### New
- Pushbullet notifications: use `pbul://` followed by your access token as the notification address.

## [1.0.11] - 2026-10-25

### New
- **Albums khinsider replaces with another version** are noticed: they reappear among the newest
  albums, every scan reads that list, the album is marked **New version**, and the next sync
  downloads it again. Before, a downloaded album was never looked at again.
- **Fill in missing tags** (Settings): title, album, track and disc number, year - from the album
  page, into files that lack them (khinsider's files often have none). Tags already there are kept.
- **Notifications** (Settings): ntfy, Gotify, Discord or Home Assistant webhooks - downloads finished,
  new albums for your ticked platforms, problems. With a **Send a test** button.
- Clean up shows how far it has looked through the download folder, in its window and the Activity panel.

### Improved
- PNG covers become a real folder.jpg (converted with Pillow, see-through parts on black). Existing
  folder.png files are swapped for folder.jpg at the next sync.
- The steps at the end of a sync (old formats, playlists and covers, tags) work out the finished
  albums once instead of each on its own.

## [1.0.10] - 2026-10-25

### Improved
- Check download folder shows a progress bar in the Activity panel: files looked at, time
  remaining, and how many are missing so far.

### Fixed
- Counters of the last sync or scan stayed on screen after they should have been hidden (e.g. during
  a check of the download folder).

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
