"""Clean up: files in the download folder that aren't part of the sync any more."""
from conftest import BIRD, SMW, on_disk, scan, select, sync, wait


def setup(site, app_factory, **kw):
    c = app_factory(site, **kw)
    scan(c)
    select(c, "a:" + SMW)
    select(c, "a:" + BIRD)
    sync(c)
    return c


def leftovers(c):
    return {f["path"]: f["reason"] for f in c.get("/api/cleanup").json()["files"]}


def test_lists_unticked_removed_and_unneeded_partial_files(site, app_factory):
    c = setup(site, app_factory)
    (c.data / "My own music").mkdir()
    (c.data / "My own music" / "song.mp3").write_bytes(b"ID3 mine")          # not ours: never listed
    (c.data / "Super Mario World" / "notes.txt").write_text("mine too")
    assert leftovers(c) == {}
    select(c, "a:" + BIRD, False)                                               # unticked
    site.albums[SMW]["tracks"].pop()                                            # removed from its album
    c.post("/api/album/read", json={"slug": SMW})                              # ... seen when read again
    (c.data / "A Bird Story" / "Yesterday - Kan (Reives) Gao.ogg.part").write_bytes(b"x" * 10)
    sync(c)
    got = leftovers(c)
    assert got == {
        "A Bird Story/A Bird Story (Piano).mp3": "unticked",
        "A Bird Story/Yesterday - Kan (Reives) Gao.mp3": "unticked",
        "A Bird Story/cover.jpg": "unticked",
        "A Bird Story/Yesterday - Kan (Reives) Gao.ogg.part": "part",
        "Super Mario World/03. Overworld.mp3": "gone",
    }
    st = c.get("/api/status").json()
    assert st["leftovers"]["files"] == 5


def test_images_and_formats_no_longer_synced_are_unticked(site, app_factory):
    c = setup(site, app_factory)
    c.post("/api/settings", json={"album_art": False})
    got = leftovers(c)
    assert got == {"Super Mario World/Cover.jpg": "unticked", "A Bird Story/cover.jpg": "unticked"}


def test_nothing_is_deleted_without_confirmation(site, app_factory):
    c = setup(site, app_factory)
    select(c, "a:" + BIRD, False)
    paths = list(leftovers(c))
    r = c.post("/api/cleanup/delete", json={"paths": paths})
    assert r.status_code == 400
    assert "A Bird Story/cover.jpg" in on_disk(c.data)


def test_deletes_only_confirmed_leftovers_and_empty_folders(site, app_factory):
    c = setup(site, app_factory)
    select(c, "a:" + BIRD, False)
    paths = list(leftovers(c)) + ["Super Mario World/01. Nintendo Logo.mp3", "../outside.txt"]
    r = c.post("/api/cleanup/delete", json={"paths": paths, "confirm": True}).json()
    assert len(r["deleted"]) == 3 and r["skipped"] == 2
    assert not (c.data / "A Bird Story").exists(), "its folder was left empty and removed"
    assert "Super Mario World/01. Nintendo Logo.mp3" in on_disk(c.data)
    assert leftovers(c) == {}
    select(c, "a:" + BIRD, True)                       # ticking it again downloads it again
    st = sync(c)
    assert st["sync"]["new"] == 3


def test_cannot_delete_while_a_sync_runs(site_factory, app_factory):
    site = site_factory("slowfile")
    c = app_factory(site)
    scan(c)
    select(c, "a:big-album")
    c.post("/api/sync")
    r = c.post("/api/cleanup/delete", json={"paths": ["x"], "confirm": True})
    assert r.status_code == 409
    c.post("/api/stop")
    assert wait(c)["job"]["phase"] == "stopped"


def test_clean_up_reports_how_far_it_has_looked(site, app_factory, monkeypatch):
    import threading
    import time
    from khinsider_sync import cleanup
    c = app_factory(site)
    scan(c)
    select(c, "a:" + SMW)
    sync(c)
    real = cleanup.find

    def slow(*a, **k):
        time.sleep(1.0)
        return real(*a, **k)
    monkeypatch.setattr(cleanup, "find", slow)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", c.get("/api/cleanup").json()))
    t.start()
    seen = []
    while t.is_alive():
        st = c.get("/api/status").json()
        if st.get("cleanup"):
            seen.append(st["cleanup"])
        time.sleep(0.1)
    t.join()
    assert seen and seen[-1]["files"] == 4 and seen[-1]["expected"] == 4 and seen[-1]["step"] == "judging"
    assert "cleanup" not in c.get("/api/status").json(), "gone once it has finished"
    assert out["r"]["files"] == []


def test_delete_an_album_from_the_drive_and_the_queue(site, app_factory):
    from conftest import BIRD, albums, on_disk, scan, select, sync
    c = app_factory(site, settings={"folder_art": True})
    scan(c)
    select(c, "p:windows")                        # ticked by its platform
    sync(c)
    smw_free = [f for f in on_disk(c.data) if f.startswith("A Bird Story/")]
    assert smw_free and "A Bird Story/folder.jpg" in on_disk(c.data)
    (c.data / "A Bird Story" / "my notes.txt").write_text("mine")
    before = on_disk(c.data)
    preview = c.post("/api/album/delete", json={"slug": BIRD}).json()
    assert preview["files"] == len(smw_free) and preview["deleted"] == 0
    assert on_disk(c.data) == before, "nothing deleted without confirm"
    r = c.post("/api/album/delete", json={"slug": BIRD, "confirm": True}).json()
    assert r["deleted"] == len(smw_free) and not r["errors"]
    left = on_disk(c.data)
    assert [f for f in left if f.startswith("A Bird Story/")] == ["A Bird Story/my notes.txt"], "other files stay"
    assert any(f.startswith("PERSONA5") for f in left), "other albums untouched"
    assert BIRD not in albums(c, show="ticked"), "off the queue, although its platform is ticked"
    site.reset_log()
    st = sync(c)
    assert not [p for p in site.file_requests() if "a-bird-story" in p[0]], "unticked: not downloaded again"
    assert st["sync"]["failed"] == 0
    assert c.post("/api/album/delete", json={"slug": "no-such-album", "confirm": True}).status_code == 404


def test_deleting_an_album_with_nothing_saved_deletes_nothing(site, app_factory):
    from conftest import SMW, scan
    c = app_factory(site)
    scan(c)
    r = c.post("/api/album/delete", json={"slug": SMW, "confirm": True}).json()
    assert r["files"] == 0 and r["deleted"] == 0
