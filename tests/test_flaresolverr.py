"""FlareSolverr as the fallback for Cloudflare's browser check (core/solver.py) - against MockSite's
mock FlareSolverr; nothing here touches the real site or a real FlareSolverr."""
import time

from conftest import BIRD, album, albums, log_lines, on_disk, scan, select, sync, wait

ALL_WINDOWS = 9        # files of the Windows albums (persona-5 and a-bird-story), as test_outage counts them


def solver_settings(site, **more):
    return dict({"flaresolverr_url": site.url + "/flaresolverr"}, **more)


def test_off_by_default_a_check_still_just_stops_the_scan(site, app_factory):
    c = app_factory(site)
    site.challenge = True
    st = scan(c)
    assert "browser check" in st["job"]["message"] and not site.solver_calls


def test_a_scan_gets_past_the_check_with_one_flaresolverr_request(site, app_factory):
    c = app_factory(site, settings=solver_settings(site))
    site.challenge = True
    st = scan(c)
    assert st["job"]["phase"] == "done", st["job"]
    assert c.get("/api/albums").json()["total"] == 7
    assert len(site.solver_calls) == 1, "its cookie carries every later page"
    later = [h for p, h in site.requests if p.startswith("/game-soundtracks/browse/") and "X-Mock-Browser" not in h]
    assert later and all("cf_clearance=" + site.CLEARANCE in h.get("Cookie", "") for h in later[-3:])
    assert all(h.get("User-Agent") == site.SOLVER_UA for h in later[-3:])
    assert any("asking FlareSolverr" in m for m in log_lines(st))


def test_only_one_cookie_header_goes_out(site, app_factory):
    c = app_factory(site, settings=solver_settings(site))
    site.challenge = True
    scan(c)
    for _p, h in site.requests:
        assert h.get("Cookie", "").count("cf_clearance=") <= 1


def test_the_solved_cookie_is_kept_for_the_next_job(site, app_factory):
    c = app_factory(site, settings=solver_settings(site))
    site.challenge = True
    scan(c)
    scan(c)
    assert len(site.solver_calls) == 1


def test_a_sync_downloads_through_the_check(site, app_factory):
    site.player = False                           # downloads need track pages
    c = app_factory(site, settings=solver_settings(site))
    scan(c)
    select(c, "p:windows")
    site.challenge = True
    st = sync(c)
    assert st["sync"]["failed"] == 0 and st["sync"]["new"] == ALL_WINDOWS
    assert len(site.solver_calls) == 1
    assert not any("/soundtracks/" in u for u in site.solver_calls), "files never go through FlareSolverr"


def test_when_its_cookie_doesnt_help_pages_come_from_flaresolverr(site, app_factory):
    site.solver = "html"
    c = app_factory(site, settings=solver_settings(site))
    site.challenge = True
    st = scan(c)
    assert st["job"]["phase"] == "done", st["job"]
    assert c.get("/api/albums").json()["total"] == 7
    assert len(site.solver_calls) > 1
    assert any("come from FlareSolverr itself" in m for m in log_lines(st))
    c.post("/api/album/read", json={"slug": BIRD})
    assert album(c, BIRD)["files"], "album pages read from its copy too"


def test_a_failing_flaresolverr_falls_back_to_waiting(site, app_factory):
    site.solver = "fail"
    site.player = False
    c = app_factory(site, settings=solver_settings(site))
    scan(c)
    select(c, "p:windows")
    c.post("/api/read", json={})
    wait(c)
    site.challenge = True
    st = sync(c)
    assert st["sync"]["failed"] == 0 and not on_disk(c.data)
    assert st["challenge_retries"] == 1 and st["queue_resume_at"] > time.time() + 55
    assert len(site.solver_calls) == 1, "no second attempt right after a failure"


def test_settings_check_and_address_form(site, app_factory):
    c = app_factory(site)
    r = c.post("/api/flaresolverr/check").json()
    assert not r["ok"] and "no FlareSolverr" in r["message"]
    host = site.url.split("://", 1)[1]
    assert c.post("/api/settings", json={"flaresolverr_url": host + "/flaresolverr/v1/"}).status_code == 200
    s = c.get("/api/settings").json()["settings"]
    assert s["flaresolverr_url"] == site.url + "/flaresolverr"
    r = c.post("/api/flaresolverr/check").json()
    assert r["ok"] and r["version"] == "3.3.21"
    assert c.post("/api/settings", json={"flaresolverr_url": "ftp://x"}).status_code == 400
    assert c.post("/api/settings", json={"flaresolverr_url": ""}).status_code == 200


def test_env_variable_is_used_when_the_setting_is_empty(site, app_factory, monkeypatch):
    monkeypatch.setenv("FLARESOLVERR_URL", site.url + "/flaresolverr")
    c = app_factory(site)
    site.challenge = True
    st = scan(c)
    assert st["job"]["phase"] == "done" and len(site.solver_calls) == 1


def test_without_curl_cffi_the_plain_client_works_too(site, app_factory):
    c = app_factory(site, settings=solver_settings(site, impersonate=False))
    site.challenge = True
    st = scan(c)
    assert st["job"]["phase"] == "done" and len(site.solver_calls) == 1
    assert albums(c)


def test_account_pages_come_signed_in_through_flaresolverr(site, app_factory):
    """FlareSolverr's copy of /cp/add_album must be signed in: the client's session cookies go along."""
    from conftest import SMW
    site.accounts["fan"] = ("secret", True)
    c = app_factory(site, settings=solver_settings(site, account_user="fan", account_password="secret"))
    scan(c)
    select(c, "a:" + SMW)
    c.post("/api/read", json={})
    wait(c)
    assert c.post("/api/account/check").json()["state"] in ("ok", "signed_in")   # signed in before the check
    site.challenge = True
    site.solver = "html"                          # every checked page comes from FlareSolverr's copy
    st = sync(c)
    zips = [p for p, _ in site.requests if p.lower().endswith(".zip")]
    assert any("xf_user" in names for names in site.solver_cookies), site.solver_cookies
    assert len(zips) == 1, "the album came as its zip, not file by file"
    assert st["sync"]["failed"] == 0
    assert any("xf_user" in names for names in site.solver_cookies), site.solver_cookies
    assert not any("login page has no login form" in m for m in log_lines(st))


def test_account_cookies_reach_curls_session_through_the_wrapper(site, app_factory):
    """1.0.31 regression: with FlareSolverr set, the account's session cookie went into httpx's jar,
    whose Cookie header ChromeTransport drops - every account page came back signed out."""
    from conftest import SMW
    site.accounts["fan"] = ("secret", True)
    c = app_factory(site, settings=solver_settings(site, account_user="fan", account_password="secret"))
    scan(c)
    select(c, "a:" + SMW)
    st = sync(c)                                  # no browser check at all
    zips = [p for p, _ in site.requests if p.lower().endswith(".zip")]
    assert len(zips) == 1 and st["sync"]["failed"] == 0
    login = max(i for i, (p, _) in enumerate(site.requests) if p.startswith("/forums/index.php"))
    cp = [h for p, h in site.requests[login:] if p.startswith("/cp/")]      # once signed in
    assert cp and all("xf_user=" in h.get("Cookie", "") for h in cp)
    assert all(h.get("Cookie", "").count("xf_user=") == 1 for h in cp), "one Cookie header, one copy"
