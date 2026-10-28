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
    # the MP3 addresses come from the album page's player: no track page at all
    assert len(site.track_pages()) == 0
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
    with open(c.data / "Super Mario World" / "02. Title Screen.mp3", "r+b") as f:   # cut short
        f.truncate(1000)
    site.reset_log()
    assert c.post("/api/check").status_code == 200
    st = wait(c)
    assert not site.requests, "checking the folder only looks at the disk"
    assert st["job"]["kind"] == "check" and "3 of 9 downloaded files are missing or cut short (2 albums)" in st["job"]["message"]
    assert not album(c, SMW)["album"]["downloaded"] and not album(c, "persona-5")["album"]["downloaded"]
    assert albums(c, show="partly").keys() == {SMW, "persona-5"}
    st = sync(c)
    assert st["sync"]["new"] == 2 and st["sync"]["resumed"] == 1
    assert "Super Mario World/03. Overworld.mp3" in on_disk(c.data)
    assert os.path.getsize(c.data / "Super Mario World" / "02. Title Screen.mp3") > 1000
    assert c.post("/api/check").status_code == 200
    assert "all 9 downloaded files are there" in wait(c)["job"]["message"]


def test_check_download_folder_reports_its_progress(site, app_factory, monkeypatch):
    import khinsider_sync.engine as engine_mod
    c = app_factory(site)
    first_sync(c)
    real = engine_mod.file_intact

    def slow(root, rel, size):
        time.sleep(0.3)
        return real(root, rel, size)
    monkeypatch.setattr(engine_mod, "file_intact", slow)
    os.remove(c.data / "Super Mario World" / "01. Nintendo Logo.mp3")
    assert c.post("/api/check").status_code == 200
    seen = []
    for _ in range(60):
        st = c.get("/api/status").json()
        if not st["busy"]:
            break
        if st.get("check"):
            seen.append(st["check"])
        time.sleep(0.1)
    assert seen and seen[0]["total"] == 4
    assert any(0 < p["done"] < 4 for p in seen), "the count moves while it runs"
    assert seen[-1]["missing"] >= 1 and "eta" in seen[-1]
    assert "check" not in c.get("/api/status").json(), "gone once it has finished"


