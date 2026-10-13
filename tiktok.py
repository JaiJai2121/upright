"""TikTok live lookup + stream selection (vertical layout only).

How the "vertical only" part works
----------------------------------
TikTok's room info lists every stream URL a live offers. Streamers who use the
dual layout (portrait + landscape at once) can expose more than one picture
shape. Rather than trusting a label, we:

  1. collect every stream URL we can find in the room info (the known
     stream_data structure first, then a deep scan of the whole JSON),
  2. ffprobe each one to read its real width x height,
  3. record the best stream that is taller than it is wide.

If nothing portrait exists, we either refuse (default) or center-crop the
landscape stream to 9:16 (LANDSCAPE_FALLBACK=crop).
"""
from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

from curl_cffi import requests as cffi

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
WWW = "https://www.tiktok.com"
WEBCAST = "https://webcast.tiktok.com"
FFMPEG_HEADERS = "Referer: https://www.tiktok.com/\r\n"

USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{2,24}$")


class TikTokError(Exception):
    """Anything that stops us from getting a usable stream."""


class NotLive(TikTokError):
    pass


class NoPortraitStream(TikTokError):
    pass


@dataclass
class Stream:
    url: str
    kind: str  # "flv" | "hls" | "rtmp"
    label: str = ""
    level: int = -1
    width: int = 0
    height: int = 0

    @property
    def portrait(self) -> bool:
        return self.height > self.width > 0

    @property
    def pixels(self) -> int:
        return self.width * self.height


@dataclass
class LiveInfo:
    username: str
    room_id: str
    title: str
    viewers: int | None
    cover_url: str | None
    streams: list[Stream]
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class Choice:
    stream: Stream
    crop: bool  # True = landscape source that must be cropped to 9:16


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def clean_username(raw: str) -> str:
    name = raw.strip()
    m = re.search(r"tiktok\.com/@([^/?#\s]+)", name)
    if m:
        name = m.group(1)
    name = name.lstrip("@")
    if not USERNAME_RE.match(name):
        raise ValueError(
            "That doesn't look like a TikTok username (letters, numbers, "
            "'.' and '_', 2-24 characters)."
        )
    return name


def _as_dict(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            d = json.loads(v)
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}
    return {}


