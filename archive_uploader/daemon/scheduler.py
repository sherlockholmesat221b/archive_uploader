"""
Lane scheduler. Every Part flows download -> opus -> upload -> mega, and each
stage is its own lane with its own worker threads, so stages of different
Parts/releases overlap (IA uploads Part 2 while Part 3 downloads and Part 1
is on mega).

Rules, all configurable in settings (defaults are placeholders, not tuned):
  priority   0 now | 1 high | 2 normal | 3 low; lower runs first in every lane.
  NOW (0)    gets an extra "express" worker in every lane except upload, ignores
             pauses/breaks/prefetch limits, and preempts at the next Part boundary
             on IA. In the upload lane it waits only for the upload in progress.
  IA lane    one upload at a time; prefers the release it is already working on
             (unless a tier <= ia_preempt_max_priority job is waiting).
  containers artist/folder jobs create at most `window` open child albums at a time.
  prefetch   download lane is bounded by max_ahead per release and max_staged_bytes.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
import traceback
from collections import Counter, deque
from typing import Any, Dict, List, Optional

from .db import DB, FINAL
from .stages import PlanResult, Stages

GB = 1024 ** 3
STAGE_ORDER = ["download", "opus", "upload", "mega"]
LANES = ["plan", "download", "opus", "upload", "mega"]


def default_settings() -> Dict[str, Any]:
    cpu = os.cpu_count() or 2
    return {
        "lanes": {
            "plan":     {"cap": 1, "express": 1},
            "download": {"cap": 2, "express": 1},
            "opus":     {"cap": max(1, cpu // 2), "express": 1},
            "upload":   {"cap": 1, "express": 0},
            "mega":     {"cap": 1, "express": 1},
        },
        "artist_window": 2,             # open child albums per artist/folder job
        "max_ahead": 2,                 # Parts of one release staged at once
        "max_staged_bytes": 30 * GB,    # est. bytes staged on disk across releases
        "min_free_bytes": 3 * GB,       # never start a download that leaves less free
        "stage_path": ".",              # where free space is measured
        "ia_preempt_max_priority": 0,   # priorities <= this may interrupt a running release
        "max_retries": 3,
        "retry_base_s": 60,
        "tick_s": 30,                   # stats window written to the ticks table
        "auto_break": {"after_bytes": 0, "after_s": 0, "break_s": 0},   # IA lane; 0 = off
        "default_opts": {"opus": True, "mega": False},
    }


def _merge(a: dict, b: dict) -> dict:
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _merge(a[k], v)
        else:
            a[k] = v
    return a


class Ctx:
    def __init__(self, sch: "Scheduler", lane: str, job: dict, part: Optional[dict], express: bool):
        self.sch, self.lane, self.job, self.part, self.express = sch, lane, job, part, express
        self.bytes = 0
        self.t0 = time.time()
        self.first = False
        self.key = ("p", part["id"]) if part else ("j", job["id"], lane)

    @property
    def settings(self):
        return self.sch.settings

    def progress(self, n: int) -> None:
        self.bytes += int(n)
        self.sch._count(self.lane, int(n))

    def event(self, kind: str, detail: str = "") -> None:
        self.sch.db.add_event(self.lane, kind, self.job["id"], detail)

    def cancelled(self) -> bool:
        return self.job["id"] in self.sch.cancelled

    def log(self, msg: str) -> None:
        print(f"[{self.lane} job {self.job['id']}] {msg}", flush=True)


class Scheduler:
    def __init__(self, db: DB, stages: Stages, settings: Optional[dict] = None):
        self.db, self.stages = db, stages
        self.settings = default_settings()
        _merge(self.settings, db.kv_get("settings", {}))
        if settings:
            _merge(self.settings, settings)
        self.cv = threading.Condition(threading.RLock())
        self.running: Dict[str, Dict[Any, Ctx]] = {l: {} for l in LANES}
        self.counters = {l: 0 for l in LANES}
        self.totals = {l: 0 for l in LANES}
        self.win = {l: deque(maxlen=60) for l in LANES}     # (ts, cumulative bytes)
        self.waiting: Dict[str, str] = {}
        self.cancelled: set = set()
        self.touched: set = set()        # lanes that finished a task since the last 1 s sample
        ls = db.kv_get("lanestate", {"paused": [], "break_until": {}})
        self.paused: set = set(ls.get("paused", []))
        self.break_until: Dict[str, float] = ls.get("break_until", {})
        self.ia_cur: Optional[int] = None
        self._ab_bytes, self._ab_since = 0, None
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    # ------------------------------------------------------------------ life
    def start(self) -> None:
        # anything mid-flight when we last died goes back to pending
        self.db.x("UPDATE parts SET status='pending' WHERE status='running'")
        self.db.x("UPDATE jobs SET status='queued' WHERE status='planning'")
        self.db.x("UPDATE jobs SET fin=1 WHERE fin=2")
        for lane in LANES:
            n = max(8, self.settings["lanes"][lane]["cap"])
            for i in range(n):
                self._spawn(lane, False, i)
            for i in range(2):
                self._spawn(lane, True, i)
        t = threading.Thread(target=self._ticker, name="ticker", daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        with self.cv:
            self.cv.notify_all()

    def _spawn(self, lane, express, i):
        t = threading.Thread(target=self._worker, args=(lane, express),
                             name=f"{lane}{'-x' if express else ''}{i}", daemon=True)
        t.start()
        self._threads.append(t)

    def wake(self) -> None:
        with self.cv:
            self.cv.notify_all()

    # ------------------------------------------------------------- user API
    @staticmethod
    def classify(ref: str):
        r = ref.strip()
        p = os.path.expanduser(r)
        if os.path.isdir(p):
            return "folder", p
        if r.startswith("artist:"):
            return "artist", r[7:].strip()
        if r.startswith("album:"):
            return "album", r[6:].strip()
        if "/artist/" in r or "/interpreter/" in r:
            return "artist", r
        return "album", r

    def add(self, refs: List[str], priority: int = 2, opts: Optional[dict] = None) -> List[dict]:
        o = dict(self.settings["default_opts"])
        o.update(opts or {})
        out = []
        for ref in refs:
            if not ref.strip():
                continue
            kind, r = self.classify(ref)
            dup = self.db.q1("SELECT id FROM jobs WHERE kind=? AND ref=? AND status NOT IN "
                             "('done','failed','cancelled')", (kind, r))
            if dup:
                out.append({"id": dup["id"], "ref": r, "duplicate": True})
                continue
            jid = self.db.add_job(kind, r, priority, o)
            out.append({"id": jid, "kind": kind, "ref": r})
        self.wake()
        return out

    def _tree(self, job_id: int) -> List[int]:
        ids, stack = [], [job_id]
        while stack:
            i = stack.pop()
            ids.append(i)
            stack += [r["id"] for r in self.db.q("SELECT id FROM jobs WHERE parent_id=?", (i,))]
        return ids

    def set_priority(self, job_id: int, prio: int) -> None:
        for i in self._tree(job_id):
            self.db.upd("jobs", i, priority=int(prio))
        self.wake()

    def cancel(self, job_id: int) -> None:
        for i in self._tree(job_id):
            j = self.db.get_job(i)
            if j and j["status"] not in FINAL:
                self.cancelled.add(i)
                self.db.upd("jobs", i, status="cancelled", finished=time.time())
        self.wake()

    def retry(self, job_id: int) -> None:
        for i in self._tree(job_id):
            j = self.db.get_job(i)
            if not j or j["status"] not in ("failed", "cancelled"):
                continue
            self.cancelled.discard(i)
            self.db.x("UPDATE parts SET status='pending', attempts=0, retry_at=0, error='' "
                      "WHERE job_id=? AND stage!='done'", (i,))
            has_parts = self.db.s("SELECT COUNT(*) FROM parts WHERE job_id=?", (i,))
            kind = j["kind"]
            st = "expanding" if kind in ("artist", "folder") and j["meta"].get("pending") is not None \
                else ("active" if has_parts else "queued")
            fin = 1 if j["fin"] == 2 else j["fin"]
            self.db.upd("jobs", i, status=st, attempts=0, retry_at=0, error="", fin=fin, finished=None)
        self.wake()

    def remove(self, job_id: int) -> bool:
        ids = self._tree(job_id)
        if any(self.db.get_job(i)["status"] not in FINAL for i in ids):
            return False
        for i in ids:
            self.db.x("DELETE FROM parts WHERE job_id=?", (i,))
            self.db.x("DELETE FROM jobs WHERE id=?", (i,))
        return True

    def _save_lanestate(self):
        self.db.kv_set("lanestate", {"paused": sorted(self.paused), "break_until": self.break_until})

    def set_pause(self, lane: str, on: bool) -> None:
        with self.cv:
            (self.paused.add if on else self.paused.discard)(lane)
            self._save_lanestate()
            self.cv.notify_all()

    def set_break(self, lane: str, seconds: float) -> None:
        with self.cv:
            self.break_until[lane] = time.time() + seconds if seconds > 0 else 0
            self._save_lanestate()
            self.db.add_event(lane, "break", None, f"{int(seconds)}s")
            self.cv.notify_all()

    def update_settings(self, patch: dict) -> None:
        with self.cv:
            _merge(self.settings, patch)
            self.db.kv_set("settings", self.settings)
            self.cv.notify_all()

    # ---------------------------------------------------------------- claim
    def _blocked(self, lane: str, prio: int, now: float) -> bool:
        if prio == 0:
            return False
        return (lane in self.paused or "all" in self.paused
                or self.break_until.get(lane, 0) > now
                or self.break_until.get("all", 0) > now)

    def _worker(self, lane: str, express: bool) -> None:
        while not self._stop.is_set():
            with self.cv:
                item = self._claim(lane, express)
                if item is None:
                    self.cv.wait(1.0)
                    continue
            self._execute(lane, *item)

    def _claim(self, lane: str, express: bool):
        now = time.time()
        L = self.settings["lanes"][lane]
        run = self.running[lane].values()
        if express:
            if sum(1 for c in run if c.express) >= L.get("express", 0):
                return None
        elif sum(1 for c in run if not c.express) >= L["cap"]:
            return None
        if lane == "plan":
            return self._claim_plan(express, now)

        rows = self.db.q(
            "SELECT p.*, j.priority AS prio FROM parts p JOIN jobs j ON j.id=p.job_id "
            "WHERE j.status='active' AND p.stage!='done'")
        cands = [p for p in rows if p["stage"] == lane and p["status"] == "pending"
                 and p["retry_at"] <= now and not self._blocked(lane, p["prio"], now)]
        fins = []
        if lane == "upload":
            fins = [j for j in self.db.q(
                "SELECT * FROM jobs WHERE status='active' AND fin=1 AND retry_at<=?", (now,))
                if not self._blocked(lane, j["priority"], now)]
        if express:
            cands = [p for p in cands if p["prio"] == 0]
            fins = [j for j in fins if j["priority"] == 0]
        self.waiting.pop(lane, None)
        if not cands and not fins:
            return None

        if lane == "upload":
            pm = self.settings["ia_preempt_max_priority"]
            tier = lambda pr: pr if pr <= pm else pm + 1
            stick = lambda jid: 0 if jid == self.ia_cur else 1
            ranked = sorted(
                [(tier(j["priority"]), 0, stick(j["id"]), j["priority"], j["id"], 0, ("fin", j)) for j in fins] +
                [(tier(p["prio"]), 1, stick(p["job_id"]), p["prio"], p["job_id"], p["idx"], ("part", p))
                 for p in cands], key=lambda t: t[:6])
            kind, obj = ranked[0][6]
        else:
            ranked = sorted(cands, key=lambda p: (p["prio"], p["job_id"], p["idx"]))
            obj, kind = None, "part"
            if lane == "download":
                obj = self._pick_download(ranked, rows, now)
            else:
                obj = ranked[0]
            if obj is None:
                return None

        if kind == "fin":
            job = obj
            self.db.upd("jobs", job["id"], fin=2)
            ctx = Ctx(self, "upload", job, None, express)
            self.running[lane][ctx.key] = ctx
            return ("finalize", job, None, ctx)
        job = self.db.get_job(obj["job_id"])
        self.db.x("UPDATE parts SET status='running', updated=? WHERE id=?", (now, obj["id"]))
        part = self.db.q1("SELECT * FROM parts WHERE id=?", (obj["id"],))
        ctx = Ctx(self, lane, job, part, express)
        if lane == "upload":
            ctx.first = self.db.s("SELECT COUNT(*) FROM parts WHERE job_id=? AND uploaded=1",
                                  (job["id"],)) == 0
            self.ia_cur = job["id"]
        self.running[lane][ctx.key] = ctx
        return ("part", job, part, ctx)

    def _pick_download(self, ranked, rows, now):
        s = self.settings
        staged = lambda p: p["stage"] in ("opus", "upload", "mega") or (
            p["stage"] == "download" and p["status"] == "running")
        sb = sum(p["est_bytes"] for p in rows if staged(p))
        per_job = Counter(p["job_id"] for p in rows if staged(p))
        try:
            free = shutil.disk_usage(s["stage_path"]).free
        except OSError:
            free = 1 << 60
        for p in ranked:
            now_ = p["prio"] == 0
            if not now_ and per_job[p["job_id"]] >= s["max_ahead"]:
                self.waiting["download"] = "prefetch limit (max_ahead)"
                continue
            if not now_ and sb > 0 and sb + p["est_bytes"] > s["max_staged_bytes"]:
                self.waiting["download"] = "staged-bytes budget"
                return None
            if free - p["est_bytes"] < s["min_free_bytes"]:
                self.waiting["download"] = "low disk space"
                return None
            return p
        return None

    def _claim_plan(self, express: bool, now: float):
        jobs = self.db.q("SELECT * FROM jobs WHERE status='queued' AND retry_at<=? "
                         "ORDER BY priority, id", (now,))
        jobs = [j for j in jobs if not self._blocked("plan", j["priority"], now)]
        if express:
            jobs = [j for j in jobs if j["priority"] == 0]
        if not jobs:
            return None
        job = jobs[0]
        self.db.upd("jobs", job["id"], status="planning", started=now)
        ctx = Ctx(self, "plan", job, None, express)
        self.running["plan"][ctx.key] = ctx
        return ("plan", job, None, ctx)

    # -------------------------------------------------------------- execute
    def _execute(self, lane, kind, job, part, ctx: Ctx) -> None:
        t0, ok, err = time.time(), True, ""
        try:
            if kind == "plan":
                self._do_plan(job, ctx)
            elif kind == "finalize":
                self.stages.finalize(job, ctx)
            else:
                ret = getattr(self.stages, lane)(job, part, ctx)
                if isinstance(ret, int) and ctx.bytes == 0:
                    ctx.progress(ret)
        except Exception as e:                                # noqa: BLE001
            ok, err = False, f"{type(e).__name__}: {e}"
            traceback.print_exc()
        t1 = time.time()
        with self.cv:
            self.running[lane].pop(ctx.key, None)
            self.touched.add(lane)
            stage = "finalize" if kind == "finalize" else lane
            if kind != "plan":
                self.db.add_sample(t0, t1, stage, job["id"], part["id"] if part else None,
                                   ctx.bytes, ok)
            try:
                (self._after_ok if ok else self._after_fail)(kind, lane, job, part, ctx, err)
                if job["id"] in self.cancelled and not any(
                        c.job["id"] == job["id"] for r in self.running.values() for c in r.values()):
                    self.stages.cleanup_job(job, ctx)         # cancelled and nothing still running
            except Exception:                                 # noqa: BLE001
                traceback.print_exc()
            self.cv.notify_all()

    # planning ------------------------------------------------------------
    def _do_plan(self, job, ctx) -> None:
        if job["kind"] in ("artist", "folder"):
            refs = self.stages.expand(job, ctx)
            meta = dict(job["meta"], pending=refs, total=len(refs), created=0)
            self.db.upd("jobs", job["id"], status="expanding", meta=meta,
                        title=job["title"] or job["ref"])
            return
        res: PlanResult = self.stages.plan(job, ctx)
        if res.skip:
            self.db.upd("jobs", job["id"], status="done", title=res.title,
                        meta=dict(res.meta, note=res.skip), finished=time.time())
            return
        opts = job["opts"]
        stage = self._first_stage(res.start_stage, opts)
        n = len(res.parts)
        for i, sp in enumerate(res.parts, 1):
            self.db.add_part(job["id"], i, n, sp.label, sp.est_bytes, sp.payload, stage)
        self.db.upd("jobs", job["id"], status="active", title=res.title,
                    meta=dict(job["meta"], **res.meta))

    # stage helpers ---------------------------------------------------------
    @staticmethod
    def _enabled(stage: str, opts: dict) -> bool:
        if stage == "opus":
            return bool(opts.get("opus", True))
        if stage == "mega":
            return bool(opts.get("mega", False))
        return True

    def _first_stage(self, start: str, opts: dict) -> str:
        for s in STAGE_ORDER[STAGE_ORDER.index(start):]:
            if self._enabled(s, opts):
                return s
        return "done"

    def _next_stage(self, cur: str, opts: dict) -> str:
        return next((s for s in STAGE_ORDER[STAGE_ORDER.index(cur) + 1:]
                     if self._enabled(s, opts)), "done")

    # completion ------------------------------------------------------------
    def _after_ok(self, kind, lane, job, part, ctx, err) -> None:
        now = time.time()
        if kind == "plan":
            return
        if kind == "finalize":
            self.db.upd("jobs", job["id"], fin=3, attempts=0)
            self._maybe_done(job["id"])
            return
        if lane == "download":
            self.db.upd("parts", part["id"], bytes=ctx.bytes)
        if lane == "upload":
            self.db.upd("parts", part["id"], uploaded=1)
            self._auto_break(ctx.bytes, now)
            left = self.db.s("SELECT COUNT(*) FROM parts WHERE job_id=? AND uploaded=0", (job["id"],))
            if left == 0 and self.db.get_job(job["id"])["fin"] == 0:
                self.db.upd("jobs", job["id"], fin=1)
        nxt = self._next_stage(lane, job["opts"])
        if nxt == "done":
            try:
                self.stages.cleanup_part(job, part, ctx)
            except Exception:                                 # noqa: BLE001
                traceback.print_exc()
        self.db.upd("parts", part["id"], stage=nxt, status="pending", attempts=0,
                    error="", updated=now)
        self._maybe_done(job["id"])

    def _after_fail(self, kind, lane, job, part, ctx, err) -> None:
        s, now = self.settings, time.time()
        if job["id"] in self.cancelled:
            return
        if kind == "part":
            att = part["attempts"] + 1
            retry = att < s["max_retries"]
            self.db.upd("parts", part["id"], status="pending" if retry else "failed", attempts=att,
                        retry_at=now + s["retry_base_s"] * 2 ** (att - 1), error=err, updated=now)
            self.db.add_event(lane, "retry" if retry else "fail", job["id"], err)
            if not retry:
                self.db.upd("jobs", job["id"], status="failed", error=err, finished=now)
            return
        att = job["attempts"] + 1                            # plan / finalize
        retry = att < s["max_retries"]
        f = {"attempts": att, "error": err, "retry_at": now + s["retry_base_s"] * 2 ** (att - 1)}
        if kind == "plan":
            f["status"] = "queued" if retry else "failed"
        else:
            f["fin"] = 1
            if not retry:
                f["status"] = "failed"
        if not retry:
            f["finished"] = now
        self.db.upd("jobs", job["id"], **f)
        self.db.add_event(lane, "retry" if retry else "fail", job["id"], err)

    def _maybe_done(self, job_id: int) -> None:
        j = self.db.get_job(job_id)
        if not j or j["status"] != "active" or j["fin"] != 3:
            return
        if self.db.s("SELECT COUNT(*) FROM parts WHERE job_id=? AND stage!='done'", (job_id,)):
            return
        self.db.upd("jobs", job_id, status="done", finished=time.time())
        try:
            self.stages.cleanup_job(j, Ctx(self, "cleanup", j, None, False))
        except Exception:                                     # noqa: BLE001
            traceback.print_exc()

    def _auto_break(self, nbytes: int, now: float) -> None:
        ab = self.settings["auto_break"]
        if not ab.get("break_s"):
            return
        self._ab_bytes += nbytes
        self._ab_since = self._ab_since or now
        if (ab.get("after_bytes") and self._ab_bytes >= ab["after_bytes"]) or \
           (ab.get("after_s") and now - self._ab_since >= ab["after_s"]):
            self.break_until["upload"] = now + ab["break_s"]
            self._ab_bytes, self._ab_since = 0, None
            self._save_lanestate()
            self.db.add_event("upload", "auto_break", None, f"{ab['break_s']}s")

    # -------------------------------------------------- containers / ticker
    def _feed(self) -> None:
        for c in self.db.q("SELECT * FROM jobs WHERE status='expanding'"):
            pending = c["meta"].get("pending") or []
            window = int(c["opts"].get("window") or self.settings["artist_window"])
            open_ = self.db.s("SELECT COUNT(*) FROM jobs WHERE parent_id=? AND status NOT IN "
                              "('done','failed','cancelled')", (c["id"],))
            made = 0
            while pending and open_ < window:
                ch = pending.pop(0)
                self.db.add_job(ch["kind"], ch["ref"], c["priority"], c["opts"], c["id"],
                                ch.get("title", ""))
                open_ += 1
                made += 1
            if made:
                meta = dict(c["meta"], pending=pending, created=c["meta"].get("created", 0) + made)
                self.db.upd("jobs", c["id"], meta=meta)
            if not pending and open_ == 0:
                self.db.upd("jobs", c["id"], status="done", finished=time.time())
        self.wake()

    def _count(self, lane: str, n: int) -> None:
        with self.cv:
            self.counters[lane] += n
            self.totals[lane] += n

    def _ticker(self) -> None:
        last = time.time()
        acc = {l: [0, 0.0, 0] for l in LANES}     # bytes, busy seconds, max active
        seen = {l: 0 for l in LANES}
        flush_at = last + self.settings["tick_s"]
        while not self._stop.wait(1.0):
            now = time.time()
            dt, last = now - last, now
            try:
                with self.cv:
                    for l in LANES:
                        b = self.counters[l] - seen[l]
                        seen[l] = self.counters[l]
                        n_act = len(self.running[l])
                        a = acc[l]
                        a[0] += b
                        a[1] += dt if (n_act or l in self.touched) else 0.0
                        a[2] = max(a[2], n_act)
                        self.win[l].append((now, self.counters[l]))
                    self.touched.clear()
                    if now >= flush_at:
                        for l in LANES:
                            a = acc[l]
                            if a[0] or a[1]:
                                self.db.add_tick(now, l, a[0], a[2], a[1])
                            acc[l] = [0, 0.0, 0]
                        flush_at = now + self.settings["tick_s"]
                self._feed()
            except Exception:                                 # noqa: BLE001
                traceback.print_exc()

    # ------------------------------------------------------------- snapshot
    def _mbps(self, lane: str) -> float:
        w = self.win[lane]
        if len(w) < 2:
            return 0.0
        (t0, b0), (t1, b1) = w[0], w[-1]
        return (b1 - b0) / (t1 - t0) / 1e6 if t1 > t0 else 0.0

    def snapshot(self) -> dict:
        now = time.time()
        lanes = {}
        with self.cv:
            for l in LANES:
                L = self.settings["lanes"][l]
                run = []
                for c in self.running[l].values():
                    el = now - c.t0
                    run.append({"job_id": c.job["id"], "title": c.job["title"] or c.job["ref"],
                                "part": c.part["label"] if c.part else ("finalize" if l == "upload" else ""),
                                "bytes": c.bytes, "elapsed": el, "express": c.express,
                                "mbps": c.bytes / el / 1e6 if el > 0 else 0})
                lanes[l] = {"cap": L["cap"], "express": L.get("express", 0), "running": run,
                            "paused": l in self.paused or "all" in self.paused,
                            "break_until": self.break_until.get(l, 0),
                            "waiting": self.waiting.get(l, ""), "mbps_1m": self._mbps(l),
                            "total_bytes": self.totals[l]}
        jobs = self.db.q("SELECT * FROM jobs WHERE status NOT IN ('done','failed','cancelled') "
                         "OR finished>? ORDER BY id DESC LIMIT 400", (now - 6 * 3600,))
        agg = {r["job_id"]: r for r in self.db.q(
            "SELECT job_id, COUNT(*) n, SUM(stage='done') done, SUM(uploaded) up, "
            "SUM(est_bytes) est, SUM(bytes) got FROM parts GROUP BY job_id")}
        stg = {}
        for r in self.db.q("SELECT job_id, stage, status, COUNT(*) c FROM parts "
                           "WHERE stage!='done' GROUP BY job_id, stage, status"):
            stg.setdefault(r["job_id"], []).append(f"{r['stage']}{'*' if r['status']=='running' else ''}:{r['c']}")
        for j in jobs:
            a = agg.get(j["id"], {})
            j["parts_total"], j["parts_done"], j["parts_uploaded"] = a.get("n", 0), a.get("done", 0), a.get("up", 0)
            j["est_bytes"], j["got_bytes"] = a.get("est", 0), a.get("got", 0)
            j["where"] = " ".join(stg.get(j["id"], []))
            m = j["meta"]
            j["children_left"] = len(m.get("pending") or []) if j["kind"] in ("artist", "folder") else 0
            j["meta"] = {k: v for k, v in m.items() if k != "pending"}
        try:
            du = shutil.disk_usage(self.settings["stage_path"])
            disk = {"free": du.free, "total": du.total}
        except OSError:
            disk = {}
        return {"now": now, "lanes": lanes, "jobs": jobs, "disk": disk,
                "settings": self.settings, "paused_all": "all" in self.paused}
