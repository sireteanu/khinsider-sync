"""Going easy on khinsider (pacing.py): page reads a minute, files an hour, albums a day."""
import asyncio
import time

from conftest import SMW, log_lines, on_disk, scan, select, sync, wait
from khinsider_sync.pacing import PageRate, Window


def album_page_requests(site):
    return [p for p, _ in site.requests if p.startswith("/game-soundtracks/album/") and p.count("/") == 3]


def test_one_album_at_a_time_is_the_default():
    from khinsider_sync.catalog import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["albums_per_batch"] == 1 and DEFAULT_SETTINGS["pages_per_minute"] == 60


def test_page_rate_spaces_requests():
    async def run():
        rate = PageRate(600)                      # one every 0.1 s
        t0 = time.monotonic()
        for _ in range(6):
            await rate.wait()
        return time.monotonic() - t0
    assert asyncio.run(run()) >= 0.45
    assert asyncio.run(PageRate(0).wait()) is None


def test_window():
    w = Window(2, 3600, earlier=[time.time() - 4000, time.time() - 100])   # one fell out of the hour
    assert w.room() == 1 and w.take() and not w.take()
    assert 3400 < w.free_at() - time.time() <= 3500
    assert Window(0, 3600).take() and Window(0, 3600).room() > 10 ** 6


def test_albums_a_day(site, app_factory):
    c = app_factory(site, settings={"albums_per_day": 2, "album_art": False})
    scan(c)
    select(c, "p:windows")                        # 3 albums
    site.reset_log()
    st = sync(c)
    assert len(album_page_requests(site)) == 2, "only the albums it downloads now get their page read"
    assert st["sync"]["albums"] == 2
    assert any("Albums per day: 1 albums wait for later (limit 2 a day)" in m for m in log_lines(st))
    assert st["queue_resume_at"] and 86000 < st["queue_resume_at"] - time.time() <= 86400
    assert st["sync"]["albums_held"] == 1 and st["sync"]["held_until"] == st["queue_resume_at"]
    assert "khinsider asks" not in str(st["job"])
    # the same day: nothing new starts
    st = sync(c)
    assert st["sync"]["albums"] == 0
    # a day later
    c.app.state.engine.catalog.db.execute("UPDATE quota SET t=t-86500")
    st = sync(c)
    assert st["sync"]["albums"] == 1 and not st["queue_resume_at"]
    assert st["sync"]["albums_later"] == 0
    assert len({f.split("/")[0] for f in on_disk(c.data)}) == 3


def test_files_an_hour_and_carrying_on_by_itself(site, app_factory):
    c = app_factory(site, settings={"files_per_hour": 3})
    scan(c)
    select(c, "a:" + SMW)                         # 3 tracks and a cover
    st = sync(c)
    assert st["sync"]["new"] == 3 and "files an hour" in st["sync"]["paused"]
    assert 3500 < st["queue_resume_at"] - time.time() <= 3600
    e = c.app.state.engine
    assert not c.portal.call(e.resume_queue), "not before the hour is over"
    e.catalog.db.execute("UPDATE quota SET t=t-3700")
    e.catalog.save_settings({"queue_resume_at": time.time() - 1})
    assert c.portal.call(e.resume_queue)
    st = wait(c)
    assert st["sync"]["new"] == 1 and not st["sync"]["paused"] and not st["queue_resume_at"]
    assert any("Carrying on with the queue" in m for m in log_lines(st))


def test_stop_doesnt_carry_on(site, app_factory):
    c = app_factory(site, settings={"files_per_hour": 1})
    scan(c)
    select(c, "a:" + SMW)
    st = sync(c)
    assert st["queue_resume_at"]
    c.app.state.engine.catalog.save_settings({"queue_resume_at": 0})
    assert not c.portal.call(c.app.state.engine.resume_queue)


def test_pages_a_minute_slows_page_reads(site, app_factory):
    c = app_factory(site, settings={"pages_per_minute": 120})   # one every 0.5 s
    t0 = time.monotonic()
    scan(c)                                        # the platform list + 27 letters + 2nd S page ~ 30 pages
    took = time.monotonic() - t0
    assert took >= 12, took


def test_a_long_list_is_done_in_batches(site_factory, app_factory):
    site = site_factory()
    c = app_factory(site, settings={"album_art": False, "albums_per_batch": 2})
    scan(c)
    select(c, "p:windows")                        # 3 albums, none read yet
    site.reset_log()
    st = sync(c)
    assert any("3 ticked albums haven't been read yet: this sync does 2 of them" in m for m in log_lines(st))
    # two pages, their downloads, then the third page - in the same job, no gap and no second run
    paths = [p for p, _ in site.requests]
    pages = album_page_requests(site)
    assert len(pages) == 3
    first_track = next(i for i, p in enumerate(paths) if p.startswith("/game-soundtracks/album/") and p.count("/") == 4)
    assert paths.index(pages[1]) < first_track < paths.index(pages[2])
    assert not st["queue_resume_at"]
    last = c.app.state.engine.catalog.history(1)[0]
    assert last["summary"]["sync"]["batches"] == 2 and last["summary"]["sync"]["albums"] == 3
    assert len({f.split("/")[0] for f in on_disk(c.data)}) == 3


def test_one_album_at_a_time(site, app_factory):
    c = app_factory(site, settings={"album_art": False, "albums_per_batch": 1})
    scan(c)
    select(c, "p:windows")
    site.reset_log()
    st = sync(c)
    assert any("this sync does 1 of them" in m for m in log_lines(st))
    # page, its downloads, next page, its downloads ...: never two album pages in a row
    kinds = ["page" if p.count("/") == 3 else "track" for p, _ in site.requests
             if p.startswith("/game-soundtracks/album/")]
    assert kinds.count("page") == 3
    assert all(not (a == b == "page") for a, b in zip(kinds, kinds[1:])), kinds


def test_a_new_page_rate_applies_at_once():
    async def run():
        rate = PageRate(6)                        # one every 10 s
        await rate.wait()
        rate.set(600)                             # Settings changed while a sync runs
        t0 = time.monotonic()
        for _ in range(3):
            await rate.wait()
        return time.monotonic() - t0
    assert asyncio.run(run()) < 1


def test_settings_change_the_running_page_rate(site, app_factory):
    c = app_factory(site)
    eng = c.app.state.engine
    c.post("/api/settings", json={"pages_per_minute": 30})
    assert eng.page_rate.interval == 2.0
    c.post("/api/settings", json={"pages_per_minute": 0})
    assert eng.page_rate.interval == 0
