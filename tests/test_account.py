"""A khinsider account (account.py): a paid one gets whole albums as zips; without one, or once it
may not any more, syncs go file by file. Pages as on the live site: tests/fixtures/forum_login.html,
cp_add_album*.html, cp_downloads.html."""
from conftest import SMW, album, fixture, log_lines, on_disk, scan, select, sync
from khinsider_sync import parse

SMW_FILES = {"Super Mario World/01. Nintendo Logo.mp3", "Super Mario World/02. Title Screen.mp3",
             "Super Mario World/03. Overworld.mp3", "Super Mario World/Cover.jpg"}


def account(site, app_factory, paid=True, password="secret", **settings):
    site.accounts["fan"] = ("secret", paid)
    c = app_factory(site, settings=dict({"account_user": "fan", "account_password": password}, **settings))
    scan(c)
    select(c, "a:" + SMW)
    site.reset_log()
    return c


def zip_requests(site):
    return [p for p, _ in site.requests if p.lower().endswith(".zip")]


def track_files(site):
    return [p for p, _ in site.requests if p.startswith("/soundtracks/") and p.count("/") == 4
            and not p.lower().endswith(".zip")]


# ------------------------------------------------------------------ the live pages
def test_real_pages():
    f = parse.login_form(fixture("forum_login.html"))
    assert f["action"] == "/forums/index.php?login/login" and set(f["fields"]) == {"_xfToken", "_xfRedirect"}
    ok = parse.add_album_reply(fixture("cp_add_album.html"))
    assert ok["signed_in"] and ok["zip"].endswith("/pqpezv/BEA-D%20Theme%202.0%20%282025%29.zip")
    out = parse.add_album_reply(fixture("cp_add_album_signed_out.html"))
    assert not out["signed_in"] and not out["zip"]
    lst = parse.mass_downloads(fixture("cp_downloads.html"))
    assert lst["aphelion-original-soundtrack-2026"]["albumid"] == 109836
    assert set(lst["aphelion-original-soundtrack-2026"]["zips"]) == {".mp3", ".flac"}
    assert set(lst["bead-theme-20-2025"]["zips"]) == {".mp3"}
    a = parse.parse_album(fixture("album_player_bead.html"),
                          "https://downloads.khinsider.com/game-soundtracks/album/bead-theme-20-2025")
    assert a["albumid"] == 115636


# ------------------------------------------------------------------ syncs
def test_a_paid_account_gets_the_album_as_one_zip(site, app_factory):
    c = account(site, app_factory)
    st = sync(c)
    assert on_disk(c.data) == SMW_FILES
    with open(c.data / "Super Mario World" / "02. Title Screen.mp3", "rb") as f:
        assert f.read() == site.content(SMW, "02. Title Screen.mp3")
    assert len(zip_requests(site)) == 1 and track_files(site) == [], "one zip, no file one by one"
    assert st["sync"]["new"] == 4 and st["sync"]["failed"] == 0
    assert site.mass["fan"] == {}, "taken off My Mass Downloads again"
    assert st["account"]["state"] == "ok" and st["account"]["user"] == "fan"
    assert any("1 albums came as zips from your khinsider account" in m for m in log_lines(st))
    assert all(f["local"] == "new" for f in album(c, SMW)["files"] if f["wanted"])
    assert not (c.data / ".khinsider-sync-zips").exists() or not list((c.data / ".khinsider-sync-zips").iterdir())


def test_flac_comes_from_the_list(site, app_factory):
    c = account(site, app_factory, formats="flac")
    st = sync(c)
    assert {f for f in on_disk(c.data) if f.endswith(".flac")} == {
        "Super Mario World/01. Nintendo Logo.flac", "Super Mario World/02. Title Screen.flac",
        "Super Mario World/03. Overworld.flac"}
    assert [z for z in zip_requests(site) if "(FLAC)" in z or "%28FLAC%29" in z]
    assert st["sync"]["failed"] == 0 and track_files(site) == []


def test_signed_in_once_then_the_saved_session(site, app_factory):
    c = account(site, app_factory)
    sync(c)
    logins = [p for p, _ in site.requests if "login/login" in p]
    assert len(logins) == 1
    select(c, "p:windows")
    site.reset_log()
    sync(c)
    assert not [p for p, _ in site.requests if "login" in p], "the saved session, no new login"
    assert zip_requests(site)


def test_an_account_without_mass_downloads_goes_file_by_file(site, app_factory):
    """The subscription ended: the site offers no zip - the app notices and downloads file by file."""
    c = account(site, app_factory, paid=False)
    st = sync(c)
    assert on_disk(c.data) == SMW_FILES and st["sync"]["failed"] == 0
    assert zip_requests(site) == []
    assert st["account"]["state"] == "no_mass"
    assert any("can't download whole albums now" in m for m in log_lines(st))


def test_a_wrong_password_goes_file_by_file(site, app_factory):
    c = account(site, app_factory, password="wrong")
    st = sync(c)
    assert on_disk(c.data) == SMW_FILES and st["sync"]["failed"] == 0
    assert st["account"]["state"] == "failed"


