"""What changed: the Activity counters and the Activity log."""
from conftest import BIRD, SMW, scan, select, sync


def test_each_counter_lists_its_files(site_factory, app_factory):
    site = site_factory("brokenfile")
    c = app_factory(site)
    scan(c)
    select(c, "a:" + SMW)
    select(c, "a:big-album")
    sync(c)
    new = c.get("/api/sync/files", params={"kind": "new"}).json()
    assert new["total"] == 5 and "Super Mario World/Cover.jpg" in {f["path"] for f in new["files"]}
    failed = c.get("/api/sync/files", params={"kind": "failed"}).json()
    assert [f["path"] for f in failed["files"]] == ["Big Album/03. Broken.mp3"] and failed["files"][0]["note"]
    sync(c)
    up = c.get("/api/sync/files", params={"kind": "uptodate"}).json()
    assert up["total"] == 5
    assert c.get("/api/sync/files", params={"kind": "nope"}).status_code == 400


def test_change_history_by_day_includes_clean_up(site, app_factory):
    c = app_factory(site)
    scan(c)
    select(c, "a:" + BIRD)
    sync(c)
    select(c, "a:" + BIRD, False)
    paths = [f["path"] for f in c.get("/api/cleanup").json()["files"]]
    c.post("/api/cleanup/delete", json={"paths": paths, "confirm": True})
    days = c.get("/api/changes/days").json()["days"]
    assert len(days) == 1 and days[0]["counts"] == {"new": 3, "deleted": 3}
    rows = c.get("/api/changes", params={"day": days[0]["day"]}).json()
    assert rows["total"] == 6
    assert {r["kind"] for r in rows["files"]} == {"new", "deleted"}
