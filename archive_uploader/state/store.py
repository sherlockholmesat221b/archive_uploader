"""
SQLite State Store compatible with original monolith schema (uploader.db).
Supports 'uploads' and 'file_manifest' tables with json dumps & fingerprinting.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Union

try:
    from mutagen.flac import FLAC
except ImportError:
    FLAC = None

STATE_DIR = Path.home() / ".config" / "archive_uploader"
DEFAULT_DB_FILE = STATE_DIR / "uploader.db"


class SQLiteStateStore:
    def __init__(self, db_path: Optional[Union[str, Path]] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_FILE
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextlib.contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                conn.execute("PRAGMA foreign_keys = ON;")
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS uploads (
                    identifier TEXT PRIMARY KEY,
                    upc TEXT,
                    qobuz_id TEXT,
                    artist TEXT,
                    title TEXT,
                    status TEXT,
                    script_version TEXT,
                    script_hash TEXT,
                    qobuz_raw_json TEXT,
                    mb_raw_json TEXT,
                    wiki_raw_json TEXT,
                    ia_payload_json TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS file_manifest (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    identifier TEXT,
                    file_name TEXT,
                    file_size INTEGER,
                    md5_hash TEXT,
                    sha256_hash TEXT,
                    audio_spec TEXT,
                    raw_flac_tags_json TEXT,
                    status TEXT,
                    FOREIGN KEY(identifier) REFERENCES uploads(identifier) ON DELETE CASCADE
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_upc ON uploads(upc)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_qobuz ON uploads(qobuz_id)")

    def is_uploaded(
        self,
        key_or_id: str,
        expected_files: Optional[Iterable[str]] = None,
    ) -> bool:
        """
        Checks local SQLite state. Returns True ONLY if:
        1. An item matching identifier, UPC, or qobuz_id has status='completed'.
        2. Its file_manifest table entries contain ALL expected_files.
        """
        if not key_or_id:
            return False

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT identifier FROM uploads 
                WHERE (identifier = ? OR upc = ? OR qobuz_id = ?) AND status = 'completed'
                """,
                (key_or_id, key_or_id, key_or_id),
            )
            row = cur.fetchone()
            if not row:
                return False

            matched_id = row["identifier"]

            if not expected_files:
                return True

            cur.execute(
                "SELECT file_name FROM file_manifest WHERE identifier = ? AND status = 'completed'",
                (matched_id,),
            )
            stored_files = {r["file_name"] for r in cur.fetchall()}

            # Validate that all expected files exist in local manifest record
            return set(expected_files).issubset(stored_files)

    def record_upload_state(
        self,
        identifier: str,
        upc: str = "",
        qobuz_id: str = "",
        artist: str = "",
        title: str = "",
        status: str = "completed",
        script_version: str = "2.1.2",
        script_hash: str = "custom",
        qobuz_raw: Optional[dict] = None,
        mb_raw: Optional[dict] = None,
        wiki_raw: Optional[str] = None,
        ia_payload: Optional[dict] = None,
    ) -> None:
        q_json = json.dumps(qobuz_raw, default=str) if qobuz_raw is not None else None
        mb_json = json.dumps(mb_raw, default=str) if mb_raw is not None else None
        wiki_json = (
            json.dumps({"extract": wiki_raw})
            if isinstance(wiki_raw, str)
            else (json.dumps(wiki_raw, default=str) if wiki_raw is not None else None)
        )
        ia_json = json.dumps(ia_payload, default=str) if ia_payload is not None else None

        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO uploads (
                    identifier, upc, qobuz_id, artist, title, status, 
                    script_version, script_hash, qobuz_raw_json, mb_raw_json, 
                    wiki_raw_json, ia_payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identifier) DO UPDATE SET
                    upc=COALESCE(NULLIF(excluded.upc, ''), uploads.upc),
                    qobuz_id=COALESCE(NULLIF(excluded.qobuz_id, ''), uploads.qobuz_id),
                    artist=COALESCE(NULLIF(excluded.artist, ''), uploads.artist),
                    title=COALESCE(NULLIF(excluded.title, ''), uploads.title),
                    status=excluded.status,
                    script_version=excluded.script_version,
                    script_hash=excluded.script_hash,
                    qobuz_raw_json=COALESCE(excluded.qobuz_raw_json, uploads.qobuz_raw_json),
                    mb_raw_json=COALESCE(excluded.mb_raw_json, uploads.mb_raw_json),
                    wiki_raw_json=COALESCE(excluded.wiki_raw_json, uploads.wiki_raw_json),
                    ia_payload_json=COALESCE(excluded.ia_payload_json, uploads.ia_payload_json),
                    updated_at=CURRENT_TIMESTAMP
            """, (
                identifier, upc, qobuz_id, artist, title, status, script_version, script_hash,
                q_json, mb_json, wiki_json, ia_json
            ))

    def record_file_manifest(
        self,
        identifier: str,
        manifest_records: Union[List[dict], Iterable[str]],
        status: str = "completed",
    ) -> None:
        with self._get_connection() as conn:
            conn.execute("DELETE FROM file_manifest WHERE identifier = ?", (identifier,))
            if manifest_records and isinstance(next(iter(manifest_records), None), dict):
                conn.executemany("""
                    INSERT INTO file_manifest (
                        identifier, file_name, file_size, md5_hash, sha256_hash, 
                        audio_spec, raw_flac_tags_json, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    (
                        identifier,
                        rec.get("file_name", ""),
                        rec.get("file_size", 0),
                        rec.get("md5_hash", ""),
                        rec.get("sha256_hash", ""),
                        rec.get("audio_spec", ""),
                        rec.get("raw_flac_tags_json", "{}"),
                        status
                    ) for rec in manifest_records
                ])
            else:
                conn.executemany("""
                    INSERT INTO file_manifest (identifier, file_name, status)
                    VALUES (?, ?, ?)
                """, [(identifier, fname, status) for fname in manifest_records])

    def mark_uploaded(
        self,
        identifier: str,
        files: Iterable[str],
        metadata: Optional[Dict[str, Any]] = None,
        upc: Optional[str] = None,
        qobuz_id: Optional[str] = None,
        status: str = "completed",
    ) -> None:
        self.record_upload_state(
            identifier=identifier,
            upc=upc or "",
            qobuz_id=qobuz_id or "",
            status=status,
            ia_payload=metadata,
        )
        self.record_file_manifest(identifier, files, status=status)

    @staticmethod
    def extract_file_metadata(file_path: Path) -> dict:
        md5 = hashlib.md5()
        sha256 = hashlib.sha256()
        size = file_path.stat().st_size

        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(131072), b""):
                md5.update(chunk)
                sha256.update(chunk)

        raw_tags = {}
        audio_spec = ""
        if FLAC and file_path.suffix.lower() == ".flac":
            try:
                audio = FLAC(str(file_path))
                bit_depth = getattr(audio.info, "bits_per_sample", 0)
                sample_rate = getattr(audio.info, "sample_rate", 0)
                length = int(getattr(audio.info, "length", 0))
                if bit_depth and sample_rate:
                    audio_spec = f"{bit_depth}-bit / {sample_rate} Hz / {length}s"

                for key, val in audio.tags:
                    raw_tags[key] = val if len(val) > 1 else (val[0] if val else "")
            except Exception:
                pass

        return {
            "file_name": file_path.name,
            "file_size": size,
            "md5_hash": md5.hexdigest(),
            "sha256_hash": sha256.hexdigest(),
            "audio_spec": audio_spec,
            "raw_flac_tags_json": json.dumps(raw_tags),
        }


# Standard aliases for compatibility
Store = SQLiteStateStore
StateStore = SQLiteStateStore
