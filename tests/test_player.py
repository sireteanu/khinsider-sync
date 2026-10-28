"""MP3 addresses from the album page's music player (parse.player_links): one page per album
instead of one per track; anything that doesn't work falls back to the track page."""
from conftest import BIRD, SMW, album, fixture, log_lines, on_disk, scan, select, sync
from khinsider_sync import parse

SMW_FLAC = 3


def test_real_album_page_player():
    """The real page (October 2026), song list and packed player script exactly as served."""
    body = fixture("album_player_bead.html")
    url = "https://downloads.khinsider.com/game-soundtracks/album/bead-theme-20-2025"
    a = parse.parse_album(body, url)
    assert [(t["songid"], t["file"]) for t in a["tracks"]] == [("4585782", "01. BEA-D Theme 2.0.mp3")]
    assert a["tracks"][0]["mp3"] == ("https://vgmtreasurechest.com/soundtracks/bead-theme-20-2025/aficpezmva/"
                                     "01.%20BEA-D%20Theme%202.0.mp3")


def test_only_this_albums_mp3_files_on_khinsiders_servers():
    good = "https://vgmtreasurechest.com/soundtracks/x/tok/01.%20A.mp3"
    assert parse._good_mp3(good, "x")
    assert parse._good_mp3(good.replace("://", "://lambda."), "x")
    for bad in (good.replace("https", "http"), good.replace("vgmtreasurechest.com", "evil.example"),
                good.replace("/x/", "/other/"), good.replace(".mp3", ".flac"),
                good.replace("tok", ".."), "https://vgmtreasurechest.com.evil.example/soundtracks/x/t/a.mp3"):
        assert not parse._good_mp3(bad, "x"), bad
    assert parse.player_links("<script>var tracks=[{\"songid\":\"1\",\"file\":\"https://evil.example/a.mp3\"}]"
                              "</script>", "x") == {}, "one address that isn't right: none of them"


def test_mp3_needs_no_track_pages(site, app_factory):
    c = app_factory(site, settings={"album_art": False})
    scan(c)
    select(c, "a:" + SMW)
    site.reset_log()
    st = sync(c)
    assert st["sync"]["new"] == 3 and st["sync"]["failed"] == 0
    assert site.track_pages() == []
    assert c.app.state.engine.catalog.history(1)[0]["summary"]["sync"]["player_files"] == 3
    assert any("3 files came with the address from their album page's player" in m for m in log_lines(st))
    with open(c.data / "Super Mario World" / "02. Title Screen.mp3", "rb") as f:
        assert f.read() == site.content(SMW, "02. Title Screen.mp3")


def test_flac_still_reads_its_track_pages(site, app_factory):
    c = app_factory(site, settings={"formats": "all:.flac,.mp3", "album_art": False})
    scan(c)
    select(c, "a:" + SMW)
    st = sync(c)
    assert st["sync"]["new"] == 6 and st["sync"]["failed"] == 0
    assert len(site.track_pages()) == SMW_FLAC, "one per track - for its FLAC; the MP3s came from the player"


def test_a_wrong_player_address_falls_back_to_the_track_page(site, app_factory):
    site.player_broken.add(SMW)
    c = app_factory(site, settings={"album_art": False})
    scan(c)
    select(c, "a:" + SMW)
    st = sync(c)
    assert st["sync"]["new"] == 3 and st["sync"]["failed"] == 0
    assert len(site.track_pages()) == 3
    assert any("needed their track page after all" in m for m in log_lines(st))
    assert len(on_disk(c.data)) == 3


def test_setting_off_reads_track_pages(site, app_factory):
    c = app_factory(site, settings={"album_art": False, "player_links": False})
    scan(c)
    select(c, "a:" + BIRD)
    st = sync(c)
    assert st["sync"]["failed"] == 0 and len(site.track_pages()) == len(site.albums[BIRD]["tracks"])
    assert album(c, BIRD)["files"]


def test_no_player_on_the_page_reads_track_pages(site, app_factory):
    site.player = False
    c = app_factory(site, settings={"album_art": False})
    scan(c)
    select(c, "a:" + SMW)
    st = sync(c)
    assert st["sync"]["new"] == 3 and len(site.track_pages()) == 3
