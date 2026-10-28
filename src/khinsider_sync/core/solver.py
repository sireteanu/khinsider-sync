"""FlareSolverr as a fallback for Cloudflare's browser check (optional, off by default).

FlareSolverr (github.com/FlareSolverr/FlareSolverr) is a small server the owner runs next to the app
(Docker: ghcr.io/flaresolverr/flaresolverr, port 8191). It opens a page in a real Chromium, waits
for Cloudflare's "Just a moment..." check to pass and answers with the page, the cookies it got
(`cf_clearance`) and the browser's User-Agent.

`SolverTransport` wraps the app's transport (ChromeTransport or plain httpx). Nothing changes until a
khinsider page answers with the check (`util.is_challenge`). Then, for GET requests only:

1. FlareSolverr is asked for that page (one at a time; at most one attempt per `cooldown`).
2. Its cookies go into the client's ONE cookie store (curl's session with ChromeTransport - never a
   second Cookie header, see browser.py) and its User-Agent is sent on every later khinsider request
   (`cf_clearance` only works together with the User-Agent it was issued for).
3. The page is asked for again directly. That normally goes through, and so do the next pages.
4. If the direct request still gets the check, FlareSolverr's copy of the page is used instead, and
   from then on every page that gets the check comes from FlareSolverr (one at a time, still paced
   by the job's page limits) until a direct request goes through again.

When FlareSolverr can't help (not reachable, an error, cooldown), the check is returned unchanged and
the app's usual waiting (`Engine._challenge_backoff`) takes over. File downloads (vgmtreasurechest)
never go through FlareSolverr. The solved cookies are kept in a `SolveState` the engine shares
between jobs, so a new job doesn't ask FlareSolverr again while they still work.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from . import browser
from .util import is_challenge

log = logging.getLogger("khinsider_sync")

TIMEOUT_MS = 60000              # how long FlareSolverr may take for one page
COOLDOWN = 60.0                 # seconds without a new attempt after FlareSolverr failed


def normalize_url(url: str) -> str:
    """'host:8191' -> 'http://host:8191'; '' stays '' (off). Raises ValueError for anything else."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/v1"):
        url = url[:-3]
    if "://" not in url:
        url = "http://" + url
    if not url.startswith(("http://", "https://")) or not httpx.URL(url).host:
        raise ValueError("FlareSolverr address must look like http://host:8191")
    return url


class FlareSolverr:
    """A FlareSolverr server's v1 API."""

    def __init__(self, url: str, timeout_ms: int = TIMEOUT_MS):
        self.url = normalize_url(url)
        self.timeout_ms = timeout_ms

    async def health(self) -> dict:
        """{ok, version, message} - the Test button in Settings."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(self.url + "/")
                data = r.json() if "json" in r.headers.get("content-type", "") else {}
        except (httpx.HTTPError, ValueError) as e:
            return {"ok": False, "version": "", "message": f"not reachable: {e or type(e).__name__}"}
        ok = r.status_code == 200 and bool(data.get("version") or data.get("msg"))
        return {"ok": ok, "version": str(data.get("version") or ""),
                "message": str(data.get("msg") or f"HTTP {r.status_code}")}

    async def get(self, url: str, user_agent: str = "") -> Optional[dict]:
        """FlareSolverr's `solution` for `url` ({url, status, headers, response, cookies, userAgent})
        or None when it couldn't get the page."""
        body = {"cmd": "request.get", "url": url, "maxTimeout": self.timeout_ms}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_ms / 1000 + 15) as c:
                r = await c.post(self.url + "/v1", json=body)
                data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("FlareSolverr (%s) didn't answer: %s", self.url, e or type(e).__name__)
            return None
        if data.get("status") != "ok" or not isinstance(data.get("solution"), dict):
            log.warning("FlareSolverr couldn't get past the check: %s", data.get("message") or data.get("status"))
            return None
        return data["solution"]


class SolveState:
    """What FlareSolverr got (cookies + User-Agent), shared by every job's client."""

    def __init__(self):
        self.cookies: dict = {}
        self.user_agent = ""
        self.solved_at = 0.0
        self.failed_at = 0.0        # FlareSolverr's last failure (no new attempt for `cooldown`)
        self.html_only = False      # its cookie didn't help direct requests: use its copies of pages
        self.solves = 0
        self.pages = 0              # pages FlareSolverr's own copy was used for
        self.lock = asyncio.Lock()

    def clear(self) -> None:
        self.cookies, self.user_agent, self.solved_at = {}, "", 0.0
        self.failed_at, self.html_only = 0.0, False


class SolverTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, solver: FlareSolverr, host: str, state: SolveState,
                 cooldown: float = COOLDOWN):
        self.inner = inner
        self.solver = solver
        self.host = host
        self.state = state
        self.cooldown = cooldown
        self._seen = 0.0            # state.solved_at last put into curl's session
        self._sync_jar()

    # -- the one cookie store
    @property
    def _curl(self):
        return self.inner.session.cookies if isinstance(self.inner, browser.ChromeTransport) else None

    def _sync_jar(self) -> None:
        if self._curl is not None and self.state.solved_at and self._seen != self.state.solved_at:
            for name, value in self.state.cookies.items():
                self._curl.set(name, value, domain=self.host, path="/")
            self._seen = self.state.solved_at

    def _prepare(self, request: httpx.Request) -> httpx.Request:
        """The request with FlareSolverr's User-Agent (and, without curl, its cookies merged into the
        request's own single Cookie header)."""
        st = self.state
        if request.url.host != self.host or not st.solved_at:
            return request
        self._sync_jar()
        headers = httpx.Headers(request.headers)
        if st.user_agent:
            headers["User-Agent"] = st.user_agent
        if self._curl is None and st.cookies:
            have = {}
            for part in (headers.get("cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k:
                    have[k] = v
            have.update(st.cookies)
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in have.items())
        return httpx.Request(request.method, request.url, headers=headers,
                             stream=request.stream, extensions=request.extensions)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        sent = time.time()
        st = self.state
        r = await self.inner.handle_async_request(self._prepare(request))
        if request.method != "GET" or request.url.host != self.host:
            return r
        if not is_challenge(r.status_code, r.headers):
            st.html_only = False                           # direct requests go through (again)
            return r
        await r.aread()
        challenge = httpx.Response(r.status_code, headers=r.headers, content=r.content, request=request)
        await r.aclose()
        async with st.lock:                                # one FlareSolverr request at a time
            if st.html_only:
                # its cookie didn't help direct requests: every checked page comes from FlareSolverr
                sol = await self._solve(request)
                if not sol:
                    return challenge
                self._take(sol)
                return self._page(sol, request) or challenge
            if st.solved_at <= sent:                       # nobody solved it while we waited
                sol = await self._solve(request)
                if not sol:
                    return challenge
                self._take(sol)
            else:
                sol = None
        # again, directly, with FlareSolverr's cookie and User-Agent
        r = await self.inner.handle_async_request(self._prepare(request))
        if not is_challenge(r.status_code, r.headers):
            return r
        await r.aclose()
        if sol is None:
            return challenge
        st.html_only = True
        log.warning("FlareSolverr's cookie doesn't get direct requests past the check - checked pages "
                    "now come from FlareSolverr itself (slower)")
        return self._page(sol, request) or challenge

    async def _solve(self, request: httpx.Request) -> Optional[dict]:
        st = self.state
        if time.time() - st.failed_at < self.cooldown:
            return None                                    # it just failed: wait as usual
        log.info("Cloudflare browser check on %s - asking FlareSolverr", request.url.path)
        sol = await self.solver.get(str(request.url), st.user_agent)
        if not sol:
            st.failed_at = time.time()
        return sol

    def _take(self, sol: dict) -> None:
        st = self.state
        cookies = {}
        for c in sol.get("cookies") or []:
            d = str(c.get("domain") or "").lstrip(".")
            if c.get("name") and (not d or self.host == d or self.host.endswith("." + d)):
                cookies[str(c["name"])] = str(c.get("value") or "")
        st.cookies = cookies
        st.user_agent = str(sol.get("userAgent") or st.user_agent)
        st.solved_at = time.time()
        st.solves += 1
        log.info("FlareSolverr got past the check (%s)", ", ".join(sorted(cookies)) or "no cookies")

    def _page(self, sol: dict, request: httpx.Request) -> Optional[httpx.Response]:
        """FlareSolverr's own copy of the page as the response (the direct request was still checked)."""
        status = int(sol.get("status") or 0)
        html = sol.get("response")
        if not isinstance(html, str) or status != 200:
            return None
        final = httpx.URL(str(sol.get("url") or request.url))
        if final.host == self.host and final.path != request.url.path:
            # it followed a redirect (a renamed album): let httpx follow it too
            return httpx.Response(301, headers={"Location": str(final)}, request=request)
        self.state.pages += 1
        log.info("Using FlareSolverr's copy of %s (direct requests still get the check)", request.url.path)
        return httpx.Response(200, headers={"Content-Type": "text/html; charset=UTF-8",
                                            "X-Khinsider-Sync-Via": "flaresolverr"},
                              content=html.encode("utf-8"), request=request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def wrap(transport: Optional[httpx.AsyncBaseTransport], url: str, host: str, state: SolveState,
         **plain) -> Optional[httpx.AsyncBaseTransport]:
    """`transport` (None = httpx's own, built from `plain` options) wrapped for FlareSolverr when `url`
    is set, else unchanged."""
    if not url:
        return transport
    inner = transport or httpx.AsyncHTTPTransport(**plain)
    return SolverTransport(inner, FlareSolverr(url), host, state)
