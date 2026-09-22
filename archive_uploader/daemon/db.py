"""queue.db: jobs, parts, raw throughput stats. One connection, one lock.

Stats are stored RAW (per-unit samples + periodic byte ticks + free-form
events). Nothing here smooths, averages or decides anything; trends (when IA
is slow, which hours are fast) are for you to read off the dashboard.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,             -- album | local | artist | folder
  ref TEXT NOT NULL,
  title TEXT DEFAULT '',
  priority INTEGER DEFAULT 2,     -- 0 now, 1 high, 2 normal, 3 low
  status TEXT DEFAULT 'queued',   -- queued planning active expanding done failed cancelled
  parent_id INTEGER,
  opts TEXT DEFAULT '{}',
  meta TEXT DEFAULT '{}',
  fin INTEGER DEFAULT 0,          -- release finalize: 0 not ready, 1 pending, 2 running, 3 done
  error TEXT DEFAULT '',
  attempts INTEGER DEFAULT 0,
  retry_at REAL DEFAULT 0,
  created REAL, started REAL, finished REAL
);
CREATE TABLE IF NOT EXISTS parts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  idx INTEGER, total INTEGER, label TEXT,
  est_bytes INTEGER DEFAULT 0, bytes INTEGER DEFAULT 0,
  payload TEXT DEFAULT '{}',
  stage TEXT DEFAULT 'download',  -- download opus upload mega done
  status TEXT DEFAULT 'pending',  -- pending running failed
  uploaded INTEGER DEFAULT 0,
  attempts INTEGER DEFAULT 0, retry_at REAL DEFAULT 0, error TEXT DEFAULT '',
  updated REAL
);
CREATE INDEX IF NOT EXISTS parts_job ON parts(job_id);
CREATE TABLE IF NOT EXISTS samples(   -- one row per finished unit of work
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  t0 REAL, t1 REAL, stage TEXT, job_id INTEGER, part_id INTEGER, bytes INTEGER, ok INTEGER
);
CREATE INDEX IF NOT EXISTS samples_t ON samples(t1);
CREATE TABLE IF NOT EXISTS ticks(     -- periodic counters: bytes moved in a window, seconds the lane was busy
  ts REAL, stage TEXT, bytes INTEGER, active INTEGER, dt REAL
);
CREATE INDEX IF NOT EXISTS ticks_ts ON ticks(ts);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, stage TEXT, kind TEXT, job_id INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""

_JSON = ("opts", "meta", "payload")
FINAL = ("done", "failed", "cancelled")


class DB:
    def __init__(self, path: str = ":memory:"):
        self.path = str(path)
        self._c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._c.row_factory = sqlite3.Row
        self._l = threading.RLock()
        with self._l:
            self._c.executescript(SCHEMA)

    # ---- primitives ------------------------------------------------------
    @staticmethod
    def _dec(r: Dict[str, Any]) -> Dict[str, Any]:
        for k in _JSON:
            if k in r and isinstance(r[k], str):
                try:
                    r[k] = json.loads(r[k] or "{}")
                except ValueError:
                    r[k] = {}
        return r

    def q(self, sql: str, a=()) -> List[Dict[str, Any]]:
        with self._l:
            return [self._dec(dict(r)) for r in self._c.execute(sql, a).fetchall()]

    def q1(self, sql: str, a=()) -> Optional[Dict[str, Any]]:
        rows = self.q(sql, a)
        return rows[0] if rows else None

    def s(self, sql: str, a=()):
        with self._l:
            r = self._c.execute(sql, a).fetchone()
            return r[0] if r else None

    def x(self, sql: str, a=()) -> int:
        with self._l:
            return self._c.execute(sql, a).lastrowid

    def upd(self, table: str, id_: int, **f) -> None:
        if not f:
            return
        for k in _JSON:
            if k in f and not isinstance(f[k], str):
                f[k] = json.dumps(f[k])
        sets = ",".join(f"{k}=?" for k in f)
        self.x(f"UPDATE {table} SET {sets} WHERE id=?", (*f.values(), id_))

    # ---- kv --------------------------------------------------------------
    def kv_get(self, k: str, default=None):
        v = self.s("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(v) if v else default

    def kv_set(self, k: str, v) -> None:
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (k, json.dumps(v)))

    # ---- jobs / parts ----------------------------------------------------
    def add_job(self, kind, ref, priority=2, opts=None, parent_id=None, title="") -> int:
        return self.x(
            "INSERT INTO jobs(kind,ref,title,priority,opts,parent_id,created) VALUES(?,?,?,?,?,?,?)",
            (kind, ref, title, priority, json.dumps(opts or {}), parent_id, time.time()))

    def get_job(self, id_: int):
        return self.q1("SELECT * FROM jobs WHERE id=?", (id_,))

    def add_part(self, job_id, idx, total, label, est_bytes, payload, stage) -> int:
        return self.x(
            "INSERT INTO parts(job_id,idx,total,label,est_bytes,payload,stage,updated) VALUES(?,?,?,?,?,?,?,?)",
            (job_id, idx, total, label, est_bytes, json.dumps(payload or {}), stage, time.time()))

    def parts(self, job_id: int):
        return self.q("SELECT * FROM parts WHERE job_id=? ORDER BY idx", (job_id,))

    # ---- stats (raw) -----------------------------------------------------
    def add_sample(self, t0, t1, stage, job_id, part_id, nbytes, ok):
        self.x("INSERT INTO samples(t0,t1,stage,job_id,part_id,bytes,ok) VALUES(?,?,?,?,?,?,?)",
               (t0, t1, stage, job_id, part_id, int(nbytes), int(ok)))

    def add_tick(self, ts, stage, nbytes, active, dt):
        self.x("INSERT INTO ticks(ts,stage,bytes,active,dt) VALUES(?,?,?,?,?)",
               (ts, stage, int(nbytes), int(active), float(dt)))

    def add_event(self, stage, kind, job_id=None, detail=""):
        self.x("INSERT INTO events(ts,stage,kind,job_id,detail) VALUES(?,?,?,?,?)",
               (time.time(), stage, kind, job_id, str(detail)[:500]))

    def series(self, stage: str, since: float, bucket: int):
        """Throughput over time. mbps = bytes / seconds-the-lane-was-busy (MB = 1e6 B)."""
        rows = self.q(
            "SELECT CAST(ts/? AS INTEGER)*? AS t, SUM(bytes) b, SUM(dt) d, MAX(active) a "
            "FROM ticks WHERE stage=? AND ts>=? GROUP BY 1 ORDER BY 1",
            (bucket, bucket, stage, since))
        return [{"t": r["t"], "bytes": r["b"], "busy_s": r["d"],
                 "mbps": (r["b"] / r["d"] / 1e6) if r["d"] else 0.0, "active": r["a"]} for r in rows]

    def cycle(self, stage: str, since: float, mode: str = "hod"):
        """Same measure grouped by hour-of-day ('%H') or weekday ('%w'), local time."""
        fmt = "%H" if mode == "hod" else "%w"
        rows = self.q(
            "SELECT strftime(?, ts,'unixepoch','localtime') k, SUM(bytes) b, SUM(dt) d, COUNT(*) n "
            "FROM ticks WHERE stage=? AND ts>=? AND dt>0 GROUP BY 1 ORDER BY 1", (fmt, stage, since))
        return [{"k": r["k"], "mbps": (r["b"] / r["d"] / 1e6) if r["d"] else 0.0,
                 "busy_s": r["d"], "n": r["n"]} for r in rows]

    def totals(self, since: float):
        rows = self.q(
            "SELECT stage, COUNT(*) n, SUM(ok) ok, SUM(bytes) b, SUM(t1-t0) busy "
            "FROM samples WHERE t1>=? GROUP BY stage", (since,))
        return [{"stage": r["stage"], "units": r["n"], "ok": r["ok"], "bytes": r["b"] or 0,
                 "busy_s": r["busy"] or 0,
                 "mbps": ((r["b"] or 0) / r["busy"] / 1e6) if r["busy"] else 0.0} for r in rows]

    def events(self, limit=100, since=0.0):
        return self.q("SELECT * FROM events WHERE ts>=? ORDER BY id DESC LIMIT ?", (since, limit))
