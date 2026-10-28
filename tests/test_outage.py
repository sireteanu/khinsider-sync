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


def test_a_cloudflare_browser_check_stops_a_scan_at_once(site, app_factory):
    c = app_factory(site)
    scan(c)
    site.challenge = True
    site.reset_log()
    st = scan(c)
    pages = [p for p, _ in site.requests if not p.startswith("/robots")]
    assert len(pages) <= 5, "stops instead of asking for all the other pages"
    msgs = log_lines(st)
    assert sum("asks this network for a browser check" in m for m in msgs) == 1
    assert "browser check" in st["job"]["message"]
    site.challenge = False
    r = c.get("/api/albums").json()
    assert r["total"] == 7, "an incomplete scan removes nothing"


def test_a_browser_check_pauses_a_sync_without_failing_files(site, app_factory):
    site.player = False                           # downloads need track pages
    c = app_factory(site)
    scan(c)
    select(c, "p:windows")
    c.post("/api/read", json={})
    wait(c)
    site.challenge = True                        # track pages answer the check, files would not
    st = sync(c)
    assert st["sync"]["failed"] == 0 and not on_disk(c.data)
    assert "browser check" in (st["sync"]["paused"] or "")
    site.challenge = False
    st = sync(c)
    assert st["sync"]["new"] == ALL_WINDOWS and st["sync"]["failed"] == 0


def test_unread_albums_are_not_marked_failed_by_a_browser_check(site, app_factory):
    c = app_factory(site)
    scan(c)
    select(c, "a:persona-5")
    site.challenge = True
    st = sync(c)
    assert "browser check" in st["job"]["message"]
    a = c.get("/api/album", params={"slug": "persona-5"}).json()["album"]
    assert not a["error"], "the album itself is fine"


def test_robots_txt_sent_as_html_is_still_respected(site, app_factory):
    site.robots_extra = "\r\nDisallow: /game-soundtracks/browse/B"
    c = app_factory(site)
    scan(c)
    assert not [p for p, _ in site.requests if p.startswith("/game-soundtracks/browse/B")]



def test_robots_txt_is_not_asked_for_in_front_of_page_requests(site, app_factory):
    """A session opening with robots.txt got Cloudflare's browser check on the album page that
    followed (owner's server, 1.0.15) - robots.txt now comes in its own session, once a day."""
    c = app_factory(site)
    scan(c)
    select(c, "a:persona-5")
    sync(c)
    sync(c)
    robots = [p for p, _ in site.requests if p == "/robots.txt"]
    assert len(robots) == 1, "kept for a day"


def test_a_browser_check_waits_and_tries_again_by_itself(site, app_factory):
    """A browser check pauses the sync and it tries again by itself: 30 min, then 1 h, 2 h ... up
    to 8 h while it keeps happening; once pages come through again, back to the start."""
    c = app_factory(site, settings={"album_art": False})
    eng = c.app.state.engine
    scan(c)
    select(c, "p:windows")
    site.challenge = True
    t0 = time.time()
    st = sync(c)                                   # the album page gets the check
    assert 1790 < st["queue_resume_at"] - t0 < 1810 and st["challenge_retries"] == 1
    assert "trying again at" in st["job"]["message"]
    assert any("Paused for 30 minutes because of khinsider's browser check" in m for m in log_lines(st))
    assert not eng.resume_queue(), "not before its time"
    waits = []
    for _ in range(5):                             # still checked: the wait doubles, up to 8 hours
        eng.catalog.save_settings({"queue_resume_at": time.time() - 1})
        assert c.portal.call(eng.resume_queue)
        st = wait(c)
        waits.append(round((st["queue_resume_at"] - time.time()) / 1800))
    assert waits == [2, 4, 8, 16, 16], waits
    assert any("Trying again after khinsider's browser check" in m for m in log_lines(st))
    site.challenge = False                         # through again: downloads, and the count starts over
    eng.catalog.save_settings({"queue_resume_at": time.time() - 1})
    assert c.portal.call(eng.resume_queue)
    st = wait(c)
    assert st["challenge_retries"] == 0 and not st["queue_resume_at"]
    assert st["sync"]["new"] and st["sync"]["failed"] == 0
    assert len({f.split("/")[0] for f in on_disk(c.data)}) == 3


def test_a_browser_check_on_track_pages_also_waits(site, app_factory):
    site.player = False
    c = app_factory(site, settings={"album_art": False})
    scan(c)
    select(c, "p:windows")
    c.post("/api/read", json={})
    wait(c)
    site.challenge = True                          # album pages read; track pages get the check
    st = sync(c)
    assert st["challenge_retries"] == 1 and st["queue_resume_at"] > time.time() + 1700
    assert st["sync"]["failed"] == 0


def test_stop_doesnt_set_a_wait(site, app_factory):
    c = app_factory(site)
    scan(c)
    select(c, "a:persona-5")
    st = sync(c)
    assert not st["queue_resume_at"] and st["challenge_retries"] == 0
