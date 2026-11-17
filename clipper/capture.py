"""Per-channel live capture into a rolling ring buffer of MPEG-TS segments.

streamlink -> (pipe) -> ffmpeg -c copy segmenter. No re-encoding, so each extra streamer costs
only bandwidth and a few % of one core. Every finished segment is stamped with local wall-clock
time so chat timestamps (same clock) map directly onto video.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import shutil
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Segment:
    path: Path
    wall_start: float
    wall_end: float


class WallClockMapper:
    """Map stream PTS to wall time. Segments arrive in bursts, so delivery time only ever *adds*
    delay: the minimum of (arrival - pts_end) over a sliding window is the best offset estimate."""

    def __init__(self, window: int = 15, reset_jump_s: float = 8.0):
        self.window: deque[float] = deque(maxlen=window)
        self.reset_jump_s = reset_jump_s

    def observe(self, pts_end: float, arrival: float) -> float:
        off = arrival - pts_end
        if self.window and abs(off - min(self.window)) > self.reset_jump_s:
            self.window.clear()  # PTS discontinuity (ad break, reconnect)
        self.window.append(off)
        return min(self.window)


class RingBuffer:
    def __init__(self, directory: Path, keep_seconds: float):
        self.dir = directory
        self.keep_seconds = keep_seconds
        self.segments: deque[Segment] = deque()
        self._holds: dict[int, tuple[float, float]] = {}
        self._hold_id = 0

    @property
    def latest_wall(self) -> float:
        return self.segments[-1].wall_end if self.segments else 0.0

    @property
    def earliest_wall(self) -> float:
        return self.segments[0].wall_start if self.segments else 0.0

    def add(self, seg: Segment) -> None:
        self.segments.append(seg)
        self.prune()

    def hold(self, t0: float, t1: float) -> int:
        """Protect a time range from pruning until release() (a pending clip needs it)."""
        self._hold_id += 1
        self._holds[self._hold_id] = (t0, t1)
        return self._hold_id

    def release(self, hold_id: int) -> None:
        self._holds.pop(hold_id, None)

    def prune(self) -> None:
        if not self.segments:
            return
        cutoff = self.segments[-1].wall_end - self.keep_seconds
        min_hold = min((a for a, _ in self._holds.values()), default=float("inf"))
        while self.segments and self.segments[0].wall_end < min(cutoff, min_hold):
            seg = self.segments.popleft()
            try:
                seg.path.unlink(missing_ok=True)
            except OSError:
                pass

    def covering(self, t0: float, t1: float) -> list[Segment]:
        return [s for s in self.segments if s.wall_end > t0 and s.wall_start < t1]


async def extract(segments: list[Segment], out_path: Path) -> float:
    """Concatenate TS segments losslessly. Returns wall time of the output's t=0."""
    if not segments:
        raise ValueError("no segments in range")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat = "concat:" + "|".join(str(s.path) for s in segments)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", concat,
        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", str(out_path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"segment concat failed: {err.decode()[-500:]}")
    return segments[0].wall_start


class StreamCapture:
    def __init__(self, channel: str, cfg, base_dir: Path):
        self.channel = channel
        self.cfg = cfg
        self.dir = base_dir / channel
        self.buffer = RingBuffer(self.dir, cfg.buffer_seconds)
        self._stop = asyncio.Event()
        self._procs: list[asyncio.subprocess.Process] = []
        self.healthy = False

    async def run(self) -> None:
        if not shutil.which("streamlink"):
            raise RuntimeError("streamlink not found on PATH (pip install streamlink)")
        attempt = 0
        while not self._stop.is_set():
            run_dir = self.dir / f"run{int(time.time())}"
            run_dir.mkdir(parents=True, exist_ok=True)
            started = time.time()
            try:
                await self._run_once(run_dir)
            except Exception:
                log.exception("[%s] capture crashed", self.channel)
            self.healthy = False
            if self._stop.is_set():
                break
            attempt = 0 if time.time() - started > 120 else attempt + 1
            delay = self.cfg.restart_backoff_s[min(attempt, len(self.cfg.restart_backoff_s) - 1)]
            log.info("[%s] capture ended, restarting in %ss", self.channel, delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _run_once(self, run_dir: Path) -> None:
        index = run_dir / "index.csv"
        r, w = os.pipe()
        sl = await asyncio.create_subprocess_exec(
            "streamlink", *self.cfg.streamlink_args, "--stdout", f"https://twitch.tv/{self.channel}", self.cfg.quality,
            stdout=w, stderr=asyncio.subprocess.DEVNULL,
        )
        ff = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts+discardcorrupt", "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-f", "segment", "-segment_time", str(self.cfg.segment_seconds), "-segment_format", "mpegts",
            "-segment_list", str(index), "-segment_list_type", "csv", "-reset_timestamps", "0",
            str(run_dir / "seg_%07d.ts"),
            stdin=r, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        os.close(r)
        os.close(w)
        self._procs = [sl, ff]
        log.info("[%s] capture started", self.channel)
        tail = asyncio.create_task(self._tail_index(index, run_dir))
        try:
            await ff.wait()
        finally:
            tail.cancel()
            for p in self._procs:
                if p.returncode is None:
                    p.terminate()
            for p in self._procs:
                try:
                    await asyncio.wait_for(p.wait(), 5)
                except asyncio.TimeoutError:
                    p.kill()
            self._procs = []

    async def _tail_index(self, index: Path, run_dir: Path) -> None:
        mapper = WallClockMapper()
        pos = 0
        pending = ""
        while True:
            await asyncio.sleep(0.25)
            if not index.exists():
                continue
            with index.open() as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            pending += chunk
            *lines, pending = pending.split("\n")
            now = time.time()
            for row in csv.reader(lines):
                if len(row) < 3:
                    continue
                name, pts_start, pts_end = row[0], float(row[1]), float(row[2])
                off = mapper.observe(pts_end, now)
                self.buffer.add(Segment(run_dir / name, pts_start + off, pts_end + off))
                self.healthy = True

    async def stop(self) -> None:
        self._stop.set()
        for p in self._procs:
            if p.returncode is None:
                p.terminate()

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
