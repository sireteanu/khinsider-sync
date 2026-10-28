"""The Chrome-like client (core/browser.py) against a local server: what it really sends."""
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from khinsider_sync.core import browser

pytestmark = pytest.mark.skipif(browser.AsyncSession is None, reason="needs curl_cffi")


def test_cookies_go_out_once_like_a_browser():
    """khinsider's Cloudflare sets __cf_bm on the first page. Sent back twice (httpx's cookie jar and
    curl's both added it) every page after the first got the browser check (owner's server, 1.0.26)."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get_all("Cookie")))
            self.send_response(200)
            if self.path == "/first":
                self.send_header("Set-Cookie", "__cf_bm=abc123; Path=/; HttpOnly")
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    async def run():
        async with httpx.AsyncClient(transport=browser.ChromeTransport(4)) as c:
            for p in ("/first", "/second", "/third"):
                assert (await c.get(base + p)).status_code == 200

    try:
        asyncio.run(run())
    finally:
        srv.shutdown()
    assert seen[0] == ("/first", None)
    assert seen[1] == ("/second", ["__cf_bm=abc123"]), "kept, and sent once"
    assert seen[2] == ("/third", ["__cf_bm=abc123"])
