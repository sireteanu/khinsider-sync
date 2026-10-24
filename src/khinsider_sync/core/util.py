"""Small helpers: extensions, safe file names, sizes, errors."""
from __future__ import annotations

import argparse
import os
import re

from .constants import _BAD_CHARS, _EXT_RE, _RESERVED

def extension(text: str) -> str:
    """'.flac' for 'Persona 5/1-01. Wake Up.flac'; '' when there is none."""
    tail = text.lower().rstrip("/").rsplit("/", 1)[-1]
    m = _EXT_RE.search(tail)
    return m.group(1) if m else ""


def safe_segment(name: str) -> str:
    """Make one path segment valid on Windows, macOS and Linux."""
    name = _BAD_CHARS.sub("_", name).strip().rstrip(". ")
    if not name:
        return "_"
    if name.split(".", 1)[0].lower() in _RESERVED:
        name = "_" + name
    if len(name) > 180:                                  # keep the extension when shortening
        stem, ext = os.path.splitext(name)
        name = stem[:180 - len(ext)].rstrip(". ") + ext
    return name


def fs_path(path: str) -> str:
    """Absolute path; on Windows, long paths get the \\\\?\\ prefix so they still work."""
    path = os.path.abspath(path)
    if os.name == "nt" and len(path) > 240 and not path.startswith("\\\\?\\"):
        path = "\\\\?\\" + path
    return path


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def parse_size(text: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?\s*", (text or "0").lower())
    if not m:
        raise argparse.ArgumentTypeError(f"not a size: {text!r} (examples: 500K, 5M, 2G)")
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}[m.group(2)]
    return int(float(m.group(1)) * mult)


class FetchError(Exception):
    pass


class ChallengeError(FetchError):
    """The site's Cloudflare protection answered with its browser check ("Just a moment...")
    instead of the page: every further page request from this network would get the same."""


CHALLENGE_MESSAGE = ("khinsider's Cloudflare protection asks this network for a browser check "
                     "(HTTP {status} \"Just a moment...\") - its pages can't be read from here right now. "
                     "Nothing was marked as failed; try again later. If it keeps happening, check that "
                     "\"Look like Chrome\" is on in Settings")


def is_challenge(status: int, headers) -> bool:
    """A Cloudflare browser check (managed challenge) instead of the page?"""
    return status in (403, 429, 503) and (headers.get("cf-mitigated") or "").lower() == "challenge"


class DiskFull(Exception):
    pass
