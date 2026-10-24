"""Reading khinsider's pages (markup checked in a browser, October 2026; see README "Facts").

  /console-list                                 every platform with its album count
  /game-soundtracks/browse/<A-Z|0-9>?page=N     500 albums a page: name, platforms, type, year
  /game-soundtracks/album/<slug>                one album: details, track list (#songlist), images
  /game-soundtracks/album/<slug>/<file>.mp3     one track: links to the file in each format

Nothing here makes requests; the scanner and the engine fetch the pages.
"""
from __future__ import annotations

import re
import time
from typing import Optional
from urllib.parse import unquote, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from .core.constants import FORMAT_EXT, IMAGE_EXTENSIONS
from .core.util import extension

LETTERS = ["0-9"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]


def letter_of(name: str) -> str:
    """The browse page an album's name is listed under ("A".."Z", else "0-9")."""
    c = (name or "")[:1].upper()
    return c if "A" <= c <= "Z" else "0-9"
ALBUM_PREFIX = "/game-soundtracks/album/"
PLATFORM_PREFIX = "/game-soundtracks/"
# links below /game-soundtracks/ that are not platforms
NOT_PLATFORMS = {"browse", "album", "developer", "publisher", "year", "gamerips", "ost", "singles",
                 "arrangements", "remixes", "compilations", "inspired-by"}
_SIZE = re.compile(r"^\s*\d+(?:[.,]\d+)?\s*(?:B|KB|MB|GB|TB)\s*$", re.I)
_DURATION = re.compile(r"^\s*(?:\d+:)?\d{1,2}:\d{2}\s*$")
_ORDINAL = re.compile(r"(\d+)(st|nd|rd|th)\b")


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.text(deep=True) if node is not None else "").strip()


