"""
Central configuration: filesystem layout and device identity.

Everything else in the package imports paths from here rather than
building its own — that's the seam that keeps "where does state live"
a one-line change instead of a grep-and-replace.
"""
from __future__ import annotations

import json
import os
import platform
import uuid
from pathlib import Path

# --------------------------------------------------------------------------
# Filesystem layout
# --------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".config" / "archive_uploader"
STATE_DIR = CONFIG_DIR / "state"
LOGS_DIR = STATE_DIR / "logs"          # per-device append-only event logs
BACKUPS_DIR = STATE_DIR / "backups"    # numbered log snapshots
CACHE_DIR = CONFIG_DIR / "metadata_cache"  # raw provider responses, never hand-edited
TEMP_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "archive_uploader"

for d in (CONFIG_DIR, STATE_DIR, LOGS_DIR, BACKUPS_DIR, CACHE_DIR, TEMP_DIR):
    d.mkdir(parents=True, exist_ok=True)

DEVICE_ID_FILE = CONFIG_DIR / "device_id"
SECRETS_FILE = CONFIG_DIR / "secrets.json"

# --------------------------------------------------------------------------
# Device identity
# --------------------------------------------------------------------------

def get_device_id() -> str:
    """
    Stable per-device id. Each device only ever writes to its own log file
    (see state/log_store.py), so this id is the sharding key that makes
    concurrent multi-device writes safe without any locking or server.
    """
    override = os.environ.get("ARCHIVE_UPLOADER_DEVICE_ID")
    if override:
        return override

    if DEVICE_ID_FILE.exists():
        return DEVICE_ID_FILE.read_text().strip()

    host = platform.node().split(".")[0].lower() or "device"
    device_id = f"{host}-{uuid.uuid4().hex[:8]}"
    DEVICE_ID_FILE.write_text(device_id)
    return device_id

# --------------------------------------------------------------------------
# Provider/service secrets
# --------------------------------------------------------------------------

def get_secret(service: str, key: str) -> str:
    """
    General-purpose credential lookup for any API-key-requiring provider
    (Discogs, Last.fm, mega.nz, whatever comes next — one mechanism for
    all of them instead of each provider rolling its own). Checks, in
    order:

      1. the ARCHIVE_UPLOADER_<SERVICE>_<KEY> environment variable
         (e.g. ARCHIVE_UPLOADER_DISCOGS_TOKEN, ARCHIVE_UPLOADER_MEGA_PASSWORD)
      2. ~/.config/archive_uploader/secrets.json, shaped like:
           {
             "discogs": {"token": "..."},
             "lastfm": {"api_key": "..."},
             "mega": {"email": "...", "password": "..."}
           }

    Returns "" if neither is set. Callers treat that as "no credential" —
    fetch() implementations should fall back to unauthenticated/keyless
    behavior or return None on a missing secret, never raise.
    """
    env_name = f"ARCHIVE_UPLOADER_{service.upper()}_{key.upper()}"
    env_val = os.environ.get(env_name)
    if env_val:
        return env_val

    if SECRETS_FILE.exists():
        try:
            data = json.loads(SECRETS_FILE.read_text())
            return str(data.get(service, {}).get(key, "") or "")
        except (json.JSONDecodeError, OSError):
            pass
    return ""

# --------------------------------------------------------------------------
# Misc shared constants
# --------------------------------------------------------------------------

SYSTEM_EXCLUDES = {".ds_store", "thumbs.db", "desktop.ini", "@eadir"}


def is_valid_asset(p: Path) -> bool:
    if p.name.startswith("."):
        return False
    if p.name.lower() in SYSTEM_EXCLUDES:
        return False
    return True
