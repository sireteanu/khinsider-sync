"""Syncing: what is downloaded where, resuming, updates, formats, folders, speed."""
import os
import time

from conftest import BIRD, SMW, album, albums, file_status, log_lines, on_disk, scan, select, sync, wait

SMW_MP3 = {"Super Mario World/01. Nintendo Logo.mp3", "Super Mario World/02. Title Screen.mp3",
           "Super Mario World/03. Overworld.mp3"}


def first_sync(c, key="a:" + SMW, settings=None):
    if settings:
        assert c.post("/api/settings", json=settings).status_code == 200
    scan(c)
    select(c, key)
    return sync(c)


def test_downloads_mp3_and_images_into_the_album_folder(site, app_factory):
    c = app_factory(site)
    st = first_sync(c)
    assert on_disk(c.data) == SMW_MP3 | {"Super Mario World/Cover.jpg"}
    assert st["sync"]["new"] == 4 and st["sync"]["failed"] == 0
    with open(c.data / "Super Mario World" / "01. Nintendo Logo.mp3", "rb") as f:
        assert f.read() == site.content(SMW, "01. Nintendo Logo.mp3")
    # one track page per track (for its download link), never for the image
    assert len(site.track_pages()) == 3
    assert all(f["local"] == "new" for f in album(c, SMW)["files"] if f["wanted"])


