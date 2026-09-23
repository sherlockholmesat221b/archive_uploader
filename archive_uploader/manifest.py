"""
Human-browsable record of finished releases: IA identifier/URL, mega
account/path/link, UPC, Qobuz id, size, when it finished.

Deliberately separate from state/store.py's upload-tracking SQLite, which
exists purely so upload_release() can answer "is this already uploaded,
skip re-checking IA" -- that db's schema and row lifecycle are tuned for
that one job, its file_manifest rows get replaced wholesale on each write
(see daemon/stages.py's QobuzStages docstring), and it's not meant to be
queried by anything else. This one is: append-mostly, one row per release,
kept even after state/store.py's own bookkeeping is long past caring.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import STATE_DIR

DB_PATH = STATE_DIR / "manifest.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases(
  identifier   TEXT PRIMARY KEY,
  title        TEXT DEFAULT '',
  artist       TEXT DEFAULT '',
  ia_url       TEXT DEFAULT '',
  upc          TEXT DEFAULT '',
  qobuz_id     TEXT DEFAULT '',
  mega_account TEXT,
  mega_path    TEXT,
  mega_link    TEXT,
  bytes        INTEGER DEFAULT 0,
  finished_at  REAL
);
CREATE INDEX IF NOT EXISTS releases_qobuz ON releases(qobuz_id);
CREATE INDEX IF NOT EXISTS releases_upc   ON releases(upc);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH))
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


def _row(cur, r) -> Dict[str, Any]:
    return dict(zip([d[0] for d in cur.description], r))


def record(identifier: str, *, title: str = "", artist: str = "", upc: str = "",
          qobuz_id: str = "", bytes_: int = 0) -> None:
    """Upsert the IA side of a release. Called once, from finalize()."""
    c = _conn()
    try:
        c.execute(
            """INSERT INTO releases(identifier, title, artist, ia_url, upc, qobuz_id,
                                    bytes, finished_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(identifier) DO UPDATE SET
                 title=excluded.title, artist=excluded.artist, ia_url=excluded.ia_url,
                 upc=CASE WHEN excluded.upc!='' THEN excluded.upc ELSE releases.upc END,
                 qobuz_id=CASE WHEN excluded.qobuz_id!='' THEN excluded.qobuz_id ELSE releases.qobuz_id END,
                 bytes=CASE WHEN excluded.bytes>0 THEN excluded.bytes ELSE releases.bytes END""",
            (identifier, title, artist, f"https://archive.org/details/{identifier}",
             upc, qobuz_id, bytes_, time.time()),
        )
        c.commit()
    finally:
        c.close()


def update_mega(identifier: str, *, mega_account: Optional[str] = None,
                mega_path: Optional[str] = None, mega_link: Optional[str] = None) -> None:
    """Called once, from the mega stage, after a successful backup (and,
    when configured, a successful link export). Creates a bare row if IA's
    finalize() hasn't run yet (e.g. mega finished first in some ordering) --
    record() later fills in the IA fields without clobbering these."""
    c = _conn()
    try:
        c.execute(
            """INSERT INTO releases(identifier, mega_account, mega_path, mega_link, finished_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(identifier) DO UPDATE SET
                 mega_account=COALESCE(excluded.mega_account, releases.mega_account),
                 mega_path=COALESCE(excluded.mega_path, releases.mega_path),
                 mega_link=COALESCE(excluded.mega_link, releases.mega_link)""",
            (identifier, mega_account, mega_path, mega_link, time.time()),
        )
        c.commit()
    finally:
        c.close()


def get(identifier: str) -> Optional[Dict[str, Any]]:
    c = _conn()
    try:
        cur = c.execute("SELECT * FROM releases WHERE identifier=?", (identifier,))
        row = cur.fetchone()
        return _row(cur, row) if row else None
    finally:
        c.close()


def all_rows(limit: int = 500) -> List[Dict[str, Any]]:
    c = _conn()
    try:
        cur = c.execute("SELECT * FROM releases ORDER BY finished_at DESC LIMIT ?", (limit,))
        return [_row(cur, r) for r in cur.fetchall()]
    finally:
        c.close()
