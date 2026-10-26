"""The web API: catalog, ticks, settings, album pages, the page itself."""
import gzip
import time

from conftest import BIRD, SMW, album, albums, log_lines, on_disk, scan, select, sync, wait


def test_scan_reads_every_album_list_but_no_album_pages_or_files(site, app_factory):
    c = app_factory(site)
    st = scan(c)
    a = albums(c)
    assert set(a) == set(site.albums)
    assert st["catalog"]["albums"] == 7 and st["catalog"]["platforms"] >= 6
    # letter S has three albums on two pages: the second page is read too
    assert any("browse/S?page=2" in p for p in site.paths())
    assert not site.album_pages() and not site.file_requests()
    smw = a[SMW]
    assert smw["platforms"] == ["SNES"] and smw["type"] == "Gamerip" and smw["year"] == "1990"
    assert not smw["read"] and smw["bytes"] == 0
    assert a["activity-case-01"]["platforms"] == []


def test_platform_list_with_counts(site, app_factory):
    c = app_factory(site)
    scan(c)
    d = c.get("/api/platforms").json()
    by = {p["slug"]: p for p in d["platforms"]}
    assert by["nintendo-snes"]["albums"] == 2 and by["windows"]["albums"] == 3
    assert by["nintendo-snes"]["name"] == "SNES" and by["nintendo-snes"]["state"] == "off"
    assert d["no_platform"] == 1
    assert dict(d["types"]) == {"Gamerip": 4, "Soundtrack": 2, "Arrangement": 1}


def test_platform_counts_follow_the_type_filter(site, app_factory):
    c = app_factory(site)
    scan(c)
    d = c.get("/api/platforms", params={"type": "Gamerip"}).json()
    by = {p["slug"]: p["albums"] for p in d["platforms"]}
    assert by["windows"] == 2 and by["nintendo-snes"] == 1 and "playstation-4" not in by
    assert d["total"] == 4 and d["no_platform"] == 0
    assert dict(d["types"])["Soundtrack"] == 2           # the type menu still counts every type
    d = c.get("/api/platforms", params={"type": "Soundtrack"}).json()
    assert d["no_platform"] == 1 and d["total"] == 2
    assert c.get("/api/platforms").json()["total"] == 7


def test_album_list_says_where_each_letter_starts(site, app_factory):
    c = app_factory(site)
    scan(c)
    d = c.get("/api/albums", params={"limit": 2}).json()
    assert d["letters"] == {"#": 0, "A": 1, "B": 2, "P": 3, "S": 4}, "for the whole view, not just this slice"
    names = [a["name"] for a in c.get("/api/albums").json()["albums"]]
    assert names[4] == "Sonic the Hedgehog"
    d = c.get("/api/albums", params={"platform": "nintendo-snes"}).json()
    assert d["letters"] == {"S": 0}
    assert "letters" not in c.get("/api/albums", params={"sort": "year"}).json()


def test_several_album_types_at_once(site, app_factory):
    c = app_factory(site)
    scan(c)
    both = "Soundtrack,Arrangement"
    assert set(albums(c, type=both)) == {"persona-5", "activity-case-01", "super-mario-world-the-jazz-arrangements"}
    d = c.get("/api/platforms", params={"type": both}).json()
    by = {p["slug"]: p["albums"] for p in d["platforms"]}
    assert d["total"] == 3 and d["no_platform"] == 1 and by["nintendo-snes"] == 1 and by["windows"] == 1
    assert c.get("/api/albums", params={"type": "Gamerip, Soundtrack"}).json()["total"] == 6
    assert c.get("/api/albums", params={"type": "Other"}).json()["total"] == 0


def test_album_list_slices_cover_every_album_and_follow_changes(site, app_factory):
    c = app_factory(site)
    scan(c)
    seen = []
    for offset in range(0, 7, 2):
        d = c.get("/api/albums", params={"limit": 2, "offset": offset}).json()
        assert d["total"] == 7 and d["offset"] == offset
        seen += [a["slug"] for a in d["albums"]]
    assert len(seen) == 7 and len(set(seen)) == 7
    names = [a["name"].casefold() for a in c.get("/api/albums").json()["albums"]]
    assert names == sorted(names)
    # a tick changes the next slice (the cached view is not stale)
    c.post("/api/select", json={"key": "a:" + seen[6], "include": True})
    d = c.get("/api/albums", params={"limit": 2, "offset": 6}).json()
    assert d["albums"][0]["state"] == "on"
    assert c.get("/api/albums", params={"show": "ticked"}).json()["total"] == 1


