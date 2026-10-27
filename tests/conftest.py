import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mock_site import MockSite  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from khinsider_sync.engine import Engine  # noqa: E402
from khinsider_sync.web import create_app  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
# fast DownloadGate timings for tests (seconds)
TUNING = {"ramp": 0.2, "confirm": 0.5, "probe": 300, "retry": 0.05, "outage_check": 0.3}
SMW = "super-mario-world-snes-gamerip"
BIRD = "a-bird-story-linux-macos-windows-gamerip-2014"


def fixture(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


@pytest.fixture
def site_factory():
    sites = []

    def make(modes: str = "") -> MockSite:
        s = MockSite(modes).start()
        sites.append(s)
        return s

    yield make
    for s in sites:
        s.stop()


@pytest.fixture
def site(site_factory):
    return site_factory()


@pytest.fixture
def app_factory(tmp_path):
    """app_factory(site, password=None, data_locked=False, tuning=None, name="config", settings=None)
    -> a started TestClient; c.data is the download folder. Several apps can share one download
    folder by passing different config names."""
    clients = []

    def make(site, password=None, data_locked=False, tuning=None, name="config", settings=None):
        cfg, data = tmp_path / name, tmp_path / "data"
        app = create_app(lambda: Engine(str(cfg), str(data), site.url + "/", data_locked=data_locked,
                                        scan_delay=0, tuning=tuning or TUNING), password=password)
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        c.data = data
        # no page pacing in tests (the mock site is local) unless a test asks for it
        settings = dict({} if password else {"pages_per_minute": 0, "albums_per_batch": 1000}, **(settings or {}))
        if settings:
            r = c.post("/api/settings", json=settings)
            assert r.status_code == 200, r.text
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)


def wait(c, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = c.get("/api/status").json()
        if not st["busy"]:
            return st
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def scan(c):
    assert c.post("/api/scan").status_code == 200
    return wait(c)


def select(c, key, include=True):
    r = c.post("/api/select", json={"key": key, "include": include})
    assert r.status_code == 200, r.text


def sync(c):
    assert c.post("/api/sync").status_code == 200
    return wait(c)


def albums(c, **params):
    return {a["slug"]: a for a in c.get("/api/albums", params=dict({"limit": 1000}, **params)).json()["albums"]}


def album(c, slug):
    return c.get("/api/album", params={"slug": slug}).json()


def on_disk(folder):
    out = set()
    for root, _, names in os.walk(folder):
        for n in names:
            if not n.startswith(".khinsider-sync.db"):
                out.add(os.path.relpath(os.path.join(root, n), folder).replace(os.sep, "/"))
    return out


def file_status(c, slug):
    """{file name: what the last sync did with it} for one album."""
    return {f["name"]: f["local"] for f in album(c, slug)["files"]}


def log_lines(st):
    return [line["msg"] for line in st["log"]]