def norm_size(text: str) -> str:
    """'1,438 MB' -> '1438 MB' (a thousands separator, not a decimal comma); others unchanged."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if re.match(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?\s", t):
        t = t.replace(",", "")
    return t


def album_slug(url: str) -> str:
    """'/game-soundtracks/album/persona-5' (or a track page below it) -> 'persona-5'."""
    path = urlsplit(url).path
    i = path.find(ALBUM_PREFIX)
    if i < 0:
        return ""
    return unquote(path[i + len(ALBUM_PREFIX):].split("/", 1)[0])


def platform_slug(href: str) -> str:
    """'/game-soundtracks/nintendo-snes' -> 'nintendo-snes' ('' for anything else)."""
    path = urlsplit(href).path.rstrip("/")
    if not path.startswith(PLATFORM_PREFIX):
        return ""
    rest = path[len(PLATFORM_PREFIX):]
    if not rest or "/" in rest or rest in NOT_PLATFORMS:
        return ""
    return rest


def track_file(page_url: str) -> str:
    """A track page's last path segment is the file name, encoded twice:
    '.../1-01.%2520Wake%2520Up.mp3' -> '1-01. Wake Up.mp3'."""
    seg = urlsplit(page_url).path.rstrip("/").rsplit("/", 1)[-1]
    return unquote(unquote(seg))


def parse_date(text: str) -> Optional[str]:
    """'Dec 17th, 2025' / 'December 17, 2025' -> '2025-12-17'."""
    t = _ORDINAL.sub(r"\1", (text or "").strip())
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return time.strftime("%Y-%m-%d", time.strptime(t, fmt))
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- platforms
def parse_platforms(body: bytes) -> list:
    """/console-list -> [(slug, name, albums)]."""
    tree = LexborHTMLParser(body)
    root = tree.css_first("#pageContent") or tree.body
    out, seen = [], set()
    if root is None:
        return out
    for a in root.css("a[href]"):
        slug = platform_slug(a.attributes.get("href") or "")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        count = 0
        sib = a.next
        if sib is not None and sib.tag == "-text":
            m = re.match(r"\s*\((\d[\d,]*)\)", sib.text_content or "")
            if m:
                count = int(m.group(1).replace(",", ""))
        out.append((slug, _text(a), count))
    return out


# --------------------------------------------------------------------------- browse pages
def _album_rows(table) -> list:
    """The rows of a table.albumList (cover | name | platforms | type | year) -> album dicts."""
    albums = []
    if table is not None:
        for tr in table.css("tr"):
            tds = [c for c in tr.iter() if c.tag == "td"]
            if len(tds) < 3:
                continue                                   # the header row (th) or a stray row
            # the first cell is the cover (td.albumIcon: an <img>, or an icon whose text is
            # "audiotrack"); the album's name is the first link after it
            link, i = None, 0
            for i, td in enumerate(tds):
                if "albumIcon" in (td.attributes.get("class") or ""):
                    continue
                link = next((a for a in td.css("a[href]")
                             if ALBUM_PREFIX in (a.attributes.get("href") or "") and _text(a)), None)
                if link is not None:
                    break
            if link is None:
                continue
            slug = album_slug(link.attributes["href"])
            if not slug:
                continue
            rest = tds[i + 1:]
            plats = []
            if rest:
                for a in rest[0].css("a[href]"):
                    ps = platform_slug(a.attributes.get("href") or "")
                    if ps:
                        plats.append((ps, _text(a)))
            kind = _text(rest[1]) if len(rest) > 1 else ""
            year = _text(rest[2]) if len(rest) > 2 else ""
            albums.append({"slug": slug, "name": _text(link), "platforms": plats, "type": kind,
                           "year": year if re.fullmatch(r"\d{4}", year) else ""})
    return albums


def _pages(tree) -> int:
    pages = 1
    counter = tree.css_first(".pagination .counter")
    m = re.search(r"of\s+(\d+)", _text(counter)) if counter is not None else None
    if m:
        pages = int(m.group(1))
    else:
        for a in tree.css("a[href]"):
            n = re.search(r"[?&]page=(\d+)", a.attributes.get("href") or "")
            if n:
                pages = max(pages, int(n.group(1)))
    return pages


def parse_browse(body: bytes) -> tuple:
    """A browse page -> ([album dicts], pages, found). Album dict: slug, name, platforms
    [(slug, name)], type, year. `pages`: how many pages this letter has; `found`: albums in it."""
    tree = LexborHTMLParser(body)
    albums = _album_rows(tree.css_first("table.albumList"))
    pages = _pages(tree)
    found = None
    m = re.search(r"Found\s+([\d,]+)\s+albums", tree.body.text() if tree.body else "")
    if m:
        found = int(m.group(1).replace(",", ""))
    return albums, pages, found


def parse_latest(body: bytes) -> tuple:
    """The home page's "Latest Soundtracks" (/?page=N, newest first, one table.albumList per day
    under h3.latestSoundtrackHeading "October 24th, 2026") -> ([(date "2026-10-24", [album dicts])], pages).
    A day can carry on at the top of the next page."""
    tree = LexborHTMLParser(body)
    days = []
    for h in tree.css("h3.latestSoundtrackHeading"):
        date = parse_date(_text(h))
        table = None
        node = h.next
        while node is not None:                  # the day's table follows its heading
            if node.tag == "h3":
                break
            if node.tag == "table" and "albumList" in (node.attributes.get("class") or ""):
                table = node
                break
            found = node.css_first("table.albumList") if node.tag not in ("-text", "-comment") else None
            if found is not None:
                table = found
                break
            node = node.next
        if date:
            days.append((date, _album_rows(table)))
    return days, _pages(tree)


# --------------------------------------------------------------------------- album pages
def _info_html(content) -> str:
    """The album's details paragraph (Platforms / Year / Date Added / Album type ...)."""
    for p in content.css("p"):
        html = p.html or ""
        if "Date Added" in html or "Album type" in html or "Platforms:" in html:
            return html
    return ""