def test_album_list_filters_and_sorts(site, app_factory):
    c = app_factory(site)
    scan(c)
    assert set(albums(c, platform="nintendo-snes")) == {SMW, "super-mario-world-the-jazz-arrangements"}
    assert set(albums(c, platform="-")) == {"activity-case-01"}
    assert set(albums(c, q="mario world")) == {SMW, "super-mario-world-the-jazz-arrangements"}
    assert set(albums(c, type="Soundtrack")) == {"persona-5", "activity-case-01"}
    years = [a["year"] for a in c.get("/api/albums", params={"sort": "year", "dir": -1}).json()["albums"]]
    assert years == sorted(years, reverse=True)
    page = c.get("/api/albums", params={"limit": 3, "offset": 3}).json()
    assert page["total"] == 7 and len(page["albums"]) == 3
    assert c.get("/api/albums", params={"sort": "nope"}).status_code == 400


def test_ticking_a_platform_takes_every_album_and_exceptions(site, app_factory):
    c = app_factory(site)
    scan(c)
    select(c, "p:windows")
    a = albums(c)
    assert {s for s, x in a.items() if x["state"] == "on"} == {BIRD, "persona-5", "big-album"}
    assert a[BIRD]["via"]
    select(c, "a:big-album", False)                     # an exception
    plats = {p["slug"]: p for p in c.get("/api/platforms").json()["platforms"]}
    assert plats["windows"]["state"] == "partial"
    assert albums(c)["big-album"]["state"] == "off" and albums(c)["big-album"]["own"] is False
    select(c, "a:big-album", True)                      # back to what the platform says: no rule left
    assert albums(c)["big-album"]["own"] is None
    select(c, "a:" + SMW)                               # one album of another platform
    plats = {p["slug"]: p for p in c.get("/api/platforms").json()["platforms"]}
    assert plats["nintendo-snes"]["state"] == "partial" and plats["windows"]["state"] == "on"
    select(c, "p:nintendo-snes")                         # ticking the platform folds the album tick in
    assert c.get("/api/status").json()["selection"]["albums"] == 5
    assert set(albums(c, show="ticked")) == {BIRD, "persona-5", "big-album", SMW, "super-mario-world-the-jazz-arrangements"}
    assert c.post("/api/select", json={"key": "p:nope", "include": True}).status_code == 404
    assert c.post("/api/select", json={"key": "nope", "include": True}).status_code == 400
    c.post("/api/selection/clear")
    assert c.get("/api/status").json()["selection"]["albums"] == 0


def test_platform_ticks_can_be_limited_to_album_types(site, app_factory):
    c = app_factory(site, settings={"platform_types": ["Gamerip"]})
    scan(c)
    select(c, "p:nintendo-snes")
    on = {s for s, x in albums(c).items() if x["state"] == "on"}
    assert on == {SMW}, "the arrangement isn't a gamerip"
    select(c, "a:super-mario-world-the-jazz-arrangements")       # but it can still be ticked by itself
    assert albums(c)["super-mario-world-the-jazz-arrangements"]["state"] == "on"


def test_new_albums_of_a_ticked_platform_are_synced_too(site, app_factory):
    c = app_factory(site)
    scan(c)
    select(c, "p:sega-mega-drive-genesis")
    site.albums["streets-of-rage-2-genesis"] = dict(
        name="Streets of Rage 2", platforms=["sega-mega-drive-genesis"], type="Gamerip", year="1992",
        added="Jan 1st, 2025", formats=["MP3"], cd=False, num=True, images=[],
        tracks=[("", "1", "01. Go Straight", "Go Straight", "3:00")])
    scan(c)
    sync(c)
    assert "Streets of Rage 2/01. Go Straight.mp3" in on_disk(c.data)


def test_scan_for_new_albums_reads_only_the_newest(site, app_factory):
    c = app_factory(site)
    scan(c)
    site.add_album("brand-new-ost", "Brand New OST", platforms=["nintendo-snes"])
    site.reset_log()
    assert c.post("/api/scan", json={"quick": True}).status_code == 200
    st = wait(c)
    pages = [p for p, _ in site.requests if not p.startswith("/robots")]
    assert pages == ["/"], "one page of the newest albums, no browse pages, no album pages"
    assert st["job"]["kind"] == "quick" and st["job"]["phase"] == "done"
    assert "brand-new-ost" in albums(c, platform="nintendo-snes")
    assert any("Newest albums done: 1 pages" in m for m in log_lines(st))


