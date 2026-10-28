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
- The list holds every album of what you picked: just scroll, there is no "Show more". The **A–Z**
  strip at its right edge jumps to a letter (`#` for names starting with a digit or symbol).
- Tick the box in front of an album to sync it.
- Tick the box in front of a **platform** (or "Sync every album of …" above its list) to sync every
  album of that platform - also the ones khinsider adds later. Untick single albums to leave them
  out. A `-` box means some albums differ from the platform's tick.
- Have a list of albums (addresses like `https://downloads.khinsider.com/game-soundtracks/album/…`)?
  ☰ → **Tick albums from a list** ticks them all at once.
- Click an album's name to see its tracks, formats and sizes. The first time you open it, its page
  is read from khinsider (a second or two).

The big line at the top shows how much you ticked, how much is downloaded and how many albums are
done. Albums whose page hasn't been read yet don't have a size; the next sync reads them.

Albums you already have show a **Downloaded** tag in the list (or **Partly downloaded** while a sync
is still getting them). Pick **Downloaded** or **Not downloaded** in the filter next to the search box
to see just those. **Sort** next to it puts the **newest releases** first (by year; within a year
the most recently added to khinsider first), or the oldest, the largest, or by name.

## 4. Choose formats and folders (Settings, in the ☰ menu)

- **Formats and download order**: tick the formats you want - and *Images* for covers and scans -
  and move them up or down. Choose whether an album gets **only the first ticked format it has**
  (e.g. FLAC, else MP3) or **every ticked format** (e.g. FLAC and MP3). With "only the first",
  a track the album lacks in that format (most tracks in FLAC, a few only as MP3) comes in the next
  ticked format it has, so no track is left out. Files download in that
  order: all the FLAC files first, then the MP3s. The buttons above the list fill it in for you.
  FLAC is often 3 to 5 times bigger.
- **Track names**: as khinsider names them, or `01 - Title.mp3` / `1-01 - Title.flac` from the
  album's disc and track numbers (a title already ending in `.mp3` doesn't get it twice).
- **Art folder**: put the images in an `Art` folder inside each album's folder.
- **Playlist** and **folder.jpg**: a playlist and the album's cover (the picture khinsider shows for it,
  also when you don't sync images; PNG covers are turned into a JPEG) in each downloaded album's
  folder, for Plex, Jellyfin, Kodi and music players.
- **Put the cover into each music file**: the same cover inside the MP3, FLAC, M4A and OGG files, for
  players that only read tags. Files that already have a picture keep theirs.
- **Fill in missing tags**: khinsider's files often have no title, album or track number, so players
  show "unknown album". This fills them in from the album page; tags already there are kept.
- **Folders**: one folder per album, or grouped by platform, type or year.
- **A ticked platform brings in these album types**: e.g. tick only *Gamerip* so ticking SNES
  doesn't also bring in every arrangement and remix.
- To put one album somewhere else, open it and change **Saved as** (e.g. `Soundtracks/Persona 5`).
- To get one album in another format (FLAC for your favourites, say), open it and pick
  **Formats for this album**.

Changing folders later is safe: the next sync moves what you have; nothing is downloaded twice.

## 5. Going easy on khinsider

khinsider is protected by Cloudflare, which starts asking for a "browser check" when a connection
reads pages too fast - then nothing can be read for a while. In Settings → **Go easy on khinsider**:

- **Page reads a minute** (60 by default): each album's page counts, and for formats other than
  MP3 each track's page too (its download address is only there). **MP3 files need no track page**:
  their addresses come from the album page's music player, so an MP3 album is one page read
  however many tracks it has (setting **Take MP3 addresses from the album page's music player**, on
  by default). For FLAC this is the most files a minute: at 10, about 10 files a minute. A change
  applies at once, also to a sync under way.
- **Files an hour** and **Albums a day**: spread a long list over hours or days.
- **Albums at a time** (10 by default): how many new album pages a sync reads (one by one) before
  downloading those albums - a round. It goes straight on with the next round in the same sync.
  Bigger rounds spend less time on the housekeeping between them; 1 reads one album's page,
  downloads that album, then reads the next.

If khinsider asks for a browser check anyway, the sync pauses and tries again by itself after a
minute - then after 10, 20, 40, 80 minutes and at most 2 hours between tries while the check keeps
coming. Often only one page gets the check and the next one goes through (that's why opening an
album by hand right after works); after hours of heavy use it can last hours. Don't keep clicking
Sync: every blocked request can keep the block going. Lower Page reads a minute afterwards.

When a limit is reached it stops cleanly and carries on by itself when there's room again.

**FlareSolverr** (optional): if browser checks keep interrupting your syncs, run
[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) next to the app (the
`ghcr.io/flaresolverr/flaresolverr` container, port 8191 - the compose files have it ready to
uncomment) and enter its address in Settings → **FlareSolverr address**, e.g. `http://flaresolverr:8191`;
**Test** checks it's reachable. Nothing changes until a page gets the check: then FlareSolverr opens
that page in a real browser, and the app carries on with the cookie it got - usually one FlareSolverr
request covers the following pages too. If it can't help, the sync waits as described above. Your
page limits still apply.

