"""The site going down during a sync: downloads wait for it instead of failing."""
import threading
import time

from conftest import log_lines, on_disk, scan, select, sync, wait

ALL_WINDOWS = 9           # tracks and images of the three Windows albums (MP3 + images)


def start_sync(c):
    scan(c)
    select(c, "p:windows")
    c.post("/api/read", json={})                 # read the album pages before the site goes down
    wait(c)
    assert c.post("/api/sync").status_code == 200


def test_downloads_wait_for_the_site_and_carry_on(site, app_factory):
    c = app_factory(site, settings={"parallel": "1"})
    site.down_after = 2                                    # goes down after two files
    start_sync(c)
    seen = None
    t0 = time.time()
    while time.time() - t0 < 30 and seen is None:
        st = c.get("/api/status").json()
        if st.get("sync") and st["sync"].get("outage"):
            seen = st["sync"]["outage"]
        time.sleep(0.1)
    assert seen and "522" in seen["error"] and seen["gives_up"] > seen["since"]
    time.sleep(1)
    site.down = None                                        # back after a while
    st = wait(c)
    assert len(on_disk(c.data)) == ALL_WINDOWS
    assert st["sync"]["failed"] == 0 and not st["sync"]["paused"]
    msgs = log_lines(st)
    assert any("isn't answering" in m for m in msgs) and any("is back" in m for m in msgs)


def test_gives_up_after_the_wait_and_the_next_sync_continues(site, app_factory):
    c = app_factory(site, settings={"outage_wait_hours": 0.0005, "parallel": "1"})     # ~2 s
    site.down_after = 1
    start_sync(c)
    st = wait(c)
    assert st["sync"]["failed"] == 0, "nothing is marked failed because of the site"
    assert "has not answered" in st["sync"]["paused"]
    assert len(on_disk(c.data)) < ALL_WINDOWS
    site.down = None
    st = sync(c)
    assert len(on_disk(c.data)) == ALL_WINDOWS and st["sync"]["failed"] == 0


def test_waiting_can_be_stopped(site, app_factory):
    c = app_factory(site)
    site.down_after = 0
    start_sync(c)
    t0 = time.time()
    while time.time() - t0 < 30:
        s = c.get("/api/status").json().get("sync") or {}
        if s.get("outage"):
            break
        time.sleep(0.1)
    threading.Timer(0.5, lambda: c.post("/api/stop")).start()
    st = wait(c, 20)
    assert st["job"]["phase"] == "stopped" and st["sync"]["failed"] == 0
