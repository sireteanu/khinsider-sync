"""The page readers against copies of khinsider's real markup (tests/fixtures, October 2026)."""
import pytest

from conftest import fixture
from khinsider_sync import parse
from khinsider_sync.catalog import wanted_exts, parse_formats

SITE = "https://downloads.khinsider.com"


def test_browse_page():
    albums, pages, found = parse.parse_browse(fixture("browse_A_page2.html"))
    assert pages == 12 and found == 5905
    assert [a["slug"] for a in albums] == [
        "accel-world-vs-sword-art-online-millennium-twilight-soundtrack-2017",
        "a-bbs-intro-demo-amiga-gamerip-1991",
        "a-bird-story-linux-macos-windows-gamerip-2014"]
    bbs = albums[1]
    # its cover cell holds an icon whose text is "audiotrack" - never taken for the name
    assert bbs["name"] == "A BBS Intro (Demo)" and bbs["platforms"] == [("amiga", "Amiga")]
    assert bbs["type"] == "Gamerip" and bbs["year"] == "1991"
    assert albums[2]["platforms"] == [("linux", "Linux"), ("mac-os", "MacOS"), ("steam", "Steam"),
                                      ("windows", "Windows")]
    # the "Top 12" grid above the list is not part of the letter's albums
    assert "animal-crossing-new-horizons-2020-switch-gamerip" not in {a["slug"] for a in albums}


def test_home_page_latest_albums_by_day():
    days, pages = parse.parse_latest(fixture("home_latest.html"))
    assert pages == 617
    assert [d for d, _ in days] == ["2026-10-24", "2026-10-23"]
    first = days[0][1]
    assert [a["slug"] for a in first] == ["crimson-desert-original-soundtrack-volume-one-2026",
                                          "fate-strange-fake-original-soundtrack-2026"]
    assert first[0]["name"] == "CRIMSON DESERT ORIGINAL SOUNDTRACK VOLUME ONE"
    assert first[0]["platforms"] == [("playstation-5", "PS5"), ("windows", "Windows"),
                                     ("xbox-series-x", "Xbox Series X/S")]
    assert first[0]["type"] == "Soundtrack" and first[0]["year"] == "2026"
    assert [a["slug"] for a in days[1][1]] == ["and-yet-it-moves-2011"]


def test_album_page_with_cd_and_track_numbers():
    a = parse.parse_album(fixture("album_persona-5.html"), f"{SITE}/game-soundtracks/album/persona-5")
    assert not a["missing"] and a["slug"] == "persona-5"
    assert a["title"] == "PERSONA5 ORIGINAL SOUNDTRACK"
    assert [p for p, _ in a["platforms"]] == ["playstation-3", "playstation-4", "nintendo-switch", "windows",
                                               "xbox-one"]
    assert (a["year"], a["type"], a["added"]) == ("2017", "Soundtrack", "2024-06-29")
    assert a["formats"] == [".mp3", ".flac"]
    t = a["tracks"]
    assert len(t) == 3
    assert t[0]["file"] == "1-01. Wake Up, Get Up, Get Out There.mp3"
    assert (t[0]["disc"], t[0]["num"], t[0]["duration"]) == ("1", "1", "4:38")
    assert t[0]["sizes"] == {".mp3": "9.22 MB", ".flac": "33.45 MB"}
    assert (t[2]["disc"], t[2]["num"], t[2]["title"]) == ("3", "31", "Hoshi To Bokura To")
    assert t[0]["page"].startswith(f"{SITE}/game-soundtracks/album/persona-5/1-01.%2520Wake")
    assert a["images"] == ["https://jetta.vgmtreasurechest.com/soundtracks/persona-5/Alternate%20Cover.png",
                           "https://jetta.vgmtreasurechest.com/soundtracks/persona-5/Cover.jpg"]


def test_missing_album_is_recognised():
    a = parse.parse_album(fixture("album_missing.html"), f"{SITE}/game-soundtracks/album/nope")
    assert a["missing"] and not a["tracks"]


def test_track_page_links_per_format():
    links = parse.parse_track(fixture("track_a-bird-story.html"),
                              f"{SITE}/game-soundtracks/album/a-bird-story-linux-macos-windows-gamerip-2014/x.mp3")
    base = "https://lambda.vgmtreasurechest.com/soundtracks/a-bird-story-linux-macos-windows-gamerip-2014/xdvqfqdnks/"
    assert links == {".mp3": base + "A%20Bird%20Story%20%28Piano%29.mp3",
                     ".ogg": base + "A%20Bird%20Story%20%28Piano%29.ogg"}


def test_platform_list():
    plats = parse.parse_platforms(fixture("console_list.html"))
    assert plats[0] == ("3do", "3DO", 171)
    assert ("sega-mega-drive-genesis", "Genesis / Mega Drive", 1717) in plats
    # the left column's links (Gamerips, Full Platform List) are not platforms
    assert "gamerips" not in {p for p, _, _ in plats}