### A khinsider account (optional)

khinsider lets paid accounts download whole albums at once. In Settings → **khinsider account**,
enter your username and password and click **Check sign-in**. With a paid account a sync then gets
each MP3 (or FLAC) album as **one zip**, unpacks it into the album's folder under the app's names
- far fewer requests than file by file. The Downloading box shows each zip with its progress bar
("Album zip (MP3)" in the album's folder). Getting a zip puts the album on your "My Mass Downloads"
list on khinsider; once the whole queue is through, the app takes the saved albums off it again (one
page read each, so it waits until nothing else needs those page reads).

**Download my khinsider list** fetches everything on that list - also albums you added on the site
yourself, which get ticked - straight from the list.

Albums in other formats, and everything once your account can't mass-download any more (when a
subscription ends), simply come file by file as without an account - the app notices by itself
and says so in the log and under the account in Settings. The password stays on your server and
is never shown again; clear the username to remove the account.

## 6. Sync

Click **Sync ticked**. The Activity panel shows how much is left and how long it will take - for a
long queue, the job line also says how many albums are still to go and about how long they'll take at
the pace of the last rounds (it appears after three rounds); the
Downloading box shows each file. You can **Stop** at any time - the next sync continues where it
stopped, also after a restart or a power cut.

A long list of new albums goes **a round at a time** (see Albums at a time above), all in
the same sync. The log then keeps one line a round ("Sync finished: …") besides the files, and sums
the queue up every half hour and at its end. The box is then called **This run**: its size and time cover only the albums read
so far, and the line under it says how many albums follow - their size isn't known until their
pages are read.

The speed in the log after each file is that file's own download speed. The overall speed is
set by your connection for MP3s, and by **Page reads a minute** for other formats (one track page
per file).

If khinsider goes down, downloads wait for it and carry on by themselves. If it starts sending web
pages instead of files (too many downloads), the sync pauses; the next one finishes the job.

## 7. Downloaded albums are done

Once an album is downloaded, syncs leave it alone - they don't ask khinsider about it or look
through its files again. The exception: when khinsider **replaces an album with another version**,
the album is marked **New version** and the next sync downloads it again. If you deleted some of
its files by hand, open the album and click **Check files**: the next sync gets the missing ones.
To check every album at once, use **Check download folder** in the ☰ menu (it only looks at your
disk; it doesn't ask khinsider anything). It also notices files that were cut short, and missing playlists or folder.jpg files (the next
sync writes them again). The Activity
panel shows how far it has got and how long it will take.

**Better quality later**: if you downloaded an album as MP3 and khinsider also has it as FLAC, its
window offers **Upgrade to FLAC**. The next sync downloads the FLAC files and, once they are all
there, deletes the album's MP3s (the images stay). Until then you keep the MP3s. A track the
album doesn't have as FLAC keeps its MP3.
**Cancel upgrade** undoes it.

To move your whole collection to another format, change **Formats** in Settings and tick
**Replace old formats**: each album's old files go once its new ones have all arrived.

**Sync now** in an album's window syncs just that album.
To pick up tracks khinsider added to an album later, open it and click **Read album page**.

## 8. Keep it up to date

In Settings, set **Sync automatically every N hours** (e.g. 24). Each run first looks at khinsider's
newest albums (one page), and every few days it reads the whole site, so new albums of your ticked
platforms arrive by themselves. **Scan for new albums** in the ☰ menu does the quick look by hand.

**Before a scheduled sync, check the download folder** is off by default: checking looks at every
file you downloaded, which wakes the disks (on Unraid, the array). Pick once a day, week or month if
you want it.

**Notifications**: in Settings, paste an ntfy topic, a Gotify, Discord or Home Assistant webhook
address - or for Pushbullet `pbul://` followed by your access token (pushbullet.com → Settings →
Access Tokens) - and click **Send a test**. You hear when downloads finish, when new albums arrive for your
ticked platforms, and when something needs a look.

## 9. Clean up

After a sync, the Activity panel says if files in your folder aren't part of the sync any more -
removed from khinsider, in albums you unticked, or formats you no longer sync. Click **Review**, tick
what should go, and confirm. Only files Khinsider Sync downloaded are ever listed; other music in the
same folder is left alone.

## 10. Activity log

**Activity log** (top) lists every file added, updated or deleted, by day, for a year.

## Tips

- If the log says khinsider's Cloudflare protection **asks for a browser check**, its pages can't be
  read from that network right now. Nothing is marked failed; try again later. Servers in a data
  center (a rented cloud server) get this all the time; a home server normally doesn't.
- If khinsider ever starts refusing Khinsider Sync, check that **Look like Chrome** is on in Settings
  (it is by default), and consider setting up **FlareSolverr** (section 5).
- Be gentle with khinsider: keep parallel downloads at the default, and don't tick everything.
- A password for the page: set `KHINSIDER_PASSWORD` in Docker, or `start.bat --password …`.
- The ☰ menu also has **Read ticked album pages** (sizes without downloading), **Restart** and
  **Shut down**.