def test_scan_for_new_albums_without_a_scan_does_a_full_one(site, app_factory):
    c = app_factory(site)
    assert c.post("/api/scan", json={"quick": True}).status_code == 200
    st = wait(c)
    assert any(p.startswith("/game-soundtracks/browse/") for p, _ in site.requests)
    assert len(albums(c)) == 7
    assert any("No finished scan yet" in m for m in log_lines(st))


def test_scan_for_new_albums_falls_back_to_a_full_scan_when_too_long_ago(site, app_factory):
    import khinsider_sync.scanner as scanner
    c = app_factory(site)
    scan(c)
    site.remove("sonic-the-hedgehog-genesis")       # only a full scan notices this
    old = scanner.Scanner.scan_latest

    async def short(self, name_of, since, max_pages=30):
        return await old(self, name_of, "1900-01-01", max_pages=1)  # not back far enough in one page
    scanner.Scanner.scan_latest = short
    try:
        assert c.post("/api/scan", json={"quick": True}).status_code == 200
        st = wait(c)
    finally:
        scanner.Scanner.scan_latest = old
    assert any("a full scan is needed" in m for m in log_lines(st))
    assert "sonic-the-hedgehog-genesis" not in albums(c)


def test_albums_gone_from_the_site_leave_the_catalog_but_not_the_disk(site, app_factory):
    c = app_factory(site)
    scan(c)
    select(c, "a:sonic-the-hedgehog-genesis")
    sync(c)
    site.remove("sonic-the-hedgehog-genesis")
    st = scan(c)
    assert "sonic-the-hedgehog-genesis" not in albums(c)
    assert any("no longer on the site" in m for m in log_lines(st))
    assert "Sonic the Hedgehog/01. Green Hill Zone.mp3" in on_disk(c.data)


def test_a_scan_with_failed_pages_removes_nothing(site_factory, app_factory):
    site = site_factory()
    c = app_factory(site)
    scan(c)
    site.remove("big-album")
    orig = site.browse
    site.browse = lambda letter, n: (_ for _ in ()).throw(RuntimeError()) if letter == "P" else orig(letter, n)
    st = scan(c)
    assert "big-album" in albums(c), "an incomplete scan never forgets albums"
    assert any("nothing was removed" in m for m in log_lines(st))


def test_folder_names_never_clash(site, app_factory):
    c = app_factory(site)
    scan(c)
    select(c, "p:nintendo-snes")
    sync(c)
    dirs = {p.split("/")[0] for p in on_disk(c.data)}
    assert dirs == {"Super Mario World", "Super Mario World (Arrangement, 1991)"}


def test_opening_an_album_reads_its_page(site, app_factory):
    c = app_factory(site)
    scan(c)
    assert not album(c, "persona-5")["files"]
    r = c.post("/api/album/read", json={"slug": "persona-5"})
    assert r.status_code == 200 and r.json()["read"] == 1
    d = album(c, "persona-5")
    a = d["album"]
    assert a["read"] and a["formats"] == [".mp3", ".flac"] and a["added"] == "2024-06-29"
    tracks = [f for f in d["files"] if f["kind"] == "track"]
    assert len(tracks) == 6 and {f["ext"] for f in tracks} == {".mp3", ".flac"}
    assert [f["disc"] for f in tracks if f["ext"] == ".mp3"] == ["1", "1", "2"]
    assert {f["name"] for f in d["files"] if f["kind"] == "art"} == {"Cover.jpg", "Alternate Cover.png"}
    wanted = {f["name"] for f in d["files"] if f["wanted"]}           # MP3 + images by default
    assert "1-01. Wake Up, Get Up, Get Out There.mp3" in wanted and "1-02. Phantom.flac" not in wanted
    assert d["dir"] == "PERSONA5 ORIGINAL SOUNDTRACK"
    assert not site.file_requests() and not site.track_pages(), "reading a page downloads nothing"
    assert c.post("/api/album/read", json={"slug": "nope"}).status_code == 404


def test_read_ticked_album_pages_as_a_job(site, app_factory):
    c = app_factory(site)
    scan(c)
    assert c.post("/api/read", json={}).status_code == 400            # nothing ticked
    select(c, "p:windows")
    assert c.post("/api/read", json={}).status_code == 200
    st = wait(c)
    assert st["selection"]["unread"] == 0 and st["selection"]["files"] > 0
    assert len(site.album_pages()) == 3


