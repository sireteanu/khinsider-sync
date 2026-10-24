# Khinsider Sync - user guide

Khinsider Sync keeps a folder of game soundtracks from [khinsider](https://downloads.khinsider.com/)
on your server or PC, and keeps it up to date. You tick what you want; it downloads it, and later
syncs only fetch what is new.

## 1. Start it

- **Unraid**: add the stack from the README (Docker → Compose), click **Compose Up**, then open
  `http://YOUR-SERVER:8485/`.
- **Windows**: double-click `start.bat`. Your browser opens `http://localhost:8485/`.

## 2. Scan the site

Click **Scan site**. It reads khinsider's album lists - about 230 pages - in a few minutes. No music
is downloaded. Afterwards you see every album, with the platforms on the left.

## 3. Find and tick albums

- Type in **Find an album** to search everything (name or address).
- Click a platform on the left to see its albums. Use **Every type** to show only gamerips,
  soundtracks, arrangements … - tick as many types as you like. The platform counts on the left then
  count only those types.
- The list holds every album of what you picked: just scroll, there is no "Show more".
- Tick the box in front of an album to sync it.
- Tick the box in front of a **platform** (or "Sync every album of …" above its list) to sync every
  album of that platform - also the ones khinsider adds later. Untick single albums to leave them
  out. A `-` box means some albums differ from the platform's tick.
- Click an album's name to see its tracks, formats and sizes. The first time you open it, its page
  is read from khinsider (a second or two).

The big line at the top shows how much you ticked, how much is downloaded and how many albums are
done. Albums whose page hasn't been read yet don't have a size; the next sync reads them.

Albums you already have show a **Downloaded** tag in the list (or **Partly downloaded** while a sync
is still getting them). Pick **Downloaded** or **Not downloaded** in the filter next to the search box
to see just those.

## 4. Choose formats and folders (Settings, in the ☰ menu)

- **Formats and download order**: tick the formats you want - and *Images* for covers and scans -
  and move them up or down. Choose whether an album gets **only the first ticked format it has**
  (e.g. FLAC, else MP3) or **every ticked format** (e.g. FLAC and MP3). Files download in that
  order: all the FLAC files first, then the MP3s. The buttons above the list fill it in for you.
  FLAC is often 3 to 5 times bigger.
- **Track names**: as khinsider names them, or `01 - Title.mp3` / `1-01 - Title.flac` from the
  album's disc and track numbers.
- **Art folder**: put the images in an `Art` folder inside each album's folder.
- **Playlist** and **folder.jpg**: a playlist and a copy of the cover in each downloaded album's
  folder, for Plex, Jellyfin, Kodi and music players.
- **Folders**: one folder per album, or grouped by platform, type or year.
- **A ticked platform brings in these album types**: e.g. tick only *Gamerip* so ticking SNES
  doesn't also bring in every arrangement and remix.
- To put one album somewhere else, open it and change **Saved as** (e.g. `Soundtracks/Persona 5`).
- To get one album in another format (FLAC for your favourites, say), open it and pick
  **Formats for this album**.

Changing folders later is safe: the next sync moves what you have; nothing is downloaded twice.

## 5. Sync

Click **Sync ticked**. The Activity panel shows how much is left and how long it will take; the
Downloading box shows each file. You can **Stop** at any time - the next sync continues where it
stopped, also after a restart or a power cut.

If khinsider goes down, downloads wait for it and carry on by themselves. If it starts sending web
pages instead of files (too many downloads), the sync pauses; the next one finishes the job.

## 6. Downloaded albums are done

khinsider doesn't change albums once they're up, so once an album is downloaded, syncs leave it
alone - they don't ask khinsider about it or look through its files again. If you deleted some of
its files by hand, open the album and click **Check files**: the next sync gets the missing ones.
To check every album at once, use **Check download folder** in the ☰ menu (it only looks at your
disk; it doesn't ask khinsider anything). It also notices files that were cut short.

**Better quality later**: if you downloaded an album as MP3 and khinsider also has it as FLAC, its
window offers **Upgrade to FLAC**. The next sync downloads the FLAC files and, once they are all
there, deletes the album's MP3s (the images stay). Until then you keep the MP3s.
**Cancel upgrade** undoes it.

To move your whole collection to another format, change **Formats** in Settings and tick
**Replace old formats**: each album's old files go once its new ones have all arrived.

**Sync now** in an album's window syncs just that album.
To pick up tracks khinsider added to an album later, open it and click **Read album page**.

## 7. Keep it up to date

In Settings, set **Sync automatically every N hours** (e.g. 24). Each run first looks at khinsider's
newest albums (one page), and every few days it reads the whole site, so new albums of your ticked
platforms arrive by themselves. **Scan for new albums** in the ☰ menu does the quick look by hand.

**Before a scheduled sync, check the download folder** is off by default: checking looks at every
file you downloaded, which wakes the disks (on Unraid, the array). Pick once a day, week or month if
you want it.

## 8. Clean up

After a sync, the Activity panel says if files in your folder aren't part of the sync any more -
removed from khinsider, in albums you unticked, or formats you no longer sync. Click **Review**, tick
what should go, and confirm. Only files Khinsider Sync downloaded are ever listed; other music in the
same folder is left alone.

## 9. Activity log

**Activity log** (top) lists every file added, updated or deleted, by day, for a year.

## Tips

- If the log says khinsider's Cloudflare protection **asks for a browser check**, its pages can't be
  read from that network right now. Nothing is marked failed; try again later. Servers in a data
  center (a rented cloud server) get this all the time; a home server normally doesn't.
- If khinsider ever starts refusing Khinsider Sync, check that **Look like Chrome** is on in Settings
  (it is by default).
- Be gentle with khinsider: keep parallel downloads at the default, and don't tick everything.
- A password for the page: set `KHINSIDER_PASSWORD` in Docker, or `start.bat --password …`.
- The ☰ menu also has **Read ticked album pages** (sizes without downloading), **Restart** and
  **Shut down**.
