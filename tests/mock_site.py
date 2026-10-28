"""In-process mock of downloads.khinsider.com for tests. Its pages copy the live site's markup
(checked in a browser, October 2026; samples in tests/fixtures/):

  /console-list                                   platform list: <a href="/game-soundtracks/x">X</a> (12)<br>
  /?page=N                                        home page: "Latest Soundtracks", newest first, one
                                                  table.albumList per day under h3.latestSoundtrackHeading
                                                  (LATEST_PAGE_SIZE albums a page; a day can span pages)
  /game-soundtracks/browse/<L>?page=N             table.albumList (cover | name | platforms | type | year),
                                                  .pagination .counter "Page N of M", "Found N albums!"
  /game-soundtracks/album/<slug>                  details <p align="left">, .albumImage links, table#songlist
                                                  (optional CD and # columns, one size column per format)
  /game-soundtracks/album/<slug>/<file twice %-encoded>   track page with .songDownloadLink links
  /soundtracks/<slug>/<token>/<file>              the file itself (Range, If-Range, Last-Modified/ETag)
  /soundtracks/<slug>/<image>                     album images

Albums (PAGE_SIZE albums per browse page, so letter S has two pages):
  super-mario-world-snes-gamerip   SNES gamerip, MP3+FLAC, # column, a cover image
  super-mario-world-the-jazz-arrangements   also named "Super Mario World" (folder names must not clash)
  sonic-the-hedgehog-genesis       Genesis, MP3 only
  a-bird-story-...-2014            Linux/MacOS/Windows, MP3+OGG, no # column
  persona-5                        PS4/Windows soundtrack, CD and # columns, two discs, two images
  big-album                        one big track (resume / progress tests)
  activity-case-01                 '"Activity" Case:01 ...': no platform, a name with characters
                                   Windows doesn't allow
Modes (combine with commas):
  throttle    every 4th page request answers 429 + Retry-After: 1
  drop        the first two downloads of the big track are cut off mid-transfer
  slowfile    the big track is streamed slowly (for stop / progress tests)
  limitfile   big-album gets "02. Limited.mp3", whose file always answers with an HTML page
  brokenfile  big-album gets "03. Broken.mp3", whose file always answers 502 (the site is fine)
  bulk        adds bulk-album (Windows) with 40 slow-ish tracks (parallel-download tests)
Attributes you can change while a test runs:
  max_conc    downloads allowed at once; more get 503 + Retry-After: 1 (None = unlimited)
  daily_limit after this many completed downloads every file answers with an HTML page
  down        an HTTP status (e.g. 522): the whole site answers with it (None = up)
  down_after  set `down` to 522 once this many downloads have completed (None = never)
  challenge   True: every page (not the files) answers Cloudflare's browser check - 403 with
              cf-mitigated: challenge and "Just a moment..." (what a data-center address gets)
  broken_images  image names whose files answer 404 although the album page lists them
  robots_extra  more robots.txt lines (robots.txt is sent as text/html, like the live site)
Methods: modify(slug, file, grow) replaces a file; remove(slug) takes an album off the site;
add_album(slug, name, **fields) puts a new album up (added today unless `added` is given);
rename(old, new) moves an album to a new address (the old one redirects).
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import socket
import threading
import time
import urllib.parse as up
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def _tiny_png() -> bytes:
    """A real 4x4 PNG, half see-through (image readers can open it; trailing bytes are ignored)."""
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + b"\xff\x00\x00\xff\xff\x00\x00\xff\x00\x00\x00\x00\x00\x00\x00\x00" for _ in range(4))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


TINY_PNG = _tiny_png()
PAGE_SIZE = 2
LATEST_PAGE_SIZE = 3
LETTERS = ["0-9"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]
SMALL = 30_000
BIG = 2_000_000
BULK = 60_000
PLATFORMS = {"nintendo-snes": "SNES", "sega-mega-drive-genesis": "Genesis / Mega Drive", "linux": "Linux",
             "mac-os": "MacOS", "windows": "Windows", "playstation-4": "PS4", "amiga": "Amiga"}


def base_albums() -> dict:
    return {
        "super-mario-world-snes-gamerip": dict(
            name="Super Mario World", platforms=["nintendo-snes"], type="Gamerip", year="1990",
            added="Apr 24th, 2025", formats=["MP3", "FLAC"], cd=False, num=True, images=["Cover.jpg"],
            tracks=[("", "1", "01. Nintendo Logo", "Nintendo Logo", "0:02"),
                    ("", "2", "02. Title Screen", "Title Screen", "0:46"),
                    ("", "3", "03. Overworld", "Overworld", "2:47")]),
        "super-mario-world-the-jazz-arrangements": dict(
            name="Super Mario World", platforms=["nintendo-snes"], type="Arrangement", year="1991",
            added="Jan 3rd, 2024", formats=["MP3"], cd=False, num=True, images=[],
            tracks=[("", "1", "01. Overworld (Jazz)", "Overworld (Jazz)", "3:10"),
                    ("", "2", "02. Castle (Rock)", "Castle (Rock)", "2:59")]),
        "sonic-the-hedgehog-genesis": dict(
            name="Sonic the Hedgehog", platforms=["sega-mega-drive-genesis"], type="Gamerip", year="1991",
            added="Mar 9th, 2023", formats=["MP3"], cd=False, num=True, images=[],
            tracks=[("", "1", "01. Green Hill Zone", "Green Hill Zone", "2:41")]),
        "a-bird-story-linux-macos-windows-gamerip-2014": dict(
            name="A Bird Story", platforms=["linux", "mac-os", "windows"], type="Gamerip", year="2014",
            added="Dec 17th, 2025", formats=["MP3", "OGG"], cd=False, num=False, images=["cover.jpg"],
            tracks=[("", "", "A Bird Story (Piano)", "A Bird Story (Piano)", "3:23"),
                    ("", "", "Yesterday - Kan (Reives) Gao", "Yesterday - Kan (Reives) Gao", "3:49")]),
        "persona-5": dict(
            name="PERSONA5 ORIGINAL SOUNDTRACK", platforms=["playstation-4", "windows"], type="Soundtrack",
            year="2017", added="Jun 29th, 2024", formats=["MP3", "FLAC"], cd=True, num=True,
            images=["Cover.jpg", "Alternate Cover.png"],
            tracks=[("1", "1", "1-01. Wake Up, Get Up, Get Out There", "Wake Up, Get Up, Get Out There", "4:38"),
                    ("1", "2", "1-02. Phantom", "Phantom", "2:18"),
                    ("2", "1", "2-01. Beneath the Mask", "Beneath the Mask", "4:40")]),
        "big-album": dict(
            name="Big Album", platforms=["windows"], type="Gamerip", year="2020", added="Feb 1st, 2025",
            formats=["MP3"], cd=False, num=True, images=[],
            tracks=[("", "1", "01. Big Track", "Big Track", "9:59")]),
        "activity-case-01": dict(
            name='"Activity" Case:01 -Graveyard Memory-', platforms=[], type="Soundtrack", year="2019",
            added="Oct 5th, 2019", formats=["MP3"], cd=False, num=True, images=[],
            tracks=[("", "1", "01. Graveyard", "Graveyard", "1:00")]),
    }


def letter_of(name: str) -> str:
    c = name[:1].upper()
    return c if "A" <= c <= "Z" else "0-9"


def mb(n: int) -> str:
    """khinsider shows track sizes in MB with two decimals: '0.10 MB', '5.64 MB'."""
    return f"{n / 1024 / 1024:.2f} MB"


def q2(s: str) -> str:
    """khinsider's track page addresses encode the file name twice."""
    return up.quote(up.quote(s))


