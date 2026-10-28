"""A khinsider account: a paid one may download whole albums as zips ("mass downloads").

How the site does it (checked in a browser with a paid account, October 2026):
  /forums/login                          XenForo login form (hidden _xfToken, _xfRedirect)
  POST /forums/index.php?login/login     login, password, remember=1 + the hidden fields
  /cp/add_album/<albumid>                adds the album to "My Mass Downloads"; the reply links its MP3
                                         zip ("Click here to download now!"); signed out it's "Please Log In"
  /cp/downloads                          the list: per album its MP3 and (if it has one) FLAC zip
  /cp/downloads_delete_album?albumid=N   takes an album off the list
The zips are on the file server (no browser check) and hold the album's files under khinsider's
names, its images and khinsider.info.txt, all at the top.

The app signs in with the username and password from Settings, keeps the session's cookies
between jobs, and signs in again when a page says it's signed out. Without an account, or once
it can't mass-download any more (a subscription ended), syncs go file by file as before.
"""
from __future__ import annotations

import logging
import time
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx

from . import parse
from .core.browser import get_cookies, set_cookies
from .core.util import CHALLENGE_MESSAGE, ChallengeError, is_challenge

log = logging.getLogger("khinsider_sync")


class AccountError(Exception):
    """The account can't be used for album zips now (the message says why)."""


class NoMassDownloads(AccountError):
    """Signed in, but the account may not download whole albums (no paid subscription now)."""


def configured(s: dict) -> bool:
    return bool((s.get("account_user") or "").strip() and s.get("account_password")
                and s.get("account_zips", True))


class Account:
    def __init__(self, catalog, base: str, client: httpx.AsyncClient, page_rate=None):
        self.catalog = catalog
        self.base = base if base.endswith("/") else base + "/"
        self.host = urlsplit(self.base).hostname or ""
        self.client = client
        self.page_rate = page_rate
        s = catalog.settings()
        self.user = (s.get("account_user") or "").strip()
        self.password = s.get("account_password") or ""
        self.off: Optional[str] = None         # why album zips are off for the rest of this job
        self.signed_in = False
        self.requests = 0
        set_cookies(client, s.get("account_cookies") or {}, self.host)

    # ------------------------------------------------------------------ requests
    async def _page(self, method: str, url: str, **kw) -> httpx.Response:
        if self.page_rate is not None:
            await self.page_rate.wait()
        self.requests += 1
        r = await self.client.request(method, url, timeout=30, **kw)
        if is_challenge(r.status_code, r.headers):
            log.error(CHALLENGE_MESSAGE.format(status=r.status_code))
            raise ChallengeError(f"HTTP {r.status_code} (Cloudflare browser check)")
        if r.status_code != 200:
            raise AccountError(f"{urlsplit(url).path}: HTTP {r.status_code}")
        return r

    def _status(self, state: str, message: str = "") -> None:
        self.catalog.save_settings({"account_status": {"state": state, "message": message, "user": self.user,
                                                       "at": time.time()}})

    # ------------------------------------------------------------------ signing in
    async def sign_in(self) -> None:
        if not (self.user and self.password):
            raise AccountError("no khinsider username and password in Settings")
        r = await self._page("GET", urljoin(self.base, "forums/login"))
        form = parse.login_form(r.content)
        if form is None:
            if parse.signed_in(r.content):           # the cookies were still good
                self._signed_in()
                return
            raise AccountError("khinsider's login page has no login form")
        data = dict(form["fields"], login=self.user, password=self.password, remember="1")
        data["_xfRedirect"] = self.base
        r = await self._page("POST", urljoin(self.base, form["action"]), data=data)
        if not parse.signed_in(r.content):
            self._status("failed", "khinsider didn't accept the username or password")
            raise AccountError("khinsider didn't accept the username or password")
        self._signed_in()

    def _signed_in(self) -> None:
        self.signed_in = True
        keep = get_cookies(self.client, self.host)
        self.catalog.save_settings({"account_cookies": keep})
        log.info("Signed in to khinsider as %s", self.user)

    # ------------------------------------------------------------------ album zips
    async def album_zip(self, albumid: int, fmt: str, slug: str) -> str:
        """The address of the album's zip in `fmt` (".mp3" / ".flac"): adds it to My Mass Downloads."""
        url = urljoin(self.base, f"cp/add_album/{albumid}")
        r = await self._page("GET", url)
        reply = parse.add_album_reply(r.content)
        if not reply["signed_in"]:
            await self.sign_in()
            reply = parse.add_album_reply((await self._page("GET", url)).content)
            if not reply["signed_in"]:
                self._status("failed", "signed out again right after signing in")
                raise AccountError("khinsider signs the account out again")
        if not reply["zip"]:
            self._status("no_mass", reply["message"])
            raise NoMassDownloads(reply["message"] or "no album zip offered")
        if self.catalog.settings().get("account_status", {}).get("state") != "ok":
            self._status("ok", "album zips available")
        if fmt == ".mp3":
            return reply["zip"]
        r = await self._page("GET", urljoin(self.base, "cp/downloads"))
        row = parse.mass_downloads(r.content).get(slug) or {}
        zip_url = (row.get("zips") or {}).get(fmt)
        if not zip_url:
            raise AccountError(f"no {fmt[1:].upper()} zip for this album")
        return zip_url

    async def remove(self, albumid: int) -> None:
        """Take the album off My Mass Downloads again (it stays downloadable from the album page)."""
        try:
            await self._page("GET", urljoin(self.base, f"cp/downloads_delete_album?albumid={albumid}"))
        except (AccountError, httpx.HTTPError) as e:
            log.debug("could not take album %s off the mass download list: %s", albumid, e)