def test_a_second_sync_downloads_nothing_and_asks_for_nothing(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    site.reset_log()
    st = sync(c)
    assert st["sync"]["albums_done"] == 1 and st["sync"]["new"] == 0 and st["sync"]["total"] == 0
    assert not site.file_requests() and not site.track_pages()
    assert not site.album_pages(), "an album page is read once"


def test_a_downloaded_album_is_never_checked_again(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    a = c.get("/api/albums", params={"show": "downloaded"}).json()["albums"]
    assert [x["slug"] for x in a] == [SMW] and a[0]["downloaded"] and a[0]["downloaded_at"]
    site.albums[SMW]["tracks"].append(("", "4", "04. Castle", "Castle", "2:00"))
    site.modify(SMW, "02. Title Screen.mp3", grow=50_000)
    select(c, "a:" + BIRD)
    site.reset_log()
    st = sync(c)
    assert not [p for p in site.paths() if SMW in p], "nothing about the downloaded album is asked"
    assert st["sync"]["albums_done"] == 1 and st["sync"]["new"] == 3 and st["sync"]["up to date"] == 0
    assert "Super Mario World/04. Castle.mp3" not in on_disk(c.data)
    assert st["selection"]["downloaded"] == 2 and st["selection"]["albums"] == 2


def test_flac_best_and_every_format(site, app_factory):
    c = app_factory(site, settings={"formats": "flac", "album_art": False})
    scan(c)
    select(c, "a:" + SMW)
    select(c, "a:" + BIRD)
    sync(c)
    files = on_disk(c.data)
    assert {f for f in files if f.startswith("Super Mario World/")} == {p.replace(".mp3", ".flac") for p in SMW_MP3}
    assert {f for f in files if f.startswith("A Bird Story/")} == {
        "A Bird Story/A Bird Story (Piano).mp3", "A Bird Story/Yesterday - Kan (Reives) Gao.mp3"}, \
        "no FLAC offered: MP3 instead"
    with open(c.data / "Super Mario World" / "01. Nintendo Logo.flac", "rb") as f:
        assert f.read(4) == b"fLaC"
    c.post("/api/settings", json={"formats": "all"})
    sync(c)
    files = on_disk(c.data)
    assert SMW_MP3 <= files and "A Bird Story/Yesterday - Kan (Reives) Gao.ogg" in files


def test_names_windows_cant_take_are_made_safe(site, app_factory):
    c = app_factory(site)
    first_sync(c, "a:activity-case-01")
    assert on_disk(c.data) == {"_Activity_ Case_01 -Graveyard Memory-/01. Graveyard.mp3"}


def test_resumes_a_dropped_transfer_with_range(site_factory, app_factory):
    site = site_factory("drop")
    c = app_factory(site)
    st = first_sync(c, "a:big-album")
    assert st["sync"]["failed"] == 0
    ranges = [r for p, r in site.file_requests() if "Big%20Track" in p]
    assert ranges[0] is None and any(r and r.startswith("bytes=") for r in ranges[1:])
    with open(c.data / "Big Album" / "01. Big Track.mp3", "rb") as f:
        assert f.read() == site.content("big-album", "01. Big Track.mp3")


def test_stop_keeps_the_part_file_and_the_next_sync_finishes_it(site_factory, app_factory):
    site = site_factory("slowfile")
    c = app_factory(site)
    scan(c)
    select(c, "a:big-album")
    c.post("/api/sync")
    part = c.data / "Big Album" / "01. Big Track.mp3.part"
    t0 = time.time()
    while time.time() - t0 < 20 and not (part.exists() and part.stat().st_size > 100_000):
        time.sleep(0.05)
    c.post("/api/stop")
    st = wait(c)
    assert st["job"]["phase"] == "stopped" and part.exists()
    site.reset_log()
    st = sync(c)
    assert st["sync"]["resumed"] == 1
    assert any(r for p, r in site.file_requests())
    assert on_disk(c.data) == {"Big Album/01. Big Track.mp3"}


def test_reading_an_album_page_by_hand_picks_up_new_tracks(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    site.albums[SMW]["tracks"].append(("", "4", "04. Castle", "Castle", "2:00"))
    assert c.post("/api/album/read", json={"slug": SMW}).status_code == 200
    a = c.get("/api/albums", params={"show": "partly"}).json()["albums"]
    assert [x["slug"] for x in a] == [SMW]
    site.reset_log()
    st = sync(c)
    assert st["sync"]["new"] == 1 and "Super Mario World/04. Castle.mp3" in on_disk(c.data)


def test_check_files_finds_deleted_ones_for_the_next_sync(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    os.remove(c.data / "Super Mario World" / "03. Overworld.mp3")
    assert sync(c)["sync"]["new"] == 0, "a downloaded album isn't looked at by a sync"
    r = c.post("/api/album/check", json={"slug": SMW}).json()
    assert r["missing"] == ["Super Mario World/03. Overworld.mp3"]
    assert not album(c, SMW)["album"]["downloaded"]
    st = sync(c)
    assert st["sync"]["new"] == 1 and "Super Mario World/03. Overworld.mp3" in on_disk(c.data)
    assert c.post("/api/album/check", json={"slug": "nope"}).status_code == 404


def test_check_download_folder_finds_deleted_files_of_every_album(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    select(c, "a:persona-5")
    sync(c)
    os.remove(c.data / "Super Mario World" / "03. Overworld.mp3")
    os.remove(c.data / "PERSONA5 ORIGINAL SOUNDTRACK" / "Cover.jpg")
    site.reset_log()
    assert c.post("/api/check").status_code == 200
    st = wait(c)
    assert not site.requests, "checking the folder only looks at the disk"
    assert st["job"]["kind"] == "check" and "2 of 9 downloaded files are missing (2 albums)" in st["job"]["message"]
    assert not album(c, SMW)["album"]["downloaded"] and not album(c, "persona-5")["album"]["downloaded"]
    assert albums(c, show="partly").keys() == {SMW, "persona-5"}
    st = sync(c)
    assert st["sync"]["new"] == 2
    assert "Super Mario World/03. Overworld.mp3" in on_disk(c.data)
    assert c.post("/api/check").status_code == 200
    assert "all 9 downloaded files are there" in wait(c)["job"]["message"]


def test_upgrading_an_album_replaces_its_old_format(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    up = album(c, SMW)["upgrade"]
    assert up == {"have": [".mp3"], "better": [".flac"], "pending": None}
    assert album(c, "sonic-the-hedgehog-genesis")["upgrade"]["better"] == []
    r = c.post("/api/album/upgrade", json={"slug": SMW, "format": "flac"})
    assert r.status_code == 200 and r.json()["pending"]["from"] == [".mp3"]
    d = album(c, SMW)
    assert d["upgrade"]["pending"]["to"] == ".flac" and d["formats"] == "first:.flac,art"
    assert not d["album"]["downloaded"]
    # meanwhile the MP3s are not offered to Clean up: the sync replaces them
    assert c.get("/api/cleanup").json()["files"] == []
    st = sync(c)
    assert st["sync"]["new"] == 3
    files = on_disk(c.data)
    assert files == {p.replace(".mp3", ".flac") for p in SMW_MP3} | {"Super Mario World/Cover.jpg"}
    d = album(c, SMW)
    assert d["album"]["downloaded"] and d["upgrade"] == {"have": [".flac"], "better": [], "pending": None}
    assert any("Upgrade of Super Mario World done" in m for m in log_lines(st))
    deleted = c.get("/api/changes", params={"kind": "deleted"}).json()["files"]
    assert len(deleted) == 3 and "Replaced by FLAC" in str(deleted)
    # nothing left for Clean up
    assert st["leftovers"]["files"] == 0


def test_an_upgrade_can_be_cancelled_and_refuses_worse_formats(site, app_factory):
    c = app_factory(site, settings={"formats": "all:.flac,art"})
    first_sync(c)
    assert album(c, SMW)["upgrade"]["better"] == []
    c.post("/api/settings", json={"formats": "mp3"})
    assert c.post("/api/album/upgrade", json={"slug": SMW, "format": "mp3"}).status_code == 400
    assert c.post("/api/album/upgrade", json={"slug": "nope", "format": "flac"}).status_code == 404
    c2 = app_factory(site, name="other")
    first_sync(c2)
    assert c2.post("/api/album/upgrade", json={"slug": SMW, "format": "flac"}).status_code == 200
    assert c2.post("/api/album/upgrade", json={"slug": SMW, "format": ""}).json()["pending"] is None
    d = album(c2, SMW)
    assert d["formats"] == "" and d["upgrade"]["pending"] is None and d["album"]["downloaded"]


def test_files_already_in_the_folder_are_adopted(site, app_factory):
    a = app_factory(site, name="one")
    first_sync(a)
    os.remove(a.data / ".khinsider-sync.db")                  # as if copied in by hand
    b = app_factory(site, name="two")
    site.reset_log()
    st = first_sync(b)
    assert st["sync"]["already in folder"] == 4, "the tracks match their listed sizes; the cover is kept too"
    assert not [p for p, _ in site.file_requests() if p.endswith(".mp3")]


def test_files_get_the_albums_date_added(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    t = time.localtime(os.stat(c.data / "Super Mario World" / "01. Nintendo Logo.mp3").st_mtime)
    assert time.strftime("%Y-%m-%d %H:%M", t) == "2025-04-24 12:00"


def test_downloaded_files_and_folders_get_the_permissions_setting(site, app_factory):
    c = app_factory(site, settings={"file_mode": "750"})
    first_sync(c)
    if os.name != "nt":
        assert os.stat(c.data / "Super Mario World" / "01. Nintendo Logo.mp3").st_mode & 0o777 == 0o750
        assert os.stat(c.data / "Super Mario World").st_mode & 0o777 == 0o750


def test_a_web_page_instead_of_a_file_is_not_saved(site_factory, app_factory):
    site = site_factory("limitfile")
    c = app_factory(site)
    st = first_sync(c, "a:big-album")
    assert "Big Album/02. Limited.mp3" not in on_disk(c.data)
    assert st["sync"]["failed"] == 1
    assert file_status(c, "big-album")["02. Limited.mp3"] is None


def test_a_broken_file_fails_after_its_retries(site_factory, app_factory):
    site = site_factory("brokenfile")
    c = app_factory(site)
    st = first_sync(c, "a:big-album")
    assert st["sync"]["failed"] == 1 and st["sync"]["new"] == 1
    assert not any("isn't answering" in m for m in log_lines(st))


def test_daily_limit_pauses_and_the_next_sync_finishes(site, app_factory):
    c = app_factory(site, settings={"parallel": "1", "album_art": False})
    scan(c)
    select(c, "p:windows")
    site.daily_limit = 2
    st = sync(c)
    assert st["sync"]["paused"] and "download limit" in st["sync"]["paused"]
    assert st["sync"]["failed"] == 0
    site.daily_limit = None
    st = sync(c)
    assert not st["sync"]["paused"] and st["sync"]["failed"] == 0
    assert len(on_disk(c.data)) == 6


def test_a_new_folder_layout_moves_what_you_have(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    c.post("/api/settings", json={"folder_layout": "platform"})
    site.reset_log()
    st = sync(c)
    assert on_disk(c.data) == {"SNES/" + p for p in SMW_MP3 | {"Super Mario World/Cover.jpg"}}
    assert not site.file_requests(), "moved, not downloaded again"
    assert any("Moved 4 files" in m for m in log_lines(st))
    assert not (c.data / "Super Mario World").exists(), "the empty old folder is gone"


def test_saved_as_puts_an_album_elsewhere(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    r = c.post("/api/album-place", json={"slug": SMW, "place": "Nintendo/SMW (SNES)"})
    assert r.status_code == 200 and r.json()["dir"] == "Nintendo/SMW (SNES)"
    sync(c)
    assert on_disk(c.data) == {p.replace("Super Mario World/", "Nintendo/SMW (SNES)/")
                               for p in SMW_MP3 | {"Super Mario World/Cover.jpg"}}
    assert c.post("/api/album-place", json={"slug": SMW, "place": "../x"}).status_code == 400
    r = c.post("/api/album-place", json={"slug": SMW, "place": ""})
    assert r.json()["dir"] == "Super Mario World" and r.json()["place"] == ""


def test_an_album_moved_to_a_new_address_keeps_its_tick_and_folder(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    site.rename(SMW, "super-mario-world-snes")
    st = scan(c)
    assert any("moved album" in m for m in log_lines(st))
    assert c.get("/api/albums", params={"show": "ticked"}).json()["albums"][0]["slug"] == "super-mario-world-snes"
    site.reset_log()
    st = sync(c)
    assert st["sync"]["new"] == 0 and st["sync"]["up to date"] + st["sync"]["already in folder"] == 4
    assert not site.file_requests()
    assert on_disk(c.data) == SMW_MP3 | {"Super Mario World/Cover.jpg"}


def test_finds_and_remembers_the_servers_limit(site_factory, app_factory):
    site = site_factory("bulk")
    site.max_conc = 3
    c = app_factory(site, settings={"album_art": False})
    st = first_sync(c, "a:bulk-album")
    assert st["sync"]["failed"] == 0 and st["sync"]["new"] == 40
    assert site.peak_active <= 3
    assert st["sync"]["limit"] <= 3


def test_fixed_number_of_parallel_downloads(site_factory, app_factory):
    site = site_factory("bulk")
    c = app_factory(site, settings={"parallel": "2", "album_art": False})
    first_sync(c, "a:bulk-album")
    assert site.peak_active <= 2


def test_scan_survives_rate_limiting(site_factory, app_factory):
    site = site_factory("throttle")
    c = app_factory(site)
    st = scan(c)
    assert st["catalog"]["albums"] == 7
    assert c.get("/api/status").json()["catalog"]["last_full_scan"]["status"] == "done"


def test_sync_reports_its_size_and_what_is_left(site, app_factory):
    c = app_factory(site)
    st = first_sync(c, "p:windows")
    s = st["sync"]
    assert s["to_fetch"] > 0 and s["bytes_planned"] > 0 and s["files_left"] == 0 and s["bytes_left"] == 0
    sel = st["selection"]
    assert sel["albums"] == 3 and sel["synced"] == sel["files"] and sel["unread"] == 0
    assert sel["synced_bytes"] == sel["bytes"], "nothing is left to download"


def test_every_request_names_khinsider_as_referer(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    sent = [h for p, h in site.requests if p.startswith("/soundtracks/") or "/album/" in p]
    assert sent and all(h.get("Referer") == site.url + "/" for h in sent)


def test_requests_look_like_chrome_unless_turned_off(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    ua = {h.get("User-Agent", "") for p, h in site.requests if "/album/" in p or p.startswith("/soundtracks/")}
    assert ua and all("Chrome/" in u for u in ua), ua
    assert any("Sec-Ch-Ua" in h for p, h in site.requests if "/album/" in p)
    c.post("/api/settings", json={"impersonate": False})
    select(c, "a:" + BIRD)
    site.reset_log()
    sync(c)
    ua = {h.get("User-Agent", "") for p, h in site.requests}
    assert ua and all("khinsider-sync/" in u for u in ua), ua


def test_numbered_track_names_and_an_art_folder(site, app_factory):
    c = app_factory(site, settings={"track_names": "numbered", "art_folder": True})
    scan(c)
    for slug in (SMW, BIRD, "persona-5"):
        select(c, "a:" + slug)
    sync(c)
    files = on_disk(c.data)
    assert "Super Mario World/01 - Nintendo Logo.mp3" in files
    assert "Super Mario World/Art/Cover.jpg" in files
    assert "A Bird Story/01 - A Bird Story (Piano).mp3" in files, "no # column: the place in the list"
    assert "A Bird Story/02 - Yesterday - Kan (Reives) Gao.mp3" in files
    assert "PERSONA5 ORIGINAL SOUNDTRACK/2-01 - Beneath the Mask.mp3" in files
    assert "PERSONA5 ORIGINAL SOUNDTRACK/Art/Alternate Cover.png" in files
    # back to khinsider's names: the next sync renames what you have, downloading nothing
    c.post("/api/settings", json={"track_names": "site", "art_folder": False})
    site.reset_log()
    st = sync(c)
    assert not site.file_requests()
    files = on_disk(c.data)
    assert "Super Mario World/01. Nintendo Logo.mp3" in files and "Super Mario World/Cover.jpg" in files
    assert not (c.data / "Super Mario World" / "Art").exists()
    assert st["sync"]["failed"] == 0


def test_an_album_can_have_its_own_formats(site, app_factory):
    c = app_factory(site, settings={"album_art": False})
    scan(c)
    select(c, "a:" + SMW)
    select(c, "a:" + BIRD)
    r = c.post("/api/album-formats", json={"slug": SMW, "formats": "flac"})
    assert r.status_code == 200 and r.json()["formats"] == "flac"
    assert c.post("/api/album-formats", json={"slug": SMW, "formats": ".exe"}).status_code == 400
    sync(c)
    files = on_disk(c.data)
    assert "Super Mario World/01. Nintendo Logo.flac" in files and "Super Mario World/01. Nintendo Logo.mp3" not in files
    assert "A Bird Story/A Bird Story (Piano).mp3" in files
    d = album(c, SMW)
    assert d["formats"] == "flac" and d["album"]["own_formats"] == "flac"
    # back to the general setting: the FLAC files are no longer part of the sync
    c.post("/api/album-formats", json={"slug": SMW, "formats": ""})
    sync(c)
    left = {f["path"]: f["reason"] for f in c.get("/api/cleanup").json()["files"]}
    assert left.get("Super Mario World/01. Nintendo Logo.flac") == "unticked"


def test_a_format_list_picks_formats_and_their_download_order(site, app_factory):
    c = app_factory(site, settings={"formats": "all:.flac,.mp3", "parallel": "1"})
    s = c.get("/api/settings").json()["settings"]
    assert s["formats"] == "all:.flac,.mp3" and s["album_art"] is False, "no images in the list"
    scan(c)
    select(c, "a:" + SMW)
    select(c, "a:" + BIRD)
    sync(c)
    files = on_disk(c.data)
    assert {f for f in files if f.startswith("Super Mario World/")} == SMW_MP3 | {p.replace(".mp3", ".flac") for p in SMW_MP3}
    assert {f for f in files if f.startswith("A Bird Story/")} == {
        "A Bird Story/A Bird Story (Piano).mp3", "A Bird Story/Yesterday - Kan (Reives) Gao.mp3"}, "no FLAC, no OGG"
    got = [p for p, _ in site.file_requests()]
    last_flac = max(i for i, p in enumerate(got) if p.endswith(".flac"))
    first_mp3 = min(i for i, p in enumerate(got) if p.endswith(".mp3"))
    assert last_flac < first_mp3, "every FLAC file comes before the first MP3"


def test_first_of_a_list_skips_albums_without_any_of_it(site, app_factory):
    c = app_factory(site, settings={"formats": "first:.flac,art"})
    scan(c)
    select(c, "a:" + SMW)
    select(c, "a:" + BIRD)
    st = sync(c)
    files = on_disk(c.data)
    assert "Super Mario World/01. Nintendo Logo.flac" in files and "Super Mario World/Cover.jpg" in files
    assert not any(f.endswith((".mp3", ".ogg")) for f in files)
    assert files & {"A Bird Story/cover.jpg"}, "its images are still on the list"
    assert st["sync"]["failed"] == 0


def test_a_download_record_from_1_0_0_is_taken_over(tmp_path):
    import sqlite3
    from khinsider_sync.core.sync import MANIFEST_NAME, Manifest
    db = sqlite3.connect(tmp_path / MANIFEST_NAME)
    db.execute("CREATE TABLE files (path TEXT PRIMARY KEY, url TEXT, size INTEGER, last_modified TEXT, etag TEXT, "
               "listing_date TEXT, listing_size TEXT, part_validator TEXT, updated REAL)")
    db.execute("INSERT INTO files VALUES ('A/x.mp3', 'u', 5, NULL, NULL, '2025-01-01', '0.01 MB', NULL, 0)")
    db.commit()
    db.close()
    m = Manifest(str(tmp_path))
    rec = m.get("A/x.mp3")
    assert rec.page_date == "2025-01-01" and rec.page_size == "0.01 MB"
    m.close()
