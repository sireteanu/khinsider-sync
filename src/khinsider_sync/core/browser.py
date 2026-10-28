"""Requests that look like Chrome's.

khinsider sits behind Cloudflare, which can turn away clients that don't look like a browser. The
khi-explorer downloaders get through by using Chrome's TLS fingerprint and headers; this does the
same with curl_cffi, as an httpx transport, so every other part of the app keeps using httpx
(streaming, redirects, timeouts and its exceptions) unchanged.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

log = logging.getLogger("khinsider_sync")

try:                                           # optional: without it, plain httpx is used
    from curl_cffi.requests import AsyncSession
    from curl_cffi.requests import exceptions as cerr
except ImportError:                            # pragma: no cover - depends on the install
    AsyncSession = None
    cerr = None

IMPERSONATE = "chrome"
# headers httpx adds by itself; curl_cffi sends Chrome's own instead. "cookie" too: curl's session keeps
# the cookies and sends them itself - httpx's copy on top made every request after the first carry two
# Cookie headers (no browser does that), and from the second page on Cloudflare asked for its check
_DROP = {"host", "connection", "content-length", "accept-encoding", "transfer-encoding", "cookie"}


def _map_error(e: Exception, request: httpx.Request) -> httpx.HTTPError:
    msg = str(e) or type(e).__name__
    if cerr is not None:
        if isinstance(e, cerr.ConnectTimeout):
            return httpx.ConnectTimeout(msg, request=request)
        if isinstance(e, cerr.Timeout):
            return httpx.ReadTimeout(msg, request=request)
        if isinstance(e, (cerr.DNSError, cerr.ProxyError, cerr.SSLError, cerr.ConnectionError)):
            return httpx.ConnectError(msg, request=request)
        if isinstance(e, (cerr.ChunkedEncodingError, cerr.IncompleteRead)):
            return httpx.RemoteProtocolError(msg, request=request)
    return httpx.ReadError(msg, request=request)


class _Body(httpx.AsyncByteStream):
    def __init__(self, response, request: httpx.Request):
        self.response = response
        self.request = request

    async def __aiter__(self):
        try:
            async for chunk in self.response.aiter_content():
                if chunk:
                    yield chunk
        except httpx.HTTPError:
            raise
        except Exception as e:
            raise _map_error(e, self.request) from e

    async def aclose(self) -> None:
        try:
            await self.response.aclose()
        except Exception:
            pass


class ChromeTransport(httpx.AsyncBaseTransport):
    """httpx transport that sends every request through curl_cffi impersonating Chrome."""

    def __init__(self, max_clients: int = 10, impersonate: str = IMPERSONATE):
        if AsyncSession is None:
            raise RuntimeError("curl_cffi is not installed")
        self.impersonate = impersonate
        self.session = AsyncSession(max_clients=max_clients, impersonate=impersonate)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        headers = []
        for k, v in request.headers.multi_items():
            lk = k.lower()
            if lk in _DROP:
                continue
            if lk == "user-agent" and v.startswith("python-httpx"):
                continue                        # Chrome's own User-Agent goes out instead
            if lk == "accept" and v == "*/*":
                continue
            headers.append((k, v))
        t = request.extensions.get("timeout") or {}
        connect = t.get("connect") or 10.0
        read = t.get("read") or 30.0
        body = None
        if request.method not in ("GET", "HEAD"):
            body = await request.aread() or None
        try:
            r = await self.session.request(
                request.method, str(request.url), headers=dict(headers), data=body,
                stream=True, allow_redirects=False, timeout=(connect, read), impersonate=self.impersonate)
        except Exception as e:
            raise _map_error(e, request) from e
        out = []
        decoded = False
        for k, v in r.headers.multi_items():
            if v is None:
                continue
            lk = k.lower()
            if lk == "content-encoding":
                decoded = True                  # curl has already decompressed the body
                continue
            out.append((k, v))
        if decoded:
            out = [(k, v) for k, v in out if k.lower() != "content-length"]
        return httpx.Response(r.status_code, headers=out, stream=_Body(r, request), request=request,
                              extensions={"http_version": b"HTTP/2" if getattr(r, "http_version", 0) == 2
                                          else b"HTTP/1.1"})

    async def aclose(self) -> None:
        try:
            await self.session.close()
        except Exception:
            pass


def make_transport(enabled: bool, max_clients: int) -> Optional[httpx.AsyncBaseTransport]:
    """ChromeTransport when wanted and possible, else None (plain httpx)."""
    if not enabled:
        return None
    if AsyncSession is None:
        global _warned
        if not _warned:
            _warned = True
            log.warning("Looking like Chrome needs curl_cffi, which isn't installed - using a plain "
                        "client (pip install curl_cffi)")
        return None
    return ChromeTransport(max_clients=max_clients)


_warned = False
