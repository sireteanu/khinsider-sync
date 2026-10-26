"""Notifications: a short message to a URL when something worth knowing happens (setting
`notify_url`; which events: `notify_on`).

  downloads    a sync downloaded new or changed files (which albums), or files failed
  new_albums   a scan found new albums that your ticked platforms bring in
  problems     a sync paused (Cloudflare's browser check, the download limit, a full disk) or a
               job failed

The URL decides the format:
  Discord webhook (discord.com/api/webhooks/...)  {"content": "**title**\\nmessage"}
  ntfy (a host with "ntfy" in its name)           the message as text, the title in a Title header
  Pushbullet: pbul://<access token>               a note to all your devices (Pushbullet's API,
                                                  the token in its Access-Token header)
  anything else (Gotify /message?token=..., Home Assistant /api/webhook/..., Apprise API ...)
                                                  JSON {"title", "message", "body"}
Unraid's own notifications live on the host, out of reach of a container: point this at ntfy,
Gotify, Discord or a Home Assistant webhook instead. Sending never fails a job; errors go to the log.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("khinsider_sync")

EVENTS = ("downloads", "new_albums", "problems")
PUSHBULLET_API = "https://api.pushbullet.com/v2/pushes"


def valid(url: str) -> bool:
    url = (url or "").strip().lower()
    return url.startswith(("http://", "https://")) or (url.startswith("pbul://") and len(url) > 7)


def kind_of(url: str) -> str:
    if url.strip().lower().startswith("pbul://"):
        return "pushbullet"
    host = (urlsplit(url).hostname or "").lower()
    path = urlsplit(url).path
    if host.endswith(("discord.com", "discordapp.com")) and "/api/webhooks/" in path:
        return "discord"
    if "ntfy" in host:
        return "ntfy"
    return "json"


async def send(url: str, title: str, message: str, timeout: float = 10.0) -> None:
    """Raises httpx errors / ValueError (for a test from the page); send_quietly() for jobs."""
    url = (url or "").strip()
    if not valid(url):
        raise ValueError("the notification address must start with http:// or https:// (or pbul:// for Pushbullet)")
    kind = kind_of(url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        if kind == "pushbullet":
            token = url[len("pbul://"):].strip("/")
            r = await client.post(PUSHBULLET_API, headers={"Access-Token": token},
                                  json={"type": "note", "title": title, "body": message})
            if r.status_code == 401:
                raise ValueError("Pushbullet doesn't accept that access token")
        elif kind == "discord":
            r = await client.post(url, json={"content": f"**{title}**\n{message}"[:1900]})
        elif kind == "ntfy":
            r = await client.post(url, content=message.encode("utf-8"),
                                  headers={"Title": title.encode("ascii", "replace").decode(), "Tags": "musical_note"})
        else:
            r = await client.post(url, json={"title": title, "message": message, "body": message})
    if r.status_code >= 400:
        raise ValueError(f"HTTP {r.status_code}: {r.text[:200]}")


async def send_quietly(settings: dict, event: str, title: str, message: str) -> None:
    url = (settings.get("notify_url") or "").strip()
    if not url or event not in (settings.get("notify_on") or EVENTS):
        return
    try:
        await send(url, title, message)
        log.info("Notification sent: %s", title)
    except Exception as e:
        log.warning("Could not send the notification (%s): %s", title, e)


def album_list(names: list, most: int = 8) -> str:
    names = sorted(names, key=str.casefold)
    out = ", ".join(names[:most])
    return out + (f" and {len(names) - most} more" if len(names) > most else "")
