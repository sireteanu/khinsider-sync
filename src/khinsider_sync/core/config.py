"""Settings for one sync (built by the engine)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .constants import DEFAULT_UA


@dataclass
class Config:
    start_url: str                    # the site; asked when downloads keep failing (is it down?)
    retries: int = 2
    user_agent: str = DEFAULT_UA
    # downloading
    download_dir: Optional[str] = None
    min_free: int = 2 * 1024 ** 3
    file_mode: Optional[int] = None       # e.g. 0o777: permissions for saved files and their folders
    retry_wait: float = 2.0           # first wait between a file's retries (doubles each time, max 60 s)
    # site outages: after 3 failed tries caused by the site (5xx, timeouts, refused connections) the
    # site itself is checked; if it doesn't answer, downloads wait for it instead of failing
    outage_wait: float = 12 * 3600    # give up waiting after this many seconds (0 = never wait)
    outage_check: float = 30.0        # first re-check after this many seconds, doubling up to 10 min
    overwrite: bool = False
    verify: bool = True