def _parse_res(v: Any) -> tuple[int, int]:
    m = re.match(r"\s*(\d{2,5})\s*[x*×]\s*(\d{2,5})", str(v or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _is_stream_url(v: Any) -> bool:
    return isinstance(v, str) and (
        (v.startswith(("http://", "https://")) and (".flv" in v or ".m3u8" in v))
        or v.startswith("rtmp://")
    )


def _kind(url: str) -> str:
    if ".m3u8" in url:
        return "hls"
    if url.startswith("rtmp"):
        return "rtmp"
    return "flv"


def _walk(obj: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Yield (path, string) for every string leaf, opening embedded JSON."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        s = obj.strip()
        if s[:1] in ("{", "["):
            try:
                inner = json.loads(s)
            except ValueError:
                inner = None
            if inner is not None:
                yield from _walk(inner, path)
                return
        yield path, obj


def collect_streams(room: dict) -> list[Stream]:
    """Every stream URL in the room info, with declared sizes where given."""
    found: dict[str, Stream] = {}

    def add(url: Any, label: str, level: int = -1, res: tuple[int, int] = (0, 0)):
        if not _is_stream_url(url):
            return
        url = html.unescape(url)
        if url in found:
            s = found[url]
            if res[0] and not s.width:
                s.width, s.height = res
            return
        found[url] = Stream(url, _kind(url), label, level, res[0], res[1])

    stream_url = room.get("stream_url") or {}
    pull = (stream_url.get("live_core_sdk_data") or {}).get("pull_data") or {}
    levels = {
        q.get("sdk_key"): q.get("level", -1)
        for q in (pull.get("options") or {}).get("qualities") or []
        if isinstance(q, dict)
    }

    # 1) the modern structure: stream_data.data.<quality>.<variant>.{flv,hls}
    for quality, entry in (_as_dict(pull.get("stream_data")).get("data") or {}).items():
        if not isinstance(entry, dict):
            continue
        for variant, v in entry.items():
            if not isinstance(v, dict):
                continue
            res = _parse_res(_as_dict(v.get("sdk_params")).get("resolution"))
            for key in ("flv", "hls", "m3u8"):
                add(v.get(key), f"{quality}/{variant}", levels.get(quality, -1), res)

    # 2) legacy fields
    for key, u in (stream_url.get("flv_pull_url") or {}).items():
        add(u, f"legacy/{key}")
    for key, u in (stream_url.get("hls_pull_url_map") or {}).items():
        add(u, f"legacy/hls-{key}")
    add(stream_url.get("hls_pull_url"), "legacy/hls")
    add(stream_url.get("rtmp_pull_url"), "legacy/rtmp")

    # 3) deep scan: catches a portrait variant living anywhere else
    for path, val in _walk(room):
        add(val, "other:" + path.rsplit(".", 1)[-1])

    return list(found.values())


def _rank(s: Stream) -> tuple:
    return (s.pixels, s.kind == "flv", s.level)


def choose(streams: list[Stream], fallback: str = "skip") -> Choice:
    known = [s for s in streams if s.width and s.height]
    portrait = [s for s in known if s.portrait]
    if portrait:
        return Choice(max(portrait, key=_rank), crop=False)
    if not known:
        raise TikTokError(
            "Found stream URLs but couldn't read their size (ffprobe failed). "
            "TikTok may be blocking the CDN from this machine."
        )
    if fallback == "crop":
        return Choice(max(known, key=_rank), crop=True)
    sizes = ", ".join(sorted({f"{s.width}x{s.height}" for s in known}))
    raise NoPortraitStream(
        f"No vertical stream is available (only {sizes}). Set "
        "LANDSCAPE_FALLBACK=crop to center-crop landscape to 9:16 instead."
    )


# --------------------------------------------------------------------------
# network
# --------------------------------------------------------------------------
def _session(sessionid: str | None):
    s = cffi.Session(impersonate="chrome", timeout=20)
    s.headers.update({"Referer": WWW + "/", "Accept-Language": "en-US,en;q=0.9"})
    if sessionid:
        for k in ("sessionid", "sessionid_ss", "sid_tt"):
            s.cookies.set(k, sessionid, domain=".tiktok.com")
    return s


def _get_room_id(s, username: str) -> str | None:
    try:
        r = s.get(
            f"{WWW}/api-live/user/room/",
            params={"aid": "1988", "uniqueId": username, "sourceType": "54"},
        )
        user = (r.json().get("data") or {}).get("user") or {}
        rid = str(user.get("roomId") or "")
        if rid.isdigit() and int(rid) > 0:
            return rid
    except Exception:
        pass  # fall through to the live page

    r = s.get(f"{WWW}/@{username}/live")
    if r.status_code == 404:
        raise TikTokError(f"There's no TikTok account called @{username}.")
    m = re.search(r'"roomId"\s*:\s*"(\d{6,})"', r.text) or re.search(
        r"room_id=(\d{6,})", r.text
    )
    return m.group(1) if m else None


def _streams_from_page(s, username: str) -> list[Stream]:
    text = s.get(f"{WWW}/@{username}/live").text
    text = text.replace("\\u002F", "/").replace("\\u0026", "&")
    urls = re.findall(r"https?://[^\s\"'<>\\]+\.(?:flv|m3u8)[^\s\"'<>\\]*", text)
    return collect_streams({"stream_url": {"flv_pull_url": {f"page{i}": u for i, u in enumerate(urls)}}})


def _fetch_live_sync(username: str, sessionid: str | None) -> LiveInfo:
    s = _session(sessionid)
    room_id = _get_room_id(s, username)
    if not room_id:
        raise NotLive(f"@{username} isn't live right now.")

    r = s.get(f"{WEBCAST}/webcast/room/info/", params={"aid": "1988", "room_id": room_id})
    try:
        data = r.json()
    except ValueError:
        raise TikTokError("TikTok returned an unreadable response (possibly rate-limited).")

    if data.get("status_code") == 4003110:  # access restricted: try the page itself
        streams = _streams_from_page(s, username)
        if not streams:
            raise TikTokError(
                "TikTok restricted access to this live (age/region/login). "
                "Try setting TIKTOK_SESSIONID in .env."
            )
        return LiveInfo(username, room_id, "", None, None, streams, {})

    room = data.get("data") or {}
    status = room.get("status")
    if status is not None and str(status) != "2":
        raise NotLive(f"@{username} isn't live right now.")

    streams = collect_streams(room)
    if not streams:
        raise TikTokError("The live is on, but TikTok gave no stream URLs for it.")

    viewers = room.get("user_count")
    cover = ((room.get("cover") or {}).get("url_list") or [None])[0]
    return LiveInfo(
        username=username,
        room_id=room_id,
        title=room.get("title") or "",
        viewers=int(viewers) if str(viewers).isdigit() else None,
        cover_url=cover,
        streams=streams,
        raw=room,
    )


async def fetch_live(username: str, sessionid: str | None = None) -> LiveInfo:
    return await asyncio.to_thread(_fetch_live_sync, username, sessionid)


async def _probe_one(stream: Stream, sem: asyncio.Semaphore) -> None:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json",
        "-analyzeduration", "3000000", "-probesize", "3000000",
    ]
    if stream.url.startswith("http"):
        cmd += ["-user_agent", UA, "-headers", FFMPEG_HEADERS, "-rw_timeout", "10000000"]
    cmd.append(stream.url)

    proc = None
    async with sem:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await asyncio.wait_for(proc.communicate(), 20)
        except (asyncio.TimeoutError, OSError):
            if proc and proc.returncode is None:
                proc.kill()
            return
    try:
        info = json.loads(out.decode() or "{}")["streams"][0]
        w, h = int(info["width"]), int(info["height"])
    except (ValueError, KeyError, IndexError):
        return
    if w and h:
        stream.width, stream.height = w, h


async def probe_streams(streams: list[Stream]) -> None:
    """Fill in each stream's real width/height (ffprobe is the source of truth)."""
    sem = asyncio.Semaphore(4)
    await asyncio.gather(*(_probe_one(s, sem) for s in streams))