@pytest.mark.parametrize("text, out", [("Dec 17th, 2025", "2025-12-17"), ("Jun 29th, 2024", "2024-06-29"),
                                       ("May 1st, 2020", "2020-05-01"), ("Apr 22nd, 2019", "2019-04-22"),
                                       ("Aug 3rd, 2018", "2018-08-03"), ("nonsense", None)])
def test_dates(text, out):
    assert parse.parse_date(text) == out


def test_names_and_sizes():
    assert parse.track_file("/game-soundtracks/album/x/1-01.%2520Wake%2520Up%252C%2520Get.mp3") == "1-01. Wake Up, Get.mp3"
    assert parse.album_slug(f"{SITE}/game-soundtracks/album/persona-5/1-01.%2520a.mp3") == "persona-5"
    assert parse.platform_slug("/game-soundtracks/nintendo-snes") == "nintendo-snes"
    assert parse.platform_slug("/game-soundtracks/gamerips") == ""
    assert parse.platform_slug("/game-soundtracks/publisher/sega") == ""
    assert parse.norm_size("1,438 MB") == "1438 MB" and parse.norm_size("5.64 MB") == "5.64 MB"


@pytest.mark.parametrize("setting, have, out", [
    ("mp3", [".mp3", ".flac"], {".mp3"}),
    ("flac", [".mp3", ".flac"], {".flac"}),
    ("flac", [".mp3", ".ogg"], {".mp3"}),
    ("best", [".mp3", ".ogg"], {".ogg"}),        # khi-explorer's order: OGG before MP3
    ("best", [".mp3", ".m4a", ".ogg"], {".m4a"}),
    ("best", [".mp3"], {".mp3"}),
    ("best", [".mp3", ".flac"], {".flac"}),
    ("mp3", [".ogg"], {".ogg"}),                 # nothing left out when an album has no MP3
    ("all", [".mp3", ".flac", ".ogg"], {".mp3", ".flac", ".ogg"}),
    (".ogg, .flac", [".mp3", ".ogg"], {".ogg"}),
])
def test_which_formats_are_synced(setting, have, out):
    assert wanted_exts(setting, have, False) == out
    assert wanted_exts(setting, have, True) == out | {"art"}


def test_formats_setting_is_checked():
    for ok in ("mp3", "flac", "best", "all", ".mp3,.ogg", "flac, mp3"):
        parse_formats(ok)
    for bad in ("", ".exe", "everything"):
        with pytest.raises(ValueError):
            parse_formats(bad)


def test_album_images_from_any_gallery_but_not_comments_or_other_albums():
    page = b"""<html><body><div id="pageContent"><h2>X</h2>
    <p align="left">Platforms: <a href="/game-soundtracks/windows">Windows</a><br>Year: <b>2020</b><br>
    Date Added: <b>Jan 1st, 2025</b><br>Album type: <b><a href="/game-soundtracks/ost">Soundtrack</a></b></p>
    <div class="albumImage"><a href="https://cdn.example/s/x/Front.jpg"><img src="https://cdn.example/s/x/thumbs/Front.jpg"></a></div>
    <div class="albumImage"><img src="https://cdn.example/s/x/Back.webp"></div>
    <table id="songlist"><tr id="songlist_header"><th></th><th colspan="2">Song Name</th><th>MP3</th></tr>
    <tr><td></td><td class="clickable-row"><a href="/game-soundtracks/album/x/a.mp3">a</a></td><td>1:00</td><td>1.00 MB</td></tr></table>
    <div id="comments"><div class="albumImage"><a href="https://cdn.example/avatar.png">me</a></div></div>
    <div class="useravatar"><img src="https://cdn.example/u.jpg"></div>
    </div></body></html>"""
    a = parse.parse_album(page, f"{SITE}/game-soundtracks/album/x")
    assert a["images"] == ["https://cdn.example/s/x/Front.jpg", "https://cdn.example/s/x/Back.webp"]


@pytest.mark.parametrize("setting, have, out", [
    ("first:.flac,.mp3", [".mp3", ".flac"], [".flac"]),
    ("first:.flac,.mp3", [".mp3", ".ogg"], [".mp3"]),
    ("first:.flac,.mp3", [".ogg"], []),                        # only these two: nothing else
    ("all:.flac,.mp3", [".mp3", ".flac", ".ogg"], [".flac", ".mp3"]),
    ("all:.mp3,art,.flac", [".mp3", ".flac"], [".mp3", "art", ".flac"]),
    ("first:art,.ogg,.mp3", [".mp3", ".ogg"], ["art", ".ogg"]),
    (".mp3, .ogg", [".ogg", ".mp3"], [".mp3", ".ogg", "art"]),  # older form: images as album_art says
])
def test_format_lists_keep_their_order(setting, have, out):
    from khinsider_sync.catalog import format_plan
    assert format_plan(setting, have, art=True) == out


def test_format_lists_are_checked():
    for bad in ("first:", "first:art", "sideways:.mp3", "all:.exe"):
        with pytest.raises(ValueError):
            parse_formats(bad)
    parse_formats("first:.flac,.mp3,images")
