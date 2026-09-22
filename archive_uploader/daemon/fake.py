"""Sleep-based stand-in for the real stages: lets you watch the scheduler and
dashboard work without Qobuz/IA. `speed` scales all durations (higher = faster)."""
from __future__ import annotations

import random
import threading
import time

from .stages import PartSpec, PlanResult, Stages

MB = 1_000_000


class FakeStages(Stages):
    def __init__(self, speed: float = 1.0, parts_per_album=None, mb_per_part: int = 400,
                 artist_albums: int = 8, fail: set | None = None):
        self.speed, self.mb = speed, mb_per_part
        self.ppa = parts_per_album or (lambda ref: 1)
        self.artist_albums = artist_albums
        self.fail = fail or set()
        self.log: list = []            # (t, lane, job_id, part_idx, "start"|"end")
        self._l = threading.Lock()

    def _rec(self, lane, job, part, what):
        with self._l:
            self.log.append((time.time(), lane, job["id"], part["idx"] if part else 0, what))

    def _work(self, lane, job, part, ctx, mb_per_s, jitter=0.2):
        self._rec(lane, job, part, "start")
        total = self.mb * MB
        done, step = 0, 0.05
        rate = mb_per_s * MB * self.speed * (1 + random.uniform(-jitter, jitter))
        while done < total:
            if ctx.cancelled():
                break
            time.sleep(step)
            n = min(int(rate * step), total - done)
            done += n
            ctx.progress(n)
        if (job["ref"], part["idx"] if part else 0, lane) in self.fail:
            self.fail.discard((job["ref"], part["idx"] if part else 0, lane))
            raise RuntimeError("simulated failure")
        self._rec(lane, job, part, "end")

    def expand(self, job, ctx):
        n = self.artist_albums
        return [{"kind": "album", "ref": f"{job['ref']}-alb{i}", "title": f"Album {i}"} for i in range(1, n + 1)]

    def plan(self, job, ctx):
        n = self.ppa(job["ref"])
        return PlanResult(title=f"Album {job['ref']}",
                          parts=[PartSpec(f"Part {i}/{n}", self.mb * MB) for i in range(1, n + 1)])

    def download(self, job, part, ctx): self._work("download", job, part, ctx, 40)
    def opus(self, job, part, ctx): self._work("opus", job, part, ctx, 120)
    def upload(self, job, part, ctx): self._work("upload", job, part, ctx, 20)
    def mega(self, job, part, ctx): self._work("mega", job, part, ctx, 30)

    def finalize(self, job, ctx):
        self._rec("finalize", job, None, "start")
        time.sleep(0.02)
        self._rec("finalize", job, None, "end")
