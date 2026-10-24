"""Small helpers: extensions, safe file names, sizes, errors."""
from __future__ import annotations

import argparse
import os
import re

from .constants import _BAD_CHARS, _EXT_RE, _RESERVED

def extension(text: str) -> str:
    """'.7z' for 'Atari/5200/Game (USA).7z' (or '...7z.001'); '' when there is none."""
    tail = text.lower().rstrip("/").rsplit("/", 1)[-1]
    m = _EXT_RE.search(tail)
    return m.group(1) if m else ""


def _fold(value: str) -> str:
    return value.casefold().strip().rstrip("/")


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


class DiskFull(Exception):
    pass
