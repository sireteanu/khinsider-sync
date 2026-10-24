"""File formats, protocol constants and name rules."""
from __future__ import annotations

import re

from .. import __version__

# Audio formats khinsider offers (column headers on an album page) -> file extension.
FORMAT_EXT = {
    "MP3": ".mp3", "FLAC": ".flac", "OGG": ".ogg", "M4A": ".m4a", "AAC": ".aac", "WAV": ".wav",
    "OPUS": ".opus", "ALAC": ".m4a", "WMA": ".wma", "APE": ".ape", "WV": ".wv",
}
AUDIO_EXTENSIONS = frozenset(FORMAT_EXT.values())
LOSSLESS = (".flac", ".wav", ".ape", ".wv")          # "best quality" picks the first one an album has
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})

# First bytes of a valid file, used to reject error pages saved under an audio or image name.
MAGIC = {
    ".mp3": (b"ID3", b"\xff\xfb", b"\xff\xfa", b"\xff\xf3", b"\xff\xf2", b"\xff\xe3", b"\xff\xe2"),
    ".flac": (b"fLaC", b"ID3"),
    ".ogg": (b"OggS",),
    ".opus": (b"OggS",),
    ".wav": (b"RIFF",),
    ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG",),
    ".gif": (b"GIF8",),
}

RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})
THROTTLE_STATUSES = frozenset({429, 503})
MAX_BODY = 25_000_000
CHUNK = 256 * 1024
DEFAULT_UA = f"Mozilla/5.0 (compatible; khinsider-sync/{__version__})"

_EXT_RE = re.compile(r"(\.[a-z0-9]{1,10})(?:\.\d{1,3})?$")      # ".mp3", also ".7z.001"
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                       *(f"lpt{i}" for i in range(1, 10))})
