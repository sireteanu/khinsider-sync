"""A track the album's format is missing for (an album in FLAC with a few tracks as MP3 only):
it comes in the next format the setting allows, instead of being left out (format_fallback)."""
from conftest import SMW, album, albums, on_disk, scan, select, sync
from khinsider_sync.catalog import format_fallback


def music(c):
    return sorted(f.rsplit(".", 1)[1] for f in on_disk(c.data) if f.endswith((".mp3", ".flac")))


def setup(site, app_factory, formats, **kw):
    site.albums[SMW]["lacks"] = {"02. Title Screen": ["FLAC"]}
    c = app_factory(site, settings=dict({"formats": formats, "album_art": False}, **kw))
    scan(c)
    select(c, "a:" + SMW)
    return c


def test_format_fallback():
    assert format_fallback("flac", [".mp3", ".flac"]) == (".flac", [".mp3"])
    assert format_fallback("first:.flac,.mp3,art", [".mp3", ".flac"]) == (".flac", [".mp3"])
    assert format_fallback("first:.flac,art", [".mp3", ".flac"]) == (".flac", []), "only what the list names"
    assert format_fallback("mp3", [".mp3", ".flac"]) == (".mp3", [".flac"])
    assert format_fallback("all", [".mp3", ".flac"]) == (None, [])
    assert format_fallback("all:.flac,.mp3", [".mp3", ".flac"]) == (None, [])


def test_a_track_missing_in_flac_comes_as_mp3(site, app_factory):
    c = setup(site, app_factory, "flac", playlists=True)
    st = sync(c)
    assert music(c) == ["flac", "flac", "mp3"], "3 tracks: 2 FLAC, the one without FLAC as MP3"
    assert st["sync"]["failed"] == 0
    a = albums(c)[SMW]
    assert a["downloaded"], "the album counts as complete"
    files = album(c, SMW)["files"]
    stand = [f for f in files if f["stand_in"]]
    assert [(f["name"].rsplit(".", 1)[1], f["wanted"]) for f in stand] == [("mp3", True)]
    assert c.get("/api/cleanup").json()["files"] == [], "the stand-in isn't a leftover"
    pl = next(f for f in on_disk(c.data) if f.endswith(".m3u8"))
    lines = [x for x in (c.data / pl).read_text(encoding="utf-8").splitlines() if not x.startswith("#")]
    assert len(lines) == 3 and sum(x.endswith(".mp3") for x in lines) == 1, lines
    st = sync(c)
    assert st["sync"]["albums"] == 0, "complete: left alone"


def test_a_list_without_mp3_leaves_it_out(site, app_factory):
    c = setup(site, app_factory, "first:.flac")
    sync(c)
    assert music(c) == ["flac", "flac"]


def test_upgrade_keeps_the_tracks_that_have_no_flac(site, app_factory):
    c = setup(site, app_factory, "mp3")
    sync(c)
    assert music(c) == ["mp3", "mp3", "mp3"]
    r = c.post("/api/album/upgrade", json={"slug": SMW, "format": ".flac"})
    assert r.status_code == 200, r.text
    sync(c)
    assert music(c) == ["flac", "flac", "mp3"], "the MP3 of the track without FLAC stays"


def test_newest_release_first(site, app_factory):
    c = app_factory(site)
    scan(c)
    got = c.get("/api/albums", params={"sort": "year", "dir": -1, "limit": 1000}).json()["albums"]
    years = [a["year"] for a in got if a["year"]]
    assert years == sorted(years, reverse=True) and years[0] > years[-1]
    assert all(a["year"] for a in got[:len(years)]), "albums without a year come last"
    same = [a["name"].casefold() for a in got if a["year"] == got[0]["year"]]
    assert same == sorted(same), "within a year by name (no Date Added known before a read)"
    old = c.get("/api/albums", params={"sort": "year", "dir": 1, "limit": 1000}).json()["albums"]
    assert [a["year"] for a in old if a["year"]] == sorted(years)
    assert not old[-1]["year"] or all(a["year"] for a in old), "no year: last here too"
