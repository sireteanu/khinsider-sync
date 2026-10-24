# Changelog

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
