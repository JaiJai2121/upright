"""Upright: a small web app that records TikTok lives, vertical layout only.

Run:  python app.py   then open http://localhost:8000
"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    import uvicorn
    from dotenv import load_dotenv
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse, Response
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    import recorder
    import tiktok
except ImportError as e:
    sys.exit(f"Missing a dependency ({e}). Run: pip install -r requirements.txt")

load_dotenv()
log = logging.getLogger("upright")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        sys.exit(f"{name} in .env must be a whole number.")


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
HOST = os.getenv("HOST", "127.0.0.1").strip()
PORT = env_int("PORT", 8000)
RECORD_DIR = Path(os.getenv("RECORD_DIR", "recordings")).resolve()
MAX_CONCURRENT = env_int("MAX_CONCURRENT", 3)
MAX_RECORD_SECONDS = env_int("MAX_RECORD_SECONDS", 0)  # 0 = until the live ends
LANDSCAPE_FALLBACK = os.getenv("LANDSCAPE_FALLBACK", "skip").strip().lower()  # skip | crop
SESSIONID = os.getenv("TIKTOK_SESSIONID") or None
PASSWORD = os.getenv("APP_PASSWORD") or ""
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"
DEBUG_DIR = BASE_DIR / "debug"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}

PREVIEW_DIR = Path(tempfile.mkdtemp(prefix="upright-previews-"))
BOOT = secrets.token_hex(4)  # lets the page notice a server restart

MEDIA_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}\.(mp4|mkv|jpg)$")
MEDIA_TYPES = {".mp4": "video/mp4", ".mkv": "video/x-matroska", ".jpg": "image/jpeg"}


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
@dataclass
class Session:
    rec: recorder.Recording
    info: tiktok.LiveInfo
    choice: tiktok.Choice
    stopping: bool = False
    task: asyncio.Task | None = None
    stop_task: asyncio.Task | None = None


active: dict[str, Session] = {}  # keyed by lowercase username
starting: set[str] = set()
events: collections.deque[dict] = collections.deque(maxlen=20)
_event_id = 0


def push_event(kind: str, username: str, message: str, file: str | None = None) -> None:
    global _event_id
    _event_id += 1
    events.append({"id": _event_id, "kind": kind, "username": username, "message": message, "file": file})


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": exc.message}, status_code=exc.status)


def guard(request: Request) -> None:
    """State-changing calls must carry a custom header. Other websites can't add one
    to a request aimed at this app without a CORS preflight, which we never allow."""
    if request.headers.get("x-upright") != "1":
        raise ApiError(403, "Open the app in your browser and use it from there.")


async def read_json(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        data = None
    if not isinstance(data, dict):
        raise ApiError(400, 'Send JSON like {"username": "testuser"}.')
    return data


def fmt_clock(sec: int) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_url(url: str | None) -> str | None:
    return url if isinstance(url, str) and url.startswith("https://") else None


def clean_name(data: dict) -> str:
    try:
        return tiktok.clean_username(str(data.get("username", "")))
    except ValueError as e:
        raise ApiError(422, str(e))


def session_json(s: Session) -> dict:
    r, c = s.rec, s.choice
    return {
        "username": r.username,
        "title": s.info.title,
        "cover": safe_url(s.info.cover_url),
        "viewers": s.info.viewers,
        "seconds": int(r.duration),
        "stopping": s.stopping,
        "preview": bool(r.preview_path and r.preview_path.exists()),
        "layout": {"cropped": c.crop, "width": c.stream.width, "height": c.stream.height, "format": c.stream.kind},
        "watch_url": f"https://www.tiktok.com/@{r.username}/live",
    }


def dump_debug(info: tiktok.LiveInfo) -> None:
    """Save the room info (URL tokens removed) so a live's stream layouts can be inspected."""
    DEBUG_DIR.mkdir(exist_ok=True)
    text = json.dumps(info.raw, indent=2, ensure_ascii=False)
    text = re.sub(r"\?[^\s\"\\]*", "?<removed>", text)
    path = DEBUG_DIR / f"{info.username}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(text, encoding="utf-8")
    log.info("wrote %s", path)


async def resolve(name: str) -> tiktok.LiveInfo:
    info = await tiktok.fetch_live(name, SESSIONID)
    if DEBUG_DUMP:
        dump_debug(info)
    await tiktok.probe_streams(info.streams)
    return info