def test_a_broken_zip_falls_back(site, app_factory):
    site.zip_broken.add(SMW)
    c = account(site, app_factory)
    st = sync(c)
    assert on_disk(c.data) == SMW_FILES and st["sync"]["failed"] == 0
    assert any("its files come one by one" in m for m in log_lines(st))


def test_no_account_no_zips(site, app_factory):
    c = app_factory(site)
    scan(c)
    select(c, "a:" + SMW)
    site.reset_log()
    st = sync(c)
    assert on_disk(c.data) == SMW_FILES and zip_requests(site) == []
    assert not [p for p, _ in site.requests if "/cp/" in p or "login" in p]
    assert st["account"]["configured"] is False


def test_the_password_never_reaches_the_page(site, app_factory):
    c = app_factory(site, settings={"account_user": "fan", "account_password": "secret"})
    s = c.get("/api/settings").json()["settings"]
    assert "account_password" not in s and s["account_password_set"] and "account_cookies" not in s
    r = c.post("/api/settings", json={"account_user": "fan", "account_password": ""}).json()["settings"]
    assert r["account_password_set"], "empty = keep it"
    r = c.post("/api/settings", json={"account_user": ""}).json()["settings"]
    assert not r["account_password_set"], "no username: the account is gone"
    assert c.app.state.engine.catalog.settings()["account_password"] == ""


def test_check_sign_in(site, app_factory):
    site.accounts["fan"] = ("secret", True)
    c = app_factory(site, settings={"account_user": "fan", "account_password": "secret"})
    r = c.post("/api/account/check").json()
    assert r["state"] == "signed_in" and r["configured"]
    c.post("/api/settings", json={"account_password": "nope"})
    assert c.post("/api/account/check").json()["state"] == "failed"


def test_a_zip_shows_in_the_downloading_box(site, app_factory):
    import time
    c = account(site, app_factory)
    site.slow_all = True
    assert c.post("/api/sync").status_code == 200
    seen = None
    t0 = time.time()
    while time.time() - t0 < 30:
        st = c.get("/api/status").json()
        rows = [a for a in ((st.get("sync") or {}).get("active") or []) if "Album zip" in a["rel"]]
        if rows:
            seen = (st, rows[0])
            break
        if not st["busy"]:
            break
        time.sleep(0.05)
    assert seen, "the zip appears with its progress while it downloads"
    st, row = seen
    assert row["rel"] == "Super Mario World/Album zip (MP3)" and 0 <= row["have"] <= row["total"]
    assert row["total"] == len(site.album_zip(SMW, site.zip_name(SMW)))
    assert st["job"]["phase"].startswith("downloading album zips")
    from conftest import wait
    assert wait(c)["sync"]["failed"] == 0
    assert on_disk(c.data) == SMW_FILES


# ------------------------------------------------------------------ the account's list
def adds(site):
    return [p for p, _ in site.requests if "/cp/add_album/" in p]


def test_queue_mode_only_puts_albums_on_the_list(site, app_factory):
    c = account(site, app_factory, account_mode="queue")
    st = sync(c)
    assert on_disk(c.data) == set(), "nothing downloaded yet"
    assert list(site.mass["fan"].values()) == [SMW] and len(adds(site)) == 1
    assert zip_requests(site) == [] and track_files(site) == []
    assert any("1 albums added to your khinsider list" in m for m in log_lines(st))
    site.reset_log()
    sync(c)
    assert adds(site) == [], "on the list already: not added again"
    assert on_disk(c.data) == set()


def test_download_my_list(site, app_factory):
    """Everything on the list comes down - also an album added on the site, not ticked in the app -
    straight from the list, and each leaves the list once it's saved."""
    from conftest import wait
    c = account(site, app_factory, account_mode="queue")
    sync(c)                                                  # SMW onto the list
    bird = "a-bird-story-linux-macos-windows-gamerip-2014"
    site.mass["fan"][site.albumid(bird)] = bird              # added by hand on khinsider
    site.reset_log()
    assert c.post("/api/account/list").status_code == 200
    st = wait(c)
    assert SMW_FILES <= on_disk(c.data)
    assert any(f.startswith("A Bird Story/") for f in on_disk(c.data))
    assert adds(site) == [], "zips straight from the list"
    assert site.mass["fan"] == {}, "each taken off the list once saved"
    assert any("Your khinsider list: 2 albums" in m for m in log_lines(st))
    assert c.get("/api/albums", params={"show": "ticked", "limit": 100}).json()["total"] == 2, "ticked now"


def test_a_sync_takes_an_album_already_on_the_list_from_there(site, app_factory):
    c = account(site, app_factory)
    site.mass["fan"] = {site.albumid(SMW): SMW}
    st = sync(c)
    assert on_disk(c.data) == SMW_FILES and adds(site) == []
    assert st["sync"]["failed"] == 0 and site.mass["fan"] == {}


def test_download_my_list_without_an_account(site, app_factory):
    from conftest import wait
    c = app_factory(site)
    scan(c)
    assert c.post("/api/account/list").status_code == 200
    st = wait(c)
    assert any("No khinsider account" in m for m in log_lines(st))