def parse_album(body: bytes, page_url: str) -> dict:
    """An album page -> {slug, missing, title, platforms, year, type, added, formats [ext],
    tracks [{page, file, disc, num, title, duration, sizes {ext: "5.64 MB"}}], images [url]}.
    `missing`: the site answered with its "Ooops!" page (no such album)."""
    tree = LexborHTMLParser(body)
    content = tree.css_first("#pageContent") or tree.body
    out = {"slug": album_slug(page_url), "missing": False, "title": "", "platforms": [], "year": "",
           "type": "", "added": None, "formats": [], "tracks": [], "images": []}
    if content is None:
        out["missing"] = True
        return out
    h2 = content.css_first("h2")
    out["title"] = _text(h2)
    table = content.css_first("table#songlist")
    if out["title"].lower().startswith("ooops") or table is None:
        out["missing"] = True
        return out

    info = _info_html(content)
    if info:
        p = LexborHTMLParser(info)
        plat_part = info.split("Year:", 1)[0] if "Year:" in info else info.split("<br", 1)[0]
        for a in LexborHTMLParser(plat_part).css("a[href]"):
            ps = platform_slug(a.attributes.get("href") or "")
            if ps:
                out["platforms"].append((ps, _text(a)))
        text = _text(p.body) if p.body is not None else ""
        m = re.search(r"Year:\s*(\d{4})", text)
        out["year"] = m.group(1) if m else ""
        m = re.search(r"Date Added:\s*([A-Za-z]+\.?\s+\d{1,2}(?:st|nd|rd|th)?,\s*\d{4})", text)
        out["added"] = parse_date(m.group(1).replace(".", "")) if m else None
        m = re.search(r"Album type:\s*<b>\s*(?:<a[^>]*>)?\s*([^<]+?)\s*<", info)
        out["type"] = m.group(1).strip() if m else ""

    # columns: expand the header by colspan so row cells line up with their labels
    rows = [tr for tr in table.css("tr")]
    head = next((tr for tr in rows if (tr.attributes.get("id") or "") == "songlist_header"), None)
    labels = []
    if head is not None:
        for th in head.css("th"):
            try:
                span = max(1, int(th.attributes.get("colspan") or 1))
            except ValueError:
                span = 1
            labels += [_text(th).upper()] * span
    fmt_cols = [(i, FORMAT_EXT[lab]) for i, lab in enumerate(labels) if lab in FORMAT_EXT]
    out["formats"] = [ext for _, ext in fmt_cols]
    cd_col = labels.index("CD") if "CD" in labels else None
    num_col = labels.index("#") if "#" in labels else None

    for tr in rows:
        if tr is head or (tr.attributes.get("id") or "") == "songlist_footer":
            continue
        tds = [c for c in tr.iter() if c.tag == "td"]
        link = None
        for td in tds:
            a = td.css_first("a[href]")
            if a is not None and ALBUM_PREFIX in (a.attributes.get("href") or "") and _text(a):
                link = a
                break
        if link is None:
            continue
        page = urljoin(page_url, link.attributes["href"])
        cells = [_text(td) for td in tds]
        sizes = {}
        if len(tds) == len(labels):
            for i, ext in fmt_cols:
                if _SIZE.match(cells[i]):
                    sizes[ext] = norm_size(cells[i])
            disc = cells[cd_col] if cd_col is not None else ""
            num = cells[num_col].rstrip(".") if num_col is not None else ""
        else:                                    # unexpected layout: sizes in column order
            found = [c for c in cells if _SIZE.match(c)]
            sizes = {ext: norm_size(found[k]) for k, (_, ext) in enumerate(fmt_cols) if k < len(found)}
            disc = num = ""
        duration = next((c for c in cells if _DURATION.match(c)), "")
        out["tracks"].append({"page": page, "file": track_file(page), "disc": disc, "num": num,
                              "title": _text(link), "duration": duration, "sizes": sizes})

    # the album's own images (covers, scans): full-size links, or the image itself when a gallery
    # has no link; never the "also viewed" albums, comments or avatars further down the page
    for sel in ("#viewed_also", ".viewed-also", "#comments", ".comments", ".useravatar"):
        for n in content.css(sel):
            n.decompose()
    for node in content.css(".albumImage a[href], .albumImages a[href], #albumImage a[href], "
                            ".image-gallery a[href], div.albumImage img[src]"):
        if node.tag == "img" and node.parent is not None and node.parent.tag == "a":
            continue                                 # its link (the full size) counts instead
        raw = node.attributes.get("href") or node.attributes.get("src") or ""
        href = urljoin(page_url, raw.strip())
        if extension(unquote(urlsplit(href).path)) in IMAGE_EXTENSIONS and href not in out["images"]:
            out["images"].append(href)
    return out


# --------------------------------------------------------------------------- track pages
def parse_track(body: bytes, page_url: str) -> dict:
    """A track page -> {extension: download address} for every format it offers."""
    tree = LexborHTMLParser(body)
    out = {}
    content = tree.css_first("#pageContent") or tree.body
    if content is None:
        return out
    links = [a for a in content.css("a[href]") if a.css_first(".songDownloadLink") is not None]
    if not links:                                # layout changed: any link to an audio file
        links = [a for a in content.css("a[href]")]
    for a in links:
        href = urljoin(page_url, a.attributes.get("href") or "")
        if ALBUM_PREFIX in href and urlsplit(href).netloc == urlsplit(page_url).netloc:
            continue                             # a link back to the album or another track page
        ext = extension(unquote(urlsplit(href).path))
        if ext and ext in set(FORMAT_EXT.values()) and ext not in out:
            out[ext] = href
    audio = content.css_first("audio[src]")
    if audio is not None:
        href = urljoin(page_url, audio.attributes["src"])
        ext = extension(unquote(urlsplit(href).path))
        out.setdefault(ext, href)
    return out