# --------------------------------------------------------------------------
# recording lifecycle
# --------------------------------------------------------------------------
async def finish(key: str, s: Session) -> None:
    """Runs in the background until ffmpeg ends, then files the result."""
    rec = s.rec
    path: Path | None = None
    try:
        path = await rec.wait()
    except Exception:
        log.exception("recording of %s failed while finishing", rec.username)
    seconds = int(rec.duration)

    if path:
        try:
            meta = {
                "title": s.info.title,
                "seconds": seconds,
                "layout": session_json(s)["layout"],
                "finished": time.time(),
            }
            path.with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")
            if rec.preview_path and rec.preview_path.exists():
                shutil.copyfile(rec.preview_path, path.with_suffix(".jpg"))
        except OSError:
            log.exception("couldn't write details next to %s", path)

    active.pop(key, None)
    if rec.preview_path:
        rec.preview_path.unlink(missing_ok=True)

    if path is None:
        detail = rec.tail_text()
        push_event("error", rec.username, f"The recording of @{rec.username} ended with no usable video."
                   + (f" ffmpeg said: {detail.splitlines()[-1]}" if detail else ""))
    elif rec.returncode not in (0, None) and not s.stopping:
        push_event("error", rec.username,
                   f"The connection to @{rec.username} dropped. Saved what was captured ({fmt_clock(seconds)}).",
                   path.name)
    else:
        push_event("done", rec.username, f"Saved @{rec.username}'s recording ({fmt_clock(seconds)}).", path.name)


# --------------------------------------------------------------------------
# library
# --------------------------------------------------------------------------
def busy_paths() -> set[Path]:
    out: set[Path] = set()
    for s in active.values():
        out |= {s.rec.raw_path.resolve(), s.rec.final_path.resolve()}
    return out