LEFT = ('<div id="leftColumn"><a href="/"><h1>Video<br>Game<br>Music</h1></a><h3>Albums</h3><ul>'
        '<li><a href="/game-soundtracks">Browse All</a></li><li><a href="/top40">Top 40</a></li></ul>'
        '<h3>Platforms</h3><ul><li><a href="/console-list">Full Platform List</a></li>'
        '<li><a href="/game-soundtracks/nintendo-snes">SNES</a></li></ul><h3>By Type</h3><ul>'
        '<li><a href="/game-soundtracks/gamerips">Gamerips</a></li><li><a href="/game-soundtracks/ost">Soundtracks</a></li>'
        '</ul></div>')


def page(title: str, content: str) -> str:
    return (f'<!DOCTYPE html>\n<html><head><meta charset="UTF-8"><title>{html.escape(title)}</title></head>\n'
            f'<body>\n<div id="container">\n\t{LEFT}\n  \t<div id="rightColumn">\n  \t\t<div id="pageContent">\n'
            f'{content}\n  \t\t</div>\n  \t</div>\n</div>\n</body></html>')


def pack(code: str) -> str:
    """Dean Edwards' packer, base 62, as khinsider's album pages use it: every word becomes its index
    in a dictionary (most frequent first), written in the packer's own digits."""
    import collections as _c

    def enc(c: int) -> str:
        return ("" if c < 62 else enc(c // 62)) + (chr(c % 62 + 29) if c % 62 > 35 else "0123456789abcdefghijklmnopqrstuvwxyz"[c % 62])
    words = [w for w, _n in _c.Counter(re.findall(r"\b[0-9A-Za-z]+\b", code)).most_common()]
    index = {w: enc(i) for i, w in enumerate(words)}
    keys = ["" if index[w] == w else w for w in words]
    payload = re.sub(r"\b[0-9A-Za-z]+\b", lambda m: index[m.group()], code)
    payload = payload.replace("\\", "\\\\").replace("'", "\\'")
    head = ("eval(function(p,a,c,k,e,d){e=function(c){return(c<a?'':e(parseInt(c/a)))+((c=c%a)>35?String."
            "fromCharCode(c+29):c.toString(36))};if(!''.replace(/^/,String)){while(c--){d[e(c)]=k[c]||e(c)}k=["
            "function(e){return d[e]}];e=function(){return'\\\\w+'};c=1};while(c--){if(k[c]){p=p.replace(new "
            "RegExp('\\\\b'+e(c)+'\\\\b','g'),k[c])}}return p}")
    return f"{head}('{payload}',62,{len(words)},'{'|'.join(keys)}'.split('|'),0,{{}}))\n"


class MockSite:
    def __init__(self, modes: str = ""):
        self.modes = {m.strip() for m in modes.split(",") if m.strip()}
        self.albums = base_albums()
        if "limitfile" in self.modes:
            self.albums["big-album"]["tracks"].append(("", "2", "02. Limited", "Limited", "1:00"))
        if "brokenfile" in self.modes:
            self.albums["big-album"]["tracks"].append(("", "3", "03. Broken", "Broken", "1:00"))
        if "bulk" in self.modes:
            self.albums["bulk-album"] = dict(
                name="Bulk Album", platforms=["windows"], type="Gamerip", year="2021", added="May 5th, 2025",
                formats=["MP3"], cd=False, num=True, images=[],
                tracks=[("", str(i), f"{i:02d}. Bulk {i:02d}", f"Bulk {i:02d}", "1:00") for i in range(1, 41)])
        self.redirects: dict = {}          # old slug -> new slug
        self.versions: dict = {}           # (slug, file) -> (version, extra bytes)
        self.max_conc = None
        self.daily_limit = None
        self.down = None
        self.down_after = None
        self.active = 0
        self.peak_active = 0
        self.completed = 0
        self.rejected = 0
        self.slow_all = False
        self.challenge = False             # pages answer Cloudflare's browser check (403, cf-mitigated)
        self.robots_extra = ""             # more robots.txt lines
        self.broken_images: set = set()    # image names that answer 404 (listed on the page, file gone)
        self.player = True                 # album pages carry the music player's packed track list
        self.player_broken: set = set()    # albums whose player addresses are wrong (403 from the files)
        self.requests: list = []           # (path, headers dict)
        self._lock = threading.Lock()
        self._count = 0
        self._drops: dict = {}
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _head(self, code, ctype, length, extra=()):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()

            def _send(self, code, body, ctype="text/html; charset=UTF-8", extra=()):
                if isinstance(body, str):
                    body = body.encode()
                self._head(code, ctype, len(body), extra)
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                u = up.urlparse(self.path)
                path = u.path
                with site._lock:
                    site.requests.append((self.path, {k.title(): v for k, v in self.headers.items()}))
                    site._count += 1
                    n = site._count
                if site.down_after is not None and site.completed >= site.down_after and path.startswith("/soundtracks/"):
                    site.down, site.down_after = 522, None
                if site.down:
                    return self._send(site.down, "<html><head><title>Connection timed out</title></head>"
                                                 "<body>The web server is not responding.</body></html>")
                if path == "/robots.txt":                     # the live site sends it as text/html
                    return self._send(200, "\nUser-agent: *\r\nDisallow: /unavailable" + site.robots_extra,
                                      "text/html; charset=UTF-8")
                if site.challenge and not path.startswith("/soundtracks/"):
                    return self._send(403, '<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>'
                                      '</head><body>Enable JavaScript and cookies to continue</body></html>',
                                      extra=[("cf-mitigated", "challenge"), ("Server", "cloudflare")])
                if path.startswith("/soundtracks/"):
                    return self._file(path)
                if "throttle" in site.modes and n % 4 == 0:
                    return self._send(429, "slow down", "text/plain", [("Retry-After", "1")])
                if path == "/console-list":
                    return self._send(200, site.console_list())
                if path.startswith("/game-soundtracks/browse/"):
                    letter = path.rsplit("/", 1)[-1]
                    num = int((up.parse_qs(u.query).get("page") or ["1"])[0])
                    return self._send(200, site.browse(letter, num))
                if path.startswith("/game-soundtracks/album/"):
                    rest = path[len("/game-soundtracks/album/"):]
                    slug, _, track = rest.partition("/")
                    if slug in site.redirects:
                        new = site.redirects[slug]
                        loc = f"/game-soundtracks/album/{new}" + (f"/{track}" if track else "")
                        return self._send(301, "", extra=[("Location", loc)])
                    if track:
                        return self._send(200, site.track_page(slug, track))
                    return self._send(200, site.album_page(slug))
                if path == "/":
                    num = int((up.parse_qs(u.query).get("page") or ["1"])[0])
                    return self._send(200, site.latest(num))
                return self._send(404, page("Not found", "<h2>Ooops!</h2>"))

            def _limit_page(self):
                self._send(200, "<html><head><title>Too many downloads</title></head><body>come back tomorrow</body></html>")

            def _file(self, path):
                parts = [up.unquote(p) for p in path.split("/")[2:]]
                if len(parts) == 2:                                    # an album image
                    slug, name = parts
                    a = site.albums.get(slug)
                    if not a or name not in a["images"] or name in site.broken_images:
                        return self._send(404, "")
                    self._counted = True                               # images aren't counted
                    return self._serve(slug, name)
                if len(parts) != 3:
                    return self._send(404, "")
                slug, token, name = parts
                if token != site.token(slug) or not site.has_file(slug, name):
                    return self._send(403, "<html><body>Forbidden</body></html>")
                if name.startswith("03. Broken"):
                    return self._send(502, "bad gateway")
                if name.startswith("02. Limited"):
                    return self._limit_page()
                if site.daily_limit is not None and site.completed >= site.daily_limit:
                    return self._limit_page()
                with site._lock:
                    busy = site.max_conc is not None and site.active >= site.max_conc
                    if busy:
                        site.rejected += 1
                    else:
                        site.active += 1
                        site.peak_active = max(site.peak_active, site.active)
                if busy:
                    return self._send(503, "too many connections", "text/plain", [("Retry-After", "1")])
                self._counted = False
                try:
                    self._serve(slug, name)
                finally:
                    if not self._counted:
                        with site._lock:
                            site.active -= 1

            def _serve(self, slug, name):
                data = site.content(slug, name)
                lm, etag = site.last_modified(slug, name), site.etag(slug, name)
                validators = [("Last-Modified", lm), ("ETag", etag)]
                ctype = site.ctype(name)
                start = 0
                rng = self.headers.get("Range")
                if_range = self.headers.get("If-Range")
                if rng and (if_range is None or if_range in (lm, etag)):
                    start = int(rng.split("=")[1].split("-")[0])
                    if start >= len(data):
                        return self._head(416, "text/plain", 0)
                    self._head(206, ctype, len(data) - start,
                               [("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")] + validators)
                else:
                    self._head(200, ctype, len(data), validators)
                chunk = data[start:]
                big = name.startswith("01. Big Track")
                if "drop" in site.modes and big and site._drops.get(name, 0) < 2:
                    site._drops[name] = site._drops.get(name, 0) + 1
                    self.wfile.write(chunk[: len(chunk) // 3])
                    self.wfile.flush()
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                slow = ("slowfile" in site.modes and big) or slug == "bulk-album" or site.slow_all
                step = (65536 if big else 15000) if slow else len(chunk) or 1
                try:
                    for i in range(0, len(chunk), step):
                        last = i + step >= len(chunk)
                        if last and not self._counted:    # count it finished before the final bytes
                            with site._lock:              # go out, so the client can never see a
                                site.active -= 1          # slot as busy that it has already freed
                                site.completed += 1
                                self._counted = True
                        self.wfile.write(chunk[i:i + step])
                        self.wfile.flush()
                        if slow and not last:
                            time.sleep(0.05)
                except OSError:
                    return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # ---- site content ----
    @staticmethod
    def token(slug: str) -> str:
        return hashlib.md5(slug.encode()).hexdigest()[:10]

    @staticmethod
    def track_formats(a: dict, stem: str) -> list:
        """The formats one track is offered in: the album's, less any listed in a["lacks"][stem]
        (khinsider: an album in FLAC with a few tracks as MP3 only - their FLAC cell is empty)."""
        return [f for f in a["formats"] if f not in (a.get("lacks") or {}).get(stem, ())]

    def has_file(self, slug: str, name: str) -> bool:
        a = self.albums.get(slug)
        if not a:
            return False
        stem, _, ext = name.rpartition(".")
        return any(t[2] == stem for t in a["tracks"]) and ext.upper() in self.track_formats(a, stem)

    @staticmethod
    def ctype(name: str) -> str:
        ext = name.rsplit(".", 1)[-1].lower()
        return {"mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg", "jpg": "image/jpeg",
                "png": "image/png"}.get(ext, "application/octet-stream")

    def content(self, slug: str, name: str) -> bytes:
        """Deterministic file body with a real header for its type (changes when modified)."""
        version, extra = self.versions.get((slug, name), (0, 0))
        ext = name.rsplit(".", 1)[-1].lower()
        size = (BIG if name.startswith("01. Big Track") else BULK if slug == "bulk-album" else SMALL) + extra
        if ext == "flac":
            size *= 3
        if ext in ("jpg", "png"):
            size = 8000 + extra
        # MP3: an empty ID3v2.4 tag; FLAC: a real STREAMINFO block - so tag readers (covers.py) can open them
        streaminfo = (b"\x10\x00\x10\x00" + b"\x00" * 6
                      + ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + b"\x00" * 16)
        head = {"mp3": b"ID3\x04\x00\x00\x00\x00\x00\x00", "flac": b"fLaC\x80\x00\x00\x22" + streaminfo,
                "ogg": b"OggS", "jpg": b"\xff\xd8\xff\xe0", "png": TINY_PNG}.get(ext, b"")
        seed = hashlib.sha256(f"{slug}/{name}#{version}".encode()).digest()
        return head + (seed * (size // 32 + 1))[: size - len(head)]

    def last_modified(self, slug, name) -> str:
        version, _ = self.versions.get((slug, name), (0, 0))
        return formatdate(1_700_000_000 + version * 60, usegmt=True)

    def etag(self, slug, name) -> str:
        version, extra = self.versions.get((slug, name), (0, 0))
        return f'"{version}-{extra}"'

    def modify(self, slug: str, name: str, grow: int = 0) -> None:
        """Simulate the site replacing a file (new content, optionally a new size)."""
        version, extra = self.versions.get((slug, name), (0, 0))
        self.versions[(slug, name)] = (version + 1, extra + grow)

    def remove(self, slug: str) -> None:
        self.albums.pop(slug, None)

    def rename(self, old: str, new: str) -> None:
        self.albums[new] = self.albums.pop(old)
        self.redirects[old] = new

    # ---- pages ----
    def console_list(self) -> str:
        counts = {p: sum(p in a["platforms"] for a in self.albums.values()) for p in PLATFORMS}
        rows = "".join(f'<a href="/game-soundtracks/{p}">{html.escape(n)}</a> ({counts[p]})<br> '
                       for p, n in sorted(PLATFORMS.items(), key=lambda kv: kv[1].casefold()))
        return page("Video Game Music", f"<h2>Consoles / Platforms</h2> {rows}")

    def add_album(self, slug: str, name: str, **fields) -> None:
        a = dict(name=name, platforms=["windows"], type="Gamerip", year="2026",
                 added=time.strftime("%b %d, %Y").replace(" 0", " "), formats=["MP3"], cd=False, num=True,
                 images=[], tracks=[("", "1", "01. New Track", "New Track", "1:00")])
        a.update(fields)
        self.albums[slug] = a

    @staticmethod
    def _row(slug: str, a: dict) -> str:
        plats = ", ".join(f'<a href="/game-soundtracks/{p}">{html.escape(PLATFORMS[p])}</a>' for p in a["platforms"])
        icon = (f'<img src="https://nu.example/soundtracks/{slug}/thumbs_small/cover.jpg">' if a["images"]
                else '<div class="albumIconDefaultSmall"><i class="material-icons">audiotrack</i></div>')
        return (f'<tr>   \n    <td class="albumIcon"><a href="/game-soundtracks/album/{slug}">{icon}</a></td>\n'
                f'    <td>\n                <a href="/game-soundtracks/album/{slug}">{html.escape(a["name"])}</a>\n'
                f'        </td>\n    <td>\n        {plats}    </td>\n    <td>{a["type"]}</td>\n'
                f'    <td>{a["year"]}</td>\n</tr>\n')

    def latest(self, num: int) -> str:
        """The home page (markup: tests/fixtures/home_latest.html)."""
        def day(a):
            t = a["added"].replace("st,", ",").replace("nd,", ",").replace("rd,", ",").replace("th,", ",")
            return time.strftime("%Y-%m-%d", time.strptime(t, "%b %d, %Y"))
        albums = sorted(self.albums.items(), key=lambda sa: (day(sa[1]), sa[0]), reverse=True)
        pages = max(1, -(-len(albums) // LATEST_PAGE_SIZE))
        chunk = albums[(num - 1) * LATEST_PAGE_SIZE: num * LATEST_PAGE_SIZE]
        head = ('<tr>                  \n    <th></th>\n    <th>        \n        <b>Album</b>    </th>\n'
                '    <th><b>Platform</b></th>\n    <th><b>Type</b></th>    \n    <th><b>Year</b></th>    \n</tr>\n')
        out, cur, rows = "", None, ""

        def flush():
            return (f'<h3 class="latestSoundtrackHeading">{heading(cur)}</h3><p><div class="albumListWrapper">\n'
                    '    <div class="albumListSize">\n        <a href="?albumListSize=compact" class="albumListSizeIconCompact">'
                    '<i class="material-icons">grid_on</i></a>\n    </div>\n    <div class="albumListDiv">\n'
                    f'<table class="albumList">\n{head}{rows}</table>\n    </div>\n</div>\n\n')

        def heading(d):
            t = time.strptime(d, "%Y-%m-%d")
            n = t.tm_mday
            suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
            return f"{time.strftime('%B', t)} {n}{suffix}, {t.tm_year}"

        for slug, a in chunk:
            if day(a) != cur:
                if cur is not None:
                    out += flush()
                cur, rows = day(a), ""
            rows += self._row(slug, a)
        if cur is not None:
            out += flush()
        pager = (f'<p></p>\n<div class="pagination">\n\t<p class="counter">\n\t\tPage {num} of {pages}\t</p>\n\t<ul>'
                 + "".join(f'<li><a title="{i}" href="?page={i}" class="pagenav">{i}</a></li>' if i != num
                           else f'<li><span class="pagenav">{i}</span></li>' for i in range(1, pages + 1))
                 + "</ul>\n</div>")
        return page("Video Game Music Downloads", '<h2>Latest Soundtracks <a href="/rss"><i class="material-icons" '
                    f'style="font-size: 15px;">rss_feed</i></a></h2>\n{out}{pager}')

    def browse(self, letter: str, num: int) -> str:
        albums = sorted(((s, a) for s, a in self.albums.items() if letter_of(a["name"]) == letter),
                        key=lambda sa: (sa[1]["name"].casefold(), sa[0]))
        pages = max(1, -(-len(albums) // PAGE_SIZE))
        chunk = albums[(num - 1) * PAGE_SIZE: num * PAGE_SIZE]
        rows = ""
        for slug, a in chunk:
            plats = ", ".join(f'<a href="/game-soundtracks/{p}">{html.escape(PLATFORMS[p])}</a>' for p in a["platforms"])
            icon = (f'<img src="https://nu.example/soundtracks/{slug}/thumbs_small/cover.jpg">' if a["images"]
                    else '<div class="albumIconDefaultSmall"><i class="material-icons">audiotrack</i></div>')
            rows += (f'<tr>   \n    <td class="albumIcon"><a href="/game-soundtracks/album/{slug}">{icon}</a></td>\n'
                     f'    <td>\n                <a href="/game-soundtracks/album/{slug}">{html.escape(a["name"])}</a>\n'
                     f'        </td>\n    <td>\n        {plats}    </td>\n    <td>{a["type"]}</td>\n'
                     f'    <td>{a["year"]}</td>\n</tr>\n')
        letters = " ".join(f'&nbsp;<a href="/game-soundtracks/browse/{x}">{x}</a> &nbsp;|' if x != letter
                           else f"&nbsp;[ {x} ] &nbsp;|" for x in LETTERS)
        pager = (f'<div class="pagination">\n\t<p class="counter">\n\t\tPage {num} of {pages}\t</p>\n\t<ul>'
                 + "".join(f'<li><a title="{i}" href="?page={i}&amp;orderby=name&amp;orderdirection=ASC" '
                           f'class="pagenav">{i}</a></li>' if i != num else f'<li><span class="pagenav">{i}</span></li>'
                           for i in range(1, pages + 1))
                 + "</ul>\n</div>") if pages > 1 else ""
        head = ('<tr>\n    <th></th>\n    <th><div style="display: flex;"><a href="?page=1&amp;orderby=name&amp;'
                'orderdirection=DESC"><b>Album Name</b> ↑</a><span><a href="?page=1&amp;orderby=date&amp;'
                'orderdirection=DESC"><b>Added</b> ⇅</a></span></div></th>\n    <th><b>Platform</b></th>\n'
                '    <th><b>Type</b></th>\n    <th style="width: 50px;"><a href="?page=1&amp;orderby=year&amp;'
                'orderdirection=DESC"><b>Year</b> ⇅</a></th>\n</tr>\n')
        table = (f'<div class="albumListWrapper"><div class="albumListDiv">\n<table class="albumList">\n<tbody>'
                 f'{head}{rows}</tbody></table>\n    </div>\n</div>') if albums else ""
        top = ('<h2>Top 12 Albums</h2><table><tbody><tr><td class="albumIconLarge"><a href="/game-soundtracks/'
               'album/persona-5"><img src="x.jpg"><p>Persona 5</p></a></td></tr></tbody></table>')
        content = (f'<p align="center">\n|\n{letters}\n</p>\n{top}\n<h2>Albums</h2>\n'
                   f'<p align="left">Found {len(albums)} albums!</p>{pager}{table}{pager}')
        return page("Albums - Free MP3 Downloads - Game Soundtracks", content)

    def album_page(self, slug: str) -> str:
        a = self.albums.get(slug)
        if a is None:
            return page("Video Game Music", "<h2>Ooops!</h2> <p>No such album</p>")
        plats = ", ".join(f'<a href="/game-soundtracks/{p}">{html.escape(PLATFORMS[p])}</a>' for p in a["platforms"])
        type_href = {"Gamerip": "gamerips", "Soundtrack": "ost", "Arrangement": "arrangements"}[a["type"]]
        sizes = {f: sum(len(self.content(slug, f"{t[2]}.{f.lower()}")) for t in a["tracks"]) for f in a["formats"]}
        total = ", ".join(f"<b>{sizes[f] // 1048576:,} MB</b> ({f})" for f in a["formats"])
        info = (f'<p align="left">\n            Platforms: {plats}<br>\n            Year: <b>{a["year"]}</b><br>\n'
                f'    <br>Number of Files: <b>{len(a["tracks"])}</b><br>\n    Total Filesize: {total}<br>\n'
                f'    Date Added: <b>{a["added"]}</b>\n            &nbsp; | <a href="/game-soundtracks/album/{slug}/'
                f'change_log">Change Log<!-- (1)--></a>\n        <br>\n            Album type: <b><a href="/game-'
                f'soundtracks/{type_href}">{a["type"]}</a></b><br>\n            Uploaded by: <a href="/forums/'
                f'index.php?members/1/">someone</a>\n        <br>\n    </p>')
        imgs = "".join(f'\n        <div class="albumImage">\n            <a href="{self.url}/soundtracks/{slug}/'
                       f'{up.quote(i)}" target="_blank">\n                <img src="{self.url}/soundtracks/{slug}/'
                       f'thumbs/{up.quote(i)}">\n            </a><br>\n        </div>' for i in a["images"])
        th = '\t<th width="25px" align="center">&nbsp;</th>\n'
        if a["cd"]:
            th += '\t<th width="20px" align="center"><b>CD</b></th>\n'
        if a["num"]:
            th += '\t<th width="25px" align="center"><b>#</b></th>\n'
        th += '\t<th colspan="2"><b>Song Name</b></th>\n'
        th += "".join(f'\t<th width="60px" align="right"><b>{f}</b></th>\n' for f in a["formats"])
        th += '    <th width="20px">&nbsp;</th>\n\t<th align="center">&nbsp;</th>\n'
        rows = ""
        for disc, num, stem, title, dur in a["tracks"]:
            href = f"/game-soundtracks/album/{slug}/{q2(stem + '.mp3')}"
            cell = lambda text: (f'   \t\t<td class="clickable-row" align="right"><a href="{href}" '  # noqa: E731
                                 f'style="font-weight:normal;">{text}</a></td>\n')
            rows += '\t<tr>\n\t\t<td align="center" title="play track"><div class="playTrack"><div class="arrow-play">&nbsp;</div></div></td>\n'
            if a["cd"]:
                rows += f'\t\t\t<td align="center">{disc}</td>\n'
            if a["num"]:
                rows += f'\t\t\t<td align="right" style="padding-right: 8px;">{num}.</td>\n'
            rows += f'\t \t<td class="clickable-row"><a href="{href}">{html.escape(title)}</a></td>\n'
            rows += cell(dur)
            rows += "".join(cell(mb(len(self.content(slug, f"{stem}.{f.lower()}")))
                                 if f in self.track_formats(a, stem) else "") for f in a["formats"])
            rows += (f'                <td class="playlistDownloadSong"><a href="{href}"><i class="material-icons">'
                     f'get_app</i></a></td>\n        <td class="playlistAddCell"><div class="playlistAddTo" songid="{self.songid(slug, stem)}">'
                     f'<i class="material-icons">playlist_add</i></div></td>\n\t</tr>\n')
        span = 2 + a["cd"] + a["num"]
        foot = (f'\t<tr id="songlist_footer">\n\t<th colspan="{span}" align="right"><b>Total:</b></th>\n'
                f'\t<th align="right"><b>1h</b></th>\n' + "".join(f'\t<th align="right"><b>{sizes[f] // 1048576:,} MB</b></th>\n'
                                                               for f in a["formats"])
                + '   <th align="right" colspan="2">&nbsp;</th>\n</tr>\n')
        content = (f'<h2>{html.escape(a["name"])}</h2>\n{info}<table><tbody><tr><td>{imgs}\n    </td></tr></tbody></table>\n'
                   f'<p></p>\n<div class="albumMassDownload"><a href="/cp/add_album/1">x</a><div> Download all songs at '
                   f'once: <a href="/cp/add_album/1">click to download</a></div>\n</div>\n<div id="nowPlay"></div>\n'
                   f'<div id="audiowrap"></div>\n<table id="songlist">\n<tbody><tr id="songlist_header">\n{th}</tr>\n'
                   f'{rows}{foot}</tbody></table>\n{self.player_script(slug)}<h2>Description</h2><p>Some text.</p>\n'
                   f'<h2>People who viewed this also viewed</h2><table><tbody><tr><td class="albumIconLarge">'
                   f'<a href="/game-soundtracks/album/sonic-the-hedgehog-genesis"><p>Sonic</p></a></td></tr></tbody></table>')
        return page(f"{a['name']} MP3 - Download Soundtracks for FREE!", content)

    @staticmethod
    def songid(slug: str, stem: str) -> str:
        return str(1_000_000 + int(hashlib.sha1(f"{slug}/{stem}".encode()).hexdigest()[:8], 16) % 9_000_000)

    def player_script(self, slug: str) -> str:
        """The album page's music player like khinsider's (tests/fixtures/album_player_bead.html): a
        `tracks` list with each MP3's address, packed with Dean Edwards' packer (base 62)."""
        a = self.albums[slug]
        if not self.player or "MP3" not in a["formats"]:
            return ""
        token = "wrongtoken" if slug in self.player_broken else self.token(slug)
        host = self.url.split("://", 1)[1]
        tracks = [{"track": int(num or 0), "name": title, "length": dur, "songid": self.songid(slug, stem),
                   "file": f"{host}/soundtracks/{slug}/{token}/{up.quote(stem + '.mp3')}"}
                  for _disc, num, stem, title, dur in a["tracks"] if "MP3" in self.track_formats(a, stem)]
        code = ("function formatTime(seconds){return seconds}jQuery(function($){var supportsAudio=!!document."
                "createElement('audio').canPlayType;if(supportsAudio){var index=1,playing=false,mediaPath='http://',"
                "extension='',tracks=" + json.dumps(tracks)[:-1] + ",],trackCount=tracks.length,shuffleOn=false;}});")
        return "<script type=\"text/javascript\">" + pack(code) + "</script>\n"

    def track_page(self, slug: str, track: str) -> str:
        a = self.albums.get(slug)
        name = up.unquote(up.unquote(track))
        stem = name.rsplit(".", 1)[0]
        if a is None or not any(t[2] == stem for t in a["tracks"]):
            return page("Video Game Music", "<h2>Ooops!</h2> <p>No such song</p>")
        links = "".join(
            f'<p><a href="{self.url}/soundtracks/{slug}/{self.token(slug)}/{up.quote(stem + "." + f.lower())}">'
            f'<span class="songDownloadLink"><i class="material-icons">get_app</i>Click here to download as {f}'
            f'</span></a> ({mb(len(self.content(slug, stem + "." + f.lower())))})</p>\n' for f in self.track_formats(a, stem))
        first = f'{self.url}/soundtracks/{slug}/{self.token(slug)}/{up.quote(stem + "." + a["formats"][0].lower())}'
        content = (f'<h2>{html.escape(a["name"])}</h2> <br><br>\n<p align="left">Go back to the album - <b><a href='
                   f'"/game-soundtracks/album/{slug}">{html.escape(a["name"])}</a></b></p>\n<p align="left"> Album '
                   f'name: <b>{html.escape(a["name"])}</b><br> Song name: <b>{html.escape(stem)}</b></p> <br><br>\n'
                   f'{links}<p>(right click, Save Link As... to download the file)</p> <br><br>\n'
                   f'<audio id="audio" controls="" preload="auto" src="{first}"></audio>')
        return page(f"{stem} - {a['name']} MP3 Download", content)

    # ---- lifecycle ----
    def start(self) -> "MockSite":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ---- helpers for assertions ----
    def paths(self):
        return [p for p, _ in self.requests]

    def file_requests(self):
        """[(path, Range header)] for every music / image file request."""
        return [(p, h.get("Range")) for p, h in self.requests if p.startswith("/soundtracks/")]

    def track_pages(self):
        return [p for p, _ in self.requests if p.startswith("/game-soundtracks/album/") and p.count("/") >= 4]

    def album_pages(self):
        return [p for p, _ in self.requests if p.startswith("/game-soundtracks/album/") and p.count("/") == 3]

    def reset_log(self):
        self.requests.clear()