def test_settings_validate_and_persist(site, app_factory):
    c = app_factory(site)
    r = c.post("/api/settings", json={"parallel": "4", "max_rate": "5M", "formats": "best",
                                      "folder_layout": "platform", "platform_types": "Gamerip, Soundtrack"})
    assert r.status_code == 200
    s = c.get("/api/settings").json()["settings"]
    assert (s["parallel"], s["max_rate"], s["formats"], s["folder_layout"]) == ("4", "5M", "best", "platform")
    assert s["platform_types"] == ["Gamerip", "Soundtrack"]
    for bad in ({"max_rate": "fast"}, {"formats": ".exe"}, {"folder_layout": "letters"}, {"file_mode": "999"},
                {"track_names": "fancy"}):
        r = c.post("/api/settings", json=bad)
        assert r.status_code == 400 and r.json()["error"]


def test_download_folder_locked_in_docker(site, app_factory, tmp_path):
    c = app_factory(site, data_locked=True)
    c.post("/api/settings", json={"data_dir": str(tmp_path / "elsewhere")})
    assert c.get("/api/settings").json()["settings"]["data_dir"] == str(c.data)


def test_password_protects_everything_but_the_health_check(site, app_factory):
    c = app_factory(site, password="s3cret")
    assert c.get("/api/status").status_code == 401
    assert c.get("/").status_code == 401
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/status", auth=("anyone", "s3cret")).status_code == 200


def test_interface_is_served_and_asks_for_its_script_by_version(site, app_factory):
    from khinsider_sync import __version__
    c = app_factory(site)
    r = c.get("/")
    assert r.status_code == 200 and "Khinsider Sync" in r.text
    assert f'/static/app.js?v={__version__}' in r.text and r.headers["cache-control"] == "no-cache"
    assert c.get("/static/app.js").status_code == 200 and c.get("/static/app.css").status_code == 200


def test_replies_are_compressed(site, app_factory):
    c = app_factory(site)
    scan(c)
    r = c.get("/api/albums", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"
    assert gzip  # imported for clarity: the client decompresses for us
    assert "visibilitychange" in c.get("/static/app.js").text


def test_unfinished_sync_continues_after_a_restart(site_factory, app_factory):
    site = site_factory("slowfile")
    c = app_factory(site)
    scan(c)
    select(c, "a:big-album")
    c.post("/api/sync")
    t0 = time.time()
    while time.time() - t0 < 20:
        s = c.get("/api/status").json().get("sync") or {}
        if s.get("active"):
            break
        time.sleep(0.05)
    c.__exit__(None, None, None)                       # the app closes in the middle of the sync
    c2 = app_factory(site, name="config")
    t0 = time.time()
    while time.time() - t0 < 30 and not c2.get("/api/status").json()["busy"]:
        time.sleep(0.1)
    st = wait(c2, 60)
    assert st["job"]["kind"] == "sync" and st["sync"]["failed"] == 0
    assert "Big Album/01. Big Track.mp3" in on_disk(c2.data)
    assert any("didn't finish" in m for m in log_lines(st))


def test_stopped_sync_does_not_restart(site, app_factory):
    c = app_factory(site)
    scan(c)
    select(c, "a:" + SMW)
    sync(c)
    assert c.get("/api/settings").json()["settings"]["interrupted_job"] == ""


def test_restart_and_shutdown_endpoints(site, tmp_path):
    from fastapi.testclient import TestClient
    from khinsider_sync.engine import Engine
    from khinsider_sync.web import create_app
    called = []
    app = create_app(lambda: Engine(str(tmp_path / "cfg"), str(tmp_path / "data"), site.url + "/"),
                     on_restart=lambda: called.append("restart"), on_shutdown=lambda: called.append("shutdown"))
    with TestClient(app) as c:
        assert c.post("/api/restart").status_code == 200
        assert c.post("/api/shutdown").status_code == 200
        time.sleep(0.6)
    assert called == ["restart", "shutdown"]


def test_startup_log_names_the_catalog_and_download_folder(site, app_factory):
    c = app_factory(site)
    msgs = log_lines(c.get("/api/status").json())
    assert any("Catalog:" in m and "new, empty catalog" in m for m in msgs)
    assert any(m.startswith("Download folder:") for m in msgs)