def read_meta(media: Path) -> dict:
    try:
        data = json.loads(media.with_suffix(".json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def list_library() -> list[dict]:
    items: list[dict] = []
    if not RECORD_DIR.exists():
        return items
    busy = busy_paths()
    for user_dir in RECORD_DIR.iterdir():
        if not user_dir.is_dir() or not tiktok.USERNAME_RE.match(user_dir.name):
            continue
        for f in user_dir.iterdir():
            if f.suffix not in (".mp4", ".mkv") or f.resolve() in busy:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            meta = read_meta(f)
            items.append({
                "username": user_dir.name,
                "name": f.name,
                "size": st.st_size,
                "modified": st.st_mtime,
                "seconds": meta.get("seconds"),
                "title": meta.get("title") or "",
                "playable": f.suffix == ".mp4",
                "thumb": f.with_suffix(".jpg").exists(),
            })
    items.sort(key=lambda i: i["modified"], reverse=True)
    return items


def media_path(username: str, name: str) -> Path | None:
    """Resolve a file inside RECORD_DIR, or None if the request is anything else."""
    if not tiktok.USERNAME_RE.match(username) or not MEDIA_RE.match(name):
        return None
    path = (RECORD_DIR / username / name).resolve()
    if not path.is_relative_to(RECORD_DIR) or not path.is_file() or path in busy_paths():
        return None
    return path


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------
async def index(request: Request) -> Response:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def api_status(request: Request) -> Response:
    return JSONResponse({
        "boot": BOOT,
        "limit": MAX_CONCURRENT,
        "active": [session_json(s) for s in active.values()],
        "events": list(events),
    })


async def api_check(request: Request) -> Response:
    """Look the user up and report what layouts their live offers, without recording."""
    guard(request)
    name = clean_name(await read_json(request))
    try:
        info = await resolve(name)
    except tiktok.NotLive as e:
        return JSONResponse({"live": False, "username": name, "message": str(e)})
    except tiktok.TikTokError as e:
        raise ApiError(502, str(e))
    except Exception:
        log.exception("check of %s failed", name)
        raise ApiError(500, "Something went wrong looking that user up. Check the app's console for details.")

    try:
        choice = tiktok.choose(info.streams, LANDSCAPE_FALLBACK)
        plan = {"mode": "crop" if choice.crop else "vertical",
                "width": choice.stream.width, "height": choice.stream.height}
    except tiktok.TikTokError as e:  # includes NoPortraitStream
        plan = {"mode": "none", "message": str(e)}

    sizes: dict[tuple[int, int], int] = collections.Counter(
        (s.width, s.height) for s in info.streams if s.width and s.height
    )
    layouts = [
        {"width": w, "height": h, "vertical": h > w, "variants": n}
        for (w, h), n in sorted(sizes.items(), key=lambda kv: kv[0][0] * kv[0][1], reverse=True)
    ]
    return JSONResponse({
        "live": True,
        "username": name,
        "title": info.title,
        "viewers": info.viewers,
        "cover": safe_url(info.cover_url),
        "layouts": layouts,
        "plan": plan,
    })


async def api_rec(request: Request) -> Response:
    guard(request)
    name = clean_name(await read_json(request))
    key = name.lower()
    if key in active or key in starting:
        raise ApiError(409, f"Already recording @{name}.")
    if len(active) + len(starting) >= MAX_CONCURRENT:
        raise ApiError(429, f"Already recording {MAX_CONCURRENT} lives at once. Stop one to start another.")

    starting.add(key)
    try:
        info = await resolve(name)
        choice = tiktok.choose(info.streams, LANDSCAPE_FALLBACK)
        rec = recorder.Recording(
            name, choice.stream, choice.crop, RECORD_DIR, MAX_RECORD_SECONDS,
            preview_path=PREVIEW_DIR / f"{key}.jpg",
        )
        await rec.start()
    except tiktok.NotLive as e:
        raise ApiError(404, str(e))
    except tiktok.NoPortraitStream as e:
        raise ApiError(422, str(e))
    except (tiktok.TikTokError, recorder.RecorderError) as e:
        raise ApiError(502, str(e))
    except Exception:
        log.exception("couldn't start recording %s", name)
        raise ApiError(500, "Something went wrong starting that recording. Check the app's console for details.")
    finally:
        starting.discard(key)

    sess = Session(rec, info, choice)
    active[key] = sess
    sess.task = asyncio.create_task(finish(key, sess))
    return JSONResponse(session_json(sess), status_code=201)


async def api_stop(request: Request) -> Response:
    guard(request)
    sess = active.get(request.path_params["username"].lower())
    if sess is None:
        raise ApiError(404, "That recording already finished.")
    if not sess.stopping:
        sess.stopping = True
        sess.stop_task = asyncio.create_task(sess.rec.stop())
    return JSONResponse({"ok": True}, status_code=202)


async def api_preview(request: Request) -> Response:
    sess = active.get(request.path_params["username"].lower())
    path = sess.rec.preview_path if sess else None
    try:
        data = path.read_bytes() if path else b""
    except OSError:
        data = b""
    if not data:
        return Response(status_code=404)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def api_library(request: Request) -> Response:
    return JSONResponse({"items": list_library()})


async def api_delete(request: Request) -> Response:
    guard(request)
    username, name = request.path_params["username"], request.path_params["name"]
    path = media_path(username, name)
    if path is None or path.suffix not in (".mp4", ".mkv"):
        raise ApiError(404, "That recording is already gone.")
    for p in (path, path.with_suffix(".json"), path.with_suffix(".jpg")):
        p.unlink(missing_ok=True)
    return JSONResponse({"ok": True})


async def file_get(request: Request) -> Response:
    path = media_path(request.path_params["username"], request.path_params["name"])
    if path is None:
        return Response("Not found", status_code=404)
    download = request.query_params.get("download") == "1"
    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(path.suffix),
        filename=path.name if download else None,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@asynccontextmanager
async def lifespan(app: Starlette):
    yield
    # Ctrl+C: let ffmpeg finish each file properly so nothing is lost.
    sessions = list(active.values())
    await asyncio.gather(*(s.rec.stop() for s in sessions), return_exceptions=True)
    tasks = [s.task for s in sessions if s.task]
    if tasks:
        await asyncio.wait(tasks, timeout=30)
    shutil.rmtree(PREVIEW_DIR, ignore_errors=True)


class BasicAuth:
    """Optional single shared password (browser's own sign-in box; any user name works)."""

    def __init__(self, app, password: str):
        self.app = app
        self.password = password.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.password:
            return await self.app(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        ok = False
        if auth[:6].lower() == "basic ":
            try:
                given = base64.b64decode(auth[6:]).decode("utf-8").partition(":")[2].encode()
                ok = secrets.compare_digest(given, self.password)
            except Exception:
                ok = False
        if not ok:
            resp = Response("Password required.", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Upright", charset="UTF-8"'})
            return await resp(scope, receive, send)
        await self.app(scope, receive, send)


def build_app() -> BasicAuth:
    middleware = []
    if HOST in LOOPBACK:  # blocks DNS-rebinding tricks against the local app
        middleware.append(Middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"]))
    starlette_app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/status", api_status),
            Route("/api/check", api_check, methods=["POST"]),
            Route("/api/rec", api_rec, methods=["POST"]),
            Route("/api/stop/{username}", api_stop, methods=["POST"]),
            Route("/api/preview/{username}", api_preview),
            Route("/api/library", api_library),
            Route("/api/library/{username}/{name}", api_delete, methods=["DELETE"]),
            Route("/files/{username}/{name}", file_get),
            Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
        ],
        middleware=middleware,
        exception_handlers={ApiError: api_error_handler},
        lifespan=lifespan,
    )
    return BasicAuth(starlette_app, PASSWORD)


app = build_app()


def main() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} was not found on PATH. Install ffmpeg first (https://ffmpeg.org/download.html).")
    if LANDSCAPE_FALLBACK not in ("skip", "crop"):
        sys.exit("LANDSCAPE_FALLBACK in .env must be 'skip' or 'crop'.")
    if HOST not in LOOPBACK and not PASSWORD:
        sys.exit("HOST makes this app reachable from other devices, so set APP_PASSWORD in .env first.")
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    shown = "localhost" if HOST in LOOPBACK else HOST
    print(f"\n  Upright is running. Open  http://{shown}:{PORT}\n  Press Ctrl+C to stop (active recordings are saved first).\n")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
