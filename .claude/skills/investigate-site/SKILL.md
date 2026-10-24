---
name: investigate-site
description: Work out why Khinsider Sync reads downloads.khinsider.com wrongly (missing albums or tracks, wrong sizes, formats or dates, failing downloads) or adapt it to a change in the site's markup. Use when a scan, an album page or a sync gives unexpected results.
argument-hint: <url of the page that behaves wrongly>
allowed-tools: WebFetch, Read, Grep, Glob, Bash(python -m pytest *)
---

Investigate: $ARGUMENTS

1. **Look at the real page** (ask the user first - development never touches the real site unasked).
   A browser's view of the raw HTML is best (WebFetch rewrites pages). Check one browse page, the
   album page and one track page. Record: the `table.albumList` row cells, `.pagination .counter`,
   the details paragraph, `#songlist_header` columns (CD, #, formats), a track row, `.albumImage`
   links, and the track page's `.songDownloadLink` links.
2. **Update the samples**: save trimmed copies to `tests/fixtures/` and fix `tests/test_parse.py`;
   then copy the change into `tests/mock_site.py` (it must match the live markup).
3. **Fix** `src/khinsider_sync/parse.py` (and `scanner.py` / `engine.Resolver` if addresses changed).
   Keep the rules in `CLAUDE.md`: scans never read album pages or files.
4. Run `python -m pytest` and `python -m pyflakes src tests`, then report what was wrong and what changed.

Never start a full download of the real site while investigating.
