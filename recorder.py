"""ffmpeg recording of one stream, with a live preview frame and a clean stop."""
from __future__ import annotations

import asyncio
import collections
import time
from datetime import datetime
from pathlib import Path

from tiktok import FFMPEG_HEADERS, UA, Stream

# 9:16 center crop with an even width (libx264 needs even dimensions)
CROP_FILTER = "crop=trunc(ih*9/16/2)*2:ih"
PREVIEW_FILTER = "fps=1/3,scale=360:-2"  # one small frame every 3 seconds


class RecorderError(Exception):
    pass


class Recording:
    def __init__(
        self,
        username: str,
        stream: Stream,
        crop: bool,
        out_dir: Path,
        max_seconds: int = 0,
        preview_path: Path | None = None,
    ):
        self.username = username
        self.stream = stream
        self.crop = crop
        self.max_seconds = max_seconds
        self.preview_path = preview_path

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir = Path(out_dir) / username
        self.raw_path = self.dir / f"{username}_{stamp}.mkv"
        self.final_path = self.raw_path.with_suffix(".mp4")

        self.started_at = time.monotonic()
        self.ended_at: float | None = None
        self.tail: collections.deque[str] = collections.deque(maxlen=20)
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None

    @property
    def duration(self) -> float:
        return (self.ended_at or time.monotonic()) - self.started_at

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc else None

    def tail_text(self) -> str:
        return "\n".join(self.tail)[-1500:]

    def _build_cmd(self) -> list[str]:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]
        if self.stream.url.startswith("http"):
            cmd += [
                "-user_agent", UA, "-headers", FFMPEG_HEADERS,
                "-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5", "-rw_timeout", "15000000",
            ]
        if self.max_seconds:
            cmd += ["-t", str(self.max_seconds)]  # input option: caps every output
        cmd += ["-i", self.stream.url]

        # Output 1: the recording itself
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
        if self.crop:
            cmd += [
                "-vf", CROP_FILTER, "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "copy",
            ]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-f", "matroska", str(self.raw_path)]

        # Output 2: a small JPEG that is overwritten every few seconds
        if self.preview_path:
            vf = f"{CROP_FILTER},{PREVIEW_FILTER}" if self.crop else PREVIEW_FILTER
            cmd += [
                "-map", "0:v:0", "-an", "-vf", vf, "-q:v", "5",
                "-f", "image2", "-update", "1", "-atomic_writing", "1",
                str(self.preview_path),
            ]
        return cmd

    async def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.preview_path:
            self.preview_path.parent.mkdir(parents=True, exist_ok=True)
            self.preview_path.unlink(missing_ok=True)
        self._proc = await asyncio.create_subprocess_exec(
            *self._build_cmd(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self.started_at = time.monotonic()

        await asyncio.sleep(2.5)  # catch an immediate failure (bad URL, 403, ...)
        if self._proc.returncode not in (None, 0):
            await self._stderr_task
            raise RecorderError(f"ffmpeg exited right away:\n{self.tail_text()}")

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        async for line in self._proc.stderr:
            self.tail.append(line.decode(errors="replace").rstrip())

    async def stop(self) -> None:
        """Ask ffmpeg to finish cleanly ('q' on stdin works on every OS)."""
        proc = self._proc
        if not proc or proc.returncode is not None:
            return
        try:
            assert proc.stdin
            proc.stdin.write(b"q")
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), 20)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    async def wait(self) -> Path | None:
        """Wait for ffmpeg to end, then return the finished file (or None)."""
        assert self._proc
        await self._proc.wait()
        if self._stderr_task:
            await self._stderr_task
        self.ended_at = time.monotonic()
        return await self._finalize()

    async def _finalize(self) -> Path | None:
        if not self.raw_path.exists() or self.raw_path.stat().st_size < 1024:
            self.raw_path.unlink(missing_ok=True)
            return None
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(self.raw_path), "-c", "copy", "-movflags", "+faststart",
            str(self.final_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        if await proc.wait() == 0 and self.final_path.exists():
            self.raw_path.unlink(missing_ok=True)
            return self.final_path
        self.final_path.unlink(missing_ok=True)
        return self.raw_path  # keep the .mkv if the mp4 remux failed