def test_upgrading_an_album_replaces_its_old_format(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    up = album(c, SMW)["upgrade"]
    assert up == {"have": [".mp3"], "better": [".flac"], "pending": None}
    assert album(c, "sonic-the-hedgehog-genesis")["upgrade"]["better"] == []
    r = c.post("/api/album/upgrade", json={"slug": SMW, "format": "flac"})
    assert r.status_code == 200 and r.json()["pending"]["from"] == [".mp3"]
    d = album(c, SMW)
    assert d["upgrade"]["pending"]["to"] == ".flac" and d["formats"] == "first:.flac,.mp3,art"
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


def test_sync_one_album_only(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    select(c, "a:persona-5")
    assert c.post("/api/sync", json={"albums": ["sonic-the-hedgehog-genesis"]}).status_code == 200
    st = wait(c)
    assert st["sync"]["albums"] == 1 and st["sync"]["new"] == 1
    files = on_disk(c.data)
    assert "Sonic the Hedgehog/01. Green Hill Zone.mp3" in files, "synced and ticked"
    assert not any(f.startswith("PERSONA5") for f in files), "other ticked albums wait for a full sync"
    assert albums(c, show="ticked").keys() == {SMW, "persona-5", "sonic-the-hedgehog-genesis"}
    assert c.post("/api/sync", json={"albums": ["nope"]}).status_code == 404


def test_replace_old_formats_after_a_format_change(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    select(c, "a:" + BIRD)
    sync(c)
    r = c.post("/api/settings", json={"formats": "first:.flac,.mp3,art", "replace_formats": True})
    assert r.status_code == 200 and r.json()["settings"]["replace_formats"] is True
    assert c.get("/api/cleanup").json()["files"] == [], "the MP3s wait for their replacement"
    st = sync(c)
    files = on_disk(c.data)
    assert {f for f in files if f.startswith("Super Mario World/")} == \
        {p.replace(".mp3", ".flac") for p in SMW_MP3} | {"Super Mario World/Cover.jpg"}
    assert "A Bird Story/A Bird Story (Piano).mp3" in files, "no FLAC on the site: its MP3s stay"
    assert any("New format of Super Mario World: deleted 3 MP3 files" in m for m in log_lines(st))
    deleted = c.get("/api/changes", params={"kind": "deleted"}).json()["files"]
    assert len(deleted) == 3 and "Replaced by FLAC" in str(deleted)
    assert st["leftovers"]["files"] == 0


def test_without_replace_old_formats_the_old_ones_go_to_clean_up(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    c.post("/api/settings", json={"formats": "first:.flac,.mp3,art"})
    st = sync(c)
    assert SMW_MP3 <= on_disk(c.data)
    assert st["leftovers"]["files"] == 3


def scheduled_run(c, kind="quick+sync"):
    """What the scheduler starts when a scheduled sync is due."""
    e = c.app.state.engine
    assert c.portal.call(lambda: e.start(kind, scheduled=True))
    return wait(c)


def test_scheduled_runs_check_the_folder_only_when_asked(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    os.remove(c.data / "Super Mario World" / "03. Overworld.mp3")
    st = scheduled_run(c)
    assert "check" not in str(st["job"]) and not any("Scheduled check" in m for m in log_lines(st))
    assert "Super Mario World/03. Overworld.mp3" not in on_disk(c.data), "the disks were left alone"
    assert c.post("/api/settings", json={"check_folder_days": 7}).status_code == 200
    st = scheduled_run(c)
    assert any("Scheduled check of the download folder" in m for m in log_lines(st))
    assert "Super Mario World/03. Overworld.mp3" in on_disk(c.data), "found missing, downloaded again"
    os.remove(c.data / "Super Mario World" / "03. Overworld.mp3")
    st = scheduled_run(c)
    assert "Super Mario World/03. Overworld.mp3" not in on_disk(c.data), "checked a week ago: not again yet"


def test_playlists_and_folder_images(site, app_factory):
    c = app_factory(site, settings={"playlists": True, "folder_art": True})
    st = first_sync(c)
    m3u = c.data / "Super Mario World" / "Super Mario World.m3u8"
    assert m3u.read_text(encoding="utf-8").splitlines() == [
        "#EXTM3U", "#PLAYLIST:Super Mario World",
        "#EXTINF:2,Nintendo Logo", "01. Nintendo Logo.mp3",
        "#EXTINF:46,Title Screen", "02. Title Screen.mp3",
        "#EXTINF:167,Overworld", "03. Overworld.mp3"]
    folder = c.data / "Super Mario World" / "folder.jpg"
    assert folder.read_bytes() == (c.data / "Super Mario World" / "Cover.jpg").read_bytes()
    assert any("1 playlists and 1 folder images written" in m for m in log_lines(st))
    before = sum("folder images written" in m for m in log_lines(st))
    st = sync(c)
    assert sum("folder images written" in m for m in log_lines(st)) == before, "unchanged: not written again"
    # a new folder layout moves them along with the music
    c.post("/api/settings", json={"folder_layout": "platform"})
    sync(c)
    files = on_disk(c.data)
    assert "SNES/Super Mario World/Super Mario World.m3u8" in files and "SNES/Super Mario World/folder.jpg" in files
    assert not (c.data / "Super Mario World").exists()
    # unticked: Clean up lists them with the music
    select(c, "a:" + SMW, False)
    left = {f["path"] for f in c.get("/api/cleanup").json()["files"]}
    assert "SNES/Super Mario World/folder.jpg" in left and "SNES/Super Mario World/Super Mario World.m3u8" in left
    r = c.post("/api/cleanup/delete", json={"paths": sorted(left), "confirm": True})
    assert r.status_code == 200 and not (c.data / "SNES").exists()


def test_folder_image_is_khinsiders_cover_the_first_image(site, app_factory):
    site.add_album("odd-names-ost", "Odd Names OST", images=["SLPS-03515.jpg", "Back Cover.jpg"])
    c = app_factory(site, settings={"folder_art": True})
    first_sync(c, "a:odd-names-ost")
    folder = c.data / "Odd Names OST" / "folder.jpg"
    assert folder.read_bytes() == (c.data / "Odd Names OST" / "SLPS-03515.jpg").read_bytes(), \
        "the first image, whatever its name - not the one called 'cover'"


def test_folder_image_without_syncing_images(site, app_factory):
    c = app_factory(site, settings={"folder_art": True, "album_art": False})
    first_sync(c, "a:persona-5")
    files = on_disk(c.data)
    assert "PERSONA5 ORIGINAL SOUNDTRACK/folder.jpg" in files, "fetched on its own"
    assert "PERSONA5 ORIGINAL SOUNDTRACK/Cover.jpg" not in files, "images still aren't synced"
    assert (c.data / "PERSONA5 ORIGINAL SOUNDTRACK" / "folder.jpg").read_bytes()[:3] == b"\xff\xd8\xff"
    site.reset_log()
    sync(c)
    assert not site.file_requests(), "fetched once, not at every sync"


def test_a_png_cover_becomes_a_real_folder_jpg(site, app_factory):
    from PIL import Image
    site.add_album("png-cover-ost", "PNG Cover OST", images=["BTD3.png", "other.jpg"])
    c = app_factory(site, settings={"folder_art": True})
    first_sync(c, "a:png-cover-ost")
    files = on_disk(c.data)
    assert "PNG Cover OST/folder.jpg" in files and "PNG Cover OST/folder.png" not in files
    with Image.open(c.data / "PNG Cover OST" / "folder.jpg") as im:
        assert im.format == "JPEG" and im.size == (4, 4)
        assert im.getpixel((3, 0))[0] < 30, "see-through parts on black"


def test_without_pillow_a_png_cover_stays_folder_png(site, app_factory, monkeypatch):
    from khinsider_sync import extras
    monkeypatch.setattr(extras, "can_convert", lambda: False)
    site.add_album("png-cover-ost", "PNG Cover OST", images=["BTD3.png"])
    c = app_factory(site, settings={"folder_art": True})
    first_sync(c, "a:png-cover-ost")
    assert "PNG Cover OST/folder.png" in on_disk(c.data)
    monkeypatch.setattr(extras, "can_convert", lambda: True)       # Pillow installed later
    sync(c)
    files = on_disk(c.data)
    assert "PNG Cover OST/folder.jpg" in files and "PNG Cover OST/folder.png" not in files


def pictures(path):
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3
    if str(path).endswith(".flac"):
        return [p.data for p in FLAC(path).pictures]
    return [f.data for f in ID3(path).getall("APIC")]


def test_cover_embedded_into_mp3_files(site, app_factory):
    c = app_factory(site, settings={"embed_cover": True})
    st = first_sync(c)
    album_dir = c.data / "Super Mario World"
    cover = (album_dir / "Cover.jpg").read_bytes()
    for name in ("01. Nintendo Logo.mp3", "02. Title Screen.mp3", "03. Overworld.mp3"):
        assert pictures(album_dir / name) == [cover]
    assert any("cover put into 3 files (0 already had one)" in m for m in log_lines(st))
    t = time.localtime(os.stat(album_dir / "01. Nintendo Logo.mp3").st_mtime)
    assert time.strftime("%Y-%m-%d %H:%M", t) == "2025-04-24 12:00", "the album's date stays"
    # the new sizes are remembered: a check doesn't take tagged files for damaged ones
    assert c.post("/api/check").status_code == 200
    assert "all 4 downloaded files are there" in wait(c)["job"]["message"]
    before = sum("Music files:" in m for m in log_lines(st))
    site.reset_log()
    st = sync(c)
    assert sum("Music files:" in m for m in log_lines(st)) == before and not site.file_requests(), "each file once"


def test_a_picture_already_in_the_file_is_kept(site, app_factory):
    from mutagen.id3 import APIC, ID3
    c = app_factory(site)
    first_sync(c)
    album_dir = c.data / "Super Mario World"
    tags = ID3(album_dir / "01. Nintendo Logo.mp3")
    tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="theirs", data=b"\xff\xd8\xff theirs"))
    tags.save()
    c.post("/api/settings", json={"embed_cover": True})
    st = sync(c)
    assert pictures(album_dir / "01. Nintendo Logo.mp3") == [b"\xff\xd8\xff theirs"]
    assert pictures(album_dir / "02. Title Screen.mp3") == [(album_dir / "Cover.jpg").read_bytes()]
    assert any("cover put into 2 files (1 already had one)" in m for m in log_lines(st))


def test_cover_embedded_into_flac_without_syncing_images(site, app_factory):
    c = app_factory(site, settings={"embed_cover": True, "formats": "first:.flac"})
    first_sync(c)
    files = on_disk(c.data)
    assert not any(f.endswith(".jpg") for f in files), "images still aren't synced"
    pics = pictures(c.data / "Super Mario World" / "01. Nintendo Logo.flac")
    assert len(pics) == 1 and pics[0][:3] == b"\xff\xd8\xff"


def test_missing_tags_are_filled_in_and_existing_ones_kept(site, app_factory):
    from mutagen.id3 import ID3, TIT2
    c = app_factory(site)
    first_sync(c, "a:persona-5")
    d = c.data / "PERSONA5 ORIGINAL SOUNDTRACK"
    t = ID3(d / "1-02. Phantom.mp3")
    t.add(TIT2(encoding=3, text="My own title"))
    t.save()
    c.post("/api/settings", json={"write_tags": True})
    st = sync(c)
    t = ID3(d / "1-01. Wake Up, Get Up, Get Out There.mp3")
    assert str(t["TIT2"]) == "Wake Up, Get Up, Get Out There"
    assert str(t["TALB"]) == "PERSONA5 ORIGINAL SOUNDTRACK"
    assert str(t["TRCK"]) == "1/2" and str(t["TPOS"]) == "1/2" and str(t["TDRC"]) == "2017"
    assert str(ID3(d / "2-01. Beneath the Mask.mp3")["TRCK"]) == "1/1"
    t = ID3(d / "1-02. Phantom.mp3")
    assert str(t["TIT2"]) == "My own title" and str(t["TRCK"]) == "2/2", "kept; only the missing ones added"
    assert any("missing tags filled in for 3 files" in m for m in log_lines(st))
    assert c.post("/api/check").status_code == 200
    assert "all 5 downloaded files are there" in wait(c)["job"]["message"]


def test_tags_for_flac_and_tracks_without_numbers(site, app_factory):
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3
    c = app_factory(site, settings={"write_tags": True, "embed_cover": True, "formats": "all:.mp3,.flac,art"})
    first_sync(c)
    f = FLAC(c.data / "Super Mario World" / "03. Overworld.flac")
    assert f["title"] == ["Overworld"] and f["tracknumber"] == ["3"] and f["tracktotal"] == ["3"]
    assert f["date"] == ["1990"] and len(f.pictures) == 1
    select(c, "a:" + BIRD)
    sync(c)
    t = ID3(c.data / "A Bird Story" / "Yesterday - Kan (Reives) Gao.mp3")     # no # column on its page
    assert str(t["TRCK"]) == "2/2" and str(t["TIT2"]) == "Yesterday - Kan (Reives) Gao"


def test_covers_and_tags_also_for_albums_with_a_failed_file(site, app_factory):
    """One broken file (here a scan image that answers 404) must not keep the rest of the album
    without its folder.jpg, embedded cover and tags."""
    site.broken_images.add("Alternate Cover.png")
    c = app_factory(site, settings={"folder_art": True, "embed_cover": True, "write_tags": True, "playlists": True})
    st = first_sync(c, "a:persona-5")
    assert st["sync"]["failed"] == 1
    d = c.data / "PERSONA5 ORIGINAL SOUNDTRACK"
    assert (d / "folder.jpg").exists()
    assert (d / "PERSONA5 ORIGINAL SOUNDTRACK.m3u8").exists()
    assert pictures(d / "1-01. Wake Up, Get Up, Get Out There.mp3") == [(d / "Cover.jpg").read_bytes()]


def test_covers_and_tags_also_after_a_paused_sync(site, app_factory):
    c = app_factory(site, settings={"parallel": "1", "album_art": False, "folder_art": True, "embed_cover": True})
    scan(c)
    select(c, "p:windows")
    site.daily_limit = 2                      # khinsider's download limit: the sync pauses
    st = sync(c)
    assert st["sync"]["paused"] and st["sync"]["failed"] == 0
    got = [f for f in on_disk(c.data) if f.endswith(".mp3")]
    assert len(got) == 2, "two tracks came down before the pause"
    for f in got:
        assert pictures(c.data / f), "what arrived has its cover"
        assert (c.data / f).parent.joinpath("folder.jpg").exists()


def test_a_deleted_album_gets_its_folder_image_back(site, app_factory):
    import shutil
    c = app_factory(site, settings={"folder_art": True, "album_art": False, "playlists": True})
    first_sync(c)
    d = c.data / "Super Mario World"
    assert (d / "folder.jpg").exists()
    shutil.rmtree(d)                          # the album's folder deleted by hand
    assert c.post("/api/check").status_code == 200
    wait(c)
    sync(c)
    assert (d / "folder.jpg").exists() and (d / "Super Mario World.m3u8").exists()
    # only the folder image deleted: a sync leaves downloaded albums alone, a check finds it
    os.remove(d / "folder.jpg")
    sync(c)
    assert not (d / "folder.jpg").exists()
    r = c.post("/api/album/check", json={"slug": SMW}).json()
    assert r["missing"] == ["Super Mario World/folder.jpg"]
    sync(c)
    assert (d / "folder.jpg").exists()


def test_folder_images_never_replace_your_own_and_go_when_turned_off(site, app_factory):
    c = app_factory(site, settings={"folder_art": True, "playlists": True})
    os.makedirs(c.data / "Super Mario World")
    (c.data / "Super Mario World" / "folder.jpg").write_bytes(b"mine")
    first_sync(c)
    assert (c.data / "Super Mario World" / "folder.jpg").read_bytes() == b"mine"
    c.post("/api/settings", json={"folder_art": False, "playlists": False})
    sync(c)
    assert not (c.data / "Super Mario World" / "Super Mario World.m3u8").exists()
    assert (c.data / "Super Mario World" / "folder.jpg").read_bytes() == b"mine"


def test_an_album_khinsider_replaced_is_read_again_and_updated(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    # khinsider puts up another version: a track changes, the album shows up among the newest
    site.modify(SMW, "02. Title Screen.mp3", grow=500)
    site.albums[SMW]["added"] = time.strftime("%b %d, %Y").replace(" 0", " ")
    assert c.post("/api/scan", json={"quick": True}).status_code == 200
    st = wait(c)
    assert any("khinsider replaced 1 albums with another version (Super Mario World)" in m for m in log_lines(st))
    assert albums(c)[SMW]["changed"]
    st = sync(c)
    assert st["sync"]["updated"] == 4, "a new version: every file again (its Date Added changed)"
    assert os.path.getsize(c.data / "Super Mario World" / "02. Title Screen.mp3") > 30_000
    d = album(c, SMW)["album"]
    assert d["downloaded"] and not d["changed"]
    site.reset_log()
    assert sync(c)["sync"]["albums_done"] == 1 and not site.file_requests(), "done again"


def test_a_full_scan_also_notices_replaced_albums(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    site.albums[SMW]["added"] = time.strftime("%b %d, %Y").replace(" 0", " ")
    scan(c)
    assert albums(c)[SMW]["changed"]


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


def test_a_moved_album_keeps_its_saved_as_and_own_formats(site, app_factory):
    c = app_factory(site)
    first_sync(c)
    assert c.post("/api/album-place", json={"slug": SMW, "place": "Faves/SMW"}).status_code == 200
    assert c.post("/api/album-formats", json={"slug": SMW, "formats": "first:.mp3,art"}).status_code == 200
    sync(c)
    site.rename(SMW, "super-mario-world-snes")
    scan(c)
    d = album(c, "super-mario-world-snes")
    assert d["place"] == "Faves/SMW" and d["formats"] == "first:.mp3,art"
    site.reset_log()
    st = sync(c)
    assert st["sync"]["new"] == 0 and not site.file_requests()
    assert on_disk(c.data) == {p.replace("Super Mario World/", "Faves/SMW/") for p in SMW_MP3 | {"Super Mario World/Cover.jpg"}}


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


def test_a_title_ending_in_its_extension_isnt_named_twice(site, app_factory, monkeypatch):
    """khinsider titles like "01 Title.mp3" made "01 - 01 Title.mp3.mp3" (fixed 1.0.32); files saved
    under the old name are renamed by the next sync, not downloaded again."""
    from khinsider_sync import engine as eng_mod
    from khinsider_sync.engine import track_name
    assert track_name({"title": "01 Title.mp3", "num": "1", "ext": ".mp3"}) == "01 - 01 Title.mp3"
    assert track_name({"title": "Song.MP3", "num": "2", "ext": ".flac"}) == "02 - Song.flac"
    assert track_name({"title": "v1.5", "num": "3", "ext": ".mp3"}) == "03 - v1.5.mp3"
    site.albums[SMW]["tracks"][1] = ("", "2", "02. Title Screen", "Title Screen.mp3", "0:46")
    real = eng_mod.track_name
    monkeypatch.setattr(eng_mod, "track_name",                       # how 1.0.31 named it
                        lambda f: eng_mod.safe_segment(f"{f['num'].zfill(2)} - {f['title']}{f['ext']}")
                        if f.get("title") else real(f))
    c = app_factory(site, settings={"track_names": "numbered"})
    scan(c)
    select(c, "a:" + SMW)
    sync(c)
    assert "Super Mario World/02 - Title Screen.mp3.mp3" in on_disk(c.data)
    c.__exit__(None, None, None)
    monkeypatch.setattr(eng_mod, "track_name", real)                 # the new version starts
    c2 = app_factory(site, name="config")
    site.reset_log()
    st = sync(c2)
    files = on_disk(c2.data)
    assert "Super Mario World/02 - Title Screen.mp3" in files
    assert "Super Mario World/02 - Title Screen.mp3.mp3" not in files
    assert not site.file_requests(), "renamed, not downloaded again"
    assert st["sync"]["failed"] == 0
