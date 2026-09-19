from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import urllib.request
from urllib.parse import unquote, quote
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_DIR = Path.home() / ".config" / "archive_uploader"
DB_FILE = STATE_DIR / "uploader.db"
SYNC_PORT = 47321
PROTOCOL = 1


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def state_files() -> list[Path]:
    if not STATE_DIR.exists():
        return []

    result = []

    for path in STATE_DIR.rglob("*"):
        if not path.is_file():
            continue

        # Never synchronize credentials or the identity of this device.
        rel = path.relative_to(STATE_DIR)

        if rel.name in {"secrets.json", "device_id"}:
            continue

        result.append(rel)

    return sorted(result)


def inventory() -> dict:
    files = {}

    for rel in state_files():
        path = STATE_DIR / rel
        stat = path.stat()

        files[str(rel)] = {
            "size": stat.st_size,
            "sha256": sha256(path),
        }

    return {
        "protocol": PROTOCOL,
        "files": files,
    }


def _merge_database(remote_db: bytes) -> None:
    """
    Merge the remote uploader.db logically instead of replacing our DB.

    Both known tables are merged. The remote database is never opened
    directly against the live database.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        dir=STATE_DIR,
        suffix=".db",
        delete=False,
    ) as tmp:
        tmp.write(remote_db)
        remote_path = Path(tmp.name)

    try:
        local = sqlite3.connect(DB_FILE)
        remote = sqlite3.connect(remote_path)

        local.row_factory = sqlite3.Row
        remote.row_factory = sqlite3.Row

        try:
            local.execute("PRAGMA foreign_keys = ON")
            local.execute("PRAGMA busy_timeout = 10000")

            local.execute("""
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

            local.execute("""
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
                    FOREIGN KEY(identifier)
                        REFERENCES uploads(identifier)
                        ON DELETE CASCADE
                )
            """)

            local.execute("""
                CREATE INDEX IF NOT EXISTS idx_upc
                ON uploads(upc)
            """)

            local.execute("""
                CREATE INDEX IF NOT EXISTS idx_qobuz
                ON uploads(qobuz_id)
            """)

            # Merge uploads.
            for row in remote.execute("SELECT * FROM uploads"):
                local.execute("""
                    INSERT INTO uploads (
                        identifier,
                        upc,
                        qobuz_id,
                        artist,
                        title,
                        status,
                        script_version,
                        script_hash,
                        qobuz_raw_json,
                        mb_raw_json,
                        wiki_raw_json,
                        ia_payload_json,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(identifier) DO UPDATE SET
                        upc = COALESCE(
                            NULLIF(excluded.upc, ''),
                            uploads.upc
                        ),
                        qobuz_id = COALESCE(
                            NULLIF(excluded.qobuz_id, ''),
                            uploads.qobuz_id
                        ),
                        artist = COALESCE(
                            NULLIF(excluded.artist, ''),
                            uploads.artist
                        ),
                        title = COALESCE(
                            NULLIF(excluded.title, ''),
                            uploads.title
                        ),
                        status = CASE
                            WHEN uploads.status = 'completed'
                              OR excluded.status = 'completed'
                            THEN 'completed'
                            ELSE excluded.status
                        END,
                        script_version = COALESCE(
                            excluded.script_version,
                            uploads.script_version
                        ),
                        script_hash = COALESCE(
                            excluded.script_hash,
                            uploads.script_hash
                        ),
                        qobuz_raw_json = COALESCE(
                            excluded.qobuz_raw_json,
                            uploads.qobuz_raw_json
                        ),
                        mb_raw_json = COALESCE(
                            excluded.mb_raw_json,
                            uploads.mb_raw_json
                        ),
                        wiki_raw_json = COALESCE(
                            excluded.wiki_raw_json,
                            uploads.wiki_raw_json
                        ),
                        ia_payload_json = COALESCE(
                            excluded.ia_payload_json,
                            uploads.ia_payload_json
                        ),
                        updated_at = CASE
                            WHEN excluded.updated_at > uploads.updated_at
                            THEN excluded.updated_at
                            ELSE uploads.updated_at
                        END
                """, tuple(row))

            # Merge file manifests.
            #
            # A manifest row is identified logically by:
            # identifier + file_name.
            for row in remote.execute("""
                SELECT
                    identifier,
                    file_name,
                    file_size,
                    md5_hash,
                    sha256_hash,
                    audio_spec,
                    raw_flac_tags_json,
                    status
                FROM file_manifest
            """):
                local.execute("""
                    DELETE FROM file_manifest
                    WHERE identifier = ?
                      AND file_name = ?
                """, (
                    row["identifier"],
                    row["file_name"],
                ))

                local.execute("""
                    INSERT INTO file_manifest (
                        identifier,
                        file_name,
                        file_size,
                        md5_hash,
                        sha256_hash,
                        audio_spec,
                        raw_flac_tags_json,
                        status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    row["identifier"],
                    row["file_name"],
                    row["file_size"],
                    row["md5_hash"],
                    row["sha256_hash"],
                    row["audio_spec"],
                    row["raw_flac_tags_json"],
                    row["status"],
                ))

            local.commit()

        finally:
            local.close()
            remote.close()

    finally:
        remote_path.unlink(missing_ok=True)


class SyncHandler(BaseHTTPRequestHandler):

    def log_message(self, *_args):
        pass

    def _send(self, status: int, data: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/inventory":
            data = json.dumps(
                inventory(),
                separators=(",", ":"),
            ).encode()

            self._send(
                200,
                data,
                "application/json",
            )
            return

        if self.path.startswith("/file/"):
            relative = unquote(
                self.path.removeprefix("/file/")
            )
            path = STATE_DIR / relative

            try:
                path.relative_to(STATE_DIR)
            except ValueError:
                self.send_error(400)
                return

            if not path.is_file():
                self.send_error(404)
                return

            self._send(
                200,
                path.read_bytes(),
                "application/octet-stream",
            )
            return

        self.send_error(404)

    def do_PUT(self):
        if not self.path.startswith("/file/"):
            self.send_error(404)
            return

        relative = self.path.removeprefix("/file/")
        path = STATE_DIR / relative

        try:
            path.relative_to(STATE_DIR)
        except ValueError:
            self.send_error(400)
            return

        if path.name in {"secrets.json", "device_id"}:
            self.send_error(403)
            return

        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length)

        path.parent.mkdir(parents=True, exist_ok=True)

        # SQLite is merged logically rather than overwritten.
        if path.name == "uploader.db":
            _merge_database(data)
        else:
            path.write_bytes(data)

        self._send(204, b"", "text/plain")


def serve(host: str = "0.0.0.0", port: int = SYNC_PORT):
    server = ThreadingHTTPServer(
        (host, port),
        SyncHandler,
    )

    print(f"State sync listening on {host}:{port}")

    try:
        server.serve_forever()
    finally:
        server.server_close()


def sync(peer: str, port: int = SYNC_PORT):
    if ":" in peer:
        host, possible_port = peer.rsplit(":", 1)

        try:
            port = int(possible_port)
            peer = host
        except ValueError:
            pass

    base = f"http://{peer}:{port}"

    with urllib.request.urlopen(
        f"{base}/inventory",
        timeout=15,
    ) as response:
        remote = json.loads(response.read())

    if remote.get("protocol") != PROTOCOL:
        raise RuntimeError("Incompatible sync protocol")

    remote_files = remote["files"]

    # state_files() returns paths relative to STATE_DIR.
    # Convert them to actual filesystem paths here.
    local_files = {
        str(rel): STATE_DIR / rel
        for rel in state_files()
    }

    # ---------------------------------------------------------------
    # Download files that are missing or differ locally.
    # ---------------------------------------------------------------

    for name, metadata in remote_files.items():

        # SQLite is handled separately below.
        if name == "uploader.db":
            continue

        local = local_files.get(name)

        if (
            local is not None
            and local.exists()
            and local.stat().st_size == metadata["size"]
            and sha256(local) == metadata["sha256"]
        ):
            continue

        url_name = quote(name, safe="/")

        with urllib.request.urlopen(
            f"{base}/file/{url_name}",
            timeout=120,
        ) as response:
            data = response.read()

        destination = STATE_DIR / name
        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        destination.write_bytes(data)

        print(f"← {name}")

    # ---------------------------------------------------------------
    # Upload local files that are missing/different on the peer.
    # ---------------------------------------------------------------

    local_inventory = inventory()

    for name, metadata in local_inventory["files"].items():

        if name == "uploader.db":
            continue

        remote_metadata = remote_files.get(name)

        if (
            remote_metadata is not None
            and remote_metadata["size"] == metadata["size"]
            and remote_metadata["sha256"] == metadata["sha256"]
        ):
            continue

        data = (STATE_DIR / name).read_bytes()

        url_name = quote(name, safe="/")

        request = urllib.request.Request(
            f"{base}/file/{url_name}",
            data=data,
            method="PUT",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(data)),
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=120,
        ):
            pass

        print(f"→ {name}")

    # ---------------------------------------------------------------
    # SQLite database.
    #
    # The peer receives our DB and merges it into its own DB.
    # We never overwrite the live DB wholesale.
    # ---------------------------------------------------------------

    local_db = DB_FILE

    if local_db.exists():
        data = local_db.read_bytes()

        request = urllib.request.Request(
            f"{base}/file/uploader.db",
            data=data,
            method="PUT",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(data)),
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=120,
        ):
            pass

    print("State synchronization complete.")
