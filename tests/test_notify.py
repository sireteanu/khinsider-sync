"""Notifications to ntfy / Gotify / Discord / Home Assistant webhooks after jobs."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import SMW, scan, select, sync, wait
from khinsider_sync import notify


@pytest.fixture
def hook():
    """A local receiver: hook.url, hook.got = [(path, headers, body)]."""
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            got.append((self.path, dict(self.headers), self.rfile.read(n)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}/hook"
    srv.got = got
    yield srv
    srv.shutdown()


def messages(hook, timeout=5):
    t0 = time.time()
    while time.time() - t0 < timeout and not hook.got:
        time.sleep(0.05)
    time.sleep(0.2)
    return [json.loads(b) for _p, _h, b in hook.got]


def test_kind_of_address():
    assert notify.kind_of("https://discord.com/api/webhooks/1/abc") == "discord"
    assert notify.kind_of("https://ntfy.sh/my-topic") == "ntfy"
    assert notify.kind_of("http://ntfy.home.lan/music") == "ntfy"
    assert notify.kind_of("https://gotify.example/message?token=x") == "json"
    assert notify.kind_of("http://homeassistant.local:8123/api/webhook/khinsider") == "json"
    assert notify.kind_of("pbul://o.AbC123") == "pushbullet"


def test_sync_sends_what_it_downloaded(site, app_factory, hook):
    c = app_factory(site, settings={"notify_url": hook.url})
    scan(c)
    select(c, "a:" + SMW)
    sync(c)
    msgs = messages(hook)
    assert msgs[0]["title"] == "Khinsider Sync: 4 new files"
    assert "Super Mario World" in msgs[0]["message"] and msgs[0]["body"] == msgs[0]["message"]
    hook.got.clear()
    sync(c)                                   # nothing new: nothing sent
    assert messages(hook, timeout=1) == []


def test_new_albums_of_ticked_platforms(site, app_factory, hook):
    c = app_factory(site, settings={"notify_url": hook.url, "notify_on": ["new_albums"]})
    scan(c)
    select(c, "p:nintendo-snes")
    site.add_album("brand-new-snes-ost", "Brand New SNES OST", platforms=["nintendo-snes"])
    site.add_album("brand-new-pc-ost", "Brand New PC OST", platforms=["windows"])
    assert c.post("/api/scan", json={"quick": True}).status_code == 200
    wait(c)
    msgs = messages(hook)
    assert msgs[0]["title"] == "Khinsider Sync: 1 new albums for you"
    assert "Brand New SNES OST" in msgs[0]["message"] and "PC" not in msgs[0]["message"]


def test_problems_and_chosen_events(site, app_factory, hook):
    c = app_factory(site, settings={"notify_url": hook.url, "notify_on": ["problems"]})
    scan(c)
    select(c, "a:persona-5")
    site.challenge = True
    sync(c)
    msgs = messages(hook)
    assert len(msgs) == 1 and msgs[0]["title"] == "Khinsider Sync needs a look"
    assert "browser check" in msgs[0]["message"]


def test_test_button_and_bad_addresses(site, app_factory, hook):
    c = app_factory(site)
    assert c.post("/api/notify/test", json={"url": hook.url}).json() == {"ok": True}
    assert messages(hook)[0]["message"].startswith("Test notification")
    r = c.post("/api/notify/test", json={"url": "ftp://nope"})
    assert r.status_code == 400 and "http" in r.json()["error"]
    assert c.post("/api/settings", json={"notify_url": "nope"}).status_code == 400
    r = c.post("/api/settings", json={"notify_on": ["downloads", "bogus"]}).json()
    assert r["settings"]["notify_on"] == ["downloads"]


def test_pushbullet(site, app_factory, hook, monkeypatch):
    monkeypatch.setattr(notify, "PUSHBULLET_API", hook.url)
    c = app_factory(site)
    assert c.post("/api/settings", json={"notify_url": "pbul://o.AbC123"}).status_code == 200
    assert c.post("/api/notify/test", json={"url": "pbul://o.AbC123"}).json() == {"ok": True}
    msgs = messages(hook)
    assert msgs[0] == {"type": "note", "title": "Khinsider Sync", "body": "Test notification - notifications work."}
    assert hook.got[0][1]["Access-Token"] == "o.AbC123"
    assert c.post("/api/settings", json={"notify_url": "pbul://"}).status_code == 400
